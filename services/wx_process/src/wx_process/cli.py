"""Kommandozeile ``wx-process``.

Beispiele::

    wx-process gfs latest                          # fällige Läufe der letzten 48 h
    wx-process gfs latest --watch 600              # Dienst, alle 10 min
    wx-process gfs run 2026102012 --force          # einen Lauf neu rechnen (Silber und Gold)
    wx-process gfs backfill 2026-10-01T00 2026-10-07T18 --clustering v2026Q4
    wx-process gfs backfill 2021-03-22T12 --runs 0,12 --newest-first --decompress --watch 3600
    wx-process gfs run 2026102012 --silver-only    # nur Silber, ohne Datenbank
    wx-process gfs prune-bronze --keep-days 92     # Bronze älter als 92 Tage löschen (mit Silber)
    wx-process gfs latest --watch 600 --bronze-days 92   # dazu laufend aufräumen
    wx-process gfs evict-silver --keep-days 92 --dry-run # GHCR-Silber mit Gold lokal löschen
"""

from __future__ import annotations

import argparse
import json
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from predictiondb_core.db import create_db_engine
from predictiondb_core.log import configure_logging, get_logger
from predictiondb_core.s3 import s3_store
from predictiondb_core.settings import get_settings
from wx_ingest.cli import add_backfill_args, parse_run
from wx_ingest.config import GfsConfig
from wx_process.config import ProcessConfig, WxProcessSettings
from wx_process.pipeline import RunOutcome, WxPipeline
from wx_process.silver import Silver
from wx_process.store import Store

log = get_logger(__name__)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="wx-process", description="Bronze → Silber (Zarr) → Features je Anlagen-Cluster"
    )
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--config", type=Path, help="Standard: PDB_WX_PROCESS_CONFIG")
    sub = p.add_subparsers(dest="model", required=True)
    gfs = sub.add_parser("gfs", help="NOAA GFS 0,25°")
    cmd = gfs.add_subparsers(dest="cmd", required=True)

    latest = cmd.add_parser("latest", help="fällige Läufe im Rückblickfenster rechnen")
    latest.add_argument(
        "--watch", type=int, metavar="SEK", help="wiederholen im Abstand von SEK Sekunden"
    )
    latest.add_argument(
        "--bronze-days",
        type=float,
        metavar="TAGE",
        help="je Durchgang Bronze älter als TAGE löschen, sofern Silber vorliegt (letzte Woche)",
    )

    evict = cmd.add_parser(
        "evict-silver", help="aus GHCR importiertes Silber mit Gold lokal löschen (bleibt auf GHCR)"
    )
    evict.add_argument("--keep-days", type=float, default=92, metavar="TAGE")
    evict.add_argument("--dry-run", action="store_true", help="nur zählen")
    evict.add_argument(
        "--pause", type=float, default=0.2, metavar="SEK", help="Pause je Lauf (Garage schonen)"
    )

    prune = cmd.add_parser("prune-bronze", help="altes Bronze löschen, sofern Silber vorliegt")
    prune.add_argument("--keep-days", type=float, default=92, metavar="TAGE")
    prune.add_argument("--since", type=parse_run, help="ab diesem Lauf suchen (Standard: 2021)")
    prune.add_argument("--dry-run", action="store_true", help="nur zählen")
    prune.add_argument(
        "--pause", type=float, default=0.2, metavar="SEK", help="Pause je Lauf (Garage schonen)"
    )

    for name, help_ in (("run", "einen Lauf rechnen"), ("backfill", "Zeitraum rechnen")):
        c = cmd.add_parser(name, help=help_)
        if name == "run":
            c.add_argument("run", type=parse_run)
        else:
            add_backfill_args(c, ordered=True)
            c.add_argument(
                "--watch",
                type=int,
                metavar="SEK",
                help="wiederholen im Abstand von SEK Sekunden (während wx-ingest nachlädt)",
            )
            c.add_argument(
                "--decompress",
                action="store_true",
                help="komprimierte Ziel-Chunks vor dem Schreiben entpacken, danach wieder packen",
            )
        c.add_argument("--force", action="store_true", help="Silber und Features neu rechnen")
        c.add_argument("--clustering", help="Clusterversion (Standard: jüngste)")
        c.add_argument("--silver-only", action="store_true", help="ohne Datenbank, nur Silber")
    return p


def _print(results: list[RunOutcome], indent: int | None = 1) -> None:
    print(json.dumps([r.as_dict() for r in results], indent=indent), flush=True)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = get_settings()
    configure_logging("DEBUG" if args.verbose else settings.log_level, settings.log_json)

    config = ProcessConfig.from_yaml(args.config or WxProcessSettings().config)
    source = GfsConfig.from_yaml(config.source_config)
    silver = Silver(
        s3_store(settings.buckets.silver, settings),
        step_chunk=config.silver.step_chunk,
        zstd_level=config.silver.zstd_level,
    )
    store = None if getattr(args, "silver_only", False) else Store(create_db_engine(settings))
    pipeline = WxPipeline(
        config, source, s3_store(settings.buckets.bronze, settings), silver, store
    )

    if args.cmd == "run":
        results = [pipeline.process(args.run, force=args.force, clustering=args.clustering)]
    elif args.cmd == "backfill":
        while True:
            results = pipeline.backfill(
                args.start,
                args.end or datetime.now(UTC),
                force=args.force,
                clustering=args.clustering,
                hours=args.runs,
                newest_first=args.newest_first,
                decompress=args.decompress,
            )
            if not args.watch:
                break
            _print([r for r in results if r.status not in ("skipped", "not_ready")], None)
            time.sleep(args.watch)
    elif args.cmd == "evict-silver":
        counts = pipeline.evict_silver(args.keep_days, dry_run=args.dry_run, pause=args.pause)
        print(json.dumps(counts), flush=True)
        return 0
    elif args.cmd == "prune-bronze":
        counts = pipeline.prune_bronze(
            args.keep_days, start=args.since, dry_run=args.dry_run, pause=args.pause
        )
        print(json.dumps(counts), flush=True)
        return 0
    else:
        while True:
            results = pipeline.latest()
            if args.bronze_days:
                # nur die Tage, die seit dem letzten Durchgang über die Grenze gerutscht sind
                since = datetime.now(UTC) - timedelta(days=args.bronze_days + 7)
                try:
                    pipeline.prune_bronze(args.bronze_days, start=since, pause=0.2)
                except Exception:  # Aufräumen darf den Dienst nicht beenden
                    log.exception("bronze.prune_failed")
            if not args.watch:
                break
            _print([r for r in results if r.status not in ("skipped", "not_ready")], None)
            time.sleep(args.watch)
    _print(results)
    return 1 if any(r.status == "failed" for r in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
