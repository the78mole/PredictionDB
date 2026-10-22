"""Rollierender Backtest (Expanding Window) aller Modellvarianten gegen Vergleichsverfahren.

Je Faltung wird jede Variante mit allen Ausgabetagen bis ``gap_days`` vor Faltungsbeginn
trainiert und auf den Ausgabetagen der Faltung geprüft. Vergleichsverfahren (Vortag, Vorwoche,
ENTSO-E-Prognosen) laufen auf denselben Zeilen mit. Ergebnis: Prognosen aller Verfahren in
Langform und Kennzahlen je Verfahren, Ziel und Gruppe.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta

import numpy as np
import pandas as pd

from ml_train.config import ModelConfig
from ml_train.dataset import FeatureBuilder, FeatureFrame, issue_days_between
from ml_train.metrics import add_context, common_rows, daily_rank_corr, score
from ml_train.models import TwoStageModel, baselines
from predictiondb_core.log import get_logger

log = get_logger(__name__)


@dataclass
class Fold:
    no: int
    test_from: date
    test_to: date  # einschließlich
    train_to: date


@dataclass
class BacktestResult:
    predictions: pd.DataFrame
    folds: list[Fold]
    timings: dict[str, float]

    def metrics(self, by: Sequence[str] = ("method", "target", "lead_day")) -> pd.DataFrame:
        return score(add_context(self.predictions), by)

    def metrics_common(
        self, methods: Sequence[str], by: Sequence[str] = ("method", "target", "lead_day")
    ) -> pd.DataFrame:
        return score(add_context(common_rows(self.predictions, methods)), by)

    def rank_corr(self, by: Sequence[str] = ("method", "target", "lead_day")) -> pd.DataFrame:
        return daily_rank_corr(add_context(self.predictions), by)


def add_months(d: date, n: int) -> date:
    m = d.month - 1 + n
    return date(d.year + m // 12, m % 12 + 1, 1)


def data_days(fb: FeatureBuilder, cfg: ModelConfig) -> tuple[date, date]:
    """Erster und letzter Ausgabetag mit Ist-Werten aller Ziele (letzter: Folgetag bekannt)."""
    e = fb.energy[[c for c in cfg.targets if c in fb.energy.columns]].dropna(how="any")
    if e.empty:
        raise ValueError("Snapshot enthält keine Stunde mit Ist-Werten aller Zielgrößen")
    first = (e.index.min() + timedelta(days=8)).date()
    last = (e.index.max() - timedelta(days=1)).date()
    return first, last


def make_folds(cfg: ModelConfig, last_issue: date) -> list[Fold]:
    bt = cfg.backtest
    end = min(bt.test_to or last_issue, last_issue)
    folds, start, no = [], bt.test_from, 0
    while start <= end:
        stop = min(add_months(start, bt.fold_months) - timedelta(days=1), end)
        folds.append(Fold(no, start, stop, start - timedelta(days=bt.gap_days)))
        start, no = stop + timedelta(days=1), no + 1
    return folds


def _long(
    method: str, preds: dict[str, pd.DataFrame | np.ndarray], frame: FeatureFrame, fold: int
) -> list[pd.DataFrame]:
    out = []
    for code, p in preds.items():
        df = frame.meta[["issue_time", "ts", "lead_day"]].copy()
        if isinstance(p, pd.DataFrame):
            for c in p.columns:
                df[c] = p[c].to_numpy()
        else:
            df["q50"] = p
        df["actual"] = frame.y[code].to_numpy()
        df["method"] = method
        df["target"] = code
        df["fold"] = fold
        out.append(df[np.isfinite(df["q50"].to_numpy(dtype=float))])
    return out


def train_frame(
    full: FeatureFrame, cfg: ModelConfig, variant_name: str, train_to: date, first: date
) -> FeatureFrame:
    v = cfg.variant(variant_name)
    start = max(v.train_from or first, first)
    days = set(issue_days_between(start, train_to, cfg.backtest.train_issue_stride))
    issue_day = full.meta["issue_time"].dt.date
    mask = np.array(issue_day.isin(days), dtype=bool)
    # Ziele, die nach ``train_to`` liegen, sind zum Faltungsbeginn noch nicht bekannt
    mask &= (full.meta["ts"] < pd.Timestamp(train_to + timedelta(days=1), tz="UTC")).to_numpy()
    if v.requires_weather and "wx_lead_h" in full.X:
        mask &= full.X["wx_lead_h"].notna().to_numpy()
    return full.subset(mask)


def run_backtest(
    fb: FeatureBuilder, cfg: ModelConfig, variants: Sequence[str] | None = None
) -> BacktestResult:
    first, last = data_days(fb, cfg)
    folds = make_folds(cfg, last)
    if not folds:
        raise ValueError(f"Kein Testzeitraum: test_from {cfg.backtest.test_from}, Daten bis {last}")
    names = list(variants or [v.name for v in cfg.variants])
    t0 = time.monotonic()
    full = fb.build(issue_days_between(first, last))
    timings = {"features_s": time.monotonic() - t0}
    log.info("backtest.features", rows=len(full), columns=full.X.shape[1], first=first, last=last)
    parts: list[pd.DataFrame] = []
    issue_day = full.meta["issue_time"].dt.date
    for fold in folds:
        test_mask = ((issue_day >= fold.test_from) & (issue_day <= fold.test_to)).to_numpy()
        test = full.subset(test_mask)
        if len(test) == 0:
            continue
        for method, preds in baselines(test, cfg).items():
            parts += _long(method, dict(preds), test, fold.no)
        for name in names:
            v = cfg.variant(name)
            tr = train_frame(full, cfg, name, fold.train_to, first)
            if len(tr) < 1000:
                log.warning("backtest.skip", fold=fold.no, variant=name, rows=len(tr))
                continue
            t1 = time.monotonic()
            model = TwoStageModel(cfg, v).fit(tr)
            te = test
            if v.requires_weather and "wx_lead_h" in test.X:
                te = test.subset(test.X["wx_lead_h"].notna().to_numpy())
            if len(te) == 0:
                continue
            parts += _long(name, dict(model.predict(te)), te, fold.no)
            dt = time.monotonic() - t1
            timings[f"fold{fold.no}_{name}_s"] = dt
            log.info(
                "backtest.fold",
                fold=fold.no,
                variant=name,
                train_rows=len(tr),
                test_rows=len(te),
                seconds=round(dt, 1),
            )
    pred = pd.concat(parts, ignore_index=True)
    return BacktestResult(pred, folds, timings)
