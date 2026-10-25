"""Eurostat: Monatsstatistik Erdgas, Öl und Steinkohle (Bestand, Bilanz, Einfuhr), ab 2005.

Statistics API (JSON-stat 2.0) ohne Schlüssel, Lizenz CC BY 4.0
(https://ec.europa.eu/eurostat/about-us/policies/copyright). Gasmengen in Terajoule
(Brennwert, ``TJ_GCV``), umgerechnet in TWh; Öl und Kohle in Tausend Tonnen (kt). Eurostat
veröffentlicht je nach Land drei bis zwölf Wochen nach Monatsende und überarbeitet ältere
Monate; die Gasreihen ergänzen die Tageswerte von GIE um Bilanz und Herkunft.

Datensätze:

- ``gas_stock`` (``nrg_stk_gasm``, Schlussbestand im Inland ``STKCL_NAT``):
  ``gas_storage_<gebiet>_m`` je Gebiet aus ``model.GAS_STORAGE_AREAS``. Der Bestand am Ende
  eines Monats steht am Beginn des Folgemonats.
- ``gas_balance`` (``nrg_cb_gasm``): ``gas_<position>_<gebiet>_m`` für die Positionen aus
  ``model.GAS_BALANCE`` (Import, Export, Förderung, Verbrauch, Einsatz in Strom und Wärme,
  Speicheränderung) am Monatsbeginn.
- ``gas_import_origin`` (``nrg_ti_gasm``): ``gas_import_<gebiet>_<herkunft>_m`` für DE und EU
  und die Herkunftsländer aus ``model.GAS_IMPORT_PARTNERS`` am Monatsbeginn.
- ``oil_crude_import`` (``nrg_ti_coifpm``, alle Ölfelder): ``oil_crude_import_price_de_m``
  (mittlerer CIF-Preis der Rohöleinfuhr, USD/bbl, ab 2005) und
  ``oil_crude_import_volume_de_m`` (kbbl). Der einzige freie Rohölpreis; Brent und WTI der
  EIA stammen von Thomson Reuters.
- ``oil_crude_balance`` (``nrg_cb_oilm``, Rohöl): ``oil_crude_<position>_<gebiet>_m`` für die
  Positionen aus ``model.OIL_CRUDE_BALANCE``.
- ``oil_stock`` (``nrg_stk_oilm``, Schlussbestand im Inland): ``oil_stock_<produkt>_<gebiet>_m``
  für Rohöl und Mineralölprodukte, wie ``gas_stock`` am Folgemonat.
- ``oil_import_origin`` (``nrg_ti_oilm``, Rohöl): ``oil_crude_import_de_<herkunft>_m`` für die
  Herkunftsländer aus ``model.OIL_IMPORT_PARTNERS``.
- ``coal_balance`` (``nrg_cb_sffm``, Steinkohle): ``coal_hard_<position>_<gebiet>_m`` für die
  Positionen aus ``model.COAL_BALANCE``; ``coal_stock`` (gleiche Tabelle, ``STKCL_NAT``):
  ``coal_hard_stock_eu_m`` am Folgemonat (DE meldet seit 2019 keine Bestände). DE meldet als
  Inlandslieferung die Einfuhr. Kohle nach Herkunft gibt es nur jährlich.

Zeitstempel 00:00 Ortszeit, als UTC.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar

from energy_ingest.http import Http
from energy_ingest.model import (
    BERLIN,
    COAL_BALANCE,
    FUEL_STAT_AREAS,
    GAS_BALANCE,
    GAS_IMPORT_AREAS,
    GAS_IMPORT_PARTNERS,
    GAS_STORAGE_AREAS,
    MONTH,
    OIL_CRUDE_BALANCE,
    OIL_CRUDE_IMPORT,
    OIL_IMPORT_PARTNERS,
    OIL_STOCK,
    SeriesData,
    next_period,
    series,
)
from energy_ingest.sources import FormatError, SourceError, clip

BASE_URL = "https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data"
TJ_PER_TWH = 3600.0
#: Eurostat-Geo -> Kürzel im Reihen-Code
GEOS = {geo: area for area, (_, geo) in GAS_STORAGE_AREAS.items()}
FUEL_GEOS = tuple(geo for geo, area in GEOS.items() if area in FUEL_STAT_AREAS)


@dataclass(frozen=True)
class Table:
    name: str  # Eurostat-Datensatz
    fixed: dict[str, str]  # Dimensionen mit genau einem Wert
    key_dim: str  # Dimension, die die Reihe unterscheidet (neben geo)
    keys: dict[str, str]  # Eurostat-Code -> Kürzel im Reihen-Code
    geos: tuple[str, ...]
    template: str  # Reihen-Code mit {area} und {key}
    stock: bool  # Bestand am Monatsende (Zeitstempel Folgemonat) statt Monatsmenge
    scale: float = TJ_PER_TWH  # Teiler von der Eurostat-Einheit zur Einheit der Reihe


GAS = {"freq": "M", "siec": "G3000", "unit": "TJ_GCV"}
TABLES: dict[str, Table] = {
    "gas_stock": Table(
        "nrg_stk_gasm",
        GAS,
        "stk_flow",
        {"STKCL_NAT": ""},
        tuple(GEOS),
        "gas_storage_{area}_m",
        stock=True,
    ),
    "gas_balance": Table(
        "nrg_cb_gasm",
        GAS,
        "nrg_bal",
        {code: short for short, code in GAS_BALANCE.items()},
        tuple(GEOS),
        "gas_{key}_{area}_m",
        stock=False,
    ),
    "gas_import_origin": Table(
        "nrg_ti_gasm",
        GAS,
        "partner",
        {code: short for short, code in GAS_IMPORT_PARTNERS.items()},
        tuple(geo for geo, area in GEOS.items() if area in GAS_IMPORT_AREAS),
        "gas_import_{area}_{key}_m",
        stock=False,
    ),
    "oil_crude_import": Table(
        "nrg_ti_coifpm",
        {"freq": "M", "crudeoil": "TOTAL"},
        "indic_nrg",
        {code: short for short, code in OIL_CRUDE_IMPORT.items()},
        ("DE",),
        "oil_crude_import_{key}_{area}_m",
        stock=False,
        scale=1.0,
    ),
    "oil_crude_balance": Table(
        "nrg_cb_oilm",
        {"freq": "M", "siec": "O4100_TOT", "unit": "THS_T"},
        "nrg_bal",
        {code: short for short, code in OIL_CRUDE_BALANCE.items()},
        FUEL_GEOS,
        "oil_crude_{key}_{area}_m",
        stock=False,
        scale=1.0,
    ),
    "oil_stock": Table(
        "nrg_stk_oilm",
        {"freq": "M", "stk_flow": "STKCL_NAT", "unit": "THS_T"},
        "siec",
        {code: short for short, code in OIL_STOCK.items()},
        FUEL_GEOS,
        "oil_stock_{key}_{area}_m",
        stock=True,
        scale=1.0,
    ),
    "oil_import_origin": Table(
        "nrg_ti_oilm",
        {"freq": "M", "siec": "O4100_TOT", "unit": "THS_T"},
        "partner",
        {code: short for short, code in OIL_IMPORT_PARTNERS.items()},
        ("DE",),
        "oil_crude_import_{area}_{key}_m",
        stock=False,
        scale=1.0,
    ),
    "coal_balance": Table(
        "nrg_cb_sffm",
        {"freq": "M", "siec": "C0100", "unit": "THS_T"},
        "nrg_bal",
        {code: short for short, code in COAL_BALANCE.items()},
        FUEL_GEOS,
        "coal_hard_{key}_{area}_m",
        stock=False,
        scale=1.0,
    ),
    "coal_stock": Table(
        "nrg_cb_sffm",
        {"freq": "M", "siec": "C0100", "unit": "THS_T"},
        "nrg_bal",
        {"STKCL_NAT": ""},
        ("EU27_2020",),  # DE meldet seit 2019 keine Bestände (0)
        "coal_hard_stock_{area}_m",
        stock=True,
        scale=1.0,
    ),
}


def _month(text: str) -> datetime:
    if len(text) != 7 or text[4] != "-":
        raise FormatError(f"Eurostat: Zeitraum {text!r} ist kein Monat")
    return datetime(int(text[:4]), int(text[5:]), 1, tzinfo=BERLIN).astimezone(UTC)


def parse_json(body: Any, table: Table) -> dict[tuple[str, str], list[tuple[datetime, float]]]:
    """JSON-stat-Antwort -> Punkte je (Geo, Eurostat-Code der Schlüsseldimension).

    Werte in der Einheit der Reihe (Eurostat-Wert geteilt durch ``table.scale``).
    """
    try:
        ids: list[str] = body["id"]
        sizes: list[int] = body["size"]
        dims = {k: body["dimension"][k]["category"]["index"] for k in ids}
        values: dict[str, float | None] = body["value"]
    except (KeyError, TypeError) as exc:
        raise FormatError(f"Eurostat: unerwartete Antwort {str(body)[:200]!r}") from exc
    if set(ids) != {*table.fixed, table.key_dim, "geo", "time"}:
        raise FormatError(f"Eurostat {table.name}: Dimensionen {ids}")
    for dim, code in table.fixed.items():
        if list(dims[dim]) != [code]:
            raise FormatError(f"Eurostat: {dim} = {list(dims[dim])}, erwartet {code!r}")
    unknown = set(dims[table.key_dim]) - set(table.keys)
    if unknown:
        raise FormatError(f"Eurostat {table.name}: fremde {table.key_dim} {sorted(unknown)}")
    strides = [1] * len(ids)
    for i in range(len(ids) - 2, -1, -1):
        strides[i] = strides[i + 1] * sizes[i + 1]
    pos = {k: strides[i] for i, k in enumerate(ids)}
    out: dict[tuple[str, str], list[tuple[datetime, float]]] = {}
    for geo, gi in dims["geo"].items():
        for key, ki in dims[table.key_dim].items():
            pts = []
            for period, ti in dims["time"].items():
                idx = gi * pos["geo"] + ki * pos[table.key_dim] + ti * pos["time"]
                v = values.get(str(idx))
                if v is not None:
                    ts = _month(period)
                    pts.append((next_period(ts, 1) if table.stock else ts, float(v) / table.scale))
            out[(geo, key)] = sorted(pts)
    return out


class Eurostat:
    code = "eurostat"
    license = "CC BY 4.0, Eurostat"
    priority = 10
    free = True
    # Eurostat überarbeitet zurückliegende Monate
    sync_lookback: ClassVar[dict[str, timedelta]] = {d: timedelta(days=400) for d in TABLES}

    def __init__(self, http: Http, base_url: str = BASE_URL) -> None:
        self.http = http
        self.base_url = base_url.rstrip("/")

    def datasets(self) -> list[str]:
        return list(TABLES)

    def max_chunk(self, dataset: str) -> timedelta:
        return timedelta(days=3650)

    def fetch(self, dataset: str, start: datetime, end: datetime) -> list[SeriesData]:
        table = TABLES[dataset]
        # Bestände stehen am Folgemonat: der Monat davor gehört noch ins Fenster
        first = start.astimezone(BERLIN) - (MONTH if table.stock else timedelta(0))
        params: list[tuple[str, str]] = [
            *table.fixed.items(),
            *((table.key_dim, k) for k in table.keys),
            *(("geo", g) for g in table.geos),
            ("sinceTimePeriod", first.strftime("%Y-%m")),
            ("untilTimePeriod", end.astimezone(BERLIN).strftime("%Y-%m")),
            ("format", "JSON"),
            ("lang", "EN"),
        ]
        r = self.http.get(f"{self.base_url}/{table.name}", params)
        if r.status_code >= 400:
            raise SourceError(f"Eurostat {table.name}: {r.status_code} {r.text[:200]}")
        found = parse_json(r.json(), table)
        unknown = {geo for geo, _ in found} - set(table.geos)
        if unknown:
            raise FormatError(f"Eurostat {table.name}: fremde Gebiete {sorted(unknown)}")
        out = []
        for (geo, key), pts in found.items():
            pts = clip(pts, start, end)
            if pts:
                code = table.template.format(area=GEOS[geo], key=table.keys[key])
                out.append(SeriesData(series(code), MONTH, pts))
        return out
