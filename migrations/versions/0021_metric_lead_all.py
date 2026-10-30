"""Backtest-Kennzahl über alle Vorlauftage unter ``lead_day`` -1 statt 0

Revision ID: 0021_metric_lead_all
Revises: 0020_ml_job
Create Date: 2026-10-29

Vorlauftag 0 ist jetzt der Rest des Ausgabetags (``issue.lead_days`` mit 0). Die Zeile über alle
Vorlauftage in ``meta.model_metric`` wandert deshalb auf -1.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0021_metric_lead_all"
down_revision: str | None = "0020_ml_job"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("UPDATE meta.model_metric SET lead_day = -1 WHERE lead_day = 0")


def downgrade() -> None:
    op.execute("DELETE FROM meta.model_metric WHERE lead_day = 0")
    op.execute("UPDATE meta.model_metric SET lead_day = 0 WHERE lead_day = -1")
