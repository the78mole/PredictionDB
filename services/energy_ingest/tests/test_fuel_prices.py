"""Brennstoffpreise und Wechselkurs: EIA, EZB, Weltbank, Oil Bulletin, THE (gekürzte echte
Antworten vom 23.10.2026 in ``fixtures``)."""

import io
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import ClassVar

import httpx
import pytest
from openpyxl import load_workbook

from energy_ingest.config import EnergySettings
from energy_ingest.http import Http
from energy_ingest.model import DAY, MONTH, WEEK, Kind, estimate_published
from energy_ingest.sources import FormatError
from energy_ingest.sources.ecb import Ecb, parse_csv
from energy_ingest.sources.eia import (
    FREE_SERIES,
    REUTERS_SERIES,
    Eia,
    EiaBulk,
    EiaReuters,
    EiaSeries,
    parse_api,
    parse_bulk,
)
from energy_ingest.sources.oil_bulletin import OilBulletin
from energy_ingest.sources.oil_bulletin import parse_xlsx as parse_oil_bulletin
from energy_ingest.sources.the import The, parse_prices
from energy_ingest.sources.worldbank import WorldBank
from energy_ingest.sources.worldbank import parse_xlsx as parse_worldbank

FIXTURES = Path(__file__).parent / "fixtures"
Handler = Callable[[httpx.Request], httpx.Response]
MakeHttp = Callable[[Handler], Http]

OCT_20 = datetime(2026, 10, 19, 22, 0, tzinfo=UTC)  # 20.10.2026 00:00 MESZ


def _bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def _xlsx_without(raw: bytes, sheet: str, row: int, col: int) -> bytes:
    """Kopie der Datei, in der eine Zelle geleert ist (simulierte Formatänderung)."""
    wb = load_workbook(io.BytesIO(raw))
    wb[sheet].cell(row=row, column=col).value = "geändert"
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


# --- EIA ---------------------------------------------------------------------------------


def test_eia_bulk_free_series() -> None:
    """@relation(SWR-ENI-045, scope=function)"""
    out = parse_bulk(_bytes("eia_bulk_PET.zip"), FREE_SERIES)
    assert set(out) == {"oil_ulsd_nyh", "oil_ulsd_usgc"}
    nyh = dict(out["oil_ulsd_nyh"])
    assert nyh[OCT_20] == pytest.approx(5.265)
    assert list(nyh) == sorted(nyh)  # Bulk liefert absteigend, wir aufsteigend


def test_eia_bulk_unit_change_is_format_error() -> None:
    """@relation(SWR-ENI-045, SWR-ENI-030, scope=function)"""
    wanted = {"oil_brent": REUTERS_SERIES["oil_brent"]}
    wanted["oil_brent"] = type(wanted["oil_brent"])("petroleum/pri/spt", "PET", "RBRTE", "$/gal")
    with pytest.raises(FormatError, match="Einheit"):
        parse_bulk(_bytes("eia_bulk_PET.zip"), wanted)


def test_eia_without_key_uses_bulk(make_http: MakeHttp) -> None:
    """@relation(SWR-ENI-045, SWR-ENI-004, scope=function)"""
    calls: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req.url.path)
        name = req.url.path.rsplit("/", 1)[-1].removesuffix(".zip")
        return httpx.Response(200, content=_bytes(f"eia_bulk_{name}.zip"))

    http = make_http(handler)
    bulk = EiaBulk(http)
    free, reuters = Eia(http, "", bulk), EiaReuters(http, "", bulk)
    start, end = OCT_20 - 5 * DAY, OCT_20 + DAY
    assert {d.code for d in free.fetch("prices", start, end)} == {"oil_ulsd_nyh", "oil_ulsd_usgc"}
    got = {d.code: d for d in reuters.fetch("prices", start, end)}
    assert set(got) == set(REUTERS_SERIES)
    assert dict(got["oil_brent"].points)[OCT_20] == pytest.approx(115.22)
    assert dict(got["gas_henry_hub"].points)[OCT_20] == pytest.approx(3.1)
    assert all(ts < end for d in got.values() for ts, _ in d.points)
    # Jede Bulk-Datei nur einmal, auch für beide Quellen
    assert sorted(calls) == ["/bulk/NG.zip", "/bulk/PET.zip"]
    assert (free.free, reuters.free) == (True, False)


def test_eia_api_pages(make_http: MakeHttp) -> None:
    """@relation(SWR-ENI-045, scope=function)"""

    def row(day: str, value: float) -> dict[str, object]:
        return {
            "period": day,
            "series": "EER_EPD2DXL0_PF4_Y35NY_DPG",
            "value": value,
            "units": "$/GAL",
        }

    pages = {
        0: {"response": {"total": "5001", "data": [row("2026-10-19", 5.173)]}},
        5000: {"response": {"total": "5001", "data": [row("2026-10-20", 5.265)]}},
    }
    seen: list[dict[str, str]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        params = dict(req.url.params)
        seen.append(params)
        return httpx.Response(200, json=pages[int(params["offset"])])

    class OneSeries(Eia):
        series_map: ClassVar[dict[str, EiaSeries]] = {"oil_ulsd_nyh": FREE_SERIES["oil_ulsd_nyh"]}

    src = OneSeries(make_http(handler), "geheim")
    out = src.fetch("prices", OCT_20 - DAY, OCT_20 + DAY)
    assert [p[1] for p in out[0].points] == [5.173, 5.265]
    assert seen[0]["api_key"] == "geheim"
    assert seen[0]["facets[series][]"] == "EER_EPD2DXL0_PF4_Y35NY_DPG"
    assert [s["offset"] for s in seen] == ["0", "5000"]


def test_eia_api_error_body_is_format_error() -> None:
    """@relation(SWR-ENI-045, SWR-ENI-030, scope=function)"""
    with pytest.raises(FormatError):
        parse_api({"error": "x"}, FREE_SERIES["oil_ulsd_nyh"])


# --- EZB ---------------------------------------------------------------------------------


def test_ecb_parse() -> None:
    """@relation(SWR-ENI-047, scope=function)"""
    pts = dict(parse_csv((FIXTURES / "ecb_usd_eur.csv").read_text()))
    assert pts[datetime(2026, 10, 14, 22, 0, tzinfo=UTC)] == pytest.approx(1.1208)
    assert parse_csv("") == []
    with pytest.raises(FormatError):
        parse_csv("A,B\n1,2\n")


def test_ecb_fetch(make_http: MakeHttp) -> None:
    """@relation(SWR-ENI-047, scope=function)"""
    body = (FIXTURES / "ecb_usd_eur.csv").read_text()
    src = Ecb(make_http(lambda req: httpx.Response(200, text=body)))
    out = src.fetch("fx", OCT_20 - 7 * DAY, OCT_20)
    assert out[0].code == "fx_usd_eur"
    assert all(ts < OCT_20 for ts, _ in out[0].points)
    assert Ecb(make_http(lambda req: httpx.Response(404))).fetch("fx", OCT_20, OCT_20 + DAY) == []


# --- Weltbank ----------------------------------------------------------------------------


def test_worldbank_parse() -> None:
    """@relation(SWR-ENI-048, scope=function)"""
    out = {d.code: d for d in parse_worldbank(_bytes("worldbank_monthly.xlsx"))}
    assert set(out) == {
        "oil_brent_m",
        "oil_wti_m",
        "oil_dubai_m",
        "coal_au_m",
        "coal_za_m",
        "gas_henry_hub_m",
        "gas_eu_m",
        "lng_japan_m",
    }
    sep = datetime(2026, 8, 31, 22, 0, tzinfo=UTC)  # 01.09.2026 MESZ
    assert dict(out["oil_brent_m"].points)[sep] == pytest.approx(116.8)
    assert dict(out["gas_eu_m"].points)[sep] == pytest.approx(25.42)
    assert out["gas_eu_m"].resolution == MONTH


def test_worldbank_format_change() -> None:
    """@relation(SWR-ENI-048, SWR-ENI-030, scope=function)"""
    raw = _worldbank_header_changed()
    with pytest.raises(FormatError, match="Natural gas, Europe"):
        parse_worldbank(raw)
    with pytest.raises(FormatError, match="xlsx"):
        parse_worldbank(b"<html>Seite nicht gefunden</html>")


def _worldbank_header_changed() -> bytes:
    wb = load_workbook(io.BytesIO(_bytes("worldbank_monthly.xlsx")))
    ws = wb["Monthly Prices"]
    for row in ws.iter_rows():
        for cell in row:
            if cell.value == "Natural gas, Europe":
                cell.value = "Natural gas, Europe (TTF)"
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


def test_worldbank_downloads_once(make_http: MakeHttp) -> None:
    """@relation(SWR-ENI-048, scope=function)"""
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req.url)
        return httpx.Response(200, content=_bytes("worldbank_monthly.xlsx"))

    src = WorldBank(make_http(handler), "https://example.org/pink.xlsx")
    a = datetime(2025, 1, 1, tzinfo=UTC)
    src.fetch("prices", a, a + timedelta(days=200))
    src.fetch("prices", a + timedelta(days=200), a + timedelta(days=400))
    assert len(calls) == 1


# --- Oil Bulletin ------------------------------------------------------------------------


def test_oil_bulletin_parse() -> None:
    """@relation(SWR-ENI-049, scope=function)"""
    out = {d.code: d for d in parse_oil_bulletin(_bytes("oil_bulletin_history.xlsx"))}
    assert set(out) == {
        f"retail_de_{p}{n}"
        for p in ["euro95", "diesel", "heating_oil", "lpg"]
        for n in ["", "_net"]
    }
    oct_19 = datetime(2026, 10, 18, 22, 0, tzinfo=UTC)
    assert dict(out["retail_de_diesel"].points)[oct_19] == pytest.approx(2.266)
    assert dict(out["retail_de_heating_oil"].points)[oct_19] == pytest.approx(1.7128)
    assert dict(out["retail_de_lpg_net"].points)[oct_19] == pytest.approx(0.5993781512605042)
    assert out["retail_de_euro95"].resolution == WEEK


def test_oil_bulletin_format_change() -> None:
    """@relation(SWR-ENI-049, SWR-ENI-030, scope=function)"""
    raw = _bytes("oil_bulletin_history.xlsx")
    # Einheitenzeile der DE-Dieselspalte (Zeile 3, Spalte 56)
    with pytest.raises(FormatError, match="Einheit"):
        parse_oil_bulletin(_xlsx_without(raw, "Prices with taxes", 3, 56))
    # Spaltenkopf der DE-Benzinspalte
    with pytest.raises(FormatError, match="DE_price_wo_tax_euro95"):
        parse_oil_bulletin(_xlsx_without(raw, "Prices wo taxes", 1, 55))


def test_oil_bulletin_http_error(make_http: MakeHttp) -> None:
    """@relation(SWR-ENI-049, SWR-ENI-031, scope=function)"""
    src = OilBulletin(make_http(lambda req: httpx.Response(404)))
    with pytest.raises(Exception, match="404"):
        src.fetch("retail_de", OCT_20 - 30 * DAY, OCT_20)


# --- THE ---------------------------------------------------------------------------------


def test_the_parse() -> None:
    """@relation(SWR-ENI-050, scope=function)"""
    out = {d.code: d for d in parse_prices(_bytes("the_prices.xml"))}
    assert set(out) == {"gas_the_vtp", "gas_the_imbalance_pos", "gas_the_imbalance_neg"}
    day = datetime(2026, 10, 22, 4, 0, tzinfo=UTC)  # Gastag 22.10. ab 06:00 MESZ
    assert dict(out["gas_the_vtp"].points)[day] == pytest.approx(89.008)
    # positiver Ausgleichsenergiepreis (Unterdeckung) ist der höhere
    assert dict(out["gas_the_imbalance_pos"].points)[day] == pytest.approx(90.788)
    assert dict(out["gas_the_imbalance_neg"].points)[day] == pytest.approx(86.6)


def test_the_format_change() -> None:
    """@relation(SWR-ENI-050, SWR-ENI-030, scope=function)"""
    body = _bytes("the_prices.xml").replace(b"VTPDailyAveragePrice>", b"VTPPrice>")
    with pytest.raises(FormatError, match="VTPDailyAveragePrice"):
        parse_prices(body)
    with pytest.raises(FormatError):
        parse_prices(b"<html/>")


def test_the_fetch_window(make_http: MakeHttp) -> None:
    """@relation(SWR-ENI-050, scope=function)"""
    seen: list[dict[str, str]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(dict(req.url.params))
        return httpx.Response(200, content=_bytes("the_prices.xml"))

    src = The(make_http(handler))
    start = datetime(2026, 10, 21, 4, 0, tzinfo=UTC)
    out = src.fetch("gas_prices", start, start + DAY)
    assert {ts for d in out for ts, _ in d.points} == {start}
    assert seen[0] == {"startDate": "2026-10-20", "endDate": "2026-10-22"}
    assert (
        src.fetch("gas_prices", datetime(2020, 1, 1, tzinfo=UTC), datetime(2021, 1, 1, tzinfo=UTC))
        == []
    )


# --- gemeinsam ---------------------------------------------------------------------------


def test_unfree_fuel_sources_need_permission(monkeypatch: pytest.MonkeyPatch) -> None:
    """@relation(SWR-ENI-003, scope=function)"""
    for name in ["ALLOW_EIA_REUTERS", "ALLOW_THE"]:
        monkeypatch.delenv(f"PDB_{name}", raising=False)
        monkeypatch.delenv(name, raising=False)
    settings = EnergySettings(_env_file=None)
    assert settings.allowed("eia") and settings.allowed("worldbank")
    assert not settings.allowed("eia_reuters")
    assert not settings.allowed("the")


def test_published_estimates() -> None:
    """@relation(SWR-ENI-021, scope=function)"""
    assert estimate_published(Kind.PRICE_DAILY, OCT_20, DAY) == OCT_20 + 2 * DAY
    assert estimate_published(Kind.PRICE_WEEKLY, OCT_20, WEEK) == OCT_20 + WEEK
    sep = datetime(2026, 8, 31, 22, 0, tzinfo=UTC)
    assert estimate_published(Kind.PRICE_MONTHLY, sep, MONTH) == datetime(
        2026, 10, 7, 22, 0, tzinfo=UTC
    )
