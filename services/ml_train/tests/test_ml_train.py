"""Merkmale ohne Leakage, Backtest und Registry auf dem synthetischen Snapshot."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest

from ml_train.backtest import backtest_steps, make_folds, run_backtest
from ml_train.calendar import calendar_features
from ml_train.config import ModelConfig, load_config
from ml_train.dataset import (
    FeatureBuilder,
    WeatherTable,
    gfs_version,
    local_day_hours,
    same_local_time,
)
from ml_train.jobs import JobLog
from ml_train.models import TwoStageModel
from ml_train.predict import by_issue, compare_issue, due_issues, needs_weather, next_wake
from ml_train.registry import Registry, compare
from ml_train.snapshot import Snapshot, load_snapshot
from ml_train.testing import synthetic_snapshot

ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture(scope="module")
def snap():  # type: ignore[no-untyped-def]
    return synthetic_snapshot("2022-06-01", "2023-10-01", weather_from="2023-03-01")


def small_lgbm(c: ModelConfig) -> None:
    """Kleine Bäume, wenige Runden: Die Tests prüfen Logik, nicht Modellgüte.

    LightGBM ist der größte Posten der Testlaufzeit; 15 Blätter und 60 Runden reichen,
    damit die Wettervariante die Baselines auf dem synthetischen Snapshot schlägt.
    Ein Thread je Testprozess: pytest-xdist startet einen Prozess je Kern, mehrere
    OpenMP-Threads je Prozess überbuchen die CPU und laufen dann quälend langsam.
    """
    c.lgbm.num_leaves = 15
    c.lgbm.n_estimators = 60
    c.lgbm.learning_rate = 0.2
    c.lgbm.n_jobs = 1


def make_cfg() -> ModelConfig:
    c, _ = load_config(ROOT / "config/ml/de_lu_v1.yaml")
    c.stage1 = [t for t in c.stage1 if t.code != "gen_wind_offshore"]
    small_lgbm(c)
    c.backtest.test_from = date(2023, 7, 1)
    c.backtest.fold_months = 2
    c.backtest.train_issue_stride = 3
    for v in c.variants:
        v.train_from = date(2022, 6, 1) if not v.requires_weather else date(2023, 3, 1)
    return c


@pytest.fixture
def cfg() -> ModelConfig:
    return make_cfg()


def test_local_days_have_23_to_25_hours() -> None:
    """@relation(SWR-MLT-020, scope=function)"""
    assert len(local_day_hours(date(2026, 3, 29))) == 23
    assert len(local_day_hours(date(2026, 10, 25))) == 25
    assert len(local_day_hours(date(2026, 10, 26))) == 24


def test_calendar_holidays_and_bridge_days() -> None:
    """@relation(SWR-MLT-024, scope=function)"""
    ts = pd.DatetimeIndex(["2026-12-25 11:00", "2026-10-31 11:00", "2026-05-15 11:00"], tz="UTC")
    cal = calendar_features(ts)
    assert cal["holiday_national"].iloc[0] == 1
    assert 0.2 < cal["holiday_share"].iloc[1] < 0.5  # Reformationstag nur in einigen Ländern
    assert cal["bridge_day"].iloc[2] == 1  # Freitag nach Christi Himmelfahrt


def test_lags_only_use_known_values(snap, cfg) -> None:  # type: ignore[no-untyped-def]
    """@relation(SWR-MLT-021, scope=function)"""
    fb = FeatureBuilder(snap, cfg)
    frame = fb.build([date(2023, 5, 10)])
    issue = frame.meta["issue_time"].iloc[0]
    assert issue == pd.Timestamp("2023-05-10 08:00", tz="UTC")
    load = fb.energy["load_actual"]
    # letzter bekannter Wert: Stunde, die bis issue - 2 h abgeschlossen war
    assert frame.X["lag_load_actual_last"].iloc[0] == pytest.approx(
        load[pd.Timestamp("2023-05-10 05:00", tz="UTC")], rel=1e-5
    )
    # gleiche Stunde am letzten vollständigen Tag (9.5., Ortszeit) bzw. für den Preis am 10.5.
    first = frame.meta["ts"].iloc[0]  # 11.5. 00:00 Ortszeit
    assert frame.X["lag_load_actual_day"].iloc[0] == pytest.approx(
        load[first - pd.Timedelta(days=2)], rel=1e-5
    )
    price = fb.energy["da_price_de_lu"]
    assert frame.X["lag_da_price_de_lu_day"].iloc[0] == pytest.approx(
        price[first - pd.Timedelta(days=1)], rel=1e-5
    )
    # Vorwoche nie aus der Zukunft des Ausgabezeitpunkts
    for code in ("load_actual", "da_price_de_lu"):
        week_src = frame.meta["ts"] - pd.Timedelta(days=7)
        known = week_src < issue - pd.Timedelta(hours=2)
        if code == "da_price_de_lu":
            known = week_src < pd.Timestamp("2023-05-10 22:00", tz="UTC")
        lagged = frame.X[f"lag_{code}_week"].to_numpy()
        direct = fb.energy[code].reindex(week_src).to_numpy()
        np.testing.assert_allclose(lagged[known.to_numpy()], direct[known.to_numpy()], rtol=1e-5)


def test_price_known_until_capped_at_present_data(snap, cfg) -> None:  # type: ignore[no-untyped-def]
    # Lauf 14 Uhr Ortszeit: per Uhrzeitregel gilt der Preis für morgen (12.5.) schon als
    # bekannt, abgerufen ist er aber noch nicht. Dann zählt der 11.5. als letzter voller Tag.
    """@relation(SWR-MLT-021, scope=function)"""
    cut = pd.Timestamp("2023-05-11 22:00", tz="UTC")  # 12.5. 00:00 Ortszeit
    e = snap.energy
    late = (e["code"] == "da_price_de_lu") & (e["ts"] >= cut)
    partial = Snapshot(e[~late], snap.weather, snap.capacity, snap.manifest)
    issue = pd.Timestamp("2023-05-11 12:00", tz="UTC")
    fb = FeatureBuilder(partial, cfg)
    frame = fb.build_at([issue])
    mean = frame.X["lag_da_price_de_lu_mean_day"]
    assert mean.notna().all()
    assert mean.iloc[0] == pytest.approx(
        fb.daily_mean["da_price_de_lu"][pd.Timestamp("2023-05-11")]
    )
    # mit vollständigen Daten gilt weiter die Uhrzeitregel (Training unverändert)
    full = FeatureBuilder(snap, cfg).build_at([issue]).X["lag_da_price_de_lu_mean_day"]
    assert full.iloc[0] == pytest.approx(
        FeatureBuilder(snap, cfg).daily_mean["da_price_de_lu"][pd.Timestamp("2023-05-12")]
    )


def test_predict_drops_nan_rows(snap, cfg) -> None:  # type: ignore[no-untyped-def]
    """@relation(SWR-MLT-032, scope=function)"""
    from types import SimpleNamespace

    from ml_train.predict import predict_model

    frame = FeatureBuilder(snap, cfg).build_at([pd.Timestamp("2023-05-11 12:00", tz="UTC")])
    n = len(frame.X)
    vals = np.arange(n, dtype=float)
    model = SimpleNamespace(
        lead_days=sorted(frame.meta["lead_day"].unique()),
        variant=SimpleNamespace(requires_weather=False),
        features=lambda: [],
        predict=lambda f: {
            "load_actual": pd.DataFrame({"q50": vals}),
            "da_price_de_lu": pd.DataFrame({"q50": np.full(n, np.nan)}),
        },
    )
    out = predict_model(frame, model, "v", pd.Timestamp("2023-05-11 12:00", tz="UTC"))  # type: ignore[arg-type]
    assert set(out["target"]) == {"load_actual"} and len(out) == n


def test_same_local_time_across_dst() -> None:
    """@relation(SWR-MLT-025, scope=function)"""
    ts = pd.DatetimeIndex(
        ["2026-10-26 04:00", "2026-10-25 01:00", "2026-03-30 01:00"], tz="UTC"
    )  # Mo 05 Uhr MEZ, So 02 Uhr MEZ (zweite), Mo 03 Uhr MESZ
    first, second = same_local_time(ts, np.array([7, 1, 1]))
    # 19.10. 05 Uhr MESZ; 24.10. 02 Uhr MESZ; 29.3. 03 Uhr MESZ (02 Uhr gibt es nicht)
    assert list(first) == list(
        pd.DatetimeIndex(["2026-10-19 03:00", "2026-10-24 00:00", "2026-03-29 01:00"], tz="UTC")
    )
    assert (first == second).all()
    # Bezugstag mit doppelter 02-Uhr-Stunde: beide Stunden
    a, b = same_local_time(pd.DatetimeIndex(["2026-10-26 01:00"], tz="UTC"), np.array([1]))
    assert (a[0], b[0]) == (
        pd.Timestamp("2026-10-25 00:00", tz="UTC"),
        pd.Timestamp("2026-10-25 01:00", tz="UTC"),
    )


def test_load_lags_keep_local_time_across_dst(snap, cfg) -> None:  # type: ignore[no-untyped-def]
    """@relation(SWR-MLT-025, scope=function)"""
    assert cfg.lag_local_time == ["load_actual"]
    fb = FeatureBuilder(snap, cfg)
    frame = fb.build([date(2022, 10, 31)])  # Montag nach der Umstellung am 30.10.
    meta, X = frame.meta, frame.X
    ts = pd.DatetimeIndex(meta["ts"])
    d1 = (meta["lead_day"] == 1).to_numpy()
    load, price = fb.energy["load_actual"], fb.energy["da_price_de_lu"]
    # Vorwoche der Last: 25.10. zur selben Ortsuhrzeit, also eine Stunde früher in UTC
    expect = load.reindex(ts[d1] - pd.Timedelta(days=7) - pd.Timedelta(hours=1)).to_numpy()
    np.testing.assert_allclose(X.loc[d1, "lag_load_actual_week"], expect, rtol=1e-5)
    # Preis und Erzeugung bleiben bei festen 7·24 h
    expect = price.reindex(ts[d1] - pd.Timedelta(days=7)).to_numpy()
    np.testing.assert_allclose(X.loc[d1, "lag_da_price_de_lu_week"], expect, rtol=1e-5)
    # kleinstes m, dessen Ortsuhrzeit vor dem Ausgabezeitpunkt bekannt war
    known = meta["issue_time"].iloc[0] - pd.Timedelta(hours=2)
    expect = np.full(len(ts), np.nan)
    for m in (3, 2, 1):
        first, second = same_local_time(ts, np.full(len(ts), 7 * m))
        ok = np.asarray(second < known)
        # doppelte 02-Uhr-Stunde (30.10.): Mittel beider Stunden
        expect[ok] = (load.reindex(first[ok]).to_numpy() + load.reindex(second[ok]).to_numpy()) / 2
    np.testing.assert_allclose(X["lag_load_actual_week"], expect, rtol=1e-5)


@pytest.fixture
def cfg_v2() -> ModelConfig:
    c, _ = load_config(ROOT / "config/ml/de_lu_v2.yaml")
    c.stage1 = [t for t in c.stage1 if t.code != "gen_wind_offshore"]
    c.series_groups["nachbarn"].codes = ["da_price_at"]
    c.series_groups["brennstoffe"].codes = ["gas_eu_m"]
    # die Merkmalstests unten prüfen den Stand um 08 UTC; Rotation und Resttag haben eigene Tests
    c.issue.hours_utc = []
    c.issue.lead_days = [1, 2, 3, 4, 5, 6, 7]
    c.calibration.months = 0
    c.lgbm.num_leaves = 15
    c.lgbm.n_jobs = 1  # siehe small_lgbm
    return c


def test_v2_config_groups_are_known() -> None:
    """@relation(SWR-MLT-002, SWR-MLT-026, scope=function)"""
    c, _ = load_config(ROOT / "config/ml/de_lu_v2.yaml")
    assert {"nachbarn", "lastprognose", "brennstoffe", "marktwerte"} <= set(c.series_groups)
    assert "da_price_fr" in c.series_codes
    bad = c.model_dump()
    bad["variants"][0]["groups"].append("gibtsnicht")
    with pytest.raises(ValueError, match="gibtsnicht"):
        ModelConfig.model_validate(bad)


def test_series_groups_only_use_known_values(snap, cfg_v2) -> None:  # type: ignore[no-untyped-def]
    """@relation(SWR-MLT-021, SWR-MLT-026, scope=function)"""
    fb = FeatureBuilder(snap, cfg_v2)
    frame = fb.build([date(2023, 5, 10), date(2023, 1, 10)])
    X, meta = frame.X, frame.meta
    for g in ("nachbarn", "lastprognose", "brennstoffe", "preisniveau"):
        assert frame.groups[g]
    # Nachbarpreis wie DE-LU: am 10.5. 08 UTC ist der Liefertag 10.5. bekannt, der 11.5. nicht
    first = meta.index[meta["issue_time"] == pd.Timestamp("2023-05-10 08:00", tz="UTC")][0]
    ts = meta.loc[first, "ts"]
    assert X.loc[first, "lag_da_price_at_day"] == pytest.approx(
        fb.energy["da_price_at"][ts - pd.Timedelta(days=1)], rel=1e-5
    )
    # 28-Tage-Mittel endet mit dem letzten vollständig bekannten Liefertag (10.5.)
    days = fb.daily_mean["da_price_de_lu"]
    expected = days.loc[pd.Timestamp("2023-04-13") : pd.Timestamp("2023-05-10")].mean()
    assert X.loc[first, "lag_da_price_de_lu_mean_28d"] == pytest.approx(expected, rel=1e-5)
    # Lastprognose: im Sommer (08 UTC = 10 Uhr) für D+1 bekannt, im Winter (9 Uhr) noch nicht
    fc = X["fc_load_da_forecast"]
    summer = meta["issue_time"].dt.month == 5
    assert fc[summer & (meta["lead_day"] == 1)].notna().all()
    assert fc[meta["lead_day"] > 1].isna().all()
    assert fc[~summer].isna().all()
    # Monatswert Gas, veröffentlicht eine Woche nach Monatsende: am 10.5. gilt der April (seit
    # 8.5.), am 10.1. der Dezember (seit 8.1.)
    gas = snap.energy[snap.energy["code"] == "gas_eu_m"].set_index("ts")["value"]
    april = gas[pd.Timestamp("2023-04-01", tz="UTC")]
    assert X.loc[first, "asof_gas_eu_m"] == pytest.approx(april)
    assert X.loc[first, "asof_gas_eu_m_chg"] == pytest.approx(1.0)
    jan = meta.index[meta["issue_time"].dt.month == 1][0]
    assert X.loc[jan, "asof_gas_eu_m"] == pytest.approx(gas[pd.Timestamp("2022-12-01", tz="UTC")])


def test_outlook_uses_latest_published_issue(snap, cfg_v2) -> None:  # type: ignore[no-untyped-def]
    """@relation(SWR-MLT-021, SWR-MLT-026, scope=function)"""
    assert cfg_v2.series_groups["steo"].stored_codes[:2] == ["steo_brent_h00", "steo_brent_h01"]
    assert "steo_henry_hub_h03" in cfg_v2.series_codes
    fb = FeatureBuilder(snap, cfg_v2)
    frame = fb.build([date(2023, 5, 10), date(2023, 5, 12)])
    X, meta = frame.X, frame.meta
    assert frame.groups["steo"] == ["ol_steo_brent_h1", "ol_steo_brent_h2", "ol_steo_brent_h3"]
    # Maiausgabe erscheint am 11.5.: am 10.5. gilt die Aprilausgabe (Index 10 ab Juni 2022,
    # steigend), am 12.5. die Maiausgabe (fallend)
    may10 = meta["issue_time"] == pd.Timestamp("2023-05-10 08:00", tz="UTC")
    assert X.loc[may10, "ol_steo_brent_h1"].to_numpy() == pytest.approx(0.05, abs=1e-6)
    assert X.loc[may10, "ol_steo_brent_h3"].to_numpy() == pytest.approx(0.15, abs=1e-6)
    may12 = meta["issue_time"] == pd.Timestamp("2023-05-12 08:00", tz="UTC")
    assert X.loc[may12, "ol_steo_brent_h2"].to_numpy() == pytest.approx(-0.10, abs=1e-6)


def test_series_groups_train_and_predict(snap, cfg_v2) -> None:  # type: ignore[no-untyped-def]
    """@relation(SWR-MLT-026, scope=function)"""
    cfg_v2.lgbm.n_estimators = 30
    fb = FeatureBuilder(snap, cfg_v2)
    frame = fb.build([date(2022, 9, 1) + pd.Timedelta(days=i) for i in range(0, 200, 4)])
    m = TwoStageModel(cfg_v2, cfg_v2.variant("frei")).fit(frame)
    feats = m.stage2["da_price_de_lu"].features
    assert "lag_da_price_at_mean_day" in feats and "asof_gas_eu_m" in feats
    m = TwoStageModel(cfg_v2, cfg_v2.variant("frei_steo")).fit(frame)
    assert "ol_steo_brent_h1" in m.stage2["da_price_de_lu"].features
    assert "fc_load_da_forecast" in m.stage1["load_actual"].features
    out = m.predict(fb.build([date(2023, 6, 1)]))
    assert out["da_price_de_lu"]["q50"].notna().all()


def test_weather_uses_run_available_at_issue(snap, cfg) -> None:  # type: ignore[no-untyped-def]
    """@relation(SWR-MLT-022, scope=function)"""
    fb = FeatureBuilder(snap, cfg)
    frame = fb.build([date(2023, 5, 10)])
    # 00Z-Lauf vom 10.5. ist um 06 UTC verarbeitet, also bei Ausgabe 08 UTC verwendbar
    lead = frame.X["wx_lead_h"] - (frame.meta["ts"] - frame.meta["issue_time"]) / pd.Timedelta(
        hours=1
    )
    assert np.allclose(lead, 8)
    # ab 120 h dreistündlich: interpoliert markiert
    assert frame.X.loc[frame.X["wx_lead_h"] > 121, "wx_interp"].eq(1).any()
    assert frame.X.loc[frame.X["wx_lead_h"] < 100, "wx_interp"].eq(0).all()


def test_gfs_version_by_run(snap, cfg) -> None:  # type: ignore[no-untyped-def]
    """@relation(SWR-MLT-023, scope=function)"""
    runs = pd.Series(
        pd.to_datetime(
            ["2019-06-12 06:00", "2019-06-12 12:00", "2021-03-22 06:00", "2021-03-22 12:00", None],
            utc=True,
        )
    )
    np.testing.assert_array_equal(gfs_version(runs), [14, 15, 15, 16, np.nan])
    frame = FeatureBuilder(snap, cfg).build([date(2023, 5, 10)])
    assert frame.groups["nwp_version"] == ["wx_gfs_v"]
    assert frame.X["wx_gfs_v"].eq(16).all()


def test_weather_alignment_interval_and_instant() -> None:
    """@relation(SWR-MLT-023, scope=function)"""
    run = pd.Timestamp("2024-01-01", tz="UTC")
    valid = run + pd.to_timedelta([120, 123], unit="h")
    w = pd.DataFrame(
        {
            "run_time": run,
            "valid_time": np.repeat(valid, 2),
            "feature": ["pv_roof_ghi", "pv_roof_t2m"] * 2,
            "value": [100.0, 0.0, 300.0, 3.0],
        }
    )
    t = WeatherTable(w).table.loc[run]
    # Energiestunde 120-121 h: Intervallwert von 123 h (gilt für 120-123 h), Mittel der Momente
    assert t.loc[run + pd.Timedelta(hours=120), "wx_pv_roof_ghi"] == pytest.approx(300)
    assert t.loc[run + pd.Timedelta(hours=120), "wx_pv_roof_t2m"] == pytest.approx(0.5)
    assert t.loc[run + pd.Timedelta(hours=121), "wx_interp"] == 1


class RecordingJob(JobLog):
    """Schritte ohne Datenbank mitschreiben."""

    def __init__(self) -> None:
        super().__init__(None)
        self.labels: list[str] = []

    def step(self, label: str) -> None:
        self.labels.append(label)


@pytest.fixture(scope="module")
def backtest(snap):  # type: ignore[no-untyped-def]
    """Ein voller Backtest für alle Tests, die nur sein Ergebnis brauchen (teuerster Schritt)."""
    c = make_cfg()
    fb = FeatureBuilder(snap, c)
    job = RecordingJob()
    return fb, c, run_backtest(fb, c, job=job), job


def test_backtest_beats_naive_baselines(backtest) -> None:  # type: ignore[no-untyped-def]
    """@relation(SWR-MLT-030, SWR-MLT-040, SWR-MLT-041, scope=function)"""
    fb, cfg, res, job = backtest
    # Fortschritt im Dashboard: angekündigte Schritte = gemeldete Schritte
    assert len(job.labels) == backtest_steps(fb, cfg)
    assert job.labels[1].startswith("Faltung 1/")
    m = res.metrics(("method", "target")).set_index(["method", "target"])
    assert m.loc[("kalender", "load_actual"), "mae"] < m.loc[("naive_week", "load_actual"), "mae"]
    assert (
        m.loc[("wetter", "gen_wind_onshore"), "mae"]
        < m.loc[("kalender", "gen_wind_onshore"), "mae"]
    )
    assert (
        m.loc[("wetter", "da_price_de_lu"), "mae"] < m.loc[("naive_day", "da_price_de_lu"), "mae"]
    )
    assert 0.6 < m.loc[("wetter", "load_actual"), "coverage80"] < 0.95
    # ENTSO-E-Benchmark nur für den Folgetag
    bench = res.predictions[res.predictions["method"] == "bench_load_da_forecast"]
    assert set(bench["lead_day"]) == {1}


def test_folds_expand(cfg) -> None:  # type: ignore[no-untyped-def]
    """@relation(SWR-MLT-040, scope=function)"""
    folds = make_folds(cfg, date(2023, 9, 20))
    assert [f.test_from for f in folds] == [date(2023, 7, 1), date(2023, 9, 1)]
    assert folds[-1].test_to == date(2023, 9, 20)
    assert all(f.train_to < f.test_from for f in folds)


def test_snapshot_roundtrip_checks_hash(snap, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    """@relation(SWR-MLT-010, scope=function)"""
    snap.save(tmp_path)
    back = load_snapshot(tmp_path)
    assert back.snapshot_id == snap.manifest["snapshot_id"]
    assert len(back.energy) == len(snap.energy)
    (tmp_path / "energy.parquet").write_bytes(b"kaputt")
    with pytest.raises(ValueError):
        load_snapshot(tmp_path)


def test_registry_champion_challenger(backtest, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    """@relation(SWR-MLT-050, SWR-MLT-051, scope=function)"""
    fb, cfg, res, _ = backtest
    frame = fb.build([date(2023, 3, 1) + pd.Timedelta(days=i) for i in range(0, 120, 3)])
    reg = Registry(str(tmp_path), cfg)
    preds = res.predictions
    man = {"snapshot_id": "synthetic"}
    base = TwoStageModel(cfg, cfg.variant("kalender")).fit(frame)
    v1 = reg.register(
        base, man, preds[preds.method == "kalender"].drop(columns="method"), "champion"
    )
    wx, labels = TwoStageModel(cfg, cfg.variant("wetter")), list[str]()
    wx.fit(frame, progress=labels.append)
    assert len(labels) == wx.fit_steps() and labels[0].startswith("Stufe 1")
    v2 = reg.register(wx, man, preds[preds.method == "wetter"].drop(columns="method"))
    assert reg.state()["champion"] == v1 and reg.state()["challengers"] == [v2]
    assert set(reg.active()) == {v1, v2}
    d = reg.evaluate(v2)
    assert not d.overall.empty
    # Laden und Vorhersagen ergibt dieselben Werte wie das Modell im Speicher
    loaded = reg.load(v2, tmp_path / "cache")
    test = fb.build([date(2023, 9, 1)], need_weather=True)
    a, b = wx.predict(test), loaded.predict(test)
    pd.testing.assert_frame_equal(a["da_price_de_lu"], b["da_price_de_lu"])


def test_attach_backtest_to_registered_version(backtest, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    """@relation(SWR-MLT-052, scope=function)"""
    fb, cfg, res, _ = backtest
    frame = fb.build([date(2023, 3, 1) + pd.Timedelta(days=i) for i in range(0, 60, 3)])
    reg = Registry(str(tmp_path), cfg)
    model = TwoStageModel(cfg, cfg.variant("kalender")).fit(frame)
    v = reg.register(model, {"snapshot_id": "synthetic"}, None, "champion")
    assert reg.backtest(v) is None
    bt = res.predictions.query("method == 'kalender'").drop(columns="method")
    reg.attach_backtest(v, bt, {"backtest_seconds": 1.0})
    got = reg.backtest(v)
    assert got is not None and len(got) == len(bt)
    man = reg.manifest(v)
    assert man["files"][-2:] == ["backtest.parquet", "manifest.json"]
    assert man["backtest_seconds"] == 1.0 and man["variant"]["name"] == "kalender"


def test_compare_blocks_group_regression() -> None:
    """@relation(SWR-MLT-051, scope=function)"""
    rng = np.random.default_rng(1)
    ts = pd.date_range("2024-01-01", periods=2000, freq="h", tz="UTC")
    base = pd.DataFrame(
        {
            "target": "x",
            "issue_time": ts.floor("D"),
            "ts": ts,
            "lead_day": 1,
            "actual": rng.normal(0, 1, len(ts)),
        }
    )
    champ = base.assign(q10=-1.3, q50=0.0, q90=1.3)
    # Challenger überall etwas besser, aber im Winter deutlich schlechter
    better = base["actual"] * 0.3
    winter = ts.month.isin([1, 2])
    chal = base.assign(q50=np.where(winter, 2.0, better))
    chal = chal.assign(q10=chal["q50"] - 1.0, q90=chal["q50"] + 1.0)
    from ml_train.config import PromotionRule

    d = compare(champ, chal, PromotionRule(min_group_n=50))
    assert not d.promote
    assert any("Winter" in r for r in d.reasons)


def test_dbsync_roles_and_metrics() -> None:
    """@relation(SWR-MLT-053, scope=function)"""
    from ml_train.dbsync import metric_rows, roles

    st = {
        "champion": "c-kalender-2-x",
        "challengers": ["c-wetter-3-x"],
        "baseline": "c-kalender-1-x",
        "previous": ["c-kalender-0-x"],
        "history": [{"version": v} for v in ("c-old-0-x", "c-kalender-0-x", "c-kalender-2-x")],
    }
    assert roles(st) == {
        "c-old-0-x": "retired",
        "c-kalender-0-x": "previous",
        "c-kalender-1-x": "baseline",
        "c-wetter-3-x": "challenger",
        "c-kalender-2-x": "champion",
    }
    ts = pd.date_range("2024-01-01", periods=48, freq="h", tz="UTC")
    bt = pd.DataFrame(
        {
            "target": "load_actual",
            "issue_time": ts[0],
            "ts": ts,
            "lead_day": [1] * 24 + [2] * 24,
            "actual": 10.0,
            "q10": 8.0,
            "q50": [11.0] * 24 + [13.0] * 24,
            "q90": 12.0,
        }
    )
    rows = {r["lead_day"]: r for r in metric_rows("v", bt)}
    assert set(rows) == {-1, 1, 2}
    assert rows[1]["mae"] == 1.0 and rows[2]["mae"] == 3.0 and rows[-1]["mae"] == 2.0
    assert rows[-1]["n"] == 48 and rows[1]["coverage80"] == 1.0


def test_due_issues_catches_up_missed_hours() -> None:
    """@relation(SWR-MLT-061, scope=function)"""
    now = pd.Timestamp("2026-10-26T10:01:30", tz="UTC")
    assert due_issues(now, None, 24) == [pd.Timestamp("2026-10-26T10", tz="UTC")]
    last = pd.Timestamp("2026-10-26T07", tz="UTC")
    assert due_issues(now, last, 24) == list(
        pd.date_range("2026-10-26T08", "2026-10-26T10", freq="h", tz="UTC")
    )
    assert due_issues(now, pd.Timestamp("2026-10-26T10", tz="UTC"), 24) == []
    # Nachholen ist nach hinten begrenzt
    assert len(due_issues(now, pd.Timestamp("2026-10-20T00", tz="UTC"), 3)) == 4


def test_next_wake_on_full_hour() -> None:
    """@relation(SWR-MLT-061, scope=function)"""
    assert next_wake(pd.Timestamp("2026-10-26T09:59", tz="UTC"), 3600) == pd.Timestamp(
        "2026-10-26T10:01", tz="UTC"
    )
    assert next_wake(pd.Timestamp("2026-10-26T10:00:30", tz="UTC"), 3600) == pd.Timestamp(
        "2026-10-26T10:01", tz="UTC"
    )
    assert next_wake(pd.Timestamp("2026-10-26T10:05", tz="UTC"), 3600) == pd.Timestamp(
        "2026-10-26T11:01", tz="UTC"
    )


def test_by_issue_groups_versions_per_issue() -> None:
    """@relation(SWR-MLT-060, scope=function)"""
    a, b, c = (pd.Timestamp(f"2026-10-26T{h:02d}", tz="UTC") for h in (8, 9, 10))
    assert by_issue({"neu1": [c, a], "neu2": [a, b], "neu3": []}) == {
        a: ["neu1", "neu2"],
        b: ["neu2"],
        c: ["neu1"],
    }
    assert list(by_issue({"neu1": [c, a, b]})) == [a, b, c]


def test_compare_issue_matches_dashboard_hours() -> None:
    """@relation(SWR-MLT-062, scope=function)"""
    assert compare_issue(pd.Timestamp("2026-10-27T08", tz="UTC"))
    # 10 Uhr Ortszeit: im Winter 09 UTC, im Sommer 08 UTC
    assert compare_issue(pd.Timestamp("2026-10-27T09", tz="UTC"))
    assert compare_issue(pd.Timestamp("2026-07-01T08", tz="UTC"))
    assert not compare_issue(pd.Timestamp("2026-07-01T09", tz="UTC"))
    assert not compare_issue(pd.Timestamp("2026-10-27T18", tz="UTC"))


def test_needs_weather_from_manifest_variant(cfg_v2) -> None:  # type: ignore[no-untyped-def]
    """@relation(SWR-MLT-063, scope=function)"""

    class Reg:
        def manifest(self, version: str) -> dict[str, dict[str, str]]:
            return {"variant": {"name": version}}

    reg: Any = Reg()
    assert not needs_weather(cfg_v2, reg, ["kalender", "frei"])
    assert needs_weather(cfg_v2, reg, ["frei", "wetter_frei"])
    assert needs_weather(cfg_v2, reg, ["gibt_es_nicht"])


def test_grenzkosten_only_uses_known_hours(snap, cfg_v2) -> None:  # type: ignore[no-untyped-def]
    """@relation(SWR-MLT-021, SWR-MLT-026, scope=function)"""
    sg = cfg_v2.series_groups["grenzkosten"]
    assert {"residual_load", "gen_lignite", "gen_gas"} <= set(sg.stored_codes)
    fb = FeatureBuilder(snap, cfg_v2)
    frame = fb.build([date(2023, 5, 10)])
    X = frame.X
    assert set(frame.groups["grenzkosten"]) >= {"gk_hoch_14d", "gk_braunkohle_56d", "gk_niveau"}
    # Fenster endet mit der letzten bekannten Ist-Stunde (08 UTC - 2 h), Preise danach zählen
    # nicht, obwohl der Preis des Liefertags schon feststeht
    issue = pd.Timestamp("2023-05-10 08:00", tz="UTC")
    end = issue - pd.Timedelta(hours=2)
    e = fb.energy.loc[end - pd.Timedelta(days=14) : end - pd.Timedelta(hours=1)]
    high = e["residual_load"] >= e["residual_load"].quantile(0.75)
    expected = e.loc[high, "da_price_de_lu"].median()
    assert X["gk_hoch_14d"].iloc[0] == pytest.approx(expected, rel=1e-5)
    assert X["gk_niveau"].iloc[0] == pytest.approx(max(expected, sg.floor), rel=1e-5)
    # ein Liefertag später ändert den Wert erst ab der nächsten Ausgabe
    shifted = fb.energy.copy()
    shifted.loc[end:, "da_price_de_lu"] += 1000.0
    fb.energy = shifted
    fb._gk_hours.clear()
    assert fb.build([date(2023, 5, 10)]).X["gk_hoch_14d"].iloc[0] == pytest.approx(expected)


def test_scaled_target_trains_and_predicts(snap, cfg_v2, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    """@relation(SWR-MLT-027, scope=function)"""
    cfg_v2.lgbm.n_estimators = 30
    fb = FeatureBuilder(snap, cfg_v2)
    frame = fb.build([date(2022, 10, 1) + pd.Timedelta(days=i) for i in range(0, 200, 4)])
    m = TwoStageModel(cfg_v2, cfg_v2.variant("frei_gk_skaliert")).fit(frame)
    price = m.stage2["da_price_de_lu"]
    assert price.scale == "gk_niveau" and "gk_niveau" in m.features()
    assert m.stage1["load_actual"].scale is None
    test = fb.build([date(2023, 6, 1)])
    out = m.predict(test)["da_price_de_lu"]
    assert out["q50"].notna().all()
    # Prognose = Zentrum + Baumausgabe x Niveau (Baum lernt die Abweichung relativ zum Niveau)
    s1 = {c: q.predict(test.X)["q50"].to_numpy() for c, q in m.stage1.items()}
    X1 = m._with_stage1(test.X, s1)
    raw = np.sort(
        np.column_stack([b.predict(X1[price.features]) for b in price.boosters.values()]), axis=1
    )[:, 1]
    expected = X1[price.center].to_numpy() + raw * X1["gk_niveau"].to_numpy()
    assert price.predict(X1)["q50"].to_numpy() == pytest.approx(expected, rel=1e-6)
    # Ablage und Laden behalten die Bezugsspalte
    d = price.save(tmp_path)
    assert type(price).load(d, tmp_path, cfg_v2.lgbm).scale == "gk_niveau"


def test_run_job_snapshot_has_no_variant(cfg, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """@relation(SWR-MLT-003, scope=function)"""
    from ml_train import cli

    job = RecordingJob()
    started: list[Any] = []
    monkeypatch.setattr(cli, "open_job_log", lambda: job)
    monkeypatch.setattr(job, "start", lambda *a: started.append(a))
    monkeypatch.setattr(cli, "cmd_snapshot", lambda cfg, a, job: 0)
    a = cli.build_parser().parse_args(["snapshot", "--out", "/tmp/x"])
    assert cli.run_job(cfg, "sha", a) == 0
    assert started[0][2] == []


def test_rotating_issue_hours_and_rest_of_day(snap, cfg_v2) -> None:  # type: ignore[no-untyped-def]
    """@relation(SWR-MLT-020, scope=function)"""
    cfg_v2.issue.hours_utc = [2, 8, 14, 20]
    cfg_v2.issue.lead_days = [0, 1, 2]
    fb = FeatureBuilder(snap, cfg_v2)
    days = [date(2023, 5, 8) + pd.Timedelta(days=i) for i in range(4)]
    issues = fb.issues(days)
    # reihum nach Datum: jede Stunde einmal in vier aufeinanderfolgenden Tagen
    assert sorted(i.hour for i in issues) == [2, 8, 14, 20]
    frame = fb.build(days)
    meta = frame.meta
    # Vorlauftag 0: nur Stunden ab der Ausgabe bis Mitternacht Ortszeit
    rest = meta[meta["lead_day"] == 0]
    assert (rest["ts"] >= rest["issue_time"]).all()
    late = next(i for i in issues if i.hour == 14)
    assert len(rest[rest["issue_time"] == late]) == 8  # 16 bis 24 Uhr Ortszeit (Sommerzeit)
    # Ausgabe 14 UTC (16 Uhr): Preis für D+1 steht fest, ist kein Lernziel und wird übernommen
    at_late = (meta["issue_time"] == late).to_numpy()
    d1 = at_late & (meta["lead_day"] == 1).to_numpy()
    assert frame.y.loc[d1, "da_price_de_lu"].isna().all()
    assert frame.known is not None
    assert frame.known.loc[d1, "da_price_de_lu"].notna().all()
    assert frame.y.loc[d1, "load_actual"].notna().all()
    assert frame.known.loc[at_late, "load_actual"].isna().all()
    # Ausgabe 08 UTC (10 Uhr): D+1 noch offen
    early = (meta["issue_time"].dt.hour == 8).to_numpy() & (meta["lead_day"] == 1).to_numpy()
    assert frame.y.loc[early, "da_price_de_lu"].notna().all()
    # Prognose übernimmt bekannte Preise in allen Quantilen
    cfg_v2.lgbm.n_estimators = 20
    train = fb.build([date(2022, 9, 1) + pd.Timedelta(days=i) for i in range(0, 200, 3)])
    m = TwoStageModel(cfg_v2, cfg_v2.variant("kalender")).fit(train)
    out = m.predict(frame)["da_price_de_lu"]
    kn = frame.known["da_price_de_lu"].to_numpy()
    assert np.allclose(out.loc[d1, "q10"], kn[d1]) and np.allclose(out.loc[d1, "q90"], kn[d1])


def test_calibration_widens_bands_per_lead_day(snap, cfg, tmp_path) -> None:  # type: ignore[no-untyped-def]
    """@relation(SWR-MLT-031, scope=function)"""
    cfg.lgbm.n_estimators = 40
    cfg.calibration.months = 3
    cfg.calibration.min_rows = 50
    fb = FeatureBuilder(snap, cfg)
    frame = fb.build([date(2022, 7, 1) + pd.Timedelta(days=i) for i in range(0, 420, 2)])
    m = TwoStageModel(cfg, cfg.variant("kalender"))
    labels: list[str] = []
    m.fit(frame, progress=labels.append)
    assert len(labels) == m.fit_steps() and labels[0].startswith("Kalibrierung")
    sh = m.stage2["da_price_de_lu"].shifts
    assert set(sh) == set(cfg.issue.lead_days)
    assert set(sh[1]) == {"q10", "q90"}
    # Abstand P10-P90 wird nach Kalibrierung nicht schmaler, wenn das Band zu schmal war
    assert all(np.isfinite(v) for d in sh.values() for v in d.values())
    # Ablage und Laden behalten die Verschiebungen und die Vorlauftage
    from ml_train.registry import Registry

    reg = Registry(str(tmp_path), cfg)
    v = reg.register(m, {"snapshot_id": "synthetic"}, None, "champion")
    loaded = reg.load(v, tmp_path / "cache")
    assert loaded.stage2["da_price_de_lu"].shifts == sh
    assert loaded.lead_days == cfg.issue.lead_days
    test = fb.build([date(2023, 9, 1)])
    pd.testing.assert_frame_equal(
        m.predict(test)["da_price_de_lu"], loaded.predict(test)["da_price_de_lu"]
    )
