"""``ml-train predict --fill-new``: welche Ausgabezeitpunkte eine neue Version nachrechnet.

Läuft nur mit ``PDB_INTEGRATION=1`` nach ``migrate``; die Testprognosen werden wieder entfernt.
"""

import os
from collections.abc import Iterator

import pandas as pd
import pytest
from sqlalchemy import Engine, text

from ml_train.predict import missing_compare_issues, write_predictions
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


def test_missing_compare_issues_in_window(engine: Engine) -> None:
    # Winterzeit: 08 UTC (Fehler D+1) und 09 UTC = 10 Uhr Ortszeit (ÜNB-Vergleich) je Tag
    """@relation(SWR-MLT-062, scope=function)"""
    day1 = [pd.Timestamp("2099-01-05T08", tz="UTC"), pd.Timestamp("2099-01-05T09", tz="UTC")]
    day2 = [t + pd.Timedelta(days=1) for t in day1]
    write_predictions(engine, _rows(NEW, [day1[1], day1[1] + pd.Timedelta(hours=3)]))
    write_predictions(engine, _rows(OTHER, day1 + day2))

    since, until = day1[0] - pd.Timedelta(hours=2), day2[1] - pd.Timedelta(minutes=30)
    # day1[1] hat die Version schon, day2[1] liegt hinter dem Fenster; andere Versionen zählen nicht
    assert missing_compare_issues(engine, NEW, since, until) == [day1[0], day2[0]]
    assert missing_compare_issues(engine, OLD, since, until) == [*day1, day2[0]]
