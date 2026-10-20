from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from energy_ingest.http import Http
from energy_ingest.sources import SourceError
from energy_ingest.sources.entsoe import Entsoe, parse_document, parse_duration

FIXTURES = Path(__file__).parent / "fixtures"
T0 = datetime(2026, 10, 18, 22, 0, tzinfo=UTC)
T1 = T0 + timedelta(hours=1)


def fixture_handler(name: str, status: int = 200) -> Callable[[httpx.Request], httpx.Response]:
    def h(req: httpx.Request) -> httpx.Response:
        assert req.url.params["securityToken"] == "tok"
        assert req.url.params["periodStart"] == "202610182200"
        return httpx.Response(status, content=(FIXTURES / name).read_bytes())

    return h


def test_parse_duration() -> None:
    assert parse_duration("PT15M") == timedelta(minutes=15)
    assert parse_duration("PT60M") == timedelta(hours=1)
    assert parse_duration("P1D") == timedelta(days=1)
    with pytest.raises(SourceError):
        parse_duration("P1Y")


def test_generation(make_http: Callable[..., Http]) -> None:
    src = Entsoe(make_http(fixture_handler("entsoe_a75.xml")), "tok")
    out = {d.code: d for d in src.fetch("generation", T0, T1)}
    # Pumpspeicher-Verbrauch (outBiddingZone) wird übergangen
    assert set(out) == {"gen_wind_onshore", "gen_nuclear", "gen_solar"}
    assert [v for _, v in out["gen_wind_onshore"].points] == [10000, 10100, 10200, 10300]
    # Kurventyp A03: fehlende Positionen wiederholen den vorigen Wert
    assert [v for _, v in out["gen_nuclear"].points] == [0, 0, 0, 0]
    assert [v for _, v in out["gen_solar"].points] == [1, 1, 5, 5]


def test_price_prefers_finest_resolution(make_http: Callable[..., Http]) -> None:
    src = Entsoe(make_http(fixture_handler("entsoe_a44.xml")), "tok")
    (data,) = src.fetch("price", T0, T1)
    assert data.resolution == timedelta(minutes=15)
    assert [v for _, v in data.points] == [81.5, -2.25, -2.25, 79]


def test_no_data_is_empty(make_http: Callable[..., Http]) -> None:
    src = Entsoe(make_http(fixture_handler("entsoe_ack.xml", status=200)), "tok")
    assert src.fetch("price", T0, T1) == []


def test_error_ack_raises() -> None:
    xml = (FIXTURES / "entsoe_ack.xml").read_bytes().replace(b"999", b"401")
    xml = xml.replace(b"No matching data found", b"Unauthorized")
    with pytest.raises(SourceError):
        parse_document(xml)


def test_requires_token(make_http: Callable[..., Http]) -> None:
    with pytest.raises(ValueError):
        Entsoe(make_http(lambda r: httpx.Response(200)), "")
