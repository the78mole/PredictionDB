from typing import Any

import numpy as np
import pytest

from gribkit import BBox, GribError, crop, describe, iter_messages
from gribkit.testing import make_global_message


def _decode(message: bytes) -> tuple[Any, Any, Any, dict[str, Any]]:
    import eccodes as ec

    h = ec.codes_new_from_message(message)
    try:
        ni, nj = ec.codes_get(h, "Ni"), ec.codes_get(h, "Nj")
        keys = {
            k: ec.codes_get(h, k)
            for k in (
                "latitudeOfFirstGridPointInDegrees",
                "latitudeOfLastGridPointInDegrees",
                "longitudeOfFirstGridPointInDegrees",
                "longitudeOfLastGridPointInDegrees",
            )
        }
        lats = ec.codes_get_array(h, "latitudes").reshape(nj, ni)
        lons = ec.codes_get_array(h, "longitudes").reshape(nj, ni)
        return ec.codes_get_values(h).reshape(nj, ni), lats, lons, keys
    finally:
        ec.codes_release(h)


def test_iter_messages_splits_and_validates(global_message: tuple[bytes, Any, Any]) -> None:
    """@relation(SWR-GRIB-005, scope=function)"""
    msg, _, _ = global_message
    assert list(iter_messages(msg + msg)) == [msg, msg]
    with pytest.raises(GribError, match="abgeschnitten"):
        list(iter_messages(msg + msg[:-10]))
    with pytest.raises(GribError, match="GRIB-Kennung"):
        list(iter_messages(b"<html>" + msg))


def test_crop_europe_box(global_message: tuple[bytes, Any, Any]) -> None:
    """@relation(SWR-GRIB-006, scope=function)"""
    msg, _, _ = global_message
    bbox = BBox(north=58, south=44, west=0, east=20)
    out = list(iter_messages(crop(msg + msg, bbox)))
    assert len(out) == 2
    values, lats, lons, keys = _decode(out[0])
    assert values.shape == (15, 21)
    assert keys["latitudeOfFirstGridPointInDegrees"] == 58
    assert keys["latitudeOfLastGridPointInDegrees"] == 44
    assert keys["longitudeOfLastGridPointInDegrees"] == 20
    np.testing.assert_allclose(values, lats * 1000 + lons, atol=0.01)
    info = describe(out[0])
    assert (info.short_name, info.ni, info.nj) == ("2t", 21, 15)
    assert len(out[0]) < len(msg) / 50


def test_crop_across_prime_meridian() -> None:
    """@relation(SWR-GRIB-006, scope=function)"""
    msg, _, _ = make_global_message(values_fn=lambda lat, lon: lat * 1000 + lon)
    out = crop(msg, BBox(north=50, south=45, west=-5, east=5))
    values, _lats, lons, keys = _decode(out)
    assert values.shape == (6, 11)
    assert keys["longitudeOfFirstGridPointInDegrees"] == 355
    assert keys["longitudeOfLastGridPointInDegrees"] == 5
    # Werte wurden mit Quell-Länge 355..359, 0..5 erzeugt
    expected_lon = np.array([355, 356, 357, 358, 359, 0, 1, 2, 3, 4, 5])
    np.testing.assert_allclose(values[0], 50 * 1000 + expected_lon, atol=0.01)
    np.testing.assert_allclose(np.mod(lons[0], 360), expected_lon)


def test_crop_south_to_north_input_is_flipped() -> None:
    """@relation(SWR-GRIB-006, scope=function)"""
    msg, _, _ = make_global_message(j_scans_positively=True)
    out = crop(msg, BBox(north=58, south=44, west=0, east=20))
    values, lats, lons, keys = _decode(out)
    assert keys["latitudeOfFirstGridPointInDegrees"] == 58
    assert keys["latitudeOfLastGridPointInDegrees"] == 44
    np.testing.assert_allclose(values, lats * 1000 + lons, atol=0.01)
    np.testing.assert_allclose(lats[:, 0], np.arange(58, 43, -1))


def test_crop_repacks_lossless_with_ccsds() -> None:
    """@relation(SWR-GRIB-007, scope=function)"""
    import eccodes as ec

    msg, _, _ = make_global_message()
    bbox = BBox(north=58, south=44, west=0, east=20)
    plain, ccsds = crop(msg, bbox), crop(msg, bbox, packing="grid_ccsds")
    h = ec.codes_new_from_message(ccsds)
    try:
        assert ec.codes_get(h, "packingType") == "grid_ccsds"
    finally:
        ec.codes_release(h)
    np.testing.assert_array_equal(_decode(ccsds)[0], _decode(plain)[0])


def test_crop_quarter_degree_grid_point_count() -> None:
    """@relation(SWR-GRIB-006, scope=function)"""
    msg, _, _ = make_global_message(inc=0.25, values_fn=lambda lat, lon: lat + 0 * lon)
    values, *_ = _decode(crop(msg, BBox(north=58, south=44, west=0, east=20)))
    assert values.shape == (57, 81)  # 4 617 Punkte, wie in ARCHITEKTUR.md 4.3


def test_crop_empty_box_raises(global_message: tuple[bytes, Any, Any]) -> None:
    """@relation(SWR-GRIB-006, scope=function)"""
    msg, _, _ = global_message
    with pytest.raises(GribError):
        crop(msg, BBox(north=45.5, south=45.2, west=1.2, east=1.4))


def test_bbox_validation() -> None:
    """@relation(SWR-GRIB-006, scope=function)"""
    with pytest.raises(ValueError):
        BBox(north=40, south=50, west=0, east=10)
    with pytest.raises(ValueError):
        BBox(north=50, south=40, west=10, east=0)
