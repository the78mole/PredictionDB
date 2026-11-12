"""Befehle ``wx-exchange energy …``: Energiedaten freier Quellen je Jahr in GHCR.

Beispiele::

    # Actions (energy-history.yml): Plan, dann je Job ein Jahr
    wx-exchange energy plan                     # fehlende Jahre, laufendes Jahr, im Jan/Feb Vorjahr
    wx-exchange energy plan --all               # alle Jahre neu
    wx-exchange energy build 2024
    wx-exchange energy --source smard build 2026

@relation(SWA-WXX-CLI, scope=file)
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import UTC, datetime
from urllib.parse import urlsplit

import httpx

from energy_ingest.config import EnergySettings
from energy_ingest.http import Http
from energy_ingest.sources.gie import AGSI_URL, ALSI_URL
from wx_exchange import energy
from wx_exchange.oci import Registry

SOURCE_URL = "https://github.com/the78mole/PredictionDB"
USER_AGENT = "PredictionDB wx-exchange (+https://github.com/the78mole/PredictionDB)"
READ_ONLY = frozenset({"plan"})
#: Der GIE-Schlüssel steht im Header ``x-key``; httpx entfernt bei Umleitungen nur
#: ``Authorization``, deshalb geht ``x-key`` nur an diese Hosts
KEY_HOSTS = frozenset({urlsplit(AGSI_URL).hostname, urlsplit(ALSI_URL).hostname})


def _strip_foreign_keys(request: httpx.Request) -> None:
    """Auch bei Umleitungen: ``x-key`` nur per https an die GIE-Hosts.

    @relation(SWR-WXX-080, scope=function)
    """
    if "x-key" in request.headers and (
        request.url.scheme != "https" or request.url.host not in KEY_HOSTS
    ):
        del request.headers["x-key"]


def add_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """@relation(SWR-WXX-080, SWR-WXX-082, scope=function)"""
    ep = sub.add_parser("energy", help="Energiedaten freier Quellen je Jahr in GHCR")
    ep.add_argument("--energy-repository", default=energy.REPOSITORY)
    ep.add_argument(
        "--source",
        action="append",
        help=f"nur diese Quellen (Standard: {', '.join(energy.SOURCES)}, mit Schlüssel auch "
        f"{', '.join(energy.KEYED_SOURCES)})",
    )
    cmd = ep.add_subparsers(dest="cmd", required=True)
    pl = cmd.add_parser("plan", help="Jahre, die fehlen oder neu abgerufen werden")
    pl.add_argument("--from", dest="first", type=int, default=energy.FIRST_YEAR, metavar="JJJJ")
    pl.add_argument("--to", dest="last", type=int, default=9999, metavar="JJJJ")
    pl.add_argument("--all", action="store_true", help="alle Jahre neu abrufen")
    b = cmd.add_parser("build", help="ein Jahr abrufen und nach GHCR")
    b.add_argument("year", type=int)
    b.add_argument("--full", action="store_true", help="auch im laufenden Jahr alles neu abrufen")


def _github_output(name: str, value: str) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{name}={value}\n")


def main(args: argparse.Namespace) -> int:
    """@relation(SWR-WXX-080, SWR-WXX-082, scope=function)"""
    settings = EnergySettings()
    timeout = httpx.Timeout(settings.http_timeout_s, connect=15.0)
    headers = {"User-Agent": USER_AGENT}
    with httpx.Client(
        timeout=timeout,
        follow_redirects=True,
        headers=headers,
        event_hooks={"request": [_strip_foreign_keys]},
    ) as client:
        registry = Registry(
            args.registry,
            args.energy_repository,
            client,
            username=os.environ.get("WX_EXCHANGE_USERNAME"),
            password=os.environ.get("WX_EXCHANGE_TOKEN") or None,
            read_only=args.cmd in READ_ONLY,
        )
        if args.cmd == "plan":
            years = energy.plan(
                registry, args.first, args.last, datetime.now(UTC), rebuild=args.all
            )
            _github_output("years", json.dumps(years, separators=(",", ":")))
            print(json.dumps({"years": years}))
            return 0
        codes = list(dict.fromkeys(args.source or [*energy.SOURCES, *energy.KEYED_SOURCES]))
        sources = energy.build_sources_free(codes, Http(client), settings)
        missing = set(codes) - {s.code for s in sources} - {energy.MARKTWERT}
        if args.source and missing:
            # ausdrücklich verlangt, aber unfrei, unbekannt oder ohne Schlüssel
            print(f"Quellen nicht verfügbar: {', '.join(sorted(missing))}")
            return 2
        builder = energy.EnergyBuilder(registry, sources, source_url=SOURCE_URL)
        result = builder.build_year(args.year, full=args.full)
        print(json.dumps(result.as_dict(), indent=1))
        return 1 if result.status == "failed" else 0
