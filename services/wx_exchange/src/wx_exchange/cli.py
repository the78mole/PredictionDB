"""Kommandozeile ``wx-exchange``.

Beispiele::

    # GitHub Actions: Plan der Matrix, dann je Job ein Monat einer Laufzeit
    wx-exchange gfs plan 2021-03-22T12 --runs 0,12,6,18 --block-years 3
    wx-exchange gfs publish 2026-09 0 --parallel-runs 2

    # Archiv ohne .idx (GDEX): Kopf-Inventare an je einem Lauf pro Monat prüfen und
    # ergänzen; das Ergebnis ersetzt die Datei unter "templates" (Workflow gfs-templates.yml)
    wx-exchange gfs --config config/sources/gfs_0p25_gdex.yaml templates 2019-06-12T12 \
        2021-03-22T06 --out gfs_0p25_gdex.templates.json.gz

    # Vollständige .idx zu GDEX (Workflow gfs-idx.yml): Plan, je Job ein Layer, Manifest
    wx-exchange gfs --config config/sources/gfs_0p25_gdex.yaml idx-plan 2015-01-15T00 \
        2021-03-22T06 --runs 0,12
    wx-exchange gfs --config config/sources/gfs_0p25_gdex.yaml idx-build 2019-07 0 \
        --out layer.json
    wx-exchange gfs --config config/sources/gfs_0p25_gdex.yaml idx-manifest layers/

    # VM: Silber aus GHCR nach wx-silver holen, stündlich wiederholen
    wx-exchange gfs import 2021-03-22T12 --runs 0,12,6,18 --watch 3600

    # VM: selbst aus Bronze gebautes Silber mit dem aus GHCR vergleichen
    wx-exchange gfs verify 2026-01-01T00 --runs 0,12

    # GEFS-Ensemble (Workflow gefs-history.yml): Plan, je Job ein Monat, nur Silber nach GHCR
    wx-exchange gefs plan 2020-09-24T00
    wx-exchange gefs publish 2026-10 0

    # Regionen und Gewichte neu bauen (MaStR-CSV von der VM, Natural Earth wird geladen)
    wx-exchange gefs regions --mastr de_capacity_0p5.csv --out config/gefs/regions_v1.json.gz

Registry und Repository kommen aus ``WX_EXCHANGE_REGISTRY`` (``ghcr.io``) und
``WX_EXCHANGE_REPOSITORY`` (Standard Silber, bei ``idx-*`` das ``.idx``-Artefakt), die
Anmeldung zum Schreiben aus ``WX_EXCHANGE_USERNAME`` und ``WX_EXCHANGE_TOKEN`` (in Actions
``github.actor`` und ``GITHUB_TOKEN``). Lesen geht anonym.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
import time
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
from obstore.store import MemoryStore

from predictiondb_core.db import create_db_engine
from predictiondb_core.log import configure_logging, get_logger
from predictiondb_core.s3 import s3_store
from predictiondb_core.settings import get_settings
from wx_exchange import gefs, gefs_regions, idx
from wx_exchange.importer import Importer
from wx_exchange.oci import Registry, RegistryError
from wx_exchange.publish import Publisher, plan
from wx_ingest.bronze import Bronze
from wx_ingest.cli import DEFAULT_CONFIG, parse_hours_ordered, parse_run
from wx_ingest.config import GfsConfig
from wx_ingest.gfs import read_templates, write_templates
from wx_ingest.ingest import GfsIngestor
from wx_process.config import ProcessConfig, WxProcessSettings
from wx_process.store import Store

log = get_logger(__name__)

DEFAULT_REPOSITORY = "the78mole/predictiondb-data/gfs-0p25-silver"
GEFS_CONFIG = Path("config/sources/gefs_0p5.yaml")
SOURCE_URL = "https://github.com/the78mole/predictiondb"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="wx-exchange", description="Silber-Austausch über GHCR")
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--registry", default=os.environ.get("WX_EXCHANGE_REGISTRY", "ghcr.io"))
    p.add_argument("--repository", default=os.environ.get("WX_EXCHANGE_REPOSITORY"))
    sub = p.add_subparsers(dest="model", required=True)
    _gefs_parser(sub)
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
    pub.add_argument(
        "--idx-repository",
        default=idx.DEFAULT_REPOSITORY,
        help="vollständige .idx für header_scan (leer: Köpfe lesen wie bisher)",
    )

    tpl = cmd.add_parser(
        "templates", help="Kopf-Inventare (header_scan) prüfen, fehlende ergänzen, alle schreiben"
    )
    tpl.add_argument("run", type=parse_run, help="erster Lauf")
    tpl.add_argument(
        "end", type=parse_run, nargs="?", help="dazu je Monat bis hier der erste 00Z-Lauf"
    )
    tpl.add_argument("--out", type=Path, required=True)
    tpl.add_argument(
        "--seed", type=Path, help="weitere Vorlagen übernehmen (z. B. Artefakt eines Laufs)"
    )
    tpl.add_argument("--archive-concurrency", type=int, default=96, metavar="N")

    ip = cmd.add_parser("idx-plan", help="Matrix der Monat-Laufzeit-Paare ohne vollständige .idx")
    _range_args(ip)
    ip.add_argument("--limit", type=int, default=256, help="höchstens so viele Jobs")
    ip.add_argument("--all", action="store_true", help="alle Paare, auch vollständige (Neubau)")

    ib = cmd.add_parser("idx-build", help="vollständige .idx eines Monats einer Laufzeit")
    ib.add_argument("month", help="JJJJ-MM")
    ib.add_argument("hour", type=int, choices=(0, 6, 12, 18))
    ib.add_argument("--start", type=parse_run, required=True, help="frühester Lauf")
    ib.add_argument("--end", type=parse_run, required=True, help="spätester Lauf")
    ib.add_argument("--out", type=Path, required=True, help="Layer-Beschreibung (JSON)")
    ib.add_argument("--parallel-files", type=int, default=12, metavar="N")
    ib.add_argument("--archive-concurrency", type=int, default=96, metavar="N")
    ib.add_argument(
        "--minutes", type=float, help="danach keine neue Datei mehr beginnen (Job-Zeitgrenze)"
    )
    ib.add_argument("--rebuild", action="store_true", help="vorhandenen Layer nicht übernehmen")
    ib.add_argument(
        "--workers",
        type=int,
        default=1,
        metavar="N",
        help="Prozesse, Anfragen und Dateien werden aufgeteilt (ein Prozess nutzt einen Kern)",
    )

    im = cmd.add_parser("idx-manifest", help="Layer-Beschreibungen ins Manifest übernehmen")
    im.add_argument("layers", type=Path, help="Verzeichnis mit Layer-Beschreibungen (*.json)")

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
    default = idx.DEFAULT_REPOSITORY if args.cmd.startswith("idx-") else DEFAULT_REPOSITORY
    return Registry(
        args.registry,
        args.repository or default,
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
    if args.model == "gefs":
        return _gefs_main(args)
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
            idx_registry = (
                Registry(
                    args.registry,
                    args.idx_repository,
                    client,
                    username=os.environ.get("WX_EXCHANGE_USERNAME"),
                    password=os.environ.get("WX_EXCHANGE_TOKEN") or None,
                )
                if args.idx_repository
                else None
            )
            return asyncio.run(_publish(args, config, registry, idx_registry))
        if args.cmd == "templates":
            return asyncio.run(_templates(args, config))
        if args.cmd == "idx-plan":
            matrix = idx.plan(
                registry,
                config,
                args.start,
                _end(args, config),
                args.runs,
                limit=args.limit,
                rebuild=args.all,
            )
            _github_output("matrix", json.dumps(matrix, separators=(",", ":")))
            print(json.dumps({"jobs": len(matrix), "matrix": matrix}, indent=1))
            return 0
        if args.cmd == "idx-build":
            return asyncio.run(_idx_build(args, config, registry))
        if args.cmd == "idx-manifest":
            layers = [
                idx.IdxLayer(**json.loads(f.read_text()))
                for f in sorted(args.layers.glob("*.json"))
            ]
            summary = idx.merge_manifest(registry, layers, SOURCE_URL)
            print(json.dumps(summary, indent=1))
            return 0

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


def _archive_client(args: argparse.Namespace, config: GfsConfig) -> httpx.AsyncClient:
    for ep in config.endpoints:
        if ep.archive:
            ep.concurrency = args.archive_concurrency
    return idx.archive_client(config)


def _sample_runs(first: datetime, end: datetime | None) -> list[datetime]:
    """``first`` und je Monat bis ``end`` der erste 00Z-Lauf (Stichproben für Modell-Updates)."""
    runs = [first]
    month = first.replace(day=1, hour=0)
    while end is not None:
        month = (month + timedelta(days=32)).replace(day=1)
        if month > end:
            break
        runs.append(month)
    return runs


async def _templates(args: argparse.Namespace, config: GfsConfig) -> int:
    """Prüft die Vorlagen aus ``config.templates`` an Stichprobenläufen und ergänzt sie.

    Je Lauf und Schritt: Passt eine vorhandene Vorlage, kostet das wenige Anfragen; sonst
    (fehlender Schritt oder geänderte Nachrichtenfolge nach einem Modell-Update) kommt eine
    weitere Variante hinzu.
    """
    runs = _sample_runs(args.run, args.end)
    async with _archive_client(args, config) as client:
        ingestor = GfsIngestor(config, Bronze(MemoryStore()), client, archive_only=True)
        scan = [ep for ep in ingestor.endpoints if ep.config.kind == "header_scan"]
        if not scan:
            print("keine header_scan-Endpunkte in der Konfiguration", file=sys.stderr)
            return 1
        if args.seed:
            scan[0].import_templates(read_templates(args.seed))
        steps = config.step_list()
        ready = {}
        for run in runs:
            built = await scan[0].build_templates(
                run, steps, ingestor.specs, config.step_concurrency
            )
            ready[f"{run:%Y-%m-%dT%H}"] = len(built)
    templates = scan[0].export_templates()
    label = f"{runs[0]:%Y-%m-%dT%H}" + (f"..{runs[-1]:%Y-%m-%dT%H}" if len(runs) > 1 else "")
    write_templates(args.out, label, templates)
    variants = sum(len(v) for v in templates.values())
    print(json.dumps({"steps": len(templates), "variants": variants, "runs": ready}))
    # Fehlende Vorlagen bauen die Monatsjobs selbst; das ist kein Fehler
    return 0


async def _idx_build(args: argparse.Namespace, config: GfsConfig, registry: Registry) -> int:
    deadline = time.monotonic() + args.minutes * 60 if args.minutes else None
    async with _archive_client(args, config) as client:
        endpoints = idx.archive_endpoints(config, client)
        if not endpoints:
            print("keine header_scan-Endpunkte in der Konfiguration", file=sys.stderr)
            return 1
        layer = await idx.build_layer(
            registry,
            config,
            endpoints,
            args.month,
            args.hour,
            args.start,
            args.end,
            parallel_files=args.parallel_files,
            deadline=deadline,
            rebuild=args.rebuild,
            workers=args.workers,
        )
    # Erst ausgeben, dann schreiben: Scheitert die Datei, steht die Beschreibung vollständig im
    # Log und das Manifest lässt sich mit ihr nachtragen
    print(json.dumps(asdict(layer)), flush=True)
    args.out.write_text(json.dumps(asdict(layer), indent=1))
    # Fehlgeschlagene Dateien holt ein Folgelauf nach; der Layer zählt trotzdem
    return 0


def _load_idx(registry: Registry, month: str, hour: int) -> dict[str, bytes]:
    """``.idx`` des Monats; ohne Layer oder bei Fehlern leer (dann Köpfe lesen wie bisher)."""
    try:
        layer, files = idx.load_layer(registry, month, hour)
    except (RegistryError, httpx.HTTPError, OSError, ValueError) as exc:
        log.warning("idx_unavailable", month=month, hour=hour, error=f"{type(exc).__name__}: {exc}")
        return {}
    if layer is None:
        log.info("idx_missing", month=month, hour=hour)
        return {}
    log.info(
        "idx_loaded",
        layer=layer.key,
        files=len(files),
        complete=layer.complete,
        failed=len(layer.failed),
    )
    return files


async def _publish(
    args: argparse.Namespace,
    config: GfsConfig,
    registry: Registry,
    idx_registry: Registry | None = None,
) -> int:
    process = ProcessConfig.from_yaml(args.process_config or WxProcessSettings().config)
    with tempfile.TemporaryDirectory(prefix="wx-exchange-") as tmp:
        workdir = args.workdir or Path(tmp)
        async with _archive_client(args, config) as client:
            publisher = Publisher(
                registry,
                config,
                process,
                client,
                workdir,
                source_url=SOURCE_URL,
                parallel_runs=args.parallel_runs,
            )
            if idx_registry is not None:
                files = await asyncio.to_thread(_load_idx, idx_registry, args.month, args.hour)
                publisher.ingestor.import_idx(files)
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


# --- GEFS ------------------------------------------------------------------------------------


def _gefs_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    gp = sub.add_parser("gefs", help="NOAA GEFS 0,5°, Ensemble je Region verdichtet")
    gp.add_argument("--config", type=Path, default=GEFS_CONFIG)
    cmd = gp.add_subparsers(dest="cmd", required=True)

    pl = cmd.add_parser("plan", help="Matrix der Monate mit fehlenden Läufen (JSON)")
    pl.add_argument("start", type=parse_run)
    pl.add_argument("end", type=parse_run, nargs="?", help="Standard: jetzt")
    pl.add_argument("--max-jobs", type=int, default=256)
    pl.add_argument("--all", action="store_true", help="alle Monate, auch vollständige (Neubau)")

    pub = cmd.add_parser("publish", help="einen Monat einer Laufzeit nach GHCR")
    pub.add_argument("month", help="JJJJ-MM")
    pub.add_argument("hour", type=int, choices=(0, 6, 12, 18))
    pub.add_argument("--start", type=parse_run, help="frühester Lauf")
    pub.add_argument("--end", type=parse_run, help="spätester Lauf (Standard: jetzt)")
    pub.add_argument("--rebuild", action="store_true", help="neu bauen, nur Änderungen hochladen")
    pub.add_argument("--parallel-runs", type=int, default=1, metavar="N")
    pub.add_argument(
        "--concurrency", type=int, help="Anfragen ans Archiv (Standard: Konfiguration)"
    )

    rg = cmd.add_parser("regions", help="Regionen und Gitter-Gewichte bauen")
    rg.add_argument("--mastr", type=Path, required=True, help="CSV der MaStR-Abfrage")
    rg.add_argument("--natural-earth", type=Path, default=Path(".natural-earth"))
    rg.add_argument("--version", default="v1")
    rg.add_argument("--out", type=Path, required=True)


def _gefs_main(args: argparse.Namespace) -> int:
    config = gefs.GefsConfig.from_yaml(args.config)
    if args.cmd == "regions":
        data = gefs_regions.build(args.mastr, args.natural_earth, config.bbox, version=args.version)
        gefs_regions.write(data, args.out)
        print(json.dumps({r["code"]: len(r["cells"]) for r in data["regions"]}, indent=1))
        return 0
    regions = gefs.Regions.load(config.regions)
    with httpx.Client(timeout=httpx.Timeout(120.0, connect=15.0)) as client:
        registry = Registry(
            args.registry,
            args.repository or gefs.DEFAULT_REPOSITORY,
            client,
            username=os.environ.get("WX_EXCHANGE_USERNAME"),
            password=os.environ.get("WX_EXCHANGE_TOKEN") or None,
        )
        if args.cmd == "plan":
            matrix = gefs.plan(
                registry,
                config,
                regions,
                args.start,
                args.end or datetime.now(UTC),
                max_jobs=args.max_jobs,
                rebuild=args.all,
            )
            _github_output("matrix", json.dumps(matrix, separators=(",", ":")))
            print(json.dumps({"jobs": len(matrix), "matrix": matrix}, indent=1))
            return 0
        if args.concurrency:
            config.concurrency = args.concurrency
        result = asyncio.run(_gefs_publish(args, config, regions, registry))
    out = result.as_dict()
    print(json.dumps(out, indent=1))
    if out["failed"]:
        print(f"{len(out['failed'])} Läufe fehlgeschlagen", file=sys.stderr)
        return 1
    return 0


async def _gefs_publish(
    args: argparse.Namespace,
    config: gefs.GefsConfig,
    regions: gefs.Regions,
    registry: Registry,
) -> gefs.MonthResult:
    limits = httpx.Limits(
        max_connections=config.concurrency, max_keepalive_connections=config.concurrency
    )
    timeout = httpx.Timeout(60.0, connect=15.0)
    async with httpx.AsyncClient(timeout=timeout, limits=limits) as client:
        publisher = gefs.GefsPublisher(
            registry,
            config,
            regions,
            client,
            source_url=SOURCE_URL,
            parallel_runs=args.parallel_runs,
        )
        return await publisher.publish_month(
            args.month, args.hour, args.start, args.end, rebuild=args.rebuild
        )


if __name__ == "__main__":
    raise SystemExit(main())
