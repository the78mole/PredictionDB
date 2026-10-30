import gzip
import json
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import obstore
import pytest
from obstore.store import MemoryStore

from wx_ingest.config import GfsConfig
from wx_process.config import ProcessConfig
from wx_process.features import FEATURES_VERSION, ClusterWeights
from wx_process.pipeline import WxPipeline
from wx_process.silver import Silver
from wx_process.store import CapacityTable, Chunk
from wx_process.testing import RUN, fill_bronze, source_config

STEPS = [0, 1, 2, 3, 6]
CONFIG: dict[str, Any] = {
    "source": "gfs_0p25",
    "source_config": "unused.yaml",
    "weight_grid": "gfs_0p25",
    "technologies": {
        "wind_on": {"kind": "wind"},
        "pv_roof": {"kind": "pv"},
    },
}


class FakeStore:
    """Speicher im Arbeitsspeicher mit derselben Schnittstelle wie ``wx_process.store.Store``."""

    def __init__(self, version: str | None = "v2099Q1") -> None:
        self.version = version
        self.values: dict[tuple[datetime, int, int, datetime], float] = {}
        self.done: dict[tuple[datetime, str], dict[str, Any]] = {}
        self.features: dict[str, int] = {}
        self.silver: dict[datetime, tuple[str, str]] = {}
        self.packed: list[Chunk] = []
        self.replaced: list[bool] = []
        self.log: list[tuple[str, str]] = []

    def source_id(self, code: str) -> int:
        return 3

    def latest_clustering(self, grid: str) -> str | None:
        return self.version

    def region_ids(self, codes) -> dict[str, int]:  # type: ignore[no-untyped-def]
        return {c: 100 + i for i, c in enumerate(codes)}

    def cluster_weights(self, version: str, grid: str) -> list[ClusterWeights]:
        a = np.array
        return [
            ClusterWeights("wind_on", 0, 11, a([54.0, 53.0]), a([9.0, 8.0]), a([0.5, 0.5]), 120),
            ClusterWeights("pv_roof", 0, 12, a([48.0]), a([11.0]), a([1.0])),
            ClusterWeights("biomass", 0, 13, a([52.0]), a([10.0]), a([1.0])),
        ]

    def capacity(self, version: str, start: date, end: date) -> CapacityTable:
        return CapacityTable([("wind_on", 0, start - timedelta(days=10), 100.0)])

    def feature_ids(
        self, source_id: int, aggregation: str, units: dict[str, str]
    ) -> dict[str, int]:
        for code in sorted(units):
            self.features.setdefault(code, len(self.features) + 1)
        return {c: self.features[c] for c in units}

    def write_values(self, run, valid_until, feature_ids, region_ids, rows, *, replace=True) -> int:  # type: ignore[no-untyped-def]
        self.replaced.append(replace)
        self.values = {k: v for k, v in self.values.items() if k[0] != run}
        n = 0
        for fid, rid, t, v in rows:
            assert run <= t <= valid_until and rid in region_ids and fid in feature_ids
            self.values[(run, fid, rid, t)] = v
            n += 1
        return n

    def processed(self, source_id: int, run: datetime, version: str, fv: str) -> bool:
        d = self.done.get((run, version))
        return d is not None and d["features_version"] == fv

    def mark_processed(self, source_id, run, version, fv, key, n, stats, bronze) -> None:  # type: ignore[no-untyped-def]
        self.done[(run, version)] = {"features_version": fv, "key": key, "n": n, "stats": stats}

    def mark_silver(self, source_id, runs, status, origin) -> None:  # type: ignore[no-untyped-def]
        for r in runs:
            self.silver[r] = (status, origin)

    def evictable_silver(self, source_id, before, version, fv) -> list[datetime]:  # type: ignore[no-untyped-def]
        return sorted(
            r
            for r, (st, origin) in self.silver.items()
            if st == "present"
            and origin == "ghcr"
            and r < before
            and self.processed(3, r, version, fv)
        )

    def silver_runs(self, source_id: int) -> dict[datetime, str]:
        return {r: st for r, (st, _) in self.silver.items()}

    def has_features(self, source_id: int, run: datetime) -> bool:
        return any(r == run for r, _ in self.done)

    def decompress_values(self, start: datetime, end: datetime) -> list[Chunk]:
        hit = [c for c in self.packed if c.start <= end and c.end > start]
        self.packed = [c for c in self.packed if c not in hit]
        self.log += [("decompress", c.name) for c in hit]
        return hit

    def compress_values(self, chunks) -> None:  # type: ignore[no-untyped-def]
        for c in chunks:
            self.packed.append(c)
            self.log.append(("compress", c.name))


@pytest.fixture
def setup():  # type: ignore[no-untyped-def]
    bronze = MemoryStore()
    source = GfsConfig.model_validate(source_config([(0, 3, 1), (6, 6, 1)]))
    silver = Silver(MemoryStore())
    store = FakeStore()
    pipeline = WxPipeline(
        ProcessConfig.model_validate(CONFIG),
        source,
        bronze,
        silver,
        store,  # type: ignore[arg-type]
    )
    return pipeline, bronze, store


def test_not_ready_without_run_manifest(setup) -> None:  # type: ignore[no-untyped-def]
    pipeline, bronze, _ = setup
    fill_bronze(bronze, STEPS, complete=False)
    assert pipeline.process(RUN).status == "not_ready"


def test_process_writes_features_and_skips_afterwards(setup) -> None:  # type: ignore[no-untyped-def]
    pipeline, bronze, store = setup
    fill_bronze(bronze, STEPS)
    r = pipeline.process(RUN)
    assert (r.status, r.silver, r.clustering_version) == ("processed", "built", "v2099Q1")
    assert store.replaced == [False]  # erster Lauf ohne DELETE
    assert r.coverage_min == pytest.approx(1.0)
    done = store.done[(RUN, "v2099Q1")]
    assert done["features_version"] == FEATURES_VERSION and done["n"] == r.n_values > 0
    assert done["key"] == "gfs/0p25/2099/01/02/06.zarr"
    assert store.silver == {RUN: ("present", "bronze")}
    # biomass ist nicht konfiguriert und fehlt
    assert {rid for (_, _, rid, _) in store.values} == {11, 12}

    fid = store.features
    wind_power = {t: v for (_, f, rid, t), v in store.values.items() if f == fid["wind_power"]}
    assert len(wind_power) == len(STEPS)
    cf = {t: v for (_, f, _, t), v in store.values.items() if f == fid["wind_cf"]}
    assert all(wind_power[t] == pytest.approx(100 * cf[t]) for t in cf)
    # Strahlung fehlt im Analyse-Schritt, also keine Zeile für f000
    ghi_times = sorted(t for (_, f, _, t) in store.values if f == fid["ghi"])
    assert ghi_times[0] == RUN + timedelta(hours=1) and len(ghi_times) == len(STEPS) - 1
    assert "pv_power" not in fid  # keine Leistung für pv_roof im Bestand

    assert pipeline.process(RUN).status == "skipped"
    # neue Clusterung macht den Lauf erneut fällig, Silber wird wiederverwendet
    store.version = "v2099Q2"
    r2 = pipeline.process(RUN)
    assert (r2.status, r2.silver) == ("processed", "reused")


def test_fixed_regions_instead_of_clustering(tmp_path: Path) -> None:
    """GFS Europa: Gold je Region aus der Regionsdatei, auch ohne Clusterung in ``assets``."""
    regions = {
        "version": "vT",
        "regions": [
            {"code": "DE:wind_on", "technology": "wind_on", "cells": [[54.0, 9.0, 2.0]]},
            {
                "code": "AT:area",
                "technology": "area",
                "cells": [[48.0, 11.0, 1.0], [52.0, 10.0, 1.0]],
            },
        ],
    }
    path = tmp_path / "regions.json.gz"
    path.write_bytes(gzip.compress(json.dumps(regions).encode()))
    cfg = {
        **CONFIG,
        "regions": str(path),
        "technologies": {"wind_on": {"kind": "wind"}, "area": {"kind": "area"}},
    }
    bronze = MemoryStore()
    store = FakeStore(version=None)
    pipeline = WxPipeline(
        ProcessConfig.model_validate(cfg),
        GfsConfig.model_validate(source_config([(0, 3, 1), (6, 6, 1)])),
        bronze,
        Silver(MemoryStore()),
        store,  # type: ignore[arg-type]
    )
    fill_bronze(bronze, STEPS)
    r = pipeline.process(RUN)
    assert (r.status, r.clustering_version) == ("processed", "regions_vT")
    assert r.coverage_min == pytest.approx(1.0)
    by_region: dict[int, set[str]] = {}
    names = {v: k for k, v in store.features.items()}
    for _, f, rid, _ in store.values:
        by_region.setdefault(rid, set()).add(names[f])
    # gefs:DE:wind_on → 100, gefs:AT:area → 101; ohne Bestand keine Leistung
    assert set(by_region) == {100, 101}
    assert {"wind_cf", "t2m"} <= by_region[100] and "ghi" not in by_region[100]
    assert {"t2m", "wind_cf", "wind_speed_hub", "ghi", "pv_cf"} <= by_region[101]
    assert not {"wind_power", "pv_power"} & set(store.features)
    assert pipeline.process(RUN).status == "skipped"


def test_force_rebuilds_silver(setup) -> None:  # type: ignore[no-untyped-def]
    pipeline, bronze, _ = setup
    fill_bronze(bronze, STEPS)
    pipeline.process(RUN)
    r = pipeline.process(RUN, force=True)
    assert (r.status, r.silver) == ("processed", "built")


def test_silver_only_without_clustering(setup) -> None:  # type: ignore[no-untyped-def]
    pipeline, bronze, store = setup
    fill_bronze(bronze, STEPS)
    store.version = None
    r = pipeline.process(RUN)
    assert (r.status, r.silver) == ("silver_only", "built")
    assert pipeline.process(RUN).silver == "reused"


def test_missing_step_is_tolerated(setup) -> None:  # type: ignore[no-untyped-def]
    pipeline, bronze, _ = setup
    fill_bronze(bronze, [0, 1, 3, 6])
    assert pipeline.process(RUN).status == "processed"
    attrs = pipeline.silver.attrs("gfs", "0p25", RUN)
    assert attrs is not None and attrs["missing_steps"] == [2]


def test_latest_and_failures(setup) -> None:  # type: ignore[no-untyped-def]
    pipeline, bronze, _ = setup
    fill_bronze(bronze, STEPS)
    obstore.put(bronze, pipeline.layout.run_key(RUN + timedelta(hours=6)), b"{}")
    out = pipeline.latest(now=RUN + timedelta(hours=7))
    by_run = {o.run: o.status for o in out}
    assert by_run[RUN] == "processed"
    assert by_run[RUN + timedelta(hours=6)] == "failed"  # Manifest ohne Schritte
    assert by_run[RUN - timedelta(hours=6)] == "not_ready"


def test_backfill_run_selection(setup) -> None:  # type: ignore[no-untyped-def]
    pipeline, bronze, _ = setup
    fill_bronze(bronze, STEPS)
    out = pipeline.backfill(RUN - timedelta(hours=12), RUN, hours=[6, 0], newest_first=True)
    assert [(o.run, o.status) for o in out] == [
        (RUN, "processed"),
        (RUN - timedelta(hours=6), "not_ready"),
    ]


def test_backfill_follows_run_priority(setup) -> None:  # type: ignore[no-untyped-def]
    """Erst alle Läufe der ersten Laufzeit, dann die nächste, je Laufzeit jüngste zuerst."""
    pipeline, _, _ = setup
    start = RUN - timedelta(hours=24)
    out = pipeline.backfill(start, RUN, hours=[12, 0, 6], newest_first=True)
    assert [o.run for o in out] == [
        RUN - timedelta(hours=18),
        RUN - timedelta(hours=6),
        RUN,
        RUN - timedelta(hours=24),
    ]


def test_backfill_decompresses_until_no_run_writes_into_chunk(setup) -> None:  # type: ignore[no-untyped-def]
    """Ein Chunk wird vor dem ersten Schreiben entpackt und erst danach wieder gepackt."""
    pipeline, bronze, store = setup
    fill_bronze(bronze, STEPS)
    day = timedelta(days=1)
    store.packed = [
        Chunk("c_vorher", RUN - 2 * day, RUN - day),
        Chunk("c_lauf", RUN - day, RUN + day),
        Chunk("c_spaeter", RUN + 3 * day, RUN + 4 * day),
    ]
    out = pipeline.backfill(RUN - timedelta(hours=6), RUN, newest_first=True, decompress=True)
    assert [(o.run, o.status) for o in out] == [
        (RUN, "processed"),
        (RUN - timedelta(hours=6), "not_ready"),
    ]
    assert store.log == [("decompress", "c_lauf"), ("compress", "c_lauf")]
    assert {c.name for c in store.packed} == {"c_vorher", "c_lauf", "c_spaeter"}


def test_imported_silver_without_bronze(setup) -> None:  # type: ignore[no-untyped-def]
    """Silber aus GHCR ohne Bronze: nur Gold rechnen, Silber nie überschreiben (auch mit force)."""
    pipeline, _, store = setup
    other = MemoryStore()
    fill_bronze(other, STEPS)
    WxPipeline(pipeline.config, pipeline.source, other, pipeline.silver, None).process(RUN)

    r = pipeline.process(RUN)
    assert (r.status, r.silver) == ("processed", "reused")
    assert store.done[(RUN, "v2099Q1")]["n"] > 0
    assert store.silver == {RUN: ("present", "ghcr")}
    assert pipeline.process(RUN, force=True).silver == "reused"
    # ohne Silber und ohne Bronze bleibt der Lauf offen
    assert pipeline.process(RUN - timedelta(hours=6)).status == "not_ready"


def test_repo_config_gfs_0p5_covers_all_region_technologies() -> None:
    from wx_process.regions import RegionSet

    cfg = ProcessConfig.from_yaml("config/process/gfs_0p5.yaml")
    assert cfg.regions is not None
    regions = RegionSet.load(cfg.regions)
    assert regions.version == "regions_v2"
    assert {r.technology for r in regions.regions} <= set(cfg.technologies)


def test_prune_bronze_only_with_silver(setup) -> None:  # type: ignore[no-untyped-def]
    pipeline, bronze, _ = setup
    other = RUN + timedelta(hours=6)
    fill_bronze(bronze, STEPS)
    fill_bronze(bronze, STEPS, run=other)
    pipeline.process(RUN)  # Silber nur für RUN
    assert pipeline.bronze_runs(RUN - timedelta(days=1), other + timedelta(hours=1)) == [RUN, other]

    now = RUN + timedelta(days=100)
    dry = pipeline.prune_bronze(92, start=RUN - timedelta(days=1), now=now, dry_run=True)
    assert dry == {"runs": 1, "objects": 0, "kept_no_silver": 1}
    # noch jung genug: nichts zu tun
    young = pipeline.prune_bronze(92, start=RUN - timedelta(days=1), now=RUN + timedelta(days=10))
    assert young["runs"] == 0

    counts = pipeline.prune_bronze(92, start=RUN - timedelta(days=1), now=now)
    assert counts["runs"] == 1 and counts["objects"] > 0
    assert pipeline.bronze_runs(RUN - timedelta(days=1), now) == [other]
    # Silber bleibt lesbar, der Lauf gilt weiter als gerechnet
    assert pipeline.process(RUN).status == "skipped"


def test_evict_silver_only_ghcr_with_gold(setup) -> None:  # type: ignore[no-untyped-def]
    pipeline, bronze, store = setup
    other = RUN + timedelta(hours=6)
    fill_bronze(bronze, STEPS)
    fill_bronze(bronze, STEPS, run=other)
    pipeline.process(RUN)
    pipeline.process(other)
    # RUN gilt als aus GHCR importiert, other stammt aus eigenem Bronze
    store.silver[RUN] = ("present", "ghcr")
    now = RUN + timedelta(days=100)
    assert pipeline.evict_silver(92, now=now, dry_run=True) == {"runs": 1, "objects": 0}
    assert pipeline.evict_silver(92, now=RUN + timedelta(days=10))["runs"] == 0

    counts = pipeline.evict_silver(92, now=now)
    assert counts["runs"] == 1 and counts["objects"] > 0
    assert store.silver[RUN] == ("evicted", "ghcr")
    assert pipeline.silver.attrs("gfs", "0p25", RUN) is None
    assert pipeline.silver.attrs("gfs", "0p25", other) is not None
    # ein zweiter Durchgang findet nichts mehr
    assert pipeline.evict_silver(92, now=now)["runs"] == 0
