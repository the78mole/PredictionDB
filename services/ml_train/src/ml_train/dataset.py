"""Merkmale je Ausgabezeitpunkt und Zielstunde, streng mit dem Wissen zum Ausgabezeitpunkt.

Eine Zeile ist ein Paar (``issue_time``, ``ts``): Prognose ausgegeben zu ``issue_time`` für die
Stunde ab ``ts`` (UTC, Intervallbeginn wie in ``energy.value``). Ausgegeben wird einmal am Tag
zur Stunde ``issue.hour_utc``; Ziele sind die ganzen Liefertage (Ortszeit) ``issue.lead_days``
nach dem Ausgabetag, also 23, 24 oder 25 Stunden je Tag.

Leakage-Schutz (ARCHITEKTUR.md 7.4):

- Ist-Werte gelten ab Intervallende plus ``actual_delay_h`` als bekannt.
- Day-Ahead-Preise eines Liefertags gelten am Vortag ab ``price_known_local_hour`` als bekannt.
- Wetter kommt aus dem jüngsten Lauf mit ``run_time + nwp_delay_h <= issue_time``.
- Prognosen Dritter (Lastprognose der ÜNB) sind nur als eigene Reihengruppe (``forecast``)
  Merkmal, und nur für Liefertage, deren Prognose zum Ausgabezeitpunkt veröffentlicht war.
- Tages-, Wochen- und Monatswerte (Brennstoffpreise, Marktwerte) gelten ab ihrer Spalte
  ``published`` im Snapshot als bekannt.

Wetter liegt im Snapshot in nativen Schritten (GFS v16 stündlich bis 120 h, danach
dreistündlich). Erst hier wird auf Stunden interpoliert (Vorgabe: Interpolation beim Lesen):
Momentanwerte linear, Intervallgrößen (Strahlung, PV, Niederschlag, Schneeanteil) gelten für
das ganze Intervall bis zum Schritt. Auf die Energiestunde ``[ts, ts+1h)`` passen Intervallwerte
mit Gültigkeit ``ts+1h`` und das Mittel der Momentanwerte bei ``ts`` und ``ts+1h``.
``wx_interp`` markiert interpolierte Stunden.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta

import numpy as np
import pandas as pd

from ml_train.calendar import BERLIN, CALENDAR_COLUMNS, calendar_features
from ml_train.config import FeatureGroup, ModelConfig, SeriesGroup
from ml_train.snapshot import Snapshot

H = pd.Timedelta(hours=1)
DAY = pd.Timedelta(days=1)

#: Intervallgrößen (Wert gilt für das Intervall bis zur Gültigkeitszeit)
INTERVAL_SUFFIXES = ("_ghi", "_pv_cf", "_pv_power", "_precip", "_snow_fraction")
#: Day-Ahead-Preise (DE-LU und Nachbarzonen): bekannt ab dem Auktionsergebnis am Vortag
PRICE_PREFIX = "da_price_"
LAG_KINDS = ("day", "week", "mean_day", "mean_7d", "last")
META_COLUMNS = ["issue_time", "ts", "lead_day", "lead_h"]


def is_interval_feature(name: str) -> bool:
    return name.endswith(INTERVAL_SUFFIXES)


def local_day_hours(day: date) -> pd.DatetimeIndex:
    """Stundenbeginne (UTC) des Liefertags ``day`` in Ortszeit, 23 bis 25 Stunden."""
    start = pd.Timestamp(day).tz_localize(BERLIN)
    end = pd.Timestamp(day + timedelta(days=1)).tz_localize(BERLIN)
    return pd.date_range(start.tz_convert("UTC"), end.tz_convert("UTC"), freq="h", inclusive="left")


def issue_time(day: date, hour_utc: int) -> pd.Timestamp:
    return pd.Timestamp(day).tz_localize("UTC") + pd.Timedelta(hours=hour_utc)


class WeatherTable:
    """Stündliche, auf Energiestunden ausgerichtete Wetter-Features je Lauf."""

    def __init__(self, weather: pd.DataFrame, features: Sequence[str] = ()) -> None:
        w = weather
        if features:
            w = w[w["feature"].isin(list(features))]
        self.columns: list[str] = []
        self.runs = pd.DatetimeIndex([], tz="UTC")
        self.table = pd.DataFrame()
        if w.empty:
            return
        wide = w.pivot_table(
            index=["run_time", "valid_time"], columns="feature", values="value", aggfunc="mean"
        )
        wide.columns.name = None
        self.columns = [f"wx_{c}" for c in wide.columns]
        parts = []
        for run, g in wide.groupby(level="run_time"):
            parts.append(self._align(pd.Timestamp(run), g.droplevel("run_time")))
        self.table = pd.concat(parts)
        self.runs = pd.DatetimeIndex(sorted(self.table.index.get_level_values(0).unique()))

    @staticmethod
    def _align(run: pd.Timestamp, g: pd.DataFrame) -> pd.DataFrame:
        g = g.sort_index()
        native = g.index
        hourly = pd.date_range(native.min(), native.max(), freq="h")
        g = g.reindex(hourly)
        interval = [c for c in g.columns if is_interval_feature(c)]
        inst = [c for c in g.columns if c not in interval]
        if inst:
            g[inst] = g[inst].interpolate(method="time", limit=5, limit_area="inside")
        if interval:
            g[interval] = g[interval].bfill(limit=5)
        nxt = g.shift(-1)
        out = pd.DataFrame(index=hourly)
        for c in inst:
            out[f"wx_{c}"] = (g[c] + nxt[c]) / 2
        for c in interval:
            out[f"wx_{c}"] = nxt[c]
        out = out[[f"wx_{c}" for c in g.columns]]
        interp = ~hourly.isin(native) | ~(hourly + H).isin(native)
        out["wx_interp"] = interp.astype(np.int8)
        out["wx_lead_h"] = (hourly - run) / H
        out.index = pd.MultiIndex.from_arrays(
            [pd.DatetimeIndex([run] * len(hourly)), hourly], names=["run_time", "ts"]
        )
        return out.astype(np.float32)

    def run_for(self, issue: pd.Timestamp, delay_h: float) -> pd.Timestamp | None:
        """Jüngster Lauf, der zum Ausgabezeitpunkt verarbeitet vorlag."""
        if len(self.runs) == 0:
            return None
        pos = self.runs.searchsorted(issue - pd.Timedelta(hours=delay_h), side="right") - 1
        return None if pos < 0 else pd.Timestamp(self.runs[pos])


@dataclass
class FeatureFrame:
    """Merkmale (``X``), Zuordnung (``meta``) und Ist-Werte je Zielgröße (``y``)."""

    X: pd.DataFrame
    meta: pd.DataFrame
    y: pd.DataFrame
    benchmarks: pd.DataFrame
    groups: dict[str, list[str]] = field(default_factory=dict)

    def columns(self, groups: Sequence[FeatureGroup]) -> list[str]:
        cols: list[str] = []
        for g in groups:
            cols += [c for c in self.groups.get(g, []) if c not in cols]
        return cols

    def subset(self, mask: np.ndarray | pd.Series) -> FeatureFrame:
        m = np.asarray(mask, dtype=bool)
        return FeatureFrame(self.X[m], self.meta[m], self.y[m], self.benchmarks[m], self.groups)

    def __len__(self) -> int:
        return len(self.X)


class FeatureBuilder:
    def __init__(self, snapshot: Snapshot, cfg: ModelConfig) -> None:
        self.cfg = cfg
        self.energy = snapshot.energy_wide()
        self.energy_long = snapshot.energy
        self.capacity = snapshot.capacity_wide()
        self.weather = WeatherTable(snapshot.weather, cfg.weather_features)
        local = self.energy.index.tz_convert(BERLIN)
        self.local_day = pd.DatetimeIndex(local.normalize().tz_localize(None))
        self.daily_mean = self.energy.groupby(self.local_day).mean()
        self._hours: dict[date, pd.DatetimeIndex] = {}

    # --- Wissen zum Ausgabezeitpunkt ---------------------------------------------------------

    def _known_until(self, code: str, issue: pd.Timestamp) -> pd.Timestamp:
        """Erste Stunde (Beginn), die zum Ausgabezeitpunkt noch nicht bekannt ist."""
        if code.startswith(PRICE_PREFIX):
            local = issue.tz_convert(BERLIN)
            last_day = local.date() + timedelta(
                days=1 if local.hour >= self.cfg.issue.price_known_local_hour else 0
            )
            return local_day_hours(last_day)[-1] + H
        return (issue - pd.Timedelta(hours=self.cfg.issue.actual_delay_h) - H).floor("h") + H

    # --- Zeilen ------------------------------------------------------------------------------

    def hours(self, day: date) -> pd.DatetimeIndex:
        h = self._hours.get(day)
        if h is None:
            h = self._hours[day] = local_day_hours(day)
        return h

    def rows(self, issues: Sequence[pd.Timestamp]) -> pd.DataFrame:
        it, ts, ld = [], [], []
        for t0 in issues:
            issue_local = t0.tz_convert(BERLIN).date()
            t0_ns = np.datetime64(t0.tz_convert("UTC").tz_localize(None), "ns")
            for lead in self.cfg.issue.lead_days:
                hrs = self.hours(issue_local + timedelta(days=lead))
                it.append(np.full(len(hrs), t0_ns))
                ts.append(hrs.tz_convert("UTC").tz_localize(None).to_numpy("datetime64[ns]"))
                ld.append(np.full(len(hrs), lead, dtype=np.int16))
        if not it:
            return pd.DataFrame(columns=META_COLUMNS)
        meta = pd.DataFrame(
            {
                "issue_time": pd.DatetimeIndex(np.concatenate(it)).tz_localize("UTC"),
                "ts": pd.DatetimeIndex(np.concatenate(ts)).tz_localize("UTC"),
                "lead_day": np.concatenate(ld),
            }
        )
        meta["lead_h"] = ((meta["ts"] - meta["issue_time"]) / H).astype(np.float32)
        return meta

    # --- Merkmale ----------------------------------------------------------------------------

    def issues(self, issue_days: Sequence[date]) -> list[pd.Timestamp]:
        return [issue_time(d, self.cfg.issue.hour_utc) for d in issue_days]

    def build(self, issue_days: Sequence[date], *, need_weather: bool = False) -> FeatureFrame:
        """Merkmale für die täglichen Ausgabezeitpunkte der ``issue_days``."""
        return self.build_at(self.issues(issue_days), need_weather=need_weather)

    def build_at(
        self, issues: Sequence[pd.Timestamp], *, need_weather: bool = False
    ) -> FeatureFrame:
        """Merkmale für beliebige Ausgabezeitpunkte (Inferenz)."""
        meta = self.rows(issues)
        groups: dict[str, list[str]] = {}
        parts: list[pd.DataFrame] = []

        cal = calendar_features(pd.DatetimeIndex(meta["ts"])).reset_index(drop=True)
        cal[["lead_day", "lead_h"]] = meta[["lead_day", "lead_h"]].to_numpy()
        groups["calendar"] = [*CALENDAR_COLUMNS, "lead_day", "lead_h"]
        parts.append(cal)

        cap = self._capacity(meta)
        groups["capacity"] = list(cap.columns)
        parts.append(cap)

        lags = self._lags(meta)
        groups["lags"] = list(lags.columns)
        parts.append(lags)

        wx = self._weather(meta)
        groups["weather"] = list(wx.columns)
        parts.append(wx)

        for name, sg in self.cfg.series_groups.items():
            extra = self._series_group(sg, meta)
            groups[name] = list(extra.columns)
            parts.append(extra)

        X = pd.concat(parts, axis=1)
        y = pd.DataFrame({c: self._lookup(c, meta["ts"]) for c in self.cfg.targets}, index=X.index)
        bench_codes = sorted({b for t in self.cfg.stage1 + self.cfg.stage2 for b in t.benchmarks})
        bench = pd.DataFrame({c: self._lookup(c, meta["ts"]) for c in bench_codes}, index=X.index)
        frame = FeatureFrame(X, meta, y, bench, groups)
        if need_weather and groups["weather"]:
            frame = frame.subset(X["wx_lead_h"].notna().to_numpy())
        return frame

    def _lookup(self, code: str, ts: pd.Series | pd.DatetimeIndex) -> np.ndarray:
        if code not in self.energy.columns:
            return np.full(len(ts), np.nan)
        out: np.ndarray = self.energy[code].reindex(pd.DatetimeIndex(ts)).to_numpy(np.float64)
        return out

    def _capacity(self, meta: pd.DataFrame) -> pd.DataFrame:
        if self.capacity.empty:
            return pd.DataFrame(index=meta.index)
        days = pd.DatetimeIndex(
            pd.DatetimeIndex(meta["ts"]).tz_convert(BERLIN).normalize().tz_localize(None)
        )
        cap = self.capacity.reindex(self.capacity.index.union(days.unique())).sort_index().ffill()
        out = cap.reindex(days).reset_index(drop=True).astype(np.float32)
        out.index = meta.index
        if {"cap_pv_roof", "cap_pv_ground"} <= set(out.columns):
            out["cap_pv"] = out["cap_pv_roof"] + out["cap_pv_ground"]
        return out

    def lag_codes(self) -> list[str]:
        codes = [t.code for t in self.cfg.stage1 + self.cfg.stage2]
        return [c for c in dict.fromkeys(codes + self.cfg.lag_series) if c in self.energy.columns]

    def _lags(self, meta: pd.DataFrame) -> pd.DataFrame:
        return self._lag_block(self.lag_codes(), LAG_KINDS, meta)

    def _lag_block(
        self, codes: Sequence[str], kinds: Sequence[str], meta: pd.DataFrame
    ) -> pd.DataFrame:
        ts = pd.DatetimeIndex(meta["ts"])
        issues = pd.DatetimeIndex(meta["issue_time"])
        uniq = issues.unique()
        target_day = pd.DatetimeIndex(ts.tz_convert(BERLIN).normalize().tz_localize(None))
        out: dict[str, np.ndarray] = {}
        for code in codes:
            if code not in self.energy.columns:
                continue
            known = {i: self._known_until(code, i) for i in uniq}
            known_arr = pd.DatetimeIndex([known[i] for i in issues])
            last_day = {
                i: (known[i].tz_convert(BERLIN) - H).date() for i in uniq
            }  # Tag der letzten bekannten Stunde
            full_day = {
                i: d if (known[i].tz_convert(BERLIN).hour == 0) else d - timedelta(days=1)
                for i, d in last_day.items()
            }
            full_arr = pd.DatetimeIndex([pd.Timestamp(full_day[i]) for i in issues])
            k_days = ((target_day - full_arr) / DAY).astype(int)
            s = self.energy[code]
            dm = self.daily_mean[code]
            if "day" in kinds:
                out[f"lag_{code}_day"] = s.reindex(ts - k_days.to_numpy() * DAY).to_numpy()
            if "week" in kinds:
                # gleiche Stunde vor m Wochen, kleinstes m mit bekanntem Wert
                m = np.ceil((ts - known_arr + H) / (7 * DAY)).astype(int)
                m = np.maximum(m, 1)
                out[f"lag_{code}_week"] = s.reindex(ts - m * 7 * DAY).to_numpy()
            if "mean_day" in kinds:
                out[f"lag_{code}_mean_day"] = dm.reindex(full_arr).to_numpy()
            if "mean_7d" in kinds:
                roll7 = dm.rolling(7, min_periods=5).mean()
                out[f"lag_{code}_mean_7d"] = roll7.reindex(full_arr).to_numpy()
            if "mean_28d" in kinds:
                roll28 = dm.rolling(28, min_periods=20).mean()
                out[f"lag_{code}_mean_28d"] = roll28.reindex(full_arr).to_numpy()
            if "last" in kinds:
                out[f"lag_{code}_last"] = s.reindex(known_arr - H).to_numpy()
        df = pd.DataFrame(out, index=meta.index).astype(np.float32)
        return df

    # --- Reihengruppen -----------------------------------------------------------------------

    def _series_group(self, sg: SeriesGroup, meta: pd.DataFrame) -> pd.DataFrame:
        if sg.mode == "lags":
            return self._lag_block(sg.codes, sg.lags, meta)
        if sg.mode == "forecast":
            return self._forecast(sg, meta)
        return self._asof(sg, meta)

    def _forecast(self, sg: SeriesGroup, meta: pd.DataFrame) -> pd.DataFrame:
        """Wert der Zielstunde, wenn die Prognose ihres Liefertags schon veröffentlicht war."""
        ts = pd.DatetimeIndex(meta["ts"])
        day = ts.tz_convert(BERLIN).normalize().tz_localize(None)
        published = {
            d: (d - DAY + pd.Timedelta(hours=sg.known_local_hour)).tz_localize(BERLIN)
            for d in day.unique()
        }
        pub = pd.DatetimeIndex([published[d] for d in day]).tz_convert("UTC")
        known = np.asarray(pd.DatetimeIndex(meta["issue_time"]) >= pub)
        out: dict[str, np.ndarray] = {}
        for code in sg.codes:
            if code not in self.energy.columns:
                continue
            v = self.energy[code].reindex(ts).to_numpy(np.float64)
            out[f"fc_{code}"] = np.where(known, v, np.nan)
        return pd.DataFrame(out, index=meta.index).astype(np.float32)

    def _asof(self, sg: SeriesGroup, meta: pd.DataFrame) -> pd.DataFrame:
        """Letzter veröffentlichter Wert je Ausgabezeitpunkt und Änderung über ``change_days``."""
        e = self.energy_long
        if "published" not in e.columns:
            raise ValueError(
                "Snapshot ohne Spalte published: mit aktuellem ml-train neu exportieren"
            )
        issues = pd.DatetimeIndex(meta["issue_time"])
        uniq = pd.DataFrame({"issue": issues.unique().sort_values().as_unit("ns")})
        uniq["before"] = uniq["issue"] - pd.Timedelta(days=sg.change_days)
        out: dict[str, np.ndarray] = {}
        for code in sg.codes:
            s = e.loc[(e["code"] == code) & e["published"].notna(), ["published", "value"]]
            if s.empty:
                continue
            s = s.sort_values("published").astype({"value": np.float64})
            s["published"] = pd.DatetimeIndex(s["published"]).as_unit("ns")
            now = pd.merge_asof(uniq, s, left_on="issue", right_on="published")["value"]
            then = pd.merge_asof(
                uniq.sort_values("before"), s, left_on="before", right_on="published"
            )["value"]
            cur = pd.Series(now.to_numpy(), index=uniq["issue"])
            chg = pd.Series(now.to_numpy() - then.to_numpy(), index=uniq["issue"])
            out[f"asof_{code}"] = cur.reindex(issues).to_numpy()
            out[f"asof_{code}_chg"] = chg.reindex(issues).to_numpy()
        return pd.DataFrame(out, index=meta.index).astype(np.float32)

    def _weather(self, meta: pd.DataFrame) -> pd.DataFrame:
        cols = [*self.weather.columns, "wx_interp", "wx_lead_h"]
        if not self.weather.columns:
            return pd.DataFrame(index=meta.index)
        issues = meta["issue_time"]
        run_of = {
            i: self.weather.run_for(pd.Timestamp(i), self.cfg.issue.nwp_delay_h)
            for i in issues.unique()
        }
        runs = issues.map(run_of)
        valid = runs.notna().to_numpy()
        out = pd.DataFrame(np.nan, index=meta.index, columns=cols, dtype=np.float32)
        if valid.any():
            key = pd.MultiIndex.from_arrays(
                [pd.DatetimeIndex(runs[valid]), pd.DatetimeIndex(meta["ts"][valid])],
                names=["run_time", "ts"],
            )
            vals = self.weather.table.reindex(key)[cols].to_numpy()
            out.loc[valid, cols] = vals
        return out


def issue_days_between(start: date, end: date, stride: int = 1) -> list[date]:
    """Ausgabetage ``start`` bis ``end`` (einschließlich), jeder ``stride``-te."""
    n = (end - start).days + 1
    return [start + timedelta(days=i) for i in range(0, max(n, 0), stride)]
