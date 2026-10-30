"""Spiegel der Registry in der Datenbank (``meta.model_*``) für das Status-Dashboard.

Die Registry-Dateien bleiben die Quelle; der Spiegel wird bei jedem Abgleich aus ihnen neu
geschrieben. Backtest-Kennzahlen entstehen einmal je Version aus ``backtest.parquet`` (eine
Version ändert sich nach dem Ablegen nicht mehr).
"""

from __future__ import annotations

import json
from datetime import date
from typing import Any

import numpy as np
import pandas as pd
from sqlalchemy import Engine, text

from ml_train.metrics import add_context, score
from ml_train.registry import Registry
from predictiondb_core.log import get_logger

log = get_logger(__name__)

UPSERT_VERSION = text("""
    INSERT INTO meta.model_version (version, config, variant, role, created, train_from, train_to,
                                    train_rows, train_seconds, backtest_seconds, snapshot_id,
                                    git_commit, manifest, synced_at)
    VALUES (:version, :config, :variant, :role, :created, :train_from, :train_to, :train_rows,
            :train_seconds, :backtest_seconds, :snapshot_id, :git_commit,
            CAST(:manifest AS jsonb), now())
    ON CONFLICT (version) DO UPDATE SET
      role = EXCLUDED.role, manifest = EXCLUDED.manifest, synced_at = now()""")

INSERT_METRIC = text("""
    INSERT INTO meta.model_metric (version, target, lead_day, n, mae, rmse, bias, pinball,
                                   coverage80)
    VALUES (:version, :target, :lead_day, :n, :mae, :rmse, :bias, :pinball, :coverage80)
    ON CONFLICT (version, target, lead_day) DO NOTHING""")

INSERT_EVENT = text("""
    INSERT INTO meta.model_event (config, at, action, version, note)
    VALUES (:config, :at, :action, :version, :note)
    ON CONFLICT DO NOTHING""")


def roles(state: dict[str, Any]) -> dict[str, str]:
    """Rolle je Version; Versionen nur im Verlauf gelten als ``retired``."""
    out = {e["version"]: "retired" for e in state.get("history", [])}
    for v in state.get("previous", []):
        out[v] = "previous"
    if state.get("baseline"):
        out[state["baseline"]] = "baseline"
    for v in state.get("challengers", []):
        out[v] = "challenger"
    if state.get("champion"):
        out[state["champion"]] = "champion"
    return out


def _day(s: Any) -> date | None:
    return date.fromisoformat(str(s)[:10]) if s else None


def version_row(version: str, role: str, man: dict[str, Any], config: str) -> dict[str, Any]:
    days = man.get("train_issue_days") or [None, None]
    return {
        "version": version,
        "config": man.get("config", config),
        "variant": (man.get("variant") or {}).get("name") or version.split("-")[1],
        "role": role,
        "created": man.get("created"),
        "train_from": _day(days[0]),
        "train_to": _day(days[1]),
        "train_rows": man.get("train_rows"),
        "train_seconds": man.get("train_seconds"),
        "backtest_seconds": man.get("backtest_seconds"),
        "snapshot_id": man.get("snapshot_id"),
        "git_commit": man.get("git_commit"),
        "manifest": json.dumps(man, default=str),
    }


def metric_rows(version: str, bt: pd.DataFrame) -> list[dict[str, Any]]:
    """Kennzahlen je Ziel und Vorlauftag sowie über alle Vorlauftage (``lead_day`` -1; 0 ist
    der Rest des Ausgabetags)."""
    ctx = add_context(bt)
    parts = [score(ctx, ["target", "lead_day"]), score(ctx, ["target"]).assign(lead_day=-1)]
    m = pd.concat(parts, ignore_index=True)
    cols = ["mae", "rmse", "bias", "pinball", "coverage80"]
    m[cols] = m[cols].astype(float).replace({np.nan: None})
    return [
        {
            "version": version,
            "target": r["target"],
            "lead_day": int(r["lead_day"]),
            "n": int(r["n"]),
            **{c: r[c] for c in cols},
        }
        for r in m.to_dict("records")
    ]


def sync_registry(engine: Engine, registry: Registry) -> int:
    """Schreibt Versionen, Rollen, Verlauf und Kennzahlen; gibt die Zahl der Versionen zurück."""
    st = registry.state()
    config = registry.cfg.name
    by_role = roles(st)
    with engine.connect() as c:
        have = {
            r[0]
            for r in c.execute(
                text("SELECT DISTINCT version FROM meta.model_metric WHERE version = ANY(:v)"),
                {"v": list(by_role)},
            )
        }
    versions, metrics = [], []
    for version, role in by_role.items():
        try:
            man = registry.manifest(version)
        except KeyError:
            log.warning("sync.manifest_missing", version=version)
            continue
        versions.append(version_row(version, role, man, config))
        if version not in have:
            bt = registry.backtest(version)
            if bt is not None and not bt.empty:
                metrics += metric_rows(version, bt)
    events = [{"config": config, **e} for e in st.get("history", [])]
    with engine.begin() as c:
        if versions:
            c.execute(UPSERT_VERSION, versions)
        if metrics:
            c.execute(INSERT_METRIC, metrics)
        if events:
            c.execute(INSERT_EVENT, events)
    log.info("sync.done", config=config, versions=len(versions), metrics=len(metrics))
    return len(versions)
