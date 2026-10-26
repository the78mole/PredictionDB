"""Lokaler Silber-Bestand je Lauf (``meta.silver_run``) für das Status-Dashboard

Revision ID: 0018_silver_run
Revises: 0017_energy_revise
Create Date: 2026-10-26

Silber liegt als Zarr-Store in Garage ``wx-silver``, Grafana sieht nur die Datenbank. Hier
melden ``wx-process`` (Silber gebaut oder gelesen) und ``wx-import`` (aus GHCR geholt) den
Stand je Lauf: ``present`` = vollständig lokal, ``loading`` = im laufenden Import-Durchgang
vorgesehen. Startwert: alle Läufe, für die schon Features gerechnet sind (dafür lag Silber vor).
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0018_silver_run"
down_revision: str | None = "0017_energy_revise"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE meta.silver_run (
          source_id   smallint NOT NULL REFERENCES meta.source,
          run_time    timestamptz NOT NULL,
          status      text NOT NULL CHECK (status IN ('loading', 'present')),
          origin      text NOT NULL CHECK (origin IN ('bronze', 'ghcr')),
          updated_at  timestamptz NOT NULL DEFAULT now(),
          PRIMARY KEY (source_id, run_time)
        )""")
    op.execute("""
        INSERT INTO meta.silver_run (source_id, run_time, status, origin, updated_at)
        SELECT f.source_id, f.run_time, 'present',
               CASE WHEN bool_or(r.endpoint = 'ghcr') THEN 'ghcr' ELSE 'bronze' END,
               max(f.processed_at)
          FROM meta.nwp_feature_run f
          LEFT JOIN meta.nwp_run r USING (source_id, run_time)
         GROUP BY f.source_id, f.run_time""")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS meta.silver_run")
