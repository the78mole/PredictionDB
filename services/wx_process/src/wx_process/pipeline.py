"""Ablauf je Lauf: Bronze vollständig? → Silber bauen (oder wiederverwenden) → Features je Cluster.

Ein Lauf ist fällig, sobald in Bronze sein ``run.json`` liegt (``wx-ingest`` schreibt es, wenn
alle Schritte gespeichert sind) oder ohne Bronze ein vollständiges Silber der aktuellen
``SILVER_VERSION`` vorliegt (aus GHCR importiert, ``wx-exchange``). Gerechnet ist er, wenn
``meta.nwp_feature_run`` einen Eintrag für die aktuelle Clusterung und Feature-Version hat.
Silber wird nur neu gebaut, wenn es fehlt, die Kodierung sich geändert hat oder Bronze neuer
ist; danach entstehen die Features aus den quantisierten Silber-Werten, sodass eine spätere
Neuberechnung aus Silber dasselbe ergibt.

@relation(SWA-WXP-PIPELINE, scope=file)
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterable
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
from wx_process.regions import RegionSet
from wx_process.silver import Dataset, Silver, assemble
from wx_process.store import Chunk, Store
from wx_process.variables import SILVER_VERSION

log = get_logger(__name__)

#: frühester möglicher Bronze-Lauf (GFS v15 im AWS-Archiv ab 2021); Startpunkt des Aufräumens
BRONZE_SINCE = datetime(2021, 1, 1, tzinfo=UTC)

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
        self.regions = RegionSet.load(config.regions) if config.regions is not None else None
        self._source_id: int | None = None
        self._region_ids: dict[str, int] | None = None

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
        """@relation(SWR-WXP-012, scope=function)"""
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

    def bronze_runs(self, start: datetime, end: datetime) -> list[datetime]:
        """Läufe mit Bronze von ``start`` bis vor ``end``, ein Listing je Tag."""
        out = []
        day = start.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        while day < end:
            prefix = f"{self.layout.model}/{self.layout.resolution}/{day:%Y/%m/%d}/"
            for p in obstore.list_with_delimiter(self.bronze_store, prefix=prefix)[
                "common_prefixes"
            ]:
                hour = p.rstrip("/").rsplit("/", 1)[-1]
                if hour.isdigit():
                    run = day.replace(hour=int(hour))
                    if start <= run < end:
                        out.append(run)
            day += timedelta(days=1)
        return out

    def prune_bronze(
        self,
        keep_days: float,
        *,
        start: datetime | None = None,
        now: datetime | None = None,
        dry_run: bool = False,
        pause: float = 0.0,
    ) -> dict[str, int]:
        """Bronze älter als ``keep_days`` löschen, wenn das Silber des Laufs vorliegt.

        Silber zählt laut ``meta.silver_run`` (ohne Datenbank: vollständiger Zarr-Store). Läufe
        ohne Silber bleiben stehen, denn aus ihnen entsteht es noch. ``pause`` Sekunden zwischen
        zwei Läufen schonen Garage, das jedes gelöschte Objekt im Hintergrund nacharbeitet.

        @relation(SWR-WXP-024, scope=function)
        """
        now = now or datetime.now(UTC)
        cutoff = now - timedelta(days=keep_days)
        runs = self.bronze_runs(start or BRONZE_SINCE, cutoff)
        present = (
            {r for r, st in self.store.silver_runs(self.source_id).items() if st == "present"}
            if self.store is not None
            else None
        )
        counts = {"runs": 0, "objects": 0, "kept_no_silver": 0}
        for run in runs:
            if present is not None:
                has_silver = run in present
            else:
                attrs = self.silver.attrs(self.source.model, self.source.resolution, run)
                has_silver = attrs is not None
            if not has_silver:
                counts["kept_no_silver"] += 1
                continue
            counts["runs"] += 1
            if dry_run:
                continue
            for batch in obstore.list(self.bronze_store, prefix=self.layout.run_prefix(run) + "/"):
                paths = [m["path"] for m in batch]
                obstore.delete(self.bronze_store, paths)
                counts["objects"] += len(paths)
            if pause:
                time.sleep(pause)
        log.info("bronze.pruned", cutoff=cutoff.isoformat(), dry_run=dry_run, **counts)
        return counts

    # --- Silber ------------------------------------------------------------------------------

    def silver_current(self, run: datetime, manifest: dict[str, Any]) -> bool:
        """@relation(SWR-WXP-021, scope=function)"""
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

    def imported_silver(self, run: datetime) -> dict[str, Any] | None:
        """Ersatz für das Bronze-Laufmanifest, wenn nur Silber vorliegt (Import aus GHCR).

        @relation(SWR-WXP-003, scope=function)
        """
        attrs = self.silver.attrs(self.source.model, self.source.resolution, run)
        if attrs is None or attrs.get("silver_version") != SILVER_VERSION:
            return None
        return {
            "completed_at": attrs.get("bronze_completed_at"),
            "steps": len(self.source.step_list()) - len(attrs.get("missing_steps") or []),
            "endpoints": {"ghcr": 1},
            "imported": True,
        }

    def load_silver(
        self, run: datetime, manifest: dict[str, Any], *, force: bool = False
    ) -> tuple[Dataset, bool]:
        # Importiertes Silber hat kein Bronze, aus dem es neu gebaut werden könnte
        """@relation(SWR-WXP-021, scope=function)"""
        if manifest.get("imported") or (not force and self.silver_current(run, manifest)):
            return self.silver.read(self.source.model, self.source.resolution, run), False
        return self.build_silver(run, manifest), True

    # --- Gold --------------------------------------------------------------------------------

    def write_features(
        self, ds: Dataset, version: str, *, replace: bool = True
    ) -> tuple[int, dict[str, Any]]:
        assert self.store is not None
        valid = ds.valid_times
        capacity: Callable[[str, int], np.ndarray | None] | None = None
        if self.regions is not None and version == self.regions.version:
            # feste Regionen: ohne Bestand, also ohne wind_power/pv_power
            if self._region_ids is None:
                self._region_ids = self.store.region_ids(self.regions.codes)
            clusters = self.regions.clusters(self._region_ids)
        else:
            clusters = self.store.cluster_weights(version, self.config.weight_grid)
            days = [v.date() for v in valid]
            table = self.store.capacity(version, min(days), max(days))

            def from_table(tech: str, no: int) -> np.ndarray | None:
                return table.at(tech, no, days)

            capacity = from_table

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
            replace=replace,
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
        self,
        run: datetime,
        *,
        force: bool = False,
        clustering: str | None = None,
        prepare: Callable[[datetime, datetime], None] | None = None,
    ) -> RunOutcome:
        """Rechnet einen Lauf; ``prepare(run, valid_until)`` läuft direkt vor dem Schreiben.

        @relation(SWR-WXP-003, SWR-WXP-022, scope=function)
        """
        run = run.astimezone(UTC)
        manifest = self.run_manifest(run)
        if manifest is None:
            manifest = self.imported_silver(run)
        if manifest is None:
            return RunOutcome(run, "not_ready")
        version = clustering
        if version is None and self.regions is not None:
            version = self.regions.version
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
        if self.store is not None:
            origin: Literal["bronze", "ghcr"] = "ghcr" if manifest.get("imported") else "bronze"
            self.store.mark_silver(self.source_id, [run], "present", origin)
        if self.store is None or version is None:
            if self.store is not None:
                log.warning("features.no_clustering", grid=self.config.weight_grid)
            return RunOutcome(run, "silver_only", silver)

        if prepare is not None:
            prepare(run, ds.valid_times[-1])
        replace = self.store.has_features(self.source_id, run)
        n, stats = self.write_features(ds, version, replace=replace)
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
        """@relation(SWR-WXP-004, scope=function)"""
        try:
            return self.process(run, **kwargs)
        except Exception as exc:
            log.exception("run_failed", run=f"{run:%Y%m%d%H}")
            return RunOutcome(run, "failed", error=f"{type(exc).__name__}: {exc}")

    def latest(self, now: datetime | None = None) -> list[RunOutcome]:
        """Dienstbetrieb: alle Läufe im Rückblickfenster, älteste zuerst.

        @relation(SWR-WXP-002, scope=function)
        """
        now = now or datetime.now(UTC)
        start = now - timedelta(hours=self.config.lookback_hours)
        return [self._safe(r) for r in self.source.runs_between(start, now)]

    def backfill(
        self,
        start: datetime,
        end: datetime,
        *,
        force: bool = False,
        clustering: str | None = None,
        hours: Iterable[int] | None = None,
        newest_first: bool = False,
        decompress: bool = False,
    ) -> list[RunOutcome]:
        """Rechnet alle Läufe im Zeitraum.

        ``decompress`` entpackt komprimierte Ziel-Chunks vor dem ersten Schreiben und packt sie
        wieder, sobald kein folgender Lauf mehr hineinschreibt (siehe ``Store.decompress_values``).

        @relation(SWR-WXP-040, SWR-WXP-041, scope=function)
        """
        hours = list(hours) if hours is not None else None
        runs = self.source.runs_between(start, end, hours)
        if newest_first:
            runs.reverse()
        # Laufzeiten in der Reihenfolge von ``hours`` (z. B. erst alle 00Z, dann 12Z, 06Z, 18Z)
        priority = {h: i for i, h in enumerate(hours or [])}
        runs.sort(key=lambda r: priority.get(r.hour, 0))
        if not decompress or self.store is None:
            return [self._safe(r, force=force, clustering=clustering) for r in runs]
        unpacked = _Unpacked(self.store)
        out = []
        try:
            for i, run in enumerate(runs):
                out.append(
                    self._safe(run, force=force, clustering=clustering, prepare=unpacked.prepare)
                )
                unpacked.release(runs[i + 1] if i + 1 < len(runs) else None)
        finally:
            unpacked.release(None)
        return out


class _Unpacked:
    """Vom Backfill entpackte Chunks; gepackt wird, sobald der nächste Lauf sie nicht mehr trifft.

    Läuft der Dienst vorher in einen Neustart, packt die Kompressions-Policy sie später.

    @relation(SWR-WXP-041, scope=class)
    """

    def __init__(self, store: Store) -> None:
        self.store = store
        self.chunks: dict[str, Chunk] = {}
        self.lead = timedelta(0)

    def prepare(self, run: datetime, valid_until: datetime) -> None:
        self.lead = max(self.lead, valid_until - run)
        for ch in self.store.decompress_values(run, valid_until):
            self.chunks[ch.name] = ch

    def release(self, next_run: datetime | None) -> None:
        done = [
            ch
            for ch in self.chunks.values()
            if next_run is None or ch.end <= next_run or ch.start > next_run + self.lead
        ]
        if not done:
            return
        try:
            self.store.compress_values(done)
        except Exception:
            log.exception("chunk.compress_failed", chunks=[ch.name for ch in done])
        for ch in done:
            self.chunks.pop(ch.name, None)


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
