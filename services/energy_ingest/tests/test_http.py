from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import httpx
import pytest

from energy_ingest.http import Http, retry_after


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_retry_after_seconds_and_date() -> None:
    """@relation(SWR-ENI-032, scope=function)"""
    assert retry_after(httpx.Response(429, headers={"Retry-After": "7"})) == 7.0
    soon = datetime.now(UTC) + timedelta(seconds=30)
    got = retry_after(httpx.Response(429, headers={"Retry-After": format_datetime(soon)}))
    assert got is not None and 25 <= got <= 31
    assert retry_after(httpx.Response(429)) is None
    assert retry_after(httpx.Response(429, headers={"Retry-After": "bald"})) is None


def test_throttle_per_host(monkeypatch: pytest.MonkeyPatch) -> None:
    """@relation(SWR-ENI-032, scope=function)"""
    clock = [100.0]
    sleeps: list[float] = []

    def sleep(s: float) -> None:
        sleeps.append(s)
        clock[0] += s

    monkeypatch.setattr("energy_ingest.http.time.monotonic", lambda: clock[0])
    monkeypatch.setattr("energy_ingest.http.time.sleep", sleep)
    with _client(lambda r: httpx.Response(200)) as c:
        h = Http(c, pace={"api.energy-charts.info": 2.0})
        h.get("https://api.energy-charts.info/price")
        clock[0] += 0.5
        h.get("https://api.energy-charts.info/price")
        h.get("https://www.smard.de/app/chart_data")
    assert sleeps == [pytest.approx(1.5)]


def test_retry_waits_for_retry_after(monkeypatch: pytest.MonkeyPatch) -> None:
    """@relation(SWR-ENI-032, scope=function)"""
    sleeps: list[float] = []
    monkeypatch.setattr("tenacity.nap.time.sleep", sleeps.append)
    calls = iter([httpx.Response(429, headers={"Retry-After": "5"}), httpx.Response(200)])
    with _client(lambda r: next(calls)) as c:
        h = Http(c, attempts=2, wait_max=0.01, pace={})
        assert h.get("https://api.energy-charts.info/price").status_code == 200
    assert sleeps == [5.0]


def test_retry_after_is_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    """@relation(SWR-ENI-032, scope=function)"""
    sleeps: list[float] = []
    monkeypatch.setattr("tenacity.nap.time.sleep", sleeps.append)
    calls = iter([httpx.Response(429, headers={"Retry-After": "3600"}), httpx.Response(200)])
    with _client(lambda r: next(calls)) as c:
        h = Http(c, attempts=2, wait_max=0.01, pace={}, retry_after_max=10)
        h.get("https://api.energy-charts.info/price")
    assert sleeps == [10.0]
