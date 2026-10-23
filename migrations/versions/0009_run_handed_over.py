"""Laufstatus 'handed_over': offene Läufe eines beendeten Live-Jobs übernimmt der nächste Job

Revision ID: 0008_run_handed_over
Revises: 0007_job_mode
Create Date: 2026-10-23
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0008_run_handed_over"
down_revision: str | None = "0007_job_mode"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

STATUSES = "'queued', 'running', 'complete', 'skipped', 'incomplete', 'aborted'"


def upgrade() -> None:
    # Ein Live-Job endet mit jedem Neustart des Containers; der jüngste Lauf ist dann bei NOAA
    # oft noch nicht (vollständig) veröffentlicht und wird vom Folgejob geladen. Bisher blieb er
    # 'queued' bzw. 'aborted' und drückte den Fortschritt des beendeten Jobs unter 100 %.
    op.execute("ALTER TABLE meta.backfill_run DROP CONSTRAINT backfill_run_status_check")
    op.execute(f"""
        ALTER TABLE meta.backfill_run ADD CONSTRAINT backfill_run_status_check
          CHECK (status IN ({STATUSES}, 'handed_over'))""")
    op.execute("""
        UPDATE meta.backfill_run r
           SET status = 'handed_over', finished_at = coalesce(r.finished_at, j.finished_at),
               updated_at = now()
          FROM meta.backfill_job j
         WHERE r.job_id = j.job_id AND j.mode = 'live' AND j.status <> 'running'
           AND r.status IN ('queued', 'running', 'aborted')""")


def downgrade() -> None:
    op.execute("UPDATE meta.backfill_run SET status = 'aborted' WHERE status = 'handed_over'")
    op.execute("ALTER TABLE meta.backfill_run DROP CONSTRAINT backfill_run_status_check")
    op.execute(f"""
        ALTER TABLE meta.backfill_run ADD CONSTRAINT backfill_run_status_check
          CHECK (status IN ({STATUSES}))""")
