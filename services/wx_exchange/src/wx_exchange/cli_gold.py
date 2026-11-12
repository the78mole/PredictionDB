"""Befehle ``wx-exchange gold …``: Gewichte der Cluster und Gold-Pakete in GHCR.

Beispiele::

    # Actions (mastr-weights.yml): neuer MaStR-Export fällig? Dann im Runner verarbeiten
    wx-exchange gold weights-due
    wx-exchange gold weights-build            # braucht PDB_DATABASE_URL (Service-Container)

    # VM (Notweg oder einmalig, um die Clusterung der VM fortzuschreiben)
    wx-exchange gold weights-publish

    # Actions (gold-history.yml): Plan, dann je Job ein Monat einer Laufzeit
    wx-exchange gold plan 2021-03-22T12 --runs 0,12,6,18
    wx-exchange gold build 2026-10 0
    wx-exchange gold --process-config config/process/gfs_0p5.yaml build 2026-10 0

@relation(SWA-WXX-CLI, scope=file)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from datetime import UTC, date, datetime
from pathlib import Path

import httpx

from mastr_ingest.config import MastrConfig, MastrSettings
from predictiondb_core.db import create_db_engine
from predictiondb_core.settings import get_settings
from wx_exchange import gold
from wx_exchange.mastr_weights import build_weights, weights_due
from wx_exchange.oci import Registry
from wx_ingest.cli import parse_hours_ordered, parse_run
from wx_process.config import ProcessConfig, WxProcessSettings
from wx_process.regions import RegionSet

SOURCE_URL = "https://github.com/the78mole/predictiondb"
#: Silber je Quelle (wie ``cli.REPOSITORIES``)
SILVER = {
    "gfs_0p25": "the78mole/predictiondb-data/gfs-0p25-silver",
    "gfs_0p5": "the78mole/predictiondb-data/gfs-0p5-silver",
}
#: höchstens so viele Einträge je Matrix (Grenze von GitHub Actions)
MATRIX_LIMIT = 256
BLOCKS = 3


def add_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """@relation(SWR-WXX-073, SWR-WXX-074, scope=function)"""
    gp = sub.add_parser("gold", help="Gewichte der Cluster und Gold-Pakete in GHCR")
    gp.add_argument("--process-config", type=Path, help="Standard: PDB_WX_PROCESS_CONFIG")
    gp.add_argument("--gold-repository", help="Standard: je Quelle (gfs-0p25-gold, gfs-0p5-gold)")
    gp.add_argument("--weights-repository", help="Standard: gfs-0p25-weights")
    gp.add_argument("--silver-repository", help="Standard: Silber der Quelle")
    gp.add_argument(
        "--weights", default=gold.LATEST, metavar="TAG", help="Tag der Gewichte (Standard latest)"
    )
    cmd = gp.add_subparsers(dest="cmd", required=True)

    cmd.add_parser("weights-due", help="neuer MaStR-Export fällig? (Ausgabe due=true|false)")

    wb = cmd.add_parser(
        "weights-build", help="MaStR laden, clustern und Gewichte veröffentlichen (Runner)"
    )
    wb.add_argument("--date", type=date.fromisoformat, help="Exportdatum (Standard: heute, UTC)")
    wb.add_argument("--zip", type=Path, help="lokal vorliegender Export statt Download")
    wb.add_argument("--mastr-config", type=Path, help="Standard: PDB_MASTR_CONFIG")
    wb.add_argument("--workdir", type=Path, help="Standard: temporäres Verzeichnis")
    wb.add_argument("--force", action="store_true", help="auch wenn nicht fällig")

    wp = cmd.add_parser("weights-publish", help="Gewichte aus der Datenbank veröffentlichen (VM)")
    wp.add_argument("--clustering", help="Version (Standard: jüngste)")

    pl = cmd.add_parser("plan", help="Matrix der Monate mit fehlendem oder veraltetem Gold")
    pl.add_argument("start", type=parse_run)
    pl.add_argument("end", type=parse_run, nargs="?", help="Standard: jetzt")
    pl.add_argument("--runs", type=parse_hours_ordered, default=[0, 12, 6, 18], metavar="HH,…")
    pl.add_argument("--all", action="store_true", help="alle Monate neu bauen")

    b = cmd.add_parser("build", help="Gold eines Monats einer Laufzeit nach GHCR")
    b.add_argument("month", help="JJJJ-MM")
    b.add_argument("hour", type=int, choices=(0, 6, 12, 18))
    b.add_argument("--workers", type=int, default=4, metavar="N", help="Läufe gleichzeitig")
    b.add_argument("--rebuild", action="store_true", help="auch bei gleichem Fingerabdruck")


def _registry(args: argparse.Namespace, client: httpx.Client, repository: str) -> Registry:
    return Registry(
        args.registry,
        repository,
        client,
        username=os.environ.get("WX_EXCHANGE_USERNAME"),
        password=os.environ.get("WX_EXCHANGE_TOKEN") or None,
    )


def _process(args: argparse.Namespace) -> ProcessConfig:
    return ProcessConfig.from_yaml(args.process_config or WxProcessSettings().config)


def _github_output(name: str, value: str) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{name}={value}\n")


def _weights(
    args: argparse.Namespace, client: httpx.Client, process: ProcessConfig
) -> gold.Weights:
    """Feste Regionen aus dem Repo, Cluster aus dem Gewichte-Artefakt."""
    if process.regions is not None:
        return gold.Weights.from_region_sets(
            [RegionSet.load(process.regions), *(RegionSet.load(p) for p in process.sites)]
        )
    repo = args.weights_repository or gold.WEIGHTS_REPOSITORIES[process.source]
    return gold.load_weights(_registry(args, client, repo), args.weights)


def _previous(args: argparse.Namespace, client: httpx.Client, repo: str) -> gold.Weights | None:
    try:
        return gold.load_weights(_registry(args, client, repo), args.weights)
    except LookupError:
        return None


def main(args: argparse.Namespace) -> int:
    """@relation(SWR-WXX-070, SWR-WXX-073, SWR-WXX-074, scope=function)"""
    process = _process(args)
    timeout = httpx.Timeout(120.0, connect=15.0)
    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        weights_repo = args.weights_repository or gold.WEIGHTS_REPOSITORIES.get(process.source)
        if args.cmd.startswith("weights-") and weights_repo is None:
            print(f"{process.source}: feste Regionen, keine Gewichte aus dem MaStR")
            return 1
        if args.cmd == "weights-due":
            assert weights_repo is not None
            previous = _previous(args, client, weights_repo)
            due = weights_due(previous, datetime.now(UTC).date())
            _github_output("due", "true" if due else "false")
            print(
                json.dumps(
                    {
                        "due": due,
                        "version": previous.version if previous else None,
                        "mastr_export_date": previous.info.get("mastr_export_date")
                        if previous
                        else None,
                    }
                )
            )
            return 0
        if args.cmd == "weights-build":
            assert weights_repo is not None
            previous = _previous(args, client, weights_repo)
            day = args.date or datetime.now(UTC).date()
            if not args.force and not weights_due(previous, day):
                print(json.dumps({"due": False}))
                return 0
            mastr = MastrSettings()
            config = MastrConfig.from_yaml(args.mastr_config or mastr.config)
            engine = create_db_engine(get_settings())
            with tempfile.TemporaryDirectory(prefix="mastr-") as tmp:
                weights = build_weights(
                    engine,
                    config,
                    process.weight_grid,
                    args.workdir or Path(tmp),
                    client,
                    day,
                    previous,
                    local_zip=args.zip,
                )
            tags = gold.publish_weights(_registry(args, client, weights_repo), weights, SOURCE_URL)
            print(json.dumps({"version": weights.version, "tags": tags, **weights.info}))
            return 0
        if args.cmd == "weights-publish":
            assert weights_repo is not None
            weights = gold.Weights.from_db(
                create_db_engine(get_settings()), process.weight_grid, args.clustering
            )
            tags = gold.publish_weights(_registry(args, client, weights_repo), weights, SOURCE_URL)
            print(json.dumps({"version": weights.version, "tags": tags, **weights.info}))
            return 0

        gold_repo = args.gold_repository or gold.REPOSITORIES[process.source]
        silver_repo = args.silver_repository or SILVER[process.source]
        try:
            weights = _weights(args, client, process)
        except LookupError as exc:
            # vor dem ersten Lauf von mastr-weights.yml: kein Fehler im Plan, nur nichts zu tun
            print(str(exc), file=sys.stderr)
            if args.cmd == "plan":
                for i in range(1, BLOCKS + 1):
                    _github_output(f"block{i}", "[]")
                return 0
            return 1
        gold_reg = _registry(args, client, gold_repo)
        silver_reg = Registry(args.registry, silver_repo, client)
        if args.cmd == "plan":
            entries = gold.plan(
                gold_reg,
                silver_reg,
                process,
                weights,
                args.start,
                args.end or datetime.now(UTC),
                args.runs,
                rebuild=args.all,
            )
            if len(entries) > MATRIX_LIMIT * BLOCKS:
                raise SystemExit(f"{len(entries)} Jobs, höchstens {MATRIX_LIMIT * BLOCKS}")
            blocks = [
                entries[i : i + MATRIX_LIMIT] for i in range(0, MATRIX_LIMIT * BLOCKS, MATRIX_LIMIT)
            ]
            for i, block in enumerate(blocks, start=1):
                _github_output(f"block{i}", json.dumps(block, separators=(",", ":")))
            _github_output("weights", weights.version)
            print(
                json.dumps(
                    {"weights": weights.version, "jobs": len(entries), "matrix": entries}, indent=1
                )
            )
            return 0
        builder = gold.GoldBuilder(
            gold_reg, silver_reg, process, weights, source_url=SOURCE_URL, workers=args.workers
        )
        result = builder.build_month(args.month, args.hour, rebuild=args.rebuild)
        print(json.dumps(result.as_dict()))
        return 0
