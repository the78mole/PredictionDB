"""Ablauf eines Snapshots: Export sichern → Einheiten importieren → verorten → Cluster → Bestand.

Jeder Schritt ist wiederholbar. Ein bereits gesicherter Export wird nicht erneut geladen, ein
erneuter Import ersetzt die Einheiten des Snapshots, Gewichte und Tagesbestand der jüngsten
Clusterung werden jedes Mal vollständig neu geschrieben.
"""

from __future__ import annotations

import zipfile
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np

from mastr_ingest.capacity import daily_capacity
from mastr_ingest.cluster import nearest, weighted_kmeans
from mastr_ingest.config import MastrConfig
from mastr_ingest.download import Bronze, Downloader, ExportManifest, parse_export_name
from mastr_ingest.store import Store
from mastr_ingest.units import iter_grid_connections, iter_units, read_catalog
from predictiondb_core.log import get_logger

log = get_logger(__name__)


def quarter_bounds(day: date) -> tuple[date, date]:
    first_month = 3 * ((day.month - 1) // 3) + 1
    start = date(day.year, first_month, 1)
    end = date(day.year + 1, 1, 1) if first_month == 10 else date(day.year, first_month + 3, 1)
    return start, date.fromordinal(end.toordinal() - 1)


def version_name(day: date, existing: set[str]) -> str:
    base = f"v{day.year}Q{(day.month - 1) // 3 + 1}"
    if base not in existing:
        return base
    for suffix in "bcdefghijklmnopqrstuvwxyz":
        if base + suffix not in existing:
            return base + suffix
    raise RuntimeError(f"keine freie Clusterversion für {base}")


@dataclass
class SnapshotResult:
    export_date: date
    status: str
    snapshot_id: int | None = None
    n_units: int = 0
    clustering_version: str | None = None
    stats: dict[str, Any] = field(default_factory=dict)


class MastrPipeline:
    def __init__(
        self,
        config: MastrConfig,
        store: Store,
        bronze: Bronze,
        work_dir: Path,
        downloader: Downloader | None = None,
    ) -> None:
        self.config = config
        self.store = store
        self.bronze = bronze
        self.work_dir = work_dir
        self.downloader = downloader

    # --- Export sichern --------------------------------------------------------------------

    def acquire(self, day: date) -> tuple[ExportManifest, Path]:
        """Export von ``day`` aus Garage oder, falls dort noch nicht vorhanden, von der Quelle."""
        manifest = self.bronze.manifest(day)
        if manifest is not None:
            path = self.work_dir / Path(manifest.object_key).name
            return manifest, self.bronze.fetch(manifest, path)
        if self.downloader is None:
            raise RuntimeError("kein Downloader konfiguriert")
        url, version = self.downloader.discover(day)
        path = self.downloader.download(url, self.work_dir / Path(url).name)
        manifest = ExportManifest(day.isoformat(), version, url, self.bronze.zip_key(day, version))
        return self.bronze.put(path, manifest), path

    def acquire_local(self, path: Path) -> ExportManifest:
        """Lokal vorliegenden Export (manuell geladen) nach Garage übernehmen."""
        day, version = parse_export_name(path.name)
        manifest = self.bronze.manifest(day)
        if manifest is None:
            manifest = self.bronze.put(
                path,
                ExportManifest(
                    day.isoformat(), version, f"file:{path.name}", self.bronze.zip_key(day, version)
                ),
            )
        return manifest

    # --- Import ----------------------------------------------------------------------------

    def import_snapshot(self, manifest: ExportManifest, path: Path) -> SnapshotResult:
        day = date.fromisoformat(manifest.export_date)
        sid = self.store.upsert_snapshot(
            day, manifest.version, manifest.object_key, manifest.sha256, manifest.size
        )
        try:
            with zipfile.ZipFile(path) as zf:
                catalog = read_catalog(zf)
                n = self.store.load_units(sid, iter_units(zf, catalog))
                naps = self.store.assign_zones(sid, iter_grid_connections(zf, catalog))
            log.info("mastr.zones.assigned", grid_connections=naps)
            sea = {k: (p.lat, p.lon) for k, p in self.config.sea_areas.items()}
            self.store.geolocate(sid, sea)
            stats: dict[str, Any] = {
                "units": self.store.unit_stats(sid, day),
                "zones": self.store.zone_stats(sid, day),
            }
            prev = self.store.latest_imported(before=day)
            if prev is not None and prev["status"] == "imported":
                stats["diff"] = {
                    "previous": prev["export_date"].isoformat(),
                    **self.store.snapshot_diff(sid, prev["snapshot_id"], prev["export_date"]),
                }
            self.store.set_snapshot_status(sid, "imported", n, stats)
        except Exception:
            self.store.set_snapshot_status(sid, "failed")
            raise
        log.info("mastr.import.done", export_date=day.isoformat(), units=n, stats=stats)
        pruned = self.store.prune(self.config.keep_snapshots)
        if pruned:
            log.info("mastr.prune", snapshot_ids=pruned)
        return SnapshotResult(day, "imported", sid, n, stats=stats)

    # --- Cluster, Gewichte, Bestand ----------------------------------------------------------

    def update_clusters(self, snapshot_id: int, export_date: date, *, new: bool = False) -> str:
        """Clusterung fortschreiben bzw. neu bilden; gibt die Version zurück."""
        grid = self.config.cluster_grid
        latest = self.store.latest_clustering()
        stale = (
            latest is not None
            and latest["export_date"] is not None
            and ((export_date - latest["export_date"]).days >= self.config.recluster_after_days)
        )
        if new or latest is None or stale:
            version = version_name(export_date, set(self.store.clustering_versions()))
            params = {
                "clusters": {t: c.clusters for t, c in self.config.technologies.items()},
                "seed": 0,
                "export_date": export_date.isoformat(),
            }
            self.store.create_clustering(version, snapshot_id, grid.name, grid.res, params)
            log.info("mastr.cluster.new", version=version)
        else:
            assert latest is not None
            version = str(latest["clustering_version"])

        for tech, tcfg in self.config.technologies.items():
            i, j, kw = self.store.cell_capacity(snapshot_id, tech, grid.res, export_date)
            if len(i) == 0:
                log.warning("mastr.cluster.no_units", technology=tech)
                continue
            lat, lon = (i * grid.res).astype(np.float64), (j * grid.res).astype(np.float64)
            existing = self.store.clusters(version, tech)
            if not existing:
                cl = weighted_kmeans(lat, lon, kw, tcfg.clusters)
                self.store.write_clusters(
                    version,
                    tech,
                    zip(cl.centroid_lat.tolist(), cl.centroid_lon.tolist(), strict=True),
                    zip(lat.tolist(), lon.tolist(), cl.labels.tolist(), strict=True),
                )
                log.info(
                    "mastr.cluster.built",
                    version=version,
                    technology=tech,
                    k=cl.k,
                    cells=len(lat),
                    capacity_mw=round(float(kw.sum()) / 1000, 1),
                )
                continue
            known = self.store.known_cells(version, tech, grid.res)
            fresh = np.array(
                [(a, b) not in known for a, b in zip(i.tolist(), j.tolist(), strict=True)]
            )
            if fresh.any():
                nos = np.array([n for n, _, _ in existing])
                idx = nearest(
                    lat[fresh],
                    lon[fresh],
                    np.array([e[1] for e in existing]),
                    np.array([e[2] for e in existing]),
                )
                self.store.add_cells(
                    version,
                    tech,
                    zip(lat[fresh].tolist(), lon[fresh].tolist(), nos[idx].tolist(), strict=True),
                )
                log.info(
                    "mastr.cluster.cells_added",
                    version=version,
                    technology=tech,
                    cells=int(fresh.sum()),
                )

        for wg in self.config.weight_grids:
            n = self.store.write_grid_weights(
                version, snapshot_id, grid.res, wg.name, wg.res, export_date
            )
            log.info("mastr.weights.written", version=version, grid=wg.name, rows=n)
        n = self.store.write_zone_weights(version, snapshot_id, grid.res, export_date)
        log.info("mastr.zones.written", version=version, rows=n)
        n = self.write_capacity(version, snapshot_id, export_date)
        log.info("mastr.capacity.written", version=version, rows=n)
        return version

    def write_capacity(self, version: str, snapshot_id: int, export_date: date) -> int:
        events: dict[tuple[str, int], list[tuple[date, float]]] = defaultdict(list)
        for tech, cluster_no, day, kw in self.store.capacity_events(
            version, snapshot_id, self.config.cluster_grid.res
        ):
            events[(tech, int(cluster_no))].append((day, float(kw)))
        start = self.config.history_start
        series = (
            (tech, no, start, daily_capacity(ev, start, export_date) / 1000)
            for (tech, no), ev in sorted(events.items())
        )
        return self.store.write_capacity_daily(version, snapshot_id, series)

    # --- Gesamtablauf ----------------------------------------------------------------------

    def is_due(self, day: date) -> bool:
        return not self.store.has_snapshot_between(*quarter_bounds(day))

    def run(
        self,
        day: date,
        *,
        local_zip: Path | None = None,
        new_clustering: bool = False,
        keep_local: bool = False,
    ) -> SnapshotResult:
        if local_zip is not None:
            manifest, path = self.acquire_local(local_zip), local_zip
        else:
            manifest, path = self.acquire(day)
        result = self.import_snapshot(manifest, path)
        assert result.snapshot_id is not None
        newest = self.store.latest_imported()
        if newest is not None and newest["export_date"] > result.export_date:
            # älterer Export (Nachimport): Clusterung und Bestand folgen dem jüngsten Snapshot
            log.info(
                "mastr.cluster.skipped_older",
                export_date=result.export_date.isoformat(),
                newest=newest["export_date"].isoformat(),
            )
        else:
            result.clustering_version = self.update_clusters(
                result.snapshot_id, result.export_date, new=new_clustering
            )
        if local_zip is None and not keep_local:
            path.unlink(missing_ok=True)
        return result
