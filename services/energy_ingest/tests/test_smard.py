from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import httpx

from energy_ingest.http import Http
from energy_ingest.sources.smard import Smard

Q = datetime(2025, 9, 28, 22, 0, tzinfo=UTC)  # erster Viertelstundenblock (Mo 00:00 MESZ)
H_PREV = Q - timedelta(days=7)


def ms(ts: datetime) -> int:
    return int(ts.timestamp() * 1000)


def handler(seen: list[str]) -> Callable[[httpx.Request], httpx.Response]:
    def h(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        seen.append(path)
        if path.endswith("/410/DE/index_quarterhour.json"):
            return httpx.Response(200, json={"timestamps": [ms(Q)]})
        if path.endswith("/410/DE/index_hour.json"):
            return httpx.Response(200, json={"timestamps": [ms(H_PREV), ms(Q)]})
        if path.endswith(f"410_DE_quarterhour_{ms(Q)}.json"):
            series: list[list[float | None]] = [
                [ms(Q + i * timedelta(minutes=15)), 1000.0] for i in range(4)
            ]
            series.append([ms(Q + timedelta(hours=1)), None])
            return httpx.Response(200, json={"meta_data": {}, "series": series})
        if path.endswith(f"410_DE_hour_{ms(H_PREV)}.json"):
            series = [[ms(Q - timedelta(hours=i)), 50000.0] for i in range(3, 0, -1)]
            return httpx.Response(200, json={"meta_data": {}, "series": series})
        if path.endswith(f"410_DE_hour_{ms(Q)}.json"):
            raise AssertionError("Stundenblock trotz Viertelstundendaten abgerufen")
        if "4169" in path and "index" in path:
            return httpx.Response(404)
        return httpx.Response(404)

    return h


def test_quarterhour_with_hourly_fallback(make_http: Callable[..., Http]) -> None:
    """@relation(SWR-ENI-040, scope=function)"""
    seen: list[str] = []
    smard = Smard(make_http(handler(seen)))
    out = smard.fetch("load_actual", Q - timedelta(hours=2), Q + timedelta(hours=2))
    assert len(out) == 1
    data = out[0]
    assert data.code == "load_actual"
    assert data.resolution == timedelta(minutes=15)
    values = dict(data.points)
    # Viertelstunde: 1000 MWh je 15 min = 4000 MW
    assert values[Q] == 4000.0
    assert values[Q + timedelta(minutes=45)] == 4000.0
    # davor Stundenwerte: MWh je Stunde = MW
    assert values[Q - timedelta(hours=1)] == 50000.0
    assert values[Q - timedelta(hours=2)] == 50000.0
    assert Q - timedelta(hours=3) not in values  # außerhalb des Fensters
    assert Q + timedelta(hours=1) not in values  # null-Wert
    # Index wird zwischengespeichert
    smard.fetch("load_actual", Q, Q + timedelta(hours=1))
    assert sum(p.endswith("index_quarterhour.json") for p in seen) == 1


def test_price_is_not_scaled(make_http: Callable[..., Http]) -> None:
    """@relation(SWR-ENI-040, SWR-ENI-020, scope=function)"""

    def h(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path.endswith("/4169/DE-LU/index_quarterhour.json"):
            return httpx.Response(200, json={"timestamps": [ms(Q)]})
        if path.endswith("/4169/DE-LU/index_hour.json"):
            return httpx.Response(200, json={"timestamps": []})
        if path.endswith(f"4169_DE-LU_quarterhour_{ms(Q)}.json"):
            return httpx.Response(200, json={"series": [[ms(Q), 87.5], [ms(Q) + 900_000, -3.1]]})
        return httpx.Response(404)

    out = Smard(make_http(h)).fetch("da_price_de_lu", Q, Q + timedelta(hours=1))
    assert out[0].points == [(Q, 87.5), (Q + timedelta(minutes=15), -3.1)]
    assert out[0].info.unit == "EUR/MWh"


def test_empty_index(make_http: Callable[..., Http]) -> None:
    """@relation(SWR-ENI-040, scope=function)"""
    out = Smard(make_http(lambda r: httpx.Response(404))).fetch("gen_solar", Q, Q + timedelta(1))
    assert out == []
