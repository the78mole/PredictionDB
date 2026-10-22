from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from energy_ingest.http import Http
from energy_ingest.model import MONTH, YEAR, Kind, estimate_published
from energy_ingest.sources import SourceError
from energy_ingest.sources.netztransparenz import Netztransparenz, parse_months, parse_years

FIXTURES = Path(__file__).parent / "fixtures"

MONTHS = (FIXTURES / "netztransparenz_marktpraemie_2025.csv").read_text()
YEARS = (FIXTURES / "netztransparenz_jahresmarktpraemie.csv").read_text()

JAN_2025 = datetime(2024, 12, 31, 23, 0, tzinfo=UTC)  # 01.01.2025 00:00 MEZ


def test_parse_months() -> None:
    out = {d.code: d for d in parse_months(MONTHS)}
    assert set(out) == {"mw_base", "mw_wind_onshore", "mw_wind_offshore", "mw_solar"}
    solar = out["mw_solar"]
    assert solar.resolution == MONTH
    assert len(solar.points) == 12
    # 1/2025: MW Solar 11,511 ct/kWh
    assert solar.points[0][0] == JAN_2025
    assert solar.points[0][1] == pytest.approx(115.11)
    # Juli beginnt in MESZ um 22:00 UTC des Vortags
    assert solar.points[6][0] == datetime(2025, 6, 30, 22, 0, tzinfo=UTC)


def test_parse_years() -> None:
    out = {d.code: d for d in parse_years(YEARS)}
    assert set(out) == {"jw_base", "jw_wind_onshore", "jw_wind_offshore", "jw_solar"}
    solar = dict(out["jw_solar"].points)
    assert out["jw_solar"].resolution == YEAR
    assert solar[JAN_2025] == pytest.approx(45.08)
    assert solar[datetime(2022, 12, 31, 23, 0, tzinfo=UTC)] == pytest.approx(72.0)  # "7,2"


def test_parse_rejects_error_text() -> None:
    with pytest.raises(SourceError):
        parse_years("Es können keine Daten vor dem 31.12.2012 23:00 abgerufen werden.")


def test_published_after_period() -> None:
    assert estimate_published(Kind.MARKET_VALUE_MONTH, JAN_2025, MONTH) == datetime(
        2025, 1, 31, 23, 0, tzinfo=UTC
    ) + timedelta(days=14)
    assert estimate_published(Kind.MARKET_VALUE_YEAR, JAN_2025, YEAR) == datetime(
        2025, 12, 31, 23, 0, tzinfo=UTC
    ) + timedelta(days=14)


class Api:
    def __init__(self) -> None:
        self.tokens = 0
        self.paths: list[str] = []
        self.reject_next = False

    def __call__(self, req: httpx.Request) -> httpx.Response:
        if req.url.host == "identity.example":
            body = req.content.decode()
            assert "grant_type=client_credentials" in body
            assert "client_id=cid" in body
            self.tokens += 1
            return httpx.Response(200, json={"access_token": f"t{self.tokens}", "expires_in": 3600})
        assert req.headers["Authorization"] == f"Bearer t{self.tokens}"
        if self.reject_next:
            self.reject_next = False
            return httpx.Response(401)
        self.paths.append(req.url.path)
        if req.url.path.startswith("/api/v1/data/marktpraemie/"):
            return httpx.Response(200, text=MONTHS)
        if req.url.path == "/api/v1/data/Jahresmarktpraemie":
            return httpx.Response(200, text=YEARS)
        return httpx.Response(404)


def source(make_http: Callable[..., Http], api: Api) -> Netztransparenz:
    return Netztransparenz(
        make_http(api),
        "cid",
        "secret",
        base_url="https://ds.example/api/v1/data",
        token_url="https://identity.example/users/connect/token",
    )


def test_fetch_months_window_and_token_reuse(make_http: Callable[..., Http]) -> None:
    api = Api()
    nt = source(make_http, api)
    # [01.03.2025, 01.05.2025) Ortszeit -> März und April
    start = datetime(2025, 2, 28, 23, 0, tzinfo=UTC)
    end = datetime(2025, 4, 30, 22, 0, tzinfo=UTC)
    out = {d.code: d for d in nt.fetch("market_value_month", start, end)}
    assert api.paths == ["/api/v1/data/marktpraemie/03/2025/04/2025"]
    assert len(out["mw_solar"].points) == 2
    nt.fetch("market_value_year", JAN_2025, JAN_2025 + YEAR)
    assert api.tokens == 1


def test_fetch_renews_token_on_401(make_http: Callable[..., Http]) -> None:
    api = Api()
    nt = source(make_http, api)
    nt.fetch("market_value_year", JAN_2025, JAN_2025 + YEAR)
    api.reject_next = True
    out = {d.code: d for d in nt.fetch("market_value_year", JAN_2025, JAN_2025 + YEAR)}
    assert api.tokens == 2
    assert out["jw_solar"].points


def test_fetch_before_first_month_is_empty(make_http: Callable[..., Http]) -> None:
    api = Api()
    nt = source(make_http, api)
    start = datetime(2010, 1, 1, tzinfo=UTC)
    assert nt.fetch("market_value_month", start, start + timedelta(days=366)) == []
    assert nt.fetch("market_value_year", start, start + timedelta(days=366)) == []
    assert api.paths == []
