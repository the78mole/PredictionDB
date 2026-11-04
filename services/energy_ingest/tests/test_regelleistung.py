"""regelleistung.net: synthetische Ergebnisdateien im Aufbau der echten (keine echten Daten im
Repo, die Quelle ist nicht frei weitergebbar)."""

import io
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx
import pytest
from openpyxl import Workbook

from energy_ingest.config import EnergySettings
from energy_ingest.http import Http
from energy_ingest.model import Kind, estimate_published, series
from energy_ingest.sources import FormatError
from energy_ingest.sources.regelleistung import Regelleistung, hours, parse_capacity

DAY = date(2026, 11, 1)  # Sonntag, MEZ
MIDNIGHT = datetime(2026, 10, 31, 23, 0, tzinfo=UTC)


def xlsx(head: list[str], rows: list[list[Any]]) -> bytes:
    wb = Workbook()
    ws = wb.active
    assert ws is not None
    ws.title = "001"
    ws.append(head)
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


AFRR_HEAD = [
    "DATE_FROM",
    "DATE_TO",
    "TYPE_OF_RESERVES",
    "PRODUCT",
    "TOTAL_AVERAGE_CAPACITY_PRICE_[(EUR/MW)/h]",
    "GERMANY_MIN_CAPACITY_PRICE_[(EUR/MW)/h]",
    "GERMANY_AVERAGE_CAPACITY_PRICE_[(EUR/MW)/h]",
    "GERMANY_MARGINAL_CAPACITY_PRICE_[(EUR/MW)/h]",
    "GERMANY_IMPORT(-)_EXPORT(+)_[MW]",
    "GERMANY_ALLOCATED_VOLUME_[MW]",
    "AUSTRIA_AVERAGE_CAPACITY_PRICE_[(EUR/MW)/h]",
    "GERMANY_SUM_OF_OFFERED_CAPACITY_[MW]",
]


def afrr_file() -> bytes:
    d = date(2026, 11, 1)
    return xlsx(
        AFRR_HEAD,
        [
            [d, d, "aFRR", "POS_00_04", 9.0, 1.0, 1.5, 2.0, -33, 1893, 7.0, 4176],
            [d, d, "aFRR", "NEG_20_24", 9.0, 0.5, 0.8, 1.1, 0, 1900, 7.0, "-"],
        ],
    )


def test_afrr_capacity_hours_and_columns() -> None:
    """@relation(SWR-ENI-054, scope=function)"""
    out = {d.code: d for d in parse_capacity(afrr_file(), "afrr_capacity")}
    assert set(out) == {
        "rl_afrr_pos_price_avg",
        "rl_afrr_pos_price_marginal",
        "rl_afrr_pos_volume",
        "rl_afrr_pos_offered",
        "rl_afrr_neg_price_avg",
        "rl_afrr_neg_price_marginal",
        "rl_afrr_neg_volume",
    }
    avg = out["rl_afrr_pos_price_avg"]
    assert avg.resolution == timedelta(hours=1)
    # 00-04 Uhr MEZ = 23-03 UTC, je Stunde der Zeitscheibe derselbe Wert
    assert avg.points == [(MIDNIGHT + timedelta(hours=h), 1.5) for h in range(4)]
    assert out["rl_afrr_pos_volume"].points[0][1] == 1893.0
    # 20-24 Uhr MEZ = 19-23 UTC
    neg = out["rl_afrr_neg_price_marginal"].points
    assert [ts.hour for ts, _ in neg] == [19, 20, 21, 22]
    assert {v for _, v in neg} == {1.1}


def test_old_prices_per_slot_become_per_hour() -> None:
    """Bis etwa 2021 stehen Leistungspreise in EUR/MW je Zeitscheibe.

    @relation(SWR-ENI-054, scope=function)
    """
    d = date(2021, 6, 1)
    raw = xlsx(
        [
            "DATE_FROM",
            "DATE_TO",
            "TYPE_OF_RESERVES",
            "PRODUCT",
            "GERMANY_AVERAGE_CAPACITY_PRICE_[EUR/MW]",
            "GERMANY_MARGINAL_CAPACITY_PRICE_[EUR/MW]",
            "GERMANY_AVERAGE_ENERGY_PRICE_[EUR/MWh]",
            "GERMANY_SUM_OF_OFFERED_CAPACITY_[MW]",
        ],
        [[d, d, "aFRR", "POS_00_04", 42.0, 48.0, "-", 4436]],
    )
    out = {s.code: s for s in parse_capacity(raw, "afrr_capacity")}
    assert out["rl_afrr_pos_price_avg"].points[0][1] == 10.5
    assert out["rl_afrr_pos_price_marginal"].points[0][1] == 12.0
    assert "rl_afrr_pos_volume" not in out  # Spalte gab es damals nicht


def test_fcr_daily_product_and_de_prefix() -> None:
    """FCR bis 06/2020 als Tagesprodukt, Spalten mit Präfix ``DE_``.

    @relation(SWR-ENI-054, scope=function)
    """
    d = date(2020, 6, 1)
    raw = xlsx(
        [
            "DATE_FROM",
            "DATE_TO",
            "PRODUCT_TYPE",
            "TENDER_NUMBER",
            "PRODUCTNAME",
            "CROSSBORDER_SETTLEMENTCAPACITY_PRICE_[EUR/MW]",
            "AT_SETTLEMENTCAPACITY_PRICE_[EUR/MW]",
            "DE_DEMAND_[MW]",
            "DE_SETTLEMENTCAPACITY_PRICE_[EUR/MW]",
            "DE_IMPORT(-)_EXPORT(+)_[MW]",
        ],
        [[d, d, "FCR", 1, "NEGPOS_00_24", 205.1, 205.1, 573, 240.0, -94]],
    )
    out = {s.code: s for s in parse_capacity(raw, "fcr_capacity")}
    price = out["rl_fcr_price"].points
    assert len(price) == 24 and {v for _, v in price} == {10.0}
    assert {v for _, v in out["rl_fcr_demand"].points} == {573.0}


def test_dst_day_has_25_hours() -> None:
    """@relation(SWR-ENI-054, scope=function)"""
    slots = list(hours(date(2026, 10, 25), 0, 4))
    assert len(slots) == 5  # 02:00-03:00 doppelt
    assert len(list(hours(date(2026, 10, 25), 0, 24))) == 25
    assert len(list(hours(date(2026, 3, 29), 0, 4))) == 3


def test_unknown_product_or_unit_is_format_error() -> None:
    """@relation(SWR-ENI-054, SWR-ENI-030, scope=function)"""
    d = date(2026, 11, 1)
    bad_product = xlsx(AFRR_HEAD, [[d, d, "aFRR", "POS_HT", 1, 1, 1, 1, 0, 1, 1, 1]])
    with pytest.raises(FormatError):
        parse_capacity(bad_product, "afrr_capacity")
    head = [h.replace("[(EUR/MW)/h]", "[EUR/kW]") for h in AFRR_HEAD]
    bad_unit = xlsx(head, [[d, d, "aFRR", "POS_00_04", 1, 1, 1, 1, 0, 1, 1, 1]])
    with pytest.raises(FormatError):
        parse_capacity(bad_unit, "afrr_capacity")
    with pytest.raises(FormatError):
        parse_capacity(b"kein xlsx", "afrr_capacity")


def test_empty_file_before_first_auction() -> None:
    """@relation(SWR-ENI-054, scope=function)"""
    assert parse_capacity(xlsx(AFRR_HEAD, []), "afrr_capacity") == []


def test_fetch_one_file_per_day(make_http: Callable[..., Http]) -> None:
    """@relation(SWR-ENI-054, scope=function)"""
    seen: list[dict[str, str]] = []

    def h(req: httpx.Request) -> httpx.Response:
        assert req.url.path.endswith("/resultsoverview")
        seen.append(dict(req.url.params))
        return httpx.Response(200, content=afrr_file())

    rl = Regelleistung(make_http(h))
    out = rl.fetch("afrr_capacity", MIDNIGHT, MIDNIGHT + timedelta(hours=2))
    assert seen == [
        {"date": "2026-11-01", "exportFormat": "xlsx", "market": "CAPACITY", "productTypes": "aFRR"}
    ]
    avg = next(s for s in out if s.code == "rl_afrr_pos_price_avg")
    assert [ts for ts, _ in avg.points] == [MIDNIGHT, MIDNIGHT + timedelta(hours=1)]


def test_train_only_and_needs_permission(monkeypatch: pytest.MonkeyPatch) -> None:
    """@relation(SWR-ENI-003, SWR-ENI-054, scope=function)"""
    assert (Regelleistung.free, Regelleistung.train) == (False, True)
    monkeypatch.delenv("PDB_ALLOW_REGELLEISTUNG", raising=False)
    assert not EnergySettings(_env_file=None).allowed("regelleistung")
    monkeypatch.setenv("PDB_ALLOW_REGELLEISTUNG", "true")
    assert EnergySettings(_env_file=None).allowed("regelleistung")


def test_capacity_known_before_day_ahead_auction() -> None:
    """@relation(SWR-ENI-021, scope=function)"""
    assert series("rl_fcr_price").kind is Kind.RESERVE_CAPACITY
    ts = MIDNIGHT + timedelta(hours=10)
    pub = estimate_published(Kind.RESERVE_CAPACITY, ts, timedelta(hours=1))
    assert pub == datetime(2026, 10, 31, 11, 0, tzinfo=UTC)  # 12:00 MEZ am Vortag
