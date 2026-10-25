"""Aktualität: Monatswerte von Eurostat dürfen bis 120 Tage alt sein

Eurostat veröffentlicht den Gasbestand am Monatsende (``nrg_stk_gasm``) je nach Land drei
bis zwölf Wochen nach Monatsende. Der Wert steht am Beginn des Folgemonats; mit 75 Tagen
löste der Alarm „Reihe veraltet“ vor fast jeder Veröffentlichung aus.

Revision ID: 0013_freshness_eurostat
Revises: 0012_ml_registry
Create Date: 2026-10-25
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0013_freshness_eurostat"
down_revision: str | None = "0012_ml_registry"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_VIEW = """
    CREATE OR REPLACE VIEW energy.series_freshness AS
    SELECT src.code AS source, s.code, s.resolution, src.enabled, last.ts AS last_ts,
           CASE WHEN s.resolution <= interval '1 hour' THEN interval '2 days'
                WHEN s.resolution <= interval '1 day' THEN interval '7 days'
                WHEN s.resolution <= interval '7 days' THEN interval '21 days'
                {eurostat}WHEN s.resolution <= interval '31 days' THEN interval '75 days'
                ELSE interval '800 days' END AS max_age
      FROM energy.series s
      JOIN meta.source src ON src.source_id = s.source_id
      LEFT JOIN LATERAL (
        SELECT max(v.ts) AS ts FROM energy.value v WHERE v.series_id = s.series_id
      ) last ON true"""

_EUROSTAT = """WHEN s.resolution <= interval '31 days' AND src.code = 'eurostat'
                     THEN interval '120 days'
                """


def upgrade() -> None:
    op.execute(_VIEW.format(eurostat=_EUROSTAT))


def downgrade() -> None:
    op.execute(_VIEW.format(eurostat=""))
