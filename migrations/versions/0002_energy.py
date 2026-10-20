"""Energiedaten: Quellen, Abrufprotokoll, bevorzugte Werte, Kompression

Revision ID: 0002_energy
Revises: 0001
Create Date: 2026-10-20
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0002_energy"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Rangfolge bei mehreren Quellen für dieselbe Reihe (kleiner = bevorzugt)
    op.execute("ALTER TABLE meta.source ADD COLUMN priority smallint NOT NULL DEFAULT 100")
    op.execute("""
        INSERT INTO meta.source (code, kind, license, priority) VALUES
          ('entsoe', 'energy',
           'ENTSO-E Transparency Platform, Nutzungsbedingungen von ENTSO-E', 10),
          ('smard', 'energy', 'CC BY 4.0, Bundesnetzagentur | SMARD.de', 20),
          ('energy_charts', 'energy', 'CC BY 4.0, Energy-Charts.info (Fraunhofer ISE)', 30)
        ON CONFLICT (code) DO NOTHING""")

    # Welche Zeitblöcke je Quelle und Datensatz sind vollständig geladen (Backfill)
    op.execute("""
        CREATE TABLE meta.energy_fetch (
          source_id    smallint NOT NULL REFERENCES meta.source,
          dataset      text NOT NULL,
          chunk_start  timestamptz NOT NULL,
          chunk_end    timestamptz NOT NULL,
          n_values     int NOT NULL,
          fetched_at   timestamptz NOT NULL DEFAULT now(),
          PRIMARY KEY (source_id, dataset, chunk_start)
        )""")

    # Je Reihen-Code und Zeitpunkt der Wert der bevorzugten Quelle
    op.execute("""
        CREATE VIEW energy.value_best AS
        SELECT DISTINCT ON (s.code, v.ts)
               s.code, v.ts, v.value, v.published, src.code AS source, s.unit
          FROM energy.value v
          JOIN energy.series s USING (series_id)
          JOIN meta.source src ON src.source_id = s.source_id
         WHERE v.value IS NOT NULL
         ORDER BY s.code, v.ts, src.priority, src.source_id""")

    op.execute("""
        ALTER TABLE energy.value SET (
          timescaledb.compress,
          timescaledb.compress_segmentby = 'series_id',
          timescaledb.compress_orderby   = 'ts'
        )""")
    op.execute("SELECT add_compression_policy('energy.value', INTERVAL '30 days')")


def downgrade() -> None:
    op.execute("SELECT remove_compression_policy('energy.value', if_exists => true)")
    op.execute("SELECT decompress_chunk(c, true) FROM show_chunks('energy.value') c")
    op.execute("ALTER TABLE energy.value SET (timescaledb.compress = false)")
    op.execute("DROP VIEW IF EXISTS energy.value_best")
    op.execute("DROP TABLE IF EXISTS meta.energy_fetch")
    op.execute("ALTER TABLE meta.source DROP COLUMN priority")
