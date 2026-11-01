"""Bronze-GRIB2 eines Laufs dekodieren und zu Feldern je Schritt zusammensetzen.

Momentanwerte gehen unverändert durch. Mittel (``avg``) und Summen (``accum``) beziehen sich
in der Quelle auf ``[start, schritt]`` mit einem Start, der alle 6 h springt (GFS:
``42-43``, ``42-44`` … ``42-48``, dann ``48-49``; ab 120 h dreistündlich ``120-123``,
``120-126``). Daraus wird der Wert für das Intervall ``(vorheriger Schritt, schritt]``:

- Beginnt die Nachricht beim vorherigen Schritt, ist sie schon das Intervall.
- Sonst wird mit der Nachricht desselben Starts aus dem vorherigen Schritt zurückgerechnet:
  Mittel ``(m₂·(t₂-a) - m₁·(t₁-a)) / (t₂-t₁)``, Summe ``s₂ - s₁``.
- Ohne passendes Paar (Analyse-Schritt f000, Lücke) bleibt der Wert leer (NaN).

@relation(SWA-WXP-DECODE, scope=file)
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

import numpy as np
from gribkit import GribError, iter_messages
from numpy.typing import NDArray

from wx_process.variables import VARIABLES, GribKey

Array = NDArray[np.float32]


@dataclass(frozen=True)
class Grid:
    """Reguläres Gitter, Breiten Nord→Süd, Längen West→Ost in (-180, 180]."""

    lat: NDArray[np.float64]
    lon: NDArray[np.float64]

    @property
    def shape(self) -> tuple[int, int]:
        return len(self.lat), len(self.lon)

    def same_as(self, other: Grid) -> bool:
        return (
            self.shape == other.shape
            and np.allclose(self.lat, other.lat)
            and np.allclose(self.lon, other.lon)
        )


@dataclass
class StepFields:
    step: int
    #: Momentanwerte je Silber-Variable
    instant: dict[str, Array] = field(default_factory=dict)
    #: Mittel bzw. Summen über ``[start, step]`` je Variable und Start
    stats: dict[str, dict[int, Array]] = field(default_factory=dict)


def _key(h: int) -> GribKey:
    import eccodes as ec

    surface = ec.codes_get(h, "typeOfFirstFixedSurface", int)
    if surface == 106:
        # Bodenschichten: ``level`` ist für 0-10 cm und 10-40 cm gleich, daher Oberkante in cm
        value = ec.codes_get(h, "scaledValueOfFirstFixedSurface", int)
        factor = ec.codes_get(h, "scaleFactorOfFirstFixedSurface", int)
        level = round(value * 10.0 ** (2 - factor))
    else:
        level = ec.codes_get(h, "level", int)
    return (
        ec.codes_get(h, "discipline", int),
        ec.codes_get(h, "parameterCategory", int),
        ec.codes_get(h, "parameterNumber", int),
        surface,
        level,
        str(ec.codes_get(h, "stepType")),
    )


def _grid(h: int) -> Grid:
    import eccodes as ec

    if ec.codes_get(h, "gridType") != "regular_ll":
        raise GribError(f"Gittertyp {ec.codes_get(h, 'gridType')!r} wird nicht unterstützt")
    ni, nj = ec.codes_get(h, "Ni", int), ec.codes_get(h, "Nj", int)
    di = ec.codes_get(h, "iDirectionIncrementInDegrees", float)
    dj = ec.codes_get(h, "jDirectionIncrementInDegrees", float)
    lat0 = ec.codes_get(h, "latitudeOfFirstGridPointInDegrees", float)
    lon0 = ec.codes_get(h, "longitudeOfFirstGridPointInDegrees", float)
    lat = lat0 + np.arange(nj) * (dj if ec.codes_get(h, "jScansPositively", int) else -dj)
    lon = np.mod(lon0 + np.arange(ni) * di + 180.0, 360.0) - 180.0
    return Grid(np.round(np.sort(lat)[::-1], 6), np.round(lon, 6))


def decode_step(grib: bytes, step: int) -> tuple[Grid, StepFields]:
    """Alle bekannten Nachrichten eines Schritts; unbekannte werden übersprungen.

    @relation(SWR-WXP-010, scope=function)
    """
    import eccodes as ec

    fields_ = StepFields(step)
    grid: Grid | None = None
    for message in iter_messages(grib):
        h = ec.codes_new_from_message(message)
        try:
            key = _key(h)
            var = VARIABLES.get(key)
            if var is None:
                continue
            ec.codes_set(h, "stepUnits", 1)
            start, end = ec.codes_get(h, "startStep", int), ec.codes_get(h, "endStep", int)
            if end != step:
                raise GribError(f"{var.name}: Nachricht für Schritt {end} in Datei f{step:03d}")
            g = _grid(h)
            if grid is None:
                grid = g
            elif not grid.same_as(g):
                raise GribError(f"{var.name}: abweichendes Gitter in f{step:03d}")
            nj, ni = g.shape
            values = ec.codes_get_values(h).reshape(nj, ni).astype(np.float32)
            if ec.codes_get(h, "jScansPositively", int):
                values = values[::-1]
            if ec.codes_get(h, "bitmapPresent", int):
                missing = ec.codes_get(h, "missingValue", float)
                values[values == missing] = np.nan
            if key[5] == "instant":
                fields_.instant[var.name] = values
            else:
                fields_.stats.setdefault(var.name, {})[start] = values
        finally:
            ec.codes_release(h)
    if grid is None:
        raise GribError(f"f{step:03d} enthält keine bekannten Nachrichten")
    return grid, fields_


def interval_values(
    steps: list[int], by_step: Mapping[int, StepFields], name: str, kind: str
) -> list[Array | None]:
    """Werte je Schritt für das Intervall seit dem vorherigen Schritt (``None`` = unbekannt).

    @relation(SWR-WXP-011, scope=function)
    """
    out: list[Array | None] = []
    for i, s in enumerate(steps):
        cur = by_step[s].stats.get(name, {})
        if i == 0 or not cur:
            out.append(None)
            continue
        p = steps[i - 1]
        prev = by_step[p].stats.get(name, {})
        if p in cur:
            out.append(cur[p])
            continue
        value: Array | None = None
        for a in sorted(cur, reverse=True):
            if a < p and a in prev:
                if kind == "avg":
                    value = ((cur[a] * (s - a) - prev[a] * (p - a)) / (s - p)).astype(np.float32)
                else:
                    value = (cur[a] - prev[a]).astype(np.float32)
                break
        out.append(value)
    return out
