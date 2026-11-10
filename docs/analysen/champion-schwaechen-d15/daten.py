"""Backtest-Prognosen beider Versionen aus der Registry als eine kleine Parquet-Datei.

Läuft im ml-train-Container auf der VM (Zugriff auf die Registry in Garage), die Auswertung
danach lokal mit ``auswertung.py``. Aufruf auf der VM::

    docker compose --profile ml run --rm --no-deps -T --entrypoint nice ml-train -n 19 \\
        python - --out /data/analysis/champion-schwaechen-d15/zeilen.parquet < daten.py

Die Datei enthält Prognosen und Ist-Werte aus dem eigenen Backtest (Ist aus SMARD bzw.
ENTSO-E über ``energy.value_best``); sie bleibt im Cache und kommt nicht ins Repository.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import pandas as pd

from ml_train.config import load_config
from ml_train.registry import Registry

VERSIONS = {
    "neu": ("config/ml/de_lu_v2.yaml", "de_lu_v2-wetter_frei-202611082227-53e301"),
    "v1": ("config/ml/de_lu_v1.yaml", "de_lu_v1-kalender-202610270115-7e355b"),
}
COLUMNS = ["issue_time", "ts", "lead_day", "target", "q10", "q50", "q90", "actual", "fold"]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True, type=Path)
    a = ap.parse_args()
    root = os.environ.get("PDB_ML_REGISTRY", "s3://ml-artifacts/models")
    parts = []
    for label, (cfg_path, version) in VERSIONS.items():
        cfg, _ = load_config(cfg_path)
        bt = Registry(root, cfg).backtest(version)
        if bt is None:
            raise SystemExit(f"kein Backtest für {version}")
        parts.append(bt[COLUMNS].assign(model=label, version=version))
    rows = pd.concat(parts, ignore_index=True)
    rows["model"] = rows["model"].astype("category")
    rows["target"] = rows["target"].astype("category")
    a.out.parent.mkdir(parents=True, exist_ok=True)
    rows.to_parquet(a.out, index=False, compression="zstd")
    print(json.dumps({"rows": len(rows), "out": str(a.out)}))


if __name__ == "__main__":
    main()
