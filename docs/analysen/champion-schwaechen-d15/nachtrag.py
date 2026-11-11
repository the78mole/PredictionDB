"""Nachtrag: Gegenproben zur Faltung 01-06/2024, Auftrag in ``nachtrag-job.yaml``.

Eingänge: ``zeilen.parquet`` (``daten.py``) und ``nachtrag.parquet`` (``nachtrag_daten.py``).
Aufruf aus dem Repository-Wurzelverzeichnis::

    uv run python docs/analysen/champion-schwaechen-d15/nachtrag.py \\
        --cache .ml-data/champion-schwaechen-d15
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from ml_train.metrics import add_context, pinball

OUT = Path(__file__).parent
JOB = OUT / "nachtrag-job.yaml"
MODELS = ["champion", "ohne_kal", "ab2018", "v1"]
COLUMNS = ["issue_time", "ts", "lead_day", "target", "q10", "q50", "q90", "actual", "model"]


def load(cache: Path) -> pd.DataFrame:
    z = pd.read_parquet(cache / "zeilen.parquet")
    z["model"] = z["model"].astype(str).map({"neu": "champion", "v1": "v1"})
    n = pd.read_parquet(cache / "nachtrag.parquet")
    rows = pd.concat([z[COLUMNS], n[COLUMNS]], ignore_index=True)
    rows["target"] = rows["target"].astype(str)
    issue = pd.DatetimeIndex(rows["issue_time"])
    fold0 = (issue >= pd.Timestamp("2024-01-01", tz="UTC")) & (
        issue < pd.Timestamp("2024-07-01", tz="UTC")
    )
    return rows[fold0 & rows["lead_day"].between(1, 7)]


def wide(rows: pd.DataFrame) -> pd.DataFrame:
    key = ["target", "issue_time", "ts", "lead_day"]
    w = rows.pivot_table(index=key, columns="model", values=["q10", "q50", "q90", "actual"])
    w.columns = [f"{v}_{m}" for v, m in w.columns]
    w = w.reset_index().dropna(subset=[f"q50_{m}" for m in MODELS])
    w["actual"] = w["actual_v1"]
    return add_context(w)


def compare(w: pd.DataFrame) -> pd.DataFrame:
    out = []
    for target in ("da_price_de_lu", "load_actual"):
        t = w[w["target"] == target]
        groups = {
            "D+1 bis D+7, 01-06/2024": t,
            "D+6/D+7, Frühling 2024": t[(t["season"] == "Frühling") & t["lead_day"].isin([6, 7])],
        }
        for label, s in groups.items():
            y = s["actual"].to_numpy()
            ref = None
            for m in ["v1", "champion", "ohne_kal", "ab2018"]:
                q = [s[f"{c}_{m}"].to_numpy() for c in ("q10", "q50", "q90")]
                pb = (pinball(y, q[0], 0.1) + pinball(y, q[1], 0.5) + pinball(y, q[2], 0.9)) / 3
                row: dict[str, Any] = {
                    "target": target,
                    "gruppe": label,
                    "verfahren": m,
                    "n": len(s),
                    "pinball": pb.mean(),
                    "mae": np.abs(q[1] - y).mean(),
                    "bias": (q[1] - y).mean(),
                    "abdeckung": ((y >= q[0]) & (y <= q[2])).mean(),
                    "breite": (q[2] - q[0]).mean(),
                }
                ref = row["pinball"] if m == "v1" else ref
                row["mehr_pinball_gegen_v1"] = row["pinball"] / ref - 1
                out.append(row)
    return pd.DataFrame(out)


def median_shift(w: pd.DataFrame) -> pd.DataFrame:
    """Wie oft weicht der Median des Champions von dem desselben Modells ohne Kalibrierung ab?

    Beide Läufe sind bis auf die Kalibrierung gleich (Snapshot, Konfiguration, Seed). Weicht der
    Median ab, hat ``QuantileModel.predict`` nach der Verschiebung die Quantile umsortiert, oder
    die verschobenen Stufe-1-Mediane der Last, des Winds und der PV gingen in den Preis ein.
    """
    out = []
    for target, t in w.groupby("target"):
        d = t["q50_champion"] - t["q50_ohne_kal"]
        tol = 1e-3 * t["q50_ohne_kal"].abs().clip(lower=1.0)
        out.append(
            {
                "target": target,
                "n": len(t),
                "anteil_median_verschoben": float((d.abs() > tol).mean()),
                "mittlere_verschiebung": float(d.mean()),
            }
        )
    return pd.DataFrame(out)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache", required=True, type=Path)
    a = ap.parse_args()
    started = datetime.now(UTC)
    rows = load(a.cache)
    w = wide(rows)
    tab = OUT / "tabellen"
    compare(w).round(4).to_csv(tab / "nachtrag_vergleich.csv", index=False)
    median_shift(w).round(4).to_csv(tab / "nachtrag_median.csv", index=False)
    commit = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True)
    info = {
        "job": "champion-schwaechen-d15 (Nachtrag)",
        "git_commit": commit.stdout.strip(),
        "job_sha256": hashlib.sha256(JOB.read_bytes()).hexdigest(),
        "gestartet": started.isoformat(timespec="seconds"),
        "beendet": datetime.now(UTC).isoformat(timespec="seconds"),
        "backtests": {
            "ab2018": "2026-11-11T00:13:50Z bis 01:25:47Z, rc 0",
            "ohne_kal": "2026-11-11T01:25:47Z bis 02:10:23Z, rc 0",
            "snapshot": "/data/snap-v2-d15-20261108b",
        },
        "umfang": {
            "zeilen_eingang": len(rows),
            "gemeinsame_zeilen": len(w),
            "ausgabe_von": str(w["issue_time"].min()),
            "ausgabe_bis": str(w["issue_time"].max()),
        },
    }
    (tab / "lauf-nachtrag.json").write_text(json.dumps(info, indent=1, ensure_ascii=False))
    print(json.dumps(info["umfang"]))


if __name__ == "__main__":
    main()
