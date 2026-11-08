"""Konfiguration eines Modells bzw. Backtests (``config/ml/*.yaml``), per Pydantic geprüft.

Eine Datei beschreibt den Ausgabezeitpunkt, die Horizonte, die Zielgrößen beider Stufen, die
Feature-Gruppen je Modellvariante und die Backtest-Faltungen. Der Hash der Datei geht in jede
Modellversion ein, damit sich ein Modell genau einer Konfiguration zuordnen lässt.

@relation(SWA-MLT-CONFIG, scope=file)
"""

from __future__ import annotations

import hashlib
from datetime import date, timedelta
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, model_validator

#: Feste Gruppen; dazu kommen die Reihengruppen aus ``series_groups`` unter ihrem Namen
BUILTIN_GROUPS = (
    "calendar",
    "lags",
    "capacity",
    "weather",
    "nachbarwetter",
    "nwp_version",
    "stage1",
)
FeatureGroup = str


class IssueConfig(BaseModel):
    """Wann eine Prognose ausgegeben wird und was zu diesem Zeitpunkt bekannt ist.

    @relation(SWR-MLT-020, scope=class)
    """

    #: Ausgabezeitpunkt je Tag (UTC); 08 UTC liegt vor dem Gebotsschluss (12 Uhr Ortszeit)
    hour_utc: int = Field(default=8, ge=0, le=23)
    #: Mehrere Ausgabestunden fürs Training (UTC), reihum je Ausgabetag eine: Das Modell lernt
    #: so alle Tageszeiten, zu denen ml-predict stündlich rechnet, bei gleicher Datenmenge.
    #: Leer = nur ``hour_utc``
    hours_utc: list[int] = Field(default_factory=list)
    #: Liefertage nach dem Ausgabetag (Ortszeit Europe/Berlin), 1 = Folgetag, 0 = Rest des
    #: Ausgabetags ab der Ausgabestunde
    lead_days: list[int] = Field(default_factory=lambda: [1, 2, 3, 4, 5, 6, 7])
    #: Ist-Werte (Last, Erzeugung) gelten ab Intervallende plus diesem Verzug als bekannt
    actual_delay_h: float = Field(default=2.0, ge=0)
    #: Day-Ahead-Preise eines Liefertags sind ab diesem Zeitpunkt am Vortag bekannt (Ortszeit)
    price_known_local_hour: int = Field(default=13, ge=0, le=23)
    #: Ein Wetterlauf ist ab ``run_time`` plus diesem Verzug vollständig verarbeitet
    nwp_delay_h: float = Field(default=6.0, ge=0)

    @property
    def train_hours(self) -> list[int]:
        return self.hours_utc or [self.hour_utc]

    @model_validator(mode="after")
    def _hours(self) -> IssueConfig:
        if any(not 0 <= h <= 23 for h in self.hours_utc):
            raise ValueError("issue.hours_utc: Stunden 0 bis 23")
        if any(d < 0 for d in self.lead_days):
            raise ValueError("issue.lead_days: Liefertage ab 0")
        return self


class StageTarget(BaseModel):
    """Eine Zielgröße: Reihen-Code in ``energy.value_best`` und Benchmarks dazu."""

    code: str
    #: Rolle für die Residuallast in Stufe 2 (Last minus Erzeugung)
    role: Literal["load", "generation", "price", "other"] = "other"
    #: Prognosen Dritter für dieselbe Größe (nur Day-Ahead), z. B. ``load_da_forecast``
    benchmarks: list[str] = Field(default_factory=list)
    #: Ziel durch die Summe dieser Kapazitätsspalten teilen (Kapazitätsfaktor lernen)
    normalize_by: list[str] = Field(default_factory=list)
    #: Ziel relativ zu dieser Merkmalsspalte lernen, z. B. ``lag_da_price_de_lu_mean_7d``
    center: str | None = None


LagKind = Literal["day", "week", "mean_day", "mean_7d", "mean_28d", "last"]


class SeriesGroup(BaseModel):
    """Weitere Reihen aus ``energy.value_best`` als eigene Merkmalsgruppe.

    - ``lags``: wie die Lags der Ziele (gleiche Stunde am letzten vollständigen Tag, Tages- und
      7-Tage-Mittel …); Reihen ``da_price_*`` gelten wie der Preis DE-LU ab dem Auktionsergebnis
      am Vortag als bekannt, alle anderen wie Ist-Werte.
    - ``forecast``: Prognose Dritter für die Zielstunde selbst, bekannt ab ``known_local_hour``
      (Ortszeit) am Vortag des Liefertags, sonst leer (z. B. Lastprognose der ÜNB nur für D+1).
    - ``asof``: letzter zum Ausgabezeitpunkt veröffentlichter Wert (Spalte ``published`` im
      Snapshot) und seine Änderung gegenüber dem Stand ``change_days`` Tage davor; für Tages-,
      Wochen- und Monatswerte wie Brennstoffpreise und Marktwerte.
    - ``grenzkosten``: implizites Brennstoff- und CO2-Niveau aus dem Strommarkt selbst. Je
      Ausgabezeitpunkt der Median des Preises (``codes[0]``) über die zuletzt bekannten
      ``windows`` Tage, einmal in den Stunden hoher Residuallast (oberes Quartil des Fensters,
      Gas oder Steinkohle preissetzend) und einmal in den Stunden, in denen Braunkohle in
      Teillast läuft und Steinkohle und Gas fast stehen (Braunkohle preissetzend, folgt dem
      CO2-Preis; docs/analysen/eua-rueckrechnung). Dazu ``gk_niveau`` (kürzestes Fenster mit
      Wert, mindestens ``floor``) als Bezugsgröße für ``Variant.scale`` und das letzte
      Tagesmittel des Preises relativ dazu.
    - ``outlook``: erwartete Änderung aus der jüngsten veröffentlichten Ausgabe einer
      Monatsprognose (EIA STEO). ``codes`` sind die Stämme (``steo_brent``), gespeichert je
      Vorlauf k als ``<stamm>_h<kk>``; Merkmal ``ol_<stamm>_h<k>`` = log(Prognose Vorlauf k /
      Prognose laufender Monat) für jedes k in ``horizons``.
    - ``kuehlwasser``: Kühlwasserlage an Kraftwerksstandorten (``site_weather``). ``codes`` sind
      die Tagesabflüsse ``river_flow_<pegel>`` (Hub'eau); je Standort Lufttemperatur am
      Gitterpunkt als Mittel des Zieltags und der 7 und 30 Tage bis dahin (``kw_<s>_t1``,
      ``_t7``, ``_t30``), der zuletzt veröffentlichte Abfluss relativ zum Median der 365 Tage
      davor (``kw_<s>_q_rel``) und als Kennzahl die gefährdete Leistung ``kw_risiko_mw``
      (dazu ``kw_hitze_mw`` und ``kw_niedrigwasser_mw``, docs/analysen/kkw-kuehlwasser-fr).
    - ``nachbarindex``: Wind- und PV-Index der Nachbarn als wenige Kennzahlen. ``codes`` sind
      die installierten Leistungen ``cap_<technik>_<land>`` (Energy-Charts, je Jahr); je Land
      zählt der zum Ausgabezeitpunkt veröffentlichte Stand mal dem mittleren ``wind_cf`` bzw.
      ``pv_cf`` seiner Zonen aus ``neighbour_weather`` (gleicher Lauf wie das DE-Wetter).
      Merkmale ``nbi_wind_mw``, ``nbi_pv_mw`` (Summe der Länder), ``nbi_wind_cf``,
      ``nbi_pv_cf`` (leistungsgewichtet) und ``nbi_cap_wind_mw``, ``nbi_cap_pv_mw``.

    @relation(SWR-MLT-026, SWR-MLT-029, SWR-MLT-033, scope=class)
    """

    mode: Literal[
        "lags", "forecast", "asof", "outlook", "grenzkosten", "nachbarindex", "kuehlwasser"
    ]
    codes: list[str]
    lags: list[LagKind] = Field(
        default_factory=lambda: list[LagKind](["day", "mean_day", "mean_7d"])
    )
    known_local_hour: int = Field(default=10, ge=0, le=23)
    change_days: int = Field(default=28, ge=1)
    horizons: list[int] = Field(default_factory=lambda: [1, 2, 3])
    #: ``grenzkosten``: Fenster in Tagen, Mindestniveau (EUR/MWh) und die Reihen der Klassen
    windows: list[int] = Field(default_factory=lambda: [14, 28, 56])
    floor: float = Field(default=20.0, gt=0)
    residual: str = "residual_load"
    lignite: str = "gen_lignite"
    hard_coal: str = "gen_hard_coal"
    gas: str = "gen_gas"

    @property
    def stored_codes(self) -> list[str]:
        """Reihen in ``energy.value_best``, die die Gruppe braucht."""
        if self.mode == "grenzkosten":
            extra = [self.residual, self.lignite, self.hard_coal, self.gas]
            return list(dict.fromkeys([*self.codes, *extra]))
        if self.mode != "outlook":
            return list(self.codes)
        ks = sorted({0, *self.horizons})
        return [f"{c}_h{k:02d}" for c in self.codes for k in ks]


class LgbmParams(BaseModel):
    num_leaves: int = 63
    learning_rate: float = 0.05
    n_estimators: int = 500
    min_child_samples: int = 50
    subsample: float = 0.8
    subsample_freq: int = 1
    colsample_bytree: float = 0.8
    reg_lambda: float = 1.0
    n_jobs: int = 0


class NeighbourWeather(BaseModel):
    """Wetter-Features der Nachbarzonen aus dem Gold einer zweiten Quelle (GFS Europa).

    Die Zonen sind die festen Regionen von ``wx-process`` (``config/gefs/regions_v2.json.gz``,
    Codes ``gefs:<zone>:area`` in ``meta.region``). Im Snapshot stehen sie als Wetter-Features
    ``nb_<zone>_<feature>``, in den Merkmalen als Gruppe ``nachbarwetter``
    (docs/analysen/austausch-nachbarlaender).

    @relation(SWR-MLT-028, scope=class)
    """

    source: str = "gfs_0p5"
    #: Zonen wie in ``regions_v2`` ohne Präfix und Technik, z. B. ``FR``, ``DK1``, ``NO2``
    zones: list[str]
    features: list[str] = Field(default_factory=lambda: ["wind_cf", "pv_cf"])

    def region_codes(self) -> list[str]:
        return [f"gefs:{z}:area" for z in self.zones]


#: Präfix der Nachbarzonen-Features im Snapshot (``weather.parquet``)
NEIGHBOUR_PREFIX = "nb_"
#: Präfix der Standort-Features im Snapshot
SITE_PREFIX = "st_"


class CoolingSite(BaseModel):
    """Ein Kraftwerksstandort mit Kühlwasser aus einem Fluss.

    Die Schwellen stammen aus docs/analysen/kkw-kuehlwasser-fr (``tabellen/schwellen.csv``):
    ab ``t7_from`` °C im 7-Tage-Mittel bzw. unter ``flow_below`` des Median-Abflusses steigt der
    Anteil der Tage mit Drosselung steil an.

    @relation(SWR-MLT-033, scope=class)
    """

    #: Kürzel wie in ``config/process/kkw_standorte.json`` (Region ``KKW_<code>:site``)
    code: str
    #: Nettoleistung des Standorts in MW
    mw: float = Field(gt=0)
    #: Tagesabflüsse in Reihenfolge des Vorrangs (der erste mit Wert zählt)
    flows: list[str] = Field(default_factory=list)
    t7_from: float | None = None
    flow_below: float | None = Field(default=None, gt=0)


class SiteWeather(BaseModel):
    """Wetter an festen Standorten aus dem Gold von GFS Europa (Regionen ``gefs:KKW_<s>:site``),
    im Snapshot als ``st_<s>_<feature>``, in den Merkmalen über die Gruppe ``kuehlwasser``.

    @relation(SWR-MLT-033, scope=class)
    """

    source: str = "gfs_0p5"
    features: list[str] = Field(default_factory=lambda: ["t2m"])
    sites: list[CoolingSite]
    #: Breite der Rampe über der Temperaturschwelle (K) und unter der Abflussschwelle
    ramp_t: float = Field(default=4.0, gt=0)
    ramp_q: float = Field(default=0.2, gt=0)
    #: Tage Wetter vor dem ersten Lauf im Snapshot (für das 30-Tage-Mittel)
    history_days: int = Field(default=31, ge=0)

    def region_codes(self) -> list[str]:
        return [f"gefs:KKW_{s.code}:site" for s in self.sites]

    @property
    def flow_codes(self) -> list[str]:
        return list(dict.fromkeys(c for s in self.sites for c in s.flows))


def site_feature(region_code: str, feature: str) -> str:
    """Name im Snapshot: ``gefs:KKW_BUG:site`` und ``t2m`` -> ``st_bug_t2m``."""
    site = region_code.split(":")[1].removeprefix("KKW_").lower()
    return f"{SITE_PREFIX}{site}_{feature}"


def zone_country(zone: str) -> str:
    """Land einer Gebotszone: ``DK1`` -> ``dk``, ``NO2`` -> ``no``, ``FR`` -> ``fr``."""
    return zone.rstrip("0123456789").lower()


def neighbour_feature(region_code: str, feature: str) -> str:
    """Name im Snapshot: ``gefs:DK1:area`` und ``wind_cf`` -> ``nb_dk1_wind_cf``."""
    zone = region_code.split(":")[1].lower().replace("-", "_")
    return f"{NEIGHBOUR_PREFIX}{zone}_{feature}"


class Variant(BaseModel):
    """Eine Modellvariante (Feature-Gruppen); jede Variante wird getrennt trainiert."""

    name: str
    groups: list[FeatureGroup]
    #: Nur Ausgabezeitpunkte mit vollständigem Wetterlauf verwenden
    requires_weather: bool = False
    #: Frühester Ausgabetag fürs Training (z. B. Beginn GFS v16 oder der Gebotszone DE-LU)
    train_from: date | None = None
    #: Ziel zusätzlich durch diese Merkmalsspalte teilen, je Ziel-Code (z. B. Preis durch
    #: ``gk_niveau``): Abweichungen werden relativ zum Niveau gelernt, nicht in EUR/MWh
    scale: dict[str, str] = Field(default_factory=dict)


class BacktestConfig(BaseModel):
    """Expanding Window: je Faltung Training bis vor ``test_from``, Test über ``fold_months``."""

    test_from: date
    test_to: date | None = None
    fold_months: int = Field(default=6, ge=1, le=24)
    #: Abstand zwischen letztem Trainings-Liefertag und erstem Test-Ausgabetag
    gap_days: int = Field(default=8, ge=0)
    #: Stufe 1 out-of-fold für das Training von Stufe 2 (zeitlich zusammenhängende Blöcke)
    oof_folds: int = Field(default=3, ge=2, le=10)
    #: nur jeden n-ten Ausgabetag trainieren (aufeinanderfolgende Tage überlappen stark)
    train_issue_stride: int = Field(default=2, ge=1)


class CompareWindow(BaseModel):
    """Festes Vergleichsfenster: Ausgabetage, auf denen jeder Backtest zusätzlich bewertet wird.

    Backtests haben je Aufruf eigene Testzeiträume (``--test-from``, ``--fold-months``). Die
    Kennzahlen über alle Testzeilen sind deshalb zwischen Läufen nicht vergleichbar. Im Fenster
    rechnet jeder Lauf, der es vollständig abdeckt, auf denselben Ausgabetagen; das Fenster wird
    nur bewusst verschoben (neuer ``name``), siehe docs/BACKTEST.md Kap. 7.

    @relation(SWR-MLT-042, scope=class)
    """

    #: Kennung in Ausgabedateien und ``meta.model_metric.scope``, z. B. ``w2024_2025``
    name: str = Field(pattern=r"^[a-z0-9_]+$", max_length=40)
    #: erster und letzter Ausgabetag (UTC-Datum von ``issue_time``), beide einschließlich
    start: date
    end: date

    @model_validator(mode="after")
    def _order(self) -> CompareWindow:
        if self.name == "all":
            raise ValueError("compare.name: 'all' steht für alle Testzeilen")
        if self.end < self.start:
            raise ValueError("compare: end liegt vor start")
        return self


#: Präfix der Namen mitlaufender Fenster in Dateien und ``meta.model_metric.scope``
ROLLING_PREFIX = "roll_"
#: Letzter Monat jeder Jahreszeit (meteorologisch: Winter Dez. bis Feb., Frühling März bis Mai …)
SEASON_END_MONTHS = (2, 5, 8, 11)


def _month_end(year: int, month: int) -> date:
    nxt = date(year + month // 12, month % 12 + 1, 1)
    return nxt - timedelta(days=1)


class RollingWindow(BaseModel):
    """Mitlaufendes Vergleichsfenster: die letzten ``months`` Monate bis zum Ende des letzten
    vollständigen Monats (``until: month``) bzw. der letzten vollständigen Jahreszeit
    (``until: season``) vor dem Stichtag.

    Anders als ``compare`` folgt es der Zeit und ist nicht an Kalenderjahre gebunden. Läufe
    sind untereinander vergleichbar, solange sie dasselbe aufgelöste Fenster haben (gleicher
    Name, z. B. ``roll_24m_202411_202610``).

    @relation(SWR-MLT-045, scope=class)
    """

    months: int = Field(default=24, ge=1, le=120)
    until: Literal["month", "season"] = "month"

    def resolve(self, today: date) -> CompareWindow:
        """Konkretes Fenster zum Stichtag ``today`` (der Stichtag selbst gehört nicht dazu).

        @relation(SWR-MLT-045, scope=function)
        """
        y, m = today.year, today.month - 1  # Vormonat
        if m == 0:
            y, m = y - 1, 12
        if self.until == "season":
            while m not in SEASON_END_MONTHS:
                y, m = (y - 1, 12) if m == 1 else (y, m - 1)
        end = _month_end(y, m)
        k = y * 12 + (m - 1) - (self.months - 1)
        start = date(k // 12, k % 12 + 1, 1)
        name = f"{ROLLING_PREFIX}{self.months}m_{start:%Y%m}_{end:%Y%m}"
        return CompareWindow(name=name, start=start, end=end)


class CalibrationConfig(BaseModel):
    """Nachkalibrierung der Quantile je Ziel und Vorlauftag (konformal, Romano et al. 2019).

    Ein zweites Modell wird ohne die letzten ``months`` Monate trainiert und sagt diese voraus.
    Aus den Abweichungen ergibt sich je Ziel, Vorlauftag und Quantil eine Verschiebung im
    Lernraum (nach Kapazitäts- und Niveau-Normierung), sodass z. B. P10 dort wirklich 10 % der
    Ist-Werte unterschreiten. Der Median bleibt unverändert. Kostet eine zweite Anpassung.

    @relation(SWR-MLT-031, scope=class)
    """

    #: Länge des Kalibrierzeitraums am Ende der Trainingsdaten; 0 = aus
    months: int = Field(default=0, ge=0, le=24)
    #: Mindestzahl Zeilen je Ziel und Vorlauftag, sonst keine Verschiebung
    min_rows: int = Field(default=200, ge=10)


class PromotionRule(BaseModel):
    """Champion/Challenger (ARCHITEKTUR.md 7.7).

    @relation(SWR-MLT-051, scope=class)
    """

    #: Challenger muss im Mittel über alle Horizonte mindestens so viel besser sein (relativ)
    min_gain: float = 0.0
    #: und darf in keiner Gruppe (Vorlauftag, Jahreszeit) mehr als so viel schlechter sein
    max_group_loss: float = 0.05
    min_group_n: int = 200


class ModelConfig(BaseModel):
    """@relation(SWR-MLT-002, scope=class)"""

    name: str
    title: str = ""
    quantiles: list[float] = Field(default_factory=lambda: [0.1, 0.5, 0.9])
    issue: IssueConfig = Field(default_factory=IssueConfig)
    #: Stufe 1: Last, Wind, PV; Stufe 2: Preis mit Stufe-1-Prognosen als Eingang
    stage1: list[StageTarget]
    stage2: list[StageTarget]
    variants: list[Variant]
    lgbm: LgbmParams = Field(default_factory=LgbmParams)
    backtest: BacktestConfig
    promotion: PromotionRule = Field(default_factory=PromotionRule)
    calibration: CalibrationConfig = Field(default_factory=CalibrationConfig)
    #: festes Vergleichsfenster für Kennzahlen, die zwischen Läufen vergleichbar sind
    compare: CompareWindow | None = None
    #: mitlaufendes Vergleichsfenster, z. B. die letzten 24 Monate bis zum Vormonat
    compare_rolling: RollingWindow | None = None
    #: Reihen, die als Lags in die Merkmale gehen (zusätzlich zu den Zielgrößen)
    lag_series: list[str] = Field(default_factory=list)
    #: Reihen, deren Lags ``day`` und ``week`` dieselbe Uhrzeit in Ortszeit nehmen (Verhalten
    #: nach der Uhr, z. B. Last); alle anderen greifen k·24 h zurück (Sonnenstand, z. B. PV).
    #: Über eine Zeitumstellung hinweg unterscheiden sich beide um eine Stunde.
    lag_local_time: list[str] = Field(default_factory=list)
    #: Wetter-Merkmale aus dem Snapshot (``<technologie>_<feature>``); leer = alle
    weather_features: list[str] = Field(default_factory=list)
    #: Wetter der Nachbarzonen (Gruppe ``nachbarwetter``); ohne Angabe bleibt die Gruppe leer
    neighbour_weather: NeighbourWeather | None = None
    #: Wetter an Kraftwerksstandorten (Gruppe im Modus ``kuehlwasser``)
    site_weather: SiteWeather | None = None
    #: weitere Merkmalsgruppen aus Energie- und Preisreihen, Name -> Gruppe
    series_groups: dict[str, SeriesGroup] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _known_groups(self) -> ModelConfig:
        for name in self.series_groups:
            if name in BUILTIN_GROUPS:
                raise ValueError(f"Reihengruppe {name!r} überdeckt eine feste Gruppe")
        for v in self.variants:
            unknown = [
                g for g in v.groups if g not in BUILTIN_GROUPS and g not in self.series_groups
            ]
            if unknown:
                raise ValueError(f"Variante {v.name!r}: unbekannte Gruppen {unknown}")
        if self.neighbour_weather is None:
            idx = [n for n, g in self.series_groups.items() if g.mode == "nachbarindex"]
            if idx:
                raise ValueError(f"Gruppen {idx}: nachbarindex braucht neighbour_weather")
        for n, g in self.series_groups.items():
            if g.mode != "kuehlwasser":
                continue
            if self.site_weather is None:
                raise ValueError(f"Gruppe {n}: kuehlwasser braucht site_weather")
            lost = set(self.site_weather.flow_codes) - set(g.codes)
            if lost:
                raise ValueError(f"Gruppe {n}: Abflüsse {sorted(lost)} fehlen in codes")
        return self

    @property
    def series_codes(self) -> list[str]:
        """Alle Reihen der Reihengruppen (für den Snapshot)."""
        return list(dict.fromkeys(c for g in self.series_groups.values() for c in g.stored_codes))

    @property
    def targets(self) -> list[str]:
        return [t.code for t in self.stage1 + self.stage2]

    def compare_windows(self, today: date) -> list[CompareWindow]:
        """Festes und mitlaufendes Vergleichsfenster, soweit konfiguriert, zum Stichtag.

        @relation(SWR-MLT-045, scope=function)
        """
        out = [self.compare] if self.compare else []
        if self.compare_rolling:
            out.append(self.compare_rolling.resolve(today))
        return out

    def variant(self, name: str) -> Variant:
        for v in self.variants:
            if v.name == name:
                return v
        raise KeyError(f"Variante {name!r} fehlt in der Konfiguration {self.name!r}")


def load_config(path: str | Path) -> tuple[ModelConfig, str]:
    """Konfiguration und SHA-256 der Datei.

    @relation(SWR-MLT-002, scope=function)
    """
    raw = Path(path).read_bytes()
    return ModelConfig.model_validate(yaml.safe_load(raw)), hashlib.sha256(raw).hexdigest()
