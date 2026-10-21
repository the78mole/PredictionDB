"""Hilfen für Tests: synthetische GFS-Schritte in Bronze mit bekannten Werten.

Die Nachrichten tragen dieselben numerischen Schlüssel wie echte GFS-Dateien, Mittel und
Summen liegen wie bei GFS in Eimern, die alle 6 h neu beginnen (Summen zusätzlich ab 0 h).
``hourly`` liefert den wahren Wert je Stunde ``(h-1, h]``, daraus entstehen die Eimer; ein Test
kann so prüfen, ob die Rückrechnung in Silber wieder die Stundenwerte ergibt.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime

import numpy as np
import obstore
from numpy.typing import NDArray
from obstore.store import ObjectStore

from wx_ingest.bronze import Bronze
from wx_process.variables import VARIABLES, GribKey

RUN = datetime(2099, 1, 2, 6, tzinfo=UTC)

F64 = NDArray[np.float64]
#: (name, step bzw. Stundenende, lat2d, lon2d) → Werte
ValueFn = Callable[[str, int, F64, F64], F64]

#: je Silber-Name genau ein Schlüssel (GFS-Varianten, Strahlung als NCEP 0/4/192)
KEYS: dict[str, GribKey] = {}
for _k, _v in VARIABLES.items():
    if _k[:3] not in ((0, 4, 7), (0, 5, 3)):
        KEYS.setdefault(_v.name, _k)


def default_instant(name: str, step: int, lat: F64, lon: F64) -> F64:
    base = {
        "t2m": 280.0,
        "d2m": 275.0,
        "r2m": 70.0,
        "u10": 3.0,
        "v10": 4.0,
        "u100": 6.0,
        "v100": 8.0,
        "gust": 9.0,
        "tcc": 40.0,
        "csnow": 0.0,
        "prmsl": 101300.0,
        "sp": 95000.0,
        "weasd": 5.0,
        "snod": 0.02,
        "tsoil_0_10": 283.0,
        "tsoil_10_40": 284.0,
        "soilw_0_10": 0.30,
        "soilw_10_40": 0.35,
    }[name]
    out: F64 = base + 0.01 * step + 0.1 * (lat - 50.0) + 0.0 * lon
    return out


def default_hourly(name: str, hour: int, lat: F64, lon: F64) -> F64:
    """Stundenwert: Strahlung als Tagesgang, Niederschlag und Rest je Stunde verschieden."""
    base = {
        "dswrf": 400.0 * max(0.0, np.sin((hour % 24 - 6) / 12 * np.pi)),
        "dlwrf": 300.0 + hour % 3,
        "sunshine": 600.0 * (hour % 6),  # Sekunden Sonnenschein in der Stunde
        "tp": 0.1 * (hour % 5),
        "tcc_mean": 30.0 + hour % 7,
        "csnow_mean": 0.0,
    }[name]
    out: F64 = base + 0.0 * lat + 0.01 * lon
    return out


def grid(
    north: float = 58, south: float = 44, west: float = 0, east: float = 20, res: float = 1.0
) -> tuple[F64, F64]:
    lat = np.round(np.arange(north, south - res / 2, -res), 6)
    lon = np.round(np.arange(west, east + res / 2, res), 6)
    return lat, lon


def _message(key: GribKey, lat: F64, lon: F64, values: F64, start: int, end: int) -> bytes:
    import eccodes as ec

    disc, cat, num, surface, level, step_type = key
    h = ec.codes_grib_new_from_samples("regular_ll_sfc_grib2")
    try:
        ec.codes_set(h, "centre", 7)
        if step_type != "instant":
            ec.codes_set(h, "productDefinitionTemplateNumber", 8)
        ec.codes_set(h, "discipline", disc)
        ec.codes_set(h, "parameterCategory", cat)
        ec.codes_set(h, "parameterNumber", num)
        ec.codes_set(h, "typeOfFirstFixedSurface", surface)
        if surface == 103:
            ec.codes_set(h, "scaleFactorOfFirstFixedSurface", 0)
            ec.codes_set(h, "scaledValueOfFirstFixedSurface", level)
        if surface == 106:  # Bodenschicht wie bei GFS: Grenzen in cm (Skalierung 2)
            ec.codes_set(h, "typeOfSecondFixedSurface", 106)
            ec.codes_set(h, "scaleFactorOfFirstFixedSurface", 2)
            ec.codes_set(h, "scaledValueOfFirstFixedSurface", level)
            ec.codes_set(h, "scaleFactorOfSecondFixedSurface", 2)
            ec.codes_set(h, "scaledValueOfSecondFixedSurface", {0: 10, 10: 40}[level])
        if step_type == "instant":
            ec.codes_set(h, "stepUnits", 1)
            ec.codes_set(h, "forecastTime", end)
        else:
            ec.codes_set(h, "typeOfStatisticalProcessing", 0 if step_type == "avg" else 1)
            ec.codes_set(h, "stepUnits", 1)
            ec.codes_set(h, "stepRange", f"{start}-{end}")
        ec.codes_set(h, "Ni", len(lon))
        ec.codes_set(h, "Nj", len(lat))
        ec.codes_set(h, "jScansPositively", 0)
        ec.codes_set(h, "iDirectionIncrementInDegrees", float(abs(lon[1] - lon[0])))
        ec.codes_set(h, "jDirectionIncrementInDegrees", float(abs(lat[1] - lat[0])))
        ec.codes_set(h, "latitudeOfFirstGridPointInDegrees", float(lat[0]))
        ec.codes_set(h, "latitudeOfLastGridPointInDegrees", float(lat[-1]))
        ec.codes_set(h, "longitudeOfFirstGridPointInDegrees", float(np.mod(lon[0], 360)))
        ec.codes_set(h, "longitudeOfLastGridPointInDegrees", float(np.mod(lon[-1], 360)))
        ec.codes_set(h, "bitsPerValue", 24)
        ec.codes_set_values(h, values.ravel().astype(float))
        return bytes(ec.codes_get_message(h))
    finally:
        ec.codes_release(h)


def make_step(
    step: int,
    lat: F64,
    lon: F64,
    instant: ValueFn = default_instant,
    hourly: ValueFn = default_hourly,
    names: set[str] | None = None,
) -> bytes:
    """Eine GFS-artige Schrittdatei: Momentanwerte, Eimer-Mittel und -Summen.

    ``names`` begrenzt die Variablen (z. B. Läufe vor einer Erweiterung der Quelle).
    """
    lat2, lon2 = np.meshgrid(lat, lon, indexing="ij")
    out = b""
    for name, key in KEYS.items():
        if names is not None and name not in names:
            continue
        kind = key[5]
        if VARIABLES[key].bucket_hours and step > 0:  # SUNSD: Eimersumme als Momentanwert
            a = ((step - 1) // 6) * 6
            v = np.sum([hourly(name, h, lat2, lon2) for h in range(a + 1, step + 1)], axis=0)
            out += _message(key, lat, lon, v, step, step)
            continue
        if VARIABLES[key].bucket_hours:
            out += _message(key, lat, lon, np.zeros_like(lat2), step, step)
            continue
        if kind == "instant":
            out += _message(key, lat, lon, instant(name, step, lat2, lon2), step, step)
            continue
        if step == 0:
            continue
        starts = [((step - 1) // 6) * 6]
        if kind == "accum" and starts[0] != 0:
            starts.append(0)
        for a in starts:
            hours = np.stack([hourly(name, h, lat2, lon2) for h in range(a + 1, step + 1)])
            v = hours.mean(axis=0) if kind == "avg" else hours.sum(axis=0)
            out += _message(key, lat, lon, v, a, step)
    return out


def fill_bronze(
    store: ObjectStore,
    steps: list[int],
    run: datetime = RUN,
    *,
    lat: F64 | None = None,
    lon: F64 | None = None,
    complete: bool = True,
    instant: ValueFn = default_instant,
    hourly: ValueFn = default_hourly,
    names: set[str] | None = None,
) -> Bronze:
    """Legt die Schritte (und bei ``complete`` das Laufmanifest) in Bronze ab."""
    if lat is None or lon is None:
        lat, lon = grid()
    bronze = Bronze(store)
    for s in steps:
        obstore.put(store, bronze.step_key(run, s), make_step(s, lat, lon, instant, hourly, names))
    if complete:
        summary = {
            "model": "gfs",
            "resolution": "0p25",
            "run": run.isoformat(),
            "steps": len(steps),
            "bytes": 0,
            "endpoints": {"aws": len(steps)},
            "specs": [],
            "completed_at": "2099-01-02T10:00:00+00:00",
        }
        obstore.put(store, bronze.run_key(run), json.dumps(summary).encode())
    return bronze


def source_config(steps: list[tuple[int, int, int]]) -> dict[str, object]:
    return {
        "code": "gfs_test",
        "runs": [0, 6, 12, 18],
        "steps": [list(s) for s in steps],
        "bbox": {"north": 58, "south": 44, "west": 0, "east": 20},
        "endpoints": [{"name": "aws", "kind": "idx_range", "url": "https://aws.test"}],
        "messages": [{"var": "TMP", "level": "2 m above ground"}],
    }
