"""Merkmale ohne Leakage, Backtest und Registry auf dem synthetischen Snapshot."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from ml_train.backtest import make_folds, run_backtest
from ml_train.calendar import calendar_features
from ml_train.config import ModelConfig, load_config
from ml_train.dataset import (
    FeatureBuilder,
    WeatherTable,
    gfs_version,
    local_day_hours,
    same_local_time,
)
from ml_train.models import TwoStageModel
from ml_train.predict import due_issues, next_wake
from ml_train.registry import Registry, compare
from ml_train.snapshot import load_snapshot
from ml_train.testing import synthetic_snapshot

ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture(scope="module")
def snap():  # type: ignore[no-untyped-def]
    return synthetic_snapshot("2022-06-01", "2023-10-01", weather_from="2023-03-01")


@pytest.fixture
def cfg() -> ModelConfig:
    c, _ = load_config(ROOT / "config/ml/de_lu_v1.yaml")
    c.stage1 = [t for t in c.stage1 if t.code != "gen_wind_offshore"]
    c.lgbm.n_estimators = 150
    c.lgbm.learning_rate = 0.1
    c.backtest.test_from = date(2023, 7, 1)
    c.backtest.fold_months = 2
    c.backtest.train_issue_stride = 3
    for v in c.variants:
        v.train_from = date(2022, 6, 1) if not v.requires_weather else date(2023, 3, 1)
    return c


def test_local_days_have_23_to_25_hours() -> None:
    assert len(local_day_hours(date(2026, 3, 29))) == 23
    assert len(local_day_hours(date(2026, 10, 25))) == 25
    assert len(local_day_hours(date(2026, 10, 26))) == 24


def test_calendar_holidays_and_bridge_days() -> None:
    ts = pd.DatetimeIndex(["2026-12-25 11:00", "2026-10-31 11:00", "2026-05-15 11:00"], tz="UTC")
    cal = calendar_features(ts)
    assert cal["holiday_national"].iloc[0] == 1
    assert 0.2 < cal["holiday_share"].iloc[1] < 0.5  # Reformationstag nur in einigen Ländern
    assert cal["bridge_day"].iloc[2] == 1  # Freitag nach Christi Himmelfahrt


def test_lags_only_use_known_values(snap, cfg) -> None:  # type: ignore[no-untyped-def]
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


def test_same_local_time_across_dst() -> None:
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
    return c


def test_v2_config_groups_are_known() -> None:
    c, _ = load_config(ROOT / "config/ml/de_lu_v2.yaml")
    assert {"nachbarn", "lastprognose", "brennstoffe", "marktwerte"} <= set(c.series_groups)
    assert "da_price_fr" in c.series_codes
    bad = c.model_dump()
    bad["variants"][0]["groups"].append("gibtsnicht")
    with pytest.raises(ValueError, match="gibtsnicht"):
        ModelConfig.model_validate(bad)


def test_series_groups_only_use_known_values(snap, cfg_v2) -> None:  # type: ignore[no-untyped-def]
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


def test_backtest_beats_naive_baselines(snap, cfg) -> None:  # type: ignore[no-untyped-def]
    fb = FeatureBuilder(snap, cfg)
    res = run_backtest(fb, cfg)
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
    folds = make_folds(cfg, date(2023, 9, 20))
    assert [f.test_from for f in folds] == [date(2023, 7, 1), date(2023, 9, 1)]
    assert folds[-1].test_to == date(2023, 9, 20)
    assert all(f.train_to < f.test_from for f in folds)


def test_snapshot_roundtrip_checks_hash(snap, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    snap.save(tmp_path)
    back = load_snapshot(tmp_path)
    assert back.snapshot_id == snap.manifest["snapshot_id"]
    assert len(back.energy) == len(snap.energy)
    (tmp_path / "energy.parquet").write_bytes(b"kaputt")
    with pytest.raises(ValueError):
        load_snapshot(tmp_path)


def test_registry_champion_challenger(snap, cfg, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    fb = FeatureBuilder(snap, cfg)
    res = run_backtest(fb, cfg)
    frame = fb.build([date(2023, 3, 1) + pd.Timedelta(days=i) for i in range(0, 120, 3)])
    reg = Registry(str(tmp_path), cfg)
    preds = res.predictions
    man = {"snapshot_id": "synthetic"}
    base = TwoStageModel(cfg, cfg.variant("kalender")).fit(frame)
    v1 = reg.register(
        base, man, preds[preds.method == "kalender"].drop(columns="method"), "champion"
    )
    wx = TwoStageModel(cfg, cfg.variant("wetter")).fit(frame)
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


def test_attach_backtest_to_registered_version(snap, cfg, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    fb = FeatureBuilder(snap, cfg)
    res = run_backtest(fb, cfg, variants=["kalender"])
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
    assert set(rows) == {0, 1, 2}
    assert rows[1]["mae"] == 1.0 and rows[2]["mae"] == 3.0 and rows[0]["mae"] == 2.0
    assert rows[0]["n"] == 48 and rows[1]["coverage80"] == 1.0


def test_due_issues_catches_up_missed_hours() -> None:
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
    assert next_wake(pd.Timestamp("2026-10-26T09:59", tz="UTC"), 3600) == pd.Timestamp(
        "2026-10-26T10:01", tz="UTC"
    )
    assert next_wake(pd.Timestamp("2026-10-26T10:00:30", tz="UTC"), 3600) == pd.Timestamp(
        "2026-10-26T10:01", tz="UTC"
    )
    assert next_wake(pd.Timestamp("2026-10-26T10:05", tz="UTC"), 3600) == pd.Timestamp(
        "2026-10-26T11:01", tz="UTC"
    )
