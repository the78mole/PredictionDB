import asyncio
import dataclasses
from pathlib import Path
from typing import Any

import httpx
import pytest
from obstore.store import MemoryStore

from gribkit import IdxEntry
from wx_ingest import gfs
from wx_ingest.bronze import Bronze
from wx_ingest.ingest import GfsIngestor
from wx_ingest.testing import GDEX_PATH, RUN, FakeGfsHost, make_client, make_config

STEPS = {0, 1, 2, 3, 6, 9, 12}
GDEX = {"name": "gdex", "kind": "header_scan", "url": "https://gdex.test", "archive": True}
SPECS = make_config().specs()


def ingestor(bronze: Bronze, client: httpx.AsyncClient, **endpoint: Any) -> GfsIngestor:
    ep = {**GDEX, "path": GDEX_PATH, **endpoint}
    return GfsIngestor(make_config(endpoints=[ep]), bronze, client, retry_wait_max=0.01)


async def _same_as_idx(bronze: Bronze, step: int) -> None:
    clean = Bronze(MemoryStore())
    aws = FakeGfsHost("aws.test", STEPS)
    async with make_client(aws) as client:
        await GfsIngestor(make_config(), clean, client).ingest_run(RUN, [step])
    got, want = await bronze.step_manifest(RUN, step), await clean.step_manifest(RUN, step)
    assert got is not None and want is not None
    assert got.sha256 == want.sha256 and got.messages == want.messages


@pytest.mark.parametrize("window", [256 * 1024, 4096])
async def test_run_from_headers(
    bronze: Bronze, monkeypatch: pytest.MonkeyPatch, window: int
) -> None:
    # kleines Fenster: Datei wird in Abschnitte geteilt, die Suche trifft mitten in Nachrichten
    monkeypatch.setattr(gfs, "SCAN_WINDOW", window)
    host = FakeGfsHost("gdex.test", STEPS, index=False)
    async with make_client(host) as client:
        result = await ingestor(bronze, client).ingest_run(RUN)
    assert result.complete and result.counts() == {"stored": 7}
    m = await bronze.step_manifest(RUN, 3)
    assert m is not None and m.endpoint == "gdex"
    assert m.messages == [
        "TMP:2 m above ground:3 hour fcst",
        "TCDC:entire atmosphere:3 hour fcst",
        "TCDC:entire atmosphere:0-3 hour ave fcst",
        "UGRD:100 m above ground:3 hour fcst",
    ]
    assert not any("range" not in r.headers for r in host.requests)
    await _same_as_idx(bronze, 3)


async def test_size_from_head_when_range_total_unknown(bronze: Bronze) -> None:
    host = FakeGfsHost("gdex.test", STEPS, index=False, unknown_size=True)
    async with make_client(host) as client:
        result = await ingestor(bronze, client).ingest_run(RUN, [3])
    assert result.steps[0].status == "stored"
    assert any(r.method == "HEAD" for r in host.requests)
    await _same_as_idx(bronze, 3)


async def test_missing_file_is_unavailable(bronze: Bronze) -> None:
    host = FakeGfsHost("gdex.test", {0}, index=False)
    async with make_client(host) as client:
        result = await ingestor(bronze, client).ingest_run(RUN, [0, 3])
    assert [s.status for s in result.steps] == ["stored", "pending"]


async def test_false_boundary_falls_back_to_whole_file(
    bronze: Bronze, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gfs, "SCAN_WINDOW", 4096)
    # Fehltreffer: angeblicher Anfang wenige Bytes hinter dem echten
    monkeypatch.setattr(gfs, "find_boundary", lambda chunk, left: 10)
    host = FakeGfsHost("gdex.test", STEPS, index=False)
    async with make_client(host) as client:
        result = await ingestor(bronze, client).ingest_run(RUN, [6])
    assert result.steps[0].status == "stored"
    assert any("range" not in r.headers for r in host.requests)
    await _same_as_idx(bronze, 6)


def test_header_scan_needs_path() -> None:
    with pytest.raises(ValueError, match="path"):
        make_config(endpoints=[GDEX])


async def _fetch(ep: gfs.Endpoint, step: int) -> gfs.StepData:
    cfg = make_config()
    return await ep.fetch_step("pgrb2.0p25", RUN, step, cfg.specs(), cfg.bbox.bbox())


async def test_template_finds_chosen_messages(monkeypatch: pytest.MonkeyPatch) -> None:
    # Zweiter Lauf desselben Schritts: nur Sprünge zu den gewählten Nachrichten statt aller Köpfe
    monkeypatch.setattr(gfs, "SCAN_WINDOW", 4096)
    host = FakeGfsHost("gdex.test", STEPS, index=False)
    async with make_client(host) as client:
        ing = ingestor(Bronze(MemoryStore()), client, segments=1)
        ep = ing.endpoints[0]
        full = await _fetch(ep, 6)
        n_full = len(host.requests)
        again = await _fetch(ep, 6)
    assert 6 in ep._templates
    assert again.messages == full.messages and again.grib == full.grib
    assert again.absent == full.absent
    # Köpfe 1 bis 7 vollständig, gezielt nur ab TMP (Nachricht 3)
    heads = [r for r in host.requests[n_full:] if r.headers["range"].endswith("-511")]
    assert len(heads) < 7


async def test_template_mismatch_rebuilds_inventory() -> None:
    host = FakeGfsHost("gdex.test", STEPS, index=False)
    async with make_client(host) as client:
        ep = ingestor(Bronze(MemoryStore()), client).endpoints[0]
        full = await _fetch(ep, 9)
        # Vorlage eines anderen Schritts: Schlüssel passen nicht (andere Prognosezeit)
        await _fetch(ep, 3)
        ep._templates[9] = list(ep._templates[3])
        again = await _fetch(ep, 9)
    assert again.messages == full.messages and again.grib == full.grib
    assert ep._templates[9][0].entries[2].key == "TMP:2 m above ground:9 hour fcst"
    assert len(ep._templates[9]) == 2  # die unpassende bleibt als weitere Variante


@pytest.mark.parametrize("factor", [0.5, 1.6])
async def test_template_with_other_lengths(monkeypatch: pytest.MonkeyPatch, factor: float) -> None:
    # Längen weichen ab: die Schätzung liegt daneben, weitere Sprünge finden die Nachricht
    monkeypatch.setattr(gfs, "SCAN_WINDOW", 1024)
    monkeypatch.setattr(gfs, "_MAX_LEAD", 0)
    host = FakeGfsHost("gdex.test", STEPS, index=False)
    async with make_client(host) as client:
        ep = ingestor(Bronze(MemoryStore()), client).endpoints[0]
        full = await _fetch(ep, 12)
        t = ep._templates[12][0]
        offsets = [int(e.offset * factor**i) for i, e in enumerate(t.entries)]
        skewed = [
            dataclasses.replace(e, offset=o, length=n - o)
            for e, o, n in zip(t.entries, offsets, [*offsets[1:], t.size], strict=True)
        ]
        ep._templates[12] = [gfs._Template(t.size, skewed)]
        again = await _fetch(ep, 12)
    assert again.messages == full.messages and again.grib == full.grib
    assert ep._templates[12][0].entries == skewed  # gezielt gefunden, nicht neu inventarisiert


async def test_template_skips_ambiguous_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    # Gewählte und benachbarte Nachrichten ohne eindeutigen Schlüssel: Sprung überspringt sie
    # oder läuft vom letzten bekannten Anfang
    monkeypatch.setattr(gfs, "SCAN_WINDOW", 1024)
    monkeypatch.setattr(gfs, "_MAX_LEAD", 0)
    index = gfs._Template.index

    def ambiguous(self: gfs._Template) -> dict[str, int]:
        return {k: i for k, i in index(self).items() if not k.startswith(("CLMR", "TMP", "DPT"))}

    monkeypatch.setattr(gfs._Template, "index", ambiguous)
    host = FakeGfsHost("gdex.test", STEPS, index=False)
    async with make_client(host) as client:
        ep = ingestor(Bronze(MemoryStore()), client).endpoints[0]
        full = await _fetch(ep, 6)
        template = ep._templates[6][0]
        again = await _fetch(ep, 6)
    assert again.messages == full.messages and again.grib == full.grib
    assert ep._templates[6] == [template]


async def test_template_overlapping_groups_once(monkeypatch: pytest.MonkeyPatch) -> None:
    # Jede gewählte Nachricht als eigene Gruppe mit Vorlauf: Gruppen überlappen sich
    monkeypatch.setattr(gfs, "_MAX_LEAD", 10)
    monkeypatch.setattr(gfs, "_groups", lambda indices: [(i, i) for i in indices])
    host = FakeGfsHost("gdex.test", STEPS, index=False)
    async with make_client(host) as client:
        ep = ingestor(Bronze(MemoryStore()), client).endpoints[0]
        full = await _fetch(ep, 9)
        template = ep._templates[9][0]
        again = await _fetch(ep, 9)
    assert again.messages == full.messages and again.grib == full.grib
    assert ep._templates[9] == [template]


async def test_second_template_variant_found_without_full_scan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Nach einem Modell-Update passt die neueste Vorlage nicht, eine ältere Variante schon
    scans: list[str] = []
    scan = gfs.Endpoint.scan

    async def counted(self: gfs.Endpoint, url: str) -> tuple[str, list[IdxEntry]]:
        scans.append(url)
        return await scan(self, url)

    monkeypatch.setattr(gfs.Endpoint, "scan", counted)
    host = FakeGfsHost("gdex.test", STEPS, index=False)
    async with make_client(host) as client:
        ep = ingestor(Bronze(MemoryStore()), client).endpoints[0]
        await _fetch(ep, 3)
        full = await _fetch(ep, 9)
        right = ep._templates[9][0]
        ep._templates[9] = [ep._templates[3][0], right]
        n = len(scans)
        again = await _fetch(ep, 9)
    assert len(scans) == n
    assert again.messages == full.messages and again.grib == full.grib
    assert ep._templates[9][0] is right  # die passende rückt nach vorn


async def test_parallel_runs_share_one_full_scan(monkeypatch: pytest.MonkeyPatch) -> None:
    scans: list[str] = []
    scan = gfs.Endpoint.scan

    async def counted(self: gfs.Endpoint, url: str) -> tuple[str, list[IdxEntry]]:
        scans.append(url)
        return await scan(self, url)

    monkeypatch.setattr(gfs.Endpoint, "scan", counted)
    host = FakeGfsHost("gdex.test", STEPS, index=False)
    async with make_client(host) as client:
        ep = ingestor(Bronze(MemoryStore()), client).endpoints[0]
        a, b = await asyncio.gather(_fetch(ep, 3), _fetch(ep, 3))
    assert a.messages == b.messages and a.grib == b.grib
    assert len(scans) == 1  # der zweite Abruf wartet und springt dann über die Vorlage


async def test_templates_travel_between_jobs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Ein Lauf baut die Vorlagen, die Datei aus der Konfiguration lässt andere sofort springen
    scans: list[str] = []
    scan = gfs.Endpoint.scan

    async def counted(self: gfs.Endpoint, url: str) -> tuple[str, list[IdxEntry]]:
        scans.append(url)
        return await scan(self, url)

    monkeypatch.setattr(gfs.Endpoint, "scan", counted)
    host = FakeGfsHost("gdex.test", {3, 6}, index=False)
    path = tmp_path / "gdex.templates.json.gz"
    async with make_client(host) as client:
        builder = ingestor(Bronze(MemoryStore()), client).endpoints[0]
        assert await builder.build_templates(RUN, [3, 6, 9], SPECS) == [3, 6]
        gfs.write_templates(path, "2026-10-20T00", builder.export_templates())
        full = await _fetch(builder, 6)
        n = len(scans)
        ep = {**GDEX, "path": GDEX_PATH}
        config = make_config(endpoints=[ep], templates=path)
        worker = GfsIngestor(config, Bronze(MemoryStore()), client).endpoints[0]
        again = await _fetch(worker, 6)
    assert len(scans) == n  # kein vollständiges Inventar im zweiten Job
    assert again.messages == full.messages and again.grib == full.grib
    assert gfs.read_templates(path) == builder.export_templates()


async def test_template_timeout_skips_step(monkeypatch: pytest.MonkeyPatch) -> None:
    scan = gfs.Endpoint.scan

    async def flaky(self: gfs.Endpoint, url: str) -> tuple[str, list[IdxEntry]]:
        if url.endswith("f006.grib2"):
            raise httpx.ReadTimeout("zu langsam")
        return await scan(self, url)

    monkeypatch.setattr(gfs.Endpoint, "scan", flaky)
    host = FakeGfsHost("gdex.test", {3, 6}, index=False)
    async with make_client(host) as client:
        ep = ingestor(Bronze(MemoryStore()), client).endpoints[0]
        assert await ep.build_templates(RUN, [3, 6], SPECS) == [3]


async def test_template_retries_failed_step_once(monkeypatch: pytest.MonkeyPatch) -> None:
    scan = gfs.Endpoint.scan
    calls: list[str] = []

    async def flaky_once(self: gfs.Endpoint, url: str) -> tuple[str, list[IdxEntry]]:
        calls.append(url)
        if url.endswith("f006.grib2") and calls.count(url) == 1:
            raise httpx.ReadTimeout("zu langsam")
        return await scan(self, url)

    monkeypatch.setattr(gfs.Endpoint, "scan", flaky_once)
    host = FakeGfsHost("gdex.test", {3, 6}, index=False)
    async with make_client(host) as client:
        ep = ingestor(Bronze(MemoryStore()), client).endpoints[0]
        assert await ep.build_templates(RUN, [3, 6], SPECS) == [3, 6]
