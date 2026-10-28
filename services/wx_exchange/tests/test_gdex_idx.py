import asyncio
import time

import httpx
from obstore.store import MemoryStore

from wx_exchange import idx
from wx_exchange.oci import Registry
from wx_exchange.testing import FakeRegistry
from wx_ingest.bronze import Bronze
from wx_ingest.gfs import Endpoint
from wx_ingest.ingest import GfsIngestor
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


class Flaky:
    """Archiv, das die ersten ``n`` Anfragen mit 503 beantwortet (wie OSDF unter Last)."""

    def __init__(self, host: FakeGfsHost, n: int) -> None:
        self.host, self.inner, self.n = host.host, host, n

    def handle(self, request: httpx.Request) -> httpx.Response:
        if self.n > 0:
            self.n -= 1
            return httpx.Response(503)
        return self.inner.handle(request)


async def build(
    reg: Registry, host: FakeGfsHost | Flaky, *, deadline: float | None = None
) -> idx.IdxLayer:
    async with make_client(host) as client:  # type: ignore[arg-type]
        endpoints = [Endpoint(ep, client, retry_wait_max=0.01) for ep in CONFIG.endpoints]
        return await idx.build_layer(
            reg, CONFIG, endpoints, MONTH, RUN.hour, RUN, RUN, deadline=deadline, retry_pause=0
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


async def test_failed_files_are_retried_in_the_same_job() -> None:
    reg = registry(FakeRegistry())
    # Je Datei scheitern erst alle Wiederholungen der ersten Anfrage
    layer = await build(reg, Flaky(FakeGfsHost("gdex.test", AVAILABLE, index=False), 40))
    assert layer.complete and layer.files == 3 and len(layer.absent) == 4


async def test_workers_split_files_and_load(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """``workers``: Dateien und Anfragen werden aufgeteilt, das Ergebnis bleibt dasselbe.

    Statt Prozessen laufen Threads mit eigener Ereignisschleife (der Testspiegel lebt nur im
    Testprozess); geprüft wird Aufteilen und Zusammenführen.
    """
    from concurrent.futures import ThreadPoolExecutor

    host = FakeGfsHost("gdex.test", AVAILABLE, index=False)
    seen: list[tuple[int, int, int]] = []

    def worker(config, items, label, parallel_files, deadline, retries, retry_pause):  # type: ignore[no-untyped-def]
        seen.append((len(items), config.endpoints[0].concurrency, parallel_files))

        async def go() -> idx._Scan:
            async with make_client(host) as client:
                endpoints = [Endpoint(ep, client, retry_wait_max=0.01) for ep in config.endpoints]
                return await idx._scan(
                    endpoints,
                    items,
                    label=label,
                    parallel_files=parallel_files,
                    deadline=deadline,
                    retries=retries,
                    retry_pause=retry_pause,
                )

        return asyncio.run(go())

    monkeypatch.setattr(idx, "_worker", worker)
    monkeypatch.setattr(
        idx, "ProcessPoolExecutor", lambda max_workers, mp_context: ThreadPoolExecutor(max_workers)
    )
    config = CONFIG.model_copy(deep=True)
    config.endpoints[0].concurrency = 24
    reg = registry(FakeRegistry())
    layer = await idx.build_layer(
        reg, config, [], MONTH, RUN.hour, RUN, RUN, retry_pause=0, workers=3
    )
    assert layer.complete and layer.files == 3 and len(layer.absent) == 4
    assert sorted(n for n, _, _ in seen) == [2, 2, 3]
    assert {(c, p) for _, c, p in seen} == {(8, 4)}
    assert config.endpoints[0].concurrency == 24  # Aufteilen ändert die Vorlage nicht


async def test_backfill_reads_idx_instead_of_headers() -> None:
    reg = registry(FakeRegistry())
    host = FakeGfsHost("gdex.test", AVAILABLE, index=False)
    idx.merge_manifest(reg, [await build(reg, host)], SOURCE)
    layer, files = idx.load_layer(reg, MONTH, RUN.hour)
    assert layer is not None and layer.complete and len(files) == 3
    assert idx.load_layer(reg, "1999-01", RUN.hour) == (None, {})

    async with make_client(host) as client:
        a, b = Bronze(MemoryStore()), Bronze(MemoryStore())
        want = await GfsIngestor(CONFIG, a, client, archive_only=True).ingest_step(RUN, 3)
        withidx = GfsIngestor(CONFIG, b, client, archive_only=True)
        assert withidx.import_idx(files) == 3
        before = len(host.requests)
        got = await withidx.ingest_step(RUN, 3)
    # keine Köpfe mehr, nur die Bereiche der gewählten Nachrichten
    ranges = [r.headers["range"] for r in host.requests[before:]]
    assert ranges and not any(r.endswith("-511") for r in ranges)
    assert got.status == want.status == "stored"
    ma, mb = await a.step_manifest(RUN, 3), await b.step_manifest(RUN, 3)
    assert ma is not None and mb is not None
    assert mb.sha256 == ma.sha256 and mb.messages == ma.messages


async def test_stale_idx_falls_back_to_headers() -> None:
    host = FakeGfsHost("gdex.test", AVAILABLE, index=False)
    # .idx passt nicht zur Datei: Offsets mitten in den Nachrichten
    (text,) = FakeGfsHost("aws.test", {3}).idx.values()
    rows = [line.split(":") for line in text.splitlines()]
    stale = "".join(":".join([r[0], str(int(r[1]) + 10), *r[2:]]) + "\n" for r in rows)
    name = GDEX_PATH.format(run=RUN, step=3) + ".idx"
    async with make_client(host) as client:
        ing = GfsIngestor(CONFIG, Bronze(MemoryStore()), client, archive_only=True)
        ing.import_idx({name: stale.encode()})
        result = await ing.ingest_step(RUN, 3)
    assert result.status == "stored"
    assert not ing.endpoints[0]._idx


async def test_broken_archive_files_are_not_retried() -> None:
    """Leere (HTTP 416) und abgeschnittene Dateien gelten als ``broken``, nicht ``failed``."""
    reg = registry(FakeRegistry())
    host = FakeGfsHost("gdex.test", AVAILABLE, index=False)
    empty, cut = ("/" + GDEX_PATH.format(run=RUN, step=s) for s in (3, 6))
    host.files[empty] = b""
    host.files[cut] = host.files[cut][:-100]
    layer = await build(reg, host)
    assert layer.complete and layer.files == 1 and not layer.failed
    assert layer.broken == [idx.file_code(RUN, 3), idx.file_code(RUN, 6)]
    summary = idx.merge_manifest(reg, [layer], SOURCE)
    assert summary["broken_files"] == 2
    assert idx.read_layers(reg)[layer.key].broken == layer.broken

    # Folgelauf mit --rebuild nicht nötig: defekte Dateien werden nicht erneut gelesen
    before = len(host.requests)
    again = await build(reg, host)
    assert len(host.requests) == before and again.broken == layer.broken
