import dataclasses

import numpy as np
import pytest
from obstore.store import MemoryStore

from wx_process.decode import decode_step
from wx_process.silver import Silver, assemble, decode, decode_array, encode
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


def test_read_uses_stored_encoding(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ein Store mit älterer Kodierung bleibt lesbar, wenn sich die Tabelle ändert.

    @relation(SWR-WXP-025, scope=function)
    """
    ds = _dataset([0, 1, 2, 3])
    silver = Silver(MemoryStore())
    silver.write(ds)
    expected = ds.quantized().data
    # neue Kodierung für t2m (anderer Schritt und Offset) und tp (float16)
    t2m, tp = BY_NAME["t2m"], BY_NAME["tp"]
    monkeypatch.setitem(BY_NAME, "t2m", dataclasses.replace(t2m, scale=0.02, offset=280.0))
    monkeypatch.setitem(BY_NAME, "tp", dataclasses.replace(tp, dtype="float16"))
    back = silver.read("gfs", "0p25", RUN)
    for name in ("t2m", "tp", "dswrf"):
        np.testing.assert_array_equal(back.data[name], expected[name])


def test_float_dtype_roundtrip(monkeypatch: pytest.MonkeyPatch) -> None:
    """Variablen mit Gleitkomma-Ablage: float16 im Store, NaN bleibt NaN, kein inf.

    @relation(SWR-WXP-020, SWR-WXP-025, scope=function)
    """
    monkeypatch.setitem(BY_NAME, "t2m", dataclasses.replace(BY_NAME["t2m"], dtype="float16"))
    ds = _dataset([0, 1, 2])
    ds.data["t2m"][1, 0, 0] = np.nan
    ds.data["t2m"][2, 0, 0] = 1e6
    silver = Silver(MemoryStore())
    key = silver.write(ds)
    import zarr

    arr = zarr.open_group(silver.store, path=key, mode="r")["t2m"]
    assert isinstance(arr, zarr.Array) and arr.dtype == np.float16
    assert "scale_factor" not in arr.attrs
    back = silver.read("gfs", "0p25", RUN).data["t2m"]
    assert np.isnan(back[1, 0, 0])
    assert back[2, 0, 0] == np.finfo(np.float16).max
    ok = np.isfinite(ds.data["t2m"]) & (ds.data["t2m"] < 1e5)
    np.testing.assert_allclose(back[ok], ds.data["t2m"][ok], rtol=2**-11)
    np.testing.assert_array_equal(back, ds.quantized().data["t2m"])


def test_decode_array_defaults() -> None:
    """Ganzzahlen ohne Skalierung und Gleitkommawerte ohne Attribute.

    @relation(SWR-WXP-025, scope=function)
    """
    raw = np.array([1, -32768, 3], np.int16)
    np.testing.assert_array_equal(decode_array(raw, {}), [1.0, -32768.0, 3.0])
    out = decode_array(raw, {"scale_factor": 0.5, "add_offset": 10.0, "_FillValue": -32768})
    assert out[0] == 10.5 and np.isnan(out[1])
    f = np.array([1.5, np.nan], np.float16)
    assert decode_array(f, {"scale_factor": 99}).dtype == np.float32
