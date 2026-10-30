"""Speicherbelegung je Wettergruppe (``pdb-storage-usage``) und die Panels im Wetter-Dashboard.

Läuft nur mit ``PDB_INTEGRATION=1`` nach ``garage-init`` und ``migrate``. Legt Objekte unter den
Gruppenpräfixen in wx-silver/wx-bronze und Gold-Zeilen in einem eigenen Chunk (2019) an, misst,
prüft Zwischenspeicher, erneutes Listing nach einer Änderung in ``meta.silver_run`` und die
Abfragen der Panels 80 bis 82; die Testdaten werden wieder entfernt.
"""

import json
import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import obstore
import pytest
from sqlalchemy import Engine, text

from predictiondb_core.db import create_db_engine
from predictiondb_core.s3 import s3_store
from predictiondb_core.settings import Settings
from predictiondb_core.storage_usage import Usage, measure

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.environ.get("PDB_INTEGRATION") != "1", reason="PDB_INTEGRATION != 1"),
]

DASHBOARD = Path(__file__).parents[2] / "config/grafana/dashboards/wetter.json"
RUN = datetime(2021, 4, 1, tzinfo=UTC)
OBJECTS = {
    ("wx-silver", "gfs/0p25/2021/04/01/00.zarr/a"): 100,
    ("wx-silver", "gfs/0p25/2021/04/01/00.zarr/b"): 50,
    ("wx-silver", "gfs/0p5/2021/04/01/00.zarr/a"): 30,
    ("wx-bronze", "gefs/0p5/2021/04/01/00/m01.f000.grib2"): 70,
}
GOLD_START = datetime(2019, 1, 1, tzinfo=UTC)
CODE = "it_storage"


@pytest.fixture
def engine() -> Iterator[Engine]:
    settings = Settings()
    eng = create_db_engine(settings)
    with eng.begin() as c:
        for t in ("meta.storage_usage", "meta.storage_s3_month", "meta.storage_pg_chunk"):
            c.execute(text(f"DELETE FROM {t}"))
    for (bucket, key), size in OBJECTS.items():
        obstore.put(s3_store(bucket, settings), key, b"x" * size)
    yield eng
    for bucket, key in OBJECTS:
        obstore.delete(s3_store(bucket, settings), key)
    with eng.begin() as c:
        c.execute(
            text("""DELETE FROM feat.nwp_value WHERE valid_time >= :a AND valid_time < :b
                     AND feature_id IN (SELECT feature_id FROM meta.feature WHERE code = :c)"""),
            {"a": GOLD_START, "b": GOLD_START + timedelta(days=7), "c": CODE},
        )
        c.execute(text("DELETE FROM meta.feature WHERE code = :c"), {"c": CODE})
        c.execute(text("DELETE FROM meta.region WHERE code = :c"), {"c": CODE})
        c.execute(text("DELETE FROM meta.silver_run WHERE run_time = :r"), {"r": RUN})
        for t in ("meta.storage_usage", "meta.storage_s3_month", "meta.storage_pg_chunk"):
            c.execute(text(f"DELETE FROM {t}"))


def _gold(engine: Engine) -> None:
    """30 Zeilen GFS DE und 10 Zeilen GEFS in einem Chunk der ersten Januarwoche 2019."""
    with engine.begin() as c:
        region = c.execute(
            text("""INSERT INTO meta.region (code) VALUES (:c)
                    ON CONFLICT (code) DO UPDATE SET code = EXCLUDED.code RETURNING region_id"""),
            {"c": CODE},
        ).scalar_one()
        for source, n in (("gfs_0p25", 30), ("gefs_0p5", 10)):
            fid = c.execute(
                text("""INSERT INTO meta.feature (source_id, code, aggregation, unit)
                        SELECT source_id, :c, 'mean', '1' FROM meta.source WHERE code = :s
                        RETURNING feature_id"""),
                {"c": CODE, "s": source},
            ).scalar_one()
            for h in range(n):
                c.execute(
                    text("""INSERT INTO feat.nwp_value
                                   (feature_id, region_id, run_time, valid_time, value)
                            VALUES (:f, :g, :r, :v, 1)"""),
                    {"f": fid, "g": region, "r": GOLD_START, "v": GOLD_START + timedelta(hours=h)},
                )


def _client() -> httpx.Client:
    token = os.environ.get("GARAGE_ADMIN_TOKEN", "")
    return httpx.Client(
        base_url=os.environ.get("PDB_GARAGE_ADMIN_URL", "http://localhost:3903"),
        headers={"Authorization": f"Bearer {token}"},
    )


def _months(engine: Engine) -> dict[tuple[str, str], tuple[int, int, datetime]]:
    with engine.connect() as c:
        return {
            (r.bucket, r.grp): (r.bytes, r.objects, r.scanned_at)
            for r in c.execute(
                text("SELECT * FROM meta.storage_s3_month WHERE month = '2021-04-01'")
            )
        }


def _panel_sql(panel_id: int, ref: str = "A") -> str:
    dash = json.loads(DASHBOARD.read_text(encoding="utf-8"))
    panel = next(p for p in dash["panels"] if p["id"] == panel_id)
    target = next(t for t in panel["targets"] if t["refId"] == ref)
    sql: str = target["rawSql"]
    return sql.replace("$__timeFilter(measured_at)", "true")


def test_measure_groups_and_panels(engine: Engine, tmp_path: Path) -> None:
    _gold(engine)
    (tmp_path / "db.lmdb").mkdir()
    (tmp_path / "db.lmdb" / "data.mdb").write_bytes(b"\0" * 8192)
    usage = Usage(engine, Settings())
    with _client() as client:
        measure(usage, client, tmp_path, 120, timedelta(days=7))

        months = _months(engine)
        assert months[("wx-silver", "gfs-de")][:2] == (150, 2)
        assert months[("wx-silver", "gfs-eu")][:2] == (30, 1)
        assert months[("wx-bronze", "gefs")][:2] == (70, 1)

        with engine.connect() as c:
            shares = c.execute(
                text("""SELECT p.shares FROM meta.storage_pg_chunk p
                          JOIN chunks_detailed_size('feat.nwp_value') d
                            ON p.chunk = format('%I.%I', d.chunk_schema, d.chunk_name)
                          JOIN timescaledb_information.chunks i
                            ON i.chunk_schema = d.chunk_schema AND i.chunk_name = d.chunk_name
                         WHERE i.range_start <= :t AND i.range_end > :t"""),
                {"t": GOLD_START},
            ).scalar_one()
            usage_rows = {
                (r.store, r.part, r.grp): r.bytes
                for r in c.execute(text("SELECT * FROM meta.storage_usage"))
            }
        assert shares["gfs_0p25"][1] == 30
        assert shares["gefs_0p5"][1] == 10
        assert usage_rows[("s3", "Silber", "gfs-de")] >= 150
        assert usage_rows[("s3", "Bronze", "gefs")] >= 70
        assert usage_rows[("postgres", "Gold", "gfs-de")] > 0
        assert usage_rows[("garage", "Metadaten (LMDB)", "sonstige")] >= 8192

        # Ohne Änderung bleibt der alte Monat im Zwischenspeicher, nach einer Meldung in
        # meta.silver_run wird er neu gelistet
        before = _months(engine)[("wx-silver", "gfs-de")][2]
        measure(usage, client, tmp_path, 120, timedelta(days=7))
        assert _months(engine)[("wx-silver", "gfs-de")][2] == before
        with engine.begin() as c:
            c.execute(
                text("""INSERT INTO meta.silver_run (source_id, run_time, status, origin)
                        SELECT source_id, :r, 'present', 'bronze' FROM meta.source
                         WHERE code = 'gfs_0p25'"""),
                {"r": RUN},
            )
        measure(usage, client, tmp_path, 120, timedelta(days=7))
        assert _months(engine)[("wx-silver", "gfs-de")][2] > before

    with engine.connect() as c:
        bars = c.execute(text(_panel_sql(80))).mappings().all()
        table = c.execute(text(_panel_sql(81))).mappings().all()
        series = c.execute(text(_panel_sql(82))).mappings().all()
        total = c.execute(text(_panel_sql(82, "B"))).mappings().all()
    assert [b["Gruppe"].split(" (")[0] for b in bars][:3] == ["GFS DE", "GFS Europa", "GEFS"]
    assert bars[0]["Garage Silber"] >= 150
    assert any(r["Teil"] == "Gold" and r["Gruppe"] == "GEFS" for r in table)
    assert {"GFS DE Garage Silber", "GEFS Postgres Gold"} <= {r["metric"] for r in series}
    assert len(total) == 3
