"""Ablauf je Lauf: Bronze vollständig? → Silber bauen (oder wiederverwenden) → Features je Cluster.

Ein Lauf ist fällig, sobald in Bronze sein ``run.json`` liegt (``wx-ingest`` schreibt es, wenn
alle Schritte gespeichert sind). Gerechnet ist er, wenn ``meta.nwp_feature_run`` einen Eintrag
für die aktuelle Clusterung und Feature-Version hat. Silber wird nur neu gebaut, wenn es fehlt,
die Kodierung sich geändert hat oder Bronze neuer ist; danach entstehen die Features aus den
quantisierten Silber-Werten, sodass eine spätere Neuberechnung aus Silber dasselbe ergibt.
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import numpy as np
import obstore
from obstore.store import ObjectStore

from predictiondb_core.log import get_logger
from wx_ingest.bronze import Bronze
from wx_ingest.config import GfsConfig
from wx_process.config import ProcessConfig
from wx_process.decode import Grid, StepFields, decode_step
from wx_process.features import FEATURES_VERSION, UNITS, FeatureBuilder, FeatureSeries
from wx_process.silver import Dataset, Silver, assemble
from wx_process.store import Store
from wx_process.variables import SILVER_VERSION

log = get_logger(__name__)

Status = Literal["processed", "skipped", "not_ready", "silver_only", "failed"]


@dataclass
class RunOutcome:
    run: datetime
    status: Status
    silver: Literal["built", "reused", "none"] = "none"
    clustering_version: str | None = None
    n_values: int = 0
    #: kleinster Anteil der Clustergewichte, der auf dem Modellgitter liegt
    coverage_min: float | None = None
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in vars(self).items()}


class WxPipeline:
    def __init__(
        self,
        config: ProcessConfig,
        source: GfsConfig,
        bronze: ObjectStore,
        silver: Silver,
        store: Store | None,
    ) -> None:
        self.config = config
        self.source = source
        self.bronze_store = bronze
        self.layout = Bronze(bronze, source.model, source.resolution)
        self.silver = silver
        self.store = store
        self.builder = FeatureBuilder(config.technologies, config.pv)
        self._source_id: int | None = None

    @property
    def source_id(self) -> int:
        if self._source_id is None:
            assert self.store is not None
            self._source_id = self.store.source_id(self.config.source)
        return self._source_id

    # --- Bronze ------------------------------------------------------------------------------

    def _get(self, key: str) -> bytes | None:
        try:
            return bytes(obstore.get(self.bronze_store, key).bytes())
        except FileNotFoundError:
            return None

    def run_manifest(self, run: datetime) -> dict[str, Any] | None:
        raw = self._get(self.layout.run_key(run))
        if raw is None:
            return None
        data: dict[str, Any] = json.loads(raw)
        return data

    def decode_run(self, run: datetime) -> tuple[dict[int, tuple[Grid, StepFields]], list[int]]:
        steps = self.source.step_list()
        with ThreadPoolExecutor(self.config.read_concurrency) as pool:
            blobs = list(pool.map(lambda s: self._get(self.layout.step_key(run, s)), steps))
        decoded: dict[int, tuple[Grid, StepFields]] = {}
        missing = []
        for step, blob in zip(steps, blobs, strict=True):
            if blob is None:
                missing.append(step)
            else:
                decoded[step] = decode_step(blob, step)
        return decoded, missing

    # --- Silber ------------------------------------------------------------------------------

    def silver_current(self, run: datetime, manifest: dict[str, Any]) -> bool:
        attrs = self.silver.attrs(self.source.model, self.source.resolution, run)
        return (
            attrs is not None
            and attrs.get("silver_version") == SILVER_VERSION
            and attrs.get("bronze_completed_at") == manifest.get("completed_at")
        )

    def build_silver(self, run: datetime, manifest: dict[str, Any]) -> Dataset:
        decoded, missing = self.decode_run(run)
        if missing:
            log.warning("silver.steps_missing", run=f"{run:%Y%m%d%H}", steps=missing)
        attrs = {
            "source": self.config.source,
            "bronze_completed_at": manifest.get("completed_at"),
            "bronze_specs": manifest.get("specs", []),
            "missing_steps": missing,
        }
        ds = assemble(self.source.model, self.source.resolution, run, decoded, attrs).quantized()
        key = self.silver.write(ds)
        log.info("silver.written", run=f"{run:%Y%m%d%H}", key=key, steps=len(ds.steps))
        return ds

    def load_silver(
        self, run: datetime, manifest: dict[str, Any], *, force: bool = False
    ) -> tuple[Dataset, bool]:
        if not force and self.silver_current(run, manifest):
            return self.silver.read(self.source.model, self.source.resolution, run), False
        return self.build_silver(run, manifest), True

    # --- Gold --------------------------------------------------------------------------------

    def write_features(self, ds: Dataset, version: str) -> tuple[int, dict[str, Any]]:
        assert self.store is not None
        clusters = self.store.cluster_weights(version, self.config.weight_grid)
        valid = ds.valid_times
        days = [v.date() for v in valid]
        table = self.store.capacity(version, min(days), max(days))

        def capacity(tech: str, no: int) -> np.ndarray | None:
            return table.at(tech, no, days)

        series, coverage = self.builder.build(ds, clusters, capacity)
        codes = {s.code for s in series}
        fids = self.store.feature_ids(
            self.source_id, self.config.aggregation, {c: UNITS[c] for c in codes}
        )
        n = self.store.write_values(
            ds.run,
            valid[-1],
            sorted(fids.values()),
            sorted({c.region_id for c in clusters}),
            _rows(series, fids, valid),
        )
        low = {k: round(v, 4) for k, v in coverage.items() if v < 0.999}
        stats = {
            "clusters": len(clusters),
            "features": sorted(codes),
            "coverage_min": min(coverage.values()) if coverage else None,
            "coverage_below_1": low,
        }
        if low:
            log.warning("features.coverage", run=f"{ds.run:%Y%m%d%H}", clusters=low)
        return n, stats

    # --- Ablauf ------------------------------------------------------------------------------

    def process(
        self, run: datetime, *, force: bool = False, clustering: str | None = None
    ) -> RunOutcome:
        run = run.astimezone(UTC)
        manifest = self.run_manifest(run)
        if manifest is None:
            return RunOutcome(run, "not_ready")
        version = clustering
        if self.store is not None and version is None:
            version = self.store.latest_clustering(self.config.weight_grid)
        if (
            not force
            and self.store is not None
            and version is not None
            and self.store.processed(self.source_id, run, version, FEATURES_VERSION)
        ):
            return RunOutcome(run, "skipped", clustering_version=version)

        ds, built = self.load_silver(run, manifest, force=force)
        silver: Literal["built", "reused"] = "built" if built else "reused"
        if self.store is None or version is None:
            if self.store is not None:
                log.warning("features.no_clustering", grid=self.config.weight_grid)
            return RunOutcome(run, "silver_only", silver)

        n, stats = self.write_features(ds, version)
        key = self.silver.key(ds.model, ds.resolution, ds.run)
        self.store.mark_processed(
            self.source_id, run, version, FEATURES_VERSION, key, n, stats, manifest
        )
        log.info(
            "features.written",
            run=f"{run:%Y%m%d%H}",
            clustering=version,
            values=n,
            silver=silver,
        )
        return RunOutcome(run, "processed", silver, version, n, stats["coverage_min"])

    def _safe(self, run: datetime, **kwargs: Any) -> RunOutcome:
        try:
            return self.process(run, **kwargs)
        except Exception as exc:
            log.exception("run_failed", run=f"{run:%Y%m%d%H}")
            return RunOutcome(run, "failed", error=f"{type(exc).__name__}: {exc}")

    def latest(self, now: datetime | None = None) -> list[RunOutcome]:
        """Dienstbetrieb: alle Läufe im Rückblickfenster, älteste zuerst."""
        now = now or datetime.now(UTC)
        start = now - timedelta(hours=self.config.lookback_hours)
        return [self._safe(r) for r in self.source.runs_between(start, now)]

    def backfill(
        self, start: datetime, end: datetime, *, force: bool = False, clustering: str | None = None
    ) -> list[RunOutcome]:
        return [
            self._safe(r, force=force, clustering=clustering)
            for r in self.source.runs_between(start, end)
        ]


def _rows(
    series: list[FeatureSeries], fids: dict[str, int], valid: list[datetime]
) -> list[tuple[int, int, datetime, float]]:
    rows = []
    for s in series:
        fid = fids[s.code]
        for t, v in zip(valid, s.values.tolist(), strict=True):
            if v == v:  # NaN auslassen (value ist NOT NULL)
                rows.append((fid, s.region_id, t, float(v)))
    return rows
