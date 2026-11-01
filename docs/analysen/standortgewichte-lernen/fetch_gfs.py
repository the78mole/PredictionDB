"""GFS-0p5-Silber (00Z, f003..f024) für 2024-2025 laden: u100, v100, dswrf, t2m."""

import datetime as dt
import io
import json
import sys
import tarfile
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numcodecs
import numpy as np

R = "the78mole/predictiondb-data/gfs-0p5-silver"
OUT = sys.argv[1]
TOK = Path(sys.argv[2]).read_text().strip()
VARS = {"u100": (0.01, 0.0), "v100": (0.01, 0.0), "dswrf": (0.05, 0.0), "t2m": (0.01, 273.15)}
blosc = numcodecs.Blosc()


def get(url, accept=None):
    req = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {TOK}", **({"Accept": accept} if accept else {})}
    )
    for _ in range(5):
        try:
            return urllib.request.urlopen(req, timeout=120).read()
        except Exception as e:
            err = e
    raise err


days = [dt.date(2024, 1, 1) + dt.timedelta(d) for d in range(731)]
idx = {d: i for i, d in enumerate(days)}
arr = {
    v: np.lib.format.open_memmap(f"{OUT}/{v}.npy", "w+", np.float32, (len(days), 8, 75, 112))
    for v in VARS
}
for v in arr:
    arr[v][:] = np.nan
ok = np.zeros(len(days), bool)
layers = []
for y in (2024, 2025):
    for m in range(1, 13):
        man = json.loads(
            get(
                f"https://ghcr.io/v2/{R}/manifests/{y}-{m:02d}-00z",
                "application/vnd.oci.image.manifest.v1+json",
            )
        )
        for layer in man["layers"]:
            layers.append(
                (
                    dt.date.fromisoformat(layer["annotations"]["org.predictiondb.run"][:10]),
                    layer["digest"],
                )
            )


def one(item):
    d, dig = item
    if d not in idx:
        return
    with tarfile.open(fileobj=io.BytesIO(get(f"https://ghcr.io/v2/{R}/blobs/{dig}"))) as tar:
        members = {v: tar.extractfile(f"{v}/c/0/0/0").read() for v in VARS}
    for v, (sc, off) in VARS.items():
        raw = members[v]
        a = np.frombuffer(blosc.decode(raw), "<i2").reshape(48, 75, 112)[1:9].astype(np.float32)
        a = np.where(a == -32768, np.nan, a * sc + off)
        arr[v][idx[d]] = a
    ok[idx[d]] = True


with ThreadPoolExecutor(12) as ex:
    for n, _ in enumerate(ex.map(one, layers)):
        if n % 100 == 0:
            print(n, len(layers), flush=True)
np.save(f"{OUT}/ok.npy", ok)
for v in arr:
    arr[v].flush()
print("fertig", ok.sum(), "von", len(days))
