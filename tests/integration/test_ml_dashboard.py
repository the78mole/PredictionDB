"""Registry-Spiegel (``meta.model_*``), ``fcst.prediction_error`` und die Dashboard-Abfragen.

Läuft nur mit ``PDB_INTEGRATION=1`` nach ``migrate``. Die Registry liegt in einem temporären
Verzeichnis; angelegte Versionen, Prognosen und die Testquelle werden wieder entfernt.
"""

import json
import os
import re
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path

import pandas as pd
import pytest
from sqlalchemy import Engine, text

from ml_train.config import load_config
from ml_train.dbsync import sync_registry
from ml_train.registry import Registry
from predictiondb_core.db import create_db_engine
from predictiondb_core.settings import Settings

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.environ.get("PDB_INTEGRATION") != "1", reason="PDB_INTEGRATION != 1"),
]

ROOT = Path(__file__).parents[2]
DASHBOARD = ROOT / "config/grafana/dashboards/prognose-energiedaten.json"
CONFIG = "it_ml_dash"
TARGETS = {"load_actual": 50_000.0, "da_price_de_lu": 80.0}


@pytest.fixture
def engine() -> Iterator[Engine]:
    eng = create_db_engine(Settings())
    yield eng
    with eng.begin() as c:
        c.execute(text(f"DELETE FROM fcst.prediction WHERE model_version LIKE '{CONFIG}-%'"))
        c.execute(text("DELETE FROM meta.model_version WHERE config = :c"), {"c": CONFIG})
        c.execute(text("DELETE FROM meta.model_event WHERE config = :c"), {"c": CONFIG})
        c.execute(
            text(
                "DELETE FROM energy.value WHERE series_id IN (SELECT series_id FROM energy.series"
                " WHERE source_id = (SELECT source_id FROM meta.source WHERE code = :c))"
            ),
            {"c": CONFIG},
        )
        c.execute(
            text(
                "DELETE FROM energy.series"
                " WHERE source_id = (SELECT source_id FROM meta.source WHERE code = :c)"
            ),
            {"c": CONFIG},
        )
        c.execute(text("DELETE FROM meta.source WHERE code = :c"), {"c": CONFIG})
    eng.dispose()


def _registry(root: Path, now: pd.Timestamp) -> tuple[Registry, list[str]]:
    cfg, _ = load_config(ROOT / "config/ml/de_lu_v1.yaml")
    cfg.name = CONFIG
    versions = [f"{CONFIG}-kalender-209901010800-aaaaaa", f"{CONFIG}-wetter-209901020800-bbbbbb"]
    ts = pd.date_range(now - timedelta(days=30), periods=24 * 7, freq="h", tz="UTC")
    for i, v in enumerate(versions):
        d = root / CONFIG / v
        d.mkdir(parents=True)
        man = {
            "config": CONFIG,
            "variant": {"name": v.split("-")[1]},
            "created": (now - timedelta(days=2 - i)).isoformat(),
            "train_issue_days": ["2018-10-15", "2026-10-01"],
            "train_rows": 1000,
            "train_seconds": 12.5,
            "backtest_seconds": 300.0,
            "snapshot_id": "abc",
            "git_commit": "deadbeef",
        }
        (d / "manifest.json").write_text(json.dumps(man))
        bt = pd.concat(
            pd.DataFrame(
                {
                    "target": t,
                    "issue_time": ts.floor("D"),
                    "ts": ts,
                    # Tage ab Fensterbeginn (Kalendertag % 7 verliert Vorlauftage)
                    "lead_day": (ts - ts[0]).days % 7 + 1,
                    "actual": base,
                    "q10": base * 0.9,
                    "q50": base * (1.01 + 0.01 * i),
                    "q90": base * 1.1,
                }
            )
            for t, base in TARGETS.items()
        )
        bt.to_parquet(d / "backtest.parquet", index=False)
    hist = [
        {"at": "2099-01-01T08:00:00+00:00", "action": "champion", "version": versions[0]},
        {"at": "2099-01-02T08:00:00+00:00", "action": "challenger", "version": versions[1]},
    ]
    state = {
        "champion": versions[0],
        "challengers": [versions[1]],
        "baseline": None,
        "previous": [],
        "history": [{**h, "note": ""} for h in hist],
    }
    (root / CONFIG / "registry.json").write_text(json.dumps(state))
    return Registry(str(root), cfg), versions


def _fill(engine: Engine, versions: list[str], now: pd.Timestamp) -> None:
    issue = (now - timedelta(days=3)).floor("D") + timedelta(hours=8)
    hours = pd.date_range(issue + timedelta(hours=16), periods=48, freq="h")
    with engine.begin() as c:
        sid = c.execute(
            text(
                "INSERT INTO meta.source (code, kind, license, priority)"
                " VALUES (:c, 'energy', 'test', 999) RETURNING source_id"
            ),
            {"c": CONFIG},
        ).scalar_one()
        # Day-Ahead-Lastprognose der ÜNB, 2 MW über dem Ist
        for code, base in {**TARGETS, "load_da_forecast": TARGETS["load_actual"] + 2}.items():
            ser = c.execute(
                text(
                    "INSERT INTO energy.series (source_id, code, unit, resolution)"
                    " VALUES (:s, :c, 'x', '1 hour') RETURNING series_id"
                ),
                {"s": sid, "c": code},
            ).scalar_one()
            c.execute(
                text("INSERT INTO energy.value (series_id, ts, value) VALUES (:s, :t, :v)"),
                [{"s": ser, "t": h.to_pydatetime(), "v": base} for h in hours],
            )
            if code not in TARGETS:
                continue
            for i, v in enumerate(versions):
                c.execute(
                    text("INSERT INTO fcst.prediction VALUES (:m, :c, :i, :t, 0.5, :v)"),
                    [
                        {
                            "m": v,
                            "c": code,
                            "i": issue.to_pydatetime(),
                            "t": h.to_pydatetime(),
                            "v": base + 1 + i,
                        }
                        for h in hours
                    ],
                )


def _sql(panel: dict, ref: int = 0) -> str:  # type: ignore[type-arg]
    sql = str(panel["targets"][ref]["rawSql"])
    return re.sub(
        r"\$__timeFilter\(([^)]+)\)", r"\1 BETWEEN now() - interval '30 days' AND now()", sql
    )


def test_sync_and_dashboard_queries(engine: Engine, tmp_path: Path) -> None:
    now = pd.Timestamp.now(tz="UTC").floor("h")
    reg, versions = _registry(tmp_path, now)
    with engine.connect() as c:
        if c.execute(
            text("SELECT count(*) FROM meta.source WHERE code = :c"), {"c": CONFIG}
        ).scalar():
            pytest.skip("Reste eines abgebrochenen Laufs")
    assert sync_registry(engine, reg) == 2
    assert sync_registry(engine, reg) == 2  # zweiter Abgleich ändert nichts
    _fill(engine, versions, now)

    with engine.connect() as c:
        n = c.execute(
            text("SELECT count(*) FROM meta.model_metric WHERE version = ANY(:v)"),
            {"v": versions},
        ).scalar_one()
        assert n == 2 * 2 * 8  # Versionen x Ziele x (7 Vorlauftage + gesamt)
        err = c.execute(
            text(
                "SELECT DISTINCT model_version, target, err FROM fcst.prediction_error"
                " WHERE model_version = ANY(:v)"
            ),
            {"v": versions},
        ).all()
        assert {(r.model_version, r.target, r.err) for r in err} == {
            (v, t, 1.0 + i) for i, v in enumerate(versions) for t in TARGETS
        }

        panels = {p["id"]: p for p in json.loads(DASHBOARD.read_text())["panels"]}
        rows = {pid: c.execute(text(_sql(panels[pid]))).mappings().all() for pid in range(35, 44)}
        assert rows[35][0]["Champion"].startswith("kalender · ")
        mv = [r for r in rows[38] if r["Version"] in versions]
        assert [r["Rolle"] for r in mv] == ["Champion", "Challenger"]
        assert mv[0]["MAE Last D+1"] == pytest.approx(500.0)
        assert any(r["Ziel"] == "Preis €/MWh" and r["Gesamt"] for r in rows[41])
        live = [r for r in rows[42] if "Champion" in r["Version"] and r["Ziel"] == "Last MW"]
        assert live and live[0]["Bias"] == pytest.approx(1.0)
        assert any(r["Aktion"] == "Champion" for r in rows[43])
        assert rows[39] and rows[40]
        for pid in (39, 40, 65, 66):
            for ref in range(len(panels[pid]["targets"])):
                c.execute(text(_sql(panels[pid], ref))).all()
        # Absolut- und Prozentwerte getrennt: 39/40 nur MAE/RMSE, 65/66 nur Fehler %
        assert all(
            "Fehler %" not in t["rawSql"] for pid in (39, 40) for t in panels[pid]["targets"]
        )
        assert all("Fehler %" in t["rawSql"] for pid in (65, 66) for t in panels[pid]["targets"])
        # Relativer Fehler (WAPE): Tages-MAE / mittlerer |Ist|, hier 1 / 80 €/MWh
        pct = c.execute(text(_sql(panels[66]))).mappings().all()
        champ = [r for r in pct if r["metric"].endswith("Champion Fehler %")]
        assert champ and champ[0]["value"] == pytest.approx(100 / 80)

        # Gestern mit allen Vorlauftagen: Lauf 10 Uhr Ortszeit, drei Tage vor dem Liefertag
        # (vor dem Lauf aus _fill, damit sich die Zeilen nicht überschneiden)
        local = (now - timedelta(days=5)).tz_convert("Europe/Berlin").normalize()
        # Wanduhrzeit statt +10 h: am Tag der Zeitumstellung wäre das sonst 9 oder 11 Uhr
        issue = local.replace(hour=10).tz_convert("UTC")
        day = pd.date_range(local + pd.DateOffset(days=3), periods=3, freq="h").tz_convert("UTC")
    # D+1: Lauf 10 Uhr Ortszeit am Vortag der ersten Ist-Stunden aus _fill
    first = (now - timedelta(days=3)).floor("D") + timedelta(hours=24)
    d1_issue = (
        (first - timedelta(hours=2)).tz_convert("Europe/Berlin").normalize().replace(hour=10)
    ).tz_convert("UTC")
    d1 = pd.date_range(first, periods=6, freq="h")
    with engine.begin() as c:
        for i, v in enumerate(versions):
            c.execute(
                text("INSERT INTO fcst.prediction VALUES (:m, 'load_actual', :i, :t, 0.5, :v)"),
                [{"m": v, "i": issue.to_pydatetime(), "t": t.to_pydatetime(), "v": i} for t in day],
            )
        c.execute(
            text("INSERT INTO fcst.prediction VALUES (:m, 'load_actual', :i, :t, 0.5, :v)"),
            [
                {"m": versions[0], "i": d1_issue.to_pydatetime(), "t": t.to_pydatetime(), "v": 1}
                for t in d1
            ],
        )
    with engine.connect() as c:
        for pid, version in ((55, versions[0]), (57, versions[1])):
            sql = _sql(panels[pid], 1).replace("${challenger}", version)
            got = c.execute(text(sql)).mappings().all()
            assert "D+3" in {r["metric"] for r in got if r["time"] in set(day)}
            c.execute(text(_sql(panels[pid + 1], 1).replace("${challenger}", version))).all()
            # Preis heute und morgen: dieselbe Abfrage wie gestern
            for other in (pid + 4, pid + 5):
                assert _sql(panels[other], 1) == _sql(panels[pid + 1], 1)
            c.execute(text(_sql(panels[pid], 0))).all()
        # Fehler je Vorlauftag im Betrieb (Preis, Last) mit Vorwoche als Vergleich
        for pid in (63, 64):
            sql = _sql(panels[pid]).replace("${challenger}", versions[1])
            got = c.execute(text(sql)).mappings().all()
            assert all({"Vorlauf", "Verfahren", "MAE"} <= set(r) for r in got)
            if pid == 64:
                d3 = {r["Verfahren"].split()[0] for r in got if r["Vorlauf"] == "D+3"}
                assert {"Champion", "Challenger"} <= d3
                # ÜNB-Prognose nur bei D+1, auf denselben Stunden wie die Modelle
                uenb = {r["Vorlauf"]: r["MAE"] for r in got if r["Verfahren"].startswith("ÜNB")}
                assert uenb == {"D+1": pytest.approx(2.0)}
