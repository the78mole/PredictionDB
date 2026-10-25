"""Gas Infrastructure Europe (GIE), AGSI+: Füllstände der Gasspeicher je Gastag.

API ``https://agsi.gie.eu/api`` mit kostenlosem Schlüssel (Header ``x-key``, Registrierung
unter https://agsi.gie.eu/account, ``PDB_GIE_API_KEY``). Höchstens 60 Abrufe je Minute, je
Seite bis zu 300 Gastage. Werte ab 2011, laut GIE vollständig ab 2016. GIE aktualisiert
täglich um 19:30 und 23:00 (MEZ) mit dem Stand des vorigen Gastags.

Nutzung: "All data published on AGSI & ALSI can be used or repackaged in any way you see
fit", Quellenangabe GIE/AGSI ist Pflicht (https://agsi.gie.eu/account). Eine formale Lizenz
gibt es nicht, die Weiterverwendung ist aber uneingeschränkt; die Quelle gilt deshalb als frei.

Je Gebiet (``model.GAS_STORAGE_AREAS``, Datensatz ``storage_<gebiet>``) entstehen die Reihen
``gas_storage_<gebiet>`` (Füllstand am Ende des Gastags, TWh), ``gas_storage_full_<gebiet>``
(Füllgrad, %), ``gas_injection_<gebiet>`` und ``gas_withdrawal_<gebiet>`` (GWh/d) sowie
``gas_storage_wgv_<gebiet>`` (Arbeitsgasvolumen, TWh). Sie stehen am Beginn des Gastags
(06:00 Ortszeit, als UTC). Gastage mit Status ``N`` (keine Daten) entfallen.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any, ClassVar

from energy_ingest.http import Http
from energy_ingest.model import DAY, GAS_STORAGE_AREAS, SeriesData, series
from energy_ingest.sources import FormatError, SourceError, clip
from energy_ingest.sources.the import gas_day_start

URL = "https://agsi.gie.eu/api"
PAGE = 300

# Feld der API -> Reihen-Präfix
FIELDS = {
    "gasInStorage": "gas_storage",
    "full": "gas_storage_full",
    "injection": "gas_injection",
    "withdrawal": "gas_withdrawal",
    "workingGasVolume": "gas_storage_wgv",
}
STATUS = {"C", "E", "N"}  # bestätigt, geschätzt, keine Daten


def _number(value: Any) -> float | None:
    """Zahlen kommen als Zeichenkette; ``-`` und leere Felder bedeuten keinen Wert."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    text = str(value).strip()
    if text in ("", "-"):
        return None
    try:
        return float(text)
    except ValueError as exc:
        raise FormatError(f"AGSI: Wert {value!r} nicht lesbar") from exc


def parse_page(body: Any) -> tuple[int, dict[str, list[tuple[datetime, float]]]]:
    """Eine Seite der API; gibt die letzte Seite und die Punkte je Reihen-Präfix zurück."""
    if not isinstance(body, dict) or not isinstance(body.get("data"), list):
        raise FormatError(f"AGSI: unerwartete Antwort {str(body)[:200]!r}")
    if body.get("error"):
        # Falscher Schlüssel kommt mit HTTP 200 und "error": "access denied"
        raise SourceError(f"AGSI: {body.get('error')} {body.get('message', '')}".strip())
    out: dict[str, list[tuple[datetime, float]]] = {p: [] for p in FIELDS.values()}
    for row in body["data"]:
        if not isinstance(row, dict) or "gasDayStart" not in row:
            raise FormatError(f"AGSI: Datensatz ohne gasDayStart {str(row)[:200]!r}")
        missing = [f for f in FIELDS if f not in row]
        if missing:
            raise FormatError(f"AGSI: Felder fehlen {missing}")
        status = str(row.get("status", "")).strip()
        if status not in STATUS:
            raise FormatError(f"AGSI: unbekannter Status {status!r}")
        if status == "N":
            continue
        ts = gas_day_start(date.fromisoformat(str(row["gasDayStart"])[:10]))
        for field, prefix in FIELDS.items():
            v = _number(row[field])
            if v is not None:
                out[prefix].append((ts, v))
    return int(body.get("last_page") or 0), out


class GieAgsi:
    code = "gie_agsi"
    license = "GIE AGSI+, Weiterverwendung frei mit Quellenangabe (GIE AGSI+)"
    priority = 10
    free = True
    # Geschätzte Werte (Status E) werden in den Folgetagen bestätigt oder korrigiert
    sync_lookback: ClassVar[dict[str, timedelta]] = {
        f"storage_{a}": timedelta(days=14) for a in GAS_STORAGE_AREAS
    }

    def __init__(self, http: Http, api_key: str, url: str = URL) -> None:
        self.http = http
        self.api_key = api_key
        self.url = url

    def datasets(self) -> list[str]:
        return [f"storage_{a}" for a in GAS_STORAGE_AREAS]

    def max_chunk(self, dataset: str) -> timedelta:
        return timedelta(days=PAGE - 10)  # eine Seite je Block

    def fetch(self, dataset: str, start: datetime, end: datetime) -> list[SeriesData]:
        area = dataset.removeprefix("storage_")
        if not dataset.startswith("storage_") or area not in GAS_STORAGE_AREAS:
            raise KeyError(dataset)
        params: dict[str, Any] = {
            **GAS_STORAGE_AREAS[area][0],
            "from": (start - DAY).date().isoformat(),
            "to": (end + DAY).date().isoformat(),
            "size": PAGE,
        }
        found: dict[str, list[tuple[datetime, float]]] = {p: [] for p in FIELDS.values()}
        page = 1
        while True:
            r = self.http.get(self.url, {**params, "page": page}, {"x-key": self.api_key})
            if r.status_code >= 400:
                raise SourceError(f"AGSI {dataset}: {r.status_code} {r.text[:200]}")
            last, part = parse_page(r.json())
            for prefix, pts in part.items():
                found[prefix].extend(pts)
            page += 1
            if page > last:
                break
        out = []
        for prefix, pts in found.items():
            pts = clip(sorted(set(pts)), start, end)
            if pts:
                out.append(SeriesData(series(f"{prefix}_{area}"), DAY, pts))
        return out
