"""Lokaler Bronze-Bestand je Lauf (``meta.bronze_run``) für das Status-Dashboard

Revision ID: 0023_bronze_run
Revises: 0022_gefs_source
Create Date: 2026-10-29

GEFS-Bronze liegt nur auf der VM, die es selbst lädt (``wx-gefs``), und wird nach der
Aufbewahrungsfrist gelöscht. Grafana sieht nur die Datenbank, daher meldet ``wx-gefs`` je Lauf:
``loading`` = Download läuft, ``present`` = vollständig in ``wx-bronze``, ``deleted`` = nach der
Frist gelöscht (Silber liegt lokal und in GHCR). GFS meldet Bronze weiter über
``meta.backfill_run``.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0023_bronze_run"
down_revision: str | None = "0022_gefs_source"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE meta.bronze_run (
          source_id   smallint NOT NULL REFERENCES meta.source,
          run_time    timestamptz NOT NULL,
          status      text NOT NULL CHECK (status IN ('loading', 'present', 'deleted')),
          bytes       bigint,
          updated_at  timestamptz NOT NULL DEFAULT now(),
          PRIMARY KEY (source_id, run_time)
        )""")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS meta.bronze_run")
