"""Aktualität: Öl- und Kohlebilanz von Eurostat dürfen bis 240 Tage alt sein

Eurostat veröffentlicht die EU-Summen der Öl- und Kohlestatistik und die Kohle je Land bis zu
vier Monate nach Monatsende. Mit 150 Tagen löste „Reihe veraltet“ für diese Reihen vor fast
jeder Veröffentlichung aus. Die Gasreihen und die Ölreihen für DE bleiben bei 150 Tagen.

Revision ID: 0014_freshness_eurostat_fuels
Revises: 0013_freshness_eurostat
Create Date: 2026-10-25
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0014_freshness_eurostat_fuels"
down_revision: str | None = "0013_freshness_eurostat"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_VIEW = """
    CREATE OR REPLACE VIEW energy.series_freshness AS
    SELECT src.code AS source, s.code, s.resolution, src.enabled, last.ts AS last_ts,
           CASE WHEN s.resolution <= interval '1 hour' THEN interval '2 days'
                WHEN s.resolution <= interval '1 day' THEN interval '7 days'
                WHEN s.resolution <= interval '7 days' THEN interval '21 days'
                {slow}WHEN s.resolution <= interval '31 days' AND src.code = 'eurostat'
                     THEN interval '150 days'
                WHEN s.resolution <= interval '31 days' THEN interval '75 days'
                ELSE interval '800 days' END AS max_age
      FROM energy.series s
      JOIN meta.source src ON src.source_id = s.source_id
      LEFT JOIN LATERAL (
        SELECT max(v.ts) AS ts FROM energy.value v WHERE v.series_id = s.series_id
      ) last ON true"""

_SLOW = r"""WHEN s.resolution <= interval '31 days' AND src.code = 'eurostat'
                     AND (s.code LIKE 'coal\_%'
                          OR (s.code LIKE 'oil\_%' AND s.code LIKE '%\_eu\_m'))
                     THEN interval '240 days'
                """


def upgrade() -> None:
    op.execute(_VIEW.format(slow=_SLOW))


def downgrade() -> None:
    op.execute(_VIEW.format(slow=""))
