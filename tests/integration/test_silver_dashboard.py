"""Silber-Bestand je Lauf (``meta.silver_run``) und die Monatsbalken für Silber und Gold.

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
CLUSTERING = "it_gold"


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
        c.execute(
            text("DELETE FROM meta.nwp_feature_run WHERE run_time >= :a AND run_time < :b"),
            {"a": APRIL, "b": MAY},
        )
        c.execute(
            text("DELETE FROM assets.grid_weight WHERE clustering_version = :v"), {"v": CLUSTERING}
        )
        c.execute(
            text("DELETE FROM assets.clustering WHERE clustering_version = :v"), {"v": CLUSTERING}
        )


def _panel_sql(hours: str, panel_id: int = 46) -> str:
    dash = json.loads(DASHBOARD.read_text(encoding="utf-8"))
    panel = next(p for p in dash["panels"] if p["id"] == panel_id)
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


def test_gold_month_bars(engine: Engine) -> None:
    """Gold: grün mit aktueller Clusterung und Feature-Version, gelb mit Silber ohne Gold."""
    store = Store(engine)
    sid = store.source_id("gfs_0p25")
    runs = _runs(0)
    store.mark_silver(sid, runs[:20], "present", "ghcr")
    future = datetime(2099, 1, 1, tzinfo=UTC)
    with engine.begin() as c:
        # jüngste Clusterung mit Gitter-Gewichten
        c.execute(
            text("""INSERT INTO assets.clustering (clustering_version, grid, grid_res, created_at)
                    VALUES (:v, '0p25', 0.25, :t)"""),
            {"v": CLUSTERING, "t": future},
        )
        c.execute(
            text("INSERT INTO assets.cluster VALUES (:v, 'wind_on', 0, 53.5, 9.5, NULL)"),
            {"v": CLUSTERING},
        )
        c.execute(
            text("""INSERT INTO assets.grid_weight VALUES
                    (:v, 'wind_on', 0, 'gfs_0p25', 53.5, 9.5, 10, 1, 0)"""),
            {"v": CLUSTERING},
        )
        for i, r in enumerate(runs[:12]):
            # 10 mit aktueller Clusterung, 1 mit alter, 1 mit alter Feature-Version
            version = CLUSTERING if i < 11 else "it_alt"
            fv = "it_fv" if i != 10 else "it_fv_alt"
            c.execute(
                text("""INSERT INTO meta.nwp_feature_run (source_id, run_time, clustering_version,
                          features_version, silver_key, n_values, processed_at)
                        VALUES (:s, :r, :v, :fv, 'test', 0, :t)"""),
                {"s": sid, "r": r, "v": version, "fv": fv, "t": future + timedelta(minutes=i)},
            )
        # zuletzt verwendete Feature-Version ist it_fv
        c.execute(
            text("""UPDATE meta.nwp_feature_run SET processed_at = :t
                    WHERE run_time = :r AND clustering_version = :v"""),
            {"t": future + timedelta(days=1), "r": runs[0], "v": CLUSTERING},
        )

    with engine.connect() as c:
        rows = {r[0]: r[1:] for r in c.execute(text(_panel_sql("'0'", 47))).all()}
    assert tuple(float(v) for v in rows["2021-04"]) == (round(1000 / 30, 1), round(1000 / 30, 1))
