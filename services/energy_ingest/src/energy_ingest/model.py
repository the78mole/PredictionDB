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
# Nominelle Auflösung von Monats- und Jahreswerten (energy.series.resolution); die
# Zeitstempel selbst liegen exakt am Monats- bzw. Jahresbeginn (Ortszeit)
MONTH = timedelta(days=30)
YEAR = timedelta(days=365)
DAY = timedelta(days=1)
WEEK = timedelta(days=7)
# Monats- und Jahresmarktwerte erscheinen spätestens zehn Werktage nach Periodenende
MARKET_VALUE_DELAY = timedelta(days=14)
# Brennstoffpreise: Tageswerte am Folgetag, das Oil Bulletin einige Tage nach dem Stichtag
# (Montag), Monatsmittel der Weltbank in den ersten Tagen des Folgemonats
DAILY_PRICE_DELAY = timedelta(days=1)
WEEKLY_PRICE_DELAY = timedelta(days=7)
MONTHLY_PRICE_DELAY = timedelta(days=7)

UNIT_MW = "MW"
UNIT_EUR_MWH = "EUR/MWh"
UNIT_EUR_L = "EUR/l"
UNIT_USD_BBL = "USD/bbl"
UNIT_USD_GAL = "USD/gal"
UNIT_USD_MMBTU = "USD/MMBtu"
UNIT_USD_T = "USD/t"
UNIT_USD_PER_EUR = "USD/EUR"


#: Nachbarzonen, deren Day-Ahead-Preise Energy-Charts unter CC BY 4.0 (SMARD) ausliefert:
#: Gebotszone -> Kürzel im Reihen-Code ``da_price_<kürzel>``
NEIGHBOUR_ZONES: dict[str, str] = {
    "AT": "at",
    "BE": "be",
    "CH": "ch",
    "CZ": "cz",
    "DK1": "dk1",
    "DK2": "dk2",
    "FR": "fr",
    "HU": "hu",
    "IT-North": "it_north",
    "NL": "nl",
    "NO2": "no2",
    "PL": "pl",
    "SE4": "se4",
    "SI": "si",
}


class Kind(StrEnum):
    """Art der Reihe; bestimmt den geschätzten Veröffentlichungszeitpunkt."""

    PRICE_DA = "price_da"  # Day-Ahead-Auktion
    ACTUAL = "actual"  # Ist-Wert (Last, Erzeugung)
    LOAD_DA_FORECAST = "load_da_forecast"  # Lastprognose der ÜNB für den Folgetag
    GEN_DA_FORECAST = "gen_da_forecast"  # Wind-/PV-Prognose der ÜNB für den Folgetag
    MARKET_VALUE_MONTH = "market_value_month"  # Monatsmarktwert nach EEG (ÜNB)
    MARKET_VALUE_YEAR = "market_value_year"  # Jahresmarktwert nach EEG (ÜNB)
    PRICE_DAILY = "price_daily"  # Tagespreis (Brennstoff, Wechselkurs)
    PRICE_WEEKLY = "price_weekly"  # Wochenpreis (EU Oil Bulletin)
    PRICE_MONTHLY = "price_monthly"  # Monatsmittel (Weltbank)


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
        # Day-Ahead-Preise der Nachbarzonen (Marktkopplung), Energy-Charts unter CC BY 4.0
        *(
            SeriesInfo(f"da_price_{z}", Kind.PRICE_DA, UNIT_EUR_MWH)
            for z in NEIGHBOUR_ZONES.values()
        ),
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
        # Marktwerte nach Anlage 1 EEG: mw_* je Monat, jw_* je Kalenderjahr; *_base ist der
        # Mittelwert des Spotpreises (MW-EPEX bzw. JW), die übrigen sind mengengewichtet
        *(
            SeriesInfo(f"{p}_{t}", kind, UNIT_EUR_MWH)
            for p, kind in [("mw", Kind.MARKET_VALUE_MONTH), ("jw", Kind.MARKET_VALUE_YEAR)]
            for t in ["base", "wind_onshore", "wind_offshore", "solar"]
        ),
        # Brennstoffe und Wechselkurs (Tageswerte); Ölprodukte in Notierungseinheit
        SeriesInfo("oil_brent", Kind.PRICE_DAILY, UNIT_USD_BBL),
        SeriesInfo("oil_wti", Kind.PRICE_DAILY, UNIT_USD_BBL),
        SeriesInfo("oil_ulsd_nyh", Kind.PRICE_DAILY, UNIT_USD_GAL),
        SeriesInfo("oil_ulsd_usgc", Kind.PRICE_DAILY, UNIT_USD_GAL),
        SeriesInfo("oil_gasoline_nyh", Kind.PRICE_DAILY, UNIT_USD_GAL),
        SeriesInfo("oil_heating_oil_nyh", Kind.PRICE_DAILY, UNIT_USD_GAL),
        SeriesInfo("oil_jet_usgc", Kind.PRICE_DAILY, UNIT_USD_GAL),
        SeriesInfo("lpg_propane_mb", Kind.PRICE_DAILY, UNIT_USD_GAL),
        SeriesInfo("gas_henry_hub", Kind.PRICE_DAILY, UNIT_USD_MMBTU),
        SeriesInfo("gas_the_vtp", Kind.PRICE_DAILY, UNIT_EUR_MWH),
        SeriesInfo("gas_the_imbalance_pos", Kind.PRICE_DAILY, UNIT_EUR_MWH),
        SeriesInfo("gas_the_imbalance_neg", Kind.PRICE_DAILY, UNIT_EUR_MWH),
        SeriesInfo("fx_usd_eur", Kind.PRICE_DAILY, UNIT_USD_PER_EUR),
        # Endkundenpreise DE (Oil Bulletin), mit Steuern und netto (*_net)
        *(
            SeriesInfo(f"retail_de_{t}{n}", Kind.PRICE_WEEKLY, UNIT_EUR_L)
            for t in ["euro95", "diesel", "heating_oil", "lpg"]
            for n in ["", "_net"]
        ),
        # Monatsmittel der Weltbank (Suffix _m, damit sie Tageswerte nicht überdecken)
        SeriesInfo("oil_brent_m", Kind.PRICE_MONTHLY, UNIT_USD_BBL),
        SeriesInfo("oil_wti_m", Kind.PRICE_MONTHLY, UNIT_USD_BBL),
        SeriesInfo("oil_dubai_m", Kind.PRICE_MONTHLY, UNIT_USD_BBL),
        SeriesInfo("coal_au_m", Kind.PRICE_MONTHLY, UNIT_USD_T),
        SeriesInfo("coal_za_m", Kind.PRICE_MONTHLY, UNIT_USD_T),
        SeriesInfo("gas_henry_hub_m", Kind.PRICE_MONTHLY, UNIT_USD_MMBTU),
        SeriesInfo("gas_eu_m", Kind.PRICE_MONTHLY, UNIT_USD_MMBTU),
        SeriesInfo("lng_japan_m", Kind.PRICE_MONTHLY, UNIT_USD_MMBTU),
    ]
}


def series(code: str) -> SeriesInfo:
    return SERIES[code]


def _at_berlin(day: datetime, hour: int) -> datetime:
    """``hour`` Uhr Ortszeit am Vortag des Liefertags, zu dem ``day`` gehört."""
    local_day = day.astimezone(BERLIN).date() - timedelta(days=1)
    return datetime.combine(local_day, time(hour), tzinfo=BERLIN).astimezone(UTC)


def next_period(ts: datetime, months: int) -> datetime:
    """Beginn der Folgeperiode (``months`` Monate) in Ortszeit, als UTC."""
    local = ts.astimezone(BERLIN)
    n = local.year * 12 + local.month - 1 + months
    return datetime(n // 12, n % 12 + 1, 1, tzinfo=BERLIN).astimezone(UTC)


def estimate_published(kind: Kind, ts: datetime, resolution: timedelta) -> datetime:
    """Spätester regulärer Veröffentlichungszeitpunkt eines Werts (Leakage-Schutz, 7.4).

    - Day-Ahead-Preis: Auktionsergebnis gegen 13:00 Ortszeit am Vortag des Liefertags.
    - Lastprognose Folgetag: bis 10:00 Ortszeit am Vortag (VO 543/2013 Art. 6.1 b).
    - Wind-/PV-Prognose Folgetag: bis 18:00 Ortszeit am Vortag (Art. 14.1 d).
    - Ist-Werte: bis eine Stunde nach Ende des Intervalls (Art. 6.1 a, 16.1 b).
    - Monats- und Jahresmarktwerte: zwei Wochen nach Ende des Monats bzw. Jahres.
    - Brennstoffpreise: Tageswerte einen Tag nach Ende des Tages, Wochenwerte eine Woche
      nach dem Stichtag, Monatsmittel eine Woche nach Monatsende.
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
        case Kind.MARKET_VALUE_MONTH:
            return next_period(ts, 1) + MARKET_VALUE_DELAY
        case Kind.MARKET_VALUE_YEAR:
            return next_period(ts, 12) + MARKET_VALUE_DELAY
        case Kind.PRICE_DAILY:
            return ts + resolution + DAILY_PRICE_DELAY
        case Kind.PRICE_WEEKLY:
            return ts + WEEKLY_PRICE_DELAY
        case Kind.PRICE_MONTHLY:
            return next_period(ts, 1) + MONTHLY_PRICE_DELAY


def published_at(kind: Kind, ts: datetime, resolution: timedelta, fetched: datetime) -> datetime:
    """Zeitpunkt, ab dem der Wert nachweislich bekannt war.

    Wer einen Wert vor dem regulären Termin abruft, kennt ihn ab dem Abruf; beim Backfill
    liegt der Abruf weit danach, dann gilt der reguläre Termin.
    """
    return min(fetched, estimate_published(kind, ts, resolution))


def floor_to(ts: datetime, step: timedelta) -> datetime:
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    return epoch + ((ts - epoch) // step) * step
