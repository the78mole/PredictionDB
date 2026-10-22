"""Konfiguration eines Modells bzw. Backtests (``config/ml/*.yaml``), per Pydantic geprüft.

Eine Datei beschreibt den Ausgabezeitpunkt, die Horizonte, die Zielgrößen beider Stufen, die
Feature-Gruppen je Modellvariante und die Backtest-Faltungen. Der Hash der Datei geht in jede
Modellversion ein, damit sich ein Modell genau einer Konfiguration zuordnen lässt.
"""

from __future__ import annotations

import hashlib
from datetime import date
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field

FeatureGroup = Literal["calendar", "lags", "capacity", "weather", "stage1"]


class IssueConfig(BaseModel):
    """Wann eine Prognose ausgegeben wird und was zu diesem Zeitpunkt bekannt ist."""

    #: Ausgabezeitpunkt je Tag (UTC); 08 UTC liegt vor dem Gebotsschluss (12 Uhr Ortszeit)
    hour_utc: int = Field(default=8, ge=0, le=23)
    #: Liefertage nach dem Ausgabetag (Ortszeit Europe/Berlin), 1 = Folgetag
    lead_days: list[int] = Field(default_factory=lambda: [1, 2, 3, 4, 5, 6, 7])
    #: Ist-Werte (Last, Erzeugung) gelten ab Intervallende plus diesem Verzug als bekannt
    actual_delay_h: float = Field(default=2.0, ge=0)
    #: Day-Ahead-Preise eines Liefertags sind ab diesem Zeitpunkt am Vortag bekannt (Ortszeit)
    price_known_local_hour: int = Field(default=13, ge=0, le=23)
    #: Ein Wetterlauf ist ab ``run_time`` plus diesem Verzug vollständig verarbeitet
    nwp_delay_h: float = Field(default=6.0, ge=0)


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


class Variant(BaseModel):
    """Eine Modellvariante (Feature-Gruppen); jede Variante wird getrennt trainiert."""

    name: str
    groups: list[FeatureGroup]
    #: Nur Ausgabezeitpunkte mit vollständigem Wetterlauf verwenden
    requires_weather: bool = False
    #: Frühester Ausgabetag fürs Training (z. B. Beginn GFS v16 oder der Gebotszone DE-LU)
    train_from: date | None = None


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


class PromotionRule(BaseModel):
    """Champion/Challenger (ARCHITEKTUR.md 7.7)."""

    #: Challenger muss im Mittel über alle Horizonte mindestens so viel besser sein (relativ)
    min_gain: float = 0.0
    #: und darf in keiner Gruppe (Vorlauftag, Jahreszeit) mehr als so viel schlechter sein
    max_group_loss: float = 0.05
    min_group_n: int = 200


class ModelConfig(BaseModel):
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
    #: Reihen, die als Lags in die Merkmale gehen (zusätzlich zu den Zielgrößen)
    lag_series: list[str] = Field(default_factory=list)
    #: Wetter-Merkmale aus dem Snapshot (``<technologie>_<feature>``); leer = alle
    weather_features: list[str] = Field(default_factory=list)

    @property
    def targets(self) -> list[str]:
        return [t.code for t in self.stage1 + self.stage2]

    def variant(self, name: str) -> Variant:
        for v in self.variants:
            if v.name == name:
                return v
        raise KeyError(f"Variante {name!r} fehlt in der Konfiguration {self.name!r}")


def load_config(path: str | Path) -> tuple[ModelConfig, str]:
    """Konfiguration und SHA-256 der Datei."""
    raw = Path(path).read_bytes()
    return ModelConfig.model_validate(yaml.safe_load(raw)), hashlib.sha256(raw).hexdigest()
