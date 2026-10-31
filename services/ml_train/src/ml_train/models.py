"""Quantil-Modelle (LightGBM) in zwei Stufen und einfache Vergleichsverfahren.

Stufe 1 sagt Last, Wind und PV vorher, Stufe 2 den Preis mit den Stufe-1-Prognosen und der
daraus gerechneten Residuallast als Eingang (ARCHITEKTUR.md 7.3). Damit Stufe 2 im Training
nicht mit zu guten Stufe-1-Werten lernt, bekommt sie dort Out-of-Fold-Prognosen aus zeitlich
zusammenhängenden Blöcken.

Wind und PV werden als Kapazitätsfaktor gelernt (Ziel durch installierte Leistung), weil
Entscheidungsbäume nicht über den Bestand der Trainingszeit hinaus extrapolieren. Der Preis
kann relativ zum Mittel der letzten bekannten Tage gelernt werden (``center``), damit
Preisniveaus wie 2022 nicht als feste Werte gelernt werden, und zusätzlich durch ein
Niveaumerkmal geteilt werden (``Variant.scale``, z. B. den Grenzkostenindex): Dann lernt das
Modell Abweichungen relativ zum Brennstoff- und CO2-Niveau statt in EUR/MWh.

Mit ``calibration.months`` werden die äußeren Quantile je Vorlauftag nachkalibriert
(``CalibrationConfig``): Quantil-Bäume sind auf den Trainingsdaten zu sicher, das Band P10-P90
enthielt im Backtest nur 55-70 % statt 80 % der Ist-Werte.

@relation(SWA-MLT-MODELS, scope=file)
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import lightgbm as lgb
import numpy as np
import pandas as pd

from ml_train.config import CalibrationConfig, LgbmParams, ModelConfig, StageTarget, Variant
from ml_train.dataset import FeatureFrame
from ml_train.jobs import Progress, no_progress

#: Eingänge der Stufe 2 aus Stufe 1
RESIDUAL = "s1_residual_load"


def lgb_params(p: LgbmParams) -> dict[str, Any]:
    return {
        "num_leaves": p.num_leaves,
        "learning_rate": p.learning_rate,
        "min_data_in_leaf": p.min_child_samples,
        "bagging_fraction": p.subsample,
        "bagging_freq": p.subsample_freq,
        "feature_fraction": p.colsample_bytree,
        "lambda_l2": p.reg_lambda,
        "num_threads": p.n_jobs,
        "verbose": -1,
        "seed": 42,
    }


def qname(q: float) -> str:
    return f"q{round(q * 100):02d}"


@dataclass
class QuantileModel:
    """@relation(SWR-MLT-030, scope=class)"""

    target: str
    quantiles: list[float]
    features: list[str]
    params: LgbmParams
    normalize_by: list[str] = field(default_factory=list)
    center: str | None = None
    scale: str | None = None
    boosters: dict[str, lgb.Booster] = field(default_factory=dict)
    #: Verschiebung je Vorlauftag und Quantil im Lernraum (Kalibrierung), sonst 0
    shifts: dict[int, dict[str, float]] = field(default_factory=dict)

    def _scale(self, X: pd.DataFrame) -> np.ndarray:
        out = np.ones(len(X))
        if self.normalize_by:
            cap = X[self.normalize_by].sum(axis=1).to_numpy(dtype=np.float64)
            out = np.where(cap > 0, cap, np.nan)
        if self.scale is not None:
            lvl = X[self.scale].to_numpy(dtype=np.float64)
            out = out * np.where(lvl > 0, lvl, np.nan)
        return out

    def _offset(self, X: pd.DataFrame) -> np.ndarray:
        if self.center is None:
            return np.zeros(len(X))
        out: np.ndarray = X[self.center].to_numpy(dtype=np.float64)
        return out

    def fit(self, X: pd.DataFrame, y: pd.Series | np.ndarray) -> QuantileModel:
        yy = (np.asarray(y, dtype=np.float64) - self._offset(X)) / self._scale(X)
        ok = np.isfinite(yy)
        Xf = X.loc[ok, self.features]
        params = lgb_params(self.params)
        data = lgb.Dataset(Xf, yy[ok], free_raw_data=True)
        for q in self.quantiles:
            self.boosters[qname(q)] = lgb.train(
                {**params, "objective": "quantile", "alpha": q},
                data,
                num_boost_round=self.params.n_estimators,
            )
        return self

    def _raw(self, X: pd.DataFrame) -> np.ndarray:
        Xf = X[self.features]
        return np.column_stack([self.boosters[qname(q)].predict(Xf) for q in self.quantiles])

    def predict(self, X: pd.DataFrame) -> pd.DataFrame:
        raw = self._raw(X)
        if self.shifts and "lead_day" in X:
            lead = X["lead_day"].to_numpy()
            for d, sh in self.shifts.items():
                rows = lead == d
                for j, q in enumerate(self.quantiles):
                    raw[rows, j] += sh.get(qname(q), 0.0)
        raw.sort(axis=1)  # keine sich kreuzenden Quantile
        vals = raw * self._scale(X)[:, None] + self._offset(X)[:, None]
        return pd.DataFrame(vals, index=X.index, columns=[qname(q) for q in self.quantiles])

    def calibration_shifts(
        self, X: pd.DataFrame, y: pd.Series | np.ndarray, cal: CalibrationConfig
    ) -> dict[int, dict[str, float]]:
        """Verschiebung je Vorlauftag, damit jedes äußere Quantil q auf ``X``/``y`` (Daten, die
        dieses Modell nicht gesehen hat) wirklich den Anteil q der Ist-Werte unterschreitet.

        @relation(SWR-MLT-031, scope=function)
        """
        r = (np.asarray(y, dtype=np.float64) - self._offset(X)) / self._scale(X)
        raw = self._raw(X)
        ok = np.isfinite(r) & np.isfinite(raw).all(axis=1)
        lead = X["lead_day"].to_numpy()
        out: dict[int, dict[str, float]] = {}
        for d in np.unique(lead[ok]):
            rows = ok & (lead == d)
            n = int(rows.sum())
            if n < cal.min_rows:
                continue
            sh = {}
            for j, q in enumerate(self.quantiles):
                if q == 0.5:
                    continue
                # endliche Stichprobe: Quantilstufe wie bei konformaler Vorhersage nach außen
                level = min(1.0, q * (n + 1) / n) if q > 0.5 else max(0.0, (q * (n + 1) - 1) / n)
                sh[qname(q)] = float(np.quantile(r[rows] - raw[rows, j], level))
            out[int(d)] = sh
        return out

    # --- Ablage ------------------------------------------------------------------------------

    def save(self, out: Path) -> dict[str, Any]:
        files = {}
        for name, b in self.boosters.items():
            f = f"{self.target}_{name}.txt"
            (out / f).write_text(b.model_to_string())
            files[name] = f
        return {
            "target": self.target,
            "quantiles": self.quantiles,
            "features": self.features,
            "normalize_by": self.normalize_by,
            "center": self.center,
            "scale": self.scale,
            "files": files,
            "shifts": {str(d): sh for d, sh in self.shifts.items()},
        }

    @classmethod
    def load(cls, d: dict[str, Any], base: Path, params: LgbmParams) -> QuantileModel:
        m = cls(
            d["target"],
            d["quantiles"],
            d["features"],
            params,
            d["normalize_by"],
            d["center"],
            d.get("scale"),
        )
        for name, f in d["files"].items():
            m.boosters[name] = lgb.Booster(model_str=(base / f).read_text())
        m.shifts = {int(k): v for k, v in d.get("shifts", {}).items()}
        return m


def blocked_folds(issue_times: pd.Series, k: int) -> np.ndarray:
    """Blocknummer 0..k-1 je Zeile nach Ausgabezeitpunkt (zeitlich zusammenhängend).

    @relation(SWR-MLT-030, scope=function)
    """
    uniq = np.sort(issue_times.unique())
    edges = np.array_split(np.arange(len(uniq)), k)
    block = {}
    for b, ix in enumerate(edges):
        for i in ix:
            block[uniq[i]] = b
    out: np.ndarray = issue_times.map(block).to_numpy()
    return out


class TwoStageModel:
    """Eine Modellvariante: Stufe-1- und Stufe-2-Modelle mit gemeinsamer Merkmalsauswahl.

    @relation(SWR-MLT-030, scope=class)
    """

    def __init__(self, cfg: ModelConfig, variant: Variant) -> None:
        self.cfg = cfg
        self.variant = variant
        self.stage1: dict[str, QuantileModel] = {}
        self.stage2: dict[str, QuantileModel] = {}
        #: Vorlauftage, für die das Modell gelernt hat (ältere Versionen: 1 bis 7)
        self.lead_days: list[int] = list(cfg.issue.lead_days)

    # Merkmale je Ziel -------------------------------------------------------------------------

    def _base_features(self, frame: FeatureFrame) -> list[str]:
        groups = [g for g in self.variant.groups if g != "stage1"]
        return frame.columns(groups)

    def _model(self, t: StageTarget, features: list[str], frame: FeatureFrame) -> QuantileModel:
        normalize = [c for c in t.normalize_by if c in frame.X.columns]
        center = t.center if t.center and t.center in frame.X.columns else None
        scale = self._scale_column(t, frame)
        if normalize:
            # Bestand wirkt sonst wie ein Zeitindex: Außerhalb der Trainingsspanne landet jede
            # Prognose im Blatt der letzten Trainingsmonate (gemessen: +6 GW Bias Wind 2024)
            features = [f for f in features if not f.startswith("cap_")]
        return QuantileModel(
            t.code, self.cfg.quantiles, features, self.cfg.lgbm, normalize, center, scale
        )

    def _scale_column(self, t: StageTarget, frame: FeatureFrame) -> str | None:
        """@relation(SWR-MLT-027, scope=function)"""
        col = self.variant.scale.get(t.code)
        if col is not None and col not in frame.X.columns:
            raise ValueError(f"Variante {self.variant.name!r}: Bezugsspalte {col!r} fehlt")
        return col

    @property
    def uses_stage1(self) -> bool:
        return "stage1" in self.variant.groups

    def _with_stage1(self, X: pd.DataFrame, s1: dict[str, np.ndarray]) -> pd.DataFrame:
        X = X.copy()
        for code, v in s1.items():
            X[f"s1_{code}"] = v
        gen = [f"s1_{t.code}" for t in self.cfg.stage1 if t.role == "generation"]
        load = [f"s1_{t.code}" for t in self.cfg.stage1 if t.role == "load"]
        if load:
            X[RESIDUAL] = X[load].sum(axis=1) - (X[gen].sum(axis=1) if gen else 0.0)
        return X

    def stage1_columns(self) -> list[str]:
        cols = [f"s1_{t.code}" for t in self.cfg.stage1]
        if any(t.role == "load" for t in self.cfg.stage1):
            cols.append(RESIDUAL)
        return cols

    # Training und Prognose --------------------------------------------------------------------

    def fit_steps(self) -> int:
        """Zahl der Rückmeldungen von :meth:`fit` an ``progress``."""
        oof = self.uses_stage1 and bool(self.cfg.stage2)
        once = len(self.cfg.stage1) * (2 if oof else 1) + len(self.cfg.stage2)
        return once * (2 if self.cfg.calibration.months else 1)

    def fit(self, frame: FeatureFrame, progress: Progress = no_progress) -> TwoStageModel:
        shifts = self._calibrate(frame, progress) if self.cfg.calibration.months else {}
        self._fit(frame, progress)
        for code, sh in shifts.items():
            (self.stage1.get(code) or self.stage2[code]).shifts = sh
        return self

    def _calibrate(
        self, frame: FeatureFrame, progress: Progress
    ) -> dict[str, dict[int, dict[str, float]]]:
        """Verschiebungen je Ziel aus einem Modell ohne die letzten ``months`` Monate.

        @relation(SWR-MLT-031, scope=function)
        """
        cal = self.cfg.calibration
        issues = frame.meta["issue_time"]
        cut = issues.max() - pd.DateOffset(months=cal.months)
        hold = (issues > cut).to_numpy()
        # Lernziele des Kalibriermodells enden vor dem Kalibrierzeitraum
        early = (~hold) & (frame.meta["ts"] <= cut).to_numpy()
        if hold.sum() < cal.min_rows or early.sum() < 1000:
            return {}
        cfg0 = self.cfg.model_copy(update={"calibration": CalibrationConfig()})
        m0 = TwoStageModel(cfg0, self.variant)._fit(
            frame.subset(early), lambda s: progress(f"Kalibrierung · {s}")
        )
        test = frame.subset(hold)
        out: dict[str, dict[int, dict[str, float]]] = {}
        s1: dict[str, np.ndarray] = {}
        for code, m in m0.stage1.items():
            out[code] = m.calibration_shifts(test.X, test.y[code], cal)
            s1[code] = m.predict(test.X)["q50"].to_numpy()
        X2 = m0._with_stage1(test.X, s1) if m0.uses_stage1 else test.X
        for code, m in m0.stage2.items():
            out[code] = m.calibration_shifts(X2, test.y[code], cal)
        return out

    def _fit(self, frame: FeatureFrame, progress: Progress = no_progress) -> TwoStageModel:
        base = self._base_features(frame)
        oof: dict[str, np.ndarray] = {}
        for t in self.cfg.stage1:
            progress(f"Stufe 1 · {t.code}")
            self.stage1[t.code] = self._model(t, base, frame).fit(frame.X, frame.y[t.code])
            if self.uses_stage1 and self.cfg.stage2:
                progress(f"Stufe 1 · {t.code} · Out-of-Fold")
                oof[t.code] = self._oof(t, base, frame)
        X2 = self._with_stage1(frame.X, oof) if self.uses_stage1 else frame.X
        feats2 = base + (self.stage1_columns() if self.uses_stage1 else [])
        for t in self.cfg.stage2:
            progress(f"Stufe 2 · {t.code}")
            self.stage2[t.code] = self._model(t, feats2, frame).fit(X2, frame.y[t.code])
        return self

    def _oof(self, t: StageTarget, base: list[str], frame: FeatureFrame) -> np.ndarray:
        """@relation(SWR-MLT-030, scope=function)"""
        k = self.cfg.backtest.oof_folds
        blocks = blocked_folds(frame.meta["issue_time"], k)
        out = np.full(len(frame), np.nan)
        median_only = self.cfg.lgbm.model_copy()
        for b in range(k):
            test = blocks == b
            m = QuantileModel(
                t.code,
                [0.5],
                [f for f in base if not (t.normalize_by and f.startswith("cap_"))],
                median_only,
                [c for c in t.normalize_by if c in frame.X.columns],
                t.center if t.center and t.center in frame.X.columns else None,
                self._scale_column(t, frame),
            )
            m.fit(frame.X[~test], frame.y[t.code].to_numpy()[~test])
            out[test] = m.predict(frame.X[test])["q50"].to_numpy()
        return out

    def features(self) -> list[str]:
        """Alle Merkmalsspalten, die das Modell aus dem FeatureFrame liest (ohne ``s1_*``)."""
        own = set(self.stage1_columns())
        cols = [
            c
            for m in [*self.stage1.values(), *self.stage2.values()]
            for c in [
                *m.features,
                *m.normalize_by,
                *([m.center] if m.center else []),
                *([m.scale] if m.scale else []),
            ]
        ]
        return [c for c in dict.fromkeys(cols) if c not in own]

    def predict(self, frame: FeatureFrame) -> dict[str, pd.DataFrame]:
        """Quantile je Ziel; zum Ausgabezeitpunkt schon bekannte Ist-Werte (``frame.known``)
        ersetzen die Prognose in allen Quantilen.

        @relation(SWR-MLT-027, SWR-MLT-032, scope=function)
        """
        res = self._predict(frame)
        if frame.known is not None:
            for code, p in res.items():
                if code in frame.known:
                    kn = frame.known[code].to_numpy()
                    hit = np.isfinite(kn)
                    if hit.any():
                        p.loc[hit, :] = np.repeat(kn[hit, None], p.shape[1], axis=1)
        return res

    def _predict(self, frame: FeatureFrame) -> dict[str, pd.DataFrame]:
        res: dict[str, pd.DataFrame] = {}
        s1: dict[str, np.ndarray] = {}
        for code, m in self.stage1.items():
            res[code] = m.predict(frame.X)
            s1[code] = res[code]["q50"].to_numpy()
        X2 = self._with_stage1(frame.X, s1) if self.uses_stage1 else frame.X
        for code, m in self.stage2.items():
            res[code] = m.predict(X2)
        return res

    # Ablage ---------------------------------------------------------------------------------

    def save(self, out: Path) -> dict[str, Any]:
        out.mkdir(parents=True, exist_ok=True)
        spec = {
            "variant": self.variant.model_dump(mode="json"),
            "lead_days": self.lead_days,
            "stage1": {c: m.save(out) for c, m in self.stage1.items()},
            "stage2": {c: m.save(out) for c, m in self.stage2.items()},
        }
        (out / "model.json").write_text(json.dumps(spec, indent=2))
        return spec

    @classmethod
    def load(cls, cfg: ModelConfig, base: Path) -> TwoStageModel:
        spec = json.loads((base / "model.json").read_text())
        m = cls(cfg, Variant.model_validate(spec["variant"]))
        m.stage1 = {c: QuantileModel.load(d, base, cfg.lgbm) for c, d in spec["stage1"].items()}
        m.stage2 = {c: QuantileModel.load(d, base, cfg.lgbm) for c, d in spec["stage2"].items()}
        m.lead_days = spec.get("lead_days", [1, 2, 3, 4, 5, 6, 7])
        return m


# --- Vergleichsverfahren -----------------------------------------------------------------------


def baselines(frame: FeatureFrame, cfg: ModelConfig) -> dict[str, dict[str, np.ndarray]]:
    """P50 je Verfahren und Ziel: Vortag bzw. letzter bekannter Tag, Vorwoche, Benchmarks.

    @relation(SWR-MLT-040, scope=function)
    """
    out: dict[str, dict[str, np.ndarray]] = {}
    day1 = (frame.meta["lead_day"] == 1).to_numpy()
    for t in cfg.stage1 + cfg.stage2:
        out.setdefault("naive_day", {})[t.code] = frame.X[f"lag_{t.code}_day"].to_numpy()
        out.setdefault("naive_week", {})[t.code] = frame.X[f"lag_{t.code}_week"].to_numpy()
        for b in t.benchmarks:
            # Prognosen Dritter nur für den Folgetag (so werden sie veröffentlicht)
            v = np.where(day1, frame.benchmarks[b].to_numpy(), np.nan)
            out.setdefault(f"bench_{b}", {})[t.code] = v
    return out
