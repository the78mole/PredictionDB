"""Fortschritt laufender Jobs (``meta.ml_job``) für das Status-Dashboard.

Die Datenbank ist für Training und Backtest nicht nötig: Ist sie nicht erreichbar, wird einmal
gewarnt und ohne Protokoll weitergerechnet. Ein Lebenszeichen-Thread hält ``updated_at`` auch in
langen Schritten frisch, damit das Dashboard hängende von langsamen Jobs unterscheiden kann.
"""

from __future__ import annotations

import json
import os
import socket
import threading
from collections.abc import Callable, Sequence
from typing import Any

from sqlalchemy import Engine, text

from predictiondb_core.log import get_logger

log = get_logger(__name__)

#: Ein laufender Job ohne Lebenszeichen seit so langer Zeit gilt beim nächsten Start als abgebrochen
STALE = "15 minutes"
#: Sekunden zwischen zwei Lebenszeichen
HEARTBEAT_S = 60.0

#: Rückmeldung eines Rechenschritts: ``progress("Faltung 3/12 · frei")`` beginnt einen Schritt
Progress = Callable[[str], None]


def no_progress(_: str) -> None:
    pass


class JobLog:
    """Ein ``ml-train``-Aufruf als Zeile in ``meta.ml_job``.

    ``step(label)`` schließt den vorigen Schritt ab und beginnt den nächsten; ``total`` lässt
    sich nachreichen, sobald die Zahl der Schritte bekannt ist (z. B. erst nach den Faltungen).
    """

    def __init__(self, engine: Engine | None, heartbeat_s: float = HEARTBEAT_S) -> None:
        self.engine = engine
        self.job_id: int | None = None
        #: Ergebnis für die Übersicht (Version, Snapshot-ID, Ausgabeordner), vor ``finish`` setzen
        self.result: str | None = None
        self._steps = 0
        self._stop = threading.Event()
        self._beat: threading.Thread | None = None
        self._heartbeat_s = heartbeat_s

    def _run(self, sql: str, params: dict[str, Any]) -> Any:
        if self.engine is None:
            return None
        try:
            with self.engine.begin() as conn:
                result = conn.execute(text(sql), params)
                return result.scalar() if result.returns_rows else None
        except Exception as exc:  # das Protokoll darf Training und Backtest nie aufhalten
            log.warning("ml_job_log_failed", error=str(exc).splitlines()[0])
            if self.job_id is None:
                self.engine = None  # ohne Datenbank weiter, nicht bei jedem Schritt erneut warnen
            return None

    def start(
        self,
        command: str,
        config: str,
        variants: Sequence[str] = (),
        args: dict[str, Any] | None = None,
        total: int | None = None,
    ) -> None:
        self._run(
            f"""UPDATE meta.ml_job SET status = 'aborted', finished_at = updated_at,
                       error = 'ohne Abschluss beendet'
                 WHERE status = 'running' AND updated_at < now() - interval '{STALE}'""",
            {},
        )
        job_id = self._run(
            """INSERT INTO meta.ml_job (command, config, variants, args, steps_total, host, pid)
               VALUES (:command, :config, :variants, CAST(:args AS jsonb), :total, :host, :pid)
               RETURNING job_id""",
            {
                "command": command,
                "config": config,
                "variants": list(variants),
                "args": json.dumps(args or {}, default=str),
                "total": total,
                "host": socket.gethostname(),
                "pid": os.getpid(),
            },
        )
        self.job_id = None if job_id is None else int(job_id)
        if self.job_id is not None and self._heartbeat_s > 0:
            self._beat = threading.Thread(target=self._heartbeat, name="ml-job", daemon=True)
            self._beat.start()

    def _heartbeat(self) -> None:
        while not self._stop.wait(self._heartbeat_s):
            self._run(
                "UPDATE meta.ml_job SET updated_at = now() WHERE job_id = :id", {"id": self.job_id}
            )

    def total(self, n: int) -> None:
        if self.job_id is None:
            return
        self._run(
            "UPDATE meta.ml_job SET steps_total = :n, updated_at = now() WHERE job_id = :id",
            {"id": self.job_id, "n": n},
        )

    def step(self, label: str) -> None:
        """Vorigen Schritt als erledigt zählen und ``label`` als laufenden Schritt eintragen."""
        log.info("ml_job.step", step=label)
        if self.job_id is None:
            return
        done, self._steps = self._steps, self._steps + 1
        self._run(
            """UPDATE meta.ml_job SET step = :step, steps_done = :done, step_started_at = now(),
                      updated_at = now()
                WHERE job_id = :id""",
            {"id": self.job_id, "step": label, "done": done},
        )

    def finish(self, error: str | None = None) -> None:
        self._stop.set()
        if self._beat is not None:
            self._beat.join(timeout=5)
        if self.job_id is None:
            return
        self._run(
            """UPDATE meta.ml_job
                  SET status = :status, result = :result, error = :error, step = NULL,
                      steps_done = CASE WHEN :status = 'done'
                                        THEN coalesce(steps_total, :steps) ELSE :done END,
                      finished_at = now(), updated_at = now()
                WHERE job_id = :id""",
            {
                "id": self.job_id,
                "status": "failed" if error else "done",
                "result": self.result,
                "error": error,
                "steps": self._steps,
                "done": max(self._steps - 1, 0),
            },
        )


def open_job_log() -> JobLog:
    """``JobLog`` mit der Datenbank aus den Einstellungen, ohne sie, wenn keine konfiguriert ist."""
    try:
        from predictiondb_core.db import create_db_engine

        return JobLog(create_db_engine())
    except Exception as exc:
        log.warning("ml_job_log_unavailable", error=str(exc).splitlines()[0])
        return JobLog(None)
