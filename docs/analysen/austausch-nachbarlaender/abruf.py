"""Rohdaten für austausch-nachbarlaender laden (nur lokal, nicht ins Repo).

- GFS-0p5-Silber von GHCR (öffentlich, CC BY 4.0): Läufe 00Z vom 1.1.2024 bis 31.12.2025,
  Schritte f000 bis f024, Größen u100, v100, dswrf, t2m -> CACHE/gfs/<größe>.npy
  (Tag x Schritt x Breite x Länge, float32), CACHE/gfs/ok.npy, CACHE/gfs/layer.json (Digests).
- Energy-Charts (CC BY 4.0): /public_power je Land und Jahr, /installed_power jährlich,
  /cbet für DE je Jahr, /price DE-LU je Jahr -> CACHE/ec/*.json

Aufruf: uv run --no-project --with numpy --with numcodecs python -I \
    docs/analysen/austausch-nachbarlaender/abruf.py --cache /tmp/an
"""

from __future__ import annotations

import argparse
import datetime as dt
import io
import json
import tarfile
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numcodecs  # type: ignore[import-untyped]
import numpy as np

REPO = "the78mole/predictiondb-data/gfs-0p5-silver"
EC = "https://api.energy-charts.info"
LAENDER = ("de", "at", "be", "ch", "cz", "dk", "fr", "nl", "no", "pl", "se")
JAHRE = (2024, 2025)
#: Größe -> (scale_factor, add_offset) wie in den zarr.json des Silbers
GROESSEN = {"u100": (0.01, 0.0), "v100": (0.01, 0.0), "dswrf": (0.05, 0.0), "t2m": (0.01, 273.15)}
TAGE = [dt.date(2024, 1, 1) + dt.timedelta(d) for d in range(731)]
SCHRITTE = 9  # f000 bis f024
MANIFEST = "application/vnd.oci.image.manifest.v1+json"


def hole(url: str, token: str | None = None, accept: str | None = None) -> bytes:
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if accept:
        headers["Accept"] = accept
    req = urllib.request.Request(url, headers=headers)
    fehler: Exception | None = None
    for _ in range(5):
        try:
            with urllib.request.urlopen(req, timeout=300) as r:
                data: bytes = r.read()
                return data
        except Exception as exc:  # Netzfehler: bis zu 5 Versuche
            fehler = exc
    assert fehler is not None
    raise fehler


def energy_charts(ziel: Path) -> None:
    ziel.mkdir(parents=True, exist_ok=True)
    for jahr in JAHRE:
        zeit = f"start={jahr}-01-01T00:00Z&end={jahr}-12-31T23:59Z"
        for land in LAENDER:
            datei = ziel / f"pp_{land}_{jahr}.json"
            if not datei.exists():
                datei.write_bytes(hole(f"{EC}/public_power?country={land}&{zeit}"))
        for name, url in (
            (f"cbet_de_{jahr}.json", f"{EC}/cbet?country=de&{zeit}"),
            (f"price_de_lu_{jahr}.json", f"{EC}/price?bzn=DE-LU&{zeit}"),
        ):
            if not (ziel / name).exists():
                (ziel / name).write_bytes(hole(url))
    for land in LAENDER:
        url = (
            f"{EC}/installed_power?country={land}&time_step=yearly&installation_decommission=false"
        )
        if not (ziel / f"ipy_{land}.json").exists():
            (ziel / f"ipy_{land}.json").write_bytes(hole(url))


def gfs(ziel: Path) -> None:
    ziel.mkdir(parents=True, exist_ok=True)
    token = json.loads(hole(f"https://ghcr.io/token?scope=repository:{REPO}:pull"))["token"]
    index = {d: i for i, d in enumerate(TAGE)}
    form = (len(TAGE), SCHRITTE, 75, 112)
    felder = {
        g: np.lib.format.open_memmap(ziel / f"{g}.npy", "w+", np.float32, form) for g in GROESSEN
    }
    for a in felder.values():
        a[:] = np.nan
    ok = np.zeros(len(TAGE), bool)
    layer: list[tuple[dt.date, str]] = []
    for jahr in JAHRE:
        for monat in range(1, 13):
            url = f"https://ghcr.io/v2/{REPO}/manifests/{jahr}-{monat:02d}-00z"
            man = json.loads(hole(url, token, MANIFEST))
            for eintrag in man["layers"]:
                lauf = dt.date.fromisoformat(eintrag["annotations"]["org.predictiondb.run"][:10])
                if lauf in index:
                    layer.append((lauf, eintrag["digest"]))
    blosc = numcodecs.Blosc()

    def eins(item: tuple[dt.date, str]) -> None:
        tag, digest = item
        blob = hole(f"https://ghcr.io/v2/{REPO}/blobs/{digest}", token)
        with tarfile.open(fileobj=io.BytesIO(blob)) as tar:
            roh = {}
            for g in GROESSEN:
                f = tar.extractfile(f"{g}/c/0/0/0")  # erster Schritt-Chunk: f000 bis f141
                assert f is not None
                roh[g] = f.read()
        for g, (skala, versatz) in GROESSEN.items():
            werte = np.frombuffer(blosc.decode(roh[g]), "<i2").reshape(48, 75, 112)[:SCHRITTE]
            felder[g][index[tag]] = np.where(werte == -32768, np.nan, werte * skala + versatz)
        ok[index[tag]] = True

    with ThreadPoolExecutor(12) as ex:
        list(ex.map(eins, layer))
    for a in felder.values():
        a.flush()
    np.save(ziel / "ok.npy", ok)
    (ziel / "layer.json").write_text(json.dumps({str(t): d for t, d in sorted(layer)}, indent=0))
    print(f"GFS: {int(ok.sum())} von {len(TAGE)} Läufen")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--cache", type=Path, required=True)
    p.add_argument("--nur-ec", action="store_true", help="nur Energy-Charts laden")
    args = p.parse_args()
    energy_charts(args.cache / "ec")
    if not args.nur_ec:
        gfs(args.cache / "gfs")


if __name__ == "__main__":
    main()
