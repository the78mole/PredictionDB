"""Snapshot ohne Datenbank: Bausteine wie die SQL-Abfragen der Datenbank-Fassung."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from ml_train.config import EnsembleWeather
from ml_train.snapshot import only_free
from ml_train.snapshot_ghcr import (
    capacity_daily,
    cluster_capacity,
    energy_hourly,
    energy_sources,
    ensemble_run,
    fixed_rows,
    weather_de,
)
from wx_exchange.gold import Weights

T0 = datetime(2025, 6, 1, tzinfo=UTC)


def _weights() -> Weights:
    cells = [
        ("wind_on:v1:c00", "wind_on", 0, 50.0, 8.0, 1.0, 100.0),
        ("wind_on:v1:c01", "wind_on", 1, 52.0, 9.0, 1.0, 100.0),
        ("pv_roof:v1:c00", "pv_roof", 0, 50.0, 8.0, 1.0, None),
    ]
    capacity = [
        ("wind_on", 0, date(2025, 1, 1), 1.0),
        ("wind_on", 1, date(2025, 1, 3), 2.0),
        ("wind_on", 1, date(2025, 1, 4), 3.0),
        ("pv_roof", 0, date(2025, 1, 2), 5.0),
    ]
    return Weights.from_rows("v1", cells, capacity)


def test_cluster_capacity_and_daily() -> None:
    """@relation(SWR-MLT-011, scope=function)"""
    w = _weights()
    assert cluster_capacity(w) == {
        "wind_on:v1:c00": ("wind_on", 1.0),
        "wind_on:v1:c01": ("wind_on", 3.0),
        "pv_roof:v1:c00": ("pv_roof", 5.0),
    }
    cap = capacity_daily(w)
    assert len(cap) == 7
    got = {(d.isoformat(), t): v for d, t, v in cap.itertuples(index=False)}
    # Cluster fehlen vor ihrem ersten Tag, danach gilt der letzte Wert
    assert got == {
        ("2025-01-01", "wind_on"): 1.0,
        ("2025-01-02", "pv_roof"): 5.0,
        ("2025-01-02", "wind_on"): 1.0,
        ("2025-01-03", "pv_roof"): 5.0,
        ("2025-01-03", "wind_on"): 3.0,
        ("2025-01-04", "pv_roof"): 5.0,
        ("2025-01-04", "wind_on"): 4.0,
    }


def _gold(rows: list[tuple[datetime, int, str, str, float]]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "run_time": pd.to_datetime([r[0] for r in rows], utc=True),
            "valid_time": pd.to_datetime([r[0] + timedelta(hours=r[1]) for r in rows], utc=True),
            "region": pd.Categorical([r[2] for r in rows]),
            "feature": pd.Categorical([r[3] for r in rows]),
            "value": np.array([r[4] for r in rows], dtype=np.float32),
        }
    )


def test_weather_de_weights_by_cluster_capacity() -> None:
    """@relation(SWR-MLT-011, scope=function)"""
    caps = cluster_capacity(_weights())
    gold = _gold(
        [
            (T0, 3, "wind_on:v1:c00", "wind_cf", 0.2),
            (T0, 3, "wind_on:v1:c01", "wind_cf", 0.6),
            (T0, 3, "wind_on:v1:c00", "wind_power", 10.0),
            (T0, 3, "wind_on:v1:c01", "wind_power", 20.0),
            (T0, 3, "pv_roof:v1:c00", "pv_cf", 0.5),
            # nur ein Cluster mit Wert: Mittel über die vorhandenen
            (T0, 6, "wind_on:v1:c01", "wind_cf", 0.4),
            # über dem Vorlauf, falsche Laufstunde, Lauf außerhalb des Monats
            (T0, 30, "wind_on:v1:c00", "wind_cf", 0.9),
            (T0 + timedelta(hours=6), 3, "wind_on:v1:c00", "wind_cf", 0.9),
            (T0 - timedelta(days=1), 30, "wind_on:v1:c00", "wind_cf", 0.9),
        ]
    )
    out = weather_de(gold, caps, T0, T0 + timedelta(days=30), [0, 12], 24)
    assert len(out) == 4
    got = {
        (r.valid_time - r.run_time, r.feature): round(float(r.value), 4) for r in out.itertuples()
    }
    assert got == {
        (timedelta(hours=3), "wind_on_wind_cf"): 0.5,
        (timedelta(hours=3), "wind_on_wind_power"): 30.0,
        (timedelta(hours=3), "pv_roof_pv_cf"): 0.5,
        (timedelta(hours=6), "wind_on_wind_cf"): 0.4,
    }
    assert list(out.columns) == ["run_time", "valid_time", "feature", "value"]
    with pytest.raises(ValueError, match="ohne Gewichte"):
        weather_de(
            _gold([(T0, 3, "hydro:v1:c00", "t2m", 1.0)]), caps, T0, T0 + timedelta(1), [0], 24
        )


def test_fixed_rows_names_and_filters() -> None:
    """@relation(SWR-MLT-011, scope=function)"""
    gold = _gold(
        [
            (T0, 3, "gefs:FR:area", "wind_cf", 0.3),
            (T0, 3, "gefs:FR:area", "t2m", 12.0),
            (T0, 3, "gefs:PT:area", "wind_cf", 0.1),
        ]
    )
    out = fixed_rows(
        gold,
        ["gefs:FR:area"],
        ["wind_cf"],
        lambda r, f: f"nb_{r.split(':')[1].lower()}_{f}",
        T0,
        T0 + timedelta(days=1),
        [0],
        24,
    )
    assert out["feature"].astype(str).tolist() == ["nb_fr_wind_cf"]


def test_ensemble_run_like_vm_gold() -> None:
    """@relation(SWR-MLT-011, scope=function)"""
    members, steps = 12, [0, 6, 12]
    values = np.zeros((members, len(steps), 2, 1), dtype=np.float32)
    values[:, :, 0, 0] = np.arange(members, dtype=np.float32)[:, None]
    values[:, 0, :, :] = np.nan  # f000 ohne Mittelwerte
    meta = {
        "run": T0.isoformat(),
        "steps": steps,
        "regions": ["DE:pv", "FR:area"],
        "variables": {"pv_cf": "1"},
    }
    en = EnsembleWeather(regions={"DE:pv": ["pv_cf_mean", "pv_cf_p90"]})
    out = ensemble_run(meta, values, en, T0, T0 + timedelta(days=1), 6)
    assert len(out) == 2
    got = {
        (r.valid_time - r.run_time, r.feature): round(float(r.value), 3) for r in out.itertuples()
    }
    assert got == {
        (timedelta(hours=6), "en_de_pv_pv_cf_mean"): 5.5,
        (timedelta(hours=6), "en_de_pv_pv_cf_p90"): 9.9,
    }


def test_energy_hourly_and_sources() -> None:
    """@relation(SWR-MLT-011, scope=function)"""
    ts = pd.date_range(T0, periods=8, freq="15min", tz="UTC")
    best = pd.DataFrame(
        {
            "code": ["da_price_de_lu"] * 8,
            "ts": ts,
            "value": [1.0, 2.0, 3.0, 4.0, 10.0, 10.0, 10.0, 10.0],
            "published": ts + pd.Timedelta(hours=1),
        }
    )
    out = energy_hourly(best, ["da_price_de_lu", "load_actual"], T0, T0 + timedelta(hours=1))
    assert out["value"].tolist() == [2.5]
    assert out["published"].iloc[0] == ts[3] + pd.Timedelta(hours=1)
    assert list(out.columns) == ["ts", "value", "published", "code"]
    meta = [
        {
            "smard": {"free": True, "train": True, "series": {"load_actual": {}}},
            "nt": {"free": False, "train": True, "series": {"load_actual": {}}},
            "ecb": {"free": True, "train": True, "series": {"fx_usd_eur": {}}},
        }
    ]
    sources = energy_sources(meta, ["load_actual"])
    assert sources == {"nt": "nur Training", "smard": "frei"}
    assert only_free({"sources": sources}) == ["nt"]
    # strengste Klasse über die Jahre; liefernde Quelle ohne Angabe ist unbekannt
    later = [{"nt": {"free": True, "train": True, "series": {"load_actual": {}}}}]
    assert energy_sources(meta + later, ["load_actual"], {"smard", "neu"}) == {
        "neu": "unbekannt",
        "nt": "nur Training",
        "smard": "frei",
    }
    assert only_free({}) == ["?"]
