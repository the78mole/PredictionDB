"""Kommandozeile ``wx-ingest``.

Beispiele::

    wx-ingest gfs latest                         # unvollständige Läufe der letzten Stunden
    wx-ingest gfs latest --watch 600             # dauerhaft, alle 10 min
    wx-ingest gfs run 2026102012 --steps 0-12    # ein Lauf, optional nur Teilbereich
    wx-ingest gfs backfill 2026-10-01T00 2026-10-07T18   # nur Archiv-Endpunkte
    wx-ingest gfs backfill 2021-03-22T12 --runs 0,12 --newest-first --parallel-runs 2
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

from predictiondb_core.db import create_db_engine
from predictiondb_core.log import configure_logging
from predictiondb_core.s3 import s3_store
from predictiondb_core.settings import get_settings
from wx_ingest.bronze import Bronze
from wx_ingest.config import GfsConfig
from wx_ingest.ingest import GfsIngestor, RunResult, StepResult, result_dict
from wx_ingest.jobs import JobLog

DEFAULT_CONFIG = Path(os.environ.get("WX_GFS_CONFIG", "config/sources/gfs_0p25.yaml"))


def parse_run(text: str) -> datetime:
    for fmt in ("%Y%m%d%H", "%Y-%m-%dT%H", "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    raise argparse.ArgumentTypeError(f"Zeitpunkt {text!r} nicht lesbar (z. B. 2026102012)")


def parse_steps(text: str) -> list[int]:
    steps: set[int] = set()
    for part in text.split(","):
        if "-" in part:
            a, b = part.split("-", 1)
            steps.update(range(int(a), int(b) + 1))
        else:
            steps.add(int(part))
    return sorted(steps)


def parse_hours(text: str) -> list[int]:
    try:
        hours = sorted({int(h) for h in text.split(",")})
    except ValueError:
        raise argparse.ArgumentTypeError(f"Laufzeiten {text!r} nicht lesbar (z. B. 0,12)") from None
    if any(h not in (0, 6, 12, 18) for h in hours):
        raise argparse.ArgumentTypeError("GFS-Läufe nur 0, 6, 12, 18 UTC")
    return hours


def add_backfill_args(p: argparse.ArgumentParser) -> None:
    """Zeitraum und Laufauswahl, gemeinsam für ``wx-ingest`` und ``wx-process``."""
    p.add_argument("start", type=parse_run)
    p.add_argument("end", type=parse_run, nargs="?", help="Standard: jetzt")
    p.add_argument("--runs", type=parse_hours, metavar="HH,…", help="nur diese Läufe, z. B. 0,12")
    p.add_argument("--newest-first", action="store_true", help="jüngste Läufe zuerst")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="wx-ingest", description="Wettermodell-Ingest nach Garage (Bronze)"
    )
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="model", required=True)
    gfs = sub.add_parser("gfs", help="NOAA GFS 0,25°")
    gfs.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    cmd = gfs.add_subparsers(dest="cmd", required=True)

    latest = cmd.add_parser("latest", help="unvollständige Läufe im Rückblickfenster laden")
    latest.add_argument(
        "--watch", type=int, metavar="SEK", help="wiederholen im Abstand von SEK Sekunden"
    )

    run = cmd.add_parser("run", help="einen Lauf laden")
    run.add_argument("run", type=parse_run)
    run.add_argument(
        "--steps",
        type=parse_steps,
        help="z. B. 0-12,24,48 (nur gültige Schritte der Konfiguration)",
    )

    bf = cmd.add_parser("backfill", help="Zeitraum aus Archiv-Endpunkten nachladen")
    add_backfill_args(bf)
    bf.add_argument(
        "--parallel-runs", type=int, default=1, metavar="N", help="Läufe gleichzeitig (Standard 1)"
    )
    bf.add_argument(
        "--archive-concurrency",
        type=int,
        metavar="N",
        help="gleichzeitige Anfragen je Archiv-Endpunkt (Standard aus der Konfiguration)",
    )
    bf.add_argument("--no-job-log", action="store_true", help="ohne Protokoll in meta.backfill_job")
    return p


async def _main(args: argparse.Namespace) -> int:
    config = GfsConfig.from_yaml(args.config)
    if getattr(args, "archive_concurrency", None):
        # Die Grenze gilt für alle parallelen Läufe zusammen; mehr Läufe brauchen mehr Anfragen
        for ep in config.endpoints:
            if ep.archive:
                ep.concurrency = args.archive_concurrency
    settings = get_settings()
    bronze = Bronze(s3_store(settings.buckets.bronze, settings), config.model, config.resolution)
    timeout = httpx.Timeout(60.0, connect=15.0)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        ingestor = GfsIngestor(config, bronze, client, archive_only=args.cmd == "backfill")
        if args.cmd == "run":
            steps = args.steps
            if steps is not None:
                valid = set(config.step_list())
                unknown = [s for s in steps if s not in valid]
                if unknown:
                    print(f"Schritte nicht in der Konfiguration: {unknown}", file=sys.stderr)
                steps = [s for s in steps if s in valid]
            results = [await ingestor.ingest_run(args.run, steps, stop_on_pending=False)]
        elif args.cmd == "backfill":
            results = await _backfill(args, config, ingestor)
        else:
            while True:
                results = await ingestor.ingest_recent()
                if not args.watch:
                    break
                print(json.dumps([result_dict(r) for r in results]), flush=True)
                await asyncio.sleep(args.watch)
    print(json.dumps([result_dict(r) for r in results], indent=1))
    return 1 if any(s.status == "failed" for r in results for s in r.steps) else 0


async def _backfill(
    args: argparse.Namespace, config: GfsConfig, ingestor: GfsIngestor
) -> list[RunResult]:
    """Backfill mit Protokoll in ``meta.backfill_job`` (für das Status-Dashboard)."""
    # Ohne Ende nur bis vor das Fenster des Live-Modus: jüngere Läufe erscheinen noch und
    # gehören ``wx-ingest gfs latest``; der Backfill würde sie sonst als unvollständig abhaken.
    end = args.end or datetime.now(UTC) - timedelta(hours=config.lookback_hours)
    hours = args.runs or config.runs
    runs = ingestor.backfill_runs(args.start, end, hours=hours, newest_first=args.newest_first)
    jobs = JobLog(None if args.no_job_log else create_db_engine())
    await asyncio.to_thread(
        jobs.start, config.code, args.start, end, hours, args.newest_first, runs
    )

    async def started(run: datetime, n_steps: int) -> None:
        await asyncio.to_thread(jobs.run_started, run, n_steps)

    async def step_done(run: datetime, step: StepResult) -> None:
        await asyncio.to_thread(jobs.step_done, run, step)

    async def progress(r: RunResult) -> None:
        await asyncio.to_thread(jobs.progress, r)

    try:
        results = await ingestor.backfill(
            args.start,
            end,
            hours=hours,
            newest_first=args.newest_first,
            parallel_runs=args.parallel_runs,
            on_run=progress,
            on_start=started,
            on_step=step_done,
        )
    except (KeyboardInterrupt, asyncio.CancelledError):
        jobs.finish("aborted", "abgebrochen")
        raise
    except Exception as exc:
        jobs.finish("failed", f"{type(exc).__name__}: {exc}")
        raise
    failed = any(s.status == "failed" for r in results for s in r.steps)
    await asyncio.to_thread(jobs.finish, "failed" if failed else "done")
    return results


def _terminate(signum: int, frame: object) -> None:
    # docker stop schickt SIGTERM: als Abbruch behandeln, damit das Protokoll abschließt
    raise KeyboardInterrupt


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = get_settings()
    configure_logging("DEBUG" if args.verbose else settings.log_level, settings.log_json)
    signal.signal(signal.SIGTERM, _terminate)
    return asyncio.run(_main(args))


if __name__ == "__main__":
    raise SystemExit(main())
