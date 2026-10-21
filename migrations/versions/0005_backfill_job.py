"""Protokoll der Backfill-Jobs (z. B. GFS-Historie) für das Status-Dashboard

Revision ID: 0005_backfill_job
Revises: 0004_grafana_reader
Create Date: 2026-10-21
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0005_backfill_job"
down_revision: str | None = "0004_grafana_reader"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Ein Eintrag je Aufruf von ``wx-ingest gfs backfill``; Zähler werden nach jedem Lauf
    # nachgeführt. Bleibt ein Job ohne Abschluss stehen (Container hart beendet), markiert ihn
    # der nächste Start als abgebrochen.
    op.execute("""
        CREATE TABLE meta.backfill_job (
          job_id         bigserial PRIMARY KEY,
          source_id      smallint NOT NULL REFERENCES meta.source,
          range_start    timestamptz NOT NULL,
          range_end      timestamptz NOT NULL,
          run_hours      smallint[] NOT NULL,
          newest_first   boolean NOT NULL,
          n_runs         int NOT NULL,
          n_complete     int NOT NULL DEFAULT 0,
          n_skipped      int NOT NULL DEFAULT 0,
          n_incomplete   int NOT NULL DEFAULT 0,
          n_failed_steps int NOT NULL DEFAULT 0,
          bytes_stored   bigint NOT NULL DEFAULT 0,
          last_run       timestamptz,
          status         text NOT NULL DEFAULT 'running'
                           CHECK (status IN ('running', 'done', 'failed', 'aborted')),
          error          text,
          host           text,
          started_at     timestamptz NOT NULL DEFAULT now(),
          updated_at     timestamptz NOT NULL DEFAULT now(),
          finished_at    timestamptz
        )""")
    op.execute("CREATE INDEX backfill_job_started ON meta.backfill_job (started_at DESC)")
    # Grafana liest über die Default-Privilegien aus 0004 (gilt auch für neue Tabellen)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS meta.backfill_job")
