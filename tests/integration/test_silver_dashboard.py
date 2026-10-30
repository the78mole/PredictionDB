"""Bestand je Lauf (``meta.silver_run``, ``meta.bronze_run``) und die Monatsbalken für Bronze,
Silber und Gold mit je Monat drei Balken: GFS DE, GFS Europa, GEFS.

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

DASHBOARD = Path(__file__).parents[2] / "config/grafana/dashboards/wetter.json"
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
        for table in ("meta.silver_run", "meta.bronze_run"):
            c.execute(
                text(f"DELETE FROM {table} WHERE run_time >= :a AND run_time < :b"),
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
        assert "2021-03" in rows and min(k for k in rows if k) == "2021-03"
        return tuple(float(v) for v in rows["2021-04"][:2])  # type: ignore[return-value]

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

    # lokal gelöschtes Silber (liegt auf GHCR) zählt weiter als fertig
    store.mark_silver(sid, _runs(12)[:5], "evicted", "ghcr")
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
    gfs = tuple(float(v) for v in rows["2021-04"][:2])
    assert gfs == (round(1000 / 30, 1), round(1000 / 30, 1))

    # lokal löschbar: aus GHCR importiert und Gold in aktueller Clusterung und Feature-Version
    assert store.evictable_silver(sid, future, CLUSTERING, "it_fv") == runs[:10]
    assert store.evictable_silver(sid, runs[5], CLUSTERING, "it_fv") == runs[:5]


def test_bronze_month_bars(engine: Engine) -> None:
    """Bronze: grün nach abgeschlossenem Download oder Silber aus Bronze, gelb im Download."""
    store = Store(engine)
    sid = store.source_id("gfs_0p25")
    runs = _runs(6)
    store.mark_silver(sid, runs[:4], "present", "bronze")  # Silber aus lokalem Bronze
    store.mark_silver(sid, runs[4:8], "present", "ghcr")  # nur importiert, kein Bronze
    with engine.begin() as c:
        job = c.execute(
            text("""INSERT INTO meta.backfill_job (source_id, range_start, range_end, run_hours,
                                                   newest_first, n_runs, host)
                    VALUES (:s, :a, :b, '{6}', true, 1, :h) RETURNING job_id"""),
            {"s": sid, "a": APRIL, "b": MAY, "h": HOST},
        ).scalar_one()
        # 3 geladen (einer davon schon mit Silber), 2 vorgefunden, 1 läuft, 1 unvollständig
        for i, status in [
            (0, "complete"),
            (10, "complete"),
            (11, "complete"),
            (12, "skipped"),
            (13, "skipped"),
            (14, "running"),
            (15, "incomplete"),
        ]:
            c.execute(
                text("""INSERT INTO meta.backfill_run (job_id, run_time, queue_pos, status)
                        VALUES (:j, :r, :p, :st)"""),
                {"j": job, "r": runs[i], "p": i, "st": status},
            )

    with engine.connect() as c:
        rows = {r[0]: r[1:] for r in c.execute(text(_panel_sql("'6'", 52))).all()}
    # fertig: 4 aus Silber (Bronze) + 4 aus Downloads = 8, im Download 1 von 30
    gfs = tuple(float(v) for v in rows["2021-04"][:2])
    assert gfs == (round(800 / 30, 1), round(100 / 30, 1))


def _bar(engine: Engine, panel_id: int, prefix: str, hours: str = "'0'") -> tuple[float, ...]:
    """Werte des Balkens ``prefix`` im April 2021; die anderen Zeilen bleiben dafür leer."""
    with engine.connect() as c:
        result = c.execute(text(_panel_sql(hours, panel_id)))
        cols = list(result.keys())
        rows = result.all()
    # je Monat vier Zeilen: GFS DE (beschriftet), GFS Europa, GEFS, Lücke
    i = next(i for i, r in enumerate(rows) if r[0] == "2021-04")
    assert [r[0] for r in rows[i + 1 : i + 4]] == ["", "", ""]
    assert all(v is None for v in rows[i + 3][1:])
    k = ["GFS DE", "GFS Europa", "GEFS"].index(prefix)
    own = [j for j, n in enumerate(cols) if n.startswith(prefix + " ")]
    for other in range(3):
        if other != k:
            assert all(rows[i + other][j] is None for j in own)
    return tuple(float(rows[i + k][j] or 0) for j in own)


def _gefs_april(engine: Engine, panel_id: int) -> tuple[float, ...]:
    return _bar(engine, panel_id, "GEFS")


def test_gefs_month_bars(engine: Engine) -> None:
    """GEFS als rechter Balken: je Tag ein 00Z-Lauf, Bronze aus meta.bronze_run."""
    store = Store(engine)
    sid = store.source_id("gefs_0p5")
    runs = _runs(0)
    # Bronze: 10 vorhanden, 2 im Download (einer davon veraltet), 3 nach der Frist gelöscht
    store.mark_bronze(sid, runs[:10], "loading")
    store.mark_bronze(sid, runs[:10], "present", 1000)
    store.mark_bronze(sid, runs[10:12], "loading")
    store.mark_bronze(sid, runs[12:15], "deleted")
    with engine.begin() as c:
        c.execute(
            text(
                "UPDATE meta.bronze_run SET updated_at = now() - interval '1 day' "
                "WHERE source_id = :s AND run_time = :r"
            ),
            {"s": sid, "r": runs[11]},
        )
    assert store.bronze_runs(sid)[runs[0]] == "present"
    assert _gefs_april(engine, 52) == (round(1000 / 30, 1), round(100 / 30, 1), 10.0)

    # Silber: 20 vorhanden, 1 im Bau
    store.mark_silver(sid, runs[:20], "present", "ghcr")
    store.mark_silver(sid, runs[20:21], "loading", "bronze")
    assert _gefs_april(engine, 46) == (round(2000 / 30, 1), round(100 / 30, 1))

    # Gold: 6 mit aktueller Version, 1 mit alter; Rest der 20 mit Silber in Arbeit
    future = datetime(2099, 1, 1, tzinfo=UTC)
    with engine.begin() as c:
        for i, r in enumerate(runs[:7]):
            c.execute(
                text("""INSERT INTO meta.nwp_feature_run (source_id, run_time, clustering_version,
                          features_version, silver_key, n_values, processed_at)
                        VALUES (:s, :r, :v, '1', 'test', 0, :t)"""),
                {
                    "s": sid,
                    "r": r,
                    "v": "it_alt" if i == 6 else CLUSTERING,
                    "t": future + timedelta(minutes=i if i < 6 else -1),
                },
            )
    assert _gefs_april(engine, 47) == (20.0, round(1400 / 30, 1))


def test_gfs_europa_month_bars(engine: Engine) -> None:
    """GFS Europa (gfs_0p5) als mittlerer Balken, unabhängig von GFS DE."""
    store = Store(engine)
    sid = store.source_id("gfs_0p5")
    runs = _runs(0)
    store.mark_silver(sid, runs[:15], "present", "ghcr")
    store.mark_silver(sid, runs[15:18], "loading", "ghcr")
    store.mark_silver(sid, runs[:3], "present", "bronze")
    assert _bar(engine, 46, "GFS Europa") == (50.0, 10.0)
    # Bronze: nur die 3 aus lokalem Bronze gebauten Läufe
    assert _bar(engine, 52, "GFS Europa") == (10.0, 0.0)
    # Gold gibt es noch nicht: alle 15 mit Silber in Arbeit
    assert _bar(engine, 47, "GFS Europa") == (0.0, 50.0)
    # GFS DE bleibt leer
    assert _bar(engine, 46, "GFS DE") == (0.0, 0.0)
