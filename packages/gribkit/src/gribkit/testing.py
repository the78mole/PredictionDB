"""Hilfen für Tests: synthetische GRIB2-Nachrichten mit bekannten Werten.

@relation(SWA-GRIB-TESTING, scope=file)
"""

from __future__ import annotations

from collections.abc import Callable

import numpy as np
import numpy.typing as npt

ValuesFn = Callable[[npt.NDArray[np.float64], npt.NDArray[np.float64]], npt.NDArray[np.float64]]


def _default_values(
    lat: npt.NDArray[np.float64], lon: npt.NDArray[np.float64]
) -> npt.NDArray[np.float64]:
    return lat * 1000 + lon


def make_global_message(
    inc: float = 1.0,
    values_fn: ValuesFn = _default_values,
    j_scans_positively: bool = False,
    short_name: str = "2t",
) -> tuple[bytes, np.ndarray, np.ndarray]:
    """Erzeugt eine globale regular_ll-GRIB2-Nachricht mit bekannten Werten.

    Gibt (Nachricht, Breiten, Längen) zurück; Werte = values_fn(lat, lon).
    """
    import eccodes as ec

    ni, nj = round(360 / inc), round(180 / inc) + 1
    lats = np.linspace(-90, 90, nj) if j_scans_positively else np.linspace(90, -90, nj)
    lons = np.arange(ni) * inc
    h = ec.codes_grib_new_from_samples("regular_ll_sfc_grib2")
    try:
        ec.codes_set(h, "Ni", ni)
        ec.codes_set(h, "Nj", nj)
        ec.codes_set(h, "jScansPositively", int(j_scans_positively))
        ec.codes_set(h, "iDirectionIncrementInDegrees", inc)
        ec.codes_set(h, "jDirectionIncrementInDegrees", inc)
        ec.codes_set(h, "latitudeOfFirstGridPointInDegrees", float(lats[0]))
        ec.codes_set(h, "latitudeOfLastGridPointInDegrees", float(lats[-1]))
        ec.codes_set(h, "longitudeOfFirstGridPointInDegrees", 0.0)
        ec.codes_set(h, "longitudeOfLastGridPointInDegrees", float(lons[-1]))
        ec.codes_set(h, "shortName", short_name)
        ec.codes_set(h, "bitsPerValue", 24)
        lat2d, lon2d = np.meshgrid(lats, lons, indexing="ij")
        ec.codes_set_values(h, values_fn(lat2d, lon2d).ravel().astype(float))
        return ec.codes_get_message(h), lats, lons
    finally:
        ec.codes_release(h)
