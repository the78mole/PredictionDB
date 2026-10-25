"""Gasspeicher, LNG, Gas-, Öl- und Kohlebilanz: GIE AGSI+, GIE ALSI, Eurostat.

Alle Fixtures sind echte Antworten vom 25.10.2026 (Eurostat ohne ``extension``);
``alsi_de_2022.json`` zeigt Gastage ohne Daten (Status ``N``, Werte ``-``).
"""

import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from energy_ingest.config import EnergySettings
from energy_ingest.http import Http
from energy_ingest.model import (
    COAL_BALANCE,
    DAY,
    MONTH,
    OIL_IMPORT_PARTNERS,
    SERIES,
    Kind,
    estimate_published,
    series,
)
from energy_ingest.sources import FormatError, SourceError
from energy_ingest.sources.eurostat import TABLES, Eurostat
from energy_ingest.sources.eurostat import parse_json as parse_eurostat
from energy_ingest.sources.gie import ALSI_FIELDS, GieAgsi, GieAlsi, parse_page

FIXTURES = Path(__file__).parent / "fixtures"
Handler = Callable[[httpx.Request], httpx.Response]
MakeHttp = Callable[[Handler], Http]

GAS_DAY_22 = datetime(2026, 10, 22, 4, 0, tzinfo=UTC)  # Gastag 22.10.2026, 06:00 MESZ


def _json(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text())


# --- AGSI+ -------------------------------------------------------------------------------


def test_agsi_parse_page() -> None:
    last, out = parse_page(_json("agsi_de.json"))
    assert last == 1
    storage = dict(out["gas_storage"])
    assert len(storage) == 7
    assert storage[GAS_DAY_22] == pytest.approx(151.2647)
    assert dict(out["gas_storage_full"])[GAS_DAY_22] == pytest.approx(61.15)
    assert dict(out["gas_injection"])[GAS_DAY_22] == pytest.approx(268.04)
    assert dict(out["gas_withdrawal"])[GAS_DAY_22] == pytest.approx(114.9)
    assert dict(out["gas_storage_wgv"])[GAS_DAY_22] == pytest.approx(247.3563)


def test_agsi_missing_values_and_status_n() -> None:
    body = _json("agsi_de.json")
    body["data"][0]["status"] = "N"  # 24.10. ohne Daten
    body["data"][2]["injection"] = "-"  # 22.10. ohne Einspeisung
    _, out = parse_page(body)
    assert len(out["gas_storage"]) == 6
    assert GAS_DAY_22 not in dict(out["gas_injection"])
    assert GAS_DAY_22 in dict(out["gas_withdrawal"])


def test_agsi_format_errors() -> None:
    body = _json("agsi_de.json")
    del body["data"][0]["gasInStorage"]
    with pytest.raises(FormatError, match="gasInStorage"):
        parse_page(body)
    body = _json("agsi_de.json")
    body["data"][0]["status"] = "X"
    with pytest.raises(FormatError, match="Status"):
        parse_page(body)
    body = _json("agsi_de.json")
    body["data"][0]["full"] = "voll"
    with pytest.raises(FormatError, match="nicht lesbar"):
        parse_page(body)


def test_agsi_bad_key_is_source_error() -> None:
    body = {
        "last_page": 0,
        "total": 0,
        "dataset": "storage ERROR",
        "error": "access denied",
        "message": "Invalid or missing API key",
        "data": [],
    }
    with pytest.raises(SourceError, match="access denied"):
        parse_page(body)


def test_agsi_fetch_pages_and_params(make_http: MakeHttp) -> None:
    seen: list[httpx.Request] = []
    body = _json("agsi_de.json")

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        page = int(req.url.params["page"])
        rows = body["data"][:2] if page == 1 else body["data"][2:]
        return httpx.Response(200, json={**body, "last_page": 2, "data": rows})

    src = GieAgsi(make_http(handler), "geheim")
    out = src.fetch("storage_de", GAS_DAY_22 - 2 * DAY, GAS_DAY_22 + DAY)
    assert [r.url.params["page"] for r in seen] == ["1", "2"]
    assert seen[0].headers["x-key"] == "geheim"
    assert seen[0].url.params["country"] == "DE"
    assert seen[0].url.params["size"] == "300"
    by_code = {s.code: s for s in out}
    assert set(by_code) == {
        "gas_storage_de",
        "gas_storage_full_de",
        "gas_injection_de",
        "gas_withdrawal_de",
        "gas_storage_wgv_de",
    }
    # Fenster [20.10., 23.10.): Gastage 20. bis 22.10.
    days = [ts for ts, _ in by_code["gas_storage_de"].points]
    assert days == [GAS_DAY_22 - 2 * DAY, GAS_DAY_22 - DAY, GAS_DAY_22]
    assert by_code["gas_storage_de"].resolution == DAY


def test_agsi_eu_uses_type(make_http: MakeHttp) -> None:
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200, json={**_json("agsi_de.json"), "dataset": "EU"})

    out = GieAgsi(make_http(handler), "k").fetch("storage_eu", GAS_DAY_22, GAS_DAY_22 + DAY)
    assert seen[0].url.params["type"] == "eu"
    assert "country" not in seen[0].url.params
    assert {s.code for s in out} >= {"gas_storage_eu"}


def test_agsi_published() -> None:
    # Gastag 22.10. (Beginn 06:00) erscheint am 23.10. um 19:30, angenommen 20:00 MESZ
    info = series("gas_storage_de")
    assert info.kind is Kind.GAS_DAY
    assert estimate_published(info.kind, GAS_DAY_22, DAY) == datetime(
        2026, 10, 23, 18, 0, tzinfo=UTC
    )


def test_agsi_needs_key() -> None:
    assert "gie_agsi" in EnergySettings().sources
    assert EnergySettings().allowed("gie_agsi")
    assert GieAgsi.free and GieAgsi.datasets(GieAgsi.__new__(GieAgsi)) == [
        "storage_de",
        "storage_at",
        "storage_nl",
        "storage_cz",
        "storage_eu",
    ]


# --- ALSI --------------------------------------------------------------------------------


def test_alsi_parse_page() -> None:
    last, out = parse_page(_json("alsi_de.json"), ALSI_FIELDS)
    assert last == 1
    inventory = dict(out["lng_inventory"])
    assert len(inventory) == 5
    assert inventory[GAS_DAY_22 - DAY] == pytest.approx(1006.87)  # GWh, nicht 10³ m³ LNG
    assert dict(out["lng_sendout"])[GAS_DAY_22 - DAY] == pytest.approx(229.5)
    assert dict(out["lng_dtmi"])[GAS_DAY_22] == pytest.approx(5158.76)
    assert dict(out["lng_dtrs"])[GAS_DAY_22] == pytest.approx(844)


def test_alsi_no_data_days() -> None:
    _, out = parse_page(_json("alsi_de_2022.json"), ALSI_FIELDS)
    assert all(not pts for pts in out.values())


def test_alsi_nested_field_missing() -> None:
    body = _json("alsi_de.json")
    del body["data"][0]["inventory"]["gwh"]
    with pytest.raises(FormatError, match=r"inventory\.gwh"):
        parse_page(body, ALSI_FIELDS)


def test_alsi_fetch(make_http: MakeHttp) -> None:
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200, json=_json("alsi_de.json"))

    src = GieAlsi(make_http(handler), "geheim")
    assert src.datasets() == ["lng_de", "lng_nl", "lng_be", "lng_fr", "lng_eu"]
    assert src.sync_lookback["lng_de"] == timedelta(days=14)
    out = src.fetch("lng_de", GAS_DAY_22 - DAY, GAS_DAY_22 + DAY)
    assert seen[0].url.host == "alsi.gie.eu"
    assert seen[0].headers["x-key"] == "geheim"
    assert seen[0].url.params["country"] == "DE"
    assert {s.code for s in out} == {
        "lng_inventory_de",
        "lng_sendout_de",
        "lng_dtmi_de",
        "lng_dtrs_de",
    }
    assert all(s.info.kind is Kind.GAS_DAY for s in out)


# --- Eurostat ----------------------------------------------------------------------------


def test_eurostat_parse() -> None:
    out = parse_eurostat(_json("eurostat_nrg_stk_gasm.json"), TABLES["gas_stock"])
    assert {geo for geo, _ in out} == {"DE", "AT", "NL", "CZ", "EU27_2020"}
    de = dict(out[("DE", "STKCL_NAT")])
    # Bestand Ende Januar 2026 steht am 1. Februar
    feb_1 = datetime(2026, 1, 31, 23, 0, tzinfo=UTC)
    assert de[feb_1] == pytest.approx(285548.0 / 3600)
    assert len(out[("DE", "STKCL_NAT")]) == 8 and len(out[("AT", "STKCL_NAT")]) == 9


def test_eurostat_format_errors() -> None:
    body = _json("eurostat_nrg_stk_gasm.json")
    body["dimension"]["unit"]["category"]["index"] = {"MIO_M3": 0}
    with pytest.raises(FormatError, match="unit"):
        parse_eurostat(body, TABLES["gas_stock"])
    body = _json("eurostat_nrg_stk_gasm.json")
    del body["dimension"]
    with pytest.raises(FormatError, match="unerwartete Antwort"):
        parse_eurostat(body, TABLES["gas_stock"])


def test_eurostat_fetch(make_http: MakeHttp) -> None:
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200, json=_json("eurostat_nrg_stk_gasm.json"))

    start = datetime(2026, 2, 28, 23, 0, tzinfo=UTC)  # 1.3.2026 00:00 MEZ
    out = Eurostat(make_http(handler)).fetch("gas_stock", start, start + timedelta(days=60))
    params = seen[0].url.params
    assert params.get_list("geo") == ["DE", "AT", "NL", "CZ", "EU27_2020"]
    assert params["stk_flow"] == "STKCL_NAT" and params["unit"] == "TJ_GCV"
    assert params["sinceTimePeriod"] == "2026-01"
    by_code = {s.code: s for s in out}
    assert set(by_code) == {f"gas_storage_{a}_m" for a in ["de", "at", "nl", "cz", "eu"]}
    de = by_code["gas_storage_de_m"]
    assert de.resolution == MONTH
    # 1.3. (Ende Februar) und 1.4. (Ende März) liegen im Fenster
    assert len(de.points) == 2


def test_eurostat_published() -> None:
    info = SERIES["gas_storage_de_m"]
    assert info.kind is Kind.STOCK_MONTHLY
    ts = datetime(2026, 9, 30, 22, 0, tzinfo=UTC)
    assert estimate_published(info.kind, ts, MONTH) == ts + timedelta(days=90)


def test_eurostat_balance() -> None:
    table = TABLES["gas_balance"]
    out = parse_eurostat(_json("eurostat_nrg_cb_gasm.json"), table)
    june = datetime(2026, 5, 31, 22, 0, tzinfo=UTC)  # Juni 2026, Monatsbeginn
    de = {k: dict(v) for (geo, k), v in out.items() if geo == "DE"}
    # Verbrauch = Förderung + Import - Export - Speicheränderung (Einspeicherung positiv)
    supply = de["IPRD"][june] + de["IMP"][june] - de["EXP"][june] - de["STK_CHG_MG"][june]
    assert de["IC_OBS"][june] == pytest.approx(supply, abs=0.01)
    assert de["IMP"][june] == pytest.approx(83.59, abs=0.01)


def test_eurostat_import_origin_fetch(make_http: MakeHttp) -> None:
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200, json=_json("eurostat_nrg_ti_gasm.json"))

    start = datetime(2026, 4, 30, 22, 0, tzinfo=UTC)  # Mai 2026
    out = Eurostat(make_http(handler)).fetch("gas_import_origin", start, start + 92 * DAY)
    params = seen[0].url.params
    assert seen[0].url.path.endswith("/nrg_ti_gasm")
    assert params.get_list("geo") == ["DE", "EU27_2020"]
    assert "NSP" in params.get_list("partner")
    assert params["sinceTimePeriod"] == "2026-05"
    by_code = {s.code: s for s in out}
    assert "gas_import_de_no_m" in by_code and "gas_import_eu_us_m" in by_code
    jun = datetime(2026, 5, 31, 22, 0, tzinfo=UTC)
    assert dict(by_code["gas_import_de_no_m"].points)[jun] == pytest.approx(37.39, abs=0.01)
    assert by_code["gas_import_de_no_m"].info.kind is Kind.FLOW_MONTHLY


def test_eurostat_flow_published() -> None:
    may = datetime(2026, 4, 30, 22, 0, tzinfo=UTC)
    june = datetime(2026, 5, 31, 22, 0, tzinfo=UTC)
    assert estimate_published(Kind.FLOW_MONTHLY, may, MONTH) == june + timedelta(days=90)


def test_eurostat_unknown_partner() -> None:
    body = _json("eurostat_nrg_ti_gasm.json")
    idx = body["dimension"]["partner"]["category"]["index"]
    idx["XX"] = idx.pop("NO")
    with pytest.raises(FormatError, match="fremde partner"):
        parse_eurostat(body, TABLES["gas_import_origin"])


# --- Eurostat: Öl und Steinkohle -----------------------------------------------------------


def test_eurostat_crude_import_price(make_http: MakeHttp) -> None:
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200, json=_json("eurostat_nrg_ti_coifpm.json"))

    start = datetime(2025, 12, 31, 23, 0, tzinfo=UTC)  # Januar 2026
    out = Eurostat(make_http(handler)).fetch("oil_crude_import", start, start + 273 * DAY)
    params = seen[0].url.params
    assert seen[0].url.path.endswith("/nrg_ti_coifpm")
    assert params["crudeoil"] == "TOTAL" and params.get_list("geo") == ["DE"]
    assert params.get_list("indic_nrg") == ["AVGPRC_USD_BBL", "VOL_THS_BBL"]
    by_code = {s.code: s for s in out}
    price = by_code["oil_crude_import_price_de_m"]
    assert price.info.kind is Kind.PRICE_MONTHLY_STAT and price.info.unit == "USD/bbl"
    aug = datetime(2026, 7, 31, 22, 0, tzinfo=UTC)
    # nicht umgerechnet: USD/bbl bzw. kbbl wie bei Eurostat
    assert dict(price.points)[aug] == pytest.approx(94.054)
    assert dict(by_code["oil_crude_import_volume_de_m"].points)[aug] == pytest.approx(44709.542)
    assert estimate_published(price.info.kind, aug, MONTH) == datetime(
        2026, 8, 31, 22, 0, tzinfo=UTC
    ) + timedelta(days=90)


def test_eurostat_crude_balance_matches_stock() -> None:
    bal = parse_eurostat(_json("eurostat_nrg_cb_oilm.json"), TABLES["oil_crude_balance"])
    stk = parse_eurostat(_json("eurostat_nrg_stk_oilm.json"), TABLES["oil_stock"])
    change = dict(bal[("DE", "STK_CHG")])
    stock = dict(stk[("DE", "O4100_TOT")])
    # Bestandsänderung im Januar = Bestand Ende Januar (am 1.2.) - Ende Dezember (am 1.1.)
    jan, feb = datetime(2025, 12, 31, 23, 0, tzinfo=UTC), datetime(2026, 1, 31, 23, 0, tzinfo=UTC)
    assert change[jan] == pytest.approx(stock[feb] - stock[jan], abs=1.0)
    assert dict(bal[("DE", "IMP")])[jan] == pytest.approx(6797.96, abs=0.01)


def test_eurostat_oil_kinds() -> None:
    assert SERIES["oil_crude_import_de_m"].kind is Kind.FLOW_MONTHLY
    assert SERIES["oil_crude_import_eu_m"].kind is Kind.FLOW_MONTHLY_SLOW
    assert SERIES["oil_stock_products_de_m"].kind is Kind.STOCK_MONTHLY
    assert SERIES["oil_stock_crude_eu_m"].kind is Kind.STOCK_MONTHLY_SLOW
    assert SERIES["coal_hard_power_heat_de_m"].kind is Kind.FLOW_MONTHLY_SLOW
    assert "coal_hard_stock_de_m" not in SERIES
    may = datetime(2026, 4, 30, 22, 0, tzinfo=UTC)
    june = datetime(2026, 5, 31, 22, 0, tzinfo=UTC)
    assert estimate_published(Kind.FLOW_MONTHLY_SLOW, may, MONTH) == june + timedelta(days=150)
    assert estimate_published(Kind.STOCK_MONTHLY_SLOW, june, MONTH) == june + timedelta(days=150)


def test_eurostat_oil_import_origin(make_http: MakeHttp) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.params["siec"] == "O4100_TOT"
        return httpx.Response(200, json=_json("eurostat_nrg_ti_oilm.json"))

    start = datetime(2025, 12, 31, 23, 0, tzinfo=UTC)
    out = Eurostat(make_http(handler)).fetch("oil_import_origin", start, start + 273 * DAY)
    by_code = {s.code: s for s in out}
    assert set(by_code) == {f"oil_crude_import_de_{p}_m" for p in OIL_IMPORT_PARTNERS}
    aug = datetime(2026, 7, 31, 22, 0, tzinfo=UTC)
    assert dict(by_code["oil_crude_import_de_ly_m"].points)[aug] == pytest.approx(1108.136)
    assert dict(by_code["oil_crude_import_de_ru_m"].points)[aug] == 0.0


def test_eurostat_coal(make_http: MakeHttp) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        stock = req.url.params.get_list("nrg_bal") == ["STKCL_NAT"]
        name = "eurostat_nrg_cb_sffm_stock.json" if stock else "eurostat_nrg_cb_sffm.json"
        return httpx.Response(200, json=_json(name))

    es = Eurostat(make_http(handler))
    start = datetime(2025, 12, 31, 23, 0, tzinfo=UTC)
    bal = {s.code: s for s in es.fetch("coal_balance", start, start + 273 * DAY)}
    assert set(bal) == {f"coal_hard_{b}_{a}_m" for b in COAL_BALANCE for a in ["de", "eu"]}
    july = datetime(2026, 6, 30, 22, 0, tzinfo=UTC)
    de = {c: dict(s.points) for c, s in bal.items() if c.endswith("_de_m")}
    assert de["coal_hard_import_de_m"][july] == pytest.approx(1973.376)
    assert de["coal_hard_power_heat_de_m"][july] < de["coal_hard_import_de_m"][july]
    stock = es.fetch("coal_stock", start, start + 273 * DAY)
    assert [s.code for s in stock] == ["coal_hard_stock_eu_m"]
    assert stock[0].info.unit == "kt"


@pytest.mark.parametrize("status", [200, 403])
def test_gie_error_hides_key(make_http: MakeHttp, status: int) -> None:
    key = "0123456789abcdef0123456789abcdef"

    def handler(req: httpx.Request) -> httpx.Response:
        body = {"last_page": 0, "error": f"key {key} blocked", "message": key, "data": []}
        return httpx.Response(status, json=body)

    with pytest.raises(SourceError) as info:
        GieAgsi(make_http(handler), key).fetch("storage_de", GAS_DAY_22, GAS_DAY_22 + DAY)
    assert key not in str(info.value)
    assert "***" in str(info.value)
    assert info.value.__cause__ is None and info.value.__suppress_context__
