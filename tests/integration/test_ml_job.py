"""Fortschritt der ``ml-train``-Jobs (``meta.ml_job``) und das Panel „Laufende Trainings“.

Läuft nur mit ``PDB_INTEGRATION=1`` nach ``migrate``. Angelegte Jobs werden wieder entfernt.
"""

import json
import os
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import Engine, text

from ml_train.jobs import JobLog
from predictiondb_core.db import create_db_engine
from predictiondb_core.settings import Settings

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.environ.get("PDB_INTEGRATION") != "1", reason="PDB_INTEGRATION != 1"),
]

DASHBOARD = Path(__file__).parents[2] / "config/grafana/dashboards/status.json"
CONFIG = "it_ml_job"


@pytest.fixture
def engine() -> Iterator[Engine]:
    eng = create_db_engine(Settings())
    yield eng
    with eng.begin() as c:
        c.execute(text("DELETE FROM meta.ml_job WHERE config = :c"), {"c": CONFIG})
    eng.dispose()


def _job(eng: Engine, job_id: int) -> dict[str, object]:
    with eng.connect() as c:
        return dict(
            c.execute(text("SELECT * FROM meta.ml_job WHERE job_id = :id"), {"id": job_id})
            .mappings()
            .one()
        )


def _panel_rows(eng: Engine) -> list[dict[str, object]]:
    panels = {p["title"]: p for p in json.loads(DASHBOARD.read_text())["panels"]}
    sql = panels["Laufende Trainings und Backtests"]["targets"][0]["rawSql"]
    with eng.connect() as c:
        return [dict(r) for r in c.execute(text(sql)).mappings()]


def test_job_lifecycle_and_panel(engine: Engine) -> None:
    job = JobLog(engine, heartbeat_s=0.2)
    job.start("backtest", CONFIG, ["kalender", "frei"], {"fold_months": 3})
    assert job.job_id is not None
    job.total(5)
    job.step("Merkmale bilden")
    job.step("Faltung 1/2 · kalender · 01/2026")
    job.step("Faltung 1/2 · frei · 01/2026")
    row = _job(engine, job.job_id)
    assert (row["steps_done"], row["steps_total"], row["status"]) == (2, 5, "running")
    assert row["variants"] == ["kalender", "frei"]

    # Lebenszeichen auch ohne neuen Schritt
    before = row["updated_at"]
    time.sleep(0.6)
    assert _job(engine, job.job_id)["updated_at"] > before  # type: ignore[operator]

    r = next(r for r in _panel_rows(engine) if r["Job"] == job.job_id)
    assert r["Art"] == "Backtest" and r["Status"] == "läuft"
    assert r["Schritt / Ergebnis"] == "Faltung 1/2 · frei · 01/2026"
    assert r["Erledigt"] == "2/5" and float(r["Fortschritt"]) == 40.0  # type: ignore[arg-type]
    assert r["Fertig ca."] is not None

    job.result = "/data/bt"
    job.finish()
    row = _job(engine, job.job_id)
    assert (row["status"], row["steps_done"], row["result"]) == ("done", 5, "/data/bt")
    r = next(r for r in _panel_rows(engine) if r["Job"] == job.job_id)
    assert r["Status"] == "fertig" and r["Erledigt"] == "5/5"
    assert r["Schritt / Ergebnis"] == "/data/bt"


def test_failed_and_stale_jobs(engine: Engine) -> None:
    failed = JobLog(engine, heartbeat_s=0)
    failed.start("fit", CONFIG, ["frei"])
    failed.total(4)
    failed.step("Snapshot laden")
    failed.step("Merkmale bilden")
    assert failed.job_id is not None
    failed.finish(error="MemoryError")
    row = _job(engine, failed.job_id)
    assert (row["status"], row["steps_done"], row["error"]) == ("failed", 1, "MemoryError")

    old = JobLog(engine, heartbeat_s=0)
    old.start("snapshot", CONFIG)
    assert old.job_id is not None
    with engine.begin() as c:
        c.execute(
            text(
                "UPDATE meta.ml_job SET updated_at = now() - interval '10 minutes'"
                " WHERE job_id = :id"
            ),
            {"id": old.job_id},
        )
    r = next(r for r in _panel_rows(engine) if r["Job"] == old.job_id)
    assert r["Status"] == "hängt?"
    with engine.begin() as c:
        c.execute(
            text(
                "UPDATE meta.ml_job SET updated_at = now() - interval '1 hour' WHERE job_id = :id"
            ),
            {"id": old.job_id},
        )
    JobLog(engine, heartbeat_s=0).start("snapshot", CONFIG)
    assert _job(engine, old.job_id)["status"] == "aborted"


def test_without_database_nothing_breaks() -> None:
    job = JobLog(None)
    job.start("fit", CONFIG)
    job.total(3)
    job.step("x")
    job.finish()
    assert job.job_id is None
