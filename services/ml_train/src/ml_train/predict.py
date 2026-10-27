"""Inferenz: alle aktiven Modellversionen für einen Ausgabezeitpunkt nach ``fcst.prediction``.

Für die Merkmale reicht ein kleiner Snapshot um den Ausgabezeitpunkt (Energie der letzten
Wochen, Wetterläufe der letzten Tage, Bestand). Je Version, Ziel, Zeitpunkt und Quantil wird
ein Wert geschrieben; ein erneuter Lauf zum selben Ausgabezeitpunkt überschreibt.

Im Dauerbetrieb (``--watch``) läuft jede volle Stunde ein Ausgabezeitpunkt. Verpasste Stunden
(Neustart, langer Lauf) werden nachgeholt, damit z. B. der Lauf um 10 Uhr Ortszeit, zeitgleich
mit der Day-Ahead-Lastprognose der ÜNB, für den Vergleich im Dashboard nie fehlt. Nachholen
ist leakage-sicher: Merkmale nutzen nur Werte, die zum Ausgabezeitpunkt veröffentlicht waren.

Kommt eine Version neu hinzu (``--fill-new``), rechnet sie rückwirkend dieselben
Ausgabezeitpunkte, die die übrigen aktiven Versionen im Fenster schon haben. Sie erscheint so
sofort in den Vergleichsgrafiken neben Champion und Challengern, mit demselben
Veröffentlichungsstand der Eingänge wie diese.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pandas as pd
from sqlalchemy import Engine, text

from ml_train.config import ModelConfig, Variant
from ml_train.dataset import FeatureBuilder, FeatureFrame
from ml_train.models import TwoStageModel
from ml_train.registry import Registry
from ml_train.snapshot import Snapshot, export_snapshot
from predictiondb_core.log import get_logger

log = get_logger(__name__)


#: Tage, die ``asof``-Gruppen zurückschauen: Der Jahresmarktwert des Vorvorjahres steht am
#: 1. Januar und ist Mitte Januar noch der jüngste veröffentlichte (2 Jahre plus Verzug)
ASOF_BACK_DAYS = 2 * 366 + 14
#: jüngste Ausgabe einer Monatsprognose: Zeitstempel am Monatsbeginn, erscheint bis Mitte Monat
OUTLOOK_BACK_DAYS = 75


def lookback_days(cfg: ModelConfig, variant: Variant) -> int:
    """Tage vor dem Ausgabezeitpunkt, die die Merkmale einer Variante brauchen."""
    back = 45  # Lags: 28-Tage-Mittel, Vorwoche und Zeitumstellung mit Reserve
    for name in variant.groups:
        g = cfg.series_groups.get(name)
        if g is not None and g.mode == "asof":
            back = max(back, ASOF_BACK_DAYS + g.change_days)
        if g is not None and g.mode == "outlook":
            back = max(back, OUTLOOK_BACK_DAYS)
    return back


def due_issues(now: pd.Timestamp, last: pd.Timestamp | None, catch_up_h: int) -> list[pd.Timestamp]:
    """Volle Stunden seit ``last`` bis ``now``, höchstens ``catch_up_h`` Stunden zurück."""
    cur = now.floor("h")
    first = cur if last is None else last + pd.Timedelta(hours=1)
    first = max(first, cur - pd.Timedelta(hours=catch_up_h))
    return list(pd.date_range(first, cur, freq="h")) if first <= cur else []


def next_wake(now: pd.Timestamp, every_s: int, delay_s: int = 60) -> pd.Timestamp:
    """Nächster Weckzeitpunkt auf dem Raster ``every_s`` (ab Epoche) plus ``delay_s``."""
    step = pd.Timedelta(seconds=every_s)
    return (now - pd.Timedelta(seconds=delay_s)).floor(step) + step + pd.Timedelta(seconds=delay_s)


def last_issue(engine: Engine, versions: list[str]) -> pd.Timestamp | None:
    """Jüngster bereits geschriebener Ausgabezeitpunkt dieser Versionen."""
    with engine.connect() as c:
        t = c.execute(
            text("SELECT max(issue_time) FROM fcst.prediction WHERE model_version = ANY(:v)"),
            {"v": versions},
        ).scalar()
    return None if t is None else pd.Timestamp(t).tz_convert("UTC")


MISSING_ISSUES = text("""
    SELECT DISTINCT issue_time FROM fcst.prediction
     WHERE model_version = ANY(:peers) AND issue_time >= :since AND issue_time <= :until
    EXCEPT
    SELECT DISTINCT issue_time FROM fcst.prediction
     WHERE model_version = :version AND issue_time >= :since AND issue_time <= :until
    ORDER BY 1""")


def missing_issues(
    engine: Engine, version: str, peers: list[str], since: pd.Timestamp, until: pd.Timestamp
) -> list[pd.Timestamp]:
    """Ausgabezeitpunkte im Fenster, die andere Versionen haben, ``version`` aber nicht."""
    peers = [p for p in peers if p != version]
    if not peers:
        return []
    with engine.connect() as c:
        rows = c.execute(
            MISSING_ISSUES,
            {
                "peers": peers,
                "version": version,
                "since": since.to_pydatetime(),
                "until": until.to_pydatetime(),
            },
        ).scalars()
        return [pd.Timestamp(t).tz_convert("UTC") for t in rows]


def by_issue(missing: dict[str, list[pd.Timestamp]]) -> dict[pd.Timestamp, list[str]]:
    """Fehlende Ausgabezeitpunkte je Version zu Versionen je Ausgabezeitpunkt (aufsteigend),
    damit jeder Zeitpunkt nur einen Snapshot braucht."""
    out: dict[pd.Timestamp, list[str]] = {}
    for version, issues in missing.items():
        for issue in issues:
            out.setdefault(issue, []).append(version)
    return dict(sorted(out.items()))


def live_snapshot(engine: Engine, cfg: ModelConfig, issue: pd.Timestamp) -> Snapshot:
    codes = list(dict.fromkeys(cfg.targets + cfg.lag_series + cfg.series_codes))
    back = max(lookback_days(cfg, v) for v in cfg.variants)
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


def predict_model(
    frame: FeatureFrame, model: TwoStageModel, version: str, issue: pd.Timestamp
) -> pd.DataFrame:
    """Prognosen einer geladenen Version in Langform (leer, wenn ein nötiger Wetterlauf fehlt).

    Fehlt eine Merkmalsspalte ganz (Reihe zum Ausgabezeitpunkt nicht abrufbar), bleibt sie leer;
    LightGBM behandelt das wie im Training als fehlenden Wert.
    """
    f = frame
    if model.variant.requires_weather:
        if "wx_lead_h" not in frame.X.columns:
            log.warning("predict.no_weather", version=version, issue=str(issue))
            return pd.DataFrame()
        f = frame.subset(frame.X["wx_lead_h"].notna().to_numpy())
        if len(f) == 0:
            log.warning("predict.no_weather", version=version, issue=str(issue))
            return pd.DataFrame()
    missing = [c for c in model.features() if c not in f.X.columns]
    if missing:
        log.warning("predict.missing_features", version=version, columns=missing)
        f = FeatureFrame(
            f.X.reindex(columns=[*f.X.columns, *missing]), f.meta, f.y, f.benchmarks, f.groups
        )
    rows = []
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
    parts = [
        predict_model(frame, registry.load(version, cache), version, issue) for version in versions
    ]
    parts = [p for p in parts if not p.empty]
    return pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()


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
