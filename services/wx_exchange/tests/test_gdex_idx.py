import time

import httpx

from wx_exchange import idx
from wx_exchange.oci import Registry
from wx_exchange.testing import FakeRegistry
from wx_ingest.gfs import Endpoint
from wx_ingest.testing import GDEX_PATH, RUN, FakeGfsHost, make_client, make_config

SOURCE = "https://github.com/the78mole/predictiondb"
MONTH = f"{RUN:%Y-%m}"
GDEX = {"name": "gdex", "kind": "header_scan", "url": "https://gdex.test", "archive": True}
CONFIG = make_config(endpoints=[{**GDEX, "path": GDEX_PATH}])
#: Schritte der Testkonfiguration 0, 1, 2, 3, 6, 9, 12; im Archiv nur diese
AVAILABLE = {0, 3, 6}


def registry(fake: FakeRegistry) -> Registry:
    client = httpx.Client(transport=httpx.MockTransport(fake))
    return Registry(fake.host, fake.repository, client, "bot", "secret", (0.0, 0.0))


async def build(reg: Registry, host: FakeGfsHost, *, deadline: float | None = None) -> idx.IdxLayer:
    async with make_client(host) as client:
        endpoints = [Endpoint(ep, client, retry_wait_max=0.01) for ep in CONFIG.endpoints]
        return await idx.build_layer(
            reg, CONFIG, endpoints, MONTH, RUN.hour, RUN, RUN, deadline=deadline
        )


def test_pack_is_reproducible() -> None:
    files = {"b/x.idx": b"2:9:d=1:B:s:anl:\n", "a/x.idx": b"1:0:d=1:A:s:anl:\n"}
    data = idx.pack(files)
    assert data == idx.pack(dict(reversed(files.items())))
    assert idx.unpack(data) == files


async def test_failed_months_are_completed_by_a_follow_up() -> None:
    fake = FakeRegistry()
    reg = registry(fake)
    host = FakeGfsHost("gdex.test", AVAILABLE, index=False)

    # Erster Lauf: Zeit ist sofort um, alle Dateien schlagen fehl; das Manifest kommt trotzdem
    first = await build(reg, host, deadline=time.monotonic() - 1)
    assert first.files == 0 and len(first.failed) == 7 and not first.complete
    summary = idx.merge_manifest(reg, [first], SOURCE)
    assert summary["layers"] == 1 and summary["complete"] == 0
    assert idx.plan(reg, CONFIG, RUN, RUN, [RUN.hour]) == [
        {"month": MONTH, "hour": RUN.hour, "runs": 1}
    ]

    # Folgelauf: nur der unvollständige Layer, danach vollständig
    second = await build(reg, host)
    assert second.complete and second.files == 3 and len(second.absent) == 4
    assert idx.merge_manifest(reg, [second], SOURCE)["updated"] == [second.key]
    assert idx.plan(reg, CONFIG, RUN, RUN, [RUN.hour]) == []

    m = fake.manifest(idx.TAG)
    (desc,) = m["layers"]
    assert desc["annotations"]["org.predictiondb.month"] == MONTH
    assert desc["annotations"]["org.predictiondb.complete"] == "true"
    files = idx.unpack(fake.blobs[desc["digest"]])
    name = GDEX_PATH.format(run=RUN, step=3) + ".idx"
    lines = files[name].decode().splitlines()
    assert len(lines) == 7 and lines[0].startswith("1:0:d=") and lines[2].split(":")[3] == "TMP"

    # Nichts mehr offen: kein Abruf im Archiv, gleicher Blob
    before = len(host.requests)
    third = await build(reg, host)
    assert len(host.requests) == before and third.digest == second.digest
    assert idx.merge_manifest(reg, [third], SOURCE)["updated"] == []


async def test_follow_up_keeps_built_files_and_other_layers() -> None:
    fake = FakeRegistry()
    reg = registry(fake)
    host = FakeGfsHost("gdex.test", AVAILABLE, index=False)
    done = await build(reg, host)
    other = idx.IdxLayer(month="2015-01", hour=0, digest=done.digest, size=done.size, runs=1)
    idx.merge_manifest(reg, [done, other], SOURCE)

    # Eine Datei fällt nachträglich aus dem Stand: nur sie wird neu gelesen
    name = GDEX_PATH.format(run=RUN, step=6) + ".idx"
    partial = idx.unpack(fake.blobs[done.digest])
    del partial[name]
    blob = idx.pack(partial)
    digest = reg.push_blob(blob)
    broken = idx.IdxLayer(
        month=MONTH,
        hour=RUN.hour,
        digest=digest,
        size=len(blob),
        runs=1,
        files=2,
        absent=done.absent,
        failed=[idx.file_code(RUN, 6)],
    )
    idx.merge_manifest(reg, [broken], SOURCE)
    before = len(host.requests)
    again = await build(reg, host)
    paths = {r.url.path for r in host.requests[before:]}
    assert paths == {"/" + name.removesuffix(".idx")}
    assert again.digest == done.digest

    summary = idx.merge_manifest(reg, [again], SOURCE)
    assert summary["layers"] == 2 and summary["complete"] == 2
    keys = [d["annotations"]["org.predictiondb.month"] for d in fake.manifest(idx.TAG)["layers"]]
    assert keys == ["2015-01", MONTH]


async def test_manifest_skips_layers_without_blob() -> None:
    fake = FakeRegistry()
    reg = registry(fake)
    ghost = idx.IdxLayer(month=MONTH, hour=0, digest="sha256:" + "0" * 64, size=1, runs=1)
    summary = idx.merge_manifest(reg, [ghost], SOURCE)
    assert summary["skipped"] == [ghost.key] and summary["layers"] == 0
