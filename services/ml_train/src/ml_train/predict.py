"""Inferenz: alle aktiven Modellversionen für einen Ausgabezeitpunkt nach ``fcst.prediction``.

Für die Merkmale reicht ein kleiner Snapshot um den Ausgabezeitpunkt (Energie der letzten
Wochen, Wetterläufe der letzten Tage, Bestand). Je Version, Ziel, Zeitpunkt und Quantil wird
ein Wert geschrieben; ein erneuter Lauf zum selben Ausgabezeitpunkt überschreibt.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pandas as pd
from sqlalchemy import Engine, text

from ml_train.config import ModelConfig
from ml_train.dataset import FeatureBuilder
from ml_train.registry import Registry
from ml_train.snapshot import Snapshot, export_snapshot
from predictiondb_core.log import get_logger

log = get_logger(__name__)


def live_snapshot(engine: Engine, cfg: ModelConfig, issue: pd.Timestamp) -> Snapshot:
    codes = list(dict.fromkeys(cfg.targets + cfg.lag_series + cfg.series_codes))
    # Monats- und Jahreswerte erscheinen erst Wochen nach Periodenende; Änderungen brauchen den
    # Stand davor
    back = 45
    for g in cfg.series_groups.values():
        if g.mode == "asof":
            back = max(back, 400 + g.change_days)
    start = (issue - timedelta(days=back)).to_pydatetime()
    end = (issue + timedelta(days=max(cfg.issue.lead_days) + 2)).to_pydatetime()
    return export_snapshot(
        engine,
        codes,
        start,
        end,
        weather_from=(issue - timedelta(days=2)).to_pydatetime(),
        max_lead_h=24 * (max(cfg.issue.lead_days) + 2),
    )


def predict_versions(
    snap: Snapshot,
    cfg: ModelConfig,
    registry: Registry,
    versions: list[str],
    issue: pd.Timestamp,
    cache: Path,
) -> pd.DataFrame:
    """Prognosen in Langform: ``model_version, target, issue_time, valid_time, quantile, value``."""
    fb = FeatureBuilder(snap, cfg)
    frame = fb.build_at([issue])
    rows = []
    for version in versions:
        model = registry.load(version, cache)
        f = frame
        if model.variant.requires_weather:
            f = frame.subset(frame.X["wx_lead_h"].notna().to_numpy())
            if len(f) == 0:
                log.warning("predict.no_weather", version=version, issue=str(issue))
                continue
        for target, q in model.predict(f).items():
            for col in q.columns:
                rows.append(
                    pd.DataFrame(
                        {
                            "model_version": version,
                            "target": target,
                            "issue_time": issue,
                            "valid_time": f.meta["ts"].to_numpy(),
                            "quantile": int(col[1:]) / 100,
                            "value": q[col].to_numpy(),
                        }
                    )
                )
    return pd.concat(rows, ignore_index=True) if rows else pd.DataFrame()


UPSERT = text("""
    INSERT INTO fcst.prediction (model_version, target, issue_time, valid_time, quantile, value)
    VALUES (:model_version, :target, :issue_time, :valid_time, :quantile, :value)
    ON CONFLICT (model_version, target, issue_time, valid_time, quantile)
    DO UPDATE SET value = EXCLUDED.value""")


def write_predictions(engine: Engine, df: pd.DataFrame) -> int:
    if df.empty:
        return 0
    recs = df.assign(
        issue_time=pd.to_datetime(df["issue_time"], utc=True).dt.to_pydatetime(),
        valid_time=pd.to_datetime(df["valid_time"], utc=True).dt.to_pydatetime(),
    ).to_dict("records")
    with engine.begin() as c:
        c.execute(UPSERT, recs)
    return len(recs)
