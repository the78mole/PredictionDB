"""Abgleich zweier Trainings-Snapshots: aus GHCR (ohne Datenbank) gegen den Export der VM.

Auftrag in job.yaml. Aufruf::

    uv run --no-project --with pandas --with pyarrow --with matplotlib --with pyyaml \\
        python docs/analysen/snapshot-ghcr-abgleich/auswertung.py \\
        --vm /tmp/snap-vm --ghcr /tmp/snap-ghcr

Beide Ordner enthalten ``energy.parquet``, ``weather.parquet``, ``capacity.parquet`` und
``manifest.json`` (``ml-train snapshot`` bzw. ``ml-train snapshot --ghcr``). Geschrieben werden
nur Kennzahlen (tabellen/*.csv, lauf.json) und eine Grafik, keine Rohdaten.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd

ORDNER = Path(__file__).resolve().parent
TOL = 1e-4


def lade(p: Path) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, dict[str, Any]]:
    energy = pd.read_parquet(p / "energy.parquet")
    weather = pd.read_parquet(p / "weather.parquet")
    capacity = pd.read_parquet(p / "capacity.parquet")
    manifest = json.loads((p / "manifest.json").read_text())
    for col in ("ts", "published"):
        energy[col] = pd.to_datetime(energy[col], utc=True)
    for col in ("run_time", "valid_time"):
        weather[col] = pd.to_datetime(weather[col], utc=True)
    weather["feature"] = weather["feature"].astype(str)
    capacity["day"] = pd.to_datetime(capacity["day"]).dt.date
    return energy, weather, capacity, manifest


def vergleich(a: pd.DataFrame, b: pd.DataFrame, keys: list[str], group: str) -> pd.DataFrame:
    """Kennzahlen je Gruppe: Zeilen in a (VM), b (GHCR), gemeinsam, Abweichung der Werte."""
    m = a[[*keys, "value"]].merge(
        b[[*keys, "value"]],
        on=keys,
        how="outer",
        suffixes=("_vm", "_ghcr"),
        indicator=True,
        validate="one_to_one",  # doppelte Schlüssel brechen ab, statt Paare zu zählen
    )
    rows = []
    for g, d in m.groupby(group, sort=True):
        both = d[d["_merge"] == "both"]
        diff = (both["value_ghcr"].astype(float) - both["value_vm"].astype(float)).abs()
        scale = both["value_vm"].astype(float).abs().clip(lower=1.0)
        std = float(both["value_vm"].astype(float).std()) if len(both) > 1 else float("nan")
        rows.append(
            {
                group: g,
                "n_vm": int((d["_merge"] != "right_only").sum()),
                "n_ghcr": int((d["_merge"] != "left_only").sum()),
                "n_both": len(both),
                "only_vm": int((d["_merge"] == "left_only").sum()),
                "only_ghcr": int((d["_merge"] == "right_only").sum()),
                "mae": float(diff.mean()) if len(both) else float("nan"),
                "max_abs": float(diff.max()) if len(both) else float("nan"),
                "mae_rel_std": float(diff.mean() / std) if len(both) and std > 0 else float("nan"),
                "share_exact": float((diff <= TOL * scale).mean()) if len(both) else float("nan"),
            }
        )
    return pd.DataFrame(rows)


def gruppe(feature: str) -> str:
    for p, name in (("nb_", "nachbarzonen"), ("st_", "standorte"), ("en_", "gefs")):
        if feature.startswith(p):
            return name
    return "de_cluster"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--vm", required=True, type=Path)
    ap.add_argument("--ghcr", required=True, type=Path)
    a = ap.parse_args()
    gestartet = datetime.now(UTC).isoformat(timespec="seconds")
    ev, wv, cv, mv = lade(a.vm)
    eg, wg, cg, mg = lade(a.ghcr)
    # beide auf denselben Zeitraum begrenzen (der kürzere zählt)
    lo = max(pd.Timestamp(mv["range"][0]), pd.Timestamp(mg["range"][0]))
    hi = min(pd.Timestamp(mv["range"][1]), pd.Timestamp(mg["range"][1]))
    ev, eg = (e[(e["ts"] >= lo) & (e["ts"] < hi)] for e in (ev, eg))
    wv, wg = (w[(w["run_time"] >= lo) & (w["run_time"] < hi)] for w in (wv, wg))

    tab = ORDNER / "tabellen"
    gra = ORDNER / "grafiken"
    tab.mkdir(exist_ok=True)
    gra.mkdir(exist_ok=True)

    energie = vergleich(ev, eg, ["code", "ts"], "code")
    energie.to_csv(tab / "energie.csv", index=False, float_format="%.6g")
    wetter = vergleich(wv, wg, ["feature", "run_time", "valid_time"], "feature")
    wetter.insert(1, "gruppe", wetter["feature"].map(gruppe))
    wetter.to_csv(tab / "wetter.csv", index=False, float_format="%.6g")
    agg = (
        wetter.groupby("gruppe")
        .agg(
            features=("feature", "count"),
            n_vm=("n_vm", "sum"),
            n_ghcr=("n_ghcr", "sum"),
            n_both=("n_both", "sum"),
            only_vm=("only_vm", "sum"),
            only_ghcr=("only_ghcr", "sum"),
            mae_rel_std_median=("mae_rel_std", "median"),
            mae_rel_std_max=("mae_rel_std", "max"),
            share_exact_median=("share_exact", "median"),
        )
        .reset_index()
    )
    agg.to_csv(tab / "wetter_gruppen.csv", index=False, float_format="%.6g")
    cap_v = cv.rename(columns={"capacity_mw": "value"})
    cap_g = cg.rename(columns={"capacity_mw": "value"})
    bestand = vergleich(cap_v, cap_g, ["day", "technology"], "technology")
    last_v = cv.sort_values("day").groupby("technology")["capacity_mw"].last()
    last_g = cg.sort_values("day").groupby("technology")["capacity_mw"].last()
    bestand["letzter_tag_vm_mw"] = bestand["technology"].map(last_v)
    bestand["letzter_tag_ghcr_mw"] = bestand["technology"].map(last_g)
    bestand.to_csv(tab / "bestand.csv", index=False, float_format="%.6g")
    quellen = pd.DataFrame(
        [
            {"quelle": k, "vm": mv.get("sources", {}).get(k), "ghcr": mg.get("sources", {}).get(k)}
            for k in sorted(set(mv.get("sources", {})) | set(mg.get("sources", {})))
        ]
    )
    quellen.to_csv(tab / "quellen.csv", index=False)

    w = wetter.dropna(subset=["mae_rel_std"]).sort_values("mae_rel_std")
    fig, ax = plt.subplots(figsize=(8, max(3.0, 0.16 * len(w))))
    colors = {
        "de_cluster": "#2a6fdb",
        "nachbarzonen": "#e07b39",
        "standorte": "#6aa84f",
        "gefs": "#8e63ce",
    }
    ax.barh(w["feature"], w["mae_rel_std"], color=[colors[g] for g in w["gruppe"]])
    ax.set_xscale("symlog", linthresh=1e-4)
    ax.set_xlabel("MAE / Standardabweichung (VM)")
    ax.set_title("Abweichung GHCR gegen VM je Wetter-Feature")
    ax.tick_params(axis="y", labelsize=6)
    fig.tight_layout()
    fig.savefig(gra / "wetter_abweichung.png", dpi=150)

    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=False, cwd=ORDNER
    )
    info = {
        "job": "snapshot-ghcr-abgleich",
        "job_sha256": hashlib.sha256((ORDNER / "job.yaml").read_bytes()).hexdigest(),
        "git_commit": commit.stdout.strip(),
        "gestartet": gestartet,
        "beendet": datetime.now(UTC).isoformat(timespec="seconds"),
        "zeitraum": [lo.isoformat(), hi.isoformat()],
        "snapshot_vm": mv.get("snapshot_id"),
        "snapshot_ghcr": mg.get("snapshot_id"),
        "clustering_vm": mv.get("clustering_version"),
        "clustering_ghcr": mg.get("clustering_version"),
        "zeilen": {
            "energie_vm": len(ev),
            "energie_ghcr": len(eg),
            "wetter_vm": len(wv),
            "wetter_ghcr": len(wg),
            "bestand_vm": len(cv),
            "bestand_ghcr": len(cg),
        },
        "reihen": int(energie["code"].nunique()),
        "features": int(wetter["feature"].nunique()),
        "toleranz_exakt": TOL,
        "ghcr_pakete": {k: len(v) for k, v in mg.get("ghcr", {}).get("packages", {}).items()},
        "ghcr_fehlend": mg.get("ghcr", {}).get("missing", []),
    }
    (tab / "lauf.json").write_text(json.dumps(info, indent=1, ensure_ascii=False))
    print(json.dumps(info, indent=1, ensure_ascii=False))
    print(agg.to_string())
    print(energie.sort_values("mae", ascending=False).head(15).to_string())
    print(bestand.to_string())


if __name__ == "__main__":
    main()
