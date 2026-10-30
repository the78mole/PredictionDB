import numpy as np
import pytest

from gribkit import GribError
from wx_process.decode import decode_step
from wx_process.silver import assemble
from wx_process.testing import RUN, _message, default_hourly, grid, make_step

LAT, LON = grid()
LAT2, LON2 = np.meshgrid(LAT, LON, indexing="ij")


def _true_mean(name: str, prev: int, step: int) -> np.ndarray:
    return np.mean([default_hourly(name, h, LAT2, LON2) for h in range(prev + 1, step + 1)], axis=0)


def test_decode_step_fields() -> None:
    """@relation(SWR-WXP-010, scope=function)"""
    g, f = decode_step(make_step(7, LAT, LON), 7)
    assert g.shape == (15, 21) and g.lat[0] == 58 and g.lon[-1] == 20
    assert {"t2m", "u100", "tcc", "csnow", "prmsl"} <= set(f.instant)
    assert set(f.stats["dswrf"]) == {6}
    assert set(f.stats["tp"]) == {6, 0}  # Eimer und Gesamtsumme wie bei GFS
    assert f.instant["t2m"][0, 0] == pytest.approx(280.0 + 0.07 + 0.8, abs=1e-3)


def test_analysis_step_has_no_statistics() -> None:
    """@relation(SWR-WXP-011, scope=function)"""
    _, f = decode_step(make_step(0, LAT, LON), 0)
    assert f.stats == {} and "t2m" in f.instant


def test_unknown_messages_ignored_and_step_checked() -> None:
    """@relation(SWR-WXP-010, scope=function)"""
    other = _message((0, 0, 0, 1, 0, "instant"), LAT, LON, np.zeros((15, 21)), 7, 7)  # TMP:surface
    _, f = decode_step(other + make_step(7, LAT, LON), 7)
    assert f.instant["t2m"].shape == (15, 21)
    with pytest.raises(GribError, match="Schritt 7"):
        decode_step(make_step(7, LAT, LON), 8)


@pytest.mark.parametrize(
    "steps",
    [
        [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12],  # stündlich über Eimergrenzen
        [0, 3, 6, 9, 12, 15, 18],  # dreistündlich
        [0, 1, 2, 3, 6, 9, 12],  # Wechsel der Schrittweite innerhalb eines Eimers
    ],
)
def test_bucket_averages_become_interval_values(steps: list[int]) -> None:
    """@relation(SWR-WXP-011, scope=function)"""
    ds = assemble("gfs", "0p25", RUN, {s: decode_step(make_step(s, LAT, LON), s) for s in steps})
    assert np.isnan(ds.data["dswrf"][0]).all() and np.isnan(ds.data["tp"][0]).all()
    for i in range(1, len(steps)):
        p, s = steps[i - 1], steps[i]
        np.testing.assert_allclose(ds.data["dswrf"][i], _true_mean("dswrf", p, s), atol=1e-3)
        # Niederschlag als Rate je Stunde über das Intervall
        np.testing.assert_allclose(ds.data["tp"][i], _true_mean("tp", p, s), atol=1e-5)
        np.testing.assert_allclose(ds.data["tcc_mean"][i], _true_mean("tcc_mean", p, s), atol=1e-3)
        # Sonnenschein: Eimersumme in Sekunden → Anteil der Stunde
        sun = _true_mean("sunshine", p, s) / 3600
        np.testing.assert_allclose(ds.data["sunshine"][i], sun, atol=1e-5)


def test_gap_without_common_bucket_stays_empty() -> None:
    # 2 → 8: Eimer 0-2 und 6-8 haben keinen gemeinsamen Start; tp geht über die Summe ab 0
    """@relation(SWR-WXP-011, scope=function)"""
    steps = [0, 2, 8]
    ds = assemble("gfs", "0p25", RUN, {s: decode_step(make_step(s, LAT, LON), s) for s in steps})
    assert np.isnan(ds.data["dswrf"][2]).all()
    np.testing.assert_allclose(ds.data["tp"][2], _true_mean("tp", 2, 8), atol=1e-5)


def test_grid_across_greenwich() -> None:
    """@relation(SWR-WXP-010, scope=function)"""
    lat, lon = grid(west=-5, east=5)
    g, _ = decode_step(make_step(1, lat, lon), 1)
    assert g.lon[0] == -5 and g.lon[-1] == 5 and np.all(np.diff(g.lon) > 0)


def test_soil_layers_are_distinct() -> None:
    """@relation(SWR-WXP-010, scope=function)"""
    _, f = decode_step(make_step(3, LAT, LON), 3)
    assert f.instant["tsoil_0_10"][0, 0] == pytest.approx(283.0 + 0.03 + 0.8, abs=1e-3)
    assert f.instant["tsoil_10_40"][0, 0] == pytest.approx(284.0 + 0.03 + 0.8, abs=1e-3)
    assert {"soilw_0_10", "soilw_10_40", "sp", "sunshine", "weasd", "snod"} <= set(f.instant)
    assert set(f.stats["dlwrf"]) == {0}


def test_runs_without_new_fields() -> None:
    # Läufe vor der Erweiterung der Quelle: neue Variablen fehlen in Silber,
    # Mischung innerhalb eines Laufs wird mit NaN aufgefüllt
    """@relation(SWR-WXP-012, scope=function)"""
    old = {"t2m", "dswrf", "tp"}
    ds = assemble(
        "gfs",
        "0p25",
        RUN,
        {s: decode_step(make_step(s, LAT, LON, names=old), s) for s in (0, 1, 2)},
    )
    assert set(ds.data) == old
    mixed = {0: decode_step(make_step(0, LAT, LON, names=old), 0)}
    mixed |= {s: decode_step(make_step(s, LAT, LON), s) for s in (1, 2)}
    ds = assemble("gfs", "0p25", RUN, mixed)
    assert np.isnan(ds.data["sp"][0]).all() and not np.isnan(ds.data["sp"][1:]).any()
    assert np.isnan(ds.data["dlwrf"][0]).all() and not np.isnan(ds.data["dlwrf"][1:]).any()
