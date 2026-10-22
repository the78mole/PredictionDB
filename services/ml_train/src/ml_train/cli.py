"""Kommandozeile ``ml-train``.

Beispiele::

    ml-train snapshot --out /data/snap                         # Snapshot aus der Datenbank
    ml-train backtest --snapshot /data/snap --out /data/bt     # alle Varianten, Expanding Window
    ml-train backtest --snapshot /data/snap --out /data/bt-wx --variant wetter \\
        --test-from 2026-09-15 --fold-months 1                 # kurzer Wetter-Backtest
    ml-train fit --snapshot /data/snap --variant kalender --backtest /data/bt --role champion
    ml-train registry list
    ml-train registry promote de_lu_v1-wetter-202611010800-ab12cd
    ml-train predict                                           # aktive Versionen, jetzt
    ml-train predict --watch 3600
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

from ml_train.backtest import BacktestResult, data_days, run_backtest
from ml_train.config import ModelConfig, load_config
from ml_train.dataset import FeatureBuilder, issue_days_between
from ml_train.models import TwoStageModel
from ml_train.registry import Registry
from ml_train.snapshot import export_snapshot, load_snapshot
from predictiondb_core.log import configure_logging, get_logger

log = get_logger(__name__)

DEFAULT_CONFIG = os.environ.get("PDB_ML_CONFIG", "config/ml/de_lu_v1.yaml")
DEFAULT_REGISTRY = os.environ.get("PDB_ML_REGISTRY", "s3://ml-artifacts/models")
DEFAULT_CACHE = os.environ.get("PDB_ML_CACHE", "/tmp/ml-train-cache")


def parse_date(s: str) -> date:
    return date.fromisoformat(s)


def to_dt(d: date) -> datetime:
    return datetime(d.year, d.month, d.day, tzinfo=UTC)


def git_commit() -> str | None:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short=12", "HEAD"], capture_output=True, text=True, check=True
        )
        return out.stdout.strip() or None
    except (OSError, subprocess.CalledProcessError):
        return os.environ.get("PDB_GIT_COMMIT")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="ml-train", description="Last- und Preisprognose DE-LU")
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--config", default=DEFAULT_CONFIG, help="Modellkonfiguration (YAML)")
    cmd = p.add_subparsers(dest="cmd", required=True)

    s = cmd.add_parser("snapshot", help="Trainings-Snapshot aus der Datenbank exportieren")
    s.add_argument("--out", required=True, type=Path)
    s.add_argument("--from", dest="start", type=parse_date, default=date(2015, 1, 1))
    s.add_argument("--to", dest="end", type=parse_date, default=None)
    s.add_argument("--weather-from", type=parse_date, default=date(2021, 3, 22))
    s.add_argument("--run-hours", default="0,12")
    s.add_argument("--extra-codes", default="", help="weitere Reihen, kommagetrennt")

    b = cmd.add_parser("backtest", help="Backtest aller oder einzelner Varianten")
    b.add_argument("--snapshot", required=True, type=Path)
    b.add_argument("--out", required=True, type=Path)
    b.add_argument("--variant", action="append")
    b.add_argument("--test-from", type=parse_date)
    b.add_argument("--test-to", type=parse_date)
    b.add_argument("--fold-months", type=int)

    f = cmd.add_parser("fit", help="Variante mit allen Daten trainieren und registrieren")
    f.add_argument("--snapshot", required=True, type=Path)
    f.add_argument("--variant", required=True)
    f.add_argument("--backtest", type=Path, help="Backtest-Ordner (Prognosen für den Vergleich)")
    f.add_argument("--role", choices=["challenger", "champion", "baseline"], default="challenger")
    f.add_argument("--registry", default=DEFAULT_REGISTRY)

    r = cmd.add_parser("registry", help="Registry anzeigen, Champion/Challenger")
    r.add_argument("--registry", default=DEFAULT_REGISTRY)
    rc = r.add_subparsers(dest="action", required=True)
    rc.add_parser("list")
    for name in ("evaluate", "promote"):
        x = rc.add_parser(name)
        x.add_argument("version")
        if name == "promote":
            x.add_argument("--force", action="store_true")

    pr = cmd.add_parser("predict", help="aktive Versionen inferieren, nach fcst.prediction")
    pr.add_argument("--registry", default=DEFAULT_REGISTRY)
    pr.add_argument("--issue", help="Ausgabezeitpunkt UTC, z. B. 2026-10-22T08 (Vorgabe: jetzt)")
    pr.add_argument("--version", action="append", help="statt der aktiven Versionen")
    pr.add_argument("--dry-run", action="store_true", help="nur ausgeben, nicht schreiben")
    pr.add_argument("--watch", type=int, default=0, help="Sekunden zwischen Läufen")
    return p


# --- Befehle ---------------------------------------------------------------------------------


def cmd_snapshot(cfg: ModelConfig, a: argparse.Namespace) -> int:
    from predictiondb_core.db import create_db_engine

    end = a.end or (datetime.now(UTC).date() + timedelta(days=2))
    codes = list(dict.fromkeys(cfg.targets + cfg.lag_series))
    codes += sorted({b for t in cfg.stage1 + cfg.stage2 for b in t.benchmarks})
    codes += [c for c in a.extra_codes.split(",") if c]
    snap = export_snapshot(
        create_db_engine(),
        list(dict.fromkeys(codes)),
        to_dt(a.start),
        to_dt(end),
        weather_from=to_dt(a.weather_from),
        run_hours=[int(h) for h in a.run_hours.split(",")],
        max_lead_h=24 * (max(cfg.issue.lead_days) + 2),
    )
    sid = snap.save(a.out)
    print(json.dumps({"snapshot_id": sid, "out": str(a.out), **snap.manifest["rows"]}))
    return 0


def write_backtest(res: BacktestResult, cfg: ModelConfig, out: Path, extra: dict[str, Any]) -> None:
    out.mkdir(parents=True, exist_ok=True)
    res.predictions.to_parquet(out / "predictions.parquet", index=False)
    res.metrics(("method", "target", "lead_day")).to_csv(out / "metrics_lead.csv", index=False)
    res.metrics(("method", "target")).to_csv(out / "metrics.csv", index=False)
    res.metrics(("method", "target", "lead_day", "season")).to_csv(
        out / "metrics_season.csv", index=False
    )
    res.metrics(("method", "target", "year")).to_csv(out / "metrics_year.csv", index=False)
    res.rank_corr(("method", "target", "lead_day")).to_csv(out / "rank_corr.csv", index=False)
    methods = sorted(res.predictions["method"].unique())
    res.metrics_common(methods, ("method", "target", "lead_day")).to_csv(
        out / "metrics_common_lead.csv", index=False
    )
    run = {
        **extra,
        "config": cfg.name,
        "folds": [
            {"no": f.no, "test_from": f.test_from, "test_to": f.test_to, "train_to": f.train_to}
            for f in res.folds
        ],
        "timings_s": {k: round(v, 1) for k, v in res.timings.items()},
        "rows": len(res.predictions),
        "git_commit": git_commit(),
        "finished": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    (out / "lauf.json").write_text(json.dumps(run, indent=2, default=str))


def cmd_backtest(cfg: ModelConfig, cfg_sha: str, a: argparse.Namespace) -> int:
    if a.test_from:
        cfg.backtest.test_from = a.test_from
    if a.test_to:
        cfg.backtest.test_to = a.test_to
    if a.fold_months:
        cfg.backtest.fold_months = a.fold_months
    snap = load_snapshot(a.snapshot)
    fb = FeatureBuilder(snap, cfg)
    res = run_backtest(fb, cfg, a.variant)
    write_backtest(res, cfg, a.out, {"snapshot_id": snap.snapshot_id, "config_sha256": cfg_sha})
    m = res.metrics(("method", "target"))
    print(m.to_string(index=False))
    return 0


def cmd_fit(cfg: ModelConfig, cfg_sha: str, a: argparse.Namespace) -> int:
    snap = load_snapshot(a.snapshot)
    fb = FeatureBuilder(snap, cfg)
    first, last = data_days(fb, cfg)
    v = cfg.variant(a.variant)
    start = max(v.train_from or first, first)
    train_to = last - timedelta(days=cfg.backtest.gap_days)
    days = issue_days_between(start, train_to, cfg.backtest.train_issue_stride)
    frame = fb.build(days, need_weather=v.requires_weather)
    t0 = time.monotonic()
    model = TwoStageModel(cfg, v).fit(frame)
    bt = None
    if a.backtest:
        p = pd.read_parquet(a.backtest / "predictions.parquet")
        bt = p[p["method"] == v.name].drop(columns=["method"])
    manifest = {
        "config": cfg.name,
        "config_sha256": cfg_sha,
        "snapshot_id": snap.snapshot_id,
        "clustering_version": snap.manifest.get("clustering_version"),
        "train_issue_days": [str(start), str(train_to)],
        "train_rows": len(frame),
        "train_seconds": round(time.monotonic() - t0, 1),
        "git_commit": git_commit(),
        "created": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    if a.backtest and (a.backtest / "metrics.csv").exists():
        m = pd.read_csv(a.backtest / "metrics.csv")
        manifest["backtest_metrics"] = m[m["method"] == v.name].to_dict("records")
    version = Registry(a.registry, cfg).register(model, manifest, bt, a.role)
    print(json.dumps({"version": version, "role": a.role, "train_rows": len(frame)}))
    return 0


def cmd_registry(cfg: ModelConfig, a: argparse.Namespace) -> int:
    reg = Registry(a.registry, cfg)
    if a.action == "list":
        st = reg.state()
        st["active"] = reg.active()
        print(json.dumps(st, indent=2))
        return 0
    d = reg.promote(a.version, a.force) if a.action == "promote" else reg.evaluate(a.version)
    if not d.overall.empty:
        print(d.overall.to_string(index=False))
    for r in d.reasons:
        print(f"- {r}")
    print("Champion: ja" if d.promote else "Champion: nein")
    return 0 if d.promote else 1


def cmd_predict(cfg: ModelConfig, a: argparse.Namespace) -> int:
    from ml_train.predict import live_snapshot, predict_versions, write_predictions
    from predictiondb_core.db import create_db_engine

    engine = create_db_engine()
    reg = Registry(a.registry, cfg)
    while True:
        issue = (
            pd.Timestamp(a.issue, tz="UTC") if a.issue else pd.Timestamp.now(tz="UTC").floor("h")
        )
        versions = a.version or reg.active()
        if not versions:
            log.error("predict.no_versions", registry=a.registry)
            return 1
        snap = live_snapshot(engine, cfg, issue)
        df = predict_versions(snap, cfg, reg, versions, issue, Path(DEFAULT_CACHE))
        if a.dry_run:
            print(df.to_string(index=False, max_rows=60))
        else:
            n = write_predictions(engine, df)
            log.info("predict.done", issue=str(issue), versions=versions, rows=n)
        if not a.watch:
            return 0
        time.sleep(a.watch)


def main(argv: list[str] | None = None) -> int:
    a = build_parser().parse_args(argv)
    configure_logging()
    cfg, sha = load_config(a.config)
    if a.cmd == "snapshot":
        return cmd_snapshot(cfg, a)
    if a.cmd == "backtest":
        return cmd_backtest(cfg, sha, a)
    if a.cmd == "fit":
        return cmd_fit(cfg, sha, a)
    if a.cmd == "registry":
        return cmd_registry(cfg, a)
    if a.cmd == "predict":
        return cmd_predict(cfg, a)
    return 2


if __name__ == "__main__":
    sys.exit(main())
