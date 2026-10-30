"""Silber-Status ``evicted``: lokal gelöscht, liegt weiter auf GHCR

Revision ID: 0026_silver_evicted
Revises: 0025_slp_h0
Create Date: 2026-10-30

``wx-process gfs evict-silver`` löscht lokales Silber, das aus GHCR importiert wurde und dessen
Gold gerechnet ist. Der Lauf steht dann als ``evicted`` in ``meta.silver_run``: der Import holt
ihn nicht erneut, das Status-Dashboard zählt ihn weiter als fertig.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0026_silver_evicted"
down_revision: str | None = "0025_slp_h0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE meta.silver_run DROP CONSTRAINT IF EXISTS silver_run_status_check")
    op.execute("""
        ALTER TABLE meta.silver_run ADD CONSTRAINT silver_run_status_check
          CHECK (status IN ('loading', 'present', 'evicted'))""")


def downgrade() -> None:
    op.execute("DELETE FROM meta.silver_run WHERE status = 'evicted'")
    op.execute("ALTER TABLE meta.silver_run DROP CONSTRAINT IF EXISTS silver_run_status_check")
    op.execute("""
        ALTER TABLE meta.silver_run ADD CONSTRAINT silver_run_status_check
          CHECK (status IN ('loading', 'present'))""")
