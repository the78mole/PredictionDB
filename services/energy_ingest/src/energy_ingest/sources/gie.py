"""Gas Infrastructure Europe (GIE): Gasspeicher (AGSI+) und LNG-Terminals (ALSI) je Gastag.

APIs ``https://agsi.gie.eu/api`` und ``https://alsi.gie.eu/api`` mit demselben kostenlosen
Schlüssel (Header ``x-key``, Registrierung unter https://agsi.gie.eu/account,
``PDB_GIE_API_KEY``). Höchstens 60 Abrufe je Minute, je Seite bis zu 300 Gastage. AGSI+ ab
2011 (laut GIE vollständig ab 2016), ALSI ab 2012. GIE aktualisiert täglich um 19:30 und
23:00 (MEZ) mit dem Stand des vorigen Gastags; ein falscher Schlüssel kommt mit HTTP 200 und
``"error": "access denied"``.

Nutzung: "All data published on AGSI & ALSI can be used or repackaged in any way you see
fit", Quellenangabe GIE/AGSI/ALSI ist Pflicht (https://agsi.gie.eu/account). Eine formale
Lizenz gibt es nicht, die Weiterverwendung ist aber uneingeschränkt; beide Quellen gelten
deshalb als frei.

Alle Zahlen kommen als Zeichenketten, ``-`` bedeutet keinen Wert; Gastage mit Status ``N``
(keine Daten) entfallen. Status ``E`` (geschätzt) wird in den Folgetagen bestätigt (``C``);
ALSI schreibt für DE geschätzte Tage teils mit dem Vortageswert fort. Der laufende Abgleich
lädt deshalb 14 Tage zurück neu. Werte stehen am Beginn des Gastags (06:00 Ortszeit, als UTC).

- ``gie_agsi``, Datensatz ``storage_<gebiet>`` (``model.GAS_STORAGE_AREAS``):
  ``gas_storage_<gebiet>`` (Füllstand am Ende des Gastags, TWh), ``gas_storage_full_<gebiet>``
  (Füllgrad, %), ``gas_injection_<gebiet>``, ``gas_withdrawal_<gebiet>`` (GWh/d),
  ``gas_storage_wgv_<gebiet>`` (Arbeitsgasvolumen, TWh).
- ``gie_alsi``, Datensatz ``lng_<gebiet>`` (``model.LNG_AREAS``): ``lng_inventory_<gebiet>``
  (LNG in den Tanks am Ende des Gastags, GWh), ``lng_sendout_<gebiet>`` (Ausspeisung ins Netz,
  GWh/d), ``lng_dtmi_<gebiet>`` (erklärte Tankkapazität, GWh), ``lng_dtrs_<gebiet>`` (erklärte
  Ausspeisekapazität, GWh/d).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any, ClassVar

from energy_ingest.http import Http
from energy_ingest.model import DAY, GAS_STORAGE_AREAS, LNG_AREAS, SeriesData, series
from energy_ingest.sources import FormatError, SourceError, clip
from energy_ingest.sources.the import gas_day_start

AGSI_URL = "https://agsi.gie.eu/api"
ALSI_URL = "https://alsi.gie.eu/api"
PAGE = 300
LOOKBACK = timedelta(days=14)

# Feld der API (verschachtelt mit Punkt) -> Reihen-Präfix
AGSI_FIELDS = {
    "gasInStorage": "gas_storage",
    "full": "gas_storage_full",
    "injection": "gas_injection",
    "withdrawal": "gas_withdrawal",
    "workingGasVolume": "gas_storage_wgv",
}
ALSI_FIELDS = {
    "inventory.gwh": "lng_inventory",
    "sendOut": "lng_sendout",
    "dtmi.gwh": "lng_dtmi",
    "dtrs": "lng_dtrs",
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
        raise FormatError(f"GIE: Wert {value!r} nicht lesbar") from exc


def _field(row: dict[str, Any], path: str) -> Any:
    value: Any = row
    for part in path.split("."):
        if not isinstance(value, dict) or part not in value:
            raise FormatError(f"GIE: Feld {path!r} fehlt")
        value = value[part]
    return value


def parse_page(
    body: Any, fields: dict[str, str] = AGSI_FIELDS
) -> tuple[int, dict[str, list[tuple[datetime, float]]]]:
    """Eine Seite der API; gibt die letzte Seite und die Punkte je Reihen-Präfix zurück."""
    if not isinstance(body, dict) or not isinstance(body.get("data"), list):
        raise FormatError(f"GIE: unerwartete Antwort {str(body)[:200]!r}")
    if body.get("error"):
        raise SourceError(f"GIE: {body.get('error')} {body.get('message', '')}".strip())
    out: dict[str, list[tuple[datetime, float]]] = {p: [] for p in fields.values()}
    for row in body["data"]:
        if not isinstance(row, dict) or "gasDayStart" not in row:
            raise FormatError(f"GIE: Datensatz ohne gasDayStart {str(row)[:200]!r}")
        status = str(row.get("status", "")).strip()
        if status not in STATUS:
            raise FormatError(f"GIE: unbekannter Status {status!r}")
        values = {prefix: _field(row, path) for path, prefix in fields.items()}
        if status == "N":
            continue
        ts = gas_day_start(date.fromisoformat(str(row["gasDayStart"])[:10]))
        for prefix, raw in values.items():
            v = _number(raw)
            if v is not None:
                out[prefix].append((ts, v))
    return int(body.get("last_page") or 0), out


class _Gie:
    """Gemeinsamer Abruf für AGSI+ und ALSI; ein Datensatz je Gebiet."""

    license = "GIE AGSI+/ALSI, Weiterverwendung frei mit Quellenangabe (GIE)"
    priority = 10
    free = True
    url: ClassVar[str]
    prefix: ClassVar[str]  # Datensatz <prefix>_<gebiet>
    areas: ClassVar[dict[str, dict[str, str]]]
    fields: ClassVar[dict[str, str]]

    def __init__(self, http: Http, api_key: str) -> None:
        self.http = http
        self.api_key = api_key
        self.sync_lookback = {d: LOOKBACK for d in self.datasets()}

    def datasets(self) -> list[str]:
        return [f"{self.prefix}_{a}" for a in self.areas]

    def max_chunk(self, dataset: str) -> timedelta:
        return timedelta(days=PAGE - 10)  # eine Seite je Block

    def fetch(self, dataset: str, start: datetime, end: datetime) -> list[SeriesData]:
        area = dataset.removeprefix(f"{self.prefix}_")
        if dataset not in self.datasets():
            raise KeyError(dataset)
        params: dict[str, Any] = {
            **self.areas[area],
            "from": (start - DAY).date().isoformat(),
            "to": (end + DAY).date().isoformat(),
            "size": PAGE,
        }
        found: dict[str, list[tuple[datetime, float]]] = {p: [] for p in self.fields.values()}
        page = 1
        while True:
            r = self.http.get(self.url, {**params, "page": page}, {"x-key": self.api_key})
            if r.status_code >= 400:
                raise SourceError(f"GIE {dataset}: {r.status_code} {r.text[:200]}")
            last, part = parse_page(r.json(), self.fields)
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


class GieAgsi(_Gie):
    code = "gie_agsi"
    url = AGSI_URL
    prefix = "storage"
    areas: ClassVar[dict[str, dict[str, str]]] = {
        a: params for a, (params, _) in GAS_STORAGE_AREAS.items()
    }
    fields = AGSI_FIELDS


class GieAlsi(_Gie):
    code = "gie_alsi"
    url = ALSI_URL
    prefix = "lng"
    areas = LNG_AREAS
    fields = ALSI_FIELDS
