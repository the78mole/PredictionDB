import hashlib
import json
from typing import Any

import httpx
import numpy as np
import obstore

from gribkit import BBox, iter_messages
from wx_ingest.bronze import Bronze
from wx_ingest.gfs import nomads_params
from wx_ingest.ingest import GfsIngestor
from wx_ingest.testing import RUN, FakeGfsHost, make_client, make_config

ALL_STEPS = {0, 1, 2, 3, 6, 9, 12}


def ingestor(bronze: Bronze, client: httpx.AsyncClient, **kw: Any) -> GfsIngestor:
    return GfsIngestor(make_config(**kw.pop("cfg", {})), bronze, client, retry_wait_max=0.01, **kw)


async def test_full_run_stored_and_cropped(bronze: Bronze) -> None:
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
    aws = FakeGfsHost("aws.test", ALL_STEPS)
    async with make_client(aws) as client:
        ing = ingestor(bronze, client)
        await ing.ingest_run(RUN)
        before = len(aws.requests)
        result = await ing.ingest_run(RUN)
    assert result.complete and result.counts() == {"skipped": 7}
    assert len(aws.requests) == before


async def test_changed_message_list_reloads(bronze: Bronze) -> None:
    aws = FakeGfsHost("aws.test", ALL_STEPS)
    async with make_client(aws) as client:
        await ingestor(bronze, client).ingest_run(RUN)
        result = await ingestor(
            bronze, client, cfg={"messages": [{"var": "TMP", "level": "2 m above ground"}]}
        ).ingest_run(RUN)
    assert result.counts() == {"stored": 7}


async def test_partial_publication_stops_and_resumes(bronze: Bronze) -> None:
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
    aws = FakeGfsHost("aws.test", ALL_STEPS, honour_range=False)
    gcs = FakeGfsHost("gcs.test", ALL_STEPS)
    async with make_client(aws, gcs) as client:
        result = await ingestor(bronze, client).ingest_run(RUN, [0])
    assert result.steps[0].status == "stored" and result.steps[0].endpoint == "gcs"


async def test_fallback_when_primary_lacks_step(bronze: Bronze) -> None:
    aws = FakeGfsHost("aws.test", set())
    gcs = FakeGfsHost("gcs.test", ALL_STEPS)
    async with make_client(aws, gcs) as client:
        result = await ingestor(bronze, client).ingest_run(RUN)
    assert result.complete and {s.endpoint for s in result.steps} == {"gcs"}


async def test_server_errors_are_retried_then_failed(bronze: Bronze) -> None:
    aws = FakeGfsHost("aws.test", ALL_STEPS, fail_status=503)
    gcs = FakeGfsHost("gcs.test", set())
    async with make_client(aws, gcs) as client:
        result = await ingestor(bronze, client).ingest_run(RUN, [0, 1])
    assert [s.status for s in result.steps] == ["failed", "failed"]
    assert "503" in result.steps[0].errors[0]
    assert len([r for r in aws.requests if r.url.path.endswith("f000.idx")]) == 4


async def test_truncated_transfer_is_rejected(bronze: Bronze) -> None:
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


async def test_nomads_filter(bronze: Bronze) -> None:
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
    p = nomads_params("pgrb2.0p25", RUN, 42, [], BBox(60, 40, -10.5, 30))
    assert p["leftlon"] == "-10.5" and p["file"] == "gfs.t06z.pgrb2.0p25.f042"


async def test_manifest_is_json(bronze: Bronze) -> None:
    aws = FakeGfsHost("aws.test", {0})
    async with make_client(aws) as client:
        await ingestor(bronze, client).ingest_run(RUN, [0])
    raw = bytes(
        await (
            await obstore.get_async(bronze.store, "gfs/0p25/2026/10/20/06/f000.json")
        ).bytes_async()
    )
    assert json.loads(raw)["bbox"] == {"north": 58, "south": 44, "west": 0, "east": 20}
