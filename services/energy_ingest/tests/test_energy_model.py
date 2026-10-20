from datetime import UTC, datetime, timedelta

from energy_ingest.model import (
    HOUR,
    QUARTER_HOUR,
    Kind,
    estimate_published,
    floor_to,
    published_at,
)


def test_price_published_day_before_13_local() -> None:
    # Liefertag 2026-10-20 (MESZ): Auktion am 19.10. um 13:00 MESZ = 11:00 UTC
    ts = datetime(2026, 10, 20, 8, 0, tzinfo=UTC)
    assert estimate_published(Kind.PRICE_DA, ts, QUARTER_HOUR) == datetime(
        2026, 10, 19, 11, 0, tzinfo=UTC
    )
    # 23:30 UTC am 19.10. ist schon 01:30 MESZ des 20.10. und gehört zum Liefertag 20.10.
    ts = datetime(2026, 10, 19, 23, 30, tzinfo=UTC)
    assert estimate_published(Kind.PRICE_DA, ts, QUARTER_HOUR).date().isoformat() == "2026-10-19"


def test_forecasts_and_actuals() -> None:
    ts = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)  # Winterzeit
    assert estimate_published(Kind.LOAD_DA_FORECAST, ts, QUARTER_HOUR) == datetime(
        2026, 1, 14, 9, 0, tzinfo=UTC
    )
    assert estimate_published(Kind.GEN_DA_FORECAST, ts, QUARTER_HOUR) == datetime(
        2026, 1, 14, 17, 0, tzinfo=UTC
    )
    assert estimate_published(Kind.ACTUAL, ts, QUARTER_HOUR) == ts + QUARTER_HOUR + HOUR


def test_published_at_prefers_earlier_fetch() -> None:
    ts = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)
    early = ts + timedelta(minutes=20)
    assert published_at(Kind.ACTUAL, ts, QUARTER_HOUR, early) == early
    late = ts + timedelta(days=300)
    assert published_at(Kind.ACTUAL, ts, QUARTER_HOUR, late) == ts + QUARTER_HOUR + HOUR


def test_floor_to() -> None:
    ts = datetime(2026, 1, 15, 12, 7, tzinfo=UTC)
    assert floor_to(ts, QUARTER_HOUR) == datetime(2026, 1, 15, 12, 0, tzinfo=UTC)
