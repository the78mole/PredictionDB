from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import create_engine

from energy_ingest.cli import build_sources
from energy_ingest.config import EnergySettings
from energy_ingest.http import Http
from energy_ingest.model import BERLIN, HOUR, MONTH, YEAR
from energy_ingest.sources.marktwert import (
    PRICE,
    Marktwert,
    Row,
    market_values,
    period_starts,
)

MAR = datetime(2025, 3, 1, tzinfo=BERLIN).astimezone(UTC)
APR = datetime(2025, 4, 1, tzinfo=BERLIN).astimezone(UTC)


def month_rows(price: float, solar: float, start: datetime = MAR, end: datetime = APR) -> list[Row]:
    """Stundenwerte eines Monats: Preis konstant, PV nur 10-14 UTC mit doppeltem Preis."""
    rows = []
    t = start
    while t < end:
        midday = 10 <= t.hour < 14
        rows.append((t, PRICE, price * 2 if midday else price))
        rows.append((t, "gen_solar", solar if midday else 0.0))
        t += HOUR
    return rows


def test_period_starts_local_time() -> None:
    start = datetime(2025, 2, 15, tzinfo=UTC)
    end = datetime(2025, 5, 1, tzinfo=UTC)
    assert period_starts(start, end, 1) == [
        MAR,
        APR,
        datetime(2025, 4, 30, 22, tzinfo=UTC),  # 1. Mai 00:00 MESZ
    ]
    assert period_starts(
        datetime(2024, 6, 1, tzinfo=UTC), datetime(2026, 1, 1, tzinfo=UTC), 12
    ) == [
        datetime(2024, 12, 31, 23, tzinfo=UTC),
        datetime(2025, 12, 31, 23, tzinfo=UTC),
    ]


def test_market_values_month() -> None:
    out = {d.code: d for d in market_values(month_rows(50.0, 1000.0), [MAR], 1)}
    assert set(out) == {"mw_base", "mw_solar"}
    assert out["mw_solar"].resolution == MONTH
    ((ts, solar),) = out["mw_solar"].points
    assert ts == MAR
    # PV erzeugt nur in den teuren Stunden: Marktwert = doppelter Preis
    assert solar == pytest.approx(100.0)
    ((_, base),) = out["mw_base"].points
    hours = (APR - MAR) / HOUR
    midday = sum(1 for t, c, _ in month_rows(50.0, 1000.0) if c == PRICE and 10 <= t.hour < 14)
    assert base == pytest.approx((50.0 * (hours - midday) + 100.0 * midday) / hours)


def test_market_values_skip_incomplete_period() -> None:
    rows = month_rows(50.0, 1000.0, end=APR - timedelta(days=2))
    assert market_values(rows, [MAR], 1) == []


def test_market_values_year_resolution() -> None:
    jan = datetime(2025, 1, 1, tzinfo=BERLIN).astimezone(UTC)
    nxt = datetime(2026, 1, 1, tzinfo=BERLIN).astimezone(UTC)
    out = {d.code: d for d in market_values(month_rows(40.0, 10.0, jan, nxt), [jan], 12)}
    assert out["jw_solar"].resolution == YEAR
    assert out["jw_solar"].points[0][1] == pytest.approx(80.0)


def test_fetch_only_finished_periods() -> None:
    src = Marktwert(create_engine("sqlite://"), clock=lambda: APR + timedelta(days=3))
    src._hourly = lambda a, b: month_rows(50.0, 1000.0, a, b)  # type: ignore[method-assign,assignment]
    out = src.fetch("market_value_month", MAR - timedelta(days=1), APR + timedelta(days=1))
    assert {d.code for d in out} == {"mw_base", "mw_solar"}
    assert all(p[0] == MAR for d in out for p in d.points)
    assert src.fetch("market_value_month", APR, APR + timedelta(days=10)) == []


def _codes(settings: EnergySettings) -> list[str]:
    http = Http(httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500))))
    engine = create_engine("sqlite://")
    return [s.code for s in build_sources(settings.sources, settings, http, engine)]


def test_unfree_sources_need_allow(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in [
        "ALLOW_ENTSO_E",
        "ALLOW_ENERGY_CHARTS",
        "ALLOW_NETZTRANSPARENZ",
        "ALLOW_EIA_REUTERS",
        "ALLOW_THE",
    ]:
        monkeypatch.delenv(f"PDB_{name}", raising=False)
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PDB_ENTSOE_TOKEN", "t")
    monkeypatch.setenv("PDB_NETZTRANSPARENZ_CLIENT_ID", "id")
    monkeypatch.setenv("PDB_NETZTRANSPARENZ_CLIENT_SECRET", "s")
    monkeypatch.setenv("PDB_GIE_API_KEY", "k")
    settings = EnergySettings(_env_file=None)
    assert _codes(settings) == [
        "entsoe_last",
        "smard",
        "energy_charts",
        "eia",
        "eia_steo",
        "ecb",
        "worldbank",
        "oil_bulletin",
        "gie_agsi",
        "gie_alsi",
        "eurostat",
        "pdb_marktwert",
    ]

    monkeypatch.setenv("PDB_ALLOW_ENTSO_E", "true")
    monkeypatch.setenv("ALLOW_NETZTRANSPARENZ", "true")
    monkeypatch.setenv("PDB_ALLOW_ENERGY_CHARTS", "true")
    settings = EnergySettings(_env_file=None)
    # Mit amtlichen Marktwerten entfällt die eigene Rechnung
    assert _codes(settings)[:5] == [
        "entsoe",
        "entsoe_last",
        "smard",
        "energy_charts",
        "netztransparenz",
    ]
    assert "pdb_marktwert" not in _codes(settings)
