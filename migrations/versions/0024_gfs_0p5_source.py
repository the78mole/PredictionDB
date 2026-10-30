"""Quelle GFS 0,5° Verbundnetz (``gfs_0p5``) für Bronze, Silber und Gold

Revision ID: 0024_gfs_0p5_source
Revises: 0023_bronze_run
Create Date: 2026-10-29

Zweiter GFS-Datensatz neben ``gfs_0p25`` (DE, 0,25°): das 0,5°-Produkt von NCEP für das ganze
Verbundnetz, dasselbe Gitter wie GEFS. Läufe, Silber-Bestand und Gold hängen an dieser Quelle.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0024_gfs_0p5_source"
down_revision: str | None = "0023_bronze_run"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
        INSERT INTO meta.source (code, kind, license)
        VALUES ('gfs_0p5', 'nwp', 'NOAA/NCEP GFS, gemeinfrei (U.S. Government Work)')
        ON CONFLICT (code) DO NOTHING""")


def downgrade() -> None:
    op.execute("""
        DELETE FROM feat.nwp_value WHERE feature_id IN (
          SELECT feature_id FROM meta.feature f JOIN meta.source s USING (source_id)
          WHERE s.code = 'gfs_0p5')""")
    for table in (
        "meta.feature",
        "meta.nwp_feature_run",
        "meta.silver_run",
        "meta.bronze_run",
        "meta.nwp_run",
    ):
        op.execute(f"""
            DELETE FROM {table}
            WHERE source_id = (SELECT source_id FROM meta.source WHERE code = 'gfs_0p5')""")
    op.execute("DELETE FROM meta.source WHERE code = 'gfs_0p5'")
