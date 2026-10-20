"""Kommandozeile ``energy-ingest``.

Beispiele::

    energy-ingest sources                              # Quellen und Datensätze anzeigen
    energy-ingest sync                                 # Fenster um heute einmal abgleichen
    energy-ingest sync --watch 900                     # Dauerbetrieb inkl. Backfill-Häppchen
    energy-ingest backfill 2015-01-01 2026-10-01       # Historie gezielt nachladen
    energy-ingest --source smard backfill 2024-01-01 2024-02-01 --dataset da_price_de_lu
    energy-ingest check --days 14                      # Quellen gegeneinander prüfen
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import asdict
from datetime import UTC, datetime

import httpx

from energy_ingest.config import ALL_SOURCES, EnergySettings
from energy_ingest.http import Http
from energy_ingest.ingest import DatasetResult, EnergyIngestor
from energy_ingest.sources import Source
from energy_ingest.sources.energy_charts import EnergyCharts
from energy_ingest.sources.entsoe import Entsoe
from energy_ingest.sources.smard import Smard
from energy_ingest.store import EnergyStore
from predictiondb_core.db import create_db_engine
from predictiondb_core.log import configure_logging, get_logger
from predictiondb_core.settings import get_settings

log = get_logger(__name__)


def parse_time(text: str) -> datetime:
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H", "%Y-%m-%dT%H:%M"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    raise argparse.ArgumentTypeError(f"Zeitpunkt {text!r} nicht lesbar (z. B. 2024-01-31)")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="energy-ingest", description="Preis-, Last- und Erzeugungsdaten nach TimescaleDB"
    )
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument(
        "--source",
        action="append",
        choices=ALL_SOURCES,
        help="nur diese Quelle(n); Vorgabe aus PDB_ENERGY_SOURCES",
    )
    cmd = p.add_subparsers(dest="cmd", required=True)
    cmd.add_parser("sources", help="aktive Quellen und Datensätze anzeigen")

    sync = cmd.add_parser("sync", help="Fenster um heute abgleichen")
    sync.add_argument(
        "--watch", type=int, metavar="SEK", help="wiederholen im Abstand von SEK Sekunden"
    )
    sync.add_argument(
        "--no-backfill", action="store_true", help="im Dauerbetrieb keine Historie nachladen"
    )

    bf = cmd.add_parser("backfill", help="Zeitraum [START, ENDE) nachladen")
    bf.add_argument("start", type=parse_time)
    bf.add_argument("end", type=parse_time)
    bf.add_argument("--dataset", action="append", help="nur diese Datensätze")
    bf.add_argument("--force", action="store_true", help="auch bereits geladene Blöcke")

    check = cmd.add_parser("check", help="Abweichung zwischen Quellen (Stundenmittel)")
    check.add_argument("--days", type=int, default=7)
    return p


def build_sources(codes: list[str], settings: EnergySettings, http: Http) -> list[Source]:
    sources: list[Source] = []
    for code in codes:
        if code == "smard":
            sources.append(Smard(http))
        elif code == "energy_charts":
            sources.append(EnergyCharts(http))
        elif code == "entsoe":
            token = settings.entsoe_token.get_secret_value()
            if not token:
                log.warning("energy.entsoe_disabled", reason="PDB_ENTSOE_TOKEN nicht gesetzt")
                continue
            sources.append(Entsoe(http, token))
        else:
            raise ValueError(f"unbekannte Quelle {code!r}")
    return sources


def _summary(results: list[DatasetResult]) -> dict[str, int]:
    out: dict[str, int] = {}
    for r in results:
        out[r.status] = out.get(r.status, 0) + 1
    return out


def run(args: argparse.Namespace, settings: EnergySettings) -> int:
    timeout = httpx.Timeout(settings.http_timeout_s, connect=15.0)
    headers = {
        "User-Agent": "PredictionDB energy-ingest (+https://github.com/the78mole/PredictionDB)"
    }
    with httpx.Client(timeout=timeout, follow_redirects=True, headers=headers) as client:
        sources = build_sources(args.source or settings.sources, settings, Http(client))
        if args.cmd == "sources":
            for s in sources:
                print(f"{s.code:14} Rang {s.priority:3}  {', '.join(s.datasets())}")
            return 0

        store = EnergyStore(create_db_engine(get_settings()))
        ingestor = EnergyIngestor(sources, store, settings)

        if args.cmd == "check":
            devs = ingestor.check(days=args.days)
            print(json.dumps([asdict(d) for d in devs], indent=1))
            return 0

        if args.cmd == "backfill":
            results = ingestor.backfill(
                args.start, args.end, datasets=args.dataset, force=args.force
            )
            print(json.dumps([r.as_dict() for r in results], indent=1))
            return 1 if any(r.status == "failed" for r in results) else 0

        while True:
            started = time.monotonic()
            results = ingestor.sync()
            log.info("energy.sync_done", **_summary(results))
            if not args.watch:
                print(json.dumps([r.as_dict() for r in results], indent=1))
                return 1 if any(r.status == "failed" for r in results) else 0
            try:
                ingestor.check(days=2)
                if not args.no_backfill:
                    bf = ingestor.backfill_step()
                    if bf:
                        log.info("energy.backfill_step", **_summary(bf))
            except Exception:
                log.exception("energy.cycle_failed")
            time.sleep(max(0.0, args.watch - (time.monotonic() - started)))


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    core = get_settings()
    configure_logging("DEBUG" if args.verbose else core.log_level, core.log_json)
    return run(args, EnergySettings())


if __name__ == "__main__":
    sys.exit(main())
