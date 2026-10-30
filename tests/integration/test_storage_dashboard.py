"""Speicherbelegung je Gruppe (``meta.storage_usage``) und das Panel im Wetter-Dashboard.

Läuft nur mit ``PDB_INTEGRATION=1`` nach ``migrate``. Misst den Anteil je Quelle an
``feat.nwp_value``, legt zusammen mit Bucket-Werten eine Messung weit in der Zukunft ab (damit sie
die jüngste ist) und prüft die Zeilen des Panels; die Testmessung wird wieder entfernt.
"""

import json
import os
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import Engine, text

from predictiondb_core.db import create_db_engine
from predictiondb_core.settings import Settings
from wx_process.storage import Usage, measure_postgres, record

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.environ.get("PDB_INTEGRATION") != "1", reason="PDB_INTEGRATION != 1"),
]

DASHBOARD = Path(__file__).parents[2] / "config/grafana/dashboards/wetter.json"
AT = datetime(2099, 1, 1, tzinfo=UTC)


@pytest.fixture
def engine() -> Iterator[Engine]:
    eng = create_db_engine(Settings())
    yield eng
    with eng.begin() as c:
        c.execute(text("DELETE FROM meta.storage_usage WHERE measured_at = :at"), {"at": AT})


def test_postgres_share_and_panel(engine: Engine) -> None:
    """@relation(SWR-WXP-050, scope=function)"""
    pg = measure_postgres(engine)
    assert all(u.store == "postgres" and u.bytes >= 0 and u.objects >= 0 for u in pg)
    rows = [
        *pg,
        Usage("bronze", "gfs_0p25", 1000, 10),
        Usage("bronze", "gefs_0p5", 300, 3),
        Usage("silver", "gfs_0p25", 5000, 50),
        Usage("silver", "gfs_0p5", 700, 7),
        Usage("silver", "sonstige", 1, 1),
    ]
    record(engine, rows, AT)

    dash = json.loads(DASHBOARD.read_text(encoding="utf-8"))
    sql = next(p for p in dash["panels"] if p["id"] == 57)["targets"][0]["rawSql"]
    with engine.connect() as c:
        out = {r[0]: r for r in c.execute(text(sql)).all()}
    pg_bytes = {u.grp: u.bytes for u in pg}
    de = out["GFS DE (gfs/0p25)"]
    assert de[1:3] == (1000, 5000) and de[4] == 6000 + pg_bytes.get("gfs_0p25", 0)
    assert out["GEFS (gefs/0p5)"][1] == 300 and out["GEFS (gefs/0p5)"][2] is None
    assert out["sonstige"][2] == 1
    assert out["Summe"][4] == sum(u.bytes for u in rows)
    assert list(out)[-1] == "Summe" and next(iter(out)) == "GFS DE (gfs/0p25)"
