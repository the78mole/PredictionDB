"""Eurostat: Gasbestand am Monatsende (Datensatz ``nrg_stk_gasm``), ab 2008.

Statistics API (JSON-stat 2.0) ohne Schlüssel, Lizenz CC BY 4.0
(https://ec.europa.eu/eurostat/about-us/policies/copyright). Gelesen wird der Schlussbestand
Erdgas im Inland (``stk_flow=STKCL_NAT``, ``siec=G3000``) in Terajoule (Brennwert) und in TWh
umgerechnet. Eurostat veröffentlicht mit rund zwei Monaten Verzug und überarbeitet ältere
Monate; die Reihe dient als Redundanz und Plausibilisierung der Tageswerte von AGSI+.

Reihen ``gas_storage_<gebiet>_m`` je Gebiet aus ``model.GAS_STORAGE_AREAS``. Der Bestand am
Ende eines Monats steht am Beginn des Folgemonats (00:00 Ortszeit, als UTC).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar

from energy_ingest.http import Http
from energy_ingest.model import BERLIN, GAS_STORAGE_AREAS, MONTH, SeriesData, next_period, series
from energy_ingest.sources import FormatError, SourceError, clip

URL = "https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data/nrg_stk_gasm"
FILTER = {"freq": "M", "stk_flow": "STKCL_NAT", "siec": "G3000", "unit": "TJ_GCV"}
TJ_PER_TWH = 3600.0


def _month(text: str) -> datetime:
    if len(text) != 7 or text[4] != "-":
        raise FormatError(f"Eurostat: Zeitraum {text!r} ist kein Monat")
    return datetime(int(text[:4]), int(text[5:]), 1, tzinfo=BERLIN).astimezone(UTC)


def parse_json(body: Any) -> dict[str, list[tuple[datetime, float]]]:
    """JSON-stat-Antwort -> Punkte je Geo-Code (TWh, Zeitstempel am Monatsende)."""
    try:
        ids: list[str] = body["id"]
        sizes: list[int] = body["size"]
        dims = {k: body["dimension"][k]["category"]["index"] for k in ids}
        values: dict[str, float | None] = body["value"]
    except (KeyError, TypeError) as exc:
        raise FormatError(f"Eurostat: unerwartete Antwort {str(body)[:200]!r}") from exc
    if set(ids) != {*FILTER, "geo", "time"}:
        raise FormatError(f"Eurostat: Dimensionen {ids}")
    for dim, code in FILTER.items():
        if list(dims[dim]) != [code]:
            raise FormatError(f"Eurostat: {dim} = {list(dims[dim])}, erwartet {code!r}")
    strides = [1] * len(ids)
    for i in range(len(ids) - 2, -1, -1):
        strides[i] = strides[i + 1] * sizes[i + 1]
    pos = {k: strides[i] for i, k in enumerate(ids)}
    out: dict[str, list[tuple[datetime, float]]] = {}
    for geo, gi in dims["geo"].items():
        pts = []
        for period, ti in dims["time"].items():
            v = values.get(str(gi * pos["geo"] + ti * pos["time"]))
            if v is not None:
                pts.append((next_period(_month(period), 1), float(v) / TJ_PER_TWH))
        out[geo] = sorted(pts)
    return out


class Eurostat:
    code = "eurostat"
    license = "CC BY 4.0, Eurostat"
    priority = 10
    free = True
    # Eurostat überarbeitet zurückliegende Monate
    sync_lookback: ClassVar[dict[str, timedelta]] = {"gas_stock": timedelta(days=400)}

    def __init__(self, http: Http, url: str = URL) -> None:
        self.http = http
        self.url = url

    def datasets(self) -> list[str]:
        return ["gas_stock"]

    def max_chunk(self, dataset: str) -> timedelta:
        return timedelta(days=3650)

    def fetch(self, dataset: str, start: datetime, end: datetime) -> list[SeriesData]:
        if dataset != "gas_stock":
            raise KeyError(dataset)
        geos = {geo: area for area, (_, geo) in GAS_STORAGE_AREAS.items()}
        # Zeitstempel liegen am Folgemonat: der Monat davor gehört noch ins Fenster
        first = (start.astimezone(BERLIN) - MONTH).strftime("%Y-%m")
        last = (end.astimezone(BERLIN)).strftime("%Y-%m")
        params: list[tuple[str, str]] = [
            *FILTER.items(),
            *(("geo", g) for g in geos),
            ("sinceTimePeriod", first),
            ("untilTimePeriod", last),
            ("format", "JSON"),
            ("lang", "EN"),
        ]
        r = self.http.get(self.url, params)
        if r.status_code >= 400:
            raise SourceError(f"Eurostat: {r.status_code} {r.text[:200]}")
        found = parse_json(r.json())
        unknown = set(found) - set(geos)
        if unknown:
            raise FormatError(f"Eurostat: fremde Gebiete {sorted(unknown)}")
        out = []
        for geo, pts in found.items():
            pts = clip(pts, start, end)
            if pts:
                out.append(SeriesData(series(f"gas_storage_{geos[geo]}_m"), MONTH, pts))
        return out
