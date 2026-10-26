"""Korrekturprotokoll ``energy.value_revision``

Revision ID: 0015_value_revision
Revises: 0014_freshness_eurostat_fuels
Create Date: 2026-10-26

Jede Änderung eines bereits gespeicherten Werts in ``energy.value`` (Korrektur der Quelle oder
von der Quelle entfernter Wert, dann ``new_value`` NULL) landet hier mit altem und neuem Wert.
Grafana meldet daraus Korrekturen der Ist-Last. Ältere Einträge räumt eine Retention-Policy ab.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0015_value_revision"
down_revision: str | None = "0014_freshness_eurostat_fuels"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE energy.value_revision (
          series_id   int NOT NULL REFERENCES energy.series ON DELETE CASCADE,
          ts          timestamptz NOT NULL,
          old_value   float8,
          new_value   float8,
          revised_at  timestamptz NOT NULL DEFAULT now()
        )""")
    op.execute(
        "SELECT create_hypertable('energy.value_revision', 'revised_at', "
        "chunk_time_interval => INTERVAL '30 days')"
    )
    op.execute("CREATE INDEX ON energy.value_revision (series_id, ts)")
    op.execute("SELECT add_retention_policy('energy.value_revision', INTERVAL '2 years')")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS energy.value_revision")
