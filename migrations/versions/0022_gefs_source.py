"""Quelle GEFS 0,5° für Silber-Bestand und Gold (``wx-exchange gefs vm``)

Revision ID: 0022_gefs_source
Revises: 0021_metric_lead_all
Create Date: 2026-10-29

Das GEFS-Gold liegt wie das GFS-Gold in ``feat.nwp_value`` (Merkmale ``<größe>_<kennzahl>``,
Aggregation ``ensemble``, Regionen ``gefs:<code>``), der Stand in ``meta.nwp_feature_run`` und
``meta.silver_run``.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0022_gefs_source"
down_revision: str | None = "0021_metric_lead_all"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
        INSERT INTO meta.source (code, kind, license)
        VALUES ('gefs_0p5', 'nwp', 'NOAA/NCEP GEFS v12, gemeinfrei (U.S. Government Work)')
        ON CONFLICT (code) DO NOTHING""")


def downgrade() -> None:
    op.execute("""
        DELETE FROM feat.nwp_value WHERE feature_id IN (
          SELECT feature_id FROM meta.feature f JOIN meta.source s USING (source_id)
          WHERE s.code = 'gefs_0p5')""")
    for table in ("meta.feature", "meta.nwp_feature_run", "meta.silver_run", "meta.nwp_run"):
        op.execute(f"""
            DELETE FROM {table}
            WHERE source_id = (SELECT source_id FROM meta.source WHERE code = 'gefs_0p5')""")
    op.execute("DELETE FROM meta.region WHERE code LIKE 'gefs:%'")
    op.execute("DELETE FROM meta.source WHERE code = 'gefs_0p5'")
