"""Wetter-Features: Quelle GFS, Verarbeitungsstand je Lauf, Kompression feat.nwp_value

Revision ID: 0003_wx_features
Revises: 0002_assets
Create Date: 2026-10-21
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0003_wx_features"
down_revision: str | None = "0002_assets"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
        INSERT INTO meta.source (code, kind, license)
        VALUES ('gfs_0p25', 'nwp', 'NOAA/NCEP GFS, gemeinfrei (U.S. Government Work)')
        ON CONFLICT (code) DO NOTHING""")

    # Welche Läufe sind für welche Clusterung und Feature-Version nach feat.nwp_value gerechnet;
    # eine neue Clusterung oder Feature-Version macht die Läufe erneut fällig
    op.execute("""
        CREATE TABLE meta.nwp_feature_run (
          source_id          smallint NOT NULL REFERENCES meta.source,
          run_time           timestamptz NOT NULL,
          clustering_version text NOT NULL,
          features_version   text NOT NULL,
          silver_key         text NOT NULL,
          n_values           int NOT NULL,
          stats              jsonb,
          processed_at       timestamptz NOT NULL DEFAULT now(),
          PRIMARY KEY (source_id, run_time, clustering_version)
        )""")
    op.execute("CREATE INDEX nwp_feature_run_time ON meta.nwp_feature_run (source_id, run_time)")

    # Chunks mit Gültigkeitszeit älter als 30 Tage komprimieren (nachträgliche Läufe aus dem
    # Backfill landen in komprimierten Chunks; TimescaleDB erlaubt dort Schreiben und Löschen)
    op.execute("SELECT add_compression_policy('feat.nwp_value', interval '30 days')")


def downgrade() -> None:
    op.execute("SELECT remove_compression_policy('feat.nwp_value', if_exists => true)")
    op.execute("DROP TABLE IF EXISTS meta.nwp_feature_run")
    op.execute("""
        DELETE FROM feat.nwp_value WHERE feature_id IN (
          SELECT feature_id FROM meta.feature f JOIN meta.source s USING (source_id)
          WHERE s.code = 'gfs_0p25')""")
    op.execute("""
        DELETE FROM meta.feature
        WHERE source_id = (SELECT source_id FROM meta.source WHERE code = 'gfs_0p25')""")
    op.execute("""
        DELETE FROM meta.nwp_run
        WHERE source_id = (SELECT source_id FROM meta.source WHERE code = 'gfs_0p25')""")
    op.execute("DELETE FROM meta.source WHERE code = 'gfs_0p25'")
