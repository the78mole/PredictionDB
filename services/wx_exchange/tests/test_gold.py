"""Gold-Pakete und Gewichte der Cluster in GHCR (``wx_exchange.gold``, ``mastr_weights``)."""

import io
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import httpx
import numpy as np
import pyarrow.parquet as pq
import pytest

from wx_exchange import gold
from wx_exchange.mastr_weights import weights_due
from wx_exchange.oci import Registry, digest
from wx_exchange.publish import Publisher
from wx_exchange.safe import ArtifactError
from wx_exchange.testing import FakeRegistry
from wx_ingest.testing import RUN, FakeGfsHost, make_client, make_config
from wx_process.config import ProcessConfig
from wx_process.regions import RegionSet
from wx_process.testing import grid, make_step

SOURCE = "https://github.com/owner/repo"
PROCESS: dict[str, Any] = {
    "source": "gfs_0p25",
    "source_config": "unused.yaml",
    "weight_grid": "gfs_0p25",
    "technologies": {"wind_on": {"kind": "wind"}},
}


@dataclass
class StepHost(FakeGfsHost):
    """GFS-Spiegel mit dem Lauf ``RUN`` (nur ``TMP`` 2 m, Schritte 0 bis 3)."""

    def __post_init__(self) -> None:
        lat, lon = grid()
        for step in self.available:
            base = f"/gfs.{RUN:%Y%m%d}/{RUN:%H}/atmos/gfs.t{RUN:%H}z.pgrb2.0p25.f{step:03d}"
            self.files[base] = make_step(step, lat, lon, names={"t2m"})
            self.idx[base + ".idx"] = (
                f"1:0:d={RUN:%Y%m%d%H}:TMP:2 m above ground:{step} hour fcst:\n"
            )


def registry(fake: FakeRegistry, *, write: bool = True) -> Registry:
    client = httpx.Client(transport=httpx.MockTransport(fake))
    return Registry(
        fake.host, fake.repository, client, "bot", "secret" if write else None, (0.0, 0.0)
    )


def weights(version: str = "v2026Q4", mw: float = 100.0) -> gold.Weights:
    cells = [
        ("wind_on:v2026Q4:c00", "wind_on", 0, 50.0, 10.0, 0.75, 120.0),
        ("wind_on:v2026Q4:c00", "wind_on", 0, 51.0, 11.0, 0.25, 120.0),
        ("wind_on:v2026Q4:c01", "wind_on", 1, 54.0, 9.0, 1.0, None),
    ]
    capacity = [
        ("wind_on", 0, date(2026, 1, 1), mw),
        ("wind_on", 0, date(2026, 1, 2), mw),  # unverändert: fällt weg
        ("wind_on", 0, date(2026, 10, 1), mw + 5),
        ("wind_on", 1, date(2026, 1, 1), 40.0),
    ]
    return gold.Weights.from_rows(
        version, cells, capacity, {"source": "db", "mastr_export_date": "2026-10-01"}
    )


async def silver(tmp_path: Path) -> FakeRegistry:
    """Silber-Registry mit einem Lauf (2026-10-20 06Z, nur ``t2m``, Schritte 0 bis 3)."""
    fake = FakeRegistry(repository="owner/data/silver")
    config = make_config(steps=[[0, 3, 1]], messages=[{"var": "TMP", "level": "2 m above ground"}])
    pub = Publisher(
        registry(fake),
        config,
        ProcessConfig.model_validate(PROCESS),
        make_client(StepHost("aws.test", available={0, 1, 2, 3})),
        tmp_path,
        source_url=SOURCE,
    )
    await pub.publish_month("2026-10", 6, RUN, RUN)
    return fake


def test_gold_tag_roundtrip() -> None:
    """@relation(SWR-WXX-071, scope=function)"""
    assert gold.gold_tag("v2026Q4", "2026-10", 6) == "v2026Q4-2026-10-06z"
    assert gold.parse_gold_tag("v2026Q4-2026-10-06z") == ("v2026Q4", "2026-10", 6)
    assert gold.parse_gold_tag("regions_v2.sites_v1-2025-01-00z") == (
        "regions_v2.sites_v1",
        "2025-01",
        0,
    )
    assert gold.parse_gold_tag("latest") is None
    assert gold.parse_gold_tag("v1-2026-10-05z") is None
    with pytest.raises(ValueError):
        gold.gold_tag("v/1", "2026-10", 0)


def test_change_points_keep_only_changes() -> None:
    """@relation(SWR-WXX-070, scope=function)"""
    w = weights()
    assert w.capacity is not None
    rows = w.capacity.to_pylist()
    assert [(r["cluster_no"], r["day"], r["capacity_mw"]) for r in rows] == [
        (0, date(2026, 1, 1), 100.0),
        (0, date(2026, 10, 1), 105.0),
        (1, date(2026, 1, 1), 40.0),
    ]
    table = w.capacity_table()
    assert table is not None
    days = [date(2025, 12, 31), date(2026, 5, 1), date(2026, 11, 1)]
    got = table.at("wind_on", 0, days)
    assert got is not None
    assert np.isnan(got[0]) and list(got[1:]) == [100.0, 105.0]


def test_weights_roundtrip_and_digest() -> None:
    """@relation(SWR-WXX-070, SWR-WXX-072, scope=function)"""
    w = weights()
    layers = dict(w.layers())
    back = gold.Weights.from_layers(w.version, layers, w.info)
    assert back.digest == w.digest
    assert back.regions == ["wind_on:v2026Q4:c00", "wind_on:v2026Q4:c01"]
    clusters = back.cluster_weights()
    assert [(c.cluster_no, c.region_id, c.hub_height_m) for c in clusters] == [
        (0, 0, 120.0),
        (1, 1, None),
    ]
    assert clusters[0].share.sum() == pytest.approx(1.0)
    # anderer Bestand, anderer Inhalt
    assert weights(mw=200.0).digest != w.digest
    with pytest.raises(ArtifactError):
        gold.Weights.from_layers(w.version, {}, {})


def test_weights_from_region_sets() -> None:
    """@relation(SWR-WXX-070, scope=function)"""
    repo = Path(__file__).resolve().parents[3]
    process = ProcessConfig.from_yaml(repo / "config/process/gfs_0p5.yaml")
    assert process.regions is not None
    sets = [
        RegionSet.load(repo / process.regions),
        *(RegionSet.load(repo / p) for p in process.sites),
    ]
    w = gold.Weights.from_region_sets(sets)
    assert w.version == ".".join(s.version for s in sets)
    assert w.capacity is None
    assert all(code.startswith("gefs:") for code in w.regions)
    assert len(w.cluster_weights()) == sum(len(s.regions) for s in sets)


def test_publish_and_load_weights() -> None:
    """@relation(SWR-WXX-070, SWR-WXX-072, scope=function)"""
    fake = FakeRegistry(repository="owner/data/weights")
    w = weights()
    tags = gold.publish_weights(registry(fake), w, SOURCE)
    assert tags == ["v2026Q4", "v2026Q4-2026-10-01", "latest"]
    back = gold.load_weights(registry(fake, write=False))
    assert back.version == "v2026Q4" and back.digest == w.digest
    assert back.info["mastr_export_date"] == "2026-10-01"
    with pytest.raises(LookupError):
        gold.load_weights(registry(FakeRegistry(repository="owner/data/weights")))


def test_weights_due() -> None:
    """@relation(SWR-WXX-074, scope=function)"""
    w = weights()  # Export 2026-10-01
    assert weights_due(None, date(2026, 11, 12))
    assert not weights_due(w, date(2026, 12, 31))
    assert weights_due(w, date(2027, 1, 1))
    w.info.pop("mastr_export_date")
    assert weights_due(w, date(2026, 11, 12))


async def test_build_month_publishes_gold_with_weights(tmp_path: Path) -> None:
    """@relation(SWR-WXX-071, SWR-WXX-072, SWR-WXX-073, scope=function)"""
    silver_fake = await silver(tmp_path)
    gold_fake = FakeRegistry(repository="owner/data/silver")  # gleicher Pfad, eigener Speicher
    process = ProcessConfig.model_validate(PROCESS)
    w = weights()
    start, end = datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 12, 31, tzinfo=UTC)

    todo = gold.plan(
        registry(gold_fake), registry(silver_fake, write=False), process, w, start, end, [6, 0]
    )
    assert todo == [{"month": "2026-10", "hour": 6, "runs": 1}]

    builder = gold.GoldBuilder(
        registry(gold_fake), registry(silver_fake, write=False), process, w, source_url=SOURCE
    )
    result = builder.build_month("2026-10", 6)
    assert result.status == "published" and result.runs == 1
    assert result.rows == 2 * 4  # zwei Cluster, t2m an vier Schritten

    table, back, annotations = gold.load_month(registry(gold_fake), "v2026Q4-2026-10-06z")
    assert back.digest == w.digest
    assert annotations[gold.PREFIX + "runs"] == "2026102006"
    assert table.num_rows == result.rows
    rows = table.to_pylist()
    assert {r["feature"] for r in rows} == {"t2m"}
    assert [r["region"] for r in rows[:4]] == ["wind_on:v2026Q4:c00"] * 4
    assert rows[0]["run_time"] == RUN and rows[3]["valid_time"] == datetime(
        2026, 10, 20, 9, tzinfo=UTC
    )
    # Gewichte im Paket sind byte-gleich mit denen des Gewichte-Artefakts
    layers = gold_fake.manifest("v2026Q4-2026-10-06z")["layers"]
    assert [d["mediaType"] for d in layers] == [
        gold.GOLD_TYPE,
        gold.CLUSTERS_TYPE,
        gold.CAPACITY_TYPE,
    ]
    assert layers[1]["digest"] == digest(dict(w.layers())[gold.CLUSTERS_TYPE])
    raw = gold_fake.blobs[layers[0]["digest"]]
    assert pq.read_metadata(io.BytesIO(raw)).row_group(0).column(4).compression == "ZSTD"

    # gleiche Eingaben: nichts zu tun
    assert gold.plan(registry(gold_fake), registry(silver_fake), process, w, start, end, [6]) == []
    puts = gold_fake.manifest_puts()
    assert builder.build_month("2026-10", 6).status == "unchanged"
    assert gold_fake.manifest_puts() == puts

    # neuer Bestand: neuer Fingerabdruck, Monat wird neu gerechnet
    newer = weights(mw=300.0)
    again = gold.plan(registry(gold_fake), registry(silver_fake), process, newer, start, end, [6])
    assert again == [{"month": "2026-10", "hour": 6, "runs": 1}]

    # kein Silber im Monat
    assert builder.build_month("2026-09", 6).status == "no_silver"
