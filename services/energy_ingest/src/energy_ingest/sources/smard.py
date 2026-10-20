"""SMARD (Bundesnetzagentur), offene Chart-API ohne Registrierung.

Aufbau der API::

    /app/chart_data/{filter}/{region}/index_{res}.json
        -> {"timestamps": [ms, ...]}   Beginn je Wochenblock (Montag 00:00 Ortszeit)
    /app/chart_data/{filter}/{region}/{filter}_{region}_{res}_{ms}.json
        -> {"series": [[ms, wert|null], ...]}

Mengen liefert SMARD als Energie je Intervall (MWh); daraus wird mittlere Leistung in MW.
Viertelstundenwerte gibt es nicht für alle Reihen und Zeiträume (Preis erst seit der
Umstellung der SDAC im Oktober 2025). Vor dem ersten Viertelstundenblock wird deshalb auf
Stundenwerte ausgewichen. Lizenz: CC BY 4.0, Quelle Bundesnetzagentur | SMARD.de.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx

from energy_ingest.http import Http
from energy_ingest.model import HOUR, QUARTER_HOUR, UNIT_MW, SeriesData, series
from energy_ingest.sources import SourceError, clip

BASE_URL = "https://www.smard.de/app/chart_data"

RESOLUTIONS: list[tuple[str, timedelta]] = [("quarterhour", QUARTER_HOUR), ("hour", HOUR)]


@dataclass(frozen=True)
class Filter:
    id: int
    region: str
    code: str


# Filter-IDs der SMARD-Oberfläche (Marktdaten visualisieren)
FILTERS: list[Filter] = [
    Filter(4169, "DE-LU", "da_price_de_lu"),
    Filter(410, "DE", "load_actual"),
    Filter(4359, "DE", "residual_load"),
    Filter(411, "DE", "load_da_forecast"),
    Filter(4362, "DE", "residual_load_da_forecast"),
    Filter(1223, "DE", "gen_lignite"),
    Filter(1224, "DE", "gen_nuclear"),
    Filter(1225, "DE", "gen_wind_offshore"),
    Filter(1226, "DE", "gen_hydro"),
    Filter(1227, "DE", "gen_other_conventional"),
    Filter(1228, "DE", "gen_other_renewable"),
    Filter(4066, "DE", "gen_biomass"),
    Filter(4067, "DE", "gen_wind_onshore"),
    Filter(4068, "DE", "gen_solar"),
    Filter(4069, "DE", "gen_hard_coal"),
    Filter(4070, "DE", "gen_hydro_pumped"),
    Filter(4071, "DE", "gen_gas"),
    Filter(3791, "DE", "gen_da_forecast_wind_offshore"),
    Filter(123, "DE", "gen_da_forecast_wind_onshore"),
    Filter(125, "DE", "gen_da_forecast_solar"),
    Filter(5097, "DE", "gen_da_forecast_wind_solar"),
    Filter(715, "DE", "gen_da_forecast_other"),
    Filter(122, "DE", "gen_da_forecast_total"),
]

INDEX_TTL_S = 900.0
# Ein Block umfasst eine Woche; Zeitumstellung verschiebt den Beginn um eine Stunde.
BLOCK = timedelta(days=7, hours=1)


def _ms(ts: datetime) -> int:
    return int(ts.timestamp() * 1000)


def _from_ms(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=UTC)


class Smard:
    code = "smard"
    license = "CC BY 4.0, Bundesnetzagentur | SMARD.de"
    priority = 20

    def __init__(self, http: Http, base_url: str = BASE_URL) -> None:
        self.http = http
        self.base_url = base_url.rstrip("/")
        self._filters = {f.code: f for f in FILTERS}
        self._index: dict[tuple[int, str, str], tuple[float, list[int]]] = {}

    def datasets(self) -> list[str]:
        return list(self._filters)

    def max_chunk(self, dataset: str) -> timedelta:
        return timedelta(days=91)

    def index(self, f: Filter, res: str) -> list[int]:
        key = (f.id, f.region, res)
        cached = self._index.get(key)
        if cached and time.monotonic() - cached[0] < INDEX_TTL_S:
            return cached[1]
        r = self.http.get(f"{self.base_url}/{f.id}/{f.region}/index_{res}.json")
        if r.status_code == httpx.codes.NOT_FOUND:
            stamps: list[int] = []
        else:
            r.raise_for_status()
            stamps = sorted(int(t) for t in r.json().get("timestamps", []))
        self._index[key] = (time.monotonic(), stamps)
        return stamps

    def _block(self, f: Filter, res: str, stamp: int) -> list[tuple[int, float | None]]:
        url = f"{self.base_url}/{f.id}/{f.region}/{f.id}_{f.region}_{res}_{stamp}.json"
        data = self.http.get_json(url)
        raw = data.get("series")
        if not isinstance(raw, list):
            raise SourceError(f"SMARD {url}: kein Feld 'series'")
        return [(int(p[0]), None if p[1] is None else float(p[1])) for p in raw]

    def fetch(self, dataset: str, start: datetime, end: datetime) -> list[SeriesData]:
        f = self._filters[dataset]
        info = series(f.code)
        points: dict[datetime, float] = {}
        finest: timedelta | None = None
        covered_from = end  # alles ab hier ist schon in feinerer Auflösung abgedeckt
        for res, step in RESOLUTIONS:
            stamps = self.index(f, res)
            if not stamps:
                continue
            factor = (HOUR / step) if info.unit == UNIT_MW else 1.0
            upper = min(end, covered_from)
            for i, stamp in enumerate(stamps):
                block_start = _from_ms(stamp)
                block_end = _from_ms(stamps[i + 1]) if i + 1 < len(stamps) else end + BLOCK
                if block_start >= upper or block_end <= start:
                    continue
                for ms, v in self._block(f, res, stamp):
                    ts = _from_ms(ms)
                    if v is not None and start <= ts < upper and ts not in points:
                        points[ts] = v * factor
            finest = finest or step
            covered_from = min(covered_from, _from_ms(stamps[0]))
            if covered_from <= start:
                break
        pts = clip(sorted(points.items()), start, end)
        return [SeriesData(info, finest or HOUR, pts)] if pts else []
