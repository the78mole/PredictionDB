from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import httpx

from energy_ingest.http import Http
from energy_ingest.sources.smard import Smard

Q = datetime(2025, 9, 28, 22, 0, tzinfo=UTC)  # erster Viertelstundenblock (Mo 00:00 MESZ)
H_PREV = Q - timedelta(days=7)
QH = timedelta(minutes=15)


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


def test_imbalance_volume_sums_control_areas(make_http: Callable[..., Http]) -> None:
    """Volumen für DE endet 2022; danach Summe der vier Regelzonen.

    @relation(SWR-ENI-040, scope=function)
    """
    zones = {"50Hertz": 10.0, "Amprion": 20.0, "TenneT": 30.0, "TransnetBW": 40.0}

    def h(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        region = path.split("/")[-2]
        if path.endswith("index_quarterhour.json"):
            return httpx.Response(200, json={"timestamps": [ms(Q)]})
        if path.endswith("index_hour.json"):
            return httpx.Response(404)
        if region == "DE":  # DE nur noch für die erste Viertelstunde
            return httpx.Response(200, json={"series": [[ms(Q), 5.0], [ms(Q + QH), None]]})
        if region in zones:
            if region == "TenneT":  # dritte Viertelstunde fehlt in einer Zone
                pts = [[ms(Q + i * QH), zones[region]] for i in range(2)]
            else:
                pts = [[ms(Q + i * QH), zones[region]] for i in range(3)]
            return httpx.Response(200, json={"series": pts})
        return httpx.Response(404)

    out = Smard(make_http(h)).fetch("imbalance_volume_pos", Q, Q + timedelta(hours=1))
    assert len(out) == 1
    values = dict(out[0].points)
    assert values[Q] == 20.0  # DE: 5 MWh je 15 min = 20 MW
    assert values[Q + QH] == 400.0  # Summe 100 MWh je 15 min
    assert Q + 2 * QH not in values  # nicht alle Zonen da


def test_imbalance_price_is_not_rescaled(make_http: Callable[..., Http]) -> None:
    """@relation(SWR-ENI-040, scope=function)"""

    def h(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path.endswith("/4382/DE/index_quarterhour.json"):
            return httpx.Response(200, json={"timestamps": [ms(Q)]})
        if path.endswith(f"4382_DE_quarterhour_{ms(Q)}.json"):
            return httpx.Response(200, json={"series": [[ms(Q), 123.4]]})
        return httpx.Response(404)

    out = Smard(make_http(h)).fetch("imbalance_price", Q, Q + QH)
    assert out[0].points == [(Q, 123.4)]
    assert out[0].info.unit == "EUR/MWh"
