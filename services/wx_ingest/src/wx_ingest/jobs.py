"""Backfill-Protokoll je Job (``meta.backfill_job``) und je Lauf (``meta.backfill_run``).

Beides speist die Backfill-Tabellen im Status-Dashboard.

Die Datenbank ist für den Download nicht nötig: Ist sie nicht erreichbar, wird nur gewarnt
und ohne Protokoll weitergeladen.
"""

from __future__ import annotations

import socket
from datetime import datetime

from sqlalchemy import Engine, text
from sqlalchemy.exc import SQLAlchemyError

from predictiondb_core.log import get_logger
from wx_ingest.ingest import RunResult, StepResult

log = get_logger(__name__)

#: Ein laufender Job ohne Lebenszeichen seit so langer Zeit gilt beim nächsten Start als abgebrochen
STALE = "1 hour"


class JobLog:
    def __init__(self, engine: Engine | None) -> None:
        self.engine = engine
        self.job_id: int | None = None
        self.mode = "backfill"

    def _run(self, sql: str, params: dict[str, object]) -> object | None:
        if self.engine is None:
            return None
        try:
            with self.engine.begin() as conn:
                result = conn.execute(text(sql), params)
                return result.scalar() if result.returns_rows else None
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
        runs: list[datetime],
        mode: str = "backfill",
    ) -> None:
        """Legt den Job und seine Läufe in Warteschlangen-Reihenfolge an."""
        self.mode = mode
        host = socket.gethostname()
        # Verwaist sind Jobs ohne Lebenszeichen und Jobs desselben Dienstes (Quelle und Modus)
        # aus einem anderen Container: Je Dienst läuft nur ein Container, ein Neustart (Deploy)
        # beendet den alten, auch wenn er seinen Abschluss nicht mehr schreiben konnte.
        self._run(
            f"""WITH stale AS (
                  UPDATE meta.backfill_job SET status = 'aborted', finished_at = updated_at,
                         error = 'ohne Abschluss beendet'
                   WHERE status = 'running'
                     AND (updated_at < now() - interval '{STALE}'
                          OR (mode = :mode AND host IS DISTINCT FROM :host
                              AND source_id = (SELECT source_id FROM meta.source
                                                WHERE code = :source)))
                  RETURNING job_id, mode)
                UPDATE meta.backfill_run r
                   SET status = CASE s.mode WHEN 'live' THEN 'handed_over' ELSE 'aborted' END,
                       finished_at = now(), updated_at = now()
                  FROM stale s
                 WHERE r.job_id = s.job_id
                   AND (r.status = 'running' OR (s.mode = 'live' AND r.status = 'queued'))""",
            {"mode": mode, "host": host, "source": source},
        )
        # Läufe beendeter Jobs, die noch als laufend stehen (z. B. nach einem Absturz)
        self._run(
            """UPDATE meta.backfill_run r SET status = 'aborted', finished_at = now(),
                      updated_at = now()
                 FROM meta.backfill_job j
                WHERE r.job_id = j.job_id AND j.status <> 'running' AND r.status = 'running'""",
            {},
        )
        job_id = self._run(
            """INSERT INTO meta.backfill_job
                 (source_id, range_start, range_end, run_hours, newest_first, n_runs, host, mode)
               SELECT source_id, :start, :end, :hours, :newest, :n, :host, :mode
                 FROM meta.source WHERE code = :source
               RETURNING job_id""",
            {
                "source": source,
                "start": start,
                "end": end,
                "hours": hours,
                "newest": newest_first,
                "n": len(runs),
                "host": host,
                "mode": mode,
            },
        )
        self.job_id = None if job_id is None else int(str(job_id))
        if self.job_id is not None:
            self._run(
                """INSERT INTO meta.backfill_run (job_id, run_time, queue_pos)
                   SELECT :id, r, n FROM unnest(CAST(:runs AS timestamptz[]))
                                         WITH ORDINALITY AS q(r, n)""",
                {"id": self.job_id, "runs": runs},
            )

    def run_started(self, run: datetime, n_steps: int) -> None:
        if self.job_id is None:
            return
        self._run(
            """UPDATE meta.backfill_run
                  SET status = 'running', n_steps = :n,
                      steps_done = CASE WHEN started_at IS NULL THEN 0 ELSE steps_done END,
                      started_at = coalesce(started_at, now()), updated_at = now()
                WHERE job_id = :id AND run_time = :run AND status <> 'complete'""",
            {"id": self.job_id, "run": run, "n": n_steps},
        )

    def step_done(self, run: datetime, step: StepResult) -> None:
        if self.job_id is None:
            return
        self._run(
            """UPDATE meta.backfill_run
                  SET steps_done = steps_done + 1,
                      steps_stored = steps_stored + :stored,
                      steps_failed = steps_failed + :failed,
                      bytes_stored = bytes_stored + :bytes, updated_at = now()
                WHERE job_id = :id AND run_time = :run""",
            {
                "id": self.job_id,
                "run": run,
                "stored": int(step.status == "stored"),
                "failed": int(step.status == "failed"),
                "bytes": step.size,
            },
        )

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
        self._run(
            """UPDATE meta.backfill_run
                  SET status = :status, steps_done = :n, finished_at = now(), updated_at = now()
                WHERE job_id = :id AND run_time = :run""",
            {
                "id": self.job_id,
                "run": r.run,
                "n": len(r.steps),
                "status": "skipped" if skipped else "complete" if r.complete else "incomplete",
            },
        )

    def live_window(self, start: datetime, end: datetime, runs: list[datetime]) -> None:
        """Live-Modus: gleitendes Fenster nachführen, neue Läufe hinten einreihen."""
        if self.job_id is None:
            return
        self._run(
            """INSERT INTO meta.backfill_run (job_id, run_time, queue_pos)
               SELECT :id, r, mx.m + row_number() OVER (ORDER BY n)
                 FROM unnest(CAST(:runs AS timestamptz[])) WITH ORDINALITY AS q(r, n),
                      (SELECT coalesce(max(queue_pos), 0) AS m FROM meta.backfill_run
                        WHERE job_id = :id) mx
                WHERE NOT EXISTS (SELECT 1 FROM meta.backfill_run b
                                   WHERE b.job_id = :id AND b.run_time = q.r)
               ON CONFLICT (job_id, run_time) DO NOTHING""",
            {"id": self.job_id, "runs": runs},
        )
        # Aus dem Fenster gefallene, nie vollständig gewordene Läufe abschließen
        self._run(
            """UPDATE meta.backfill_run SET status = 'incomplete', finished_at = now(),
                      updated_at = now()
                WHERE job_id = :id AND run_time < :start AND status IN ('queued', 'running')""",
            {"id": self.job_id, "start": start},
        )
        self._run(
            """UPDATE meta.backfill_job
                  SET range_start = :start, range_end = :end, updated_at = now(),
                      n_runs = (SELECT count(*) FROM meta.backfill_run WHERE job_id = :id),
                      n_incomplete = (SELECT count(*) FROM meta.backfill_run
                                       WHERE job_id = :id AND status = 'incomplete')
                WHERE job_id = :id""",
            {"id": self.job_id, "start": start, "end": end},
        )

    def live_progress(self, r: RunResult) -> None:
        """Live-Modus: Stand eines Laufs nach dem Zyklus; unvollständige warten weiter."""
        if self.job_id is None:
            return
        done = sum(1 for s in r.steps if s.status in ("stored", "skipped"))
        self._run(
            """UPDATE meta.backfill_run
                  SET status = CASE WHEN :complete THEN 'complete' ELSE 'queued' END,
                      steps_done = :done,
                      finished_at = CASE WHEN :complete THEN now() END, updated_at = now()
                WHERE job_id = :id AND run_time = :run AND status <> 'complete'""",
            {"id": self.job_id, "run": r.run, "complete": r.complete, "done": done},
        )
        self._run(
            """UPDATE meta.backfill_job j
                  SET n_complete = c.complete, n_skipped = c.skipped, n_failed_steps = c.failed,
                      bytes_stored = c.bytes, last_run = :run, updated_at = now()
                 FROM (SELECT count(*) FILTER (WHERE status = 'complete') AS complete,
                              count(*) FILTER (WHERE status = 'complete' AND steps_stored = 0)
                                AS skipped,
                              coalesce(sum(steps_failed), 0) AS failed,
                              coalesce(sum(bytes_stored), 0) AS bytes
                         FROM meta.backfill_run WHERE job_id = :id) c
                WHERE j.job_id = :id""",
            {"id": self.job_id, "run": r.run},
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
        if self.mode == "live":
            # Offene Läufe (meist der jüngste, bei NOAA noch nicht vollständig) lädt der nächste
            # Live-Job; sie zählen beim beendeten Job weder als fertig noch als fehlend.
            self._run(
                """UPDATE meta.backfill_run SET status = 'handed_over', finished_at = now(),
                          updated_at = now()
                    WHERE job_id = :id AND status IN ('queued', 'running')""",
                {"id": self.job_id},
            )
            return
        # Läufe, die mitten im Download standen, gelten als abgebrochen; wartende bleiben
        # liegen und werden vom nächsten Job neu eingeplant.
        self._run(
            """UPDATE meta.backfill_run SET status = 'aborted', finished_at = now(),
                      updated_at = now()
                WHERE job_id = :id AND status = 'running'""",
            {"id": self.job_id},
        )
