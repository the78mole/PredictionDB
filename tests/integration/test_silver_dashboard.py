"""Silber-Bestand je Lauf (``meta.silver_run``) und das Monatsbalken-Panel im Status-Dashboard.

Läuft nur mit ``PDB_INTEGRATION=1`` nach ``migrate``. Meldet Läufe im April 2021 über
``wx_process.store.Store`` wie ``wx-process`` und ``wx-import``, legt einen laufenden
Bronze-Download an und prüft die Anteile je Monat; die Testdaten werden wieder entfernt.
"""

import json
import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import Engine, text

from predictiondb_core.db import create_db_engine
from predictiondb_core.settings import Settings
from wx_process.store import Store

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.environ.get("PDB_INTEGRATION") != "1", reason="PDB_INTEGRATION != 1"),
]

DASHBOARD = Path(__file__).parents[2] / "config/grafana/dashboards/status.json"
APRIL = datetime(2021, 4, 1, tzinfo=UTC)
MAY = datetime(2021, 5, 1, tzinfo=UTC)
HOST = "it-silver"


def _runs(hour: int) -> list[datetime]:
    return [APRIL + timedelta(days=d, hours=hour) for d in range(30)]


@pytest.fixture
def engine() -> Iterator[Engine]:
    eng = create_db_engine(Settings())
    yield eng
    with eng.begin() as c:
        c.execute(
            text("DELETE FROM meta.silver_run WHERE run_time >= :a AND run_time < :b"),
            {"a": APRIL, "b": MAY},
        )
        c.execute(text("DELETE FROM meta.backfill_job WHERE host = :h"), {"h": HOST})


def _panel_sql(hours: str) -> str:
    dash = json.loads(DASHBOARD.read_text(encoding="utf-8"))
    panel = next(p for p in dash["panels"] if p["id"] == 46)
    sql: str = panel["targets"][0]["rawSql"]
    return sql.replace("$gfs_lauf", hours)


def test_store_and_month_bars(engine: Engine) -> None:
    store = Store(engine)
    sid = store.source_id("gfs_0p25")
    store.mark_silver(sid, _runs(0), "present", "bronze")  # 00Z komplett
    store.mark_silver(sid, _runs(12)[:10], "loading", "ghcr")  # 12Z: 10 angekündigt
    store.mark_silver(sid, _runs(12)[:5], "present", "ghcr")  # davon 5 schon geholt
    with engine.begin() as c:
        job = c.execute(
            text("""INSERT INTO meta.backfill_job (source_id, range_start, range_end, run_hours,
                                                   newest_first, n_runs, host)
                    VALUES (:s, :a, :b, '{12}', true, 1, :h) RETURNING job_id"""),
            {"s": sid, "a": APRIL, "b": MAY, "h": HOST},
        ).scalar_one()
        c.execute(
            text("""INSERT INTO meta.backfill_run (job_id, run_time, queue_pos, status)
                    VALUES (:j, :r, 0, 'running')"""),
            {"j": job, "r": _runs(12)[20]},
        )

    runs = store.silver_runs(sid)
    assert sum(1 for r, s in runs.items() if APRIL <= r < MAY and s == "loading") == 5

    def april(hours: str) -> tuple[float, float]:
        with engine.connect() as c:
            rows = {r[0]: r[1:] for r in c.execute(text(_panel_sql(hours))).all()}
        assert "2021-03" in rows and min(rows) == "2021-03"
        return tuple(float(v) for v in rows["2021-04"])  # type: ignore[return-value]

    # 00Z: alles fertig
    assert april("'0'") == (100.0, 0.0)
    # 12Z: 5 fertig, 5 im Import, 1 im Bronze-Download von 30
    assert april("'12'") == (round(500 / 30, 1), round(600 / 30, 1))
    # alle vier Laufzeiten: 35 fertig, 6 laden von 120
    assert april("'0','6','12','18'") == (round(3500 / 120, 1), 5.0)

    # neuer Import-Durchgang verwirft die Ankündigungen, Fertiges bleibt
    store.clear_silver_loading(sid, _runs(12)[5:7])
    assert april("'12'")[1] == round(400 / 30, 1)
    store.clear_silver_loading(sid)
    assert april("'12'") == (round(500 / 30, 1), round(100 / 30, 1))
