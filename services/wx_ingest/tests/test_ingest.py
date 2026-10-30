import hashlib
import json
from datetime import datetime, timedelta
from typing import Any

import httpx
import numpy as np
import obstore
from obstore.store import MemoryStore

from gribkit import BBox, iter_messages
from wx_ingest.bronze import Bronze
from wx_ingest.gfs import nomads_params
from wx_ingest.ingest import GfsIngestor, RunResult
from wx_ingest.testing import RUN, FakeGfsHost, make_client, make_config

ALL_STEPS = {0, 1, 2, 3, 6, 9, 12}


def ingestor(bronze: Bronze, client: httpx.AsyncClient, **kw: Any) -> GfsIngestor:
    return GfsIngestor(make_config(**kw.pop("cfg", {})), bronze, client, retry_wait_max=0.01, **kw)


async def test_full_run_stored_and_cropped(bronze: Bronze) -> None:
    """@relation(SWR-WXI-014, SWR-WXI-020, SWR-WXI-021, scope=function)"""
    aws = FakeGfsHost("aws.test", ALL_STEPS)
    async with make_client(aws) as client:
        result = await ingestor(bronze, client).ingest_run(RUN)
    assert result.complete
    assert result.counts() == {"stored": 7}

    grib = bytes(
        await (
            await obstore.get_async(bronze.store, "gfs/0p25/2026/10/20/06/f003.grib2")
        ).bytes_async()
    )
    msgs = list(iter_messages(grib))
    assert len(msgs) == 4  # TMP, TCDC (Momentan + Mittel), UGRD 100 m

    import eccodes as ec

    h = ec.codes_new_from_message(msgs[0])
    assert (ec.codes_get(h, "Ni"), ec.codes_get(h, "Nj")) == (21, 15)
    lats = ec.codes_get_array(h, "latitudes")
    # Nachricht 3 der Datei (TMP) hat Werte 300 + lat
    np.testing.assert_allclose(ec.codes_get_values(h), 300 + lats, atol=0.01)
    ec.codes_release(h)

    m = await bronze.step_manifest(RUN, 3)
    assert m is not None
    assert m.endpoint == "aws" and m.n_messages == 4
    assert m.sha256 == hashlib.sha256(grib).hexdigest() and m.size == len(grib)
    assert m.messages[0] == "TMP:2 m above ground:3 hour fcst"
    run = await bronze.run_manifest(RUN)
    assert run is not None
    assert run["steps"] == 7 and run["endpoints"] == {"aws": 7}

    # je Schritt zwei Range-Anfragen: TMP allein (DPT liegt dazwischen),
    # beide TCDC-Nachrichten und UGRD zusammenhängend
    ranges = [r.headers["range"] for r in aws.requests if "range" in r.headers]
    assert len(ranges) == 7 * 2


async def test_second_run_is_idempotent(bronze: Bronze) -> None:
    """@relation(SWR-WXI-022, scope=function)"""
    aws = FakeGfsHost("aws.test", ALL_STEPS)
    async with make_client(aws) as client:
        ing = ingestor(bronze, client)
        await ing.ingest_run(RUN)
        before = len(aws.requests)
        result = await ing.ingest_run(RUN)
    assert result.complete and result.counts() == {"skipped": 7}
    assert len(aws.requests) == before


async def test_changed_message_list_reloads(bronze: Bronze) -> None:
    """@relation(SWR-WXI-022, scope=function)"""
    aws = FakeGfsHost("aws.test", ALL_STEPS)
    async with make_client(aws) as client:
        await ingestor(bronze, client).ingest_run(RUN)
        result = await ingestor(
            bronze, client, cfg={"messages": [{"var": "TMP", "level": "2 m above ground"}]}
        ).ingest_run(RUN)
    assert result.counts() == {"stored": 7}


async def test_partial_publication_stops_and_resumes(bronze: Bronze) -> None:
    """@relation(SWR-WXI-003, scope=function)"""
    aws = FakeGfsHost("aws.test", {0, 1, 2})
    async with make_client(aws) as client:
        result = await ingestor(bronze, client).ingest_run(RUN)
    assert not result.complete
    assert [s.status for s in result.steps] == ["stored"] * 3 + ["pending"] * 4
    # nach dem ersten fehlenden Schritt nicht mehr alle weiteren abgefragt
    idx_requests = [r for r in aws.requests if r.url.path.endswith(".idx")]
    assert len(idx_requests) < 7
    assert await bronze.run_manifest(RUN) is None

    aws_later = FakeGfsHost("aws.test", ALL_STEPS)
    async with make_client(aws_later) as client:
        result = await ingestor(bronze, client).ingest_run(RUN)
    assert result.complete
    assert result.counts() == {"skipped": 3, "stored": 4}


async def test_fallback_when_range_not_honoured(bronze: Bronze) -> None:
    """@relation(SWR-WXI-010, SWR-WXI-014, scope=function)"""
    aws = FakeGfsHost("aws.test", ALL_STEPS, honour_range=False)
    gcs = FakeGfsHost("gcs.test", ALL_STEPS)
    async with make_client(aws, gcs) as client:
        result = await ingestor(bronze, client).ingest_run(RUN, [0])
    assert result.steps[0].status == "stored" and result.steps[0].endpoint == "gcs"


async def test_stale_idx_realigned_from_whole_file(bronze: Bronze) -> None:
    """@relation(SWR-WXI-015, scope=function)"""
    aws = FakeGfsHost("aws.test", ALL_STEPS, stale_idx=True)
    gcs = FakeGfsHost("gcs.test", set())
    async with make_client(aws, gcs) as client:
        result = await ingestor(bronze, client).ingest_run(RUN, [0])
    assert result.steps[0].status == "stored" and result.steps[0].endpoint == "aws"
    assert any(r.url.path.endswith("f000") and "range" not in r.headers for r in aws.requests)
    # gleiches Ergebnis wie mit passender .idx
    clean = Bronze(MemoryStore())
    async with make_client(FakeGfsHost("aws.test", ALL_STEPS), gcs) as client:
        await ingestor(clean, client).ingest_run(RUN, [0])
    got, want = await bronze.step_manifest(RUN, 0), await clean.step_manifest(RUN, 0)
    assert got is not None and want is not None and got.sha256 == want.sha256


async def test_fallback_when_primary_lacks_step(bronze: Bronze) -> None:
    """@relation(SWR-WXI-010, scope=function)"""
    aws = FakeGfsHost("aws.test", set())
    gcs = FakeGfsHost("gcs.test", ALL_STEPS)
    async with make_client(aws, gcs) as client:
        result = await ingestor(bronze, client).ingest_run(RUN)
    assert result.complete and {s.endpoint for s in result.steps} == {"gcs"}


async def test_server_errors_are_retried_then_failed(bronze: Bronze) -> None:
    """@relation(SWR-WXI-011, scope=function)"""
    aws = FakeGfsHost("aws.test", ALL_STEPS, fail_status=503)
    gcs = FakeGfsHost("gcs.test", set())
    async with make_client(aws, gcs) as client:
        result = await ingestor(bronze, client).ingest_run(RUN, [0, 1])
    assert [s.status for s in result.steps] == ["failed", "failed"]
    assert "503" in result.steps[0].errors[0]
    assert len([r for r in aws.requests if r.url.path.endswith("f000.idx")]) == 4


async def test_truncated_transfer_is_rejected(bronze: Bronze) -> None:
    """@relation(SWR-WXI-014, scope=function)"""

    class Truncating(FakeGfsHost):
        def handle(self, request: httpx.Request) -> httpx.Response:
            resp = super().handle(request)
            if resp.status_code == 206:
                return httpx.Response(206, content=resp.content[:-100])
            return resp

    aws = Truncating("aws.test", ALL_STEPS)
    gcs = FakeGfsHost("gcs.test", set())
    async with make_client(aws, gcs) as client:
        result = await ingestor(bronze, client).ingest_run(RUN, [0])
    assert result.steps[0].status == "failed"
    assert await bronze.step_manifest(RUN, 0) is None


async def test_backfill_uses_archive_endpoints_only(bronze: Bronze) -> None:
    """@relation(SWR-WXI-030, scope=function)"""
    cfg = {
        "runs": [6],
        "endpoints": [
            {"name": "live", "kind": "nomads_filter", "url": "https://nomads.test/filter"},
            {"name": "aws", "kind": "idx_range", "url": "https://aws.test", "archive": True},
        ],
    }
    aws = FakeGfsHost("aws.test", ALL_STEPS)
    async with make_client(aws) as client:
        results = await ingestor(bronze, client, cfg=cfg, archive_only=True).backfill(RUN, RUN)
    assert len(results) == 1 and results[0].complete


async def test_backfill_selects_runs_newest_first(bronze: Bronze) -> None:
    """@relation(SWR-WXI-030, scope=function)"""
    aws = FakeGfsHost("aws.test", ALL_STEPS)
    async with make_client(aws) as client:
        ing = ingestor(bronze, client, archive_only=True)
        results = await ing.backfill(
            RUN - timedelta(hours=12), RUN, hours=[0, 6], newest_first=True
        )
    assert [f"{r.run:%d%H}" for r in results] == ["2006", "2000"]
    assert results[0].complete
    # Älterer Lauf fehlt im Archiv: bleibt offen statt fehlerhaft
    assert not results[1].complete and results[1].counts() == {"pending": len(ALL_STEPS)}
    assert f"/gfs.{RUN:%Y%m%d}/06/" in aws.requests[0].url.path


async def test_backfill_parallel_runs_and_resume(bronze: Bronze) -> None:
    """@relation(SWR-WXI-012, SWR-WXI-030, scope=function)"""
    aws = FakeGfsHost("aws.test", ALL_STEPS)
    async with make_client(aws) as client:
        ing = ingestor(bronze, client, archive_only=True)
        first = await ing.backfill(RUN - timedelta(hours=18), RUN, parallel_runs=3)
        n = len(aws.requests)
        again = await ing.backfill(RUN - timedelta(hours=18), RUN, parallel_runs=3)
    assert [r.run for r in first] == [RUN - timedelta(hours=h) for h in (18, 12, 6, 0)]
    assert first[-1].complete and again[-1].complete
    # vollständiger Lauf: keine Anfrage mehr an den Spiegel
    assert len(aws.requests) - n == 3 * len(ALL_STEPS)


async def test_backfill_skips_runs_with_silver(bronze: Bronze) -> None:
    """@relation(SWR-WXI-031, scope=function)"""
    aws = FakeGfsHost("aws.test", ALL_STEPS)

    async def has_silver(run: datetime) -> bool:
        return run == RUN

    async with make_client(aws) as client:
        ing = ingestor(bronze, client, archive_only=True)
        results = await ing.backfill(RUN - timedelta(hours=6), RUN, skip_run=has_silver)
    assert results[1].complete and results[1].counts() == {"skipped": len(ALL_STEPS)}
    assert all(f"/gfs.{RUN:%Y%m%d}/06/" not in r.url.path for r in aws.requests)
    assert await bronze.run_manifest(RUN) is None


async def test_silver_marker() -> None:
    """@relation(SWR-WXI-031, scope=function)"""
    import json

    import obstore
    from obstore.store import MemoryStore

    from wx_ingest.bronze import SilverMarker

    store = MemoryStore()
    marker = SilverMarker(store, make_config())
    assert not await marker.complete(RUN)
    obstore.put(store, marker.key(RUN), json.dumps({"attributes": {}}).encode())
    assert not await marker.complete(RUN)
    obstore.put(store, marker.key(RUN), json.dumps({"attributes": {"complete": True}}).encode())
    assert await marker.complete(RUN)


async def test_nomads_filter(bronze: Bronze) -> None:
    """@relation(SWR-WXI-013, scope=function)"""
    from gribkit.testing import make_global_message

    msg, _, _ = make_global_message()
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.params["file"].endswith("f001"):
            return httpx.Response(200, text="<html>data file is not present</html>")
        return httpx.Response(200, content=msg + msg)

    cfg = {
        "steps": [[0, 1, 1]],
        "endpoints": [
            {
                "name": "nomads",
                "kind": "nomads_filter",
                "url": "https://nomads.test/filter",
                "rate_per_min": 600,
            }
        ],
    }
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await ingestor(bronze, client, cfg=cfg).ingest_run(RUN)
    assert [s.status for s in result.steps] == ["stored", "pending"]
    p = next(r.url.params for r in seen if r.url.params["file"].endswith("f000"))
    assert p["dir"] == "/gfs.20261020/06/atmos" and p["file"] == "gfs.t06z.pgrb2.0p25.f000"
    assert (
        p["var_TMP"] == "on"
        and p["lev_2_m_above_ground"] == "on"
        and p["lev_entire_atmosphere"] == "on"
    )
    assert (p["toplat"], p["bottomlat"], p["leftlon"], p["rightlon"]) == ("58", "44", "0", "20")
    m = await bronze.step_manifest(RUN, 0)
    assert m is not None
    assert m.endpoint == "nomads" and m.n_messages == 2
    # NOMADS-Ausgabe wird auf Ausschnitt, Nord→Süd und CCSDS-Packung gebracht
    import eccodes as ec

    grib = bytes(
        await (await obstore.get_async(bronze.store, bronze.step_key(RUN, 0))).bytes_async()
    )
    first = next(iter(iter_messages(grib)))
    h = ec.codes_new_from_message(first)
    try:
        assert (ec.codes_get(h, "Ni"), ec.codes_get(h, "Nj")) == (21, 15)
        assert ec.codes_get(h, "jScansPositively") == 0
        assert ec.codes_get(h, "packingType") == "grid_ccsds"
    finally:
        ec.codes_release(h)


def test_nomads_params_negative_lon() -> None:
    """@relation(SWR-WXI-013, scope=function)"""
    p = nomads_params("pgrb2.0p25", RUN, 42, [], BBox(60, 40, -10.5, 30))
    assert p["leftlon"] == "-10.5" and p["file"] == "gfs.t06z.pgrb2.0p25.f042"


async def test_manifest_is_json(bronze: Bronze) -> None:
    """@relation(SWR-WXI-020, scope=function)"""
    aws = FakeGfsHost("aws.test", {0})
    async with make_client(aws) as client:
        await ingestor(bronze, client).ingest_run(RUN, [0])
    raw = bytes(
        await (
            await obstore.get_async(bronze.store, "gfs/0p25/2026/10/20/06/f000.json")
        ).bytes_async()
    )
    assert json.loads(raw)["bbox"] == {"north": 58, "south": 44, "west": 0, "east": 20}


async def test_backfill_reports_each_run(bronze: Bronze) -> None:
    """@relation(SWR-WXI-032, scope=function)"""
    aws = FakeGfsHost("aws.test", ALL_STEPS)
    seen: list[RunResult] = []

    async def on_run(r: RunResult) -> None:
        seen.append(r)

    async with make_client(aws) as client:
        ing = ingestor(bronze, client, archive_only=True)
        await ing.backfill(RUN - timedelta(hours=6), RUN, on_run=on_run, newest_first=True)
    assert [r.run for r in seen] == [RUN, RUN - timedelta(hours=6)]
    assert seen[0].complete and not seen[1].complete
