"""GEFS-Ensemble im Runner: Felder per Byte-Bereich laden und je Region und Mitglied verdichten.

Je Lauf, Mitglied (``gec00``, ``gep01`` … ``gep30``) und Schritt werden aus der ``.idx`` der
Dateien ``pgrb2a``/``pgrb2b`` (0,5°) nur die Nachrichten der Felder in
``config/sources/gefs_0p5.yaml`` geladen, auf den Europa-Ausschnitt zugeschnitten und zu
Kennzahlen je Region verdichtet (Gewichte aus ``config/gefs/regions_v2.json.gz``, Summe je
Region 1). Nichtlineare Größen (Wind in Nabenhöhe, Leistungskurve, PV-Modell) werden wie in
``wx_process.features`` erst je Zelle gerechnet und dann gewichtet. Erst danach, auf der VM,
wird über die Mitglieder verdichtet (Mittel, Streuung, Quantile); das Silber behält jedes
Mitglied.

Silber eines Laufs: Tar (gzip, reproduzierbar) mit ``meta.json`` und ``values.npy``
(``float32``, Achsen Mitglied x Schritt x Region x Größe, NaN = unbekannt). Mittel und Summen
beziehen sich auf die 6 h vor dem Schritt; in f000 sind sie leer.

Nach GHCR (``predictiondb-data/gefs-0p5-silver``) geht je Monat und Laufzeit ein Manifest mit
einem Layer je Lauf, wie beim GFS (``wx_exchange.artifact``), nur mit eigenem Layer-Typ.
"""

from __future__ import annotations

import asyncio
import gzip
import hashlib
import io
import json
import tarfile
from collections import Counter
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

import httpx
import numpy as np
import yaml
from numpy.typing import NDArray
from pydantic import BaseModel, Field

from predictiondb_core.log import get_logger
from wx_exchange.artifact import LICENSE, PREFIX, month_bounds, tag
from wx_exchange.oci import EMPTY, MANIFEST, Registry
from wx_process.features import PowerCurve, PvModel, TechnologyFeatures, shear_exponent

log = get_logger(__name__)

F32 = NDArray[np.float32]
F64 = NDArray[np.float64]

#: Version des Silber-Formats; eine Änderung baut alle Läufe neu
SILVER_VERSION = "1"
ARTIFACT_TYPE = "application/vnd.predictiondb.gefs-silver.v1"
LAYER_TYPE = "application/vnd.predictiondb.gefs-silver.v1.tar+gzip"
ATTRIBUTION = (
    "NOAA/NCEP Global Ensemble Forecast System (GEFS v12); Gewichte aus MaStR (DL-DE BY 2.0)"
    " und Natural Earth (gemeinfrei); verdichtet von PredictionDB"
)
DEFAULT_REPOSITORY = "the78mole/predictiondb-data/gefs-0p5-silver"

#: Größen je Region in dieser Reihenfolge (Einheiten wie ``wx_process.features.UNITS``)
VARIABLES: dict[str, str] = {
    "t2m": "degC",
    "wind_speed_100m": "m s-1",
    "wind_cf": "1",
    "ghi": "W m-2",
    "pv_cf": "1",
    "cloud_cover": "%",
    "precip": "mm h-1",
}

RunStatus = Literal["uploaded", "unchanged", "missing", "pending", "failed"]


# --- Konfiguration ---------------------------------------------------------------------------


class FieldSpec(BaseModel):
    product: Literal["a", "b"]
    param: str
    level: str


class StepRange(BaseModel):
    start: int = 0
    end: int = 384
    every: int = Field(default=6, gt=0)

    def list(self) -> list[int]:
        return list(range(self.start, self.end + 1, self.every))


class BBox(BaseModel):
    north: float
    south: float
    west: float
    east: float


class GefsConfig(BaseModel):
    code: str = "gefs_0p5"
    base_url: str = "https://noaa-gefs-pds.s3.amazonaws.com"
    first_run: datetime = datetime(2021, 3, 22, 12, tzinfo=UTC)
    hours: list[int] = Field(default_factory=lambda: [0])
    perturbed: int = Field(default=30, ge=0)
    steps: StepRange = Field(default_factory=StepRange)
    fields: dict[str, FieldSpec]
    bbox: BBox
    regions: Path
    concurrency: int = Field(default=48, gt=0)
    pending_hours: float = 30
    max_missing_share: float = Field(default=0.1, ge=0, le=1)
    technologies: dict[str, TechnologyFeatures]
    pv: PvModel = Field(default_factory=PvModel)

    @classmethod
    def from_yaml(cls, path: Path) -> GefsConfig:
        cfg = cls.model_validate(yaml.safe_load(path.read_text()))
        if not cfg.regions.is_absolute():
            # relativ zum Repository (Konfiguration liegt unter config/sources)
            root = path.resolve().parent.parent.parent
            cfg.regions = root / cfg.regions
        return cfg

    @property
    def members(self) -> list[str]:
        return ["gec00", *(f"gep{i:02d}" for i in range(1, self.perturbed + 1))]

    def url(self, run: datetime, member: str, step: int, product: str) -> str:
        hh = f"{run:%H}"
        return (
            f"{self.base_url}/gefs.{run:%Y%m%d}/{hh}/atmos/pgrb2{product}p5/"
            f"{member}.t{hh}z.pgrb2{product}.0p50.f{step:03d}"
        )

    def runs_between(self, start: datetime, end: datetime, hours: list[int]) -> list[datetime]:
        start = max(start, self.first_run)
        day = start.replace(hour=0, minute=0, second=0, microsecond=0)
        out = []
        while day <= end:
            out += [day.replace(hour=h) for h in hours if start <= day.replace(hour=h) <= end]
            day += timedelta(days=1)
        return out


# --- Regionen --------------------------------------------------------------------------------


@dataclass(frozen=True)
class Region:
    code: str
    technology: str
    lat: F64
    lon: F64
    share: F64
    hub_height_m: float | None = None


@dataclass
class Regions:
    version: str
    regions: list[Region]
    sha256: str
    sources: list[str] = field(default_factory=list)

    @classmethod
    def load(cls, path: Path) -> Regions:
        raw = path.read_bytes()
        data = json.loads(gzip.decompress(raw) if path.suffix == ".gz" else raw)
        regions = []
        for r in data["regions"]:
            cells = np.asarray(r["cells"], dtype=np.float64).reshape(-1, 3)
            regions.append(
                Region(
                    r["code"],
                    r["technology"],
                    cells[:, 0],
                    cells[:, 1],
                    cells[:, 2] / cells[:, 2].sum(),
                    r.get("hub_height_m"),
                )
            )
        return cls(
            data["version"],
            regions,
            hashlib.sha256(raw).hexdigest()[:16],
            data.get("sources", []),
        )

    @property
    def codes(self) -> list[str]:
        return [r.code for r in self.regions]


def fingerprint(config: GefsConfig, regions: Regions) -> str:
    """Hash der Eingaben eines Silber-Baus: gleiche Eingaben ergeben denselben Inhalt."""
    data = {
        "silver_version": SILVER_VERSION,
        "members": config.members,
        "steps": config.steps.list(),
        "fields": {k: v.model_dump() for k, v in sorted(config.fields.items())},
        "bbox": config.bbox.model_dump(),
        "regions": regions.sha256,
        "technologies": {k: v.model_dump() for k, v in sorted(config.technologies.items())},
        "pv": config.pv.model_dump(),
        "variables": list(VARIABLES),
    }
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()[:16]


# --- .idx und Byte-Bereiche ------------------------------------------------------------------


@dataclass(frozen=True)
class Message:
    name: str
    start: int
    #: exklusiv; ``None`` = bis Dateiende
    end: int | None


def select(idx_text: str, wanted: dict[str, FieldSpec]) -> list[Message]:
    """Nachrichten der gewünschten Felder aus einer ``.idx`` (je Feld die erste passende)."""
    lines = [ln.split(":") for ln in idx_text.splitlines() if ln.strip()]
    offsets = [int(p[1]) for p in lines]
    out: list[Message] = []
    for name, spec in wanted.items():
        for i, p in enumerate(lines):
            if len(p) > 4 and p[3] == spec.param and p[4] == spec.level:
                end = offsets[i + 1] if i + 1 < len(offsets) else None
                out.append(Message(name, offsets[i], end))
                break
    return sorted(out, key=lambda m: m.start)


def ranges(messages: list[Message]) -> list[list[Message]]:
    """Aneinander grenzende Nachrichten zu einer Anfrage zusammenfassen."""
    out: list[list[Message]] = []
    for m in messages:
        if out and out[-1][-1].end == m.start:
            out[-1].append(m)
        else:
            out.append([m])
    return out


# Eigener Pool für die blockierende Arbeit beim Aufbau (Garage-PUT, Dekodieren, Packen). Der
# Standard-Pool von asyncio löst auch die DNS-Namen der neuen Verbindungen auf; ist er mit
# langsamen PUTs belegt (Garage unter Last 17 s und mehr), laufen alle Verbindungen zu AWS in
# den ConnectTimeout (VM, 2026-10-30: 639 gefs_file_failed in 2 h).
_WORK = ThreadPoolExecutor(max_workers=32, thread_name_prefix="gefs-work")


async def _work(fn: Callable[..., Any], *args: Any) -> Any:
    return await asyncio.get_running_loop().run_in_executor(_WORK, fn, *args)


class Missing(Exception):
    """Datei fehlt im Archiv (404)."""


async def _get(client: httpx.AsyncClient, url: str, headers: dict[str, str] | None = None) -> bytes:
    for wait in (1.0, 4.0, 15.0, None):
        try:
            r = await client.get(url, headers=headers)
        except httpx.TransportError:
            if wait is None:
                raise
        else:
            if r.status_code in (403, 404):
                raise Missing(url)
            if r.status_code < 500 and r.status_code != 429:
                r.raise_for_status()
                return r.content
            if wait is None:
                r.raise_for_status()
        await asyncio.sleep(wait or 0)
    raise AssertionError("unerreichbar")


async def fetch_fields(
    client: httpx.AsyncClient, config: GefsConfig, run: datetime, member: str, step: int
) -> dict[str, bytes]:
    """GRIB-Nachrichten je Feld eines Mitglied-Schritts (fehlende Felder fehlen im Ergebnis)."""
    out: dict[str, bytes] = {}
    for product in ("a", "b"):
        wanted = {k: v for k, v in config.fields.items() if v.product == product}
        if not wanted:
            continue
        url = config.url(run, member, step, product)
        text = (await _get(client, url + ".idx")).decode()
        for group in ranges(select(text, wanted)):
            a, b = group[0].start, group[-1].end
            rng = f"bytes={a}-" + ("" if b is None else str(b - 1))
            data = await _get(client, url, {"Range": rng})
            for m in group:
                out[m.name] = data[m.start - a : None if m.end is None else m.end - a]
    return out


# --- Dekodieren und Verdichten ---------------------------------------------------------------


@dataclass(frozen=True)
class Cutout:
    """Zeilen und Spalten des Ausschnitts in einem globalen Gitter (Breiten Nord→Süd)."""

    rows: NDArray[np.int64]
    cols: NDArray[np.int64]
    lat: F64
    lon: F64
    flip: bool

    @classmethod
    def from_message(cls, h: int, bbox: BBox) -> Cutout:
        import eccodes as ec

        ni, nj = ec.codes_get(h, "Ni", int), ec.codes_get(h, "Nj", int)
        di = ec.codes_get(h, "iDirectionIncrementInDegrees", float)
        dj = ec.codes_get(h, "jDirectionIncrementInDegrees", float)
        lat0 = ec.codes_get(h, "latitudeOfFirstGridPointInDegrees", float)
        lon0 = ec.codes_get(h, "longitudeOfFirstGridPointInDegrees", float)
        flip = bool(ec.codes_get(h, "jScansPositively", int))
        lat = lat0 + np.arange(nj) * (dj if flip else -dj)
        if flip:
            lat = lat[::-1]
        lon = np.mod(lon0 + np.arange(ni) * di + 180.0, 360.0) - 180.0
        rows = np.flatnonzero((lat <= bbox.north + 1e-6) & (lat >= bbox.south - 1e-6))
        cols = np.flatnonzero((lon >= bbox.west - 1e-6) & (lon <= bbox.east + 1e-6))
        cols = cols[np.argsort(lon[cols])]
        return cls(rows, cols, np.round(lat[rows], 6), np.round(lon[cols], 6), flip)

    def cut(self, h: int) -> F64:
        import eccodes as ec

        nj, ni = ec.codes_get(h, "Nj", int), ec.codes_get(h, "Ni", int)
        values = ec.codes_get_values(h).reshape(nj, ni)
        if self.flip:
            values = values[::-1]
        out: F64 = values[np.ix_(self.rows, self.cols)].astype(np.float64)
        if ec.codes_get(h, "bitmapPresent", int):
            out[out == ec.codes_get(h, "missingValue", float)] = np.nan
        return out


def decode(messages: dict[str, bytes], bbox: BBox) -> tuple[Cutout | None, dict[str, F64]]:
    import eccodes as ec

    cutout: Cutout | None = None
    out: dict[str, F64] = {}
    for name, data in messages.items():
        h = ec.codes_new_from_message(data)
        try:
            if cutout is None:
                cutout = Cutout.from_message(h, bbox)
            out[name] = cutout.cut(h)
        finally:
            ec.codes_release(h)
    return cutout, out


class Aggregator:
    """Kennzahlen je Region aus den Feldern eines Mitglied-Schritts."""

    def __init__(self, config: GefsConfig, regions: Regions) -> None:
        self.config = config
        self.regions = regions
        self._weights: tuple[F64, F64, F64] | None = None
        self._grid: tuple[F64, F64] | None = None

    def _prepare(self, cutout: Cutout) -> F64:
        """Gewichtsmatrix Region x Zelle des Ausschnitts (zeilenweise Summe 1)."""
        grid = (cutout.lat, cutout.lon)
        if self._grid is not None and all(
            np.array_equal(a, b) for a, b in zip(self._grid, grid, strict=True)
        ):
            assert self._weights is not None
            return self._weights[0]
        nlat, nlon = len(cutout.lat), len(cutout.lon)
        w = np.zeros((len(self.regions.regions), nlat * nlon))
        res_lat = abs(cutout.lat[1] - cutout.lat[0]) if nlat > 1 else 1.0
        res_lon = abs(cutout.lon[1] - cutout.lon[0]) if nlon > 1 else 1.0
        for k, r in enumerate(self.regions.regions):
            i = np.round((cutout.lat[0] - r.lat) / res_lat).astype(np.int64)
            j = np.round((r.lon - cutout.lon[0]) / res_lon).astype(np.int64)
            ok = (i >= 0) & (i < nlat) & (j >= 0) & (j < nlon)
            np.add.at(w[k], i[ok] * nlon + j[ok], r.share[ok])
            total = w[k].sum()
            if total > 0:
                w[k] /= total
        self._grid = grid
        self._weights = (w, cutout.lat, cutout.lon)
        return w

    def __call__(self, cutout: Cutout, f: dict[str, F64]) -> F32:
        """Werte Region x Größe (NaN, wo ein Feld fehlt)."""
        w = self._prepare(cutout)
        n = w.shape[1]
        out = np.full((len(self.regions.regions), len(VARIABLES)), np.nan)

        def flat(name: str) -> F64 | None:
            a = f.get(name)
            return None if a is None else a.reshape(n)

        def put(var: str, cells: F64 | None, rows: slice | list[int] = slice(None)) -> None:
            if cells is None:
                return
            col = list(VARIABLES).index(var)
            out[rows, col] = w[rows] @ cells

        t2m = flat("t2m")
        t_c = None if t2m is None else t2m - 273.15
        put("t2m", t_c)
        u10, v10, u100, v100 = (flat(x) for x in ("u10", "v10", "u100", "v100"))
        if u100 is not None and v100 is not None:
            ws100 = np.hypot(u100, v100)
            put("wind_speed_100m", ws100)
            if u10 is not None and v10 is not None:
                alpha = shear_exponent(np.hypot(u10, v10), ws100)
            else:
                alpha = np.full_like(ws100, 1 / 7)
            for hub, curve, rows in self._wind_groups():
                cf = curve(ws100 * (hub / 100.0) ** alpha)
                put("wind_cf", cf, rows)
        ghi = flat("dswrf")
        put("ghi", ghi)
        if ghi is not None and t_c is not None:
            put("pv_cf", self.config.pv(ghi, t_c))
        put("cloud_cover", flat("tcc"))
        tp = flat("tp")
        put("precip", None if tp is None else tp / self.config.steps.every)
        return out.astype(np.float32)

    def _wind_groups(self) -> list[tuple[float, PowerCurve, list[int]]]:
        """Regionen mit gleicher Nabenhöhe und Leistungskurve (je Gruppe eine Rechnung)."""
        groups: dict[tuple[float, float, float, float], tuple[PowerCurve, list[int]]] = {}
        default = self.config.technologies["wind_on"]
        for k, r in enumerate(self.regions.regions):
            tech = self.config.technologies.get(r.technology, default)
            if tech.kind != "wind":
                tech = default
            hub = float(r.hub_height_m or tech.hub_height_m)
            c = tech.power_curve
            groups.setdefault((hub, c.cut_in, c.rated, c.cut_out), (c, []))[1].append(k)
        return [(key[0], c, rows) for key, (c, rows) in groups.items()]


# --- Lauf bauen und packen -------------------------------------------------------------------


@dataclass
class RunSilver:
    run: datetime
    values: F32
    members: list[str]
    steps: list[int]
    regions: list[str]
    #: fehlende Mitglied-Schritt-Dateien
    missing: int = 0
    failed: int = 0

    @property
    def total(self) -> int:
        return len(self.members) * len(self.steps)

    def meta(self, fp: str, regions_version: str) -> dict[str, Any]:
        return {
            "model": "gefs",
            "grid": "0p5",
            "run": self.run.isoformat(),
            "members": self.members,
            "steps": self.steps,
            "regions": self.regions,
            "regions_version": regions_version,
            "variables": VARIABLES,
            "missing": self.missing,
            "fingerprint": fp,
            "silver_version": SILVER_VERSION,
            "shape": list(self.values.shape),
        }


def pack(meta: dict[str, Any], values: F32) -> tuple[bytes, str]:
    """Reproduzierbarer Tar (gzip ohne Zeitstempel) und Inhalts-Hash."""
    buf = io.BytesIO()
    np.save(buf, np.ascontiguousarray(values, dtype="<f4"), allow_pickle=False)
    files = {
        "meta.json": json.dumps(meta, sort_keys=True, indent=1).encode(),
        "values.npy": buf.getvalue(),
    }
    tbuf = io.BytesIO()
    with tarfile.open(fileobj=tbuf, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for name in sorted(files):
            info = tarfile.TarInfo(name)
            info.size = len(files[name])
            info.mode = 0o644
            tar.addfile(info, io.BytesIO(files[name]))
    h = hashlib.sha256()
    for name in sorted(files):
        h.update(name.encode() + b"\0" + hashlib.sha256(files[name]).digest())
    return gzip.compress(tbuf.getvalue(), compresslevel=6, mtime=0), h.hexdigest()


def unpack(data: bytes) -> tuple[dict[str, Any], F32]:
    with tarfile.open(fileobj=io.BytesIO(gzip.decompress(data)), mode="r") as tar:
        files = {}
        for m in tar.getmembers():
            f = tar.extractfile(m) if m.isfile() else None
            if f is not None:
                files[m.name] = f.read()
    meta = json.loads(files["meta.json"])
    values = np.load(io.BytesIO(files["values.npy"]), allow_pickle=False)
    return meta, values.astype(np.float32)


class BronzeSink:
    """Ablage der geladenen Nachrichten eines Mitglied-Schritts (VM: ``gefs_vm.GefsBronze``)."""

    def put(self, run: datetime, member: str, step: int, messages: dict[str, bytes]) -> int:
        raise NotImplementedError

    def complete(self, run: datetime, stats: dict[str, Any]) -> None:
        raise NotImplementedError


class RunBuilder:
    def __init__(
        self,
        config: GefsConfig,
        regions: Regions,
        client: httpx.AsyncClient,
        *,
        bronze: BronzeSink | None = None,
    ) -> None:
        self.config = config
        self.regions = regions
        self.client = client
        self.bronze = bronze
        self.aggregate = Aggregator(config, regions)
        self.sem = asyncio.Semaphore(config.concurrency)

    async def available(self, run: datetime) -> bool:
        """Liegt der letzte Schritt des letzten Mitglieds schon vor (Lauf vollständig)?"""
        member, step = self.config.members[-1], self.config.steps.list()[-1]
        products = sorted({f.product for f in self.config.fields.values()})
        for product in products:
            try:
                r = await self.client.head(self.config.url(run, member, step, product) + ".idx")
            except httpx.TransportError:
                return False
            if r.status_code != 200:
                return False
        return True

    async def build(self, run: datetime) -> RunSilver:
        members, steps = self.config.members, self.config.steps.list()
        values = np.full(
            (len(members), len(steps), len(self.regions.regions), len(VARIABLES)),
            np.nan,
            dtype=np.float32,
        )
        counts: Counter[str] = Counter()

        async def one(m: int, s: int) -> None:
            try:
                async with self.sem:
                    msgs = await fetch_fields(self.client, self.config, run, members[m], steps[s])
                if self.bronze is not None and msgs:
                    counts["bytes"] += await _work(self.bronze.put, run, members[m], steps[s], msgs)
                cutout, f = await _work(decode, msgs, self.config.bbox)
                if cutout is not None:
                    values[m, s] = await _work(self.aggregate, cutout, f)
                counts["ok"] += 1
            except Missing:
                counts["missing"] += 1
            except Exception as exc:  # eine Datei darf den Lauf nicht abbrechen
                log.warning(
                    "gefs_file_failed",
                    run=f"{run:%Y%m%d%H}",
                    member=members[m],
                    step=steps[s],
                    error=f"{type(exc).__name__}: {exc}",
                )
                counts["failed"] += 1

        await asyncio.gather(*(one(m, s) for m in range(len(members)) for s in range(len(steps))))
        if self.bronze is not None and counts["ok"]:
            stats = {"files": counts["ok"], "missing": counts["missing"], "bytes": counts["bytes"]}
            await _work(self.bronze.complete, run, stats)
        return RunSilver(
            run,
            values,
            members,
            steps,
            self.regions.codes,
            missing=counts["missing"],
            failed=counts["failed"],
        )


# --- Manifest je Monat -----------------------------------------------------------------------


@dataclass
class GefsLayer:
    run: datetime
    digest: str
    size: int
    content: str
    fingerprint: str
    missing: int = 0

    def descriptor(self) -> dict[str, Any]:
        return {
            "mediaType": LAYER_TYPE,
            "digest": self.digest,
            "size": self.size,
            "annotations": {
                "org.opencontainers.image.title": f"{self.run:%Y%m%d%H}.tar.gz",
                PREFIX + "run": self.run.isoformat(),
                PREFIX + "content": self.content,
                PREFIX + "fingerprint": self.fingerprint,
                PREFIX + "silver_version": SILVER_VERSION,
                PREFIX + "missing": str(self.missing),
            },
        }

    @classmethod
    def from_descriptor(cls, d: dict[str, Any]) -> GefsLayer:
        a = d.get("annotations", {})
        return cls(
            run=datetime.fromisoformat(a[PREFIX + "run"]),
            digest=d["digest"],
            size=int(d["size"]),
            content=a.get(PREFIX + "content", ""),
            fingerprint=a.get(PREFIX + "fingerprint", ""),
            missing=int(a.get(PREFIX + "missing", 0)),
        )


@dataclass
class GefsMonth:
    month: str
    hour: int
    layers: dict[datetime, GefsLayer] = field(default_factory=dict)
    missing: set[datetime] = field(default_factory=set)
    regions_version: str = ""

    @property
    def tag(self) -> str:
        return tag(self.month, self.hour)

    def done(self, fp: str) -> set[datetime]:
        return {r for r, layer in self.layers.items() if layer.fingerprint == fp} | self.missing

    def manifest(self, source: str) -> dict[str, Any]:
        return {
            "schemaVersion": 2,
            "mediaType": MANIFEST,
            "artifactType": ARTIFACT_TYPE,
            "config": EMPTY,
            "layers": [self.layers[r].descriptor() for r in sorted(self.layers)],
            "annotations": {
                "org.opencontainers.image.source": source,
                "org.opencontainers.image.created": datetime.now(UTC).isoformat(timespec="seconds"),
                "org.opencontainers.image.licenses": LICENSE,
                PREFIX + "attribution": ATTRIBUTION,
                PREFIX + "model": "gefs",
                PREFIX + "grid": "0p5",
                PREFIX + "month": self.month,
                PREFIX + "hour": str(self.hour),
                PREFIX + "regions_version": self.regions_version,
                PREFIX + "missing": ",".join(f"{r:%Y%m%d%H}" for r in sorted(self.missing)),
            },
        }

    @classmethod
    def from_manifest(cls, month: str, hour: int, m: dict[str, Any] | None) -> GefsMonth:
        art = cls(month, hour)
        if m is None:
            return art
        a = m.get("annotations", {})
        art.regions_version = a.get(PREFIX + "regions_version", "")
        for d in m.get("layers", []):
            if d.get("mediaType") == LAYER_TYPE:
                layer = GefsLayer.from_descriptor(d)
                art.layers[layer.run] = layer
        for text in filter(None, a.get(PREFIX + "missing", "").split(",")):
            art.missing.add(datetime.strptime(text, "%Y%m%d%H").replace(tzinfo=UTC))
        return art


def months_between(start: datetime, end: datetime) -> list[str]:
    out = []
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        out.append(f"{y:04d}-{m:02d}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def plan(
    registry: Registry,
    config: GefsConfig,
    regions: Regions,
    start: datetime,
    end: datetime,
    *,
    max_jobs: int = 256,
    rebuild: bool = False,
) -> list[dict[str, Any]]:
    """Matrix der Monat-Laufzeit-Paare mit fehlenden Läufen, jüngste zuerst."""
    fp = fingerprint(config, regions)
    existing = set(registry.tags())
    out = []
    for month in months_between(max(start, config.first_run), end)[::-1]:
        a, b = month_bounds(month)
        for hour in config.hours:
            runs = set(config.runs_between(max(a, start), min(b, end), [hour]))
            if not runs:
                continue
            t = tag(month, hour)
            art = GefsMonth.from_manifest(
                month, hour, registry.manifest(t) if t in existing else None
            )
            todo = len(runs - (art.missing if rebuild else art.done(fp)))
            if todo:
                out.append({"month": month, "hour": hour, "todo": todo})
    if len(out) > max_jobs:
        out = out[:max_jobs]
    return out


@dataclass
class MonthResult:
    tag: str
    runs: dict[str, RunStatus] = field(default_factory=dict)
    skipped: int = 0
    bytes_uploaded: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "tag": self.tag,
            "skipped": self.skipped,
            "counts": dict(Counter(self.runs.values())),
            "failed": sorted(r for r, s in self.runs.items() if s == "failed"),
            "bytes_uploaded": self.bytes_uploaded,
        }


class GefsPublisher:
    def __init__(
        self,
        registry: Registry,
        config: GefsConfig,
        regions: Regions,
        client: httpx.AsyncClient,
        *,
        source_url: str,
        parallel_runs: int = 1,
        now: datetime | None = None,
        bronze: BronzeSink | None = None,
        on_packed: Callable[[datetime, RunSilver, bytes, str], None] | None = None,
    ) -> None:
        self.registry = registry
        self.config = config
        self.regions = regions
        self.builder = RunBuilder(config, regions, client, bronze=bronze)
        self.source_url = source_url
        self.parallel_runs = max(1, parallel_runs)
        self.fp = fingerprint(config, regions)
        self.now = now
        #: nach dem Packen eines vollständigen Laufs (VM: lokales Silber, Bestand melden)
        self.on_packed = on_packed

    def _young(self, run: datetime) -> bool:
        now = self.now or datetime.now(UTC)
        return now - run < timedelta(hours=self.config.pending_hours)

    def _status(self, run: datetime, silver: RunSilver) -> RunStatus:
        bad = silver.missing + silver.failed
        if bad <= self.config.max_missing_share * silver.total:
            return "uploaded"
        if silver.failed:
            return "failed"
        if self._young(run):
            return "pending"
        return "missing"

    async def publish_month(
        self,
        month: str,
        hour: int,
        start: datetime | None = None,
        end: datetime | None = None,
        *,
        rebuild: bool = False,
    ) -> MonthResult:
        a, b = month_bounds(month)
        a, b = max(a, start or a), min(b, end or b)
        t = tag(month, hour)
        art = GefsMonth.from_manifest(
            month, hour, await asyncio.to_thread(self.registry.manifest, t)
        )
        art.regions_version = self.regions.version
        runs = self.config.runs_between(a, b, [hour])
        done = art.missing if rebuild else art.done(self.fp)
        todo = [r for r in runs if r not in done]
        result = MonthResult(t, skipped=len(runs) - len(todo))
        log.info("gefs_month_start", tag=t, runs=len(runs), todo=len(todo))
        sem = asyncio.Semaphore(self.parallel_runs)
        lock = asyncio.Lock()

        async def one(run: datetime) -> None:
            key = f"{run:%Y%m%d%H}"
            async with sem:
                try:
                    # junger Lauf noch nicht vollständig: keine 2000 Anfragen ins Leere
                    if self._young(run) and not await self.builder.available(run):
                        result.runs[key] = "pending"
                        return
                    silver = await self.builder.build(run)
                    status = self._status(run, silver)
                    data, content = b"", ""
                    if status == "uploaded":
                        meta = silver.meta(self.fp, self.regions.version)
                        data, content = await _work(pack, meta, silver.values)
                        if self.on_packed is not None:
                            await _work(self.on_packed, run, silver, data, content)
                except Exception as exc:
                    log.exception("gefs_run_failed", run=key, error=str(exc))
                    status, data, content = "failed", b"", ""
            async with lock:
                old = art.layers.get(run)
                if status == "uploaded" and old is not None and old.content == content:
                    status = "unchanged"
                    if old.fingerprint == self.fp:
                        result.runs[key] = status
                        return
                    old.fingerprint = self.fp
                elif status == "uploaded":
                    digest = await asyncio.to_thread(self.registry.push_blob, data)
                    result.bytes_uploaded += len(data)
                    art.layers[run] = GefsLayer(
                        run, digest, len(data), content, self.fp, silver.missing + silver.failed
                    )
                    art.missing.discard(run)
                elif status == "missing":
                    art.missing.add(run)
                else:
                    result.runs[key] = status
                    return
                await asyncio.to_thread(
                    self.registry.put_manifest, t, art.manifest(self.source_url)
                )
                result.runs[key] = status
                log.info("gefs_run_published", run=key, status=status, tag=t)

        await asyncio.gather(*(one(r) for r in todo))
        return result
