"""Wichtigkeit der Merkmale einer Version, ohne Backtest.

Maß ist der Gain der LightGBM-Bäume, je Booster auf 100 % normiert und über die Quantile
gemittelt. Das zeigt, worauf das Modell im Training baut, nicht, was die Prognose verbessert:
Ähnliche Spalten teilen sich den Gain, und ein Merkmal kann im Training viel Gain sammeln und im
Test nichts bringen. Über die Aufnahme einer Merkmalsgruppe entscheidet weiter der Backtest.

@relation(SWA-MLT-IMPORTANCE, scope=file)
"""

from __future__ import annotations

import pandas as pd

from ml_train.calendar import CALENDAR_COLUMNS
from ml_train.config import NEIGHBOUR_PREFIX, ModelConfig, Variant
from ml_train.models import RESIDUAL, TwoStageModel

OTHER = "sonstige"


def feature_group(feature: str, cfg: ModelConfig, variant: Variant) -> str:
    """Merkmalsgruppe zu einer Spalte, aus dem Namen; die Version speichert die Gruppen nicht.

    @relation(SWR-MLT-072, scope=function)
    """
    if feature in CALENDAR_COLUMNS or feature in ("lead_day", "lead_h"):
        return "calendar"
    if feature.startswith("s1_") or feature == RESIDUAL:
        return "stage1"
    if feature.startswith("cap_"):
        return "capacity"
    if feature == "wx_gfs_v":
        return "nwp_version"
    if feature.startswith(f"wx_{NEIGHBOUR_PREFIX}"):
        return "nachbarwetter"
    if feature.startswith("wx_"):
        return "weather"
    for name in variant.groups:
        sg = cfg.series_groups.get(name)
        if sg is None:
            continue
        if sg.mode == "grenzkosten" and feature.startswith("gk_"):
            return name
        for code in sg.codes:
            if sg.mode == "lags" and any(feature == f"lag_{code}_{lag}" for lag in sg.lags):
                return name
            if sg.mode == "forecast" and feature == f"fc_{code}":
                return name
            if sg.mode == "asof" and feature in (f"asof_{code}", f"asof_{code}_chg"):
                return name
            if sg.mode == "outlook" and feature.startswith(f"ol_{code}_h"):
                return name
    return "lags" if feature.startswith("lag_") else OTHER


def feature_importance(model: TwoStageModel) -> pd.DataFrame:
    """Je Ziel und Merkmal: ``target, stage, feature, group, gain_pct`` (Summe je Ziel 100).

    @relation(SWR-MLT-072, scope=function)
    """
    rows = []
    for stage, models in (("stage1", model.stage1), ("stage2", model.stage2)):
        for target, qm in models.items():
            parts = []
            for booster in qm.boosters.values():
                gain = pd.Series(
                    booster.feature_importance("gain"), index=booster.feature_name(), dtype=float
                )
                total = gain.sum()
                parts.append(gain / total * 100 if total > 0 else gain)
            if not parts:
                continue
            mean = pd.concat(parts, axis=1).mean(axis=1)
            for feature, pct in mean.items():
                rows.append(
                    {
                        "target": target,
                        "stage": stage,
                        "feature": str(feature),
                        "group": feature_group(str(feature), model.cfg, model.variant),
                        "gain_pct": float(pct),
                    }
                )
    columns = ["target", "stage", "feature", "group", "gain_pct"]
    out = pd.DataFrame(rows, columns=columns)
    return out.sort_values(["stage", "target", "gain_pct"], ascending=[True, True, False])


def by_group(table: pd.DataFrame) -> pd.DataFrame:
    """Anteil je Ziel und Merkmalsgruppe, größte zuerst.

    @relation(SWR-MLT-072, scope=function)
    """
    g = table.groupby(["stage", "target", "group"], as_index=False).agg(
        gain_pct=("gain_pct", "sum"), features=("feature", "count")
    )
    return g.sort_values(["stage", "target", "gain_pct"], ascending=[True, True, False])
