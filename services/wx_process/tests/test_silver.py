import numpy as np
import pytest
from obstore.store import MemoryStore

from wx_process.decode import decode_step
from wx_process.silver import Silver, assemble, decode, encode
from wx_process.testing import RUN, grid, make_step
from wx_process.variables import BY_NAME, SILVER_VERSION

LAT, LON = grid()


def _dataset(steps: list[int]):  # type: ignore[no-untyped-def]
    decoded = {s: decode_step(make_step(s, LAT, LON), s) for s in steps}
    return assemble("gfs", "0p25", RUN, decoded, {"bronze_completed_at": "x"})


def test_encode_roundtrip_within_resolution() -> None:
    """@relation(SWR-WXP-020, scope=function)"""
    for name, var in BY_NAME.items():
        lin = np.linspace(var.offset - 100 * var.scale, var.offset + 100 * var.scale, 50)
        values = lin.astype(np.float32)
        values[3] = np.nan
        back = decode(encode(values, name), name)
        assert np.isnan(back[3])
        assert np.nanmax(np.abs(back - values)) <= var.scale / 2 + 1e-3 * var.scale + 1e-6


def test_encode_clips_out_of_range() -> None:
    """@relation(SWR-WXP-020, scope=function)"""
    var = BY_NAME["t2m"]
    back = decode(encode(np.array([1e6], np.float32), "t2m"), "t2m")
    assert back[0] == pytest.approx(var.offset + 32767 * var.scale, abs=1e-3)


def test_write_read_roundtrip() -> None:
    """@relation(SWR-WXP-020, scope=function)"""
    ds = _dataset([0, 1, 2, 3, 6])
    silver = Silver(MemoryStore(), step_chunk=2)
    assert silver.attrs("gfs", "0p25", RUN) is None
    key = silver.write(ds)
    assert key == "gfs/0p25/2099/01/02/06.zarr"
    attrs = silver.attrs("gfs", "0p25", RUN)
    assert attrs is not None and attrs["complete"] and attrs["silver_version"] == SILVER_VERSION
    assert attrs["bronze_completed_at"] == "x"

    back = silver.read("gfs", "0p25", RUN)
    assert back.steps.tolist() == [0, 1, 2, 3, 6]
    assert back.grid.same_as(ds.grid)
    assert set(back.data) == set(ds.data)
    q = ds.quantized()
    for name in ds.data:
        np.testing.assert_array_equal(back.data[name], q.data[name])
    assert np.isnan(back.data["dswrf"][0]).all()
    assert back.valid_times[-1].hour == 12


def test_overwrite_replaces_store() -> None:
    """@relation(SWR-WXP-020, scope=function)"""
    silver = Silver(MemoryStore())
    silver.write(_dataset([0, 1, 2]))
    silver.write(_dataset([0, 1]))
    assert silver.read("gfs", "0p25", RUN).steps.tolist() == [0, 1]
