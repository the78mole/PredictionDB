"""Schema assets: MaStR-Snapshots, Einheiten, Gebietscluster (docs/ARCHITEKTUR.md 7.5)

Revision ID: 0002_assets
Revises: 0001
Create Date: 2026-10-20
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0002_assets"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS assets")
    op.execute("""
        INSERT INTO meta.source (code, kind, license)
        VALUES ('mastr', 'static', 'DL-DE BY 2.0')
        ON CONFLICT (code) DO NOTHING""")

    # Ein Gesamtdatenexport je Zeile; das ZIP liegt unverändert in assets-bronze
    op.execute("""
        CREATE TABLE assets.snapshot (
          snapshot_id  serial PRIMARY KEY,
          export_date  date UNIQUE NOT NULL,
          version      text NOT NULL,
          object_key   text NOT NULL,
          sha256       text,
          size_bytes   bigint,
          status       text NOT NULL CHECK (status IN ('stored', 'imported', 'pruned', 'failed')),
          n_units      int,
          stats        jsonb,
          updated_at   timestamptz NOT NULL DEFAULT now()
        )""")
    # Reduzierte, normalisierte Sicht auf die Einheiten je Snapshot
    op.execute("""
        CREATE TABLE assets.unit (
          snapshot_id      int NOT NULL REFERENCES assets.snapshot ON DELETE CASCADE,
          mastr_nr         text NOT NULL,
          technology       text NOT NULL,
          status           text CHECK (status IN ('active', 'paused', 'decommissioned', 'planned')),
          capacity_kw      real NOT NULL,
          net_capacity_kw  real,
          commissioned     date,
          decommissioned   date,
          paused_from      date,
          paused_until     date,
          lat              real,
          lon              real,
          geo_precision    text CHECK (geo_precision IN
                             ('coord', 'gemeinde', 'plz', 'kreis', 'land', 'seegebiet')),
          ags              text,
          plz              text,
          bundesland       text,
          sea_area         text,
          hub_height_m     real,
          rotor_diameter_m real,
          azimuth          text,
          tilt             text,
          PRIMARY KEY (snapshot_id, mastr_nr)
        )""")
    op.execute("CREATE INDEX unit_snapshot_technology ON assets.unit (snapshot_id, technology)")
    # Optionale Referenzpunkte je Gemeinde (z. B. Schwerpunkte aus BKG VG250); sonst werden
    # Gemeinde-Schwerpunkte aus den Einheiten mit Koordinaten geschätzt
    op.execute("""
        CREATE TABLE assets.gemeinde (
          ags      text PRIMARY KEY,
          name     text,
          lat      real NOT NULL,
          lon      real NOT NULL,
          source   text
        )""")

    # Eine Clusterung ist versioniert und bleibt fest; Modelle merken sich die Version
    op.execute("""
        CREATE TABLE assets.clustering (
          clustering_version text PRIMARY KEY,
          snapshot_id        int REFERENCES assets.snapshot ON DELETE SET NULL,
          grid               text NOT NULL,
          grid_res           real NOT NULL,
          params             jsonb,
          created_at         timestamptz NOT NULL DEFAULT now()
        )""")
    op.execute("""
        CREATE TABLE assets.cluster (
          clustering_version text NOT NULL REFERENCES assets.clustering ON DELETE CASCADE,
          technology         text NOT NULL,
          cluster_no         int NOT NULL,
          centroid_lat       real NOT NULL,
          centroid_lon       real NOT NULL,
          region_id          smallint REFERENCES meta.region,
          PRIMARY KEY (clustering_version, technology, cluster_no)
        )""")
    # Zugehörigkeit der Gitterzellen; neue Zellen kommen über das nächstgelegene Zentrum dazu
    op.execute("""
        CREATE TABLE assets.cluster_cell (
          clustering_version text NOT NULL,
          technology         text NOT NULL,
          lat                real NOT NULL,
          lon                real NOT NULL,
          cluster_no         int NOT NULL,
          PRIMARY KEY (clustering_version, technology, lat, lon),
          FOREIGN KEY (clustering_version, technology, cluster_no)
            REFERENCES assets.cluster ON DELETE CASCADE
        )""")
    # Leistung je Zelle eines Wettermodellgitters und Cluster, Stand des jüngsten Snapshots;
    # share = Anteil an der Clusterleistung (Summe je Cluster = 1)
    op.execute("""
        CREATE TABLE assets.grid_weight (
          clustering_version text NOT NULL,
          technology         text NOT NULL,
          cluster_no         int NOT NULL,
          grid               text NOT NULL,
          lat                real NOT NULL,
          lon                real NOT NULL,
          capacity_mw        real NOT NULL,
          share              real NOT NULL,
          snapshot_id        int NOT NULL,
          PRIMARY KEY (clustering_version, technology, grid, cluster_no, lat, lon),
          FOREIGN KEY (clustering_version, technology, cluster_no)
            REFERENCES assets.cluster ON DELETE CASCADE
        )""")
    # Rekonstruierte installierte Leistung je Cluster und Tag
    op.execute("""
        CREATE TABLE assets.capacity_daily (
          clustering_version text NOT NULL,
          technology         text NOT NULL,
          cluster_no         int NOT NULL,
          day                date NOT NULL,
          capacity_mw        real NOT NULL,
          snapshot_id        int NOT NULL,
          PRIMARY KEY (clustering_version, technology, cluster_no, day)
        )""")
    op.execute(
        "SELECT create_hypertable('assets.capacity_daily', 'day', "
        "chunk_time_interval => interval '1 year')"
    )


def downgrade() -> None:
    op.execute("DROP SCHEMA IF EXISTS assets CASCADE")
    # \\: schützt den Doppelpunkt vor der Parameter-Erkennung von SQLAlchemy
    op.execute(r"DELETE FROM meta.region WHERE code ~ '^[a-z_]+\:v[0-9]{4}Q[1-4][a-z]?\:c[0-9]+$'")
    op.execute("DELETE FROM meta.source WHERE code = 'mastr'")
