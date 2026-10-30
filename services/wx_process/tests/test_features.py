from datetime import date

import numpy as np
import pytest

from wx_process.config import ProcessConfig
from wx_process.decode import Grid
from wx_process.features import (
    ClusterWeights,
    FeatureBuilder,
    PowerCurve,
    PvModel,
    TechnologyFeatures,
    cell_index,
    shear_exponent,
)
from wx_process.silver import Dataset
from wx_process.store import CapacityTable
from wx_process.testing import RUN, grid

LAT, LON = grid()


def _ds(**fields: float | np.ndarray) -> Dataset:
    shape = (3, len(LAT), len(LON))
    data = {k: np.broadcast_to(np.asarray(v, np.float32), shape).copy() for k, v in fields.items()}
    return Dataset("gfs", "0p25", RUN, np.array([0, 1, 2]), Grid(LAT, LON), data)


def _cluster(tech: str, cells: list[tuple[float, float, float]], hub: float | None = None):  # type: ignore[no-untyped-def]
    a = np.array(cells, float)
    return ClusterWeights(tech, 0, 7, a[:, 0], a[:, 1], a[:, 2], hub)


def test_cell_index_and_coverage() -> None:
    """@relation(SWR-WXP-030, scope=function)"""
    ds = _ds(t2m=280.0)
    idx, ok = cell_index(
        ds, np.array([58.0, 44.0, 50.0, 60.0, 50.5]), np.array([0, 20, 10, 5, 360.0])
    )
    assert ok.tolist() == [True, True, True, False, False]  # 50,5/0 liegt nicht auf dem 1°-Gitter
    assert idx[:3].tolist() == [0, 14 * 21 + 20, 8 * 21 + 10]
    _, ok = cell_index(ds, np.array([50.0]), np.array([370.0]))  # 370° = 10°
    assert ok.tolist() == [True]


def test_weighted_mean_and_coverage() -> None:
    """@relation(SWR-WXP-030, scope=function)"""
    field = np.zeros((len(LAT), len(LON)), np.float32)
    field[LAT.tolist().index(54.0), 9] = 10 + 273.15
    field[LAT.tolist().index(48.0), 11] = 20 + 273.15
    ds = _ds(t2m=field)
    b = FeatureBuilder({"biomass": TechnologyFeatures(kind="other")})
    c = _cluster("biomass", [(54, 9, 0.25), (48, 11, 0.5), (70, 9, 0.25)])
    series, coverage = b.build(ds, [c])
    assert coverage == {"biomass:c00": pytest.approx(0.75)}
    (t2m,) = series
    assert t2m.code == "t2m" and t2m.unit == "degC" and t2m.region_id == 7
    np.testing.assert_allclose(t2m.values, (10 * 0.25 + 20 * 0.5) / 0.75, atol=1e-4)


def test_unknown_technology_skipped() -> None:
    """@relation(SWR-WXP-030, scope=function)"""
    series, coverage = FeatureBuilder({}).build(
        _ds(t2m=280.0), [_cluster("wind_on", [(50, 10, 1)])]
    )
    assert series == [] and coverage == {}


def test_power_curve() -> None:
    """@relation(SWR-WXP-031, scope=function)"""
    pc = PowerCurve(cut_in=3, rated=12, cut_out=25)
    v = np.array([0, 2.9, 3, 7.5, 12, 20, 25, 30, np.nan])
    cf = pc(v)
    assert cf[:3].tolist() == [0, 0, 0]
    assert cf[3] == pytest.approx((7.5**3 - 27) / (12**3 - 27))
    assert cf[4:6].tolist() == [1, 1] and cf[6:8].tolist() == [0, 0] and np.isnan(cf[8])


def test_shear_exponent() -> None:
    """@relation(SWR-WXP-031, scope=function)"""
    a = shear_exponent(np.array([5.0, 0.1, 5.0, 5.0]), np.array([5 * 10**0.2, 3.0, 4.0, 5e3]))
    assert a[0] == pytest.approx(0.2) and a[1] == pytest.approx(1 / 7)
    assert a[2] == 0 and a[3] == 0.6


def test_wind_features_with_capacity() -> None:
    # 10 m: 3/4 → 5 m/s, 100 m: 6/8 → 10 m/s ⇒ Exponent log10(2), Nabenhöhe 150 m
    """@relation(SWR-WXP-031, SWR-WXP-034, scope=function)"""
    ds = _ds(u10=3.0, v10=4.0, u100=6.0, v100=8.0, gust=11.0, t2m=283.15)
    tech = TechnologyFeatures(kind="wind", power_curve=PowerCurve(cut_in=3, rated=13, cut_out=25))
    b = FeatureBuilder({"wind_on": tech})
    cap = np.array([100.0, 100.0, 120.0])
    series, _ = b.build(ds, [_cluster("wind_on", [(50, 10, 1.0)], hub=150)], lambda t, n: cap)
    f = {s.code: s.values for s in series}
    assert set(f) == {
        "t2m", "wind_speed_10m", "wind_speed_100m", "wind_speed_hub", "gust", "wind_cf",
        "wind_power",
    }  # fmt: skip
    hub = 10 * 1.5 ** np.log10(2)
    np.testing.assert_allclose(f["wind_speed_hub"], hub, rtol=1e-5)
    cf = (hub**3 - 27) / (13**3 - 27)
    np.testing.assert_allclose(f["wind_cf"], cf, rtol=1e-5)
    np.testing.assert_allclose(f["wind_power"], cf * cap, rtol=1e-5)
    np.testing.assert_allclose(f["t2m"], 10.0, atol=1e-4)


def test_wind_default_hub_height() -> None:
    """@relation(SWR-WXP-031, scope=function)"""
    ds = _ds(u10=3.0, v10=4.0, u100=6.0, v100=8.0)
    b = FeatureBuilder({"wind_off": TechnologyFeatures(kind="wind", hub_height_m=100)})
    series, _ = b.build(ds, [_cluster("wind_off", [(54, 6, 1.0)])])
    f = {s.code: s.values for s in series}
    np.testing.assert_allclose(f["wind_speed_hub"], 10.0, rtol=1e-5)
    assert "wind_power" not in f


def test_pv_features() -> None:
    """@relation(SWR-WXP-032, scope=function)"""
    ghi = np.zeros((3, len(LAT), len(LON)), np.float32)
    ghi[0] = np.nan  # f000 ohne Strahlung
    ghi[1] = 800.0
    ghi[2] = 200.0
    ds = _ds(dswrf=ghi, t2m=293.15, tcc=55.0, csnow_mean=0.0)
    pv = PvModel(performance_ratio=0.8, temp_coeff=-0.004, noct_c=45)
    b = FeatureBuilder({"pv_roof": TechnologyFeatures(kind="pv")}, pv)
    series, _ = b.build(ds, [_cluster("pv_roof", [(48, 11, 1.0)])], lambda t, n: np.full(3, 50.0))
    f = {s.code: s.values for s in series}
    assert np.isnan(f["ghi"][0]) and np.isnan(f["pv_cf"][0])
    t_cell = 20 + 800 * 25 / 800
    assert f["pv_cf"][1] == pytest.approx(0.8 * 0.8 * (1 - 0.004 * (t_cell - 25)), rel=1e-5)
    assert f["pv_power"][1] == pytest.approx(50 * f["pv_cf"][1])
    assert f["cloud_cover"][2] == pytest.approx(55.0)
    assert f["snow_fraction"][1] == 0


def test_hydro_features() -> None:
    """@relation(SWR-WXP-033, scope=function)"""
    b = FeatureBuilder({"hydro": TechnologyFeatures(kind="hydro")})
    series, _ = b.build(
        _ds(tp=0.5, t2m=273.15, csnow_mean=0.25), [_cluster("hydro", [(47, 11, 1)])]
    )
    f = {s.code: s.values for s in series}
    assert f["precip"][0] == pytest.approx(0.5) and f["snow_fraction"][0] == pytest.approx(0.25)


def test_capacity_table_forward_fill() -> None:
    """@relation(SWR-WXP-034, scope=function)"""
    t = CapacityTable(
        [("wind_on", 0, date(2099, 1, 1), 10.0), ("wind_on", 0, date(2099, 1, 3), 12.0)]
    )
    days = [date(2098, 12, 31), date(2099, 1, 1), date(2099, 1, 2), date(2099, 1, 5)]
    out = t.at("wind_on", 0, days)
    assert out is not None and np.isnan(out[0]) and out[1:].tolist() == [10, 10, 12]
    assert t.at("pv_roof", 0, days) is None


def test_repo_config_matches_mastr() -> None:
    """@relation(SWR-WXP-001, scope=function)"""
    from mastr_ingest.config import MastrConfig

    config = ProcessConfig.from_yaml("config/process/gfs_0p25.yaml")
    mastr = MastrConfig.from_yaml("config/sources/mastr.yaml")
    assert set(config.technologies) == set(mastr.technologies)
    assert config.weight_grid in {g.name for g in mastr.weight_grids}
