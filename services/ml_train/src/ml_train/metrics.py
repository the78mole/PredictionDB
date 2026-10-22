"""Fehlermaße für Punkt- und Quantilprognosen, gruppiert nach Horizont und Lage."""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np
import pandas as pd

from ml_train.calendar import BERLIN

SEASONS = {
    12: "Winter",
    1: "Winter",
    2: "Winter",
    3: "Frühling",
    4: "Frühling",
    5: "Frühling",
    6: "Sommer",
    7: "Sommer",
    8: "Sommer",
    9: "Herbst",
    10: "Herbst",
    11: "Herbst",
}


def pinball(y: np.ndarray, pred: np.ndarray, q: float) -> np.ndarray:
    d = y - pred
    out: np.ndarray = np.maximum(q * d, (q - 1) * d)
    return out


def add_context(pred: pd.DataFrame) -> pd.DataFrame:
    """Spalten für Gruppen: Jahreszeit, Jahr, Liefertag (Ortszeit)."""
    local = pd.DatetimeIndex(pred["ts"]).tz_convert(BERLIN)
    pred = pred.copy()
    pred["season"] = [SEASONS[m] for m in local.month]
    pred["year"] = local.year
    pred["day"] = local.date
    return pred


def score(pred: pd.DataFrame, by: Sequence[str]) -> pd.DataFrame:
    """Kennzahlen je Gruppe. Erwartet ``actual``, ``q50`` und optional ``q10``/``q90``."""
    p = pred[np.isfinite(pred["actual"]) & np.isfinite(pred["q50"])].copy()
    p["err"] = p["q50"] - p["actual"]
    p["abs"] = p["err"].abs()
    p["sq"] = p["err"] ** 2
    has_q = (
        p["q10"].notna().to_numpy() & p["q90"].notna().to_numpy()
        if "q10" in p and "q90" in p
        else np.zeros(len(p), dtype=bool)
    )
    y = p["actual"].to_numpy()
    if has_q.any():
        q10, q50, q90 = (p[c].to_numpy() for c in ("q10", "q50", "q90"))
        pb = (pinball(y, q10, 0.1) + pinball(y, q50, 0.5) + pinball(y, q90, 0.9)) / 3
        p["pinball"] = np.where(has_q, pb, np.nan)
        p["covered"] = np.where(has_q, (y >= q10) & (y <= q90), np.nan)
    else:
        p["pinball"] = np.nan
        p["covered"] = np.nan
    # Pinball nur über den Median (= MAE/2) ist auch für Punktprognosen definiert
    p["pinball50"] = p["abs"] / 2
    g = p.groupby(list(by), observed=True)
    out = pd.DataFrame(
        {
            "n": g.size(),
            "mae": g["abs"].mean(),
            "rmse": np.sqrt(g["sq"].mean()),
            "bias": g["err"].mean(),
            "pinball": g["pinball"].mean(),
            "pinball50": g["pinball50"].mean(),
            "coverage80": g["covered"].mean(),
        }
    )
    return out.reset_index()


def daily_rank_corr(pred: pd.DataFrame, by: Sequence[str]) -> pd.DataFrame:
    """Mittlere Rangkorrelation (Spearman) der Stunden je Liefertag, z. B. für Lastverschiebung."""
    p = pred[np.isfinite(pred["actual"]) & np.isfinite(pred["q50"])]
    keys = [*by, "issue_time", "day"]

    def rho(g: pd.DataFrame) -> float:
        if len(g) < 12:
            return np.nan
        return float(g["q50"].rank().corr(g["actual"].rank()))

    per_day = p.groupby(keys, observed=True)[["q50", "actual"]].apply(rho).rename("rho")
    return per_day.groupby(list(by), observed=True).mean().rename("rank_corr_day").reset_index()


def common_rows(pred: pd.DataFrame, methods: Sequence[str]) -> pd.DataFrame:
    """Nur Zeilen (Ziel, Ausgabe, Stunde), für die alle ``methods`` eine Prognose haben.

    Je Ziel zählen nur die Verfahren, die es für dieses Ziel überhaupt gibt.
    """
    p = pred[pred["method"].isin(methods) & np.isfinite(pred["q50"])]
    parts = []
    key = ["issue_time", "ts"]
    for _, g in p.groupby("target"):
        n = g["method"].nunique()
        cnt = g.groupby(key)["method"].nunique()
        full = cnt[cnt == n].index
        parts.append(g.set_index(key).loc[lambda d, f=full: d.index.isin(f)].reset_index())
    return pd.concat(parts, ignore_index=True) if parts else p
