"""Kommandozeile ``ml-train``.

Beispiele::

    ml-train snapshot --out /data/snap                         # Snapshot aus der Datenbank
    ml-train backtest --snapshot /data/snap --out /data/bt     # alle Varianten, Expanding Window
    ml-train backtest --snapshot /data/snap --out /data/bt-wx --variant wetter \\
        --test-from 2026-09-15 --fold-months 1                 # kurzer Wetter-Backtest
    ml-train fit --snapshot /data/snap --variant kalender --backtest /data/bt --role champion
    ml-train registry list
    ml-train registry promote de_lu_v1-wetter-202611010800-ab12cd
    ml-train registry sync                                     # Spiegel für Grafana
    ml-train predict                                           # aktive Versionen, jetzt
    ml-train predict --watch 3600 --catch-up 24                # stündlich, Lücken nachholen
    ml-train bundle de_lu_v2-frei-202610241200-ab12cd --out /data/pakete/de_lu_v2-frei

@relation(SWA-MLT-CLI, scope=file)
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
from sqlalchemy import Engine

from ml_train.backtest import BacktestResult, backtest_steps, data_days, run_backtest
from ml_train.config import ModelConfig, load_config
from ml_train.dataset import FeatureBuilder, issue_days_between
from ml_train.jobs import JobLog, open_job_log
from ml_train.models import TwoStageModel
from ml_train.registry import Registry
from ml_train.snapshot import export_snapshot, export_steps, load_snapshot
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
    """@relation(SWR-MLT-001, scope=function)"""
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
    b.add_argument("--stride", type=int, help="nur jeden n-ten Ausgabetag trainieren")
    b.add_argument("--issue-hour", type=int, help="Ausgabestunde (UTC) statt issue.hour_utc")

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
    rc.add_parser("sync", help="Versionen, Rollen und Kennzahlen nach meta.model_* spiegeln")
    at = rc.add_parser(
        "attach-backtest", help="Backtest-Prognosen nachträglich zu einer Version legen"
    )
    at.add_argument("version")
    at.add_argument("--backtest", required=True, type=Path, help="Ordner von ml-train backtest")
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
    pr.add_argument(
        "--watch", type=int, default=0, help="Sekunden zwischen Läufen, auf dieses Raster"
    )
    pr.add_argument(
        "--catch-up",
        type=int,
        default=0,
        metavar="STUNDEN",
        help="verpasste volle Stunden nachholen, höchstens so weit zurück (auch nach Neustart)",
    )
    pr.add_argument(
        "--also-config",
        action="append",
        default=[],
        help="weitere Konfiguration (eigene Registry-Linie), deren aktive Versionen mitlaufen",
    )
    pr.add_argument(
        "--fill-new",
        type=int,
        default=0,
        metavar="STUNDEN",
        help="neue aktive Versionen rechnen die Ausgabezeitpunkte der Vergleichsgrafiken der "
        "letzten STUNDEN nach (Vergleich im Dashboard ab sofort)",
    )

    bu = cmd.add_parser("bundle", help="Prognosepaket einer Version für den Stack predict/")
    bu.add_argument("version")
    bu.add_argument("--out", required=True, type=Path)
    bu.add_argument("--registry", default=DEFAULT_REGISTRY)
    bu.add_argument("--snapshot", type=Path, help="Bestand aus dem Snapshot statt der Datenbank")
    bu.add_argument("--wx-config", type=Path, default=Path("config/process/gfs_0p25.yaml"))
    return p


# --- Befehle ---------------------------------------------------------------------------------


def cmd_snapshot(cfg: ModelConfig, a: argparse.Namespace, job: JobLog) -> int:
    """@relation(SWR-MLT-010, scope=function)"""
    from predictiondb_core.db import create_db_engine

    end = a.end or (datetime.now(UTC).date() + timedelta(days=2))
    codes = list(dict.fromkeys(cfg.targets + cfg.lag_series + cfg.series_codes))
    codes += sorted({b for t in cfg.stage1 + cfg.stage2 for b in t.benchmarks})
    codes += [c for c in a.extra_codes.split(",") if c]
    codes = list(dict.fromkeys(codes))
    job.total(export_steps(codes, to_dt(a.start), to_dt(end), to_dt(a.weather_from)) + 1)
    snap = export_snapshot(
        create_db_engine(),
        codes,
        to_dt(a.start),
        to_dt(end),
        weather_from=to_dt(a.weather_from),
        run_hours=[int(h) for h in a.run_hours.split(",")],
        max_lead_h=24 * (max(cfg.issue.lead_days) + 2),
        progress=job.step,
    )
    job.step("Speichern")
    sid = snap.save(a.out)
    job.result = sid
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


def cmd_backtest(cfg: ModelConfig, cfg_sha: str, a: argparse.Namespace, job: JobLog) -> int:
    """@relation(SWR-MLT-040, scope=function)"""
    if a.test_from:
        cfg.backtest.test_from = a.test_from
    if a.test_to:
        cfg.backtest.test_to = a.test_to
    if a.fold_months:
        cfg.backtest.fold_months = a.fold_months
    if a.stride:
        cfg.backtest.train_issue_stride = a.stride
    if a.issue_hour is not None:
        cfg.issue.hour_utc = a.issue_hour
        cfg.issue.hours_utc = []
    job.step("Snapshot laden")
    snap = load_snapshot(a.snapshot)
    fb = FeatureBuilder(snap, cfg)
    # Schritte: Snapshot laden, Backtest, Ergebnisse schreiben
    job.total(2 + backtest_steps(fb, cfg, a.variant))
    res = run_backtest(fb, cfg, a.variant, job)
    job.step("Ergebnisse schreiben")
    write_backtest(res, cfg, a.out, {"snapshot_id": snap.snapshot_id, "config_sha256": cfg_sha})
    job.result = str(a.out)
    m = res.metrics(("method", "target"))
    print(m.to_string(index=False))
    return 0


def cmd_fit(cfg: ModelConfig, cfg_sha: str, a: argparse.Namespace, job: JobLog) -> int:
    """@relation(SWR-MLT-050, scope=function)"""
    v = cfg.variant(a.variant)
    model = TwoStageModel(cfg, v)
    # Schritte: Snapshot laden, Merkmale, Training je Stufe und Ziel, Registrieren
    job.total(3 + model.fit_steps())
    job.step("Snapshot laden")
    snap = load_snapshot(a.snapshot)
    fb = FeatureBuilder(snap, cfg)
    first, last = data_days(fb, cfg)
    start = max(v.train_from or first, first)
    train_to = last - timedelta(days=cfg.backtest.gap_days)
    days = issue_days_between(start, train_to, cfg.backtest.train_issue_stride)
    job.step("Merkmale bilden")
    frame = fb.build(days, need_weather=v.requires_weather)
    t0 = time.monotonic()
    model.fit(frame, progress=job.step)
    bt, extra = backtest_for(a.backtest, v.name) if a.backtest else (None, {})
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
    manifest.update(extra)
    job.step("Registrieren")
    reg = Registry(a.registry, cfg)
    version = reg.register(model, manifest, bt, a.role)
    job.result = version
    try_sync(reg)
    print(json.dumps({"version": version, "role": a.role, "train_rows": len(frame)}))
    return 0


def backtest_for(folder: Path, variant: str) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Backtest-Prognosen einer Variante und Angaben fürs Manifest aus ``ml-train backtest``."""
    p = pd.read_parquet(folder / "predictions.parquet")
    bt = p[p["method"] == variant].drop(columns=["method"])
    extra: dict[str, Any] = {}
    if (folder / "lauf.json").exists():
        timings = json.loads((folder / "lauf.json").read_text()).get("timings_s", {})
        extra["backtest_seconds"] = round(sum(timings.values()), 1)
    if (folder / "metrics.csv").exists():
        m = pd.read_csv(folder / "metrics.csv")
        extra["backtest_metrics"] = m[m["method"] == variant].to_dict("records")
    return bt, extra


def try_sync(reg: Registry, engine: Engine | None = None) -> None:
    """Spiegel aktualisieren, wo eine Datenbank erreichbar ist (Training läuft auch ohne).

    @relation(SWR-MLT-053, scope=function)
    """
    from ml_train.dbsync import sync_registry
    from predictiondb_core.db import create_db_engine

    try:
        sync_registry(engine or create_db_engine(), reg)
    except Exception as e:  # der Spiegel darf Training und Inferenz nie aufhalten
        log.warning("sync.failed", error=str(e))


def cmd_registry(cfg: ModelConfig, a: argparse.Namespace) -> int:
    """@relation(SWR-MLT-051, SWR-MLT-052, SWR-MLT-053, scope=function)"""
    reg = Registry(a.registry, cfg)
    if a.action == "sync":
        from ml_train.dbsync import sync_registry
        from predictiondb_core.db import create_db_engine

        print(json.dumps({"versions": sync_registry(create_db_engine(), reg)}))
        return 0
    if a.action == "attach-backtest":
        variant = reg.manifest(a.version)["variant"]["name"]
        bt, extra = backtest_for(a.backtest, variant)
        if bt.empty:
            raise SystemExit(f"Backtest {a.backtest} enthält die Variante {variant} nicht")
        reg.attach_backtest(a.version, bt, extra)
        try_sync(reg)
        print(json.dumps({"version": a.version, "backtest_rows": len(bt)}))
        return 0
    if a.action == "list":
        st = reg.state()
        st["active"] = reg.active()
        print(json.dumps(st, indent=2))
        return 0
    d = reg.promote(a.version, a.force) if a.action == "promote" else reg.evaluate(a.version)
    if a.action == "promote":
        try_sync(reg)
    if not d.overall.empty:
        print(d.overall.to_string(index=False))
    for r in d.reasons:
        print(f"- {r}")
    print("Champion: ja" if d.promote else "Champion: nein")
    return 0 if d.promote else 1


def cmd_predict(cfg: ModelConfig, a: argparse.Namespace) -> int:
    """@relation(SWR-MLT-060, SWR-MLT-061, scope=function)"""
    from ml_train.predict import (
        due_issues,
        last_issue,
        live_snapshot,
        needs_weather,
        next_wake,
        predict_versions,
        write_predictions,
    )
    from predictiondb_core.db import create_db_engine

    engine = create_db_engine()
    # Jede Konfiguration ist eine eigene Registry-Linie mit eigenem Champion/Challenger
    lines = [(cfg, Registry(a.registry, cfg))]
    lines += [(c, Registry(a.registry, c)) for c, _ in map(load_config, a.also_config)]
    # Zuletzt gerechneter Ausgabezeitpunkt je Linie; beim Start aus der Datenbank
    last: dict[str, pd.Timestamp | None] = {}
    # Versionen, deren Nachrechnen (--fill-new) erledigt ist; beim Start sind alle neu
    filled: set[str] = set()
    while True:
        now = pd.Timestamp.now(tz="UTC")
        found = False
        active: dict[str, list[str]] = {}
        for line_cfg, reg in lines:
            try_sync(reg, engine)
            # --version gilt nur für die Hauptkonfiguration
            active[line_cfg.name] = (a.version if line_cfg is cfg else None) or reg.active()
        for line_cfg, reg in lines:
            versions = active[line_cfg.name]
            if not versions:
                log.warning("predict.no_versions", registry=a.registry, config=line_cfg.name)
                continue
            found = True
            if a.issue:
                issues = [pd.Timestamp(a.issue, tz="UTC")]
            else:
                if line_cfg.name not in last:
                    last[line_cfg.name] = (
                        last_issue(engine, versions) if a.catch_up and not a.dry_run else None
                    )
                issues = due_issues(now, last[line_cfg.name], a.catch_up)
            wx = needs_weather(line_cfg, reg, versions)
            for issue in issues:
                snap = live_snapshot(engine, line_cfg, issue, weather=wx)
                df = predict_versions(snap, line_cfg, reg, versions, issue, Path(DEFAULT_CACHE))
                if a.dry_run:
                    print(df.to_string(index=False, max_rows=60))
                else:
                    n = write_predictions(engine, df)
                    log.info("predict.done", issue=str(issue), versions=versions, rows=n)
                last[line_cfg.name] = issue
        if a.fill_new and not a.issue and not a.dry_run:
            # Nach dem regulären Lauf, damit die aktuelle Prognose nie wartet
            for line_cfg, reg in lines:
                fill_new(engine, line_cfg, reg, active[line_cfg.name], filled, now, a.fill_new)
        if not found:
            log.error("predict.no_versions", registry=a.registry)
            return 1
        if not a.watch:
            return 0
        # Auf dem Raster wecken (volle Stunde plus eine Minute), damit keine Stunde ausfällt
        wake = next_wake(pd.Timestamp.now(tz="UTC"), a.watch)
        time.sleep(max(0.0, (wake - pd.Timestamp.now(tz="UTC")).total_seconds()))


def fill_new(
    engine: Engine,
    cfg: ModelConfig,
    reg: Registry,
    versions: list[str],
    filled: set[str],
    now: pd.Timestamp,
    hours: int,
) -> None:
    """Neue Versionen einer Linie rechnen die Ausgabezeitpunkte der Vergleichsgrafiken nach
    (08 UTC und 10 Uhr Ortszeit), die ihnen im Fenster fehlen.

    @relation(SWR-MLT-062, scope=function)
    """
    from ml_train.predict import (
        by_issue,
        live_snapshot,
        missing_compare_issues,
        needs_weather,
        predict_versions,
        write_predictions,
    )

    new = [v for v in versions if v not in filled]
    if not new:
        return
    cur = now.floor("h")
    # Die laufende Stunde rechnet der reguläre Lauf
    since, until = cur - pd.Timedelta(hours=hours), cur - pd.Timedelta(hours=1)
    plan = by_issue({v: missing_compare_issues(engine, v, since, until) for v in new})
    if plan:
        log.info("predict.fill_new", config=cfg.name, versions=new, issues=len(plan))
    for issue, vs in plan.items():
        snap = live_snapshot(engine, cfg, issue, weather=needs_weather(cfg, reg, vs))
        df = predict_versions(snap, cfg, reg, vs, issue, Path(DEFAULT_CACHE))
        n = write_predictions(engine, df)
        log.info("predict.filled", issue=str(issue), versions=vs, rows=n)
    filled.update(new)


def cmd_bundle(cfg: ModelConfig, a: argparse.Namespace) -> int:
    """@relation(SWR-MLT-070, scope=function)"""
    from ml_train.bundle import export_package

    reg = Registry(a.registry, cfg)
    snap = load_snapshot(a.snapshot) if a.snapshot else None
    pkg = export_package(
        reg,
        a.version,
        a.out,
        config_path=Path(a.config),
        snapshot=snap,
        wx_process_config=a.wx_config,
        cache=Path(DEFAULT_CACHE),
    )
    print(json.dumps({"out": str(a.out), **{k: pkg[k] for k in ("version", "variant", "codes")}}))
    return 0


def _terminate(signum: int, _: object) -> None:
    """@relation(SWR-MLT-003, scope=function)"""
    raise SystemExit(128 + signum)


def run_job(cfg: ModelConfig, sha: str, a: argparse.Namespace) -> int:
    """Snapshot, Backtest oder Training mit Fortschritt in ``meta.ml_job``.

    @relation(SWR-MLT-003, scope=function)
    """
    # nur der Eintrag des laufenden Befehls: snapshot hat kein --variant
    variants: list[str] = []
    if a.cmd == "backtest":
        variants = a.variant or [v.name for v in cfg.variants]
    elif a.cmd == "fit":
        variants = [a.variant]
    args = {k: v for k, v in vars(a).items() if k not in ("cmd", "verbose") and v is not None}
    job = open_job_log()
    job.start(a.cmd, cfg.name, variants, args)
    # docker stop: als Abbruch eintragen statt erst nach der Frist für hängende Jobs
    signal.signal(signal.SIGTERM, _terminate)
    try:
        if a.cmd == "snapshot":
            rc = cmd_snapshot(cfg, a, job)
        elif a.cmd == "backtest":
            rc = cmd_backtest(cfg, sha, a, job)
        else:
            rc = cmd_fit(cfg, sha, a, job)
    except BaseException as e:
        if isinstance(e, SystemExit) and e.code == 128 + signal.SIGTERM:
            msg = "beendet (SIGTERM)"
        else:
            msg = f"{type(e).__name__}: {e}".strip().rstrip(":")
        job.finish(error=msg[:1000])
        raise
    job.finish(error=None if rc == 0 else f"Rückgabewert {rc}")
    return rc


def main(argv: list[str] | None = None) -> int:
    """@relation(SWR-MLT-001, scope=function)"""
    a = build_parser().parse_args(argv)
    configure_logging()
    cfg, sha = load_config(a.config)
    if a.cmd in ("snapshot", "backtest", "fit"):
        return run_job(cfg, sha, a)
    if a.cmd == "registry":
        return cmd_registry(cfg, a)
    if a.cmd == "predict":
        return cmd_predict(cfg, a)
    if a.cmd == "bundle":
        return cmd_bundle(cfg, a)
    return 2


if __name__ == "__main__":
    sys.exit(main())
