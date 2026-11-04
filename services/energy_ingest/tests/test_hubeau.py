"""Abfluss von Hub'eau (eaufrance).

Fixtures sind echte Antworten vom 04.11.2026 für Loire à Gien (K4180010): Tagesmittel
28.10. bis 03.11. und Echtzeitwerte 10:00 bis 12:20 UTC, jeweils mit der Reihe des Standorts
(``code_station`` leer) und den Reihen der beiden Stationen.
"""

import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from energy_ingest.config import ALL_SOURCES, EnergySettings
from energy_ingest.http import Http
from energy_ingest.model import DAY, HOUR, RIVER_GAUGES, Kind, estimate_published, series
from energy_ingest.sources import FormatError, SourceError
from energy_ingest.sources.hubeau import Hubeau, hourly_means, parse_elab, parse_tr, prefer_site

FIXTURES = Path(__file__).parent / "fixtures"
Handler = Callable[[httpx.Request], httpx.Response]
MakeHttp = Callable[[Handler], Http]

NOW = datetime(2026, 11, 4, 20, 0, tzinfo=UTC)
OCT28 = datetime(2026, 10, 28, tzinfo=UTC)


def _json(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text())


def test_parse_elab_prefers_site_and_converts() -> None:
    """@relation(SWR-ENI-053, scope=function)"""
    cands, nxt = parse_elab(_json("hubeau_obs_elab.json"))
    assert nxt is None
    assert len(cands) == 21
    pts = prefer_site(cands)
    assert [ts for ts, _ in pts] == [OCT28 + i * DAY for i in range(7)]
    # Standort 62 996 l/s, nicht Station K418001022 mit 60 520 l/s
    assert pts[0][1] == pytest.approx(62.996)
    assert pts[-1][1] == pytest.approx(48.216)


def test_parse_elab_falls_back_to_first_station() -> None:
    """@relation(SWR-ENI-053, scope=function)"""
    body = _json("hubeau_obs_elab.json")
    body["data"] = [r for r in body["data"] if r["code_station"] is not None]
    pts = prefer_site(parse_elab(body)[0])
    assert len(pts) == 7
    assert pts[0][1] == pytest.approx(62.996)  # K418001010 vor K418001022


def test_parse_tr_and_hourly_means() -> None:
    """@relation(SWR-ENI-053, SWR-ENI-020, scope=function)"""
    cands, nxt = parse_tr(_json("hubeau_observations_tr.json"))
    assert nxt is None
    raw = prefer_site(cands)
    assert len(raw) == 29  # 10:00 bis 12:20 alle fünf Minuten
    hours = hourly_means(raw)
    h10 = datetime(2026, 11, 4, 10, tzinfo=UTC)
    # 12:00 bis 12:20 ist noch unvollständig und fehlt
    assert [ts for ts, _ in hours] == [h10, h10 + HOUR]
    site_10 = [v for ts, v in raw if h10 <= ts < h10 + HOUR]
    assert len(site_10) == 12
    assert hours[0][1] == pytest.approx(sum(site_10) / 12)
    assert hours[0][1] == pytest.approx(46.742, abs=0.001)


def test_format_errors() -> None:
    """@relation(SWR-ENI-053, SWR-ENI-030, scope=function)"""
    with pytest.raises(FormatError, match="unerwartete Antwort"):
        parse_elab({"count": 0})
    body = _json("hubeau_obs_elab.json")
    del body["data"][0]["resultat_obs_elab"]
    with pytest.raises(FormatError, match="resultat_obs_elab"):
        parse_elab(body)
    body = _json("hubeau_obs_elab.json")
    body["data"][0]["resultat_obs_elab"] = "viel"
    with pytest.raises(FormatError, match="nicht lesbar"):
        parse_elab(body)
    body = _json("hubeau_observations_tr.json")
    body["data"][0]["date_obs"] = "2026-11-04T10:00:00"
    with pytest.raises(FormatError, match="Zeitzone"):
        parse_tr(body)


def test_missing_value_is_skipped() -> None:
    """@relation(SWR-ENI-053, scope=function)"""
    body = _json("hubeau_obs_elab.json")
    for r in body["data"]:
        if r["date_obs_elab"] == "2026-10-28":
            r["resultat_obs_elab"] = None
    pts = prefer_site(parse_elab(body)[0])
    assert OCT28 not in dict(pts)


def test_fetch_day_follows_cursor(make_http: MakeHttp) -> None:
    """@relation(SWR-ENI-053, scope=function)"""
    body = _json("hubeau_obs_elab.json")
    seen: list[httpx.Request] = []
    nxt = "https://hubeau.eaufrance.fr/api/v2/hydrometrie/obs_elab?cursor=abc&size=20000"

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        if "cursor" in req.url.params:
            return httpx.Response(200, json={**body, "data": body["data"][7:], "next": None})
        return httpx.Response(200, json={**body, "data": body["data"][:7], "next": nxt})

    out = Hubeau(make_http(handler), lambda: NOW).fetch(
        "flow_day_loire_gien", OCT28 + DAY, OCT28 + 4 * DAY
    )
    assert len(seen) == 2
    p = seen[0].url.params
    assert (p["code_entite"], p["grandeur_hydro_elab"]) == ("K4180010", "QmnJ")
    assert (p["date_debut_obs_elab"], p["date_fin_obs_elab"]) == ("2026-10-29", "2026-10-31")
    assert seen[1].url.params["cursor"] == "abc"
    assert len(out) == 1
    s = out[0]
    assert (s.code, s.resolution) == ("river_flow_loire_gien", DAY)
    assert [ts for ts, _ in s.points] == [OCT28 + i * DAY for i in (1, 2, 3)]
    assert s.points[0][1] == pytest.approx(60.660)  # Standort, auf Seite 2


def test_fetch_hour(make_http: MakeHttp) -> None:
    """@relation(SWR-ENI-053, scope=function)"""
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200, json=_json("hubeau_observations_tr.json"))

    h10 = datetime(2026, 11, 4, 10, tzinfo=UTC)
    out = Hubeau(make_http(handler), lambda: NOW).fetch(
        "flow_hour_loire_gien", h10 - DAY, h10 + DAY
    )
    p = seen[0].url.params
    assert (p["code_entite"], p["grandeur_hydro"], p["sort"]) == ("K4180010", "Q", "asc")
    assert p["date_debut_obs"] == "2026-11-03T10:00:00Z"
    assert p["date_fin_obs"] == "2026-11-05T11:00:00Z"
    assert out[0].code == "river_flow_loire_gien_h"
    assert out[0].resolution == HOUR
    assert [ts for ts, _ in out[0].points] == [h10, h10 + HOUR]


def test_fetch_hour_skips_old_blocks(make_http: MakeHttp) -> None:
    """Echtzeitwerte gibt es nur etwa einen Monat; ältere Blöcke ohne Abruf.

    @relation(SWR-ENI-053, scope=function)
    """

    def handler(req: httpx.Request) -> httpx.Response:
        raise AssertionError("kein Abruf erwartet")

    src = Hubeau(make_http(handler), lambda: NOW)
    assert src.fetch("flow_hour_meuse_chooz", NOW - 60 * DAY, NOW - 30 * DAY) == []


def test_fetch_http_error(make_http: MakeHttp) -> None:
    """@relation(SWR-ENI-053, SWR-ENI-030, scope=function)"""

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"code": 400, "message": "mauvais paramètre"})

    with pytest.raises(SourceError, match="400"):
        Hubeau(make_http(handler), lambda: NOW).fetch("flow_day_meuse_chooz", OCT28, NOW)


def test_datasets_series_and_published(make_http: MakeHttp) -> None:
    """@relation(SWR-ENI-053, SWR-ENI-021, SWR-ENI-004, scope=function)"""
    src = Hubeau.__new__(Hubeau)
    assert len(src.datasets()) == 2 * len(RIVER_GAUGES) == 28
    assert Hubeau.free
    assert "hubeau" in ALL_SOURCES
    assert EnergySettings().allowed("hubeau")
    day = series("river_flow_rhone_st_rambert")
    hour = series("river_flow_rhone_st_rambert_h")
    assert (day.kind, day.unit) == (Kind.RIVER_DAY, "m3/s")
    assert hour.kind is Kind.RIVER_RT
    assert estimate_published(day.kind, OCT28, DAY) == OCT28 + 2 * DAY
    h = datetime(2026, 11, 4, 10, tzinfo=UTC)
    assert estimate_published(hour.kind, h, HOUR) == h + timedelta(hours=4)
    http = make_http(lambda req: httpx.Response(500))
    assert Hubeau(http, lambda: NOW).sync_lookback["flow_day_vienne_lussac"] == timedelta(days=10)
