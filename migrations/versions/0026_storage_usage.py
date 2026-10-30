"""Speicherbelegung je Wettergruppe in Garage und Postgres für das Wetter-Dashboard

Revision ID: 0026_storage_usage
Revises: 0025_slp_h0
Create Date: 2026-10-30

``pdb-storage-usage`` misst periodisch und schreibt ``meta.storage_usage`` (je Messung, Speicher,
Teil und Gruppe gfs-de/gfs-eu/gefs/sonstige). Grafana liest nur diese Tabelle und listet nie
selbst Buckets. Zwischenspeicher, damit nicht jede Messung alles neu liest:
``meta.storage_s3_month`` (Listing je Bucket, Gruppe und Monatspräfix) und
``meta.storage_pg_chunk`` (Aufteilung je Chunk von ``feat.nwp_value``).
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
          measured_at  timestamptz NOT NULL,
          store        text NOT NULL CHECK (store IN ('s3', 'postgres', 'garage')),
          part         text NOT NULL,
          grp          text NOT NULL,
          bytes        bigint NOT NULL,
          objects      bigint NOT NULL DEFAULT 0,
          PRIMARY KEY (measured_at, store, part, grp)
        )""")
    op.execute("""
        CREATE TABLE meta.storage_s3_month (
          bucket      text NOT NULL,
          grp         text NOT NULL,
          month       date NOT NULL,
          prefix      text NOT NULL,
          bytes       bigint NOT NULL,
          objects     bigint NOT NULL,
          scanned_at  timestamptz NOT NULL DEFAULT now(),
          PRIMARY KEY (bucket, grp, month)
        )""")
    op.execute("""
        CREATE TABLE meta.storage_pg_chunk (
          chunk        text PRIMARY KEY,
          bytes        bigint NOT NULL,
          shares       jsonb NOT NULL,
          measured_at  timestamptz NOT NULL DEFAULT now()
        )""")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS meta.storage_pg_chunk")
    op.execute("DROP TABLE IF EXISTS meta.storage_s3_month")
    op.execute("DROP TABLE IF EXISTS meta.storage_usage")
