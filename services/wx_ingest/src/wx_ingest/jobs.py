"""Backfill-Protokoll in ``meta.backfill_job`` (Status-Dashboard).

Die Datenbank ist für den Download nicht nötig: Ist sie nicht erreichbar, wird nur gewarnt
und ohne Protokoll weitergeladen.
"""

from __future__ import annotations

import socket
from datetime import datetime

from sqlalchemy import Engine, text
from sqlalchemy.exc import SQLAlchemyError

from predictiondb_core.log import get_logger
from wx_ingest.ingest import RunResult

log = get_logger(__name__)

#: Ein laufender Job ohne Lebenszeichen seit so langer Zeit gilt beim nächsten Start als abgebrochen
STALE = "1 hour"


class JobLog:
    def __init__(self, engine: Engine | None) -> None:
        self.engine = engine
        self.job_id: int | None = None

    def _run(self, sql: str, params: dict[str, object]) -> object | None:
        if self.engine is None:
            return None
        try:
            with self.engine.begin() as conn:
                return conn.execute(text(sql), params).scalar()
        except SQLAlchemyError as exc:
            log.warning("backfill_job_log_failed", error=str(exc).splitlines()[0])
            if self.job_id is None:
                self.engine = None  # ohne Datenbank weiter, nicht bei jedem Lauf erneut warnen
            return None

    def start(
        self,
        source: str,
        start: datetime,
        end: datetime,
        hours: list[int],
        newest_first: bool,
        n_runs: int,
    ) -> None:
        self._run(
            f"""UPDATE meta.backfill_job SET status = 'aborted', finished_at = updated_at,
                       error = 'ohne Abschluss beendet'
                 WHERE status = 'running' AND updated_at < now() - interval '{STALE}'""",
            {},
        )
        job_id = self._run(
            """INSERT INTO meta.backfill_job
                 (source_id, range_start, range_end, run_hours, newest_first, n_runs, host)
               SELECT source_id, :start, :end, :hours, :newest, :n, :host
                 FROM meta.source WHERE code = :source
               RETURNING job_id""",
            {
                "source": source,
                "start": start,
                "end": end,
                "hours": hours,
                "newest": newest_first,
                "n": n_runs,
                "host": socket.gethostname(),
            },
        )
        self.job_id = None if job_id is None else int(str(job_id))

    def progress(self, r: RunResult) -> None:
        if self.job_id is None:
            return
        skipped = r.complete and all(s.status == "skipped" for s in r.steps)
        self._run(
            """UPDATE meta.backfill_job
                  SET n_complete = n_complete + :complete,
                      n_skipped = n_skipped + :skipped,
                      n_incomplete = n_incomplete + :incomplete,
                      n_failed_steps = n_failed_steps + :failed,
                      bytes_stored = bytes_stored + :bytes,
                      last_run = :run, updated_at = now()
                WHERE job_id = :id""",
            {
                "id": self.job_id,
                "complete": int(r.complete),
                "skipped": int(skipped),
                "incomplete": int(not r.complete),
                "failed": sum(1 for s in r.steps if s.status == "failed"),
                "bytes": sum(s.size for s in r.steps),
                "run": r.run,
            },
        )

    def finish(self, status: str, error: str | None = None) -> None:
        if self.job_id is None:
            return
        self._run(
            """UPDATE meta.backfill_job
                  SET status = :status, error = :error, updated_at = now(), finished_at = now()
                WHERE job_id = :id""",
            {"id": self.job_id, "status": status, "error": error},
        )
