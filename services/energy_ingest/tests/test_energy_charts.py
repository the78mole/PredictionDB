from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from energy_ingest.http import Http
from energy_ingest.sources import FormatError
from energy_ingest.sources.energy_charts import EnergyCharts

T0 = datetime(2026, 10, 18, 22, 0, tzinfo=UTC)
STAMPS = [int((T0 + i * timedelta(minutes=15)).timestamp()) for i in range(4)]
LICENSE = "CC BY 4.0 (creativecommons.org/licenses/by/4.0) from Bundesnetzagentur | SMARD.de"


def handler(req: httpx.Request) -> httpx.Response:
    p = req.url.path
    if p == "/price":
        assert req.url.params["bzn"] in ("DE-LU", "IT-North")
        assert req.url.params["start"] == "2026-10-18T22:00+00:00"
        return httpx.Response(
            200,
            json={
                "license_info": LICENSE,
                "unix_seconds": STAMPS,
                "price": [90.1, 88.0, None, 85.5],
                "unit": "EUR / MWh",
            },
        )
    if p == "/public_power":
        return httpx.Response(
            200,
            json={
                "unix_seconds": STAMPS,
                "production_types": [
                    {"name": "Wind onshore", "data": [1.0, 2.0, 3.0, 4.0]},
                    {"name": "Load", "data": [50.0, 51.0, 52.0, None]},
                    {"name": "Renewable share of load", "data": [10, 10, 10, 10]},
                    {"name": "Hydro pumped storage consumption", "data": [-1, -1, -1, -1]},
                ],
            },
        )
    if p == "/public_power_forecast":
        if req.url.params["production_type"] == "solar":
            return httpx.Response(
                200, json={"unix_seconds": STAMPS, "forecast_values": [0, 0, 1, 2]}
            )
        return httpx.Response(404, json={"detail": "no data"})
    return httpx.Response(500)


def test_price(make_http: Callable[..., Http]) -> None:
    """@relation(SWR-ENI-041, scope=function)"""
    ec = EnergyCharts(make_http(handler))
    (data,) = ec.fetch("price", T0, T0 + timedelta(hours=1))
    assert data.code == "da_price_de_lu"
    assert data.resolution == timedelta(minutes=15)
    assert [v for _, v in data.points] == [90.1, 88.0, 85.5]


def test_neighbour_price(make_http: Callable[..., Http]) -> None:
    """@relation(SWR-ENI-041, scope=function)"""
    ec = EnergyCharts(make_http(handler))
    assert "price_it_north" in ec.datasets()
    (data,) = ec.fetch("price_it_north", T0, T0 + timedelta(hours=1))
    assert data.code == "da_price_it_north"
    assert len(data.points) == 3


def test_price_without_free_license_is_rejected(make_http: Callable[..., Http]) -> None:
    """@relation(SWR-ENI-041, SWR-ENI-003, scope=function)"""

    def restricted(req: httpx.Request) -> httpx.Response:
        body = {"license_info": "private and internal use only", "unix_seconds": STAMPS}
        return httpx.Response(200, json={**body, "price": [1.0, 2.0, 3.0, 4.0]})

    with pytest.raises(FormatError, match="ohne freie Lizenz"):
        EnergyCharts(make_http(restricted)).fetch("price", T0, T0 + timedelta(hours=1))


def test_public_power_maps_names(make_http: Callable[..., Http]) -> None:
    """@relation(SWR-ENI-041, scope=function)"""
    ec = EnergyCharts(make_http(handler))
    out = {d.code: d for d in ec.fetch("public_power", T0, T0 + timedelta(minutes=30))}
    assert set(out) == {"gen_wind_onshore", "load_actual"}
    assert out["gen_wind_onshore"].points == [(T0, 1.0), (T0 + timedelta(minutes=15), 2.0)]


def test_forecast_and_missing(make_http: Callable[..., Http]) -> None:
    """@relation(SWR-ENI-041, scope=function)"""
    ec = EnergyCharts(make_http(handler))
    (solar,) = ec.fetch("forecast_solar", T0, T0 + timedelta(hours=1))
    assert solar.code == "gen_da_forecast_solar"
    assert ec.fetch("forecast_load", T0, T0 + timedelta(hours=1)) == []


def cross_border(req: httpx.Request) -> httpx.Response:
    assert req.url.path in ("/cbet", "/cbpf")
    assert req.url.params["country"] == "de"
    return httpx.Response(
        200,
        json={
            "unix_seconds": STAMPS,
            "countries": [
                {"name": "France", "data": [-1.078, -0.752, None, 0.5]},
                {"name": "Czech Republic", "data": [1.0, 1.0, 1.0, 1.0]},
                {"name": "Atlantis", "data": [9, 9, 9, 9]},
                {"name": "sum", "data": [-1.225, -0.608, -0.247, 0.0]},
            ],
            "deprecated": False,
        },
    )


@pytest.mark.parametrize(("dataset", "prefix"), [("cbet", "xb_trade_"), ("cbpf", "xb_flow_")])
def test_cross_border(make_http: Callable[..., Http], dataset: str, prefix: str) -> None:
    """@relation(SWR-ENI-041, scope=function)"""
    ec = EnergyCharts(make_http(cross_border))
    assert dataset in ec.datasets()
    out = {d.code: d for d in ec.fetch(dataset, T0, T0 + timedelta(hours=1))}
    # unbekannte Partner werden übergangen, GW werden zu MW
    assert set(out) == {prefix + "fr", prefix + "cz", prefix + "net"}
    assert [v for _, v in out[prefix + "fr"].points] == pytest.approx([-1078, -752, 500])
    assert [v for _, v in out[prefix + "net"].points] == pytest.approx([-1225, -608, -247, 0])
    assert out[prefix + "net"].resolution == timedelta(minutes=15)
