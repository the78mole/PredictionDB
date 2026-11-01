"""Prüft den laufenden Compose-Stack (Postgres mit Schema, Garage mit Buckets).

Läuft nur mit ``PDB_INTEGRATION=1``, nachdem ``garage-init`` und ``migrate`` gelaufen sind.
"""

import os
import uuid

import obstore
import pytest
from sqlalchemy import text

from predictiondb_core.db import create_db_engine
from predictiondb_core.s3 import s3_store
from predictiondb_core.settings import Settings

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.environ.get("PDB_INTEGRATION") != "1", reason="PDB_INTEGRATION != 1"),
]


def test_schema_and_hypertables() -> None:
    """@relation(SWR-CORE-002, scope=function)"""
    engine = create_db_engine(Settings())
    with engine.connect() as conn:
        version = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
        hypertables = set(
            conn.execute(
                text(
                    "SELECT hypertable_schema || '.' || hypertable_name "
                    "FROM timescaledb_information.hypertables"
                )
            ).scalars()
        )
    assert version
    assert {"feat.nwp_value", "energy.value", "fcst.prediction", "assets.capacity_daily"} <= (
        hypertables
    )


def test_buckets_read_write() -> None:
    """@relation(SWR-CORE-003, scope=function)"""
    settings = Settings()
    for bucket in settings.buckets.all():
        store = s3_store(bucket, settings)
        key = f"_ci/{uuid.uuid4()}.bin"
        obstore.put(store, key, b"predictiondb")
        assert bytes(obstore.get(store, key).bytes()) == b"predictiondb"
        obstore.delete(store, key)
