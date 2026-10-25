"""Zeitbereich des Preis-Panels: Variablen ``preis_bis``/``preis_ab`` im Status-Dashboard.

Das Panel zeigt immer gestern 00:00 bis Ende heute bzw. ab 12 Uhr bis Ende morgen (Europe/Berlin),
unabhängig vom Dashboard-Zeitbereich.

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


def _expected(now: datetime) -> tuple[datetime, datetime]:
    local = now.astimezone(BERLIN)
    days = 2 if local.hour >= 12 else 1
    lo = datetime.combine(local.date() - timedelta(days=1), datetime.min.time(), BERLIN)
    hi = datetime.combine(local.date() + timedelta(days=days), datetime.min.time(), BERLIN)
    return lo.astimezone(UTC), hi.astimezone(UTC)


@pytest.mark.parametrize("span", [timedelta(hours=6), timedelta(days=2), timedelta(days=30)])
def test_panel_spans_yesterday_to_end_of_day(engine: Engine, span: timedelta) -> None:
    with engine.connect() as c:
        now = c.execute(text("SELECT now()")).scalar_one()
    length = _var(engine, "preis_bis", now - span, now)
    shift = _var(engine, "preis_ab", now - span, now)
    assert length.startswith("+") and length.endswith("s") and shift.endswith("s")

    # Grafana: [now, now + Länge] um Rückversatz nach hinten verschoben
    lo = now - timedelta(seconds=int(shift[:-1]))
    hi = now + timedelta(seconds=int(length[1:-1]) - int(shift[:-1]))
    exp_lo, exp_hi = _expected(now)
    assert abs(lo - exp_lo) < timedelta(seconds=2)
    assert abs(hi - exp_hi) < timedelta(seconds=2)


def test_past_range_still_shows_yesterday_and_today(engine: Engine) -> None:
    end = datetime(2025, 1, 10, tzinfo=UTC)
    with engine.connect() as c:
        now = c.execute(text("SELECT now()")).scalar_one()
    shift = _var(engine, "preis_ab", end - timedelta(days=1), end)
    assert abs(now - timedelta(seconds=int(shift[:-1])) - _expected(now)[0]) < timedelta(seconds=2)
