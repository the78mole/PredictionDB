"""Merkmale je Ausgabezeitpunkt und Zielstunde, streng mit dem Wissen zum Ausgabezeitpunkt.

Eine Zeile ist ein Paar (``issue_time``, ``ts``): Prognose ausgegeben zu ``issue_time`` für die
Stunde ab ``ts`` (UTC, Intervallbeginn wie in ``energy.value``). Ausgegeben wird einmal am Tag
zur Stunde ``issue.hour_utc``, fürs Training mit ``issue.hours_utc`` reihum zu verschiedenen
Stunden; Ziele sind die ganzen Liefertage (Ortszeit) ``issue.lead_days`` nach dem Ausgabetag,
also 23, 24 oder 25 Stunden je Tag, und für Vorlauftag 0 der Rest des Ausgabetags.

Ist-Werte, die zum Ausgabezeitpunkt schon bekannt sind (Day-Ahead-Preis des Folgetags ab
13 Uhr), sind kein Lernziel (``y`` leer); ``known`` trägt sie, damit die Inferenz sie übernimmt.

Leakage-Schutz (ARCHITEKTUR.md 7.4):

- Ist-Werte gelten ab Intervallende plus ``actual_delay_h`` als bekannt.
- Day-Ahead-Preise eines Liefertags gelten am Vortag ab ``price_known_local_hour`` als bekannt.
- Wetter kommt aus dem jüngsten Lauf mit ``run_time + nwp_delay_h <= issue_time``, das
  GEFS-Ensemble (Gruppe ``gefs``) aus dem jüngsten GEFS-Lauf mit
  ``run_time + ensemble_weather.delay_h <= issue_time``.
- Prognosen Dritter (Lastprognose der ÜNB) sind nur als eigene Reihengruppe (``forecast``)
  Merkmal, und nur für Liefertage, deren Prognose zum Ausgabezeitpunkt veröffentlicht war.
- Tages-, Wochen- und Monatswerte (Brennstoffpreise, Marktwerte) gelten ab ihrer Spalte
  ``published`` im Snapshot als bekannt.
- Der Grenzkostenindex (``grenzkosten``) nutzt nur Stunden, in denen Preis und Erzeugung
  zum Ausgabezeitpunkt beide bekannt sind.

Wetter liegt im Snapshot in nativen Schritten (GFS v16 stündlich bis 120 h, danach
dreistündlich). Erst hier wird auf Stunden interpoliert (Vorgabe: Interpolation beim Lesen):
Momentanwerte linear, Intervallgrößen (Strahlung, PV, Niederschlag, Schneeanteil) gelten für
das ganze Intervall bis zum Schritt. Auf die Energiestunde ``[ts, ts+1h)`` passen Intervallwerte
mit Gültigkeit ``ts+1h`` und das Mittel der Momentanwerte bei ``ts`` und ``ts+1h``. GEFS liegt
6-stündlich vor; Mittel und Summen beziehen sich dort auf die 6 h vor dem Schritt.
``wx_interp`` markiert interpolierte Stunden.

@relation(SWA-MLT-DATASET, scope=file)
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta

import numpy as np
import pandas as pd

from ml_train.calendar import BERLIN, CALENDAR_COLUMNS, calendar_features
from ml_train.config import (
    ENSEMBLE_PREFIX,
    NEIGHBOUR_PREFIX,
    SITE_PREFIX,
    FeatureGroup,
    ModelConfig,
    SeriesGroup,
    neighbour_feature,
    zone_country,
)
from ml_train.snapshot import Snapshot

H = pd.Timedelta(hours=1)
DAY = pd.Timedelta(days=1)

#: Intervallgrößen (Wert gilt für das Intervall bis zur Gültigkeitszeit)
INTERVAL_SUFFIXES = ("_ghi", "_pv_cf", "_pv_power", "_precip", "_snow_fraction")
#: Kennzahlen über die Ensemble-Mitglieder (GEFS-Gold), angehängt an den Namen der Größe
ENSEMBLE_STATS = ("_mean", "_std", "_p10", "_p90")
#: Day-Ahead-Preise (DE-LU und Nachbarzonen): bekannt ab dem Auktionsergebnis am Vortag
PRICE_PREFIX = "da_price_"
LAG_KINDS = ("day", "week", "mean_day", "mean_7d", "last")
#: Erster Lauf je GFS-Hauptversion (Merkmalsgruppe ``nwp_version``, Wind-Bias je Version,
#: docs/analysen/gfs-v15-v16); ältere Läufe stammen aus NCAR GDEX d084001
#: Grenzkostenindex: Auslastung je Energieträger relativ zum 5-/95-%-Quantil der letzten 30
#: Tage; Braunkohle preissetzend = Braunkohle in Teillast, Steinkohle und Gas fast aus
UTIL_DAYS = 30
UTIL_Q = (0.05, 0.95)
LIGNITE_PART = (0.2, 0.8)
OTHERS_LOW = 0.2
#: Residuallast "hoch" = oberes Quartil des Fensters; Mindestzahl Stunden je Klasse
HIGH_Q = 0.75
MIN_HOURS = 6
GFS_VERSIONS = (
    (pd.Timestamp("2021-03-22 12:00", tz="UTC"), 16),
    (pd.Timestamp("2019-06-12 12:00", tz="UTC"), 15),
    (pd.Timestamp("2017-07-19 12:00", tz="UTC"), 14),
    (pd.Timestamp("2015-01-14 12:00", tz="UTC"), 13),
)
META_COLUMNS = ["issue_time", "ts", "lead_day", "lead_h"]


def is_interval_feature(name: str) -> bool:
    """@relation(SWR-MLT-023, SWR-MLT-034, scope=function)"""
    if name.startswith((ENSEMBLE_PREFIX, f"wx_{ENSEMBLE_PREFIX}")):
        for stat in ENSEMBLE_STATS:
            if name.endswith(stat):
                name = name.removesuffix(stat)
                break
    return name.endswith(INTERVAL_SUFFIXES)


def local_day_hours(day: date) -> pd.DatetimeIndex:
    """Stundenbeginne (UTC) des Liefertags ``day`` in Ortszeit, 23 bis 25 Stunden.

    @relation(SWR-MLT-020, scope=function)
    """
    start = pd.Timestamp(day).tz_localize(BERLIN)
    end = pd.Timestamp(day + timedelta(days=1)).tz_localize(BERLIN)
    return pd.date_range(start.tz_convert("UTC"), end.tz_convert("UTC"), freq="h", inclusive="left")


def same_local_time(ts: pd.DatetimeIndex, days: np.ndarray) -> tuple[pd.DatetimeIndex, ...]:
    """Dieselbe Uhrzeit (Europe/Berlin) ``days`` Ortstage vor ``ts``, als UTC-Stundenbeginne.

    Gibt es die Uhrzeit am Bezugstag doppelt (Oktober, 02 Uhr), kommen beide Stunden zurück
    (erste und zweite); sonst sind beide gleich. Fehlt sie (März, 02 Uhr), gilt 03 Uhr.

    @relation(SWR-MLT-025, scope=function)
    """
    wall = ts.tz_convert(BERLIN).tz_localize(None) - pd.to_timedelta(np.asarray(days), unit="D")
    n = len(wall)
    first = wall.tz_localize(BERLIN, ambiguous=np.ones(n, bool), nonexistent="shift_forward")
    second = wall.tz_localize(BERLIN, ambiguous=np.zeros(n, bool), nonexistent="shift_forward")
    return first.tz_convert("UTC"), second.tz_convert("UTC")


def issue_time(day: date, hour_utc: int) -> pd.Timestamp:
    """@relation(SWR-MLT-020, scope=function)"""
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
        """@relation(SWR-MLT-023, scope=function)"""
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
        """Jüngster Lauf, der zum Ausgabezeitpunkt verarbeitet vorlag.

        @relation(SWR-MLT-022, scope=function)
        """
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
    #: Ist-Werte je Ziel, die zum Ausgabezeitpunkt schon bekannt sind, sonst leer
    known: pd.DataFrame | None = None

    def columns(self, groups: Sequence[FeatureGroup]) -> list[str]:
        cols: list[str] = []
        for g in groups:
            cols += [c for c in self.groups.get(g, []) if c not in cols]
        return cols

    def subset(self, mask: np.ndarray | pd.Series) -> FeatureFrame:
        m = np.asarray(mask, dtype=bool)
        known = None if self.known is None else self.known[m]
        return FeatureFrame(
            self.X[m], self.meta[m], self.y[m], self.benchmarks[m], self.groups, known
        )

    def __len__(self) -> int:
        return len(self.X)


class FeatureBuilder:
    def __init__(self, snapshot: Snapshot, cfg: ModelConfig) -> None:
        self.cfg = cfg
        self.energy = snapshot.energy_wide()
        self.energy_long = snapshot.energy
        self.capacity = snapshot.capacity_wide()
        wx = snapshot.weather
        feat = wx["feature"].astype(str)
        nb = feat.str.startswith(NEIGHBOUR_PREFIX)
        st = feat.str.startswith(SITE_PREFIX)
        en = feat.str.startswith(ENSEMBLE_PREFIX)
        self.weather = WeatherTable(wx[~nb & ~st & ~en], cfg.weather_features)
        # Nachbarzonen: eigene Tabelle, damit wx_interp und die Stunden des DE-Wetters gleich
        # bleiben; gewählt wird derselbe Lauf wie fürs DE-Wetter
        self.neighbour = WeatherTable(wx[nb])
        # Kraftwerksstandorte: Tagesmittel je Lauf (gleitende Mittel über Vortage)
        self.sites = WeatherTable(wx[st])
        # GEFS: eigene Läufe (nur 00Z, 6-stündlich), eigener Verzug
        self.ensemble = WeatherTable(wx[en])
        self._site_days: tuple[dict[pd.Timestamp, pd.DataFrame], pd.DataFrame] | None = None
        local = self.energy.index.tz_convert(BERLIN)
        self.local_day = pd.DatetimeIndex(local.normalize().tz_localize(None))
        self.daily_mean = self.energy.groupby(self.local_day).mean()
        self._hours: dict[date, pd.DatetimeIndex] = {}
        self._gk_hours: dict[str, pd.DataFrame] = {}
        # Erste Stunde nach dem letzten vorhandenen Wert je Reihe (Grenze für „bekannt“)
        self._present_until = {c: self.energy[c].last_valid_index() for c in self.energy.columns}

    # --- Wissen zum Ausgabezeitpunkt ---------------------------------------------------------

    def _known_until(self, code: str, issue: pd.Timestamp) -> pd.Timestamp:
        """Erste Stunde (Beginn), die zum Ausgabezeitpunkt noch nicht bekannt ist.

        @relation(SWR-MLT-021, scope=function)
        """
        if code.startswith(PRICE_PREFIX):
            local = issue.tz_convert(BERLIN)
            last_day = local.date() + timedelta(
                days=1 if local.hour >= self.cfg.issue.price_known_local_hour else 0
            )
            rule = local_day_hours(last_day)[-1] + H
            # Im Betrieb kann die Auktion für morgen schon gelaufen, aber noch nicht abgerufen
            # sein: dann gilt der letzte vorhandene Tag, sonst wäre das Tagesmittel (center der
            # Preisprognose) leer und die ganze Preisprognose NaN.
            present = self._present_until.get(code)
            if present is not None and present + H < rule:
                return present + H
            return rule
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
                if lead == 0:
                    hrs = hrs[hrs >= t0]
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
        """Ein Ausgabezeitpunkt je Tag; mit mehreren ``train_hours`` reihum nach Datum.

        @relation(SWR-MLT-020, scope=function)
        """
        hours = self.cfg.issue.train_hours
        return [issue_time(d, hours[d.toordinal() % len(hours)]) for d in issue_days]

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

        runs = self._weather_runs(meta)
        wx = self._weather(meta, runs)
        groups["weather"] = list(wx.columns)
        parts.append(wx)
        nbw = self._neighbour_weather(meta, runs)
        groups["nachbarwetter"] = list(nbw.columns)
        parts.append(nbw)
        ens = self._ensemble(meta)
        groups["gefs"] = list(ens.columns)
        parts.append(ens)
        ver = pd.DataFrame({"wx_gfs_v": gfs_version(runs)}, index=meta.index)
        groups["nwp_version"] = list(ver.columns)
        parts.append(ver)

        for name, sg in self.cfg.series_groups.items():
            if sg.mode == "nachbarindex":
                extra = self._neighbour_index(sg, meta, nbw)
            elif sg.mode == "kuehlwasser":
                extra = self._cooling(meta)
            else:
                extra = self._series_group(sg, meta)
            groups[name] = list(extra.columns)
            parts.append(extra)

        X = pd.concat(parts, axis=1)
        y = pd.DataFrame({c: self._lookup(c, meta["ts"]) for c in self.cfg.targets}, index=X.index)
        known = pd.DataFrame(index=X.index)
        for c in self.cfg.targets:
            kn = self._known_mask(c, meta)
            known[c] = np.where(kn, y[c].to_numpy(), np.nan)
            y.loc[kn, c] = np.nan
        bench_codes = sorted({b for t in self.cfg.stage1 + self.cfg.stage2 for b in t.benchmarks})
        bench = pd.DataFrame({c: self._lookup(c, meta["ts"]) for c in bench_codes}, index=X.index)
        frame = FeatureFrame(X, meta, y, bench, groups, known)
        if need_weather and groups["weather"]:
            frame = frame.subset(X["wx_lead_h"].notna().to_numpy())
        return frame

    def _known_mask(self, code: str, meta: pd.DataFrame) -> np.ndarray:
        """Zielstunden, deren Ist-Wert zum Ausgabezeitpunkt schon veröffentlicht ist.

        @relation(SWR-MLT-021, scope=function)
        """
        issues = pd.DatetimeIndex(meta["issue_time"])
        uniq = issues.unique()
        until = pd.Series([self._known_until(code, i) for i in uniq], index=uniq)
        ts = pd.DatetimeIndex(meta["ts"]).tz_convert("UTC").tz_localize(None).to_numpy()
        lim = pd.DatetimeIndex(until.reindex(issues)).tz_convert("UTC").tz_localize(None)
        return np.asarray(ts < lim.to_numpy(), dtype=bool)

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
        """@relation(SWR-MLT-021, SWR-MLT-025, scope=function)"""
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
            local = code in self.cfg.lag_local_time
            if "day" in kinds:
                out[f"lag_{code}_day"] = self._back(s, ts, k_days.to_numpy(), local)
            if "week" in kinds:
                # gleiche Stunde vor m Wochen, kleinstes m mit bekanntem Wert
                m = np.ceil((ts - known_arr + H) / (7 * DAY)).astype(int)
                m = np.maximum(m, 1)
                if local:
                    # Ortszeit kann eine Stunde später in UTC liegen als m·168 h
                    _, later = same_local_time(ts, 7 * m)
                    m = np.where(later >= known_arr, m + 1, m)
                out[f"lag_{code}_week"] = self._back(s, ts, 7 * m, local)
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

    @staticmethod
    def _back(s: pd.Series, ts: pd.DatetimeIndex, days: np.ndarray, local: bool) -> np.ndarray:
        """Wert ``days`` Tage vor ``ts``: feste 24 h je Tag oder dieselbe Ortsuhrzeit.

        @relation(SWR-MLT-025, scope=function)
        """
        out: np.ndarray
        if not local:
            out = s.reindex(ts - days * DAY).to_numpy()
            return out
        first, second = same_local_time(ts, days)
        a = s.reindex(first).to_numpy(np.float64)
        b = s.reindex(second).to_numpy(np.float64)
        # doppelte Stunde im Oktober: Mittel beider Stunden
        out = np.where(np.isnan(a), b, np.where(np.isnan(b), a, (a + b) / 2))
        return out

    # --- Reihengruppen -----------------------------------------------------------------------

    def _series_group(self, sg: SeriesGroup, meta: pd.DataFrame) -> pd.DataFrame:
        """@relation(SWR-MLT-026, scope=function)"""
        if sg.mode == "lags":
            return self._lag_block(sg.codes, sg.lags, meta)
        if sg.mode == "forecast":
            return self._forecast(sg, meta)
        if sg.mode == "outlook":
            return self._outlook(sg, meta)
        if sg.mode == "grenzkosten":
            return self._grenzkosten(sg, meta)
        return self._asof(sg, meta)

    def _forecast(self, sg: SeriesGroup, meta: pd.DataFrame) -> pd.DataFrame:
        """Wert der Zielstunde, wenn die Prognose ihres Liefertags schon veröffentlicht war.

        @relation(SWR-MLT-021, SWR-MLT-026, scope=function)
        """
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
        """Letzter veröffentlichter Wert je Ausgabezeitpunkt und Änderung über ``change_days``.

        @relation(SWR-MLT-021, SWR-MLT-026, scope=function)
        """
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

    def _outlook(self, sg: SeriesGroup, meta: pd.DataFrame) -> pd.DataFrame:
        """Erwartete log-Änderung je Vorlauf aus der jüngsten veröffentlichten Ausgabe.

        Eine Ausgabe gilt erst als bekannt, wenn alle ihre Werte veröffentlicht sind; Vorlauf und
        Bezugswert (laufender Monat) stammen immer aus derselben Ausgabe.

        @relation(SWR-MLT-026, scope=function)
        """
        e = self.energy_long
        if "published" not in e.columns:
            raise ValueError(
                "Snapshot ohne Spalte published: mit aktuellem ml-train neu exportieren"
            )
        issues = pd.DatetimeIndex(meta["issue_time"])
        uniq = pd.DataFrame({"issue": issues.unique().sort_values().as_unit("ns")})
        ks = sorted(set(sg.horizons))
        out: dict[str, np.ndarray] = {}
        for stem in sg.codes:
            cols = {f"{stem}_h{k:02d}": k for k in sorted({0, *ks})}
            s = e.loc[e["code"].isin(list(cols)) & e["published"].notna()]
            if s.empty:
                continue
            wide = s.pivot_table(index="ts", columns="code", values="value", aggfunc="last")
            pub = s.groupby("ts")["published"].max()
            wide = wide.reindex(columns=list(cols)).rename(columns=cols)
            wide["published"] = pd.DatetimeIndex(pub.reindex(wide.index)).as_unit("ns")
            wide = wide.sort_values("published").reset_index(drop=True)
            cur = pd.merge_asof(uniq, wide, left_on="issue", right_on="published")
            cur.index = uniq["issue"]
            for k in ks:
                with np.errstate(divide="ignore", invalid="ignore"):
                    v = np.log(cur[k].to_numpy(np.float64) / cur[0].to_numpy(np.float64))
                out[f"ol_{stem}_h{k}"] = pd.Series(v, index=cur.index).reindex(issues).to_numpy()
        return pd.DataFrame(out, index=meta.index).astype(np.float32)

    def _gk_classes(self, sg: SeriesGroup) -> pd.DataFrame:
        """Stündlich: Preis, Residuallast und ob Braunkohle preissetzend ist."""
        key = f"{sg.codes[0]}|{sg.residual}|{sg.lignite}|{sg.hard_coal}|{sg.gas}"
        cached = self._gk_hours.get(key)
        if cached is not None:
            return cached
        e = self.energy
        need = [sg.codes[0], sg.residual]
        if any(c not in e.columns for c in need):
            out = pd.DataFrame(columns=["price", "residual", "lignite"])
        else:
            idx = pd.date_range(e.index.min(), e.index.max(), freq="h")
            win = UTIL_DAYS * 24

            def util(code: str) -> pd.Series:
                if code not in e.columns:
                    return pd.Series(np.nan, index=idx)
                g = e[code].reindex(idx)
                lo = g.rolling(win, min_periods=win // 3).quantile(UTIL_Q[0])
                hi = g.rolling(win, min_periods=win // 3).quantile(UTIL_Q[1])
                return ((g - lo) / (hi - lo).where(hi - lo > 0)).clip(0, 1)

            price = e[sg.codes[0]].reindex(idx)
            lig, coal, gas = util(sg.lignite), util(sg.hard_coal), util(sg.gas)
            marginal = (
                lig.between(*LIGNITE_PART) & (coal < OTHERS_LOW) & (gas < OTHERS_LOW) & (price > 0)
            )
            out = pd.DataFrame(
                {"price": price, "residual": e[sg.residual].reindex(idx), "lignite": marginal}
            )
        self._gk_hours[key] = out
        return out

    def _grenzkosten(self, sg: SeriesGroup, meta: pd.DataFrame) -> pd.DataFrame:
        """Preisniveau in Stunden hoher Residuallast und in Braunkohle-Stunden je Fenster.

        @relation(SWR-MLT-026, scope=function)
        """
        cls = self._gk_classes(sg)
        issues = pd.DatetimeIndex(meta["issue_time"])
        uniq = issues.unique()
        windows = sorted(set(sg.windows))
        names = [f"gk_{k}_{w}d" for w in windows for k in ("hoch", "braunkohle")]
        rows: dict[pd.Timestamp, list[float]] = {}
        if len(cls):
            t = cls.index
            price = cls["price"].to_numpy(np.float64)
            resid = cls["residual"].to_numpy(np.float64)
            lig = cls["lignite"].to_numpy(bool)
            for i in uniq:
                end = min(self._known_until(sg.codes[0], i), self._known_until(sg.residual, i))
                hi = int(t.searchsorted(end))
                vals: list[float] = []
                for w in windows:
                    lo = int(t.searchsorted(end - pd.Timedelta(days=w)))
                    p, r, m = price[lo:hi], resid[lo:hi], lig[lo:hi]
                    ok = np.isfinite(p) & np.isfinite(r)
                    high = ok & (r >= np.quantile(r[ok], HIGH_Q)) if ok.sum() else ok
                    brk = ok & m
                    vals.append(float(np.median(p[high])) if high.sum() >= MIN_HOURS else np.nan)
                    vals.append(float(np.median(p[brk])) if brk.sum() >= MIN_HOURS else np.nan)
                rows[i] = vals
        tab = pd.DataFrame.from_dict(rows, orient="index", columns=names)
        tab = tab.reindex(uniq)
        # Bezugsniveau: kürzestes Fenster mit Wert, nach unten begrenzt (Ziel wird dadurch geteilt)
        niveau = pd.Series(np.nan, index=tab.index)
        for w in windows:
            niveau = niveau.fillna(tab[f"gk_hoch_{w}d"])
        tab["gk_niveau"] = niveau.clip(lower=sg.floor)
        # letztes vollständig bekanntes Tagesmittel des Preises relativ zum Niveau
        if sg.codes[0] in self.daily_mean.columns:
            dm = self.daily_mean[sg.codes[0]]
            full = []
            for i in tab.index:
                known = self._known_until(sg.codes[0], i).tz_convert(BERLIN)
                d = (known - H).date()
                full.append(pd.Timestamp(d if known.hour == 0 else d - timedelta(days=1)))
            tab["gk_vortag_rel"] = (
                dm.reindex(pd.DatetimeIndex(full)).to_numpy() / tab["gk_niveau"].to_numpy()
            )
        out = tab.reindex(issues)
        out.index = meta.index
        return out.astype(np.float32)

    def _weather_runs(self, meta: pd.DataFrame) -> pd.Series:
        """Wetterlauf je Zeile: der jüngste, der zum Ausgabezeitpunkt verarbeitet vorlag.

        @relation(SWR-MLT-022, scope=function)
        """
        issues = meta["issue_time"]
        run_of = {
            i: self.weather.run_for(pd.Timestamp(i), self.cfg.issue.nwp_delay_h)
            for i in issues.unique()
        }
        return issues.map(run_of)

    def _weather(self, meta: pd.DataFrame, runs: pd.Series) -> pd.DataFrame:
        cols = [*self.weather.columns, "wx_interp", "wx_lead_h"]
        if not self.weather.columns:
            return pd.DataFrame(index=meta.index)
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

    def _neighbour_weather(self, meta: pd.DataFrame, runs: pd.Series) -> pd.DataFrame:
        """Features der Nachbarzonen aus dem Lauf des DE-Wetters (fehlt er dort: leer).

        @relation(SWR-MLT-028, scope=function)
        """
        cols = self.neighbour.columns
        out = pd.DataFrame(np.nan, index=meta.index, columns=cols, dtype=np.float32)
        valid = runs.notna().to_numpy()
        if cols and valid.any():
            key = pd.MultiIndex.from_arrays(
                [pd.DatetimeIndex(runs[valid]), pd.DatetimeIndex(meta["ts"][valid])],
                names=["run_time", "ts"],
            )
            out.loc[valid, cols] = self.neighbour.table.reindex(key)[cols].to_numpy()
        return out

    def _ensemble(self, meta: pd.DataFrame) -> pd.DataFrame:
        """GEFS-Kennzahlen aus dem jüngsten GEFS-Lauf, der zum Ausgabezeitpunkt verarbeitet
        vorlag, dazu ``wx_en_lead_h`` (Stunden seit diesem Lauf). Ohne Lauf: leer (NaN).

        @relation(SWR-MLT-034, scope=function)
        """
        en = self.cfg.ensemble_weather
        cols = self.ensemble.columns
        if en is None or not cols:
            return pd.DataFrame(index=meta.index)
        issues = meta["issue_time"]
        run_of = {i: self.ensemble.run_for(pd.Timestamp(i), en.delay_h) for i in issues.unique()}
        runs = issues.map(run_of)
        names = [*cols, f"wx_{ENSEMBLE_PREFIX}lead_h"]
        out = pd.DataFrame(np.nan, index=meta.index, columns=names, dtype=np.float32)
        valid = runs.notna().to_numpy()
        if valid.any():
            key = pd.MultiIndex.from_arrays(
                [pd.DatetimeIndex(runs[valid]), pd.DatetimeIndex(meta["ts"][valid])],
                names=["run_time", "ts"],
            )
            out.loc[valid, names] = self.ensemble.table.reindex(key)[
                [*cols, "wx_lead_h"]
            ].to_numpy()
        return out

    def _newest_published(self, code: str, issues: pd.DatetimeIndex) -> pd.DataFrame:
        """Jüngster zum Ausgabezeitpunkt veröffentlichter Stand einer Reihe (``ts``, ``value``).

        Unter den schon veröffentlichten Werten gilt der mit dem jüngsten ``ts``, nicht der
        zuletzt veröffentlichte: ein Vorjahreswert erscheint regulär erst nach dem laufenden
        Jahr, ein nachgereichter Tageswert nach dem folgenden Tag.

        @relation(SWR-MLT-029, SWR-MLT-033, scope=function)
        """
        e = self.energy_long
        if "published" not in e.columns:
            raise ValueError(
                "Snapshot ohne Spalte published: mit aktuellem ml-train neu exportieren"
            )
        s = e.loc[(e["code"] == code) & e["published"].notna(), ["ts", "published", "value"]]
        if s.empty:
            ts = pd.DatetimeIndex([pd.NaT] * len(issues), tz="UTC")
            return pd.DataFrame({"ts": ts, "value": np.nan}, index=issues)
        s = s.sort_values("published").astype({"value": np.float64}).reset_index(drop=True)
        s["published"] = pd.DatetimeIndex(s["published"]).as_unit("ns")
        newest = s["ts"].cummax()
        s["value"] = s["value"].where(s["ts"] == newest).ffill()
        s["ts"] = newest
        uniq = pd.DataFrame({"issue": issues.unique().sort_values().as_unit("ns")})
        cur = pd.merge_asof(uniq, s, left_on="issue", right_on="published")
        cur.index = pd.DatetimeIndex(uniq["issue"])
        return cur[["ts", "value"]].reindex(issues)

    def _capacity_asof(self, code: str, issues: pd.DatetimeIndex) -> np.ndarray:
        """Zum Ausgabezeitpunkt veröffentlichter Stand einer Jahresreihe (NaN, wenn keiner).

        @relation(SWR-MLT-029, scope=function)
        """
        out: np.ndarray = self._newest_published(code, issues)["value"].to_numpy(np.float64)
        return out

    # --- Kühlwasser an Kraftwerksstandorten --------------------------------------------------

    def _site_tables(self) -> tuple[dict[pd.Timestamp, pd.DataFrame], pd.DataFrame]:
        """Tagesmittel (Ortstag) je Lauf und die Näherung für vergangene Tage.

        Für einen vergangenen Tag gilt das Tagesmittel aus dem jüngsten Lauf, der vor
        Tagesbeginn lag (Vorlauf bis gut einen Tag, also fast eine Analyse).

        @relation(SWR-MLT-033, scope=function)
        """
        if self._site_days is not None:
            return self._site_days
        cols = [c for c in self.sites.columns if c not in ("wx_interp", "wx_lead_h")]
        per_run: dict[pd.Timestamp, pd.DataFrame] = {}
        past = pd.DataFrame(columns=cols, dtype=np.float64)
        if cols and not self.sites.table.empty:
            t = self.sites.table[cols].astype(np.float64)
            run = t.index.get_level_values("run_time")
            ts = pd.DatetimeIndex(t.index.get_level_values("ts"))
            day = pd.DatetimeIndex(ts.tz_convert(BERLIN).normalize().tz_localize(None))
            g = t.groupby([run, day])
            mean, n = g.mean(), g.count()
            # Teiltage am Laufbeginn zählen ab 6 Stunden, sonst bleibt der Tag leer
            daily = mean.where(n >= 6)
            daily.index.names = ["run_time", "day"]
            for r, d in daily.groupby(level="run_time"):
                per_run[pd.Timestamp(r)] = d.droplevel("run_time")
            # vergangene Tage: Lauf vor Tagesbeginn, voller Tag
            full = mean.where(n >= 20)
            full.index.names = ["run_time", "day"]
            runs = pd.DatetimeIndex(sorted(per_run))
            days = pd.DatetimeIndex(sorted(set(full.index.get_level_values("day"))))
            start = days.tz_localize(BERLIN, ambiguous=True, nonexistent="shift_forward")
            pos = runs.searchsorted(start.tz_convert("UTC"), side="right") - 1
            key = [(runs[p], d) for p, d in zip(pos, days, strict=True) if p >= 0]
            if key:
                idx = pd.MultiIndex.from_tuples(key, names=["run_time", "day"])
                past = full.reindex(idx).droplevel("run_time")
        self._site_days = (per_run, past)
        return self._site_days

    def _site_temperatures(self, meta: pd.DataFrame) -> pd.DataFrame:
        """Mittel des Zieltags und gleitende 7- und 30-Tage-Mittel je Standort.

        Je Zeile der jüngste Standort-Lauf zum Ausgabezeitpunkt: ab seinem Tag dessen Prognose,
        davor die Näherung für vergangene Tage.

        @relation(SWR-MLT-033, scope=function)
        """
        sw = self.cfg.site_weather
        assert sw is not None
        per_run, past = self._site_tables()
        names = [s.code.lower() for s in sw.sites]
        out_cols = [f"kw_{n}_{k}" for n in names for k in ("t1", "t7", "t30")]
        out = pd.DataFrame(np.nan, index=meta.index, columns=out_cols, dtype=np.float64)
        if not per_run:
            return out
        issues = pd.DatetimeIndex(meta["issue_time"])
        target = pd.DatetimeIndex(
            pd.DatetimeIndex(meta["ts"]).tz_convert(BERLIN).normalize().tz_localize(None)
        )
        run_of = {i: self.sites.run_for(i, self.cfg.issue.nwp_delay_h) for i in issues.unique()}
        runs = pd.Series([run_of[i] for i in issues], index=meta.index)
        for r, rows in runs.groupby(runs, sort=False):
            fc = per_run.get(pd.Timestamp(r))
            if fc is None:
                continue
            d0 = pd.Timestamp(pd.Timestamp(r).tz_convert(BERLIN).date())
            days = target[runs.index.get_indexer(rows.index)]
            hist = past[past.index < d0].iloc[-30:]
            comb = pd.concat([hist, fc[fc.index >= d0]])
            comb = comb.reindex(pd.date_range(min(comb.index.min(), d0), days.max(), freq="D"))
            r7 = comb.rolling(7, min_periods=5).mean()
            r30 = comb.rolling(30, min_periods=20).mean()
            for n in names:
                col = f"wx_{SITE_PREFIX}{n}_t2m"
                if col not in comb.columns:
                    continue
                out.loc[rows.index, f"kw_{n}_t1"] = comb[col].reindex(days).to_numpy()
                out.loc[rows.index, f"kw_{n}_t7"] = r7[col].reindex(days).to_numpy()
                out.loc[rows.index, f"kw_{n}_t30"] = r30[col].reindex(days).to_numpy()
        return out

    def _flow_rel(self, code: str, issues: pd.DatetimeIndex) -> np.ndarray:
        """Zuletzt veröffentlichter Tagesabfluss relativ zum Median der 365 Tage bis dahin.

        @relation(SWR-MLT-033, scope=function)
        """
        cur = self._newest_published(code, issues)
        e = self.energy_long
        f = e.loc[e["code"] == code, ["ts", "value"]].dropna()
        if f.empty or cur["value"].isna().all():
            return np.full(len(issues), np.nan)
        f = f.groupby("ts")["value"].mean().sort_index().astype(np.float64)
        med = f.rolling("365D", min_periods=180).median()
        ref = med.reindex(pd.DatetimeIndex(cur["ts"])).to_numpy()
        with np.errstate(divide="ignore", invalid="ignore"):
            out: np.ndarray = np.where(ref > 0, cur["value"].to_numpy(np.float64) / ref, np.nan)
        return out

    def _cooling(self, meta: pd.DataFrame) -> pd.DataFrame:
        """Kühlwasserlage je Standort und die gefährdete Leistung als Kennzahl.

        Je Standort gilt der Anteil ``clip((t7 - t7_from) / ramp_t, 0, 1)`` bzw.
        ``clip((flow_below - q_rel) / ramp_q, 0, 1)`` seiner Leistung als gefährdet, der größere
        von beiden zählt für ``kw_risiko_mw``.

        @relation(SWR-MLT-033, scope=function)
        """
        sw = self.cfg.site_weather
        assert sw is not None  # vom Konfigurations-Check sichergestellt
        out = self._site_temperatures(meta)
        issues = pd.DatetimeIndex(meta["issue_time"])
        n = len(meta)
        heat, low, risk = np.zeros(n), np.zeros(n), np.zeros(n)
        seen = np.zeros(n, dtype=bool)
        for site in sw.sites:
            name = site.code.lower()
            q = np.full(n, np.nan)
            for code in site.flows:
                q = np.where(np.isnan(q), self._flow_rel(code, issues), q)
            if site.flows:
                out[f"kw_{name}_q_rel"] = q
            t7 = out[f"kw_{name}_t7"].to_numpy(np.float64)
            h = np.zeros(n)
            lo = np.zeros(n)
            if site.t7_from is not None:
                h = np.clip((t7 - site.t7_from) / sw.ramp_t, 0, 1)
                seen |= ~np.isnan(h)
                h = np.nan_to_num(h)
            if site.flow_below is not None and site.flows:
                lo = np.clip((site.flow_below - q) / sw.ramp_q, 0, 1)
                seen |= ~np.isnan(lo)
                lo = np.nan_to_num(lo)
            heat += site.mw * h
            low += site.mw * lo
            risk += site.mw * np.maximum(h, lo)
        out["kw_hitze_mw"] = np.where(seen, heat, np.nan)
        out["kw_niedrigwasser_mw"] = np.where(seen, low, np.nan)
        out["kw_risiko_mw"] = np.where(seen, risk, np.nan)
        return out.astype(np.float32)

    def _neighbour_index(
        self, sg: SeriesGroup, meta: pd.DataFrame, nbw: pd.DataFrame
    ) -> pd.DataFrame:
        """Wind- und PV-Leistung der Nachbarn: Stand der Leistung je Land mal mittlerem
        Kapazitätsfaktor seiner Zonen, summiert.

        @relation(SWR-MLT-029, scope=function)
        """
        nb = self.cfg.neighbour_weather
        assert nb is not None  # vom Konfigurations-Check sichergestellt
        issues = pd.DatetimeIndex(meta["issue_time"])
        caps = {c: self._capacity_asof(c, issues) for c in sg.codes}
        countries = list(dict.fromkeys(zone_country(z) for z in nb.zones))
        techs = {
            "wind": ("wind_cf", ("wind_onshore", "wind_offshore")),
            "pv": ("pv_cf", ("solar",)),
        }
        n = len(meta)
        out: dict[str, np.ndarray] = {}
        for name, (feature, cap_techs) in techs.items():
            power = np.zeros(n)
            cap_sum = np.zeros(n)
            seen = np.zeros(n, dtype=bool)
            for country in countries:
                cols = [
                    f"wx_{neighbour_feature(f'gefs:{z}:area', feature)}"
                    for z in nb.zones
                    if zone_country(z) == country
                ]
                cols = [c for c in cols if c in nbw.columns]
                codes = [f"cap_{t}_{country}" for t in cap_techs if f"cap_{t}_{country}" in caps]
                if not cols or not codes:
                    continue
                with np.errstate(invalid="ignore"):
                    cf = np.nanmean(nbw[cols].to_numpy(np.float64), axis=1)
                # eine fehlende Teilreihe (z. B. Wind auf See ohne Küste) zählt als 0 MW
                cap = np.nansum(np.vstack([caps[c] for c in codes]), axis=0)
                has_cap = ~np.all(np.isnan(np.vstack([caps[c] for c in codes])), axis=0)
                ok = has_cap & ~np.isnan(cf)
                power += np.where(ok, cap * np.nan_to_num(cf), 0.0)
                cap_sum += np.where(ok, cap, 0.0)
                seen |= ok
            out[f"nbi_{name}_mw"] = np.where(seen, power, np.nan)
            with np.errstate(divide="ignore", invalid="ignore"):
                out[f"nbi_{name}_cf"] = np.where(seen & (cap_sum > 0), power / cap_sum, np.nan)
            out[f"nbi_cap_{name}_mw"] = np.where(seen, cap_sum, np.nan)
        return pd.DataFrame(out, index=meta.index).astype(np.float32)


def gfs_version(runs: pd.Series) -> np.ndarray:
    """GFS-Hauptversion je Lauf (NaN ohne Lauf), siehe ``GFS_VERSIONS``.

    @relation(SWR-MLT-023, scope=function)
    """
    out = np.full(len(runs), np.nan, dtype=np.float32)
    t = pd.DatetimeIndex(runs)
    if t.tz is None:
        t = t.tz_localize("UTC")
    for start, version in reversed(GFS_VERSIONS):
        out[np.asarray(t >= start)] = version
    return out


def issue_days_between(start: date, end: date, stride: int = 1) -> list[date]:
    """Ausgabetage ``start`` bis ``end`` (einschließlich), jeder ``stride``-te."""
    n = (end - start).days + 1
    return [start + timedelta(days=i) for i in range(0, max(n, 0), stride)]
