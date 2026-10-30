"""Speicherbelegung je Wettergruppe in Garage und PostgreSQL (``meta.storage_usage``)

Revision ID: 0026_storage_usage
Revises: 0025_slp_h0
Create Date: 2026-10-30

Grafana sieht Garage nicht. ``wx-process storage`` misst daher regelmäßig Größe und Zahl der
Objekte je Gruppe in ``wx-bronze`` und ``wx-silver`` sowie den Anteil der Gruppe an
``feat.nwp_value`` und legt je Messung eine Zeile je Ablage (``bronze``, ``silver``, ``postgres``)
und Gruppe ab. ``grp`` ist der Code der Quelle (``gfs_0p25``, ``gfs_0p5``, ``gefs_0p5``) oder
``sonstige`` für Objekte außerhalb dieser Präfixe.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0026_storage_usage"
down_revision: str | None = "0025_slp_h0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE meta.storage_usage (
          measured_at timestamptz NOT NULL,
          store       text NOT NULL CHECK (store IN ('bronze', 'silver', 'postgres')),
          grp         text NOT NULL,
          bytes       bigint NOT NULL,
          objects     bigint NOT NULL,
          PRIMARY KEY (measured_at, store, grp)
        )""")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS meta.storage_usage")
