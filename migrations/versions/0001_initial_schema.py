"""Initiales Schema nach docs/ARCHITEKTUR.md, Kapitel 5.3

Revision ID: 0001
Revises:
Create Date: 2026-10-20
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMAS = ("meta", "feat", "energy", "fcst")


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS timescaledb")
    for schema in SCHEMAS:
        op.execute(f"CREATE SCHEMA IF NOT EXISTS {schema}")

    # meta: was wurde wann woher geladen
    op.execute("""
        CREATE TABLE meta.source (
          source_id    smallserial PRIMARY KEY,
          code         text UNIQUE NOT NULL,
          kind         text NOT NULL CHECK (kind IN ('nwp', 'energy', 'static')),
          license      text
        )""")
    op.execute("""
        CREATE TABLE meta.nwp_run (
          source_id    smallint NOT NULL REFERENCES meta.source,
          run_time     timestamptz NOT NULL,
          status       text NOT NULL CHECK (status IN
                         ('discovered', 'downloading', 'complete', 'processed', 'failed')),
          endpoint     text,
          n_expected   int,
          n_stored     int,
          bytes_stored bigint,
          updated_at   timestamptz NOT NULL DEFAULT now(),
          PRIMARY KEY (source_id, run_time)
        )""")
    op.execute("""
        CREATE TABLE meta.grib_message (
          source_id    smallint NOT NULL,
          run_time     timestamptz NOT NULL,
          step_h       smallint NOT NULL,
          var          text NOT NULL,
          level        text NOT NULL,
          status       text NOT NULL,
          object_key   text,
          sha256       bytea,
          size_bytes   int,
          PRIMARY KEY (source_id, run_time, step_h, var, level),
          FOREIGN KEY (source_id, run_time) REFERENCES meta.nwp_run ON DELETE CASCADE
        )""")
    # Register der Merkmale (PDID-Idee aus SPECIFICATION.md)
    op.execute("""
        CREATE TABLE meta.feature (
          feature_id   serial PRIMARY KEY,
          source_id    smallint REFERENCES meta.source,
          code         text NOT NULL,
          aggregation  text NOT NULL,
          unit         text NOT NULL,
          UNIQUE (source_id, code, aggregation)
        )""")
    op.execute("""
        CREATE TABLE meta.region (
          region_id    smallserial PRIMARY KEY,
          code         text UNIQUE NOT NULL
        )""")

    # feat: Wetter-Features (Gold), Vorlaufzeit = valid_time - run_time
    op.execute("""
        CREATE TABLE feat.nwp_value (
          feature_id   int NOT NULL,
          region_id    smallint NOT NULL,
          run_time     timestamptz NOT NULL,
          valid_time   timestamptz NOT NULL,
          value        real NOT NULL,
          PRIMARY KEY (feature_id, region_id, run_time, valid_time)
        )""")
    op.execute(
        "SELECT create_hypertable('feat.nwp_value', 'valid_time', "
        "chunk_time_interval => interval '7 days')"
    )
    op.execute("""
        ALTER TABLE feat.nwp_value SET (
          timescaledb.compress,
          timescaledb.compress_segmentby = 'feature_id, region_id',
          timescaledb.compress_orderby   = 'run_time, valid_time'
        )""")

    # energy: Ist-Werte und offizielle Prognosen; published dient dem Leakage-Schutz
    op.execute("""
        CREATE TABLE energy.series (
          series_id    serial PRIMARY KEY,
          source_id    smallint REFERENCES meta.source,
          code         text NOT NULL,
          unit         text,
          resolution   interval,
          UNIQUE (source_id, code)
        )""")
    op.execute("""
        CREATE TABLE energy.value (
          series_id    int NOT NULL,
          ts           timestamptz NOT NULL,
          value        double precision,
          published    timestamptz,
          PRIMARY KEY (series_id, ts)
        )""")
    op.execute("SELECT create_hypertable('energy.value', 'ts')")

    # fcst: eigene Prognosen je Modellversion und Quantil
    op.execute("""
        CREATE TABLE fcst.prediction (
          model_version text NOT NULL,
          target        text NOT NULL,
          issue_time    timestamptz NOT NULL,
          valid_time    timestamptz NOT NULL,
          quantile      real NOT NULL,
          value         double precision,
          PRIMARY KEY (model_version, target, issue_time, valid_time, quantile)
        )""")
    op.execute("SELECT create_hypertable('fcst.prediction', 'valid_time')")


def downgrade() -> None:
    for schema in reversed(SCHEMAS):
        op.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
