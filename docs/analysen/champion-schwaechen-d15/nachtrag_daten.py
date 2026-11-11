"""Nachtrag: Prognosen der Gegenproben für die Faltung 01-06/2024 als kleine Parquet-Datei.

Läuft im ml-train-Container auf der VM, nach den Backtests aus ``nachtrag-job.yaml``::

    docker compose --profile ml run --rm --no-deps -T --entrypoint nice ml-train -n 19 \\
        python - --out /data/analysis/champion-schwaechen-d15/nachtrag.parquet < nachtrag_daten.py
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

RUNS = {
    "ab2018": ("/data/analysis/champion-schwaechen-d15/bt-ab2018", "wetter_frei_ab2018"),
    "ohne_kal": ("/data/analysis/champion-schwaechen-d15/bt-ohne-kal", "wetter_frei"),
}
COLUMNS = ["issue_time", "ts", "lead_day", "target", "q10", "q50", "q90", "actual"]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True, type=Path)
    a = ap.parse_args()
    parts = []
    for label, (folder, method) in RUNS.items():
        p = pd.read_parquet(Path(folder) / "predictions.parquet")
        p = p[p["method"] == method][COLUMNS]
        parts.append(p.assign(model=label))
    rows = pd.concat(parts, ignore_index=True)
    rows.to_parquet(a.out, index=False, compression="zstd")
    print(json.dumps({"rows": len(rows), "out": str(a.out)}))


if __name__ == "__main__":
    main()
