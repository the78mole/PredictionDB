"""Energiedaten freier Quellen je Jahr in GHCR (``wx_exchange.energy``)."""

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import httpx
import pandas as pd
import pytest

from energy_ingest.model import HOUR, Kind, SeriesData, SeriesInfo
from wx_exchange import energy
from wx_exchange.oci import Registry
from wx_exchange.testing import FakeRegistry

SOURCE = "https://github.com/owner/repo"
NOW = datetime(2026, 3, 20, tzinfo=UTC)


@dataclass
class FakeSource:
    """Stündlicher Preis und Wind-Erzeugung mit festem Wert, optional ein Fehler."""

    code: str
    priority: int
    price: float
    free: bool = True
    train: bool = True
    license: str = "CC BY 4.0, Test"
    fail: set[str] = field(default_factory=set)
    calls: int = 0

    def datasets(self) -> list[str]:
        return ["price", "wind"]

    def max_chunk(self, dataset: str) -> timedelta:
        return timedelta(days=30)

    def fetch(self, dataset: str, start: datetime, end: datetime) -> list[SeriesData]:
        self.calls += 1
        if dataset in self.fail:
            raise RuntimeError("Quelle antwortet nicht")
        hours = int((end - start) / HOUR)
        ts = [start + i * HOUR for i in range(hours)]
        if dataset == "price":
            info = SeriesInfo("da_price_de_lu", Kind.PRICE_DA, "EUR/MWh")
            return [SeriesData(info, HOUR, [(t, self.price) for t in ts])]
        info = SeriesInfo("gen_wind_onshore", Kind.ACTUAL, "MW")
        return [SeriesData(info, HOUR, [(t, 100.0) for t in ts])]


def registry(fake: FakeRegistry, *, write: bool = True) -> Registry:
    client = httpx.Client(transport=httpx.MockTransport(fake))
    return Registry(fake.host, fake.repository, client, "bot", "secret" if write else None, (0.0,))


def builder(fake: FakeRegistry, *sources: FakeSource) -> energy.EnergyBuilder:
    return energy.EnergyBuilder(
        registry(fake),
        list(sources),
        source_url=SOURCE,
        clock=lambda: NOW,
    )


def test_year_bounds_local() -> None:
    """@relation(SWR-WXX-080, scope=function)"""
    a, b = energy.year_bounds(2025)
    assert a == datetime(2024, 12, 31, 23, tzinfo=UTC)
    assert b == datetime(2025, 12, 31, 23, tzinfo=UTC)


def test_value_best_prefers_lower_rank() -> None:
    """@relation(SWR-WXX-081, scope=function)"""
    t = pd.Timestamp("2025-01-01", tz="UTC")
    a = pd.DataFrame({"code": ["x", "y"], "ts": [t, t], "value": [1.0, 2.0], "published": [t, t]})
    b = pd.DataFrame({"code": ["x"], "ts": [t], "value": [9.0], "published": [t]})
    best = energy.value_best([(30, b), (10, a)])
    assert dict(zip(best["code"], best["value"], strict=True)) == {"x": 1.0, "y": 2.0}


def test_build_year_publish_unchanged_and_stale() -> None:
    """@relation(SWR-WXX-080, SWR-WXX-081, SWR-WXX-082, scope=function)"""
    fake = FakeRegistry(repository="owner/data/energy")
    first = FakeSource("smard", 10, 50.0)
    second = FakeSource("energy_charts", 30, 70.0)
    result = builder(fake, first, second).build_year(2025)
    assert result.status == "published" and result.tag == "2025"
    m = fake.manifest("2025")
    titles = [d["annotations"]["org.opencontainers.image.title"] for d in m["layers"]]
    assert titles == [
        "energy_charts.parquet",
        "pdb_marktwert.parquet",
        "smard.parquet",
        "sources.json",
    ]
    a = m["annotations"]
    assert a["org.predictiondb.energy_version"] == energy.ENERGY_VERSION
    assert len(a["org.opencontainers.image.description"]) <= 512

    values, meta = energy.load_year(registry(fake, write=False), 2025)
    assert meta["smard"]["priority"] == 10 and meta["smard"]["series"]["da_price_de_lu"]["unit"]
    price = values["smard"].query("code == 'da_price_de_lu'")
    start, end = energy.year_bounds(2025)
    assert price["ts"].min() == start and price["ts"].max() == end - HOUR
    # Auktion um 13 Uhr Ortszeit am Vortag
    first_pub = price.sort_values("ts")["published"].iloc[0]
    assert first_pub == pd.Timestamp("2024-12-31 12:00", tz="UTC")
    # Marktwerte aus dem Preis der bevorzugten Quelle (SMARD, 50), alle Monate plus Jahr
    mw = values["pdb_marktwert"]
    assert set(mw["code"]) >= {"mw_base", "mw_wind_onshore", "jw_base"}
    assert mw.query("code == 'mw_base'")["value"].eq(50.0).all()
    assert len(mw.query("code == 'mw_base'")) == 12

    best = energy.load_best(registry(fake, write=False), [2025], ["da_price_de_lu"])
    assert best["value"].eq(50.0).all() and len(best) == len(price)

    puts = fake.manifest_puts()
    again = builder(fake, FakeSource("smard", 10, 50.0), FakeSource("energy_charts", 30, 70.0))
    assert again.build_year(2025).status == "unchanged"
    assert fake.manifest_puts() == puts

    # ein Datensatz scheitert: bisherige Werte der Quelle bleiben, Jahr gilt als veraltet
    broken = FakeSource("energy_charts", 30, 71.0, fail={"wind"})
    stale = builder(fake, FakeSource("smard", 10, 51.0), broken).build_year(2025)
    assert stale.status == "published"
    assert stale.failed == {"energy_charts.wind": "failed: RuntimeError: Quelle antwortet nicht"}
    assert fake.manifest("2025")["annotations"]["org.predictiondb.stale"] == "energy_charts"
    values, _ = energy.load_year(registry(fake, write=False), 2025)
    assert values["energy_charts"].query("code == 'da_price_de_lu'")["value"].eq(70.0).all()
    assert energy.plan(registry(fake), 2025, 2025, datetime(2027, 6, 1, tzinfo=UTC)) == [2025]

    # ohne bisherige Werte: nichts veröffentlichen
    empty = FakeRegistry(repository="owner/data/energy")
    failed = builder(empty, FakeSource("smard", 10, 50.0, fail={"price"})).build_year(2024)
    assert failed.status == "failed" and empty.manifest_puts() == 0


def test_plan_years() -> None:
    """@relation(SWR-WXX-082, scope=function)"""
    fake = FakeRegistry(repository="owner/data/energy")
    for y in (2023, 2024, 2025):
        builder(fake, FakeSource("smard", 10, 50.0)).build_year(y)
    reg = registry(fake, write=False)
    # laufendes Jahr immer, Vorjahr bis Februar, fehlende Jahre
    assert energy.plan(reg, 2022, 9999, datetime(2025, 2, 10, tzinfo=UTC)) == [2025, 2024, 2022]
    assert energy.plan(reg, 2022, 9999, datetime(2025, 3, 10, tzinfo=UTC)) == [2025, 2022]
    assert energy.plan(reg, 2024, 2025, NOW, rebuild=True) == [2025, 2024]


def test_unfree_source_refused() -> None:
    """@relation(SWR-WXX-080, scope=function)"""
    src = FakeSource("entsoe", 20, 1.0, free=False)
    with pytest.raises(ValueError, match="nicht frei"):
        energy.fetch_source(src, *energy.year_bounds(2025), NOW)


@dataclass
class Window(FakeSource):
    """Merkt sich den frühesten abgefragten Zeitpunkt."""

    first: datetime | None = None

    def fetch(self, dataset: str, start: datetime, end: datetime) -> list[SeriesData]:
        self.first = start if self.first is None else min(self.first, start)
        return super().fetch(dataset, start, end)


def test_current_year_incremental_and_partial_sources() -> None:
    """@relation(SWR-WXX-083, scope=function)"""
    fake = FakeRegistry(repository="owner/data/energy")
    builder(fake, FakeSource("smard", 10, 50.0), FakeSource("energy_charts", 30, 70.0)).build_year(
        2026
    )
    start, _ = energy.year_bounds(2026)
    # laufendes Jahr: nur der Rückblick wird neu abgerufen, ältere Werte bleiben
    w = Window("smard", 10, 55.0)
    assert builder(fake, w).build_year(2026).status == "published"
    assert w.first is not None and w.first >= NOW - timedelta(days=energy.INCREMENTAL_DAYS + 31)
    assert w.first > start
    values, meta = energy.load_year(registry(fake, write=False), 2026)
    price = values["smard"].query("code == 'da_price_de_lu'").sort_values("ts")
    assert price["value"].iloc[0] == 50.0 and price["value"].iloc[-1] == 55.0
    # energy_charts wurde nicht abgerufen und bleibt unverändert im Artefakt
    ec = values["energy_charts"].query("code == 'da_price_de_lu'")
    assert ec["value"].eq(70.0).all() and "energy_charts" in meta
    # vollständig: wieder ab Jahresbeginn
    w = Window("smard", 10, 56.0)
    builder(fake, w).build_year(2026, full=True)
    assert w.first == start


def test_redact_and_earliest_published() -> None:
    """@relation(SWR-WXX-080, scope=function)"""
    msg = "Server error for url 'https://web-api.tp.entsoe.eu/api?securityToken=abc-123&x=1'"
    assert "abc-123" not in energy.redact(msg) and "securityToken=***&x=1" in energy.redact(msg)
    assert energy.redact("api_key=XYZ") == "api_key=***"
    t = pd.Timestamp("2025-01-01", tz="UTC")
    rows = pd.DataFrame(
        {
            "code": ["x", "x"],
            "ts": [t, t],
            "value": [1.0, 2.0],
            "published": [t, t + pd.Timedelta(days=5)],
        }
    )
    merged = energy.merge_rows(rows)
    assert merged["value"].tolist() == [2.0] and merged["published"].tolist() == [t]


def test_secret_in_error_not_published() -> None:
    """@relation(SWR-WXX-080, scope=function)"""

    @dataclass
    class Leaky(FakeSource):
        def fetch(self, dataset: str, start: datetime, end: datetime) -> list[SeriesData]:
            raise RuntimeError("url 'https://x.example/api?securityToken=geheim'")

    layer = energy.fetch_source(Leaky("entsoe_last", 20, 1.0), *energy.year_bounds(2025), NOW)
    assert all("geheim" not in s for s in layer.status.values())


def test_fallback_marktwert_and_stale_kept() -> None:
    """@relation(SWR-WXX-080, SWR-WXX-082, scope=function)"""
    fake = FakeRegistry(repository="owner/data/energy")
    builder(fake, FakeSource("smard", 10, 50.0)).build_year(2025)
    # Preis scheitert, Wind kommt neu: SMARD bleibt vollständig alt, Marktwerte passen dazu
    r = builder(fake, FakeSource("smard", 10, 60.0, fail={"price"})).build_year(2025)
    assert r.status == "published"
    values, meta = energy.load_year(registry(fake, write=False), 2025)
    assert values["pdb_marktwert"].query("code == 'mw_base'")["value"].eq(50.0).all()
    assert meta["smard"]["status"]["price"].startswith("failed")
    # Lauf nur mit einer anderen Quelle: SMARD bleibt veraltet und das Jahr fällig
    builder(fake, FakeSource("energy_charts", 30, 70.0)).build_year(2025)
    assert fake.manifest("2025")["annotations"]["org.predictiondb.stale"] == "smard"
    assert energy.plan(registry(fake), 2025, 2025, datetime(2027, 6, 1, tzinfo=UTC)) == [2025]
    # Erholung mit denselben Werten: nur der Zustand ändert sich, trotzdem ein neues Manifest
    puts = fake.manifest_puts()
    ok = builder(fake, FakeSource("smard", 10, 50.0), FakeSource("energy_charts", 30, 70.0))
    assert ok.build_year(2025).status == "published" and fake.manifest_puts() == puts + 1
    assert fake.manifest("2025")["annotations"]["org.predictiondb.stale"] == ""


def test_duplicate_sources_rejected() -> None:
    """@relation(SWR-WXX-080, scope=function)"""
    fake = FakeRegistry(repository="owner/data/energy")
    with pytest.raises(ValueError, match="doppelt"):
        builder(fake, FakeSource("smard", 10, 1.0), FakeSource("smard", 10, 2.0))


def test_gie_key_not_sent_to_other_host() -> None:
    """@relation(SWR-WXX-080, scope=function)"""
    from wx_exchange.cli_energy import _strip_foreign_keys

    seen: dict[str, str | None] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen[request.url.host] = request.headers.get("x-key")
        if request.url.host == "agsi.gie.eu":
            return httpx.Response(302, headers={"Location": "https://evil.example/x"})
        return httpx.Response(200, json={})

    client = httpx.Client(
        transport=httpx.MockTransport(handler),
        follow_redirects=True,
        event_hooks={"request": [_strip_foreign_keys]},
    )
    client.get("https://agsi.gie.eu/api", headers={"x-key": "k"})
    assert seen == {"agsi.gie.eu": "k", "evil.example": None}
