"""Gasspeicher: GIE AGSI+ und Eurostat.

``eurostat_nrg_stk_gasm.json`` ist eine echte Antwort vom 25.10.2026 (ohne ``extension``),
``agsi_de.json`` folgt dem Aufbau aus der GIE-API-Dokumentation v013 (Zahlen als
Zeichenketten, ``-`` für fehlende Werte, Status ``N`` ohne Daten).
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
from energy_ingest.model import DAY, MONTH, SERIES, Kind, estimate_published, series
from energy_ingest.sources import FormatError, SourceError
from energy_ingest.sources.eurostat import Eurostat
from energy_ingest.sources.eurostat import parse_json as parse_eurostat
from energy_ingest.sources.gie_agsi import GieAgsi, parse_page

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
    assert storage[GAS_DAY_22] == pytest.approx(193.0621)
    # Status N entfällt, "-" ergibt keinen Wert
    assert len(storage) == 3
    assert len(out["gas_injection"]) == 2
    assert dict(out["gas_storage_full"])[GAS_DAY_22] == pytest.approx(76.75)
    assert dict(out["gas_withdrawal"])[GAS_DAY_22] == pytest.approx(20.11)
    assert dict(out["gas_storage_wgv"])[GAS_DAY_22] == pytest.approx(251.5401)


def test_agsi_format_errors() -> None:
    body = _json("agsi_de.json")
    del body["data"][0]["gasInStorage"]
    with pytest.raises(FormatError, match="Felder fehlen"):
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
    # Fenster [21.10., 23.10.): 23.10. fällt heraus, 20.10. hat Status N
    assert [ts for ts, _ in by_code["gas_storage_de"].points] == [GAS_DAY_22 - DAY, GAS_DAY_22]
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
    assert info.kind is Kind.GAS_DAY_STORAGE
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


# --- Eurostat ----------------------------------------------------------------------------


def test_eurostat_parse() -> None:
    out = parse_eurostat(_json("eurostat_nrg_stk_gasm.json"))
    assert set(out) == {"DE", "AT", "NL", "CZ", "EU27_2020"}
    de = dict(out["DE"])
    # Bestand Ende Januar 2026 steht am 1. Februar
    feb_1 = datetime(2026, 1, 31, 23, 0, tzinfo=UTC)
    assert de[feb_1] == pytest.approx(285548.0 / 3600)
    assert len(out["DE"]) == 8 and len(out["AT"]) == 9


def test_eurostat_format_errors() -> None:
    body = _json("eurostat_nrg_stk_gasm.json")
    body["dimension"]["unit"]["category"]["index"] = {"MIO_M3": 0}
    with pytest.raises(FormatError, match="unit"):
        parse_eurostat(body)
    body = _json("eurostat_nrg_stk_gasm.json")
    del body["dimension"]
    with pytest.raises(FormatError, match="unerwartete Antwort"):
        parse_eurostat(body)


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
