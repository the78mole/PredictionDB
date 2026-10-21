"""wx-process gegen die laufende Datenbank: Gewichte lesen, Features schreiben, Zustand führen.

Läuft nur mit ``PDB_INTEGRATION=1`` nach ``migrate``. Bronze und Silber liegen im Speicher.
Clusterung ``v2098Q4`` und Läufe im Jahr 2099 werden angelegt und danach wieder entfernt.
"""

import os
from collections.abc import Iterator
from datetime import date, datetime, timedelta
from typing import Any

import psycopg
import pytest
from obstore.store import MemoryStore
from sqlalchemy import Engine, text

from predictiondb_core.db import create_db_engine
from predictiondb_core.settings import Settings
from wx_ingest.config import GfsConfig
from wx_process.config import ProcessConfig
from wx_process.features import FEATURES_VERSION
from wx_process.pipeline import WxPipeline
from wx_process.silver import Silver
from wx_process.store import Store
from wx_process.testing import RUN, fill_bronze, source_config

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.environ.get("PDB_INTEGRATION") != "1", reason="PDB_INTEGRATION != 1"),
]

VERSION = "v2098Q4"
STEPS = list(range(0, 22, 3))


def _cleanup(eng: Engine) -> None:
    with eng.begin() as c:
        c.execute(
            text("""DELETE FROM feat.nwp_value WHERE region_id IN
                    (SELECT region_id FROM meta.region WHERE code LIKE :p)"""),
            {"p": f"%:{VERSION}:%"},
        )
        c.execute(
            text("DELETE FROM meta.nwp_feature_run WHERE clustering_version = :v"), {"v": VERSION}
        )
        c.execute(text("DELETE FROM meta.nwp_run WHERE run_time >= '2098-01-01'"))
        c.execute(
            text("DELETE FROM assets.capacity_daily WHERE clustering_version = :v"), {"v": VERSION}
        )
        c.execute(
            text("DELETE FROM assets.clustering WHERE clustering_version = :v"), {"v": VERSION}
        )
        c.execute(text("DELETE FROM meta.region WHERE code LIKE :p"), {"p": f"%:{VERSION}:%"})
        c.execute(text("DELETE FROM assets.snapshot WHERE export_date = '2098-10-01'"))


def _setup(eng: Engine) -> dict[str, int]:
    regions: dict[str, int] = {}
    with eng.begin() as c:
        sid = c.execute(
            text("""INSERT INTO assets.snapshot (export_date, version, object_key, status)
                    VALUES ('2098-10-01', 'test', 'test', 'imported') RETURNING snapshot_id""")
        ).scalar_one()
        c.execute(
            text("""INSERT INTO assets.clustering VALUES (:v, :s, '0p25', 0.25, '{}', now())"""),
            {"v": VERSION, "s": sid},
        )
        for tech, lat, lon in (("wind_on", 54.0, 9.0), ("pv_roof", 48.0, 11.0)):
            code = f"{tech}:{VERSION}:c00"
            rid = c.execute(
                text("INSERT INTO meta.region (code) VALUES (:c) RETURNING region_id"), {"c": code}
            ).scalar_one()
            regions[tech] = rid
            c.execute(
                text("INSERT INTO assets.cluster VALUES (:v, :t, 0, :lat, :lon, :r)"),
                {"v": VERSION, "t": tech, "lat": lat, "lon": lon, "r": rid},
            )
            c.execute(
                text("INSERT INTO assets.cluster_cell VALUES (:v, :t, :lat, :lon, 0)"),
                {"v": VERSION, "t": tech, "lat": lat, "lon": lon},
            )
        # Wind: zwei Zellen, PV: eine Zelle außerhalb des Gitters (Abdeckung 0,8)
        weights = [
            ("wind_on", 54.0, 9.0, 30.0, 0.6),
            ("wind_on", 53.0, 8.0, 20.0, 0.4),
            ("pv_roof", 48.0, 11.0, 8.0, 0.8),
            ("pv_roof", 40.0, 11.0, 2.0, 0.2),
        ]
        c.execute(
            text("""INSERT INTO assets.grid_weight
                    VALUES (:v, :t, 0, 'gfs_0p25', :lat, :lon, :mw, :share, :s)"""),
            [
                {"v": VERSION, "t": t, "lat": la, "lon": lo, "mw": mw, "share": sh, "s": sid}
                for t, la, lo, mw, sh in weights
            ],
        )
        c.execute(
            text("""INSERT INTO assets.unit (snapshot_id, mastr_nr, technology, capacity_kw,
                      lat, lon, hub_height_m)
                    VALUES (:s, 'W1', 'wind_on', 3000, 54.02, 9.01, 140)"""),
            {"s": sid},
        )
        c.execute(
            text("""INSERT INTO assets.capacity_daily VALUES
                    (:v, 'wind_on', 0, '2098-12-01', 50, :s),
                    (:v, 'wind_on', 0, '2099-01-03', 60, :s)"""),
            {"v": VERSION, "s": sid},
        )
    return regions


@pytest.fixture
def engine() -> Iterator[Engine]:
    eng = create_db_engine(Settings())
    _cleanup(eng)
    yield eng
    _cleanup(eng)


def _q(eng: Engine, sql: str, **p: Any) -> list[Any]:
    with eng.connect() as c:
        return list(c.execute(text(sql), p).all())


def test_features_written(engine: Engine) -> None:
    regions = _setup(engine)
    config = ProcessConfig.from_yaml("config/process/gfs_0p25.yaml")
    source = GfsConfig.model_validate(source_config([(0, 21, 3)]))
    bronze = MemoryStore()
    fill_bronze(bronze, STEPS)
    store = Store(engine)
    pipeline = WxPipeline(config, source, bronze, Silver(MemoryStore()), store)

    url = Settings().database_url.replace("postgresql+psycopg://", "postgresql://")
    with psycopg.connect(url, autocommit=True) as listener:
        listener.execute("LISTEN features_ready")
        r = pipeline.process(RUN, clustering=VERSION)
        notes = list(listener.notifies(timeout=2, stop_after=1))
    assert r.status == "processed" and r.n_values > 0
    assert r.coverage_min == pytest.approx(0.8)
    assert notes and VERSION in notes[0].payload

    def values(code: str, tech: str) -> dict[datetime, float]:
        rows = _q(
            engine,
            """SELECT v.valid_time, v.value FROM feat.nwp_value v
               JOIN meta.feature f USING (feature_id)
               WHERE f.code = :c AND f.aggregation = 'cluster_cap_weighted'
                 AND v.region_id = :r AND v.run_time = :run""",
            c=code,
            r=regions[tech],
            run=RUN,
        )
        return {t: float(v) for t, v in rows}

    n = _q(engine, "SELECT count(*) FROM feat.nwp_value WHERE run_time = :r", r=RUN)[0][0]
    assert n == r.n_values

    # Bestand: bis 2099-01-02 gilt 50 MW (letzter Tag davor), ab 2099-01-03 60 MW
    cf, power = values("wind_cf", "wind_on"), values("wind_power", "wind_on")
    assert len(power) == len(STEPS)
    t_late = RUN + timedelta(hours=21)
    assert t_late.date() == date(2099, 1, 3)
    assert power[RUN] == pytest.approx(50 * cf[RUN], rel=1e-4)
    assert power[t_late] == pytest.approx(60 * cf[t_late], rel=1e-4)
    # Nabenhöhe 140 m aus dem MaStR liegt über 100 m
    hub, ws100 = values("wind_speed_hub", "wind_on"), values("wind_speed_100m", "wind_on")
    assert all(hub[t] > ws100[t] for t in hub)
    # Strahlung fehlt in f000; PV ohne Bestand hat keine Leistung
    assert len(values("ghi", "pv_roof")) == len(STEPS) - 1
    assert values("pv_power", "pv_roof") == {}

    state = _q(
        engine,
        """SELECT features_version, n_values, silver_key FROM meta.nwp_feature_run
           WHERE clustering_version = :v AND run_time = :r""",
        v=VERSION,
        r=RUN,
    )
    assert state == [(FEATURES_VERSION, r.n_values, "gfs/0p25/2099/01/02/06.zarr")]
    assert _q(engine, "SELECT status, n_stored FROM meta.nwp_run WHERE run_time = :r", r=RUN) == [
        ("processed", len(STEPS))
    ]
    units = dict(
        _q(engine, "SELECT code, unit FROM meta.feature WHERE aggregation = 'cluster_cap_weighted'")
    )
    assert units["wind_power"] == "MW" and units["t2m"] == "degC"

    # erneut: übersprungen; erzwungen: dieselbe Zahl an Werten (idempotent)
    assert pipeline.process(RUN, clustering=VERSION).status == "skipped"
    r2 = pipeline.process(RUN, clustering=VERSION, force=True)
    assert r2.n_values == r.n_values
    n2 = _q(engine, "SELECT count(*) FROM feat.nwp_value WHERE run_time = :r", r=RUN)[0][0]
    assert n2 == n
