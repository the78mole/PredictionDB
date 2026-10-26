"""Regelzone (ÜNB) und Netzbetreiber je MaStR-Einheit, Leistung je Regelzone und Zelle

Revision ID: 0016_unit_regelzone
Revises: 0015_value_revision
Create Date: 2026-10-26

Einheiten verweisen über ihre Lokation auf Netzanschlusspunkte, die im MaStR die Regelzone des
Übertragungsnetzbetreibers und den Netzbetreiber tragen. ``regelzone_source`` sagt, ob die
Zuordnung direkt aus dem Netzanschlusspunkt stammt oder (ohne Netzanschlusspunkt) aus der
Mehrheit der Einheiten derselben Gemeinde, PLZ, desselben Kreises oder Seegebiets übernommen
wurde. ``assets.zone_weight`` hält die Leistung je Cluster, Regelzone und Gitterzelle, damit
Grafana Prognosen je ÜNB aufteilen und die Regelzonen als Karte zeigen kann.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0016_unit_regelzone"
down_revision: str | None = "0015_value_revision"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE assets.unit
          ADD COLUMN lokation_nr      text,
          ADD COLUMN regelzone        text
            CHECK (regelzone IN ('50hertz', 'amprion', 'tennet', 'transnetbw')),
          ADD COLUMN netzbetreiber_nr text,
          ADD COLUMN regelzone_source text
            CHECK (regelzone_source IN ('nap', 'gemeinde', 'plz', 'kreis', 'seegebiet', 'land'))
        """)
    # Leistung (Stand Exportdatum) je Cluster, Regelzone und Zelle des Clustergitters;
    # regelzone 'unbekannt', wenn sich keine Zuordnung finden ließ
    op.execute("""
        CREATE TABLE assets.zone_weight (
          clustering_version text NOT NULL,
          technology         text NOT NULL,
          cluster_no         int NOT NULL,
          regelzone          text NOT NULL,
          lat                real NOT NULL,
          lon                real NOT NULL,
          capacity_mw        real NOT NULL,
          snapshot_id        int NOT NULL,
          PRIMARY KEY (clustering_version, technology, cluster_no, regelzone, lat, lon),
          FOREIGN KEY (clustering_version, technology, cluster_no)
            REFERENCES assets.cluster ON DELETE CASCADE
        )""")


def downgrade() -> None:
    op.execute("DROP TABLE assets.zone_weight")
    op.execute("""
        ALTER TABLE assets.unit
          DROP COLUMN regelzone_source, DROP COLUMN netzbetreiber_nr,
          DROP COLUMN regelzone, DROP COLUMN lokation_nr""")
