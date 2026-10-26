"""MaStR-Ablauf gegen die laufende Datenbank: Import, Verortung, Cluster, Tagesbestand.

Läuft nur mit ``PDB_INTEGRATION=1`` nach ``migrate``. Die Testdaten liegen in den Jahren 2093
bis 2099 (jüngster Snapshot, eigene Clusterversion) und werden danach wieder entfernt, damit
eine echte Datenbank nicht dauerhaft beeinflusst wird.
"""

import os
from collections.abc import Iterator
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from obstore.store import MemoryStore
from sqlalchemy import Engine, text

from mastr_ingest.config import MastrConfig
from mastr_ingest.download import Bronze
from mastr_ingest.pipeline import MastrPipeline
from mastr_ingest.store import Store
from mastr_ingest.testing import make_export, nap, unit
from predictiondb_core.db import create_db_engine
from predictiondb_core.settings import Settings

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.environ.get("PDB_INTEGRATION") != "1", reason="PDB_INTEGRATION != 1"),
]

CONFIG: dict[str, Any] = {
    "page_url": "https://mastr.test/page",
    "download_url": "https://mastr.test/Gesamtdatenexport_{date}_{version}.zip",
    "keep_snapshots": 100,
    "history_start": "2093-01-01",
    "cluster_grid": {"name": "0p25", "res": 0.25},
    "weight_grids": [{"name": "gfs_0p25", "res": 0.25}, {"name": "icon_eu", "res": 0.0625}],
    "technologies": {
        "wind_on": {"clusters": 2},
        "wind_off": {"clusters": 1},
        "pv_roof": {"clusters": 1},
        "pv_ground": {"clusters": 1},
        "biomass": {"clusters": 1},
    },
    "sea_areas": {"Nordsee": {"lat": 54.3, "lon": 6.6}},
}


def _coord(lat: float, lon: float) -> dict[str, str]:
    return {"Breitengrad": str(lat), "Laengengrad": str(lon)}


def tables(extra_wind: list[dict[str, Any]] | None = None) -> dict[str, list[dict[str, Any]]]:
    muc = {"Gemeindeschluessel": "09162000", "Postleitzahl": "80331", "Bundesland": "1403"}
    return {
        "EinheitenWind": [
            unit(
                "W1",
                2000,
                "2094-03-01",
                **_coord(54.02, 9.01),
                WindAnLandOderAufSee="888",
                LokationMaStRNummer="SELW1",
            ),
            unit("W2", 2000, "2096-01-01", **_coord(54.10, 9.20), WindAnLandOderAufSee="888"),
            unit(
                "W3",
                1000,
                "2095-01-01",
                **_coord(53.90, 8.90),
                WindAnLandOderAufSee="888",
                EinheitBetriebsstatus="38",
                DatumEndgueltigeStilllegung="2098-06-01",
            ),
            unit("W4", 3000, "2097-01-01", **_coord(48.50, 11.00), WindAnLandOderAufSee="888"),
            # vertauschte Koordinaten werden korrigiert
            unit("W5", 3000, "2097-06-01", **_coord(11.10, 48.60), WindAnLandOderAufSee="888"),
            unit(
                "O1",
                5000,
                "2097-01-01",
                **_coord(54.5, 6.3),
                WindAnLandOderAufSee="889",
                Seelage="640",
                LokationMaStRNummer="SELO1",
            ),
            unit(
                "O2",
                5000,
                "2097-01-01",
                WindAnLandOderAufSee="889",
                Seelage="640",
                Bundesland="1416",
            ),
            unit("P1", 3000, None, WindAnLandOderAufSee="888", EinheitBetriebsstatus="31"),
            *(extra_wind or []),
        ],
        "EinheitenSolar": [
            unit(
                "S1",
                100,
                "2097-01-01",
                ArtDerSolaranlage="853",
                **_coord(48.14, 11.58),
                **muc,
                LokationMaStRNummer="SELS1",
            ),
            unit("S2", 9.9, "2097-05-01", ArtDerSolaranlage="853", **muc),
            unit("S3", 9.9, "2097-05-01", ArtDerSolaranlage="2961", Postleitzahl="80331"),
            unit("S4", 9.9, "2097-05-01", ArtDerSolaranlage="853", Bundesland="1403"),
            unit(
                "S5", 9.9, "2097-05-01", ArtDerSolaranlage="853", Gemeindeschluessel="09175111"
            ),  # Kreis 09175 ohne Koordinaten → Land fehlt
            unit("G1", 8000, "2098-01-01", ArtDerSolaranlage="852", **_coord(51.0, 13.0)),
        ],
        "EinheitenBiomasse": [
            unit(
                "B1",
                500,
                "2094-01-01",
                **_coord(52.0, 10.0),
                EinheitBetriebsstatus="37",
                DatumBeginnVoruebergehendeStilllegung="2098-01-01",
            ),
        ],
        # Regelzone nur über diese drei Lokationen; der Rest wird aus Nachbarn übernommen
        "Netzanschlusspunkte": [
            nap("SANW1", "SELW1", "1001564"),
            nap("SANO1", "SELO1", "1001564"),
            nap("SANS1", "SELS1", "1000010"),
            nap("SANX", "SELX", None),
        ],
    }


@pytest.fixture
def engine() -> Iterator[Engine]:
    eng = create_db_engine(Settings())
    _cleanup(eng)
    yield eng
    _cleanup(eng)


def _cleanup(eng: Engine) -> None:
    with eng.begin() as c:
        c.execute(text("DELETE FROM assets.capacity_daily WHERE clustering_version LIKE 'v2099%'"))
        c.execute(text("DELETE FROM assets.clustering WHERE clustering_version LIKE 'v2099%'"))
        c.execute(text("DELETE FROM meta.region WHERE code LIKE :p"), {"p": "%:v2099%"})
        c.execute(text("DELETE FROM assets.snapshot WHERE export_date >= '2090-01-01'"))


def _q(eng: Engine, sql: str, **p: Any) -> list[Any]:
    with eng.connect() as c:
        return list(c.execute(text(sql), p).all())


def test_snapshot_cluster_capacity(engine: Engine, tmp_path: Path) -> None:
    config = MastrConfig.model_validate(CONFIG)
    pipeline = MastrPipeline(config, Store(engine), Bronze(MemoryStore()), tmp_path / "work")
    first = make_export(tmp_path, tables(), export_date="20990115", version="00.2")

    assert pipeline.is_due(date(2099, 2, 1))
    r = pipeline.run(date(2099, 1, 15), local_zip=first)
    assert r.clustering_version == "v2099Q1" and r.n_units == 15
    assert not pipeline.is_due(date(2099, 2, 1))

    geo = dict(
        _q(
            engine,
            """SELECT mastr_nr, COALESCE(geo_precision, '-') FROM assets.unit
                             WHERE snapshot_id = :id""",
            id=r.snapshot_id,
        )
    )
    assert geo["W5"] == "coord" and geo["O2"] == "seegebiet"
    assert (geo["S2"], geo["S3"], geo["S4"], geo["S5"]) == ("gemeinde", "plz", "land", "-")
    assert _q(engine, "SELECT lat, lon FROM assets.unit WHERE mastr_nr = 'W5'")[0] == (
        pytest.approx(48.6),
        pytest.approx(11.1),
    )

    # zwei Wind-Cluster (Nord/Süd), Region-Codes angelegt
    clusters = _q(
        engine,
        """SELECT c.cluster_no, c.centroid_lat, r.code
                             FROM assets.cluster c JOIN meta.region r USING (region_id)
                             WHERE clustering_version = 'v2099Q1' AND technology = 'wind_on'
                             ORDER BY 1""",
    )
    assert [n for n, _, _ in clusters] == [0, 1]
    assert clusters[0][1] < 50 < 53 < clusters[1][1]  # 0 = Süden (mehr Leistung), 1 = Norden
    assert clusters[0][2] == "wind_on:v2099Q1:c00"

    # Tagesbestand: Nord-Cluster 2095 = W1 + W3, nach Stilllegung von W3 nur W1 + W2
    def cap(tech: str, no: int, day: str) -> float:
        return float(
            _q(
                engine,
                """SELECT capacity_mw FROM assets.capacity_daily
            WHERE clustering_version = 'v2099Q1' AND technology = :t AND cluster_no = :n
              AND day = :d""",
                t=tech,
                n=no,
                d=date.fromisoformat(day),
            )[0][0]
        )

    assert cap("wind_on", 1, "2093-01-01") == 0
    assert cap("wind_on", 1, "2095-06-01") == pytest.approx(3.0)
    assert cap("wind_on", 1, "2098-05-31") == pytest.approx(5.0)
    assert cap("wind_on", 1, "2098-06-01") == pytest.approx(4.0)
    assert cap("biomass", 0, "2097-12-31") == pytest.approx(0.5)
    assert cap("biomass", 0, "2099-01-15") == 0  # vorübergehend stillgelegt
    assert cap("wind_off", 0, "2099-01-15") == pytest.approx(10.0)
    last = _q(
        engine, "SELECT max(day) FROM assets.capacity_daily WHERE clustering_version = 'v2099Q1'"
    )[0][0]
    assert last == date(2099, 1, 15)

    # Regelzonen: direkt über den Netzanschlusspunkt, sonst Mehrheit der Nachbarn
    zones = dict(
        _q(
            engine,
            """SELECT mastr_nr, COALESCE(regelzone, '-') || '/' || COALESCE(regelzone_source, '-')
                 FROM assets.unit WHERE snapshot_id = :id""",
            id=r.snapshot_id,
        )
    )
    assert (zones["W1"], zones["O1"], zones["S1"]) == ("tennet/nap", "tennet/nap", "amprion/nap")
    assert zones["O2"] == "tennet/seegebiet"
    assert (zones["S2"], zones["S3"], zones["S4"]) == (
        "amprion/gemeinde",
        "amprion/plz",
        "amprion/land",
    )
    assert zones["S5"] == "-/-" and zones["W2"] == "-/-"
    assert r.stats["zones"]["wind_off"]["zone_mw"] == {"tennet": 10.0}
    assert _q(
        engine,
        """SELECT netzbetreiber_nr FROM assets.unit
             WHERE snapshot_id = :id AND mastr_nr = 'S1'""",
        id=r.snapshot_id,
    ) == [("SNB900000000001",)]
    # Leistung je Regelzone ergibt in Summe die Clusterleistung des Gitters
    zw = _q(
        engine,
        """SELECT z.technology, z.regelzone, sum(z.capacity_mw),
                  (SELECT sum(capacity_mw) FROM assets.grid_weight g
                    WHERE g.clustering_version = 'v2099Q1' AND g.grid = 'gfs_0p25'
                      AND g.technology = z.technology)
             FROM assets.zone_weight z WHERE clustering_version = 'v2099Q1'
            GROUP BY 1, 2""",
    )
    by = {(t, z): mw for t, z, mw, _ in zw}
    assert by[("wind_off", "tennet")] == pytest.approx(10.0)
    assert by[("pv_roof", "amprion")] == pytest.approx(0.1 + 3 * 0.0099, rel=1e-4)
    for tech in {t for t, *_ in zw}:
        rows = [x for x in zw if x[0] == tech]
        assert sum(x[2] for x in rows) == pytest.approx(rows[0][3], rel=1e-4)

    # Gewichte je Cluster summieren sich auf 1, auch auf dem feineren Gitter
    sums = _q(
        engine,
        """SELECT grid, technology, cluster_no, sum(share), sum(capacity_mw)
                         FROM assets.grid_weight WHERE clustering_version = 'v2099Q1'
                         GROUP BY 1, 2, 3""",
    )
    assert {g for g, *_ in sums} == {"gfs_0p25", "icon_eu"}
    assert all(s == pytest.approx(1.0) for *_, s, _ in sums)

    # erneuter Import desselben Exports ist idempotent
    again = pipeline.run(date(2099, 1, 15), local_zip=first)
    assert again.snapshot_id == r.snapshot_id and again.clustering_version == "v2099Q1"

    # Folgesnapshot: Nachmeldung in neuer Zelle kommt in den nächstgelegenen Cluster
    late = unit("W6", 2500, "2098-12-01", **_coord(53.0, 8.0), WindAnLandOderAufSee="888")
    second = make_export(tmp_path, tables([late]), export_date="20990415", version="01.1")
    r2 = pipeline.run(date(2099, 4, 15), local_zip=second)
    assert r2.clustering_version == "v2099Q1"
    diff = r2.stats["diff"]["wind_on"]
    assert (diff["n_new"], diff["n_late"], diff["late_mw"]) == (1, 1, 2.5)
    assert _q(
        engine,
        """SELECT cluster_no FROM assets.cluster_cell
                         WHERE clustering_version = 'v2099Q1' AND technology = 'wind_on'
                           AND lat = 53.0 AND lon = 8.0""",
    ) == [(1,)]
    assert cap("wind_on", 1, "2099-04-15") == pytest.approx(6.5)
