"""Spiegel der Registry in der Datenbank (``meta.model_*``) für das Status-Dashboard.

Die Registry-Dateien bleiben die Quelle; der Spiegel wird bei jedem Abgleich aus ihnen neu
geschrieben. Backtest-Kennzahlen entstehen einmal je Version und Sicht (alle Zeilen, festes
Vergleichsfenster) aus ``backtest.parquet``; eine Version ändert sich nach dem Ablegen nicht mehr.

@relation(SWA-MLT-DBSYNC, scope=file)
"""

from __future__ import annotations

import json
from datetime import date
from typing import Any

import numpy as np
import pandas as pd
from sqlalchemy import Engine, text

from ml_train.config import CompareWindow
from ml_train.metrics import add_context, covers_window, in_window, score
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
    INSERT INTO meta.model_metric (version, scope, season, target, lead_day, n, mae, rmse, bias,
                                   pinball, coverage80)
    VALUES (:version, :scope, :season, :target, :lead_day, :n, :mae, :rmse, :bias, :pinball,
            :coverage80)
    ON CONFLICT (version, scope, season, target, lead_day) DO NOTHING""")

INSERT_SCOPE = text("""
    INSERT INTO meta.model_metric_scope (version, scope, covered)
    VALUES (:version, :scope, :covered)
    ON CONFLICT (version, scope) DO NOTHING""")

#: ``meta.model_metric.scope`` für Kennzahlen über alle Backtest-Zeilen einer Version
SCOPE_ALL = "all"
#: ``meta.model_metric.season`` für Kennzahlen über alle Jahreszeiten
SEASON_ALL = "all"

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
    """@relation(SWR-MLT-053, scope=function)"""
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


def _score_rows(version: str, scope: str, bt: pd.DataFrame) -> list[dict[str, Any]]:
    """Je Ziel und Vorlauftag (``lead_day`` -1 = alle), jeweils über alle Jahreszeiten
    (``season`` ``all``) und je Jahreszeit."""
    ctx = add_context(bt)
    parts = [
        score(ctx, ["target", "lead_day"]).assign(season=SEASON_ALL),
        score(ctx, ["target"]).assign(lead_day=-1, season=SEASON_ALL),
        score(ctx, ["target", "lead_day", "season"]),
        score(ctx, ["target", "season"]).assign(lead_day=-1),
    ]
    m = pd.concat(parts, ignore_index=True)
    cols = ["mae", "rmse", "bias", "pinball", "coverage80"]
    m[cols] = m[cols].astype(float).replace({np.nan: None})
    return [
        {
            "version": version,
            "scope": scope,
            "season": r["season"],
            "target": r["target"],
            "lead_day": int(r["lead_day"]),
            "n": int(r["n"]),
            **{c: r[c] for c in cols},
        }
        for r in m.to_dict("records")
    ]


def metric_rows(
    version: str,
    bt: pd.DataFrame,
    window: CompareWindow | None = None,
    scopes: set[str] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Kennzahlen je Ziel und Vorlauftag sowie über alle Vorlauftage (``lead_day`` -1; 0 ist
    der Rest des Ausgabetags), je Jahreszeit und über alle (``season`` ``all``), dazu je
    geprüfter Sicht eine Zeile für ``meta.model_metric_scope``.

    Einmal über alle Zeilen (``scope`` ``all``) und, wenn der Backtest das Vergleichsfenster
    vollständig abdeckt, nur im Fenster (``scope`` = Name des Fensters). Deckt er es nicht ab,
    wird das als ``covered = false`` vermerkt, damit der nächste Abgleich die Datei nicht erneut
    liest. ``scopes`` nennt die schon geprüften Sichten.

    @relation(SWR-MLT-053, SWR-MLT-042, SWR-MLT-043, scope=function)
    """
    have = scopes or set()
    rows: list[dict[str, Any]] = []
    checked: list[dict[str, Any]] = []
    if SCOPE_ALL not in have:
        rows += _score_rows(version, SCOPE_ALL, bt)
        checked.append({"version": version, "scope": SCOPE_ALL, "covered": True})
    if window is not None and window.name not in have:
        ok = covers_window(bt, window.start, window.end)
        if ok:
            rows += _score_rows(version, window.name, in_window(bt, window.start, window.end))
        checked.append({"version": version, "scope": window.name, "covered": ok})
    return rows, checked


def sync_registry(engine: Engine, registry: Registry) -> int:
    """Schreibt Versionen, Rollen, Verlauf und Kennzahlen; gibt die Zahl der Versionen zurück.

    @relation(SWR-MLT-053, scope=function)
    """
    st = registry.state()
    config = registry.cfg.name
    by_role = roles(st)
    window = registry.cfg.compare
    wanted = {SCOPE_ALL} | ({window.name} if window else set())
    have: dict[str, set[str]] = {}
    with engine.connect() as c:
        for v, sc in c.execute(
            text("SELECT version, scope FROM meta.model_metric_scope WHERE version = ANY(:v)"),
            {"v": list(by_role)},
        ):
            have.setdefault(v, set()).add(sc)
    versions: list[dict[str, Any]] = []
    metrics: list[dict[str, Any]] = []
    scopes: list[dict[str, Any]] = []
    for version, role in by_role.items():
        try:
            man = registry.manifest(version)
        except KeyError:
            log.warning("sync.manifest_missing", version=version)
            continue
        versions.append(version_row(version, role, man, config))
        # Eine Version ändert sich nach dem Ablegen nicht mehr: jede Sicht nur einmal prüfen
        if not wanted <= have.get(version, set()):
            bt = registry.backtest(version)
            if bt is not None and not bt.empty:
                rows, checked = metric_rows(version, bt, window, have.get(version, set()))
                metrics += rows
                scopes += checked
    events = [{"config": config, **e} for e in st.get("history", [])]
    with engine.begin() as c:
        if versions:
            c.execute(UPSERT_VERSION, versions)
        if metrics:
            c.execute(INSERT_METRIC, metrics)
        if scopes:
            c.execute(INSERT_SCOPE, scopes)
        if events:
            c.execute(INSERT_EVENT, events)
    log.info("sync.done", config=config, versions=len(versions), metrics=len(metrics))
    return len(versions)
