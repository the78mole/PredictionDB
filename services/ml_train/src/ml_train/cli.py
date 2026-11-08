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
import shutil
import signal
import subprocess
import sys
import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd
from sqlalchemy import Engine

from ml_train import tracking
from ml_train.backtest import (
    BacktestResult,
    Checkpoint,
    Paused,
    backtest_steps,
    data_days,
    run_backtest,
)
from ml_train.config import CompareWindow, ModelConfig, RollingWindow, load_config
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
#: Unterordner des Backtest-Ausgabeordners mit den Zwischenständen je Faltung und Verfahren
CHECKPOINT_DIR = "teile"
#: Rückgabewert, wenn der Backtest an seiner Frist anhält (EX_TEMPFAIL: später fortsetzen)
EXIT_PAUSED = 75
#: Zeitzone für ``--stop-at HH:MM``
LOCAL_TZ = ZoneInfo(os.environ.get("PDB_LOCAL_TZ", "Europe/Berlin"))


def parse_date(s: str) -> date:
    return date.fromisoformat(s)


def parse_deadline(s: str, now: datetime | None = None) -> datetime:
    """Frist als ``HH:MM`` (nächstes Auftreten in ``LOCAL_TZ``) oder ISO-Zeit mit Zeitzone."""
    if len(s) <= 5 and ":" in s:
        hh, mm = (int(x) for x in s.split(":"))
        ref = (now or datetime.now(UTC)).astimezone(LOCAL_TZ)
        at = ref.replace(hour=hh, minute=mm, second=0, microsecond=0)
        return at if at > ref else at + timedelta(days=1)
    at = datetime.fromisoformat(s)
    if at.tzinfo is None:
        raise argparse.ArgumentTypeError(
            "Frist braucht eine Zeitzone, z. B. 2026-11-10T08:00+01:00"
        )
    return at


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
    b.add_argument(
        "--active",
        action="store_true",
        help="nur die Varianten der aktiven Registry-Versionen (Champion, Challenger, Baseline)",
    )
    b.add_argument("--registry", default=DEFAULT_REGISTRY)
    b.add_argument("--test-from", type=parse_date)
    b.add_argument("--test-to", type=parse_date)
    b.add_argument("--fold-months", type=int)
    b.add_argument("--stride", type=int, help="nur jeden n-ten Ausgabetag trainieren")
    b.add_argument("--issue-hour", type=int, help="Ausgabestunde (UTC) statt issue.hour_utc")
    b.add_argument(
        "--rolling",
        action="store_true",
        help="Testzeitraum = mitlaufendes Fenster aus compare_rolling (z. B. 24 Monate)",
    )
    b.add_argument("--rolling-months", type=int, help="Länge des mitlaufenden Fensters in Monaten")
    b.add_argument(
        "--rolling-until",
        choices=["month", "season"],
        help="Ende: letzter voller Monat oder letzte volle Jahreszeit",
    )
    b.add_argument(
        "--stop-at",
        type=parse_deadline,
        help="keinen Teil mehr beginnen, der nicht bis dahin fertig wird (HH:MM oder ISO-Zeit); "
        f"Rückgabewert {EXIT_PAUSED}, der nächste Aufruf setzt fort",
    )

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

    im = cmd.add_parser("importance", help="Wichtigkeit der Merkmale einer Version (Gain)")
    im.add_argument("version")
    im.add_argument("--registry", default=DEFAULT_REGISTRY)
    im.add_argument("--by", choices=["group", "feature"], default="group")
    im.add_argument("--top", type=int, default=15, help="Zeilen je Ziel bei --by feature")
    im.add_argument("--out", type=Path, help="alle Merkmale zusätzlich als CSV")
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
        neighbour=cfg.neighbour_weather,
        sites=cfg.site_weather,
        progress=job.step,
    )
    job.step("Speichern")
    sid = snap.save(a.out)
    job.result = sid
    print(json.dumps({"snapshot_id": sid, "out": str(a.out), **snap.manifest["rows"]}))
    return 0


def _window_files(
    res: BacktestResult, w: CompareWindow | None, out: Path, metrics: str, season: str
) -> dict[str, Any] | None:
    """Kennzahlen und Jahreszeitenvergleich in einem Vergleichsfenster; Angaben für lauf.json."""
    if w is None:
        return None
    res.metrics_window(w, ("method", "target")).to_csv(out / f"metrics_{metrics}.csv", index=False)
    res.metrics_window(w, ("method", "target", "lead_day")).to_csv(
        out / f"metrics_{metrics}_lead.csv", index=False
    )
    res.season_comparison(w).to_csv(out / f"vergleich_jahreszeit_{season}.csv", index=False)
    done = res.window_methods(w)
    return {
        "name": w.name,
        "start": w.start,
        "end": w.end,
        "complete": sorted(m for m, ok in done.items() if ok),
        "incomplete": sorted(m for m, ok in done.items() if not ok),
    }


def write_backtest(
    res: BacktestResult,
    cfg: ModelConfig,
    out: Path,
    extra: dict[str, Any],
    today: date | None = None,
) -> None:
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
    res.season_comparison().to_csv(out / "vergleich_jahreszeit.csv", index=False)
    w_today = today or datetime.now(UTC).date()
    compare = _window_files(res, cfg.compare, out, "window", "fenster")
    rolling = None
    if cfg.compare_rolling is not None:
        rolling = _window_files(
            res, cfg.compare_rolling.resolve(w_today), out, "rolling", "rolling"
        )
    run = {
        **extra,
        "config": cfg.name,
        "compare": compare,
        "compare_rolling": rolling,
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
    """Backtest, wahlweise im mitlaufenden Fenster und mit Frist.

    @relation(SWR-MLT-040, SWR-MLT-042, SWR-MLT-043, SWR-MLT-044, SWR-MLT-045, scope=function)
    @relation(SWR-MLT-046, scope=function)
    """
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
    today = datetime.now(UTC).date()
    if a.rolling_months or a.rolling_until:
        base = cfg.compare_rolling or RollingWindow()
        cfg.compare_rolling = RollingWindow(
            months=a.rolling_months or base.months, until=a.rolling_until or base.until
        )
    if a.rolling or a.rolling_months or a.rolling_until:
        if cfg.compare_rolling is None:
            raise SystemExit("--rolling braucht compare_rolling in der Konfiguration")
        w = cfg.compare_rolling.resolve(today)
        # Testzeitraum = mitlaufendes Fenster; Faltungen beginnen an seinem ersten Tag
        cfg.backtest.test_from, cfg.backtest.test_to = w.start, w.end
        log.info("backtest.rolling", window=w.name, start=w.start, end=w.end)
    job.step("Snapshot laden")
    snap = load_snapshot(a.snapshot)
    fb = FeatureBuilder(snap, cfg)
    # Schritte: Snapshot laden, Backtest, Ergebnisse schreiben
    job.total(2 + backtest_steps(fb, cfg, a.variant))
    # Zwischenstände je Faltung und Verfahren: ein abgebrochener Lauf setzt hier wieder an
    ck = Checkpoint(a.out / CHECKPOINT_DIR, snap.snapshot_id, cfg)
    try:
        res = run_backtest(fb, cfg, a.variant, job, ck, deadline=a.stop_at)
    except Paused as e:
        job.paused = str(e)
        log.info("backtest.pause", out=str(a.out), parts=len(list(ck.root.glob("*.parquet"))))
        print(f"Backtest {e}; fortsetzen mit demselben Aufruf")
        return EXIT_PAUSED
    job.step("Ergebnisse schreiben")
    extra = {"snapshot_id": snap.snapshot_id, "config_sha256": cfg_sha, "reused_parts": ck.reused}
    write_backtest(res, cfg, a.out, extra, today)
    shutil.rmtree(ck.root, ignore_errors=True)
    run_id = tracking.log_backtest(cfg, Path(a.config), a.out, _job_args(a))
    if run_id:
        print(f"MLflow-Lauf {run_id}")
    job.result = str(a.out)
    m = res.metrics(("method", "target"))
    print(m.to_string(index=False))
    windows = cfg.compare_windows(today)
    for w in windows:
        print(f"\nVergleichsfenster {w.name} ({w.start} bis {w.end}):")
        print(res.metrics_window(w, ("method", "target")).to_string(index=False))
    cols = ["target", "season", "rank", "method", "n", "mae", "vs_best"]
    shown: list[CompareWindow | None] = [*windows] or [None]
    for sw in shown:
        sc = res.season_comparison(sw)
        if sc.empty:
            continue  # kein Verfahren deckt das Fenster ab
        label = sw.name if sw else "alle Testtage"
        print(f"\nVerfahren je Jahreszeit, {label} (gemeinsame Zeilen, Rang nach MAE):")
        print(sc[cols].to_string(index=False))
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
    tracking.log_fit(cfg, version, manifest, a.registry)
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
    tracking.mirror_registry(reg)  # fängt seine Fehler selbst ab


def cmd_registry(cfg: ModelConfig, a: argparse.Namespace) -> int:
    """@relation(SWR-MLT-051, SWR-MLT-052, SWR-MLT-053, scope=function)"""
    reg = Registry(a.registry, cfg)
    if a.action == "sync":
        from ml_train.dbsync import sync_registry
        from predictiondb_core.db import create_db_engine

        n = sync_registry(create_db_engine(), reg)
        print(json.dumps({"versions": n, "mlflow": tracking.mirror_registry(reg)}))
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


def _job_args(a: argparse.Namespace) -> dict[str, Any]:
    return {k: v for k, v in vars(a).items() if k not in ("cmd", "verbose") and v is not None}


def active_variants(cfg: ModelConfig, registry: str) -> list[str]:
    """Varianten der aktiven Versionen in der Reihenfolge der Konfiguration.

    @relation(SWR-MLT-046, scope=function)
    """
    reg = Registry(registry, cfg)
    used = {reg.manifest(v)["variant"]["name"] for v in reg.active()}
    return [v.name for v in cfg.variants if v.name in used]


def run_job(cfg: ModelConfig, sha: str, a: argparse.Namespace) -> int:
    """Snapshot, Backtest oder Training mit Fortschritt in ``meta.ml_job``.

    @relation(SWR-MLT-003, scope=function)
    """
    # nur der Eintrag des laufenden Befehls: snapshot hat kein --variant
    variants: list[str] = []
    if a.cmd == "backtest":
        if a.active:
            a.variant = [*(a.variant or []), *active_variants(cfg, a.registry)]
            if not a.variant:
                raise SystemExit(f"Keine aktive Version in {a.registry} für {cfg.name}")
            a.variant = list(dict.fromkeys(a.variant))
        variants = a.variant or [v.name for v in cfg.variants]
    elif a.cmd == "fit":
        variants = [a.variant]
    args = _job_args(a)
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
    if rc == EXIT_PAUSED and job.paused:
        job.finish(error=job.paused, status="aborted")
    else:
        job.finish(error=None if rc == 0 else f"Rückgabewert {rc}")
    return rc


def cmd_importance(cfg: ModelConfig, a: argparse.Namespace) -> int:
    """@relation(SWR-MLT-072, scope=function)"""
    from ml_train.importance import by_group, feature_importance

    model = Registry(a.registry, cfg).load(a.version, Path(DEFAULT_CACHE))
    table = feature_importance(model)
    if a.out:
        a.out.parent.mkdir(parents=True, exist_ok=True)
        table.to_csv(a.out, index=False)
    show = by_group(table) if a.by == "group" else table.groupby("target").head(a.top)
    print(show.round(2).to_string(index=False))
    return 0


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
    if a.cmd == "importance":
        return cmd_importance(cfg, a)
    return 2


if __name__ == "__main__":
    sys.exit(main())
