"""Backtests, Fits und Registry-Rollen nach MLflow melden (docs/MLFLOW.md).

Nur aktiv, wenn ``MLFLOW_TRACKING_URI`` gesetzt ist. Jede Funktion fängt Fehler ab und
protokolliert sie: Ein fehlender MLflow-Server hält weder Training noch Inferenz auf, wie der
Registry-Spiegel (``try_sync``). Die Datei-Registry bleibt die Quelle; MLflow bekommt Kennzahlen,
kleine Dateien und Verweise, keine großen Parquet-Dateien.

- **Backtest:** ein Elternlauf je Aufruf, darunter ein Unterlauf je Verfahren (Varianten und
  Vergleichsverfahren) mit Kennzahlen ``<ziel>.d<vorlauf>.<maß>`` und ``<ziel>.all.<maß>``, im
  festen Vergleichsfenster mit Präfix ``w.``, im mitlaufenden mit ``r.``.
- **Fit:** ein Lauf je registrierter Version mit Trainingsangaben.
- **Registry:** je Version eine Modellversion des registrierten Modells ``<konfiguration>``,
  Quelle ist der Registry-Ordner; Tag ``role`` und Aliase ``champion``, ``baseline``, ``previous``.

@relation(SWA-MLT-TRACKING, scope=file)
"""

from __future__ import annotations

import contextlib
import json
import math
import os
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pandas as pd

from predictiondb_core.log import get_logger

if TYPE_CHECKING:
    from mlflow import MlflowClient

    from ml_train.config import ModelConfig
    from ml_train.registry import Registry

log = get_logger(__name__)

MEASURES = ("mae", "rmse", "bias", "pinball", "coverage80")
#: Präfix der Kennzahlen je Sicht und Datei im Backtest-Ordner
VIEWS = {"": "metrics", "w.": "metrics_window", "r.": "metrics_rolling"}
#: kleine Dateien des Backtest-Ordners, die als Artefakt in den Elternlauf kommen
ARTIFACTS = ("lauf.json", "metrics*.csv", "vergleich_jahreszeit*.csv", "rank_corr.csv")
#: so viele Kennzahlen je Aufruf (Grenze der MLflow-API: 1000)
BATCH = 900


def enabled() -> bool:
    return bool(os.environ.get("MLFLOW_TRACKING_URI"))


def _client() -> MlflowClient:
    # kurze Zeitgrenzen: ein hängender Server soll den Lauf nicht minutenlang aufhalten
    os.environ.setdefault("MLFLOW_HTTP_REQUEST_TIMEOUT", "20")
    os.environ.setdefault("MLFLOW_HTTP_REQUEST_MAX_RETRIES", "2")
    from mlflow import MlflowClient

    return MlflowClient()


def _experiment(c: MlflowClient, name: str) -> str:
    e = c.get_experiment_by_name(name)
    return e.experiment_id if e is not None else c.create_experiment(name)


def _value(v: Any) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def metric_items(lead: pd.DataFrame, total: pd.DataFrame, prefix: str = "") -> dict[str, float]:
    """Kennzahlen eines Verfahrens als ``<präfix><ziel>.d<vorlauf>.<maß>`` bzw. ``.all.``.

    @relation(SWR-MLT-047, scope=function)
    """
    out: dict[str, float] = {}
    for frame, part in ((lead, "lead"), (total, "all")):
        for row in frame.to_dict("records"):
            where = f"d{int(row['lead_day'])}" if part == "lead" else "all"
            for m in (*MEASURES, "n"):
                v = _value(row.get(m))
                if v is not None:
                    out[f"{prefix}{row['target']}.{where}.{m}"] = v
    return out


def _view_metrics(out: Path) -> Iterator[tuple[str, str, dict[str, float]]]:
    """(Sicht, Verfahren, Kennzahlen) aus den CSV-Dateien eines Backtest-Ordners."""
    for prefix, stem in VIEWS.items():
        lead_p, all_p = out / f"{stem}_lead.csv", out / f"{stem}.csv"
        if not (lead_p.exists() and all_p.exists()):
            continue
        lead, total = pd.read_csv(lead_p), pd.read_csv(all_p)
        for method in sorted(set(lead["method"]) | set(total["method"])):
            items = metric_items(
                lead[lead["method"] == method], total[total["method"] == method], prefix
            )
            yield prefix, str(method), items


def _log_metrics(c: MlflowClient, run_id: str, items: dict[str, float]) -> None:
    from mlflow.entities import Metric

    ms = [Metric(k, v, 0, 0) for k, v in sorted(items.items())]
    for i in range(0, len(ms), BATCH):
        c.log_batch(run_id, metrics=ms[i : i + BATCH])


def _tags(d: dict[str, Any]) -> dict[str, str]:
    return {k: str(v)[:5000] for k, v in d.items() if v not in (None, "", [])}


def log_backtest(
    cfg: ModelConfig, config_path: Path, out: Path, args: dict[str, Any]
) -> str | None:
    """Elternlauf mit einem Unterlauf je Verfahren; gibt die Lauf-ID zurück.

    @relation(SWR-MLT-047, scope=function)
    """
    if not enabled():
        return None
    try:
        return _log_backtest(cfg, config_path, out, args)
    except Exception as e:  # MLflow darf den Backtest nie scheitern lassen
        log.warning("mlflow.failed", what="backtest", error=str(e))
        return None


def _log_backtest(cfg: ModelConfig, config_path: Path, out: Path, args: dict[str, Any]) -> str:
    c = _client()
    run = json.loads((out / "lauf.json").read_text())
    folds = run.get("folds") or [{}]
    variants = args.get("variant") or [v.name for v in cfg.variants]
    compare, rolling = run.get("compare") or {}, run.get("compare_rolling") or {}
    tags = _tags(
        {
            "kind": "backtest",
            "config": cfg.name,
            "snapshot_id": run.get("snapshot_id"),
            "config_sha256": run.get("config_sha256"),
            "git_commit": run.get("git_commit"),
            "test_from": folds[0].get("test_from"),
            "test_to": folds[-1].get("test_to"),
            "compare_window": compare.get("name"),
            "compare_rolling": rolling.get("name"),
            "backtest_uri": str(out),
        }
    )
    exp = _experiment(c, cfg.name)
    name = f"bt {datetime.now(UTC):%Y-%m-%d} {','.join(variants)}"[:250]
    parent: str = c.create_run(exp, run_name=name, tags=tags).info.run_id
    params = {
        "fold_months": cfg.backtest.fold_months,
        "train_issue_stride": cfg.backtest.train_issue_stride,
        "gap_days": cfg.backtest.gap_days,
        "folds": len(run.get("folds") or []),
        "variants": ",".join(variants),
        **{f"arg.{k}": v for k, v in args.items() if k not in ("snapshot", "out", "variant")},
    }
    from mlflow.entities import Param

    c.log_batch(parent, params=[Param(k, str(v)[:6000]) for k, v in params.items()])
    for pattern in ARTIFACTS:
        for p in sorted(out.glob(pattern)):
            c.log_artifact(parent, str(p))
    if config_path.exists():
        c.log_artifact(parent, str(config_path))
    complete = {
        "w.": set(compare.get("complete") or []),
        "r.": set(rolling.get("complete") or []),
    }
    children: dict[str, str] = {}
    for prefix, method, items in _view_metrics(out):
        if method not in children:
            child_tags = {
                "mlflow.parentRunId": parent,
                "kind": "backtest_method",
                "method": method,
                "baseline": str(method not in variants).lower(),
            }
            children[method] = c.create_run(exp, run_name=method, tags=child_tags).info.run_id
        _log_metrics(c, children[method], items)
        if prefix in complete:
            key = "compare_complete" if prefix == "w." else "rolling_complete"
            c.set_tag(children[method], key, str(method in complete[prefix]).lower())
    for rid in children.values():
        c.set_terminated(rid)
    c.set_terminated(parent)
    log.info("mlflow.backtest", run_id=parent, methods=len(children))
    return parent


def log_fit(cfg: ModelConfig, version: str, manifest: dict[str, Any], registry: str) -> str | None:
    """Ein Lauf je registrierter Version; die Modellversion entsteht beim Registry-Spiegel.

    @relation(SWR-MLT-047, scope=function)
    """
    if not enabled():
        return None
    try:
        c = _client()
        days = manifest.get("train_issue_days") or [None, None]
        tags = _tags(
            {
                "kind": "fit",
                "config": cfg.name,
                "pdb_version": version,
                "snapshot_id": manifest.get("snapshot_id"),
                "config_sha256": manifest.get("config_sha256"),
                "git_commit": manifest.get("git_commit"),
                "model_uri": f"{registry.rstrip('/')}/{cfg.name}/{version}",
            }
        )
        rid = c.create_run(_experiment(c, cfg.name), run_name=f"fit {version}", tags=tags)
        run_id: str = rid.info.run_id
        from mlflow.entities import Metric, Param

        params = {"train_from": days[0], "train_to": days[1]}
        c.log_batch(
            run_id,
            metrics=[
                Metric(k, float(manifest[k]), 0, 0)
                for k in ("train_rows", "train_seconds")
                if _value(manifest.get(k)) is not None
            ],
            params=[Param(k, str(v)) for k, v in params.items() if v],
        )
        c.set_terminated(run_id)
        log.info("mlflow.fit", run_id=run_id, version=version)
        return run_id
    except Exception as e:
        log.warning("mlflow.failed", what="fit", error=str(e))
        return None


def mirror_registry(reg: Registry) -> int:
    """Rollen der Datei-Registry nach MLflow spiegeln; gibt die Zahl der Versionen zurück.

    Fehlende Versionen werden angelegt (Quelle: Registry-Ordner, ohne Kopie), Tag ``role`` und
    Aliase folgen ``registry.json``. Von Hand in MLflow versetzte Aliase überschreibt der nächste
    Abgleich.

    @relation(SWR-MLT-047, scope=function)
    """
    if not enabled():
        return 0
    try:
        return _mirror(reg)
    except Exception as e:
        log.warning("mlflow.failed", what="registry", error=str(e))
        return 0


def _mirror(reg: Registry) -> int:
    from mlflow.exceptions import MlflowException

    from ml_train.dbsync import roles

    c = _client()
    name = reg.cfg.name
    try:
        c.get_registered_model(name)
    except MlflowException:
        c.create_registered_model(
            name,
            description="Spiegel der Datei-Registry (ml-train registry sync). Rollen und Aliase "
            "werden beim Abgleich überschrieben; freigeben mit ml-train registry promote.",
        )
    have = {mv.tags.get("pdb_version"): mv for mv in c.search_model_versions(f"name = '{name}'")}
    by_role = roles(reg.state())
    for version, role in by_role.items():
        mv = have.get(version)
        if mv is None:
            try:
                man = reg.manifest(version)
            except KeyError:
                continue
            mv = c.create_model_version(
                name,
                source=f"{reg.store.root}/{name}/{version}",
                tags=_tags(
                    {
                        "pdb_version": version,
                        "variant": (man.get("variant") or {}).get("name"),
                        "snapshot_id": man.get("snapshot_id"),
                        "role": role,
                    }
                ),
                description=version,
            )
            have[version] = mv
        elif mv.tags.get("role") != role:
            c.set_model_version_tag(name, mv.version, "role", role)
    st = reg.state()
    # Challenger gibt es mehrere: nur Tag; eindeutige Rollen bekommen ein Alias
    targets = {
        "champion": st.get("champion"),
        "baseline": st.get("baseline"),
        "previous": (st.get("previous") or [None])[0],
    }
    for alias, version in targets.items():
        if version and version in have:
            c.set_registered_model_alias(name, alias, have[version].version)
        else:
            with contextlib.suppress(MlflowException):
                c.delete_registered_model_alias(name, alias)
    log.info("mlflow.registry", model=name, versions=len(by_role))
    return len(by_role)
