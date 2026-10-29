import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import obstore
import pytest
from obstore.store import MemoryStore

from wx_exchange import cli
from wx_exchange.artifact import (
    MonthArtifact,
    content_hash,
    fingerprint,
    pack,
    parse_tag,
    tag,
    unpack,
)
from wx_exchange.importer import Importer
from wx_exchange.oci import Registry, ServerError
from wx_exchange.publish import Publisher, months_between, plan
from wx_exchange.testing import FakeRegistry
from wx_ingest.config import GfsConfig
from wx_ingest.testing import RUN, FakeGfsHost, make_client, make_config
from wx_process.config import ProcessConfig
from wx_process.pipeline import WxPipeline
from wx_process.silver import Silver
from wx_process.testing import fill_bronze, grid, make_step, source_config
from wx_process.variables import SILVER_VERSION

REPO = Path(__file__).resolve().parents[3]

PROCESS: dict[str, Any] = {
    "source": "gfs_0p25",
    "source_config": "unused.yaml",
    "weight_grid": "gfs_0p25",
    "technologies": {"wind_on": {"kind": "wind"}},
}


def host(*runs: datetime) -> FakeGfsHost:
    return StepHost("aws.test", available={0, 1, 2, 3}, runs=set(runs or [RUN]))


def registry(fake: FakeRegistry, *, write: bool = True) -> Registry:
    client = httpx.Client(transport=httpx.MockTransport(fake))
    return Registry(
        fake.host, fake.repository, client, "bot", "secret" if write else None, (0.0, 0.0)
    )


def test_tag_roundtrip() -> None:
    assert tag("2026-10", 6) == "2026-10-06z"
    assert parse_tag("2026-10-06z") == ("2026-10", 6)
    assert parse_tag("latest") is None
    assert parse_tag("2026-10-05z") is None


def test_months_between() -> None:
    a = datetime(2025, 11, 3, tzinfo=UTC)
    b = datetime(2026, 2, 1, tzinfo=UTC)
    assert months_between(a, b) == ["2025-11", "2025-12", "2026-01", "2026-02"]


def test_registry_blob_manifest_and_tags() -> None:
    fake = FakeRegistry(page_size=2)
    reg = registry(fake)
    d = reg.push_blob(b"hello")
    assert reg.push_blob(b"hello") == d  # vorhanden: kein zweiter Upload
    assert fake.blob_puts() == 1
    for t in ("2026-01-00z", "2026-02-00z", "2026-03-00z"):
        reg.put_manifest(t, {"layers": [{"digest": d}], "config": {}})
    assert reg.tags() == ["2026-01-00z", "2026-02-00z", "2026-03-00z"]
    assert reg.blob(d) == b"hello"
    # anonym lesen geht, schreiben nicht
    ro = registry(fake, write=False)
    assert ro.manifest("2026-01-00z") is not None
    assert ro.manifest("2099-01-00z") is None


def test_push_blob_retries_server_errors() -> None:
    fake = FakeRegistry(fail_uploads=2)
    d = registry(fake).push_blob(b"hello")
    assert fake.blobs[d] == b"hello" and fake.blob_puts() == 3
    fake = FakeRegistry(fail_uploads=3)
    with pytest.raises(ServerError):
        registry(fake).push_blob(b"hello")


def _silver(run: datetime = RUN, completed: str = "2099-01-02T10:00:00+00:00") -> MemoryStore:
    bronze, silver = MemoryStore(), MemoryStore()
    fill_bronze(bronze, [0, 1, 2, 3], run)
    summary = json.loads(bytes(obstore.get(bronze, f"gfs/0p25/{run:%Y/%m/%d/%H}/run.json").bytes()))
    summary["completed_at"] = completed
    obstore.put(bronze, f"gfs/0p25/{run:%Y/%m/%d/%H}/run.json", json.dumps(summary).encode())
    cfg = ProcessConfig.model_validate(PROCESS)
    from wx_ingest.config import GfsConfig

    src = GfsConfig.model_validate(source_config([(0, 3, 1)]))
    WxPipeline(cfg, src, bronze, Silver(silver), None).process(run)
    return silver


def test_pack_is_reproducible_and_ignores_build_time() -> None:
    key = Silver.key("gfs", "0p25", RUN)
    a = _silver(completed="2099-01-02T10:00:00+00:00")
    b = _silver(completed="2099-01-03T11:00:00+00:00")
    tar_a, hash_a = pack(a, key)
    tar_a2, _ = pack(a, key)
    tar_b, hash_b = pack(b, key)
    assert tar_a == tar_a2
    assert tar_a != tar_b  # Zeitpunkt des Bronze-Abschlusses steht im Silber
    assert hash_a == hash_b  # zählt aber nicht zum Inhalt

    target = MemoryStore()
    unpack(tar_a, target, key)
    attrs = Silver(target).attrs("gfs", "0p25", RUN)
    assert attrs is not None and attrs["silver_version"] == SILVER_VERSION
    files = {
        m["path"][len(key) + 1 :]: bytes(obstore.get(target, m["path"]).bytes())
        for batch in obstore.list(target, prefix=key + "/")
        for m in batch
    }
    assert content_hash(files) == hash_a


@dataclass
class StepHost(FakeGfsHost):
    """GFS-Spiegel mit echten Schritten (nur ``TMP`` 2 m) für die Läufe in ``runs``."""

    runs: set[datetime] = field(default_factory=set)

    def __post_init__(self) -> None:
        lat, lon = grid()
        for day in (20, 21):
            for hh in (0, 6):
                run = datetime(2026, 10, day, hh, tzinfo=UTC)
                if run not in self.runs:
                    continue
                for step in self.available:
                    base = f"/gfs.{run:%Y%m%d}/{run:%H}/atmos/gfs.t{run:%H}z.pgrb2.0p25.f{step:03d}"
                    data = make_step(step, lat, lon, names={"t2m"})
                    self.files[base] = data
                    self.idx[base + ".idx"] = (
                        f"1:0:d={run:%Y%m%d%H}:TMP:2 m above ground:{step} hour fcst:\n"
                    )


def _publisher(fake: FakeRegistry, gfs: FakeGfsHost, tmp_path: Path) -> Publisher:
    config = make_config(steps=[[0, 3, 1]], messages=[{"var": "TMP", "level": "2 m above ground"}])
    return Publisher(
        registry(fake),
        config,
        ProcessConfig.model_validate(PROCESS),
        make_client(gfs),
        tmp_path,
        source_url="https://github.com/owner/repo",
    )


@pytest.mark.parametrize("hour", [6])
async def test_publish_month_uploads_once_and_records_missing(tmp_path: Path, hour: int) -> None:
    fake = FakeRegistry()
    gfs = host()
    pub = _publisher(fake, gfs, tmp_path)
    start, end = RUN, datetime(2026, 10, 21, 6, tzinfo=UTC)  # 20. vorhanden, 21. fehlt

    result = await pub.publish_month("2026-10", hour, start, end)
    assert result.runs == {"2026102006": "uploaded", "2026102106": "missing"}
    art = MonthArtifact.from_manifest("2026-10", hour, fake.manifest("2026-10-06z"))
    assert set(art.layers) == {RUN}
    assert art.missing == {datetime(2026, 10, 21, 6, tzinfo=UTC)}
    assert art.layers[RUN].fingerprint == fingerprint(pub.config)
    # Bronze und Silber des Laufs sind danach gelöscht (Zarr lässt nur leere Elterngruppen)
    left = [p for p in tmp_path.rglob("*") if p.is_file()]
    assert not [p for p in left if "bronze" in p.parts or ".zarr" in str(p)]

    # zweiter Aufruf: nichts zu tun, keine Anfrage an GFS
    n = len(gfs.requests)
    again = await pub.publish_month("2026-10", hour, start, end)
    assert again.runs == {} and again.skipped == 2
    assert len(gfs.requests) == n

    # Neubau mit gleichem Inhalt: kein Upload, kein neues Manifest
    blobs, manifests = fake.blob_puts(), fake.manifest_puts()
    rebuilt = await pub.publish_month("2026-10", hour, start, end, rebuild=True)
    assert rebuilt.runs == {"2026102006": "unchanged"}
    assert fake.blob_puts() == blobs and fake.manifest_puts() == manifests


async def test_plan_lists_only_gaps(tmp_path: Path) -> None:
    fake = FakeRegistry()
    gfs = host()
    pub = _publisher(fake, gfs, tmp_path)
    await pub.publish_month("2026-10", 6, RUN, RUN)

    config = pub.config
    start, end = RUN, datetime(2026, 11, 30, 18, tzinfo=UTC)
    blocks = plan(registry(fake), config, start, end, [6, 0], block_years=1, blocks=2)
    first = [(e["month"], e["hour"]) for e in blocks[0]]
    # 06Z vor 00Z, jüngste Monate zuerst; Oktober 06Z hat nach dem Lauf noch Lücken (21. bis 31.)
    assert first == [("2026-11", 6), ("2026-10", 6), ("2026-11", 0), ("2026-10", 0)]
    assert blocks[1] == []
    oct6 = next(e for e in blocks[0] if e["month"] == "2026-10" and e["hour"] == 6)
    assert oct6["todo"] == 11

    # nur der eine Lauf: nichts mehr offen
    only = plan(registry(fake), config, RUN, RUN, [6], blocks=1)
    assert only == [[]]


async def test_import_fills_silver_and_skips_present(tmp_path: Path) -> None:
    fake = FakeRegistry()
    pub = _publisher(fake, host(), tmp_path)
    await pub.publish_month("2026-10", 6, RUN, RUN)

    silver = MemoryStore()
    imp = Importer(registry(fake, write=False), silver)
    start, end = datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 12, 31, tzinfo=UTC)
    first = imp.run(start, end, [0, 6])
    assert first.runs == {"2026102006": "imported"}
    assert imp.present(RUN)
    assert imp.run(start, end, [0, 6]).runs == {"2026102006": "present"}
    assert imp.run(start, end, [0]).runs == {}


class FakeLedger:
    """``meta.silver_run`` im Arbeitsspeicher, protokolliert die Meldungen."""

    def __init__(self, rows: dict[datetime, str] | None = None) -> None:
        self.rows = dict(rows or {})
        self.events: list[tuple[str, list[datetime]]] = []

    def source_id(self, code: str) -> int:
        return 3

    def silver_runs(self, source_id: int) -> dict[datetime, str]:
        return dict(self.rows)

    def mark_silver(self, source_id, runs, status, origin) -> None:  # type: ignore[no-untyped-def]
        runs = list(runs)
        self.events.append((status, runs))
        for r in runs:
            self.rows[r] = status

    def clear_silver_loading(self, source_id, runs=None) -> None:  # type: ignore[no-untyped-def]
        drop = [r for r, st in self.rows.items() if st == "loading"]
        if runs is not None:
            drop = [r for r in drop if r in list(runs)]
        for r in drop:
            del self.rows[r]


async def test_import_reports_local_silver(tmp_path: Path) -> None:
    fake = FakeRegistry()
    pub = _publisher(fake, host(), tmp_path)
    await pub.publish_month("2026-10", 6, RUN, RUN)
    start, end = datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 12, 31, tzinfo=UTC)

    stale = datetime(2026, 9, 1, tzinfo=UTC)
    ledger = FakeLedger({stale: "loading"})
    imp = Importer(registry(fake, write=False), MemoryStore(), ledger=ledger)  # type: ignore[arg-type]
    imp.run(start, end)
    # erst angekündigt, dann vorhanden; der Rest des abgebrochenen Durchgangs ist weg
    assert ("loading", [RUN]) in ledger.events and ledger.events[-1] == ("present", [RUN])
    assert ledger.rows == {RUN: "present"}

    # vorhandenes Silber ohne Eintrag (z. B. vor der Tabelle importiert) wird nachgetragen
    ledger.rows.clear()
    assert imp.run(start, end).runs == {"2026102006": "present"}
    assert ledger.rows == {RUN: "present"}


async def test_verify_local_build_against_ghcr(tmp_path: Path) -> None:
    fake = FakeRegistry()
    pub = _publisher(fake, host(), tmp_path)
    await pub.publish_month("2026-10", 6, RUN, RUN)
    start, end = datetime(2026, 10, 1, tzinfo=UTC), datetime(2026, 10, 31, tzinfo=UTC)

    silver = MemoryStore()
    imp = Importer(registry(fake, write=False), silver)
    assert imp.verify(start, end).runs == {"2026102006": "local_missing"}
    imp.run(start, end)
    assert imp.verify(start, end).runs == {"2026102006": "imported"}

    # eigener Bau aus Bronze (anderer Abschlusszeitpunkt): gleicher Inhalt
    key = Silver.key("gfs", "0p25", RUN)
    ok = StepHost("aws.test", available={0, 1, 2, 3}, runs={RUN})
    again = _publisher(FakeRegistry(), ok, tmp_path / "b")
    await again.ingestor.ingest_run(RUN, stop_on_pending=False)
    run_key = again.bronze.run_key(RUN)
    summary = json.loads(bytes(obstore.get(again.bronze_store, run_key).bytes()))
    summary["completed_at"] = "2000-01-01T00:00:00+00:00"
    obstore.put(again.bronze_store, run_key, json.dumps(summary).encode())
    again.pipeline.process(RUN, force=True)
    tar, _ = pack(again.silver_store, key)
    imp._clear(key)
    unpack(tar, silver, key)
    assert imp.verify(start, end).runs == {"2026102006": "equal"}

    # ein abweichender Chunk wird mit Dateinamen gemeldet
    chunk = next(
        m["path"]
        for batch in obstore.list(silver, prefix=key + "/t2m/")
        for m in batch
        if not m["path"].endswith("zarr.json")
    )
    obstore.put(silver, chunk, b"kaputt")
    result = imp.verify(start, end)
    assert result.runs == {"2026102006": "different"}
    assert result.details["2026102006"]["different"] == [chunk[len(key) + 1 :]]


async def test_import_order_follows_run_priority(tmp_path: Path) -> None:
    fake = FakeRegistry()
    r00 = datetime(2026, 10, 20, 0, tzinfo=UTC)
    pub = _publisher(fake, host(RUN, r00), tmp_path)
    await pub.publish_month("2026-10", 6, RUN, RUN)
    await pub.publish_month("2026-10", 0, r00, r00)

    imp = Importer(registry(fake, write=False), MemoryStore())
    start, end = datetime(2026, 10, 1, tzinfo=UTC), datetime(2026, 10, 31, tzinfo=UTC)
    assert [layer.run for _, layer in imp.layers(start, end, [0, 6])] == [r00, RUN]
    assert [layer.run for _, layer in imp.layers(start, end, [6, 0])] == [RUN, r00]


async def test_import_several_runs_in_parallel(tmp_path: Path) -> None:
    fake = FakeRegistry()
    r00 = datetime(2026, 10, 20, 0, tzinfo=UTC)
    pub = _publisher(fake, host(RUN, r00), tmp_path)
    await pub.publish_month("2026-10", 6, RUN, RUN)
    await pub.publish_month("2026-10", 0, r00, r00)

    ledger = FakeLedger()
    imp = Importer(registry(fake, write=False), MemoryStore(), ledger=ledger)  # type: ignore[arg-type]
    start, end = datetime(2026, 10, 1, tzinfo=UTC), datetime(2026, 10, 31, tzinfo=UTC)
    result = imp.run(start, end, [0, 6], workers=2)
    assert result.runs == {"2026102000": "imported", "2026102006": "imported"}
    assert imp.present(RUN) and imp.present(r00)
    assert ledger.rows == {RUN: "present", r00: "present"}


def test_registry_repository_per_source(monkeypatch: pytest.MonkeyPatch) -> None:
    """GFS Europa nutzt sein eigenes Paket, auch wenn die Umgebung das GFS-DE-Paket vorgibt."""
    monkeypatch.setenv("WX_EXCHANGE_REPOSITORY", cli.DEFAULT_REPOSITORY)
    eu = GfsConfig.from_yaml(REPO / "config/sources/gfs_0p5.yaml")
    de = GfsConfig.from_yaml(REPO / "config/sources/gfs_0p25.yaml")

    def repo(config: GfsConfig, *argv: str) -> str:
        args = cli.build_parser().parse_args([*argv, "gfs", "plan", "2021-03-22T12"])
        with httpx.Client() as client:
            return cli._registry(args, client, config).repository

    assert repo(eu) == cli.REPOSITORIES["gfs_0p5"]
    assert repo(de) == cli.DEFAULT_REPOSITORY
    assert repo(eu, "--repository", "x/y") == "x/y"
