"""Europäische Zentralbank (EZB): Referenzkurs US-Dollar je Euro (täglich, ab 1999).

Data Portal API, Reihe ``EXR.D.USD.EUR.SP00.A`` als CSV. Die EZB erlaubt die
Weiterverwendung ihrer Statistiken mit Quellenangabe, auch kommerziell
(https://www.ecb.europa.eu/stats/ecb_statistics/governance_and_quality_framework/html/usage_policy.en.html).

Zum Umrechnen der Dollar-Notierungen (EIA, Weltbank) in Euro.
"""

from __future__ import annotations

import csv
import io
from datetime import date, datetime, timedelta

from energy_ingest.http import Http
from energy_ingest.model import DAY, SeriesData, series
from energy_ingest.sources import FormatError, SourceError, day_start

URL = "https://data-api.ecb.europa.eu/service/data/EXR/D.USD.EUR.SP00.A"
COLUMNS = {"KEY", "TIME_PERIOD", "OBS_VALUE", "UNIT"}


def parse_csv(body: str) -> list[tuple[datetime, float]]:
    rows = list(csv.DictReader(io.StringIO(body)))
    if not body.strip():
        return []
    if not rows or not set(rows[0]) >= COLUMNS:
        raise FormatError(f"EZB: unerwartete Spalten {body[:200]!r}")
    pts = []
    for row in rows:
        if row["KEY"] != "EXR.D.USD.EUR.SP00.A" or row["UNIT"] != "USD":
            raise FormatError(f"EZB: fremde Reihe {row['KEY']} {row['UNIT']}")
        if row["OBS_VALUE"]:
            pts.append((day_start(date.fromisoformat(row["TIME_PERIOD"])), float(row["OBS_VALUE"])))
    return pts


class Ecb:
    code = "ecb"
    license = "Europäische Zentralbank, Weiterverwendung mit Quellenangabe"
    priority = 10
    free = True

    def __init__(self, http: Http, url: str = URL) -> None:
        self.http = http
        self.url = url

    def datasets(self) -> list[str]:
        return ["fx"]

    def max_chunk(self, dataset: str) -> timedelta:
        return timedelta(days=3650)

    def fetch(self, dataset: str, start: datetime, end: datetime) -> list[SeriesData]:
        if dataset != "fx":
            raise KeyError(dataset)
        params = {
            "startPeriod": (start - DAY).date().isoformat(),
            "endPeriod": (end + DAY).date().isoformat(),
            "format": "csvdata",
        }
        r = self.http.get(self.url, params)
        if r.status_code == 404:  # keine Werte im Zeitraum (Wochenende, vor 1999)
            return []
        if r.status_code >= 400:
            raise SourceError(f"EZB: {r.status_code} {r.text[:200]}")
        pts = [(ts, v) for ts, v in parse_csv(r.text) if start <= ts < end]
        return [SeriesData(series("fx_usd_eur"), DAY, pts)] if pts else []
