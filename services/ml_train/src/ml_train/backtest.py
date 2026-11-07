"""Rollierender Backtest (Expanding Window) aller Modellvarianten gegen Vergleichsverfahren.

Je Faltung wird jede Variante mit allen Ausgabetagen bis ``gap_days`` vor Faltungsbeginn
trainiert und auf den Ausgabetagen der Faltung geprüft. Vergleichsverfahren (Vortag, Vorwoche,
ENTSO-E-Prognosen) laufen auf denselben Zeilen mit. Ergebnis: Prognosen aller Verfahren in
Langform und Kennzahlen je Verfahren, Ziel und Gruppe.

@relation(SWA-MLT-BACKTEST, scope=file)
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from ml_train.config import CompareWindow, ModelConfig
from ml_train.dataset import FeatureBuilder, FeatureFrame, issue_days_between
from ml_train.jobs import JobLog
from ml_train.metrics import (
    add_context,
    common_rows,
    covers_window,
    daily_rank_corr,
    in_window,
    score,
    season_comparison,
)
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

    def window_methods(self, w: CompareWindow) -> dict[str, bool]:
        """Je Verfahren, ob seine Prognosen das Vergleichsfenster vollständig abdecken.

        @relation(SWR-MLT-042, scope=function)
        """
        p = self.predictions
        return {str(m): covers_window(g, w.start, w.end) for m, g in p.groupby("method", sort=True)}

    def metrics_window(
        self, w: CompareWindow, by: Sequence[str] = ("method", "target", "lead_day")
    ) -> pd.DataFrame:
        """Kennzahlen nur im Vergleichsfenster, mit Spalte ``complete`` je Verfahren.

        @relation(SWR-MLT-042, scope=function)
        """
        m = score(add_context(in_window(self.predictions, w.start, w.end)), by)
        m.insert(0, "window", w.name)
        m["complete"] = m["method"].map(self.window_methods(w)).fillna(False).astype(bool)
        return m

    def season_comparison(self, w: CompareWindow | None = None) -> pd.DataFrame:
        """Verfahren gegeneinander je Ziel und Jahreszeit, auf gemeinsamen Zeilen.

        Mit Fenster nur die Ausgabetage im Fenster und nur Verfahren, die es abdecken.

        @relation(SWR-MLT-043, scope=function)
        """
        p = self.predictions
        if w is not None:
            ok = [m for m, done in self.window_methods(w).items() if done]
            p = in_window(p[p["method"].isin(ok)], w.start, w.end)
        return season_comparison(p)

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
    """@relation(SWR-MLT-040, scope=function)"""
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


def backtest_steps(
    fb: FeatureBuilder, cfg: ModelConfig, variants: Sequence[str] | None = None
) -> int:
    """Rückmeldungen von :func:`run_backtest`: Merkmale, dann je Faltung und Variante eine."""
    names = variants or [v.name for v in cfg.variants]
    return 1 + len(make_folds(cfg, data_days(fb, cfg)[1])) * len(names)


#: Name der Zwischenstände für die Vergleichsverfahren einer Faltung (keine Variante heißt so)
BASELINES = "_vergleich"


class Checkpoint:
    """Zwischenstände des Backtests: eine Datei je Faltung und Verfahren.

    Bricht ein Lauf ab, übernimmt der nächste Aufruf mit demselben Ausgabeordner alle fertigen
    Teile und rechnet nur die fehlenden. Jeder Teil trägt einen Schlüssel aus Snapshot, den für
    ihn maßgeblichen Teilen der Konfiguration und den Grenzen seiner Faltung; ändert sich davon
    etwas, passt der Schlüssel nicht mehr und der Teil wird neu gerechnet. Nach dem Schreiben der
    Ergebnisse entfernt ``ml-train backtest`` den Ordner wieder.

    @relation(SWR-MLT-044, scope=class)
    """

    def __init__(self, root: Path, snapshot_id: str, cfg: ModelConfig) -> None:
        self.root = root
        # Was alle Verfahren gemeinsam betrifft; Varianten gehen je Teil einzeln ein
        common = cfg.model_dump(
            mode="json", exclude={"variants", "compare", "compare_rolling", "promotion"}
        )
        self._base = json.dumps({"snapshot": snapshot_id, "cfg": common}, sort_keys=True)
        self._variants = {v.name: v.model_dump(mode="json") for v in cfg.variants}
        self.reused = 0

    def _key(self, fold: Fold, method: str) -> str:
        spec = self._variants.get(method, BASELINES)
        raw = json.dumps(
            [self._base, spec, str(fold.test_from), str(fold.test_to), str(fold.train_to)],
            sort_keys=True,
        )
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    def _path(self, fold: Fold, method: str) -> Path:
        return self.root / f"fold{fold.no:02d}_{method}_{self._key(fold, method)}.parquet"

    def load(self, fold: Fold, method: str) -> tuple[pd.DataFrame, float] | None:
        p = self._path(fold, method)
        if not p.exists():
            return None
        side = p.with_suffix(".json")
        meta = json.loads(side.read_text()) if side.exists() else {}
        self.reused += 1
        return pd.read_parquet(p), float(meta.get("seconds", 0.0))

    def save(self, fold: Fold, method: str, parts: list[pd.DataFrame], seconds: float) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        p = self._path(fold, method)
        df = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()
        # erst Begleitdatei, dann Parquet umbenennen: ein Teil gilt erst mit beiden als fertig
        p.with_suffix(".json").write_text(json.dumps({"seconds": round(seconds, 3)}))
        tmp = p.with_suffix(".tmp")
        df.to_parquet(tmp, index=False)
        tmp.replace(p)


class Paused(Exception):
    """Der Backtest hält vor einem Teil an, der nicht mehr bis zur Frist fertig würde.

    Die fertigen Teile liegen im Zwischenspeicher; der nächste Aufruf mit demselben Ausgabeordner
    und Snapshot rechnet ab hier weiter.
    """


def _now() -> datetime:
    return datetime.now(UTC)


def _estimate(timings: dict[str, float], name: str) -> float | None:
    """Erwartete Dauer eines Teils: längste bekannte Faltung der Variante, sonst aller Varianten."""
    own = [s for k, s in timings.items() if k.startswith("fold") and k.endswith(f"_{name}_s")]
    if own:
        return max(own)
    any_ = [s for k, s in timings.items() if k.startswith("fold") and k.endswith("_s")]
    return max(any_) if any_ else None


def _check_deadline(
    deadline: datetime | None, clock: Callable[[], datetime], need_s: float | None, what: str
) -> None:
    if deadline is None:
        return
    now = clock()
    if now >= deadline or (need_s and now + timedelta(seconds=need_s) > deadline):
        log.info("backtest.paused", before=what, deadline=deadline, expected_s=need_s)
        raise Paused(f"angehalten vor {what}, Frist {deadline:%Y-%m-%d %H:%M %Z}")


def run_backtest(
    fb: FeatureBuilder,
    cfg: ModelConfig,
    variants: Sequence[str] | None = None,
    job: JobLog | None = None,
    checkpoint: Checkpoint | None = None,
    deadline: datetime | None = None,
    clock: Callable[[], datetime] = _now,
) -> BacktestResult:
    """Backtest aller oder der genannten Varianten.

    Mit ``deadline`` beginnt der Lauf keinen Teil mehr, dessen erwartete Dauer (längste bekannte
    Faltung) über die Frist reichen würde, und wirft :class:`Paused`. Das braucht einen
    ``checkpoint``, sonst ginge die Arbeit verloren.

    @relation(SWR-MLT-040, SWR-MLT-041, SWR-MLT-044, SWR-MLT-046, scope=function)
    """
    if deadline is not None and checkpoint is None:
        raise ValueError("deadline braucht einen Checkpoint")
    job = job or JobLog(None)
    _check_deadline(deadline, clock, None, "den Merkmalen")
    first, last = data_days(fb, cfg)
    folds = make_folds(cfg, last)
    if not folds:
        raise ValueError(f"Kein Testzeitraum: test_from {cfg.backtest.test_from}, Daten bis {last}")
    names = list(variants or [v.name for v in cfg.variants])
    job.step("Merkmale bilden")
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
        done = checkpoint.load(fold, BASELINES) if checkpoint else None
        if done is not None:
            parts.append(done[0])
        else:
            base: list[pd.DataFrame] = []
            for method, preds in baselines(test, cfg).items():
                base += _long(method, dict(preds), test, fold.no)
            parts += base
            if checkpoint:
                checkpoint.save(fold, BASELINES, base, 0.0)
        for name in names:
            job.step(f"Faltung {fold.no + 1}/{len(folds)} · {name} · {fold.test_from:%m/%Y}")
            done = checkpoint.load(fold, name) if checkpoint else None
            if done is not None:
                parts.append(done[0])
                if done[1]:
                    timings[f"fold{fold.no}_{name}_s"] = done[1]
                log.info("backtest.reused", fold=fold.no, variant=name)
                continue
            _check_deadline(deadline, clock, _estimate(timings, name), f"Faltung {fold.no} {name}")
            got, dt = _fit_predict(full, test, cfg, name, fold, first)
            parts += got
            if dt:
                timings[f"fold{fold.no}_{name}_s"] = dt
            if checkpoint:
                checkpoint.save(fold, name, got, dt)
    pred = pd.concat([p for p in parts if not p.empty], ignore_index=True)
    return BacktestResult(pred, folds, timings)


def _fit_predict(
    full: FeatureFrame, test: FeatureFrame, cfg: ModelConfig, name: str, fold: Fold, first: date
) -> tuple[list[pd.DataFrame], float]:
    """Eine Variante in einer Faltung trainieren und auf den Testzeilen prognostizieren."""
    v = cfg.variant(name)
    tr = train_frame(full, cfg, name, fold.train_to, first)
    if len(tr) < 1000:
        log.warning("backtest.skip", fold=fold.no, variant=name, rows=len(tr))
        return [], 0.0
    t1 = time.monotonic()
    model = TwoStageModel(cfg, v).fit(tr)
    te = test
    if v.requires_weather and "wx_lead_h" in test.X:
        te = test.subset(test.X["wx_lead_h"].notna().to_numpy())
    if len(te) == 0:
        return [], 0.0
    out = _long(name, dict(model.predict(te)), te, fold.no)
    dt = time.monotonic() - t1
    log.info(
        "backtest.fold",
        fold=fold.no,
        variant=name,
        train_rows=len(tr),
        test_rows=len(te),
        seconds=round(dt, 1),
    )
    return out, dt
