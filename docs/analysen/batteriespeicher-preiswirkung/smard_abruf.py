"""Stundenwerte von SMARD (Bundesnetzagentur, CC BY 4.0) ab 2019 als JSON je Reihe.

Filter-IDs wie in ``services/energy_ingest/src/energy_ingest/sources/smard.py``. Aufruf::

    uv run --no-project --with requests python smard_abruf.py ./speicher-daten/smard
"""

import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import requests  # type: ignore[import-untyped]

B = "https://www.smard.de/app/chart_data"
F = {
    4169: ("DE-LU", "price"),
    410: ("DE", "load"),
    4359: ("DE", "residual"),
    4068: ("DE", "solar"),
    4067: ("DE", "wind_on"),
    1225: ("DE", "wind_off"),
    4066: ("DE", "biomass"),
    1226: ("DE", "hydro"),
    1228: ("DE", "other_re"),
    4070: ("DE", "pumped_gen"),
    1223: ("DE", "lignite"),
    4069: ("DE", "hard_coal"),
    4071: ("DE", "gas"),
    1227: ("DE", "other_conv"),
}
OUT = Path(sys.argv[1])
OUT.mkdir(parents=True, exist_ok=True)
START = 1546300800000  # 2019-01-01 UTC
s = requests.Session()


def get(u: str) -> dict[str, Any]:
    for i in range(6):
        try:
            r = s.get(u, timeout=60)
            r.raise_for_status()
            return dict(r.json())
        except Exception:
            time.sleep(2**i)
    raise RuntimeError(u)


def eine(fid: int) -> tuple[str, int]:
    reg, name = F[fid]
    idx = [
        t
        for t in get(f"{B}/{fid}/{reg}/index_hour.json")["timestamps"]
        if t >= START - 8 * 86400000
    ]
    rows = {}
    for t in idx:
        for ts, v in get(f"{B}/{fid}/{reg}/{fid}_{reg}_hour_{t}.json")["series"]:
            if v is not None:
                rows[ts] = v
    (OUT / f"{name}.json").write_text(json.dumps(rows))
    return name, len(rows)


with ThreadPoolExecutor(4) as ex:
    for r in ex.map(eine, F):
        print(r, flush=True)
