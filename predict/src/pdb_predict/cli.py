"""Kommandozeile ``pdb-predict``.

Beispiele::

    pdb-predict serve                         # Dauerbetrieb: Läufe nach Zeitplan und HTTP
    pdb-predict once                          # eine Prognose jetzt, nach out/ und stdout
    pdb-predict once --issue 2026-10-24T08 --no-write
    pdb-predict info                          # Modellpaket anzeigen (holt es bei Bedarf)
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import UTC, datetime, timedelta

import httpx
import structlog

from energy_ingest.http import Http
from pdb_predict.energy import EnergyCache, build_sources
from pdb_predict.package import ModelPackage, PackageSource
from pdb_predict.server import Api, serve
from pdb_predict.service import Predictor, write_outputs
from pdb_predict.settings import PredictSettings
from pdb_predict.weather import WeatherCache
from predictiondb_core.log import configure_logging, get_logger
from predictiondb_core.settings import get_settings
from wx_ingest.config import GfsConfig

log = get_logger(__name__)

USER_AGENT = "PredictionDB pdb-predict (+https://github.com/the78mole/PredictionDB)"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="pdb-predict", description="Last- und Preisprognose DE-LU")
    cmd = p.add_subparsers(dest="cmd", required=True)
    cmd.add_parser("serve", help="Läufe nach Zeitplan, Ausgabe als Dateien und HTTP")
    o = cmd.add_parser("once", help="eine Prognose rechnen und ausgeben")
    o.add_argument("--issue", help="Ausgabezeitpunkt UTC, z. B. 2026-10-24T08 (Vorgabe: jetzt)")
    o.add_argument("--no-write", action="store_true", help="nur ausgeben, nicht nach out/")
    cmd.add_parser("info", help="Modellpaket anzeigen")
    return p


def next_slot(now: datetime, every_min: int, offset_min: int) -> datetime:
    """Nächster Startzeitpunkt im Raster ``offset_min + k · every_min`` ab Mitternacht UTC."""
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    k = int(((now - day).total_seconds() / 60 - offset_min) // every_min) + 1
    slot = day + timedelta(minutes=offset_min + k * every_min)
    return slot if slot > now else slot + timedelta(minutes=every_min)


def make_predictor(s: PredictSettings, client: httpx.Client) -> tuple[Predictor, PackageSource]:
    source = PackageSource(s.model, s.cache_dir, client, s.model_refresh_h)
    energy = EnergyCache(s.cache_dir, build_sources(Http(client)))

    def weather(pkg: ModelPackage) -> WeatherCache:
        w = pkg.info.get("weather") or {}
        return WeatherCache(
            s.cache_dir,
            GfsConfig.from_yaml(s.gfs_config),
            run_hours=w.get("run_hours", [0, 12]),
            max_lead_h=int(w.get("max_lead_h", 216)),
            timeout_s=s.http_timeout_s,
        )

    return Predictor(source.get, energy, weather), source


def main(argv: list[str] | None = None) -> int:
    a = build_parser().parse_args(argv)
    core = get_settings()
    configure_logging(core.log_level, core.log_json)
    if a.cmd != "serve":  # stdout gehört der Ausgabe
        structlog.configure(logger_factory=structlog.PrintLoggerFactory(file=sys.stderr))
    s = PredictSettings()
    timeout = httpx.Timeout(s.http_timeout_s, connect=15.0)
    with httpx.Client(
        timeout=timeout, follow_redirects=True, headers={"User-Agent": USER_AGENT}
    ) as client:
        predictor, source = make_predictor(s, client)
        if a.cmd == "info":
            print(json.dumps({**source.get().describe(), "path": str(source.get().path)}, indent=1))
            return 0
        if a.cmd == "once":
            issue = (
                datetime.strptime(a.issue, "%Y-%m-%dT%H").replace(tzinfo=UTC) if a.issue else None
            )
            doc = predictor.run(issue)
            if not a.no_write:
                write_outputs(doc, s.out_dir, s.keep_days)
            print(json.dumps(doc, indent=1, ensure_ascii=False))
            return 0 if doc["targets"] else 1

        api = Api(s.out_dir, s.stale_after_h)
        if s.http_port:
            serve(api, s.http_host, s.http_port)
            log.info("http.listening", host=s.http_host, port=s.http_port)
        while True:
            try:
                doc = predictor.run()
                if doc["targets"]:
                    write_outputs(doc, s.out_dir, s.keep_days)
                    api.last_error = None
                else:
                    api.last_error = "Lauf ohne Prognose (Eingänge fehlen?)"
            except Exception as e:  # der Dienst bleibt stehen und liefert die letzte Prognose
                log.exception("predict.failed")
                api.last_error = f"{type(e).__name__}: {e}"
            now = datetime.now(UTC)
            slot = next_slot(now, s.every_min, s.offset_min)
            log.info("predict.next", at=slot.isoformat(timespec="minutes"))
            time.sleep((slot - now).total_seconds())


if __name__ == "__main__":
    sys.exit(main())
