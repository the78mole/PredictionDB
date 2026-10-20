"""GRIB2-Nachrichten trennen und auf einen geografischen Ausschnitt zuschneiden.

Dekodieren und Neucodieren übernimmt ecCodes. Die Packungsart der Quelle
(bei GFS ``grid_complex_spatial_differencing``) bleibt erhalten, ebenso die
Genauigkeit über den Dezimal-Skalierungsfaktor.
"""

from __future__ import annotations

import struct
from collections.abc import Iterator
from dataclasses import dataclass

import numpy as np

from gribkit.bbox import BBox

_EPS = 1e-6


class GribError(ValueError):
    pass


def iter_messages(data: bytes) -> Iterator[bytes]:
    """Zerlegt einen Bytestrom in einzelne GRIB2-Nachrichten.

    Prüft Kennung ``GRIB``, Edition 2 und Endkennung ``7777``. Bytes zwischen
    den Nachrichten sind nicht erlaubt; so fallen abgeschnittene Downloads auf.
    """
    pos = 0
    n = len(data)
    while pos < n:
        if data[pos : pos + 4] != b"GRIB":
            raise GribError(f"Keine GRIB-Kennung an Byte {pos}")
        if n - pos < 16:
            raise GribError(f"Abgeschnittener Kopf an Byte {pos}")
        edition = data[pos + 7]
        if edition != 2:
            raise GribError(f"Nur GRIB2 unterstützt, Edition {edition} an Byte {pos}")
        (length,) = struct.unpack(">Q", data[pos + 8 : pos + 16])
        end = pos + length
        if end > n:
            raise GribError(f"Nachricht an Byte {pos} abgeschnitten ({n - pos} von {length} Bytes)")
        if data[end - 4 : end] != b"7777":
            raise GribError(f"Endkennung 7777 fehlt in Nachricht an Byte {pos}")
        yield data[pos:end]
        pos = end


@dataclass(frozen=True, slots=True)
class MessageInfo:
    short_name: str
    type_of_level: str
    level: int
    step_range: str
    ni: int
    nj: int


def describe(message: bytes) -> MessageInfo:
    import eccodes as ec

    h = ec.codes_new_from_message(message)
    try:
        return MessageInfo(
            short_name=ec.codes_get(h, "shortName"),
            type_of_level=ec.codes_get(h, "typeOfLevel"),
            level=ec.codes_get(h, "level"),
            step_range=str(ec.codes_get(h, "stepRange")),
            ni=ec.codes_get(h, "Ni"),
            nj=ec.codes_get(h, "Nj"),
        )
    finally:
        ec.codes_release(h)


def _axis(first: float, inc: float, count: int, positive: bool) -> np.ndarray:
    return first + np.arange(count) * (inc if positive else -inc)


def crop_message(message: bytes, bbox: BBox) -> bytes:
    """Schneidet eine ``regular_ll``-Nachricht auf alle Gitterpunkte innerhalb von ``bbox``.

    Längen werden modulo 360 behandelt, sodass auch Ausschnitte über den
    Nullmeridian (``west < 0`` bei einem 0–360°-Gitter) funktionieren.
    """
    import eccodes as ec

    h = ec.codes_new_from_message(message)
    try:
        if ec.codes_get(h, "gridType") != "regular_ll":
            raise GribError(f"Gittertyp {ec.codes_get(h, 'gridType')!r} wird nicht unterstützt")
        if ec.codes_get(h, "iScansNegatively") or ec.codes_get(h, "jPointsAreConsecutive"):
            raise GribError("Nur Abtastung West→Ost mit zeilenweiser Anordnung unterstützt")
        ni, nj = ec.codes_get(h, "Ni"), ec.codes_get(h, "Nj")
        di = ec.codes_get(h, "iDirectionIncrementInDegrees")
        dj = ec.codes_get(h, "jDirectionIncrementInDegrees")
        j_pos = bool(ec.codes_get(h, "jScansPositively"))
        lats = _axis(ec.codes_get(h, "latitudeOfFirstGridPointInDegrees"), dj, nj, j_pos)
        lons = _axis(ec.codes_get(h, "longitudeOfFirstGridPointInDegrees"), di, ni, True)

        rows = np.flatnonzero((lats >= bbox.south - _EPS) & (lats <= bbox.north + _EPS))
        rel = np.mod(lons - bbox.west + _EPS, 360.0) - _EPS
        cols = np.flatnonzero(rel <= bbox.width + _EPS)
        cols = cols[np.argsort(rel[cols], kind="stable")]
        if rows.size == 0 or cols.size == 0:
            raise GribError(f"Ausschnitt {bbox} enthält keine Gitterpunkte")

        values = ec.codes_get_values(h).reshape(nj, ni)
        sub = values[np.ix_(rows, cols)]

        out = ec.codes_clone(h)
        try:
            ec.codes_set(out, "Ni", int(cols.size))
            ec.codes_set(out, "Nj", int(rows.size))
            ec.codes_set(out, "latitudeOfFirstGridPointInDegrees", float(lats[rows[0]]))
            ec.codes_set(out, "latitudeOfLastGridPointInDegrees", float(lats[rows[-1]]))
            ec.codes_set(out, "longitudeOfFirstGridPointInDegrees", float(np.mod(lons[cols[0]], 360.0)))
            ec.codes_set(out, "longitudeOfLastGridPointInDegrees", float(np.mod(lons[cols[-1]], 360.0)))
            ec.codes_set_values(out, sub.ravel())
            return ec.codes_get_message(out)
        finally:
            ec.codes_release(out)
    finally:
        ec.codes_release(h)


def crop(data: bytes, bbox: BBox) -> bytes:
    """Schneidet alle Nachrichten eines GRIB2-Bytestroms zu."""
    return b"".join(crop_message(m, bbox) for m in iter_messages(data))
