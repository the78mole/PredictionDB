"""Stand des Revisionslaufs je Quelle und Datensatz (``meta.energy_revise``)

Revision ID: 0017_energy_revise
Revises: 0016_unit_regelzone
Create Date: 2026-10-26

``energy-ingest revise`` läuft als eigener Dienst und merkt sich hier, wann ein Datensatz
zuletzt vollständig erneut abgefragt wurde. Ein Neustart (Auto-Update) setzt dort fort, statt
von vorn zu beginnen oder den Lauf zu verlieren.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0017_energy_revise"
down_revision: str | None = "0016_unit_regelzone"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE meta.energy_revise (
          source_id   smallint NOT NULL REFERENCES meta.source,
          dataset     text NOT NULL,
          revised_at  timestamptz NOT NULL,
          PRIMARY KEY (source_id, dataset)
        )""")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS meta.energy_revise")
