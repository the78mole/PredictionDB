from datetime import UTC, datetime

from sqlalchemy import create_engine

from wx_ingest.ingest import RunResult, StepResult
from wx_ingest.jobs import JobLog

RUN = datetime(2021, 3, 22, 12, tzinfo=UTC)


def test_without_database_is_silent() -> None:
    jobs = JobLog(None)
    jobs.start("gfs_0p25", RUN, RUN, [0, 12], True, 1)
    jobs.progress(RunResult(RUN, [StepResult(0, "stored", "aws", 10)], complete=True))
    jobs.finish("done")
    assert jobs.job_id is None


def test_unreachable_database_disables_log() -> None:
    engine = create_engine(
        "postgresql+psycopg://x:y@127.0.0.1:9/none", connect_args={"connect_timeout": 1}
    )
    jobs = JobLog(engine)
    jobs.start("gfs_0p25", RUN, RUN, [0, 12], True, 1)
    assert jobs.engine is None and jobs.job_id is None


def test_statements_without_rows(tmp_path) -> None:  # type: ignore[no-untyped-def]
    # UPDATE liefert keine Zeilen; das darf das Protokoll nicht abschalten
    engine = create_engine(f"sqlite:///{tmp_path / 'db.sqlite'}")
    jobs = JobLog(engine)
    assert jobs._run("CREATE TABLE t (x int)", {}) is None
    assert jobs._run("SELECT 7", {}) == 7
    assert jobs.engine is engine
