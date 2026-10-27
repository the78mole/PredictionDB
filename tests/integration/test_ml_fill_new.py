"""``ml-train predict --fill-new``: welche Ausgabezeitpunkte eine neue Version nachrechnet.

Läuft nur mit ``PDB_INTEGRATION=1`` nach ``migrate``; die Testprognosen werden wieder entfernt.
"""

import os
from collections.abc import Iterator

import pandas as pd
import pytest
from sqlalchemy import Engine, text

from ml_train.predict import missing_issues, write_predictions
from predictiondb_core.db import create_db_engine
from predictiondb_core.settings import Settings

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.environ.get("PDB_INTEGRATION") != "1", reason="PDB_INTEGRATION != 1"),
]

PREFIX = "it_fill_new"
OLD, OTHER, NEW = (f"{PREFIX}-{n}" for n in ("champion", "andere_linie", "neu"))


@pytest.fixture
def engine() -> Iterator[Engine]:
    eng = create_db_engine(Settings())
    yield eng
    with eng.begin() as c:
        c.execute(text(f"DELETE FROM fcst.prediction WHERE model_version LIKE '{PREFIX}-%'"))
    eng.dispose()


def _rows(version: str, issues: list[pd.Timestamp]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "model_version": version,
                "target": "load_actual",
                "issue_time": i,
                "valid_time": i + pd.Timedelta(hours=h),
                "quantile": 0.5,
                "value": 50_000.0,
            }
            for i in issues
            for h in (24, 25)
        ]
    )


def test_missing_issues_follow_peers_in_window(engine: Engine) -> None:
    t0 = pd.Timestamp("2099-01-05T08", tz="UTC")
    hours = [t0 + pd.Timedelta(hours=h) for h in range(6)]
    write_predictions(engine, _rows(OLD, hours[:4]))
    write_predictions(engine, _rows(OTHER, [hours[4], hours[5]]))
    write_predictions(engine, _rows(NEW, [hours[1]]))

    got = missing_issues(engine, NEW, [OLD, OTHER, NEW], hours[0], hours[4])
    # hours[1] hat die neue Version schon, hours[5] liegt hinter dem Fenster
    assert got == [hours[0], hours[2], hours[3], hours[4]]
    # ohne andere Versionen gibt es nichts nachzurechnen
    assert missing_issues(engine, NEW, [NEW], hours[0], hours[5]) == []
    assert missing_issues(engine, OLD, [OLD, OTHER], hours[0], hours[5]) == hours[4:]
