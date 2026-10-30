"""Energy-Charts (Fraunhofer ISE), offene API ``api.energy-charts.info`` ohne Registrierung.

Genutzte Endpunkte::

    /price?bzn=DE-LU&start=…&end=…               -> unix_seconds, price (EUR/MWh), license_info
    /public_power?country=de&start=…&end=…        -> unix_seconds, production_types[name, data] (MW)
    /public_power_forecast?country=de&production_type=solar&forecast_type=day-ahead&…
                                                  -> unix_seconds, forecast_values (MW)
    /cbet?country=de&start=…&end=…                -> unix_seconds, countries[name, data] (GW)
    /cbpf?country=de&start=…&end=…                -> wie /cbet, physikalische Flüsse (GW)

Zeitstempel bezeichnen den Beginn des Intervalls. Lizenz laut API-Dokumentation (v2.0): CC BY 4.0
mit Quellenangabe Energy-Charts.info, soweit nicht anders angegeben. Für ``/price`` steht die
Lizenz im Feld ``license_info``; für DE-LU ist es CC BY 4.0 von Bundesnetzagentur | SMARD.de,
für andere Gebotszonen nur private und interne Nutzung. Neben DE-LU lädt die Quelle die
Nachbarzonen mit CC BY 4.0 (``NEIGHBOUR_ZONES``, Datensätze ``price_<kürzel>``). Fehlt der
Vermerk oder weicht er ab, schreibt die Quelle keine Preise (``FormatError``).

``/cbet`` (Handel) und ``/cbpf`` (physikalische Flüsse) liefern je Grenzpartner und als Summe
den Austausch Deutschlands in GW, positiv als Import; gespeichert wird in MW. Die Antwort hat
kein ``license_info``, damit gilt laut API-Dokumentation CC BY 4.0.

@relation(SWA-ENI-SRC-EC, scope=file)
"""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from typing import Any

import httpx

from energy_ingest.http import Http
from energy_ingest.model import NEIGHBOUR_ZONES, QUARTER_HOUR, XB_PARTNERS, SeriesData, series
from energy_ingest.sources import FormatError, SourceError, clip
from predictiondb_core.log import get_logger

log = get_logger(__name__)

BASE_URL = "https://api.energy-charts.info"

# Namen in /public_power -> Reihen-Code; nicht aufgeführte Namen (Anteile, Handel,
# Pumpspeicher-Verbrauch) werden übergangen.
PUBLIC_POWER: dict[str, str] = {
    "load": "load_actual",
    "residual load": "residual_load",
    "nuclear": "gen_nuclear",
    "biomass": "gen_biomass",
    "fossil brown coal / lignite": "gen_lignite",
    "fossil hard coal": "gen_hard_coal",
    "fossil oil": "gen_oil",
    "fossil coal-derived gas": "gen_coal_gas",
    "fossil gas": "gen_gas",
    "geothermal": "gen_geothermal",
    "hydro run-of-river": "gen_hydro_ror",
    "hydro water reservoir": "gen_hydro_reservoir",
    "hydro pumped storage": "gen_hydro_pumped",
    "others": "gen_other",
    "waste": "gen_waste",
    "wind offshore": "gen_wind_offshore",
    "wind onshore": "gen_wind_onshore",
    "solar": "gen_solar",
}

FORECASTS: dict[str, str] = {
    "solar": "gen_da_forecast_solar",
    "wind_onshore": "gen_da_forecast_wind_onshore",
    "wind_offshore": "gen_da_forecast_wind_offshore",
    "load": "load_da_forecast",
}


# Datensatz -> Endpunkt und Präfix der Reihen-Codes
CROSS_BORDER: dict[str, str] = {"cbet": "xb_trade_", "cbpf": "xb_flow_"}

GW = 1000.0


def _iso(ts: datetime) -> str:
    return ts.isoformat(timespec="minutes")


def _resolution(stamps: list[datetime]) -> timedelta:
    diffs = Counter(b - a for a, b in pairwise(stamps) if b > a)
    return diffs.most_common(1)[0][0] if diffs else QUARTER_HOUR


def _points(
    stamps: list[datetime], values: list[Any], start: datetime, end: datetime
) -> list[tuple[datetime, float]]:
    if len(values) != len(stamps):
        raise SourceError(f"Energy-Charts: {len(values)} Werte für {len(stamps)} Zeitstempel")
    pts = [(ts, float(v)) for ts, v in zip(stamps, values, strict=True) if v is not None]
    return clip(pts, start, end)


class EnergyCharts:
    """@relation(SWR-ENI-041, SWR-ENI-003, scope=class)"""

    code = "energy_charts"
    license = (
        "CC BY 4.0, Energy-Charts.info (Fraunhofer ISE); Preise DE-LU CC BY 4.0, "
        "Bundesnetzagentur | SMARD.de"
    )
    priority = 30
    free = True

    def __init__(
        self, http: Http, base_url: str = BASE_URL, bzn: str = "DE-LU", country: str = "de"
    ) -> None:
        self.http = http
        self.base_url = base_url.rstrip("/")
        self.bzn = bzn
        self.country = country

    def datasets(self) -> list[str]:
        return [
            "price",
            *(f"price_{z}" for z in NEIGHBOUR_ZONES.values()),
            "public_power",
            *(f"forecast_{t}" for t in FORECASTS),
            *CROSS_BORDER,
        ]

    def max_chunk(self, dataset: str) -> timedelta:
        return timedelta(days=31)

    def _get(self, endpoint: str, params: dict[str, str]) -> dict[str, Any] | None:
        r = self.http.get(f"{self.base_url}/{endpoint}", params)
        # Für Zeiträume ohne Daten antwortet die API mit 404
        if r.status_code == httpx.codes.NOT_FOUND:
            return None
        r.raise_for_status()
        data: dict[str, Any] = r.json()
        return data

    def fetch(self, dataset: str, start: datetime, end: datetime) -> list[SeriesData]:
        window = {"start": _iso(start), "end": _iso(end)}
        if dataset == "price" or dataset.startswith("price_"):
            if dataset == "price":
                bzn, price_code = self.bzn, "da_price_de_lu"
            else:
                short = dataset.removeprefix("price_")
                bzn = next(z for z, c in NEIGHBOUR_ZONES.items() if c == short)
                price_code = f"da_price_{short}"
            data = self._get("price", {"bzn": bzn, **window})
            if not data:
                return []
            lic = str(data.get("license_info", ""))
            if "CC BY 4.0" not in lic:
                raise FormatError(f"Energy-Charts: Preise ohne freie Lizenz ({lic!r})")
            stamps = [datetime.fromtimestamp(int(s), tz=UTC) for s in data["unix_seconds"]]
            pts = _points(stamps, data["price"], start, end)
            return [SeriesData(series(price_code), _resolution(stamps), pts)] if pts else []

        if dataset == "public_power":
            data = self._get("public_power", {"country": self.country, **window})
            if not data:
                return []
            stamps = [datetime.fromtimestamp(int(s), tz=UTC) for s in data["unix_seconds"]]
            res = _resolution(stamps)
            out = []
            for pt in data.get("production_types", []):
                code = PUBLIC_POWER.get(str(pt.get("name", "")).strip().lower())
                if code is None:
                    continue
                pts = _points(stamps, pt.get("data") or [], start, end)
                if pts:
                    out.append(SeriesData(series(code), res, pts))
            return out

        if dataset.startswith("forecast_"):
            ptype = dataset.removeprefix("forecast_")
            data = self._get(
                "public_power_forecast",
                {
                    "country": self.country,
                    "production_type": ptype,
                    "forecast_type": "day-ahead",
                    **window,
                },
            )
            if not data or not data.get("unix_seconds"):
                return []
            stamps = [datetime.fromtimestamp(int(s), tz=UTC) for s in data["unix_seconds"]]
            pts = _points(stamps, data.get("forecast_values") or [], start, end)
            return [SeriesData(series(FORECASTS[ptype]), _resolution(stamps), pts)] if pts else []

        if dataset in CROSS_BORDER:
            data = self._get(dataset, {"country": self.country, **window})
            if not data or not data.get("unix_seconds"):
                return []
            stamps = [datetime.fromtimestamp(int(s), tz=UTC) for s in data["unix_seconds"]]
            res = _resolution(stamps)
            out = []
            for c in data.get("countries", []):
                partner = XB_PARTNERS.get(str(c.get("name", "")).strip())
                if partner is None:
                    log.warning("energy_charts.unknown_partner", name=c.get("name"))
                    continue
                values = [None if v is None else float(v) * GW for v in c.get("data") or []]
                pts = _points(stamps, values, start, end)
                if pts:
                    out.append(SeriesData(series(CROSS_BORDER[dataset] + partner), res, pts))
            return out

        raise KeyError(dataset)
