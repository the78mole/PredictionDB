"""Backfill-Protokoll je Lauf (Warteschlange, laufend, fertig) für das Status-Dashboard

Revision ID: 0006_backfill_run
Revises: 0005_backfill_job
Create Date: 2026-10-21
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0006_backfill_run"
down_revision: str | None = "0005_backfill_job"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Beim Start eines Jobs landen alle Läufe mit ihrer Position in der Warteschlange hier
    # ('queued'); der Downloader führt Status und Schrittzähler je Lauf nach.
    op.execute("""
        CREATE TABLE meta.backfill_run (
          job_id       bigint NOT NULL REFERENCES meta.backfill_job ON DELETE CASCADE,
          run_time     timestamptz NOT NULL,
          queue_pos    int NOT NULL,
          status       text NOT NULL DEFAULT 'queued'
                         CHECK (status IN ('queued', 'running', 'complete', 'skipped',
                                           'incomplete', 'aborted')),
          n_steps      int,
          steps_done   int NOT NULL DEFAULT 0,
          steps_stored int NOT NULL DEFAULT 0,
          steps_failed int NOT NULL DEFAULT 0,
          bytes_stored bigint NOT NULL DEFAULT 0,
          started_at   timestamptz,
          finished_at  timestamptz,
          updated_at   timestamptz NOT NULL DEFAULT now(),
          PRIMARY KEY (job_id, run_time)
        )""")
    op.execute("CREATE INDEX backfill_run_queue ON meta.backfill_run (status, job_id, queue_pos)")
    op.execute("CREATE INDEX backfill_run_finished ON meta.backfill_run (finished_at DESC)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS meta.backfill_run")
