"""Silber GFS: Int16 mit Scale/Offset (heute) gegen float16, komprimierte Größe und Fehler.

Zwei Messungen, beide mit dem Kompressor des Silbers (Blosc/Zstd 5, Byte-Shuffle):

``silber``  echte Läufe aus GHCR (``gfs-0p25-silver``, ``gfs-0p5-silver``), alle Schritte,
            Chunks wie im Store (48 Schritte). float16 entsteht aus den schon quantisierten
            Werten, das schmeichelt float16 etwas.
``quelle``  GRIB direkt aus dem AWS-Archiv (f000 bis f012, GFS DE), dekodiert wie in wx-process
            (Intervallwerte) und dann einmal als Int16, einmal als float16 abgelegt.

float16 in natürlichen Einheiten: °C statt K, hPa statt Pa (101 300 Pa liegt über dem
float16-Maximum 65 504), sonst wie Silber.

Aufruf (aus dem Repo-Wurzelverzeichnis)::

    uv run --package wx-process python docs/analysen/silber-float16/auswertung.py silber /tmp/sf16
    uv run --package wx-process python docs/analysen/silber-float16/auswertung.py quelle /tmp/sf16
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
import sys
import tarfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import numpy as np
import zarr
from numpy.typing import NDArray
from zarr.codecs import BloscCodec
from zarr.storage import MemoryStore

HERE = Path(__file__).parent
REGISTRY = "https://ghcr.io"
NAMESPACE = "the78mole/predictiondb-data"
#: (Paket, Tag), je Paket der erste Lauf des Monats
SILVER_RUNS = [
    ("gfs-0p25-silver", "2025-01-00z"),
    ("gfs-0p25-silver", "2025-04-00z"),
    ("gfs-0p25-silver", "2025-07-00z"),
    ("gfs-0p25-silver", "2025-10-00z"),
    ("gfs-0p25-silver", "2026-10-12z"),
    ("gfs-0p5-silver", "2026-01-00z"),
    ("gfs-0p5-silver", "2026-07-00z"),
]
SOURCE_RUNS = ["2025-01-01T00:00", "2025-07-01T00:00"]
SOURCE_STEPS = list(range(13))


def packed(a: NDArray[Any], chunk: int) -> int:
    """Bytes der Chunks nach Blosc/Zstd 5 mit Byte-Shuffle (wie ``wx_process.silver``)."""
    st = MemoryStore()
    z = zarr.create_array(
        st,
        shape=a.shape,
        dtype=a.dtype,
        chunks=(chunk, *a.shape[1:]),
        compressors=[BloscCodec(cname="zstd", clevel=5, shuffle="shuffle")],
        fill_value=0,
        zarr_format=3,
    )
    z[...] = a
    return sum(len(v.to_bytes()) for k, v in st._store_dict.items() if k.startswith("c/"))


def natural(x: NDArray[np.float64], units: str) -> tuple[NDArray[np.float64], float, float]:
    """Werte in °C/hPa und die Rückrechnung (Faktor, Summand) in die Silber-Einheit."""
    if units == "Pa":
        return x / 100.0, 100.0, 0.0
    if units == "K":
        return x - 273.15, 1.0, 273.15
    return x, 1.0, 0.0


def compare(
    name: str, units: str, x: NDArray[np.float64], i16: NDArray[np.int16], chunk: int
) -> dict[str, Any]:
    nat, f, s = natural(x, units)
    f16 = nat.astype(np.float16)
    back = f16.astype(np.float64) * f + s
    ok = np.isfinite(x)
    err = np.abs(back[ok] - x[ok])
    return {
        "variable": name,
        "int16_bytes": packed(i16, chunk),
        "float16_bytes": packed(f16, chunk),
        "float32_bytes": packed(x.astype(np.float32), chunk),
        "float16_max_err": float(err.max()) if err.size else 0.0,
        "float16_rmse": float(np.sqrt((err**2).mean())) if err.size else 0.0,
    }


def token(repo: str) -> str:
    r = httpx.get(f"{REGISTRY}/token", params={"scope": f"repository:{NAMESPACE}/{repo}:pull"})
    return str(r.json()["token"])


def silver(workdir: Path) -> None:
    rows: dict[tuple[str, str], dict[str, Any]] = {}
    for repo, tag in SILVER_RUNS:
        t = token(repo)
        h = {"Authorization": f"Bearer {t}", "Accept": "application/vnd.oci.image.manifest.v1+json"}
        m = httpx.get(f"{REGISTRY}/v2/{NAMESPACE}/{repo}/manifests/{tag}", headers=h).json()
        layer = m["layers"][0]
        target = workdir / f"{repo}-{tag}"
        if not target.exists():
            blob = httpx.get(
                f"{REGISTRY}/v2/{NAMESPACE}/{repo}/blobs/{layer['digest']}",
                headers={"Authorization": f"Bearer {t}"},
                follow_redirects=True,
                timeout=300,
            ).content
            with tarfile.open(fileobj=io.BytesIO(blob)) as tar:
                tar.extractall(target, filter="data")
        grid = "0p5" if "0p5" in repo else "0p25"
        g = zarr.open_group(str(target), mode="r")
        for name in sorted(g.array_keys()):
            if name in ("step", "lat", "lon"):
                continue
            arr = g[name]
            assert isinstance(arr, zarr.Array)
            at = dict(arr.attrs)
            raw = np.asarray(arr[...]).astype(np.int16)
            x = raw.astype(np.float64) * float(at["scale_factor"]) + float(at["add_offset"])  # type: ignore[arg-type]
            x[raw == -32768] = np.nan
            r = compare(name, str(at["units"]), x, raw, int(arr.chunks[0]))
            acc = rows.setdefault(
                (grid, name),
                {"grid": grid, **{k: 0 for k in r if k.endswith("bytes")}, "float16_max_err": 0.0},
            )
            for k in r:
                if k.endswith("bytes"):
                    acc[k] += r[k]
            acc["variable"] = name
            acc["float16_max_err"] = max(acc["float16_max_err"], r["float16_max_err"])
    write(HERE / "tabellen" / "silber.csv", list(rows.values()))


async def _fetch(c: httpx.AsyncClient, cfg: Any, run: datetime, step: int) -> tuple[int, bytes]:
    from gribkit.grib import crop
    from gribkit.idx import parse_idx, select

    u = (
        f"https://noaa-gfs-bdp-pds.s3.amazonaws.com/gfs.{run:%Y%m%d}/{run:%H}/atmos/"
        f"gfs.t{run:%H}z.{cfg.product}.f{step:03d}"
    )
    total = int((await c.head(u)).headers["content-length"])
    entries = select(parse_idx((await c.get(u + ".idx")).text, total), cfg.specs())
    parts = []
    for e in entries:
        assert e.length is not None
        r = await c.get(u, headers={"Range": f"bytes={e.offset}-{e.offset + e.length - 1}"})
        if r.status_code != 206:
            raise RuntimeError(f"{u}: HTTP {r.status_code}")
        parts.append(r.content)
    return step, crop(b"".join(parts), cfg.bbox.bbox())


def source(workdir: Path) -> None:
    from wx_ingest.config import GfsConfig
    from wx_process.decode import decode_step
    from wx_process.silver import assemble, encode
    from wx_process.variables import BY_NAME

    cfg = GfsConfig.from_yaml("config/sources/gfs_0p25.yaml")
    rows: list[dict[str, Any]] = []
    for text in SOURCE_RUNS:
        run = datetime.fromisoformat(text).replace(tzinfo=UTC)

        async def main(run: datetime = run) -> list[tuple[int, bytes]]:
            async with httpx.AsyncClient(timeout=120) as c:
                return await asyncio.gather(*(_fetch(c, cfg, run, s) for s in SOURCE_STEPS))

        got = asyncio.run(main())
        ds = assemble("gfs", "0p25", run, {s: decode_step(g, s) for s, g in got})
        for name, a in ds.data.items():
            x = a.astype(np.float64)
            r = compare(name, BY_NAME[name].units, x, encode(a, name), len(SOURCE_STEPS))
            rows.append({"run": f"{run:%Y-%m-%d %HZ}", **r})
    write(HERE / "tabellen" / "quelle.csv", rows)


def write(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    print(json.dumps({"datei": str(path), "zeilen": len(rows)}))


if __name__ == "__main__":
    mode, workdir = sys.argv[1], Path(sys.argv[2])
    workdir.mkdir(parents=True, exist_ok=True)
    {"silber": silver, "quelle": source}[mode](workdir)
