"""Gewichte der Cluster wie im Runner: MaStR verarbeiten, exportieren, Clusterung fortschreiben.

Läuft nur mit ``PDB_INTEGRATION=1`` nach ``migrate``. Snapshots in den Jahren 2091/2092 mit der
Clusterversion ``v2091Q4`` werden angelegt und danach wieder entfernt. Zwischen den beiden
Läufen wird die Clusterung gelöscht, wie in einer frischen Datenbank im Runner; der zweite Lauf
muss sie aus dem Gewichte-Artefakt wiederherstellen und fortschreiben.
"""

import os
from collections.abc import Iterator
from datetime import date
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import Engine, text

from mastr_ingest.config import MastrConfig
from mastr_ingest.testing import make_export, unit
from predictiondb_core.db import create_db_engine
from predictiondb_core.settings import Settings
from wx_exchange.gold import Weights
from wx_exchange.mastr_weights import build_weights

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.environ.get("PDB_INTEGRATION") != "1", reason="PDB_INTEGRATION != 1"),
]

CONFIG: dict[str, Any] = {
    "page_url": "https://mastr.test/page",
    "download_url": "https://mastr.test/Gesamtdatenexport_{date}_{version}.zip",
    "keep_snapshots": 100,
    "history_start": "2090-01-01",
    "cluster_grid": {"name": "0p25", "res": 0.25},
    "weight_grids": [{"name": "gfs_0p25", "res": 0.25}],
    "technologies": {"wind_on": {"clusters": 2}},
}


def _wind(nr: str, kw: float, lat: float, lon: float, since: str) -> dict[str, Any]:
    return unit(
        nr, kw, since, Breitengrad=str(lat), Laengengrad=str(lon), WindAnLandOderAufSee="888"
    )


def _cleanup(eng: Engine) -> None:
    with eng.begin() as c:
        c.execute(text("DELETE FROM assets.capacity_daily WHERE clustering_version LIKE 'v209_Q%'"))
        c.execute(text("DELETE FROM assets.zone_weight WHERE clustering_version LIKE 'v209_Q%'"))
        c.execute(text("DELETE FROM assets.clustering WHERE clustering_version LIKE 'v209_Q%'"))
        c.execute(text("DELETE FROM meta.region WHERE code LIKE :p"), {"p": "%:v209_Q%"})
        c.execute(
            text(
                "DELETE FROM assets.snapshot"
                " WHERE export_date BETWEEN '2091-01-01' AND '2092-12-31'"
            )
        )


@pytest.fixture
def engine() -> Iterator[Engine]:
    eng = create_db_engine(Settings())
    _cleanup(eng)
    yield eng
    _cleanup(eng)


def test_weights_build_restore_and_continue(engine: Engine, tmp_path: Path) -> None:
    """@relation(SWR-WXX-070, SWR-WXX-075, scope=function)"""
    config = MastrConfig.model_validate(CONFIG)
    south = [
        _wind("S1", 3000, 48.5, 11.0, "2090-06-01"),
        _wind("S2", 2000, 48.6, 11.2, "2091-01-01"),
    ]
    north = [_wind("N1", 1000, 54.0, 9.0, "2090-03-01")]
    first = make_export(
        tmp_path, {"EinheitenWind": south + north}, export_date="20911001", version="91.2"
    )
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(404)))

    w1 = build_weights(
        engine,
        config,
        "gfs_0p25",
        tmp_path / "a",
        client,
        date(2091, 10, 1),
        None,
        local_zip=first,
    )
    assert w1.version == "v2091Q4"
    assert w1.info["mastr_export_date"] == "2091-10-01"
    assert w1.info["clustering_export_date"] == "2091-10-01"
    assert w1.regions == ["wind_on:v2091Q4:c00", "wind_on:v2091Q4:c01"]
    assert w1.cells is not None and w1.cells.num_rows == 3
    assert w1.capacity is not None
    # Gewichte und Zustand überstehen den Weg über das Artefakt
    back = Weights.from_layers(w1.version, dict(w1.layers(state=True)), w1.info)
    assert back.digest == w1.digest and back.cells is not None

    # frische Datenbank wie im Runner, dann nächstes Quartal mit einer neuen Anlage im Norden
    _cleanup(engine)
    second = make_export(
        tmp_path,
        {"EinheitenWind": [*south, *north, _wind("N2", 4000, 54.25, 9.25, "2091-11-01")]},
        export_date="20920105",
        version="92.1",
    )
    w2 = build_weights(
        engine,
        config,
        "gfs_0p25",
        tmp_path / "b",
        client,
        date(2092, 1, 5),
        back,
        local_zip=second,
    )
    assert w2.version == "v2091Q4"  # fortgeschrieben, nicht neu geclustert
    assert w2.info["mastr_export_date"] == "2092-01-05"
    assert w2.info["clustering_export_date"] == "2091-10-01"
    assert w2.cells is not None and w2.cells.num_rows == 4
    north_no = next(
        c["cluster_no"] for c in w1.cells.to_pylist() if c["lat"] == pytest.approx(54.0)
    )
    added = next(c for c in w2.cells.to_pylist() if c["lat"] == pytest.approx(54.25))
    assert added["cluster_no"] == north_no  # neue Zelle geht an das nächste Zentrum
    assert w2.digest != w1.digest
