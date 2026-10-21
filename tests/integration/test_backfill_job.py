"""Backfill-Protokoll (``meta.backfill_job``, ``meta.backfill_run``) und die Dashboard-Abfragen.

Läuft nur mit ``PDB_INTEGRATION=1`` nach ``migrate``. Angelegte Jobs werden wieder entfernt.
"""

import json
import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import Engine, text

from predictiondb_core.db import create_db_engine
from predictiondb_core.settings import Settings
from wx_ingest.ingest import RunResult, StepResult
from wx_ingest.jobs import JobLog

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.environ.get("PDB_INTEGRATION") != "1", reason="PDB_INTEGRATION != 1"),
]

DASHBOARD = Path(__file__).parents[2] / "config/grafana/dashboards/status.json"
RUN = datetime(2099, 1, 1, tzinfo=UTC)
RUNS = [RUN + timedelta(hours=12 * i) for i in range(3)]


@pytest.fixture
def engine() -> Iterator[Engine]:
    eng = create_db_engine(Settings())
    yield eng
    with eng.begin() as c:
        c.execute(text("DELETE FROM meta.backfill_job WHERE range_start >= '2099-01-01'"))


def _job(eng: Engine, job_id: int) -> dict[str, object]:
    with eng.connect() as c:
        row = c.execute(
            text("SELECT * FROM meta.backfill_job WHERE job_id = :id"), {"id": job_id}
        ).mappings()
        return dict(row.one())


def test_job_log_lifecycle(engine: Engine) -> None:
    jobs = JobLog(engine)
    jobs.start("gfs_0p25", RUN, RUN, [0, 12], True, RUNS)
    assert jobs.job_id is not None
    jobs.progress(RunResult(RUN, [StepResult(0, "stored", "aws", 100)], complete=True))
    jobs.progress(RunResult(RUN, [StepResult(0, "skipped")], complete=True))
    jobs.progress(RunResult(RUN, [StepResult(0, "failed", errors=["x"])]))
    row = _job(engine, jobs.job_id)
    assert (row["n_complete"], row["n_skipped"], row["n_incomplete"]) == (2, 1, 1)
    assert (row["n_failed_steps"], row["bytes_stored"], row["status"]) == (1, 100, "running")

    panels = {p["id"]: p for p in json.loads(DASHBOARD.read_text())["panels"]}
    with engine.connect() as c:
        running = c.execute(text(panels[25]["targets"][0]["rawSql"])).mappings().all()
    assert any(r["Job"] == jobs.job_id and float(r["Fortschritt"]) == 100.0 for r in running)

    jobs.finish("done")
    with engine.connect() as c:
        done = c.execute(text(panels[26]["targets"][0]["rawSql"])).mappings().all()
    assert done[0]["Job"] == jobs.job_id and done[0]["Status"] == "fertig"


def test_stale_job_is_marked_aborted(engine: Engine) -> None:
    old = JobLog(engine)
    old.start("gfs_0p25", RUN, RUN, [0], False, [RUN])
    old.run_started(RUN, 2)
    assert old.job_id is not None
    with engine.begin() as c:
        c.execute(
            text(
                "UPDATE meta.backfill_job SET updated_at = now() - interval '2 hours' "
                "WHERE job_id = :id"
            ),
            {"id": old.job_id},
        )
    JobLog(engine).start("gfs_0p25", RUN, RUN, [0], False, [RUN])
    assert _job(engine, old.job_id)["status"] == "aborted"
    assert _runs(engine, old.job_id)[RUN]["status"] == "aborted"


def _runs(eng: Engine, job_id: int) -> dict[datetime, dict[str, object]]:
    with eng.connect() as c:
        rows = c.execute(
            text("SELECT * FROM meta.backfill_run WHERE job_id = :id"), {"id": job_id}
        ).mappings()
        return {r["run_time"]: dict(r) for r in rows}


def test_run_log_queue_and_dashboard(engine: Engine) -> None:
    jobs = JobLog(engine)
    jobs.start("gfs_0p25", RUN, RUN, [0, 12], True, RUNS[::-1])
    assert jobs.job_id is not None
    runs = _runs(engine, jobs.job_id)
    assert [runs[r]["queue_pos"] for r in RUNS[::-1]] == [1, 2, 3]
    assert {r["status"] for r in runs.values()} == {"queued"}

    newest, middle, oldest = RUNS[::-1]
    jobs.run_started(newest, 2)
    jobs.step_done(newest, StepResult(0, "stored", "aws", 100))
    jobs.run_started(middle, 2)
    jobs.progress(RunResult(middle, [StepResult(0, "skipped"), StepResult(3, "skipped")], True))
    runs = _runs(engine, jobs.job_id)
    assert (runs[newest]["status"], runs[newest]["steps_done"]) == ("running", 1)
    assert (runs[newest]["bytes_stored"], runs[middle]["status"]) == (100, "skipped")

    panels = {p["id"]: p for p in json.loads(DASHBOARD.read_text())["panels"]}
    with engine.connect() as c:
        queue = c.execute(text(panels[27]["targets"][0]["rawSql"])).mappings().all()
        done = c.execute(text(panels[28]["targets"][0]["rawSql"])).mappings().all()
    mine = [r for r in queue if r["Lauf"].endswith("2099 00Z") or r["Lauf"].endswith("2099 12Z")]
    assert [(r["Status"], r["Pos"]) for r in mine] == [("läuft", 1), ("wartet", 3)]
    assert float(mine[0]["Fortschritt"]) == 50.0
    assert done[0]["Lauf"].startswith("01.01.2099") and done[0]["Ergebnis"] == "schon da"

    jobs.finish("aborted", "abgebrochen")
    runs = _runs(engine, jobs.job_id)
    assert (runs[newest]["status"], runs[oldest]["status"]) == ("aborted", "queued")


def test_live_job(engine: Engine) -> None:
    jobs = JobLog(engine)
    jobs.start("gfs_0p25", RUN, RUN, [0, 12], False, RUNS[:2], "live")
    assert jobs.job_id is not None
    jobs.live_window(RUN, RUNS[2], RUNS)  # neuer Lauf kommt hinten dazu
    jobs.run_started(RUNS[0], 2)
    jobs.step_done(RUNS[0], StepResult(0, "stored", "nomads", 50))
    jobs.live_progress(RunResult(RUNS[0], [StepResult(0, "stored"), StepResult(3, "pending")]))
    jobs.live_progress(RunResult(RUNS[1], [StepResult(0, "skipped")], complete=True))
    runs = _runs(engine, jobs.job_id)
    assert [runs[r]["queue_pos"] for r in RUNS] == [1, 2, 3]
    assert (runs[RUNS[0]]["status"], runs[RUNS[0]]["steps_done"]) == ("queued", 1)
    assert runs[RUNS[1]]["status"] == "complete"
    row = _job(engine, jobs.job_id)
    assert (row["mode"], row["n_runs"], row["n_complete"]) == ("live", 3, 1)
    assert row["bytes_stored"] == 50

    jobs.live_window(RUNS[1], RUNS[2], RUNS[1:])  # RUNS[0] fällt aus dem Fenster
    assert _runs(engine, jobs.job_id)[RUNS[0]]["status"] == "incomplete"
