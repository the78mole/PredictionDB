"""Aktualität: Jahreswerte dürfen bis gut zwei Jahre alt sein

Der Jahreswert eines Jahres steht am 1. Januar und bleibt der jüngste, bis das Folgejahr
vorbei ist und gerechnet wurde. 500 Tage lösten den Alarm „Reihe veraltet“ jedes Jahr ab Mai
aus.

Revision ID: 0011_freshness_year
Revises: 0010_run_handed_over
Create Date: 2026-10-24
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0011_freshness_year"
down_revision: str | None = "0010_run_handed_over"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_VIEW = """
    CREATE OR REPLACE VIEW energy.series_freshness AS
    SELECT src.code AS source, s.code, s.resolution, src.enabled, last.ts AS last_ts,
           CASE WHEN s.resolution <= interval '1 hour' THEN interval '2 days'
                WHEN s.resolution <= interval '1 day' THEN interval '7 days'
                WHEN s.resolution <= interval '7 days' THEN interval '21 days'
                WHEN s.resolution <= interval '31 days' THEN interval '75 days'
                ELSE interval '{year}' END AS max_age
      FROM energy.series s
      JOIN meta.source src ON src.source_id = s.source_id
      LEFT JOIN LATERAL (
        SELECT max(v.ts) AS ts FROM energy.value v WHERE v.series_id = s.series_id
      ) last ON true"""


def upgrade() -> None:
    op.execute(_VIEW.format(year="800 days"))


def downgrade() -> None:
    op.execute(_VIEW.format(year="500 days"))
