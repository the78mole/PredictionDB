"""Nur lesend: Manifeste, Merkmale und Merkmalsbeiträge der aktiven Versionen für den 29.10.2026.

Baut die Merkmale für den Ausgabezeitpunkt 28.10.2026 08 UTC wie ``ml-train predict`` und zerlegt
den P50 des Preismodells je Version mit LightGBM ``pred_contrib`` (SHAP-Werte) in Beiträge je
Merkmal, umgerechnet in EUR/MWh (Skalierung des Modells). Der P50 ist Zentrum (Mittel des letzten
bekannten Liefertags) + Grundwert + Summe der Beiträge. Aufruf auf der VM im ml-train-Image::

    docker compose --profile ml run --rm -T --no-deps --entrypoint nice ml-train -n 19 \
        python - < merkmalsbeitraege.py > modelle.json

Aus der JSON-Ausgabe entstanden daten/modelle.csv, merkmal*.csv und stufe1_prognose_29_10.csv.
"""

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from ml_train.config import load_config
from ml_train.dataset import FeatureBuilder
from ml_train.predict import live_snapshot
from ml_train.registry import Registry
from predictiondb_core.db import create_db_engine

ISSUE = pd.Timestamp("2026-10-28T08:00", tz="UTC")
DAY0 = pd.Timestamp("2026-10-29", tz="Europe/Berlin").tz_convert("UTC")
DAY1 = DAY0 + pd.Timedelta(days=1)
REG = "s3://ml-artifacts/models"
cache = Path("/tmp/ml-cache-analyse")
engine = create_db_engine()
out = {"issue": str(ISSUE), "configs": {}}

for path in ["config/ml/de_lu_v1.yaml", "config/ml/de_lu_v2.yaml"]:
    cfg, sha = load_config(path)
    reg = Registry(REG, cfg)
    st = reg.state()
    res = {"state": st, "active": reg.active(), "versions": {}}
    snap = live_snapshot(engine, cfg, ISSUE, weather=True)
    fb = FeatureBuilder(snap, cfg)
    frame = fb.build_at([ISSUE])
    ts = pd.to_datetime(frame.meta["ts"], utc=True)
    sel = ((ts >= DAY0) & (ts < DAY1)).to_numpy()
    for version in reg.active():
        man = reg.manifest(version)
        man.pop("files", None)
        model = reg.load(version, cache)
        v = {"manifest": man}
        X = frame.X.copy()
        for c in model.features():
            if c not in X.columns:
                X[c] = np.nan
        s1 = {}
        s1_out = {}
        for code, m in model.stage1.items():
            p = m.predict(X)
            s1[code] = p["q50"].to_numpy()
            s1_out[code] = [round(float(x), 4) for x in p["q50"].to_numpy()[sel]]
        X2 = model._with_stage1(X, s1) if model.uses_stage1 else X
        v["stage1_q50_29_10"] = s1_out
        for code, m in model.stage2.items():
            b = m.boosters["q50"]
            Xf = X2[m.features]
            contrib = b.predict(Xf.loc[sel], pred_contrib=True)
            scale = m._scale(X2.loc[sel])
            offset = m._offset(X2.loc[sel])
            cdf = pd.DataFrame(contrib[:, :-1] * scale[:, None], columns=m.features)
            imp = pd.Series(b.feature_importance("gain"), index=b.feature_name()).sort_values(
                ascending=False
            )
            v[code] = {
                "features": m.features,
                "center": m.center,
                "normalize_by": m.normalize_by,
                "gain_top30": {
                    k: round(float(x), 1) for k, x in (imp / imp.sum() * 100).head(30).items()
                },
                "offset_29_10": [round(float(x), 2) for x in offset],
                "bias": round(float(contrib[0, -1] * scale[0]), 2),
                "contrib_mean_29_10": {
                    k: round(float(x), 2)
                    for k, x in cdf.mean().sort_values(key=abs, ascending=False).head(30).items()
                },
                "contrib_hourly_top": {
                    k: [round(float(x), 1) for x in cdf[k]]
                    for k in cdf.abs().mean().sort_values(ascending=False).head(12).index
                },
                "values_29_10": {
                    k: [None if pd.isna(x) else round(float(x), 3) for x in Xf.loc[sel, k]]
                    for k in m.features
                    if not k.startswith(
                        (
                            "hour",
                            "dow",
                            "doy",
                            "month",
                            "week",
                            "holiday",
                            "bridge",
                            "dst",
                            "weekend",
                            "lead",
                        )
                    )
                },
                "valid_ts": [str(x) for x in ts[sel]],
            }
        res["versions"][version] = v
    out["configs"][cfg.name] = res

json.dump(out, sys.stdout, indent=1, default=str)
