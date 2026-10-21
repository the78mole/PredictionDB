"""Backfill-Protokoll auch für den Live-Download (Spalte mode)

Revision ID: 0007_job_mode
Revises: 0006_backfill_run
Create Date: 2026-10-21
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0007_job_mode"
down_revision: str | None = "0006_backfill_run"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 'live': ein Eintrag je Start von ``wx-ingest gfs latest``; Zeitraum und Läufe folgen dem
    # gleitenden Rückblickfenster, unvollständige Läufe warten ('queued') auf den nächsten Zyklus.
    op.execute("""
        ALTER TABLE meta.backfill_job
          ADD COLUMN mode text NOT NULL DEFAULT 'backfill'
            CHECK (mode IN ('backfill', 'live'))""")


def downgrade() -> None:
    op.execute("DELETE FROM meta.backfill_job WHERE mode = 'live'")
    op.execute("ALTER TABLE meta.backfill_job DROP COLUMN mode")
