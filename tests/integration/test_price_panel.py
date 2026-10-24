"""Zeitbereich des Preis-Panels: Variablen ``preis_bis``/``preis_ab`` im Status-Dashboard.

Läuft nur mit ``PDB_INTEGRATION=1``. Die Grafana-Makros ``$__timeFrom()``/``$__timeTo()`` werden
wie im Postgres-Plugin durch Zeitstempel-Literale ersetzt.
"""

import json
import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import Engine, text

from predictiondb_core.db import create_db_engine
from predictiondb_core.settings import Settings

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.environ.get("PDB_INTEGRATION") != "1", reason="PDB_INTEGRATION != 1"),
]

DASHBOARD = Path(__file__).parents[2] / "config/grafana/dashboards/status.json"
BERLIN = ZoneInfo("Europe/Berlin")


@pytest.fixture
def engine() -> Iterator[Engine]:
    eng = create_db_engine(Settings())
    yield eng
    eng.dispose()


def _var(engine: Engine, name: str, start: datetime, end: datetime) -> str:
    variables = json.loads(DASHBOARD.read_text())["templating"]["list"]
    q = next(v["query"] for v in variables if v["name"] == name)
    q = q.replace("$__timeFrom()", f"'{start.isoformat()}'::timestamptz")
    q = q.replace("$__timeTo()", f"'{end.isoformat()}'::timestamptz")
    with engine.connect() as c:
        return str(c.execute(text(q)).scalar_one())


def test_panel_reaches_end_of_day(engine: Engine) -> None:
    with engine.connect() as c:
        now = c.execute(text("SELECT now()")).scalar_one()
    start = now - timedelta(days=2)
    length = _var(engine, "preis_bis", start, now)
    shift = _var(engine, "preis_ab", start, now)
    assert length.startswith("+") and length.endswith("s") and shift.endswith("s")

    # Grafana: [now, now + Länge] um Rückversatz nach hinten verschoben
    lo = now - timedelta(seconds=int(shift[:-1]))
    hi = now + timedelta(seconds=int(length[1:-1]) - int(shift[:-1]))
    assert abs(lo - start) < timedelta(seconds=2)

    local = now.astimezone(BERLIN)
    days = 2 if local.hour >= 12 else 1
    end = datetime.combine(local.date() + timedelta(days=days), datetime.min.time(), BERLIN)
    assert abs(hi - end.astimezone(UTC)) < timedelta(seconds=2)


def test_past_range_keeps_dashboard_range(engine: Engine) -> None:
    end = datetime(2025, 1, 10, tzinfo=UTC)
    assert _var(engine, "preis_bis", end - timedelta(days=1), end) == "aus"
    assert _var(engine, "preis_ab", end - timedelta(days=1), end) == "0s"
