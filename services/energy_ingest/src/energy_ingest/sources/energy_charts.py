"""Energy-Charts (Fraunhofer ISE), offene API ``api.energy-charts.info`` ohne Registrierung.

Genutzte Endpunkte::

    /price?bzn=DE-LU&start=…&end=…               -> unix_seconds, price (EUR/MWh)
    /public_power?country=de&start=…&end=…        -> unix_seconds, production_types[name, data] (MW)
    /public_power_forecast?country=de&production_type=solar&forecast_type=day-ahead&…
                                                  -> unix_seconds, forecast_values (MW)

Zeitstempel bezeichnen den Beginn des Intervalls. Lizenz laut API-Dokumentation (v2.0): CC BY 4.0
mit Quellenangabe Energy-Charts.info, soweit nicht anders angegeben. Für ``/price`` steht die
Lizenz im Feld ``license_info``; für DE-LU ist es CC BY 4.0 von Bundesnetzagentur | SMARD.de,
für andere Gebotszonen nur private und interne Nutzung. Fehlt der Vermerk oder weicht er ab,
schreibt die Quelle keine Preise (``FormatError``).
"""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from typing import Any

import httpx

from energy_ingest.http import Http
from energy_ingest.model import QUARTER_HOUR, SeriesData, series
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
        return ["price", "public_power", *(f"forecast_{t}" for t in FORECASTS)]

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
        if dataset == "price":
            data = self._get("price", {"bzn": self.bzn, **window})
            if not data:
                return []
            lic = str(data.get("license_info", ""))
            if "CC BY 4.0" not in lic:
                raise FormatError(f"Energy-Charts: Preise ohne freie Lizenz ({lic!r})")
            stamps = [datetime.fromtimestamp(int(s), tz=UTC) for s in data["unix_seconds"]]
            pts = _points(stamps, data["price"], start, end)
            return [SeriesData(series("da_price_de_lu"), _resolution(stamps), pts)] if pts else []

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

        raise KeyError(dataset)
