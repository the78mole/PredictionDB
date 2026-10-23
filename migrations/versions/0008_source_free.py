"""Freie und unfreie Quellen: meta.source.free/enabled, SMARD vor ENTSO-E

Revision ID: 0008_source_free
Revises: 0007_job_mode
Create Date: 2026-10-23
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0008_source_free"
down_revision: str | None = "0007_job_mode"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Nutzungsbedingungen schränken die Weitergabe ein (docs/ARCHITEKTUR.md 7.11.1)
UNFREE = ("entsoe", "energy_charts", "netztransparenz")


def upgrade() -> None:
    # free: Bedingungen der Quelle erlauben freie Weitergabe; enabled: Freigabe im Betrieb,
    # energy-ingest setzt sie beim Start aus PDB_ALLOW_*. Bis dahin sind unfreie Quellen aus.
    op.execute("ALTER TABLE meta.source ADD COLUMN free boolean NOT NULL DEFAULT true")
    op.execute("ALTER TABLE meta.source ADD COLUMN enabled boolean NOT NULL DEFAULT true")
    codes = ", ".join(f"'{c}'" for c in UNFREE)
    op.execute(f"UPDATE meta.source SET free = false, enabled = false WHERE code IN ({codes})")
    op.execute("UPDATE meta.source SET priority = 10 WHERE code = 'smard'")
    op.execute("UPDATE meta.source SET priority = 20 WHERE code = 'entsoe'")
    op.execute("""
        CREATE OR REPLACE VIEW energy.value_best AS
        SELECT DISTINCT ON (s.code, v.ts)
               s.code, v.ts, v.value, v.published, src.code AS source, s.unit
          FROM energy.value v
          JOIN energy.series s USING (series_id)
          JOIN meta.source src ON src.source_id = s.source_id
         WHERE v.value IS NOT NULL AND src.enabled
         ORDER BY s.code, v.ts, src.priority, src.source_id""")


def downgrade() -> None:
    op.execute("""
        CREATE OR REPLACE VIEW energy.value_best AS
        SELECT DISTINCT ON (s.code, v.ts)
               s.code, v.ts, v.value, v.published, src.code AS source, s.unit
          FROM energy.value v
          JOIN energy.series s USING (series_id)
          JOIN meta.source src ON src.source_id = s.source_id
         WHERE v.value IS NOT NULL
         ORDER BY s.code, v.ts, src.priority, src.source_id""")
    op.execute("UPDATE meta.source SET priority = 10 WHERE code = 'entsoe'")
    op.execute("UPDATE meta.source SET priority = 20 WHERE code = 'smard'")
    op.execute("ALTER TABLE meta.source DROP COLUMN enabled")
    op.execute("ALTER TABLE meta.source DROP COLUMN free")
