"""Gemeinsames Datenmodell der Energiequellen.

Alle Quellen liefern ``SeriesData`` mit einheitlichen Reihen-Codes (``da_price_de_lu``,
``load_actual``, ``gen_wind_onshore`` …), Zeitstempeln in UTC (Beginn des Intervalls)
und Leistungen in MW bzw. Preisen in EUR/MWh. Gleiche Codes verschiedener Quellen sind
damit direkt vergleichbar (Redundanz und Plausibilitätsabgleich, Kapitel 3.3).

@relation(SWA-ENI-MODEL, scope=file)
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
# Gasspeicher und LNG: GIE veröffentlicht den Gastag D am Folgetag um 19:30 (MEZ), Eurostat
# Monatswerte je nach Land nach drei bis zwölf Wochen
GAS_DAY_DELAY = timedelta(hours=14)
MONTHLY_STAT_DELAY = timedelta(days=90)
# Öl- und Kohlebilanz der EU und Kohle je Land: Eurostat braucht bis zu vier Monate
MONTHLY_STAT_DELAY_SLOW = timedelta(days=150)
# Short-Term Energy Outlook der EIA: erscheint in der ersten Monatshälfte (meist 6.-12.)
OUTLOOK_DELAY = timedelta(days=14)

UNIT_MW = "MW"
UNIT_EUR_MWH = "EUR/MWh"
UNIT_EUR_L = "EUR/l"
UNIT_USD_BBL = "USD/bbl"
UNIT_USD_GAL = "USD/gal"
UNIT_USD_MMBTU = "USD/MMBtu"
UNIT_USD_T = "USD/t"
UNIT_USD_PER_EUR = "USD/EUR"
UNIT_TWH = "TWh"
UNIT_GWH = "GWh"
UNIT_GWH_D = "GWh/d"
UNIT_PERCENT = "%"
UNIT_KT = "kt"
UNIT_KBBL = "kbbl"


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

#: Grenzpartner in Energy-Charts ``/cbet`` und ``/cbpf``: Name -> Kürzel im Reihen-Code
#: ``xb_trade_<kürzel>`` (Handel) bzw. ``xb_flow_<kürzel>`` (physikalischer Fluss); ``net`` ist
#: die Summe über alle Grenzen. Positiv heißt Import nach Deutschland.
XB_PARTNERS: dict[str, str] = {
    "Austria": "at",
    "Belgium": "be",
    "Czech Republic": "cz",
    "Denmark": "dk",
    "France": "fr",
    "Luxembourg": "lu",
    "Netherlands": "nl",
    "Norway": "no",
    "Poland": "pl",
    "Sweden": "se",
    "Switzerland": "ch",
    "sum": "net",
}

#: Nachbarzonen mit Day-Ahead-Lastprognose (ENTSO-E 6.1.b, Freigabeliste), Reihen-Code
#: ``load_da_forecast_<kürzel>`` mit den Kürzeln aus ``NEIGHBOUR_ZONES``
LOAD_FORECAST_ZONES: tuple[str, ...] = (
    "at",
    "be",
    "ch",
    "cz",
    "dk1",
    "dk2",
    "fr",
    "nl",
    "no2",
    "pl",
    "se4",
)

#: Gebiete der Gasspeicher-Reihen: Kürzel im Reihen-Code -> AGSI-Parameter bzw. Eurostat-Geo.
#: Neben DE die Nachbarn mit Speichern, die (auch) am deutschen Markt hängen (Haidach und
#: 7Fields in AT, Grijpskerk/Norg in NL, Dolní Bojanovice in CZ), und die EU.
GAS_STORAGE_AREAS: dict[str, tuple[dict[str, str], str]] = {
    "de": ({"country": "DE"}, "DE"),
    "at": ({"country": "AT"}, "AT"),
    "nl": ({"country": "NL"}, "NL"),
    "cz": ({"country": "CZ"}, "CZ"),
    "eu": ({"type": "eu"}, "EU27_2020"),
}

#: Gebiete der LNG-Reihen (GIE ALSI): DE und die Terminals, über die LNG nach Deutschland
#: kommt (Rotterdam, Zeebrugge, Dunkerque), und die EU
LNG_AREAS: dict[str, dict[str, str]] = {
    "de": {"country": "DE"},
    "nl": {"country": "NL"},
    "be": {"country": "BE"},
    "fr": {"country": "FR"},
    "eu": {"type": "eu"},
}

#: Herkunftsländer der Gasimporte (Eurostat ``nrg_ti_gasm``) für DE und EU; ``nsp`` = nicht
#: angegeben. Kürzel im Reihen-Code -> Eurostat-Partner
GAS_IMPORT_PARTNERS: dict[str, str] = {
    "no": "NO",
    "ru": "RU",
    "us": "US",
    "qa": "QA",
    "dz": "DZ",
    "uk": "UK",
    "nl": "NL",
    "be": "BE",
    "nsp": "NSP",
}
GAS_IMPORT_AREAS = ("de", "eu")

#: Monatsbilanz Erdgas (Eurostat ``nrg_cb_gasm``): Kürzel im Reihen-Code -> Bilanzposition
GAS_BALANCE: dict[str, str] = {
    "import": "IMP",
    "export": "EXP",
    "production": "IPRD",
    "consumption": "IC_OBS",
    "power_heat": "TI_EHG_MAP",  # Einsatz in Strom- und Wärmeerzeugung (Versorger)
    "stock_change": "STK_CHG_MG",  # Einspeicherung positiv (Verbrauch = Aufkommen - Änderung)
}

#: Gebiete der Öl- und Kohlereihen von Eurostat (Kürzel aus ``GAS_STORAGE_AREAS``)
FUEL_STAT_AREAS = ("de", "eu")

#: Rohöleinfuhr DE (Eurostat ``nrg_ti_coifpm``, alle Felder): Kürzel -> Indikator
OIL_CRUDE_IMPORT: dict[str, str] = {
    "price": "AVGPRC_USD_BBL",  # mittlerer CIF-Preis frei Grenze
    "volume": "VOL_THS_BBL",
}

#: Monatsbilanz Rohöl (Eurostat ``nrg_cb_oilm``): Kürzel im Reihen-Code -> Bilanzposition
OIL_CRUDE_BALANCE: dict[str, str] = {
    "import": "IMP",
    "refinery_intake": "RI_OBS",
    "stock_change": "STK_CHG",  # Aufbau positiv (Schlussbestand - Anfangsbestand)
}

#: Schlussbestände Öl im Inland (Eurostat ``nrg_stk_oilm``): Kürzel -> Produkt
OIL_STOCK: dict[str, str] = {
    "crude": "O4100_TOT",
    "products": "O4600",
}

#: Herkunftsländer der Rohöleinfuhr DE (Eurostat ``nrg_ti_oilm``), die größten seit 2020;
#: Kürzel im Reihen-Code -> Eurostat-Partner
OIL_IMPORT_PARTNERS: dict[str, str] = {
    "no": "NO",
    "us": "US",
    "kz": "KZ",
    "ly": "LY",
    "uk": "UK",
    "ru": "RU",  # bis 2022 größter Lieferant
    "iq": "IQ",
    "ng": "NG",
    "az": "AZ",
    "sa": "SA",
}

#: Monatsbilanz Steinkohle (Eurostat ``nrg_cb_sffm``): Kürzel im Reihen-Code -> Position
COAL_BALANCE: dict[str, str] = {
    "import": "IMP",
    "import_non_eu": "IMP_FROM_NEU",
    "consumption": "GID_OBS",  # Bruttoinlandslieferungen (DE meldet hier die Einfuhr)
    "power_heat": "TI_EHG_MAP",  # Einsatz in Strom- und Wärmeerzeugung (Versorger)
}
#: Prognosen des EIA Short-Term Energy Outlook: Kürzel im Reihen-Code -> Einheit. Je Ausgabe
#: (Zeitstempel = Ausgabemonat) und Vorlauf k Monate eine Reihe ``steo_<kürzel>_h<kk>`` mit
#: der Prognose für den Monat Ausgabemonat + k. So bleibt jede Ausgabe erhalten (``energy.value``
#: hat nur einen Wert je Reihe und Zeitstempel). Der STEO reicht bis Dezember des Folgejahrs,
#: also 12 bis 23 Monate.
OUTLOOK_SERIES: dict[str, str] = {"brent": UNIT_USD_BBL, "henry_hub": UNIT_USD_MMBTU}
OUTLOOK_HORIZONS = range(24)


def outlook_code(name: str, horizon: int) -> str:
    return f"steo_{name}_h{horizon:02d}"


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
    GAS_DAY = "gas_day"  # Gasspeicher und LNG je Gastag (GIE AGSI+, ALSI)
    STOCK_MONTHLY = "stock_monthly"  # Bestand am Monatsende (Eurostat)
    FLOW_MONTHLY = "flow_monthly"  # Menge je Monat (Eurostat)
    PRICE_MONTHLY_STAT = "price_monthly_stat"  # Monatsmittel aus der Statistik (Eurostat)
    STOCK_MONTHLY_SLOW = "stock_monthly_slow"  # wie STOCK_MONTHLY, später veröffentlicht
    FLOW_MONTHLY_SLOW = "flow_monthly_slow"  # wie FLOW_MONTHLY, später veröffentlicht
    OUTLOOK_MONTHLY = "outlook_monthly"  # Prognose je Ausgabe, Zeitstempel = Ausgabemonat (STEO)


@dataclass(frozen=True)
class SeriesInfo:
    """@relation(SWR-ENI-020, scope=class)"""

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
        # Day-Ahead-Lastprognosen der Nachbarzonen (ENTSO-E 6.1.b, CC BY 4.0)
        *(
            SeriesInfo(f"load_da_forecast_{z}", Kind.LOAD_DA_FORECAST, UNIT_MW)
            for z in LOAD_FORECAST_ZONES
        ),
        # Grenzüberschreitender Handel und physikalische Flüsse (Energy-Charts), + = Import
        *(
            SeriesInfo(f"xb_{kind}_{p}", Kind.ACTUAL, UNIT_MW)
            for kind in ("trade", "flow")
            for p in XB_PARTNERS.values()
        ),
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
        # Gasspeicher je Gastag (AGSI+): Füllstand am Ende des Gastags, Füllgrad, Ein- und
        # Ausspeisung, Arbeitsgasvolumen
        *(
            SeriesInfo(f"gas_{q}_{a}", Kind.GAS_DAY, unit)
            for a in GAS_STORAGE_AREAS
            for q, unit in [
                ("storage", UNIT_TWH),
                ("storage_full", UNIT_PERCENT),
                ("injection", UNIT_GWH_D),
                ("withdrawal", UNIT_GWH_D),
                ("storage_wgv", UNIT_TWH),
            ]
        ),
        # LNG-Terminals je Gastag (ALSI): Bestand in den Tanks, Ausspeisung ins Netz,
        # Speicher- und Ausspeisekapazität
        *(
            SeriesInfo(f"lng_{q}_{a}", Kind.GAS_DAY, unit)
            for a in LNG_AREAS
            for q, unit in [
                ("inventory", UNIT_GWH),
                ("sendout", UNIT_GWH_D),
                ("dtmi", UNIT_GWH),
                ("dtrs", UNIT_GWH_D),
            ]
        ),
        # Eurostat je Monat (Suffix _m): Gasbestand am Monatsende im Inland, Bilanz,
        # Importe nach Herkunft
        *(
            SeriesInfo(f"gas_storage_{a}_m", Kind.STOCK_MONTHLY, UNIT_TWH)
            for a in GAS_STORAGE_AREAS
        ),
        *(
            SeriesInfo(f"gas_{b}_{a}_m", Kind.FLOW_MONTHLY, UNIT_TWH)
            for a in GAS_STORAGE_AREAS
            for b in GAS_BALANCE
        ),
        *(
            SeriesInfo(f"gas_import_{a}_{p}_m", Kind.FLOW_MONTHLY, UNIT_TWH)
            for a in GAS_IMPORT_AREAS
            for p in GAS_IMPORT_PARTNERS
        ),
        # Eurostat Öl und Steinkohle je Monat (kt, Rohöleinfuhr DE auch Preis und Volumen);
        # die EU-Summen und die Kohle erscheinen später als die Ölzahlen für DE
        SeriesInfo("oil_crude_import_price_de_m", Kind.PRICE_MONTHLY_STAT, UNIT_USD_BBL),
        SeriesInfo("oil_crude_import_volume_de_m", Kind.FLOW_MONTHLY, UNIT_KBBL),
        *(
            SeriesInfo(
                f"oil_crude_{b}_{a}_m",
                Kind.FLOW_MONTHLY if a == "de" else Kind.FLOW_MONTHLY_SLOW,
                UNIT_KT,
            )
            for a in FUEL_STAT_AREAS
            for b in OIL_CRUDE_BALANCE
        ),
        *(
            SeriesInfo(
                f"oil_stock_{p}_{a}_m",
                Kind.STOCK_MONTHLY if a == "de" else Kind.STOCK_MONTHLY_SLOW,
                UNIT_KT,
            )
            for a in FUEL_STAT_AREAS
            for p in OIL_STOCK
        ),
        *(
            SeriesInfo(f"oil_crude_import_de_{p}_m", Kind.FLOW_MONTHLY, UNIT_KT)
            for p in OIL_IMPORT_PARTNERS
        ),
        *(
            SeriesInfo(f"coal_hard_{b}_{a}_m", Kind.FLOW_MONTHLY_SLOW, UNIT_KT)
            for a in FUEL_STAT_AREAS
            for b in COAL_BALANCE
        ),
        # Kohlebestände meldet DE seit 2019 nicht mehr (Wert 0), daher nur EU
        SeriesInfo("coal_hard_stock_eu_m", Kind.STOCK_MONTHLY_SLOW, UNIT_KT),
        # Prognosen der EIA (STEO) je Ausgabe und Vorlauf
        *(
            SeriesInfo(outlook_code(n, k), Kind.OUTLOOK_MONTHLY, unit)
            for n, unit in OUTLOOK_SERIES.items()
            for k in OUTLOOK_HORIZONS
        ),
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
    - Gasspeicher und LNG (GIE): Gastag D (Beginn 06:00) am Folgetag um 20:00 Ortszeit.
    - Monatsendbestand (Eurostat, Zeitstempel am Monatsende): 90 Tage danach.
    - Monatsmengen und -preise (Eurostat, Zeitstempel am Monatsbeginn): 90 Tage nach
      Monatsende.
    - Öl- und Kohlebilanz der EU, Kohle je Land (Eurostat): wie oben, aber 150 Tage.
    - STEO-Prognosen (Zeitstempel am Beginn des Ausgabemonats): 14 Tage danach.

    @relation(SWR-ENI-021, scope=function)
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
        case Kind.GAS_DAY:
            return ts + resolution + GAS_DAY_DELAY
        case Kind.STOCK_MONTHLY:
            return ts + MONTHLY_STAT_DELAY
        case Kind.FLOW_MONTHLY | Kind.PRICE_MONTHLY_STAT:
            return next_period(ts, 1) + MONTHLY_STAT_DELAY
        case Kind.STOCK_MONTHLY_SLOW:
            return ts + MONTHLY_STAT_DELAY_SLOW
        case Kind.FLOW_MONTHLY_SLOW:
            return next_period(ts, 1) + MONTHLY_STAT_DELAY_SLOW
        case Kind.OUTLOOK_MONTHLY:
            return ts + OUTLOOK_DELAY


def published_at(kind: Kind, ts: datetime, resolution: timedelta, fetched: datetime) -> datetime:
    """Zeitpunkt, ab dem der Wert nachweislich bekannt war.

    Wer einen Wert vor dem regulären Termin abruft, kennt ihn ab dem Abruf; beim Backfill
    liegt der Abruf weit danach, dann gilt der reguläre Termin.

    @relation(SWR-ENI-021, scope=function)
    """
    return min(fetched, estimate_published(kind, ts, resolution))


def floor_to(ts: datetime, step: timedelta) -> datetime:
    """@relation(SWR-ENI-020, scope=function)"""
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    return epoch + ((ts - epoch) // step) * step
