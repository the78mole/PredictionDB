"""Hub'eau Hydrométrie (eaufrance): Abfluss an den Pegeln der französischen Kernkraftwerke.

API ``https://hubeau.eaufrance.fr/api/v2/hydrometrie`` ohne Schlüssel, Antworten als JSON
mit Cursor-Blättern (``next``), bis zu 20 000 Zeilen je Seite. Lizenz: Licence Ouverte 2.0
(Etalab), Quellenangabe „Hub'eau / eaufrance“ (https://hubeau.eaufrance.fr). Die Pegel stehen
in ``model.RIVER_GAUGES``, ausgewählt in ``docs/analysen/kkw-kuehlwasser-fr``.

- ``flow_day_<pegel>``: Tagesmittel ``QmnJ`` aus ``obs_elab`` (Tag in UTC), Reihe
  ``river_flow_<pegel>``. Historie meist über Jahrzehnte (Saint-Rambert erst ab 07/2024),
  aktuell bis zum Vortag, an manchen Pegeln (z. B. Lagnieu, CNR) einige Tage später.
  Rohwerte (Statut 4) werden in den Folgetagen korrigiert (Statut 8). Der Abgleich lädt
  deshalb zehn Tage zurück neu.
- ``flow_hour_<pegel>``: Stundenmittel aus den Echtzeitwerten ``observations_tr``, Reihe
  ``river_flow_<pegel>_h``. Hub'eau hält nur etwa einen Monat vor; ältere Blöcke werden ohne
  Abruf leer beantwortet. Eine Stunde gilt erst als vollständig, wenn ein späterer Wert da ist.

Werte kommen in l/s und werden in m³/s umgerechnet. Hub'eau liefert je Zeitpunkt die Reihe des
Standorts (``code_station`` leer) und die der einzelnen Stationen; genommen wird die des
Standorts, ohne sie die der ersten Station.

@relation(SWA-ENI-SRC-HUBEAU, scope=file)
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any

from energy_ingest.http import Http
from energy_ingest.model import DAY, HOUR, RIVER_GAUGES, SeriesData, floor_to, series
from energy_ingest.sources import FormatError, SourceError, clip

BASE_URL = "https://hubeau.eaufrance.fr/api/v2/hydrometrie"
ELAB_URL = f"{BASE_URL}/obs_elab"
TR_URL = f"{BASE_URL}/observations_tr"
PAGE = 20000
#: So weit reichen die Echtzeitwerte bei Hub'eau sicher zurück (gut einen Monat)
TR_HISTORY = timedelta(days=28)
LOOKBACK_DAY = timedelta(days=10)
L_PER_M3 = 1000.0

Point = tuple[datetime, float]
Candidate = tuple[datetime, str, float]  # Zeitpunkt, Station ("" = Standort), m³/s


def _rows(body: Any) -> tuple[list[dict[str, Any]], str | None]:
    if not isinstance(body, dict) or not isinstance(body.get("data"), list):
        raise FormatError(f"Hub'eau: unerwartete Antwort {str(body)[:200]!r}")
    nxt = body.get("next")
    if nxt is not None and not isinstance(nxt, str):
        raise FormatError(f"Hub'eau: Verweis next {nxt!r} nicht lesbar")
    rows = body["data"]
    if not all(isinstance(r, dict) for r in rows):
        raise FormatError("Hub'eau: Datensatz ist kein Objekt")
    return rows, nxt or None


def _value(row: dict[str, Any], field: str) -> float | None:
    if field not in row:
        raise FormatError(f"Hub'eau: Feld {field!r} fehlt")
    raw = row[field]
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, int | float):
        raise FormatError(f"Hub'eau: Wert {raw!r} nicht lesbar")
    return float(raw) / L_PER_M3


def _candidates(
    rows: list[dict[str, Any]], when: Callable[[dict[str, Any]], datetime], field: str
) -> list[Candidate]:
    out: list[Candidate] = []
    for row in rows:
        v = _value(row, field)
        if v is not None:
            station = row.get("code_station")
            out.append((when(row), "" if station is None else str(station), v))
    return out


def prefer_site(cands: list[Candidate]) -> list[Point]:
    """Je Zeitpunkt die Reihe des Standorts, sonst die der ersten Station (nach Code).

    @relation(SWR-ENI-053, scope=function)
    """
    best: dict[datetime, tuple[str, float]] = {}
    for ts, station, v in cands:
        if ts not in best or station < best[ts][0]:
            best[ts] = (station, v)
    return sorted((ts, v) for ts, (_, v) in best.items())


def _day(row: dict[str, Any]) -> datetime:
    try:
        d = date.fromisoformat(str(row["date_obs_elab"])[:10])
    except (KeyError, ValueError) as exc:
        raise FormatError(f"Hub'eau: Datum {row.get('date_obs_elab')!r} nicht lesbar") from exc
    return datetime(d.year, d.month, d.day, tzinfo=UTC)  # Tagesmittel über den Tag in UTC


def _instant(row: dict[str, Any]) -> datetime:
    try:
        ts = datetime.fromisoformat(str(row["date_obs"]).replace("Z", "+00:00"))
    except (KeyError, ValueError) as exc:
        raise FormatError(f"Hub'eau: Zeitpunkt {row.get('date_obs')!r} nicht lesbar") from exc
    if ts.tzinfo is None:
        raise FormatError(f"Hub'eau: Zeitpunkt {row['date_obs']!r} ohne Zeitzone")
    return ts.astimezone(UTC)


def parse_elab(body: Any) -> tuple[list[Candidate], str | None]:
    """Eine Seite ``obs_elab``: Tagesmittel in m³/s je Tagesbeginn (UTC) und nächste Seite.

    @relation(SWR-ENI-053, SWR-ENI-030, scope=function)
    """
    rows, nxt = _rows(body)
    return _candidates(rows, _day, "resultat_obs_elab"), nxt


def parse_tr(body: Any) -> tuple[list[Candidate], str | None]:
    """Eine Seite ``observations_tr``: Einzelwerte in m³/s und nächste Seite.

    @relation(SWR-ENI-053, SWR-ENI-030, scope=function)
    """
    rows, nxt = _rows(body)
    return _candidates(rows, _instant, "resultat_obs"), nxt


def hourly_means(points: list[Point]) -> list[Point]:
    """Mittel je Stunde; die Stunde des letzten Werts fehlt, bis ein späterer Wert kommt.

    @relation(SWR-ENI-053, SWR-ENI-020, scope=function)
    """
    if not points:
        return []
    last = max(ts for ts, _ in points)
    buckets: dict[datetime, list[float]] = defaultdict(list)
    for ts, v in points:
        buckets[floor_to(ts, HOUR)].append(v)
    return sorted((h, sum(vs) / len(vs)) for h, vs in buckets.items() if h + HOUR <= last)


class Hubeau:
    """@relation(SWR-ENI-053, scope=class)"""

    code = "hubeau"
    license = "Licence Ouverte 2.0 (Etalab), Hub'eau / eaufrance"
    priority = 10
    free = True
    train = True

    def __init__(self, http: Http, now: Callable[[], datetime] | None = None) -> None:
        self.http = http
        self.now = now or (lambda: datetime.now(UTC))
        self.sync_lookback = {f"flow_day_{g}": LOOKBACK_DAY for g in RIVER_GAUGES}

    def datasets(self) -> list[str]:
        return [f"flow_{k}_{g}" for g in RIVER_GAUGES for k in ("day", "hour")]

    def max_chunk(self, dataset: str) -> timedelta:
        return 5 * 365 * DAY if dataset.startswith("flow_day_") else 7 * DAY

    def _pages(
        self,
        url: str,
        params: dict[str, Any],
        parse: Callable[[Any], tuple[list[Candidate], str | None]],
    ) -> list[Point]:
        """Alle Seiten; die Wahl der Reihe je Zeitpunkt erst danach (über Seitengrenzen)."""
        out: list[Candidate] = []
        nxt: str | None = url
        query: dict[str, Any] | None = params
        while nxt:
            r = self.http.get(nxt, query)
            if r.status_code >= 400:
                raise SourceError(f"Hub'eau: {r.status_code} {r.text[:200]}")
            pts, nxt = parse(r.json())
            out.extend(pts)
            query = None  # der Verweis next enthält alle Parameter
        return prefer_site(out)

    def fetch(self, dataset: str, start: datetime, end: datetime) -> list[SeriesData]:
        if dataset not in self.datasets():
            raise KeyError(dataset)
        kind, gauge = dataset.removeprefix("flow_").split("_", 1)
        site = RIVER_GAUGES[gauge][0]
        if kind == "day":
            pts = self._pages(
                ELAB_URL,
                {
                    "code_entite": site,
                    "grandeur_hydro_elab": "QmnJ",
                    "date_debut_obs_elab": start.date().isoformat(),
                    "date_fin_obs_elab": (end - timedelta(microseconds=1)).date().isoformat(),
                    "size": PAGE,
                },
                parse_elab,
            )
            pts = clip(pts, start, end)
            return [SeriesData(series(f"river_flow_{gauge}"), DAY, pts)] if pts else []
        start = max(start, floor_to(self.now() - TR_HISTORY, HOUR))
        if end <= start:
            return []
        raw = self._pages(
            TR_URL,
            {
                "code_entite": site,
                "grandeur_hydro": "Q",
                "date_debut_obs": start.isoformat().replace("+00:00", "Z"),
                "date_fin_obs": (end + HOUR).isoformat().replace("+00:00", "Z"),
                "size": PAGE,
                "sort": "asc",
            },
            parse_tr,
        )
        pts = clip(hourly_means(raw), start, end)
        return [SeriesData(series(f"river_flow_{gauge}_h"), HOUR, pts)] if pts else []
