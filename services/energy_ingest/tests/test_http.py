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


def test_pace_grows_after_429_and_relaxes(monkeypatch: pytest.MonkeyPatch) -> None:
    """@relation(SWR-ENI-032, scope=function)"""
    monkeypatch.setattr("tenacity.nap.time.sleep", lambda s: None)
    monkeypatch.setattr("energy_ingest.http.time.sleep", lambda s: None)
    host = "api.energy-charts.info"
    calls = iter([httpx.Response(429), httpx.Response(429), httpx.Response(200)])
    with _client(lambda r: next(calls)) as c:
        h = Http(c, attempts=3, wait_max=0.01, pace={host: 2.0}, pace_max=6.0)
        assert h.get(f"https://{host}/price").status_code == 200
        # 2 → 4 → 6 (Grenze), danach ein Erfolg: 6 · 0,95
        assert h.current_pace[host] == pytest.approx(5.7)
    with _client(lambda r: httpx.Response(200)) as c:
        h.client = c
        for _ in range(100):
            h.get(f"https://{host}/price")
    assert h.current_pace[host] == pytest.approx(2.0)


def test_pace_never_below_base_and_follows_redirect(monkeypatch: pytest.MonkeyPatch) -> None:
    """@relation(SWR-ENI-032, scope=function)"""
    monkeypatch.setattr("tenacity.nap.time.sleep", lambda s: None)
    monkeypatch.setattr("energy_ingest.http.time.sleep", lambda s: None)
    slow, other = "agsi.gie.eu", "cdn.example.org"

    def handler(r: httpx.Request) -> httpx.Response:
        if r.url.host == slow:
            return httpx.Response(429)
        return httpx.Response(429 if r.url.path == "/limit" else 200)

    with httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True) as c:
        h = Http(c, attempts=1, pace={slow: 40.0}, pace_max=30.0)
        with pytest.raises(httpx.HTTPStatusError):
            h.get(f"https://{slow}/x")
        assert h.current_pace[slow] == 40.0

    def redirect(r: httpx.Request) -> httpx.Response:
        if r.url.host == "api.energy-charts.info":
            return httpx.Response(302, headers={"Location": f"https://{other}/limit"})
        return httpx.Response(429)

    with httpx.Client(transport=httpx.MockTransport(redirect), follow_redirects=True) as c:
        h = Http(c, attempts=1, pace={}, pace_max=30.0)
        with pytest.raises(httpx.HTTPStatusError):
            h.get("https://api.energy-charts.info/price")
        assert h.current_pace == {other: 1.0}
