"""Kommandozeile ``mastr-ingest``.

Beispiele::

    mastr-ingest snapshot                       # heutigen Export laden, importieren, clustern
    mastr-ingest snapshot --if-due --watch 21600   # Dienst: einmal je Quartal, Prüfung alle 6 h
    mastr-ingest import Gesamtdatenexport_20261001_26.1.zip   # manuell geladene Datei
    mastr-ingest cluster --new                  # neue Clusterversion auf dem jüngsten Snapshot
    mastr-ingest gemeinden vg250_schwerpunkte.csv   # Referenzpunkte je Gemeinde (ags,name,lat,lon)

@relation(SWA-MAI-CLI, scope=file)
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from dataclasses import asdict
from datetime import UTC, date, datetime
from pathlib import Path

import httpx
from sqlalchemy import text

from mastr_ingest.config import MastrConfig, MastrSettings
from mastr_ingest.download import Bronze, Downloader, ExportUnavailable
from mastr_ingest.pipeline import MastrPipeline, SnapshotResult
from mastr_ingest.store import Store
from predictiondb_core.db import create_db_engine
from predictiondb_core.log import configure_logging, get_logger
from predictiondb_core.s3 import s3_store
from predictiondb_core.settings import get_settings

log = get_logger(__name__)


def build_parser() -> argparse.ArgumentParser:
    """@relation(SWR-MAI-001, scope=function)"""
    p = argparse.ArgumentParser(
        prog="mastr-ingest",
        description="Marktstammdatenregister: Snapshot, Bestandsrekonstruktion, Gebietscluster",
    )
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--config", type=Path, help="Standard: PDB_MASTR_CONFIG")
    cmd = p.add_subparsers(dest="cmd", required=True)

    snap = cmd.add_parser("snapshot", help="Gesamtdatenexport laden und verarbeiten")
    snap.add_argument("--date", type=date.fromisoformat, help="Exportdatum (Standard: heute, UTC)")
    snap.add_argument(
        "--if-due",
        action="store_true",
        help="nur, wenn im laufenden Quartal noch kein Snapshot liegt",
    )
    snap.add_argument(
        "--watch",
        type=int,
        metavar="SEK",
        help="wiederholen im Abstand von SEK Sekunden (mit --if-due)",
    )
    snap.add_argument("--new-clustering", action="store_true", help="neue Clusterversion bilden")
    snap.add_argument("--keep-local", action="store_true", help="ZIP im Arbeitsverzeichnis lassen")

    imp = cmd.add_parser("import", help="lokal vorliegenden Export importieren")
    imp.add_argument("zip", type=Path)
    imp.add_argument("--new-clustering", action="store_true")

    cl = cmd.add_parser("cluster", help="Cluster, Gewichte und Bestand neu rechnen")
    cl.add_argument("--new", action="store_true", help="neue Clusterversion bilden")

    gem = cmd.add_parser("gemeinden", help="Referenzpunkte je Gemeinde laden (CSV)")
    gem.add_argument("csv", type=Path, help="Spalten ags, name, lat, lon (Trenner , oder ;)")
    gem.add_argument("--source", default="csv")
    return p


def _print(result: SnapshotResult) -> None:
    print(json.dumps(asdict(result), default=str, indent=1), flush=True)


def _snapshot(args: argparse.Namespace, pipeline: MastrPipeline) -> int:
    while True:
        day = args.date or datetime.now(UTC).date()
        if args.if_due and not pipeline.is_due(day):
            log.info("mastr.snapshot.not_due", day=day.isoformat())
        else:
            try:
                _print(
                    pipeline.run(
                        day, new_clustering=args.new_clustering, keep_local=args.keep_local
                    )
                )
            except ExportUnavailable as e:
                log.warning("mastr.snapshot.unavailable", day=day.isoformat(), error=str(e))
                if not args.watch:
                    return 2
        if not args.watch:
            return 0
        time.sleep(args.watch)


def _gemeinden(path: Path, source: str, store: Store) -> int:
    """@relation(SWR-MAI-024, scope=function)"""
    with path.open(newline="", encoding="utf-8-sig") as f:
        sample = f.read(4096)
        f.seek(0)
        rows = [
            {
                "ags": r["ags"].strip().zfill(8),
                "name": r.get("name"),
                "lat": float(r["lat"].replace(",", ".")),
                "lon": float(r["lon"].replace(",", ".")),
                "src": source,
            }
            for r in csv.DictReader(f, dialect=csv.Sniffer().sniff(sample, delimiters=",;"))
        ]
    with store.engine.begin() as c:
        c.execute(
            text("""
            INSERT INTO assets.gemeinde VALUES (:ags, :name, :lat, :lon, :src)
            ON CONFLICT (ags) DO UPDATE SET name = EXCLUDED.name, lat = EXCLUDED.lat,
              lon = EXCLUDED.lon, source = EXCLUDED.source"""),
            rows,
        )
    print(json.dumps({"gemeinden": len(rows)}))
    return 0


def main(argv: list[str] | None = None) -> int:
    """@relation(SWR-MAI-001, SWR-MAI-003, scope=function)"""
    args = build_parser().parse_args(argv)
    settings = get_settings()
    configure_logging("DEBUG" if args.verbose else settings.log_level, settings.log_json)
    mastr = MastrSettings()
    config = MastrConfig.from_yaml(args.config or mastr.config)
    store = Store(create_db_engine(settings))
    if args.cmd == "gemeinden":
        return _gemeinden(args.csv, args.source, store)

    bronze = Bronze(s3_store(settings.buckets.assets_bronze, settings))
    timeout = httpx.Timeout(120.0, connect=20.0)
    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        pipeline = MastrPipeline(
            config,
            store,
            bronze,
            mastr.work_dir,
            Downloader(client, config.page_url, config.download_url),
        )
        if args.cmd == "snapshot":
            return _snapshot(args, pipeline)
        if args.cmd == "import":
            _print(
                pipeline.run(
                    datetime.now(UTC).date(),
                    local_zip=args.zip,
                    new_clustering=args.new_clustering,
                )
            )
            return 0
        latest = store.latest_imported()
        if latest is None or latest["status"] != "imported":
            print("kein importierter Snapshot mit Einheiten vorhanden", file=sys.stderr)
            return 1
        version = pipeline.update_clusters(
            latest["snapshot_id"], latest["export_date"], new=args.new
        )
        print(
            json.dumps(
                {"clustering_version": version, "export_date": latest["export_date"].isoformat()}
            )
        )
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
