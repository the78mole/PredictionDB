"""Monats- und Jahresmarktwerte nach Anlage 1 EEG, selbst gerechnet aus freien Daten.

Ersatz für ``netztransparenz``, solange diese Quelle nicht freigegeben ist. Grundlage sind die
Stundenmittel aus ``energy.value_best`` (also nur freigegebene Quellen, in der Regel SMARD):

- ``*_base``: Mittel des Day-Ahead-Preises DE-LU über alle Stunden der Periode
  (entspricht MW-EPEX bzw. JW)
- ``*_solar``, ``*_wind_onshore``, ``*_wind_offshore``: mit der Ist-Erzeugung gewichtetes
  Mittel des Preises

Die ÜNB gewichten mit ihrer Online-Hochrechnung und rechnen seit Oktober 2025 mit
Viertelstundenpreisen; die Werte hier sind daher Schätzungen auf Stundenbasis. Eine Periode
wird erst nach ihrem Ende und nur bei ausreichender Abdeckung (``MIN_COVERAGE``) geschrieben,
damit ein Block ohne vollständige Grunddaten leer bleibt und später erneut gerechnet wird.
"""

from __future__ import annotations

from bisect import bisect_right
from collections.abc import Callable, Iterable
from datetime import UTC, datetime, timedelta
from typing import ClassVar

from sqlalchemy import Engine, text

from energy_ingest.model import BERLIN, HOUR, MONTH, YEAR, SeriesData, next_period, series

PRICE = "da_price_de_lu"
#: Reihen-Code der Erzeugung -> Endung des Marktwerts
WEIGHTS = {
    "gen_solar": "solar",
    "gen_wind_onshore": "wind_onshore",
    "gen_wind_offshore": "wind_offshore",
}
#: Mindestanteil der Stunden einer Periode mit Preis (und Erzeugung für gewichtete Werte)
MIN_COVERAGE = 0.98

_HOURLY = """
    SELECT time_bucket('1 hour', ts) AS hour, code, avg(value) AS value
      FROM energy.value_best
     WHERE code = ANY(:codes) AND ts >= :start AND ts < :end
     GROUP BY 1, 2
"""

Row = tuple[datetime, str, float]


def period_starts(start: datetime, end: datetime, months: int) -> list[datetime]:
    """Beginn aller Perioden (``months`` = 1 oder 12, Ortszeit) mit Beginn in ``[start, end)``."""
    local = start.astimezone(BERLIN)
    first_month = 1 if months == 12 else local.month
    p = datetime(local.year, first_month, 1, tzinfo=BERLIN).astimezone(UTC)
    if p < start:
        p = next_period(p, months)
    out = []
    while p < end:
        out.append(p)
        p = next_period(p, months)
    return out


def market_values(rows: Iterable[Row], periods: list[datetime], months: int) -> list[SeriesData]:
    """Marktwerte je Periode aus Stundenwerten ``(stunde, code, wert)``."""
    if not periods:
        return []
    bounds = [*periods, next_period(periods[-1], months)]
    hours: list[dict[datetime, dict[str, float]]] = [{} for _ in periods]
    for hour, code, value in rows:
        i = bisect_right(bounds, hour) - 1
        if 0 <= i < len(periods):
            hours[i].setdefault(hour, {})[code] = value

    prefix, resolution = ("mw", MONTH) if months == 1 else ("jw", YEAR)
    points: dict[str, list[tuple[datetime, float]]] = {}
    for p, q, by_hour in zip(periods, bounds[1:], hours, strict=True):
        need = MIN_COVERAGE * (q - p) / HOUR
        prices = [v[PRICE] for v in by_hour.values() if PRICE in v]
        if len(prices) < need:
            continue
        points.setdefault(f"{prefix}_base", []).append((p, sum(prices) / len(prices)))
        for gen, suffix in WEIGHTS.items():
            pairs = [(v[PRICE], v[gen]) for v in by_hour.values() if PRICE in v and gen in v]
            weight = sum(g for _, g in pairs)
            if len(pairs) < need or weight <= 0:
                continue
            value = sum(price * g for price, g in pairs) / weight
            points.setdefault(f"{prefix}_{suffix}", []).append((p, value))
    return [SeriesData(series(code), resolution, pts) for code, pts in points.items()]


class Marktwert:
    code = "pdb_marktwert"
    license = (
        "Eigene Berechnung nach Anlage 1 EEG aus freien Quellen "
        "(CC BY 4.0, Bundesnetzagentur | SMARD.de)"
    )
    # Hinter netztransparenz (15): sind beide vorhanden, gelten die amtlichen Werte
    priority = 40
    free = True

    # Laufender Abgleich rechnet den Vormonat bzw. das Vorjahr nach, bis alle Stunden da sind
    sync_lookback: ClassVar[dict[str, timedelta]] = {
        "market_value_month": timedelta(days=40),
        "market_value_year": timedelta(days=370),
    }

    def __init__(self, engine: Engine, clock: Callable[[], datetime] | None = None) -> None:
        self.engine = engine
        self.clock = clock or (lambda: datetime.now(UTC))

    def datasets(self) -> list[str]:
        return ["market_value_month", "market_value_year"]

    def max_chunk(self, dataset: str) -> timedelta:
        # Höchstens ein Periodenbeginn je Block, damit ein Block nie halb gerechnet als
        # erledigt gilt (Monate haben mindestens 28, Jahre mindestens 365 Tage)
        return timedelta(days=27) if dataset == "market_value_month" else timedelta(days=300)

    def _hourly(self, start: datetime, end: datetime) -> list[Row]:
        with self.engine.connect() as conn:
            rows = conn.execute(
                text(_HOURLY), {"codes": [PRICE, *WEIGHTS], "start": start, "end": end}
            ).all()
        return [(r.hour, r.code, float(r.value)) for r in rows]

    def fetch(self, dataset: str, start: datetime, end: datetime) -> list[SeriesData]:
        months = {"market_value_month": 1, "market_value_year": 12}[dataset]
        now = self.clock()
        periods = [p for p in period_starts(start, end, months) if next_period(p, months) <= now]
        if not periods:
            return []
        return market_values(
            self._hourly(periods[0], next_period(periods[-1], months)), periods, months
        )
