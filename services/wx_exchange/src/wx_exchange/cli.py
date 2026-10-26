"""Kommandozeile ``wx-exchange``.

Beispiele::

    # GitHub Actions: Plan der Matrix, dann je Job ein Monat einer Laufzeit
    wx-exchange gfs plan 2021-03-22T12 --runs 0,12,6,18 --block-years 3
    wx-exchange gfs publish 2026-09 0 --parallel-runs 2

    # VM: Silber aus GHCR nach wx-silver holen, stündlich wiederholen
    wx-exchange gfs import 2021-03-22T12 --runs 0,12,6,18 --watch 3600

    # VM: selbst aus Bronze gebautes Silber mit dem aus GHCR vergleichen
    wx-exchange gfs verify 2026-01-01T00 --runs 0,12

Registry und Repository kommen aus ``WX_EXCHANGE_REGISTRY`` (``ghcr.io``) und
``WX_EXCHANGE_REPOSITORY``, die Anmeldung zum Schreiben aus ``WX_EXCHANGE_USERNAME`` und
``WX_EXCHANGE_TOKEN`` (in Actions ``github.actor`` und ``GITHUB_TOKEN``). Lesen geht anonym.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

from predictiondb_core.db import create_db_engine
from predictiondb_core.log import configure_logging
from predictiondb_core.s3 import s3_store
from predictiondb_core.settings import get_settings
from wx_exchange.importer import Importer
from wx_exchange.oci import Registry
from wx_exchange.publish import Publisher, plan
from wx_ingest.cli import DEFAULT_CONFIG, parse_run
from wx_ingest.config import GfsConfig
from wx_process.config import ProcessConfig, WxProcessSettings
from wx_process.store import Store

DEFAULT_REPOSITORY = "the78mole/predictiondb-data/gfs-0p25-silver"
SOURCE_URL = "https://github.com/the78mole/predictiondb"


def parse_hours_ordered(text: str) -> list[int]:
    """Wie ``wx_ingest.cli.parse_hours``, aber in der angegebenen Reihenfolge (Priorität)."""
    try:
        hours = list(dict.fromkeys(int(h) for h in text.split(",")))
    except ValueError:
        raise argparse.ArgumentTypeError(f"Laufzeiten {text!r} nicht lesbar (z. B. 0,12)") from None
    if any(h not in (0, 6, 12, 18) for h in hours):
        raise argparse.ArgumentTypeError("GFS-Läufe nur 0, 6, 12, 18 UTC")
    return hours


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="wx-exchange", description="Silber-Austausch über GHCR")
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--registry", default=os.environ.get("WX_EXCHANGE_REGISTRY", "ghcr.io"))
    p.add_argument(
        "--repository", default=os.environ.get("WX_EXCHANGE_REPOSITORY", DEFAULT_REPOSITORY)
    )
    sub = p.add_subparsers(dest="model", required=True)
    gfs = sub.add_parser("gfs", help="NOAA GFS 0,25°")
    gfs.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    gfs.add_argument("--process-config", type=Path, help="Standard: PDB_WX_PROCESS_CONFIG")
    cmd = gfs.add_subparsers(dest="cmd", required=True)

    pl = cmd.add_parser("plan", help="Matrix der fehlenden Monate je Block (JSON)")
    _range_args(pl)
    pl.add_argument("--block-years", type=int, default=3)
    pl.add_argument("--blocks", type=int, default=3, help="Anzahl Block-Jobs im Workflow")
    pl.add_argument("--all", action="store_true", help="alle Monate, auch vollständige (Neubau)")

    pub = cmd.add_parser("publish", help="einen Monat einer Laufzeit nach GHCR")
    pub.add_argument("month", help="JJJJ-MM")
    pub.add_argument("hour", type=int, choices=(0, 6, 12, 18))
    pub.add_argument("--start", type=parse_run, help="frühester Lauf (z. B. 2021-03-22T12)")
    pub.add_argument("--end", type=parse_run, help="spätester Lauf")
    pub.add_argument("--rebuild", action="store_true", help="neu bauen, nur Änderungen hochladen")
    pub.add_argument("--parallel-runs", type=int, default=2, metavar="N")
    pub.add_argument("--archive-concurrency", type=int, default=16, metavar="N")
    pub.add_argument("--workdir", type=Path, help="Standard: temporäres Verzeichnis")

    imp = cmd.add_parser("import", help="Silber aus GHCR nach wx-silver")
    _range_args(imp)
    imp.add_argument("--force", action="store_true", help="auch vorhandenes Silber ersetzen")
    imp.add_argument("--watch", type=int, metavar="SEK", help="wiederholen im Abstand von SEK")

    ver = cmd.add_parser("verify", help="lokales Silber mit GHCR vergleichen")
    _range_args(ver)
    ver.add_argument(
        "--details", type=int, default=5, metavar="N", help="für N Abweichungen Datei für Datei"
    )
    return p


def _range_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("start", type=parse_run)
    p.add_argument("end", type=parse_run, nargs="?", help="Standard: vor dem Live-Fenster")
    p.add_argument(
        "--runs",
        type=parse_hours_ordered,
        default=[0, 12, 6, 18],
        metavar="HH,…",
        help="Laufzeiten in Reihenfolge der Priorität (Standard 0,12,6,18)",
    )


def _registry(args: argparse.Namespace, client: httpx.Client) -> Registry:
    return Registry(
        args.registry,
        args.repository,
        client,
        username=os.environ.get("WX_EXCHANGE_USERNAME"),
        password=os.environ.get("WX_EXCHANGE_TOKEN") or None,
    )


def _end(args: argparse.Namespace, config: GfsConfig) -> datetime:
    end: datetime = getattr(args, "end", None) or datetime.now(UTC) - timedelta(
        hours=config.lookback_hours
    )
    return end


def _github_output(name: str, value: str) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{name}={value}\n")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = get_settings()
    configure_logging("DEBUG" if args.verbose else settings.log_level, settings.log_json)
    config = GfsConfig.from_yaml(args.config)
    timeout = httpx.Timeout(120.0, connect=15.0)

    with httpx.Client(timeout=timeout) as client:
        registry = _registry(args, client)
        if args.cmd == "plan":
            blocks = plan(
                registry,
                config,
                args.start,
                _end(args, config),
                args.runs,
                block_years=args.block_years,
                blocks=args.blocks,
                rebuild=args.all,
            )
            for i, entries in enumerate(blocks, start=1):
                _github_output(f"block{i}", json.dumps(entries, separators=(",", ":")))
            print(json.dumps({f"block{i}": e for i, e in enumerate(blocks, start=1)}, indent=1))
            return 0
        if args.cmd == "publish":
            return asyncio.run(_publish(args, config, registry))

        importer_ = _importer(registry, config, ledger=args.cmd == "import")
        if args.cmd == "verify":
            verified = importer_.verify(
                args.start, _end(args, config), args.runs, details=args.details
            )
            out = verified.as_dict()
            print(json.dumps(out, indent=1))
            return 1 if out["different"] else 0
        while True:
            result = importer_.run(args.start, _end(args, config), args.runs, force=args.force)
            print(json.dumps(result.as_dict()), flush=True)
            if not args.watch:
                return 1 if result.as_dict()["failed"] else 0
            time.sleep(args.watch)


def _importer(registry: Registry, config: GfsConfig, *, ledger: bool = False) -> Importer:
    """``ledger``: lokalen Bestand nach ``meta.silver_run`` melden (Status-Dashboard)."""
    settings = get_settings()
    return Importer(
        registry,
        s3_store(settings.buckets.silver),
        config.model,
        config.resolution,
        ledger=Store(create_db_engine(settings)) if ledger else None,
        source=config.code,
    )


async def _publish(args: argparse.Namespace, config: GfsConfig, registry: Registry) -> int:
    for ep in config.endpoints:
        if ep.archive:
            ep.concurrency = args.archive_concurrency
    process = ProcessConfig.from_yaml(args.process_config or WxProcessSettings().config)
    with tempfile.TemporaryDirectory(prefix="wx-exchange-") as tmp:
        workdir = args.workdir or Path(tmp)
        timeout = httpx.Timeout(60.0, connect=15.0)
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            publisher = Publisher(
                registry,
                config,
                process,
                client,
                workdir,
                source_url=SOURCE_URL,
                parallel_runs=args.parallel_runs,
            )
            # Nie ins Live-Fenster: noch nicht veröffentlichte Läufe gälten sonst als fehlend
            result = await publisher.publish_month(
                args.month, args.hour, args.start, _end(args, config), rebuild=args.rebuild
            )
    out = result.as_dict()
    print(json.dumps(out, indent=1))
    if out["failed"]:
        print(f"{len(out['failed'])} Läufe fehlgeschlagen", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
