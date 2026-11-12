"""Gewichte der MaStR-Cluster im Runner: Export laden, in eine flüchtige Datenbank importieren,
Clusterung fortschreiben, Gewichte nach GHCR.

Der Workflow ``mastr-weights.yml`` startet dafür Postgres/Timescale als Service-Container und
spielt die Migrationen ein. Import, Geokodierung, Clusterung, Gewichte und Bestandsreihe sind
dieselben wie auf der VM (``mastr_ingest.MastrPipeline``); nur das ZIP liegt statt in Garage im
Arbeitsverzeichnis des Runners.

Damit die Clusterung nicht bei jedem Lauf neu entsteht (die Datenbank ist danach weg), legt
``restore_clustering`` die Clusterung aus dem letzten Gewichte-Artefakt wieder an: Zentren,
Region-Codes, Zellzuordnung und das Exportdatum, auf dem sie gebildet wurde. Die Pipeline
schreibt sie dann wie auf der VM fort und bildet erst nach ``recluster_after_days`` eine neue
Version. Das erste Artefakt kann von der VM kommen (``gold weights-publish``); dann setzt der
Runner genau deren Clusterung fort.

@relation(SWA-WXX-MASTR, scope=file)
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import httpx
from obstore.store import LocalStore
from sqlalchemy import Engine, text

from mastr_ingest.config import MastrConfig
from mastr_ingest.download import Bronze, Downloader
from mastr_ingest.pipeline import MastrPipeline, quarter_bounds
from mastr_ingest.store import Store as MastrStore
from predictiondb_core.log import get_logger
from wx_exchange.gold import CELL_COLUMNS, Weights

log = get_logger(__name__)


def weights_due(previous: Weights | None, today: date) -> bool:
    """Neue Gewichte fällig: noch keine, oder ihr MaStR-Export liegt vor dem laufenden Quartal.

    @relation(SWR-WXX-074, scope=function)
    """
    if previous is None:
        return True
    export = previous.info.get("mastr_export_date")
    if not export:
        return True
    start, _ = quarter_bounds(today)
    return date.fromisoformat(export) < start


def restore_clustering(engine: Engine, weights: Weights) -> bool:
    """Clusterung aus dem Gewichte-Artefakt in eine leere Datenbank übernehmen.

    Ohne ``cells`` (älteres Artefakt) oder ohne Exportdatum der Clusterung gibt es nichts
    fortzuschreiben, dann entsteht eine neue Clusterung.

    @relation(SWR-WXX-075, scope=function)
    """
    info = weights.info
    export = info.get("clustering_export_date")
    if weights.cells is None or not export:
        return False
    cells = weights.cells.select(list(CELL_COLUMNS)).to_pylist()
    params = json.loads(info.get("clustering_params") or "{}")
    with engine.begin() as c:
        sid = c.execute(
            text("""
            INSERT INTO assets.snapshot (export_date, version, object_key, status)
            VALUES (:d, 'ghcr', :k, 'pruned')
            ON CONFLICT (export_date) DO UPDATE SET export_date = EXCLUDED.export_date
            RETURNING snapshot_id"""),
            {"d": date.fromisoformat(export), "k": f"ghcr:{weights.version}"},
        ).scalar_one()
        c.execute(
            text("""
            INSERT INTO assets.clustering (clustering_version, snapshot_id, grid, grid_res, params)
            VALUES (:v, :s, :g, :r, CAST(:p AS jsonb))"""),
            {
                "v": weights.version,
                "s": sid,
                "g": info.get("clustering_grid", "0p25"),
                "r": float(info.get("clustering_grid_res", "0.25")),
                "p": json.dumps(params),
            },
        )
        centroids: dict[tuple[str, int], tuple[float, float, str | None]] = {}
        for r in cells:
            centroids[(r["technology"], int(r["cluster_no"]))] = (
                float(r["centroid_lat"]),
                float(r["centroid_lon"]),
                r["region"],
            )
        for (tech, no), (lat, lon, code) in sorted(centroids.items()):
            code = code or f"{tech}:{weights.version}:c{no:02d}"
            region_id = c.execute(
                text("""
                INSERT INTO meta.region (code) VALUES (:c)
                ON CONFLICT (code) DO UPDATE SET code = EXCLUDED.code
                RETURNING region_id"""),
                {"c": code},
            ).scalar_one()
            c.execute(
                text("INSERT INTO assets.cluster VALUES (:v, :t, :n, :lat, :lon, :r)"),
                {"v": weights.version, "t": tech, "n": no, "lat": lat, "lon": lon, "r": region_id},
            )
        c.execute(
            text("INSERT INTO assets.cluster_cell VALUES (:v, :t, :lat, :lon, :n)"),
            [
                {
                    "v": weights.version,
                    "t": r["technology"],
                    "lat": float(r["lat"]),
                    "lon": float(r["lon"]),
                    "n": int(r["cluster_no"]),
                }
                for r in cells
            ],
        )
    log.info(
        "mastr.clustering.restored",
        version=weights.version,
        export_date=export,
        clusters=len(centroids),
        cells=len(cells),
    )
    return True


def build_weights(
    engine: Engine,
    config: MastrConfig,
    grid: str,
    workdir: Path,
    client: httpx.Client,
    day: date,
    previous: Weights | None,
    *,
    local_zip: Path | None = None,
) -> Weights:
    """MaStR-Export von ``day`` (oder ``local_zip``) verarbeiten und die Gewichte auf ``grid``
    exportieren; ``previous`` liefert die Clusterung zum Fortschreiben.

    @relation(SWR-WXX-075, scope=function)
    """
    workdir.mkdir(parents=True, exist_ok=True)
    (workdir / "bronze").mkdir(exist_ok=True)
    if previous is not None:
        restore_clustering(engine, previous)
    pipeline = MastrPipeline(
        config,
        MastrStore(engine),
        Bronze(LocalStore(workdir / "bronze")),
        workdir,
        Downloader(client, config.page_url, config.download_url),
    )
    result = pipeline.run(day, local_zip=local_zip)
    log.info(
        "mastr.snapshot.done",
        export_date=result.export_date.isoformat(),
        units=result.n_units,
        clustering=result.clustering_version,
    )
    return Weights.from_db(engine, grid, result.clustering_version)
