"""Gemeinsames Datenmodell der Energiequellen.

Alle Quellen liefern ``SeriesData`` mit einheitlichen Reihen-Codes (``da_price_de_lu``,
``load_actual``, ``gen_wind_onshore`` …), Zeitstempeln in UTC (Beginn des Intervalls)
und Leistungen in MW bzw. Preisen in EUR/MWh. Gleiche Codes verschiedener Quellen sind
damit direkt vergleichbar (Redundanz und Plausibilitätsabgleich, Kapitel 3.3).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, time, timedelta
from enum import StrEnum
from zoneinfo import ZoneInfo

BERLIN = ZoneInfo("Europe/Berlin")

QUARTER_HOUR = timedelta(minutes=15)
HOUR = timedelta(hours=1)

UNIT_MW = "MW"
UNIT_EUR_MWH = "EUR/MWh"


class Kind(StrEnum):
    """Art der Reihe; bestimmt den geschätzten Veröffentlichungszeitpunkt."""

    PRICE_DA = "price_da"  # Day-Ahead-Auktion
    ACTUAL = "actual"  # Ist-Wert (Last, Erzeugung)
    LOAD_DA_FORECAST = "load_da_forecast"  # Lastprognose der ÜNB für den Folgetag
    GEN_DA_FORECAST = "gen_da_forecast"  # Wind-/PV-Prognose der ÜNB für den Folgetag


@dataclass(frozen=True)
class SeriesInfo:
    code: str
    kind: Kind
    unit: str


@dataclass
class SeriesData:
    info: SeriesInfo
    resolution: timedelta
    points: list[tuple[datetime, float]] = field(default_factory=list)

    @property
    def code(self) -> str:
        return self.info.code


# Einheitliche Reihen-Codes. Nicht jede Quelle liefert jede Reihe.
SERIES: dict[str, SeriesInfo] = {
    s.code: s
    for s in [
        SeriesInfo("da_price_de_lu", Kind.PRICE_DA, UNIT_EUR_MWH),
        SeriesInfo("load_actual", Kind.ACTUAL, UNIT_MW),
        SeriesInfo("residual_load", Kind.ACTUAL, UNIT_MW),
        SeriesInfo("load_da_forecast", Kind.LOAD_DA_FORECAST, UNIT_MW),
        SeriesInfo("residual_load_da_forecast", Kind.LOAD_DA_FORECAST, UNIT_MW),
        *(
            SeriesInfo(f"gen_{t}", Kind.ACTUAL, UNIT_MW)
            for t in [
                "biomass",
                "lignite",
                "hard_coal",
                "gas",
                "coal_gas",
                "oil",
                "nuclear",
                "hydro",  # SMARD fasst Lauf- und Speicherwasser zusammen
                "hydro_ror",
                "hydro_reservoir",
                "hydro_pumped",
                "wind_onshore",
                "wind_offshore",
                "solar",
                "geothermal",
                "waste",
                "other_renewable",
                "other_conventional",
                "other",
                "storage",
            ]
        ),
        *(
            SeriesInfo(f"gen_da_forecast_{t}", Kind.GEN_DA_FORECAST, UNIT_MW)
            for t in ["wind_onshore", "wind_offshore", "solar", "wind_solar", "total", "other"]
        ),
    ]
}


def series(code: str) -> SeriesInfo:
    return SERIES[code]


def _at_berlin(day: datetime, hour: int) -> datetime:
    """``hour`` Uhr Ortszeit am Vortag des Liefertags, zu dem ``day`` gehört."""
    local_day = day.astimezone(BERLIN).date() - timedelta(days=1)
    return datetime.combine(local_day, time(hour), tzinfo=BERLIN).astimezone(UTC)


def estimate_published(kind: Kind, ts: datetime, resolution: timedelta) -> datetime:
    """Spätester regulärer Veröffentlichungszeitpunkt eines Werts (Leakage-Schutz, 7.4).

    - Day-Ahead-Preis: Auktionsergebnis gegen 13:00 Ortszeit am Vortag des Liefertags.
    - Lastprognose Folgetag: bis 10:00 Ortszeit am Vortag (VO 543/2013 Art. 6.1 b).
    - Wind-/PV-Prognose Folgetag: bis 18:00 Ortszeit am Vortag (Art. 14.1 d).
    - Ist-Werte: bis eine Stunde nach Ende des Intervalls (Art. 6.1 a, 16.1 b).
    """
    match kind:
        case Kind.PRICE_DA:
            return _at_berlin(ts, 13)
        case Kind.LOAD_DA_FORECAST:
            return _at_berlin(ts, 10)
        case Kind.GEN_DA_FORECAST:
            return _at_berlin(ts, 18)
        case Kind.ACTUAL:
            return ts + resolution + HOUR


def published_at(kind: Kind, ts: datetime, resolution: timedelta, fetched: datetime) -> datetime:
    """Zeitpunkt, ab dem der Wert nachweislich bekannt war.

    Wer einen Wert vor dem regulären Termin abruft, kennt ihn ab dem Abruf; beim Backfill
    liegt der Abruf weit danach, dann gilt der reguläre Termin.
    """
    return min(fetched, estimate_published(kind, ts, resolution))


def floor_to(ts: datetime, step: timedelta) -> datetime:
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    return epoch + ((ts - epoch) // step) * step
