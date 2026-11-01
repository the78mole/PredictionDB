"""Silber GFS: Int16 mit Scale/Offset (heute) gegen float16, komprimierte Größe und Fehler.

Zwei Messungen, beide mit dem Kompressor des Silbers (Blosc/Zstd 5, Byte-Shuffle):

``silber``  echte Läufe aus GHCR (``gfs-0p25-silver``, ``gfs-0p5-silver``), alle Schritte,
            Chunks wie im Store (48 Schritte). float16 entsteht aus den schon quantisierten
            Werten, das schmeichelt float16 etwas.
``quelle``  GRIB direkt aus dem AWS-Archiv (f000 bis f012, GFS DE), dekodiert wie in wx-process
            (Intervallwerte) und dann einmal als Int16, einmal als float16 abgelegt.

float16 in natürlichen Einheiten: °C statt K, hPa statt Pa (101 300 Pa liegt über dem
float16-Maximum 65 504), sonst wie Silber.

Auftrag: ``job.yaml`` im selben Ordner. Aufruf (aus dem Repo-Wurzelverzeichnis)::

    uv run --package wx-process python docs/analysen/silber-float16/auswertung.py silber /tmp/sf16
    uv run --package wx-process python docs/analysen/silber-float16/auswertung.py quelle /tmp/sf16
    uv run --package wx-process --with matplotlib python \\
        docs/analysen/silber-float16/auswertung.py bericht /tmp/sf16

``bericht`` liest nur die Tabellen und schreibt ``grafiken/groesse_je_gruppe.png`` sowie
``tabellen/lauf.json``.
"""

from __future__ import annotations

import asyncio
import csv
import hashlib
import io
import json
import subprocess
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


GROUPS: dict[str, list[str]] = {
    "Temperaturen": ["t2m", "d2m", "tsoil_0_10", "tsoil_10_40"],
    "Wind": ["u10", "v10", "u100", "v100", "gust"],
    "Strahlung": ["dswrf", "dlwrf", "sunshine"],
    "Feuchte, Wolken": ["r2m", "tcc", "tcc_mean", "soilw_0_10", "soilw_10_40"],
    "Niederschlag, Schnee": ["tp", "csnow", "csnow_mean", "weasd", "snod"],
    "Druck": ["prmsl", "sp"],
}


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


def read(name: str) -> list[dict[str, str]]:
    with (HERE / "tabellen" / f"{name}.csv").open() as f:
        return list(csv.DictReader(f))


def change(rows: list[dict[str, str]], variables: list[str] | None = None) -> float:
    sel = [r for r in rows if variables is None or r["variable"] in variables]
    i16 = sum(int(r["int16_bytes"]) for r in sel)
    f16 = sum(int(r["float16_bytes"]) for r in sel)
    return f16 / i16 - 1 if i16 else float("nan")


def report(workdir: Path) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    silver_rows, source_rows = read("silber"), read("quelle")
    columns = [
        ("Silber DE", [r for r in silver_rows if r["grid"] == "0p25"]),
        ("Silber Europa", [r for r in silver_rows if r["grid"] == "0p5"]),
    ] + [
        (f"Quelle {run[:7]}", [r for r in source_rows if r["run"] == run])
        for run in sorted({r["run"] for r in source_rows})
    ]
    groups = [*GROUPS, "alle", "alle ohne Druck"]
    no_pressure = [v for g, vs in GROUPS.items() if g != "Druck" for v in vs]
    fig, ax = plt.subplots(figsize=(9, 4.8))
    width = 0.8 / len(columns)
    for k, (label, rows) in enumerate(columns):
        values = []
        for g in groups:
            vs = GROUPS.get(g, no_pressure if g == "alle ohne Druck" else None)
            values.append(100 * change(rows, vs))
        ax.bar(
            [i + (k - (len(columns) - 1) / 2) * width for i in range(len(groups))],
            values,
            width,
            label=label,
        )
    ax.axhline(0, color="black", lw=0.8)
    ax.set_xticks(range(len(groups)), groups, rotation=20, ha="right")
    ax.set_ylabel("Größe float16 gegen Int16 in %")
    ax.set_title("Komprimierte Größe float16 gegen Int16 (Blosc/Zstd 5, Byte-Shuffle)")
    ax.legend(fontsize=8)
    ax.grid(axis="y", alpha=0.3)
    fig.tight_layout()
    (HERE / "grafiken").mkdir(exist_ok=True)
    fig.savefig(HERE / "grafiken" / "groesse_je_gruppe.png", dpi=130)
    job = (HERE / "job.yaml").read_bytes()
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"], capture_output=True, text=True
    ).stdout.strip()
    lauf = {
        "job": "silber-float16",
        "job_sha256": hashlib.sha256(job).hexdigest(),
        "git_commit": commit,
        "erstellt": datetime.now(UTC).isoformat(timespec="seconds"),
        "silber_laeufe": [f"{repo}:{tag}" for repo, tag in SILVER_RUNS],
        "quelle_laeufe": SOURCE_RUNS,
        "quelle_schritte_h": SOURCE_STEPS,
        "variablen": sorted({r["variable"] for r in silver_rows}),
        "bytes": {
            label: {
                "int16": sum(int(r["int16_bytes"]) for r in rows),
                "float16": sum(int(r["float16_bytes"]) for r in rows),
                "float32": sum(int(r["float32_bytes"]) for r in rows),
            }
            for label, rows in columns
        },
        "schritte_zeitlich_interpoliert": {},
    }
    (HERE / "tabellen" / "lauf.json").write_text(
        json.dumps(lauf, indent=1, ensure_ascii=False) + "\n"
    )
    print(json.dumps({"grafik": "grafiken/groesse_je_gruppe.png", "lauf": "tabellen/lauf.json"}))


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
    {"silber": silver, "quelle": source, "bericht": report}[mode](workdir)
