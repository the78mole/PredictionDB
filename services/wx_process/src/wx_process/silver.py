"""Silber: ein Zarr-v3-Store je Lauf in Garage ``wx-silver``.

Schlüssel wie Bronze, mit ``.zarr`` statt Schrittdateien::

    gfs/0p25/2026/10/20/12.zarr/          # Gruppe, Attribute: Lauf, Herkunft, Versionen
    gfs/0p25/2026/10/20/12.zarr/t2m/      # (step, lat, lon), Int16 mit scale_factor/add_offset
    gfs/0p25/2026/10/20/12.zarr/step/     # Vorlauf in Stunden; lat, lon als Koordinaten

Je Lauf ein Store statt eines wachsenden Stores je Modell: Neuberechnung und Aufräumen
bleiben auf einen Lauf beschränkt, parallele Läufe kommen sich nicht in die Quere. Das Layout
folgt der CF-Konvention, ``xarray.open_zarr`` liest es direkt (``valid_time = run + step``).
Ein Store gilt erst als vollständig, wenn das Attribut ``complete`` gesetzt ist.

Jedes Array beschreibt seine Kodierung selbst (Datentyp, bei Ganzzahlen ``scale_factor``,
``add_offset`` und ``_FillValue``). Geschrieben wird nach :data:`wx_process.variables.VARIABLES`,
gelesen nach diesen Attributen. So bleiben Stores mit älterer Kodierung lesbar, wenn sich
Datentyp, Schritt oder Offset einer Variable ändern; neu gebaut werden muss nur bei einer neuen
``SILVER_VERSION`` (anderer Inhalt).

@relation(SWA-WXP-SILVER, scope=file)
"""

from __future__ import annotations

import warnings
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import numpy as np
import zarr
from numpy.typing import NDArray
from obstore.store import ObjectStore
from zarr.codecs import BloscCodec
from zarr.storage import ObjectStore as ZarrObjectStore

from wx_process.decode import Array, Grid, StepFields, interval_values
from wx_process.variables import BY_NAME, SILVER_VERSION, VARIABLES

FILL = np.int16(-32768)
#: Koordinaten-Arrays eines Stores (keine Variablen)
COORDS = ("step", "lat", "lon")


@dataclass
class Dataset:
    """Dekodierte Felder eines Laufs, ``data[name]`` hat die Form ``(step, lat, lon)``."""

    model: str
    resolution: str
    run: datetime
    steps: NDArray[np.int64]
    grid: Grid
    data: dict[str, Array]
    attrs: dict[str, Any] = field(default_factory=dict)

    @property
    def valid_times(self) -> list[datetime]:
        return [self.run + timedelta(hours=int(s)) for s in self.steps]

    def quantized(self) -> Dataset:
        """Dieselben Werte, wie sie nach dem Speichern in Silber wieder gelesen werden."""
        data = {n: decode(encode(a, n), n) for n, a in self.data.items()}
        return Dataset(
            self.model, self.resolution, self.run, self.steps, self.grid, data, self.attrs
        )


def assemble(
    model: str,
    resolution: str,
    run: datetime,
    decoded: dict[int, tuple[Grid, StepFields]],
    attrs: dict[str, Any] | None = None,
) -> Dataset:
    """Setzt dekodierte Schritte zu einem Datensatz zusammen (Intervallwerte siehe decode).

    @relation(SWR-WXP-012, scope=function)
    """
    if not decoded:
        raise ValueError("keine Schritte")
    steps = sorted(decoded)
    grid = decoded[steps[0]][0]
    for s in steps[1:]:
        if not decoded[s][0].same_as(grid):
            raise ValueError(f"Schritt {s}: abweichendes Gitter")
    by_step = {s: decoded[s][1] for s in steps}
    shape = (len(steps), *grid.shape)
    data: dict[str, Array] = {}
    for key, var in VARIABLES.items():
        if var.name in data:
            continue
        kind = key[5]
        if var.bucket_hours:
            b = var.bucket_hours
            pseudo = {
                s: StepFields(s, stats={var.name: {((s - 1) // b) * b: f.instant[var.name]}})
                for s, f in by_step.items()
                if s > 0 and var.name in f.instant
            }
            pseudo |= {s: StepFields(s) for s in steps if s not in pseudo}
            values: list[Array | None] = interval_values(steps, pseudo, var.name, "accum")
        elif kind == "instant":
            values = [by_step[s].instant.get(var.name) for s in steps]
        else:
            values = interval_values(steps, by_step, var.name, kind)
        if var.per_hour:
            hours = np.diff([steps[0], *steps])
            values = [
                None if v is None else (v / h / var.divisor).astype(np.float32)
                for v, h in zip(values, hours, strict=True)
            ]
        if all(v is None for v in values):
            continue
        arr = np.full(shape, np.nan, dtype=np.float32)
        for i, v in enumerate(values):
            if v is not None:
                arr[i] = v
        if var.min_value is not None:
            arr = np.where(arr < var.min_value, np.float32(var.min_value), arr)
        data[var.name] = arr
    return Dataset(model, resolution, run, np.asarray(steps, np.int64), grid, data, attrs or {})


def encode(values: Array, name: str) -> NDArray[Any]:
    """Werte im Ablage-Datentyp der Variable (Int16 skaliert, sonst Gleitkomma).

    @relation(SWR-WXP-020, scope=function)
    """
    var = BY_NAME[name]
    if var.dtype != "int16":
        dt = np.dtype(var.dtype)
        lim = float(np.finfo(dt).max)
        # begrenzen statt ±inf, wie bei Int16
        return np.where(np.isnan(values), np.nan, np.clip(values, -lim, lim)).astype(dt)
    q = np.round((np.clip(values, var.lo, var.hi) - var.offset) / var.scale)
    out = np.where(np.isnan(values), FILL, q).astype(np.int16)
    return out


def decode(values: NDArray[Any], name: str) -> Array:
    """Rückweg zu :func:`encode` nach der aktuellen Tabelle.

    @relation(SWR-WXP-020, scope=function)
    """
    var = BY_NAME[name]
    if var.dtype != "int16":
        return values.astype(np.float32)
    return decode_array(
        values, {"scale_factor": var.scale, "add_offset": var.offset, "_FillValue": int(FILL)}
    )


def decode_array(values: NDArray[Any], attrs: dict[str, Any]) -> Array:
    """Werte eines gespeicherten Arrays nach seinen eigenen Attributen.

    Ganzzahlen werden mit ``scale_factor``/``add_offset`` (Standard 1 und 0) zurückgerechnet,
    ``_FillValue`` wird NaN; Gleitkommawerte kommen unverändert als float32 zurück.

    @relation(SWR-WXP-025, scope=function)
    """
    if not np.issubdtype(values.dtype, np.integer):
        return values.astype(np.float32)
    scale = np.float32(attrs.get("scale_factor", 1.0))
    offset = np.float32(attrs.get("add_offset", 0.0))
    out = (values.astype(np.float32) * scale + offset).astype(np.float32)
    fill = attrs.get("_FillValue")
    if fill is not None:
        out[values == fill] = np.nan
    return out


class Silver:
    def __init__(self, store: ObjectStore, *, step_chunk: int = 48, zstd_level: int = 5) -> None:
        self.store = ZarrObjectStore(store, read_only=False)
        self.step_chunk = step_chunk
        self.zstd_level = zstd_level

    @staticmethod
    def key(model: str, resolution: str, run: datetime) -> str:
        return f"{model}/{resolution}/{run:%Y/%m/%d/%H}.zarr"

    def attrs(self, model: str, resolution: str, run: datetime) -> dict[str, Any] | None:
        """Attribute eines vollständigen Stores, sonst ``None``."""
        try:
            group = zarr.open_group(self.store, path=self.key(model, resolution, run), mode="r")
        except FileNotFoundError:
            return None
        attrs = dict(group.attrs)
        return attrs if attrs.get("complete") else None

    def write(self, ds: Dataset) -> str:
        """@relation(SWR-WXP-020, scope=function)"""
        key = self.key(ds.model, ds.resolution, ds.run)
        group = zarr.open_group(
            self.store,
            path=key,
            mode="w",
            zarr_format=3,
            attributes={
                **ds.attrs,
                "model": ds.model,
                "resolution": ds.resolution,
                "run_time": ds.run.isoformat(),
                "silver_version": SILVER_VERSION,
                "Conventions": "CF-1.10",
            },
        )
        # Byte-Shuffle vor Zstd spart bei Int16 gut 10 % gegenüber Zstd allein
        compressors = [BloscCodec(cname="zstd", clevel=self.zstd_level, shuffle="shuffle")]
        coords: list[tuple[str, np.ndarray, dict[str, Any]]] = [
            ("step", ds.steps.astype(np.int16), {"units": "h", "long_name": "Vorlauf"}),
            ("lat", ds.grid.lat.astype(np.float32), {"units": "degrees_north"}),
            ("lon", ds.grid.lon.astype(np.float32), {"units": "degrees_east"}),
        ]
        for name, values, attrs in coords:
            group.create_array(
                name, data=values, dimension_names=[name], attributes=attrs, compressors=None
            )
        chunks = (min(self.step_chunk, len(ds.steps)), *ds.grid.shape)
        for name, arr in ds.data.items():
            var = BY_NAME[name]
            attributes: dict[str, Any] = {
                "units": var.units,
                "long_name": var.long_name,
                "coordinates": "step lat lon",
            }
            fill: float | int
            if var.dtype == "int16":
                fill = int(FILL)
                attributes |= {
                    "scale_factor": var.scale,
                    "add_offset": var.offset,
                    "_FillValue": fill,
                }
            else:
                # fehlende Werte sind NaN; der Füllwert steht in den Zarr-Metadaten
                fill = float("nan")
            z = group.create_array(
                name,
                shape=arr.shape,
                dtype=var.dtype,
                chunks=chunks,
                compressors=compressors,
                fill_value=fill,
                dimension_names=["step", "lat", "lon"],
                attributes=attributes,
            )
            z[...] = encode(arr, name)
        group.attrs["complete"] = True
        with warnings.catch_warnings():
            # konsolidierte Metadaten sind in Zarr v3 noch eine Erweiterung; xarray nutzt sie
            warnings.simplefilter("ignore", UserWarning)
            zarr.consolidate_metadata(self.store, path=key, zarr_format=3)
        return key

    def read(self, model: str, resolution: str, run: datetime) -> Dataset:
        """Alle Variablen eines vollständigen Stores, dekodiert nach den Array-Attributen.

        @relation(SWR-WXP-020, SWR-WXP-025, scope=function)
        """
        group = zarr.open_group(self.store, path=self.key(model, resolution, run), mode="r")
        attrs = dict(group.attrs)
        if not attrs.get("complete"):
            raise FileNotFoundError(f"Silber für {run:%Y%m%d%H} unvollständig")

        def array(name: str) -> zarr.Array[Any]:
            arr = group[name]
            assert isinstance(arr, zarr.Array)
            return arr

        def values(name: str) -> np.ndarray:
            return np.asarray(array(name)[...])

        steps = values("step").astype(np.int64)
        grid = Grid(
            values("lat").astype(np.float64).round(6), values("lon").astype(np.float64).round(6)
        )
        names = sorted(n for n in group.array_keys() if n not in COORDS)
        data = {n: decode_array(values(n), dict(array(n).attrs)) for n in names}
        return Dataset(model, resolution, run, steps, grid, data, attrs)
