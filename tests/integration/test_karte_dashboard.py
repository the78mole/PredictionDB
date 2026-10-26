"""Abfragen des Karten-Dashboards (``config/grafana/dashboards/karte.json``).

Läuft nur mit ``PDB_INTEGRATION=1`` nach ``migrate``. Legt eine Test-Clusterung mit einem
Wind- und einem PV-Cluster an, ersetzt die Dashboard-Variablen wie Grafana und prüft die
Energiemenge am Prognosetag; die Testdaten werden wieder entfernt.
"""

import json
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, text

from predictiondb_core.db import create_db_engine
from predictiondb_core.settings import Settings

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.environ.get("PDB_INTEGRATION") != "1", reason="PDB_INTEGRATION != 1"),
]

DASHBOARD = Path(__file__).parents[2] / "config/grafana/dashboards/karte.json"
VERSION = "it_karte"
RUN = "2026-10-26T00:00:00Z"
# Dienstag 27.10.2026, 24 Stunden (Umstellung auf Winterzeit war am 25.10.)
DAY = "2026-10-27"
WIND_MW, PV_MW = 100.0, 40.0

VARS = {
    "version": VERSION,
    "lauf": RUN,
    "tag": DAY,
    "raster": "1",
    "technologie": "wind_on",
    "merkmal": "wind_power",
}


def _sub(sql: str, values: dict[str, str]) -> str:
    for name in sorted(values, key=len, reverse=True):
        sql = sql.replace(f"${{{name}}}", values[name]).replace(f"${name}", values[name])
    return sql


@pytest.fixture
def engine() -> Iterator[Engine]:
    eng = create_db_engine(Settings())
    with eng.begin() as c:
        gfs = c.execute(text("SELECT source_id FROM meta.source WHERE code = 'gfs_0p25'")).scalar()
        snap = c.execute(text("SELECT min(snapshot_id) FROM assets.snapshot")).scalar()
        c.execute(
            text("""INSERT INTO assets.clustering (clustering_version, snapshot_id, grid, grid_res)
                    VALUES (:v, :s, '0p25', 0.25)"""),
            {"v": VERSION, "s": snap},
        )
        c.execute(
            text("""INSERT INTO meta.nwp_feature_run (source_id, run_time, clustering_version,
                                                      features_version, silver_key, n_values)
                    VALUES (:g, :r, :v, 'test', 'test', 0)"""),
            {"g": gfs, "r": RUN, "v": VERSION},
        )
        for tech, code, mw, lat, lon in (
            ("wind_on", "wind_power", WIND_MW, 53.6, 9.6),
            ("pv_roof", "pv_power", PV_MW, 53.4, 9.4),
        ):
            region = c.execute(
                text("INSERT INTO meta.region (code) VALUES (:c) RETURNING region_id"),
                {"c": f"{tech}:{VERSION}:c00"},
            ).scalar_one()
            c.execute(
                text("INSERT INTO assets.cluster VALUES (:v, :t, 0, :lat, :lon, :r)"),
                {"v": VERSION, "t": tech, "lat": lat, "lon": lon, "r": region},
            )
            # zwei Zellen im selben 1°-Rasterfeld, Anteile 3/4 und 1/4
            for clat, clon, share in ((53.5, 9.5, 0.75), (53.75, 9.25, 0.25)):
                c.execute(
                    text("INSERT INTO assets.cluster_cell VALUES (:v, :t, :lat, :lon, 0)"),
                    {"v": VERSION, "t": tech, "lat": clat, "lon": clon},
                )
                c.execute(
                    text("""INSERT INTO assets.grid_weight
                            VALUES (:v, :t, 0, 'gfs_0p25', :lat, :lon, :cap, :s, :snap)"""),
                    {
                        "v": VERSION,
                        "t": tech,
                        "lat": clat,
                        "lon": clon,
                        "cap": 10 * share,
                        "s": share,
                        "snap": snap or 0,
                    },
                )
            c.execute(
                text("""INSERT INTO assets.capacity_daily VALUES (:v, :t, 0, :d, 10, :snap)"""),
                {"v": VERSION, "t": tech, "d": DAY, "snap": snap or 0},
            )
            feature = c.execute(
                text("""INSERT INTO meta.feature (source_id, code, aggregation, unit)
                        VALUES (:g, :c, 'cluster_cap_weighted', 'MW')
                        ON CONFLICT (source_id, code, aggregation)
                          DO UPDATE SET unit = EXCLUDED.unit
                        RETURNING feature_id"""),
                {"g": gfs, "c": code},
            ).scalar_one()
            # 1 h bis +120 h; konstante Leistung
            c.execute(
                text("""INSERT INTO feat.nwp_value
                        SELECT :f, :r, rt, rt + h * interval '1 hour', :mw
                          FROM CAST(:rt AS timestamptz) rt, generate_series(0, 120) h"""),
                {"f": feature, "r": region, "rt": RUN, "mw": mw},
            )
    yield eng
    with eng.begin() as c:
        c.execute(
            text("""DELETE FROM feat.nwp_value WHERE region_id IN
                    (SELECT region_id FROM assets.cluster WHERE clustering_version = :v)"""),
            {"v": VERSION},
        )
        c.execute(
            text("DELETE FROM assets.capacity_daily WHERE clustering_version = :v"), {"v": VERSION}
        )
        c.execute(
            text("DELETE FROM assets.grid_weight WHERE clustering_version = :v"), {"v": VERSION}
        )
        c.execute(
            text("DELETE FROM assets.clustering WHERE clustering_version = :v"), {"v": VERSION}
        )
        c.execute(
            text("DELETE FROM meta.nwp_feature_run WHERE clustering_version = :v"), {"v": VERSION}
        )
        c.execute(text("DELETE FROM meta.region WHERE code LIKE :p"), {"p": f"%:{VERSION}:%"})
    eng.dispose()


def _dashboard() -> dict[str, Any]:
    dash: dict[str, Any] = json.loads(DASHBOARD.read_text())
    return dash


def _rows(engine: Engine, sql: str, values: dict[str, str] = VARS) -> list[dict[str, Any]]:
    with engine.connect() as c:
        return [dict(r) for r in c.execute(text(_sub(sql, values).replace(":", r"\:"))).mappings()]


def _panel_sql(panel_id: int, ref: str = "A") -> str:
    panel = next(p for p in _dashboard()["panels"] if p["id"] == panel_id)
    return str(next(t["rawSql"] for t in panel["targets"] if t["refId"] == ref))


def test_all_queries_run(engine: Engine) -> None:
    dash = _dashboard()
    for var in dash["templating"]["list"]:
        if var["type"] == "query" and var["name"] != "version":
            assert _rows(engine, var["query"]), var["name"]
    for panel in dash["panels"]:
        for t in panel.get("targets", []):
            assert _rows(engine, t["rawSql"]), (panel["title"], t["refId"])


def test_energy_per_raster_cell_split_by_technology(engine: Engine) -> None:
    rows = _rows(engine, _panel_sql(1))
    assert len(rows) == 1
    r = rows[0]
    assert (float(r["lat"]), float(r["lon"])) == (53.5, 9.5)
    assert r["Wind an Land"] == pytest.approx(24 * WIND_MW, rel=1e-6)
    assert r["PV Dach"] == pytest.approx(24 * PV_MW, rel=1e-6)
    assert r["Wind auf See"] is None and r["PV Freifläche"] is None
    assert r["Gesamt"] == pytest.approx(24 * (WIND_MW + PV_MW), rel=1e-6)
    assert float(r["Windanteil"]) == pytest.approx(100 * WIND_MW / (WIND_MW + PV_MW))


def test_cluster_table_and_days(engine: Engine) -> None:
    rows = {r["Erzeugungsart"]: r for r in _rows(engine, _panel_sql(3))}
    assert rows["Wind an Land"]["Volllaststunden"] == pytest.approx(24 * WIND_MW / 10)
    tag = next(v for v in _dashboard()["templating"]["list"] if v["name"] == "tag")
    days = [r["__value"] for r in _rows(engine, tag["query"])]
    # Lauf 26.10. 00Z: dem 26.10. fehlt nur die erste Stunde, letzter voller Tag ist der 10.11.;
    # "morgen" steht vorn, wenn der Lauf ihn abdeckt, sonst sind die Tage aufsteigend
    assert sorted(days) == [
        f"2026-{m:02d}-{d:02d}"
        for m, d in [(10, i) for i in range(26, 32)] + [(11, i) for i in range(1, 11)]
    ]
    assert days[1:] == sorted(days[1:])
