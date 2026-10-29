"""GEFS-Gold gegen die laufende Datenbank: Kennzahlen je Region nach ``feat.nwp_value``.

Läuft nur mit ``PDB_INTEGRATION=1`` nach ``migrate``. Silber liegt im Speicher, Läufe im Jahr
2099 mit Regionen ``gefs:ZZ…`` werden angelegt und danach wieder entfernt.
"""

import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import numpy as np
import pytest
from obstore.store import MemoryStore
from sqlalchemy import Engine, text

from predictiondb_core.db import create_db_engine
from predictiondb_core.settings import Settings
from wx_exchange.gefs import VARIABLES, pack
from wx_exchange.gefs_vm import GOLD_VERSION, SOURCE, GefsGold, GefsLocal, LocalInfo
from wx_exchange.oci import digest
from wx_process.store import Store

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.environ.get("PDB_INTEGRATION") != "1", reason="PDB_INTEGRATION != 1"),
]

RUN = datetime(2099, 3, 1, tzinfo=UTC)
REGIONS = ["ZZ:wind_on", "ZZ:area"]
VERSION = "vtest"


def _cleanup(eng: Engine) -> None:
    with eng.begin() as c:
        c.execute(
            text("""DELETE FROM feat.nwp_value WHERE region_id IN
                    (SELECT region_id FROM meta.region WHERE code LIKE 'gefs:ZZ:%')""")
        )
        c.execute(text("DELETE FROM meta.region WHERE code LIKE 'gefs:ZZ:%'"))
        c.execute(
            text("DELETE FROM meta.nwp_feature_run WHERE clustering_version = :v"), {"v": VERSION}
        )
        c.execute(text("DELETE FROM meta.nwp_run WHERE run_time >= '2099-01-01'"))


@pytest.fixture
def engine() -> Iterator[Engine]:
    eng = create_db_engine(Settings())
    _cleanup(eng)
    yield eng
    _cleanup(eng)
    eng.dispose()


def test_gold_writes_ensemble_stats(engine: Engine) -> None:
    steps = [0, 6, 12]
    members = ["gec00", *(f"gep{i:02d}" for i in range(1, 31))]
    values = np.zeros((31, 3, 2, len(VARIABLES)), dtype=np.float32)
    values[:] = np.arange(31, dtype=np.float32)[:, None, None, None]  # Mitglied m hat Wert m
    ghi = list(VARIABLES).index("ghi")
    values[:, 0, :, ghi] = np.nan  # f000 ohne Mittel
    meta = {
        "run": RUN.isoformat(),
        "members": members,
        "steps": steps,
        "regions": REGIONS,
        "regions_version": VERSION,
        "variables": VARIABLES,
        "missing": 0,
    }
    data, content = pack(meta, values)
    local = GefsLocal(MemoryStore())
    local.save(RUN, data, LocalInfo(digest(data), content, "fp", "ghcr"))

    gold = GefsGold(Store(engine), local)
    n = gold.process(RUN)
    # 3 Schritte x 2 Regionen x 7 Größen x 4 Kennzahlen, ohne ghi in f000 (beide Regionen)
    assert n == (3 * 2 * len(VARIABLES) - 2) * 4
    assert RUN in gold.done(VERSION)
    with engine.connect() as c:
        rows = c.execute(
            text("""
            SELECT f.code, f.aggregation, r.code AS region, n.valid_time, n.value
              FROM feat.nwp_value n
              JOIN meta.feature f USING (feature_id)
              JOIN meta.region r USING (region_id)
              JOIN meta.source s ON s.source_id = f.source_id
             WHERE s.code = :s AND n.run_time = :r AND r.code = 'gefs:ZZ:area'
               AND n.valid_time = :v"""),
            {"s": SOURCE, "r": RUN, "v": RUN + timedelta(hours=6)},
        ).mappings()
        got = {r["code"]: r["value"] for r in rows}
        state = c.execute(
            text("""SELECT features_version FROM meta.nwp_feature_run
                    WHERE run_time = :r AND clustering_version = :v"""),
            {"r": RUN, "v": VERSION},
        ).scalar_one()
    assert state == GOLD_VERSION
    assert got["t2m_mean"] == pytest.approx(15.0)
    assert got["t2m_p10"] == pytest.approx(3.0)
    assert got["t2m_p90"] == pytest.approx(27.0)
    assert got["t2m_std"] == pytest.approx(np.arange(31).std(), rel=1e-5)

    # zweiter Lauf ersetzt die Werte (kein doppelter Schlüssel)
    assert gold.process(RUN) == n
