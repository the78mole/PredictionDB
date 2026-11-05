"""GFS-Wetter-Features für Wetter-Varianten, ohne Bronze, Silber und Datenbank.

Je Lauf lädt der Dienst nur die Schritte bis zum Horizont des Modells und nur die Nachrichten,
die die Features brauchen, über dieselben Endpunkte wie ``wx-ingest`` (NOMADS-Filter, dann
AWS/Google per Byte-Range). Dekodieren, Rückrechnen der Intervallwerte und die Features je
Cluster übernimmt ``wx-process`` unverändert; die Gitter-Gewichte und die Leistung je Cluster
kommen aus dem Modellpaket, also mit der Clusterung des Trainings. Zusammengefasst wird wie in
``ml_train.snapshot.WEATHER_SQL``: ``*_power`` als Summe, alles andere nach Clusterleistung
gewichtet. Je Lauf bleibt eine kleine Parquet-Datei im Cache (``cache/weather``).

Varianten mit der Gruppe ``nachbarwetter`` brauchen zusätzlich die Flächenmittel der
Nachbarzonen aus GFS 0,5° (dreistündlich), wie sie ``wx-process`` für ``gfs_0p5`` als Gold
``gefs:<zone>:area`` rechnet. Die Zonenzellen (aus ``config/gefs/regions_v2.json.gz``) und die
Definition der Technik ``area`` liegen im Paket; geladen wird derselbe Lauf wie fürs DE-Wetter
(``cache/weather_nb``).

@relation(SWA-PRD-WEATHER, scope=file)
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
import numpy as np
import pandas as pd
from gribkit import MessageSpec

from ml_train.config import neighbour_feature
from predictiondb_core.log import get_logger
from wx_ingest.config import GfsConfig
from wx_ingest.gfs import Endpoint, FetchError, StepUnavailable
from wx_process.decode import decode_step
from wx_process.features import ClusterWeights, FeatureBuilder, PvModel, TechnologyFeatures
from wx_process.silver import Dataset, assemble
from wx_process.store import CapacityTable

if TYPE_CHECKING:
    from pdb_predict.package import ModelPackage

log = get_logger(__name__)

#: Nachrichten, aus denen ``wx_process.features`` rechnet (Teilmenge von gfs_0p25.yaml)
FEATURE_MESSAGES = [
    MessageSpec("TMP", "2 m above ground"),
    MessageSpec("UGRD", "10 m above ground"),
    MessageSpec("VGRD", "10 m above ground"),
    MessageSpec("UGRD", "100 m above ground"),
    MessageSpec("VGRD", "100 m above ground"),
    MessageSpec("GUST", "surface"),
    MessageSpec("DSWRF", "surface"),
    MessageSpec("TCDC", "entire atmosphere"),
    MessageSpec("APCP", "surface"),
    MessageSpec("CSNOW", "surface"),
]
#: Nachrichten für ``wind_cf`` und ``pv_cf`` der Technik ``area`` (Teilmenge von gfs_0p5.yaml)
NEIGHBOUR_MESSAGES = [
    MessageSpec("TMP", "2 m above ground"),
    MessageSpec("UGRD", "10 m above ground"),
    MessageSpec("VGRD", "10 m above ground"),
    MessageSpec("UGRD", "100 m above ground"),
    MessageSpec("VGRD", "100 m above ground"),
    MessageSpec("DSWRF", "surface"),
]
#: GFS-Läufe stehen bei NOMADS etwa 3,5 bis 5 h nach Laufzeit vollständig bereit
AVAILABLE_AFTER = timedelta(hours=4)
KEEP_RUNS = 4


def cluster_weights(
    clusters: pd.DataFrame,
) -> tuple[list[ClusterWeights], dict[tuple[str, int], float]]:
    """Gewichte je Cluster und Clusterleistung (Summe der Zellen) für die Zusammenfassung.

    @relation(SWR-PRD-023, scope=function)
    """
    out = []
    caps: dict[tuple[str, int], float] = {}
    for i, ((tech, no), g) in enumerate(clusters.groupby(["technology", "cluster_no"], sort=True)):
        hub = g["hub_height_m"].dropna()
        out.append(
            ClusterWeights(
                str(tech),
                int(no),
                i,
                g["lat"].to_numpy(np.float64),
                g["lon"].to_numpy(np.float64),
                g["share"].to_numpy(np.float64),
                float(hub.iloc[0]) if len(hub) else None,
            )
        )
        caps[(str(tech), int(no))] = float(g["capacity_mw"].sum())
    return out, caps


def technology_features(
    ds: Dataset,
    clusters: pd.DataFrame,
    cluster_capacity: pd.DataFrame,
    wx_process: dict[str, Any],
) -> pd.DataFrame:
    """Features je Technologie in Langform wie im Snapshot (``run_time, valid_time, feature,
    value``).

    @relation(SWR-PRD-023, scope=function)
    """
    techs = {k: TechnologyFeatures.model_validate(v) for k, v in wx_process["technologies"].items()}
    builder = FeatureBuilder(techs, PvModel.model_validate(wx_process.get("pv") or {}))
    weights, caps = cluster_weights(clusters)
    by_region = {w.region_id: (w.technology, w.cluster_no) for w in weights}
    days = [v.date() for v in ds.valid_times]
    table = CapacityTable(
        (str(r.technology), int(r.cluster_no), pd.Timestamp(r.day).date(), float(r.capacity_mw))
        for r in cluster_capacity.itertuples()
    )
    series, _coverage = builder.build(ds, weights, lambda t, n: table.at(t, n, days))
    sums: dict[str, np.ndarray] = {}
    wsum: dict[str, np.ndarray] = {}
    for s in series:
        tech, no = by_region[s.region_id]
        name = f"{tech}_{s.code}"
        v = s.values.astype(np.float64)
        ok = ~np.isnan(v)
        if s.code.endswith("_power"):
            # Summe über die Cluster; NaN nur, wenn kein Cluster einen Wert hat
            sums[name] = np.where(ok, v, 0.0) + sums.get(name, np.zeros_like(v))
            wsum[name] = ok.astype(np.float64) + wsum.get(name, np.zeros_like(v))
            continue
        cap = caps[(tech, no)]
        sums[name] = np.where(ok, v * cap, 0.0) + sums.get(name, np.zeros_like(v))
        wsum[name] = np.where(ok, cap, 0.0) + wsum.get(name, np.zeros_like(v))
    valid = pd.DatetimeIndex(ds.valid_times)
    rows = []
    for name, total in sums.items():
        w = wsum[name]
        if name.endswith("_power"):
            value = np.where(w > 0, total, np.nan)
        else:
            value = np.where(w > 0, total / np.where(w > 0, w, 1.0), np.nan)
        rows.append(
            pd.DataFrame({"run_time": ds.run, "valid_time": valid, "feature": name, "value": value})
        )
    out = pd.concat(rows, ignore_index=True) if rows else pd.DataFrame()
    if not out.empty:
        out["run_time"] = pd.to_datetime(out["run_time"], utc=True)
        out["valid_time"] = pd.to_datetime(out["valid_time"], utc=True)
        out["value"] = out["value"].astype("float32")
    return out


async def download_run(
    gfs: GfsConfig,
    run: datetime,
    steps: Sequence[int],
    messages: list[MessageSpec],
    timeout_s: float,
) -> dict[int, bytes] | None:
    """GRIB je Schritt eines Laufs über die Endpunkte der Quelle; ``None``, wenn einer fehlt.

    @relation(SWR-PRD-023, SWR-PRD-025, scope=function)
    """
    timeout = httpx.Timeout(timeout_s, connect=15.0)
    headers = {
        "User-Agent": "PredictionDB pdb-predict (+https://github.com/the78mole/PredictionDB)"
    }
    bbox = gfs.bbox.bbox()
    async with httpx.AsyncClient(timeout=timeout, headers=headers) as client:
        endpoints = [Endpoint(e, client) for e in gfs.endpoints]
        sem = asyncio.Semaphore(gfs.step_concurrency)

        async def one(step: int) -> bytes | None:
            async with sem:
                for ep in endpoints:
                    try:
                        data = await ep.fetch_step(gfs.product, run, step, messages, bbox)
                        return data.grib
                    except StepUnavailable:
                        continue
                    except (FetchError, httpx.HTTPError) as e:
                        log.warning(
                            "weather.step_failed",
                            source=gfs.code,
                            step=step,
                            endpoint=ep.name,
                            error=str(e),
                        )
                        continue
            return None

        blobs = await asyncio.gather(*(one(s) for s in steps))
    if any(b is None for b in blobs):
        missing = [s for s, b in zip(steps, blobs, strict=True) if b is None]
        log.warning(
            "weather.run_incomplete", source=gfs.code, run=f"{run:%Y%m%d%H}", missing=missing[:10]
        )
        return None
    return {s: b for s, b in zip(steps, blobs, strict=True) if b is not None}


def neighbour_features(
    ds: Dataset,
    regions: pd.DataFrame,
    wx_process: dict[str, Any],
    features: Sequence[str],
) -> pd.DataFrame:
    """Flächenmittel je Nachbarzone wie das Gold ``gefs:<zone>:area`` von ``wx-process``, in
    Langform mit den Snapshot-Namen ``nb_<zone>_<feature>``.

    @relation(SWR-PRD-025, scope=function)
    """
    techs = {k: TechnologyFeatures.model_validate(v) for k, v in wx_process["technologies"].items()}
    builder = FeatureBuilder(techs, PvModel.model_validate(wx_process.get("pv") or {}))
    zones = list(dict.fromkeys(regions["zone"].astype(str)))
    weights = []
    for i, zone in enumerate(zones):
        g = regions[regions["zone"] == zone]
        hub = g["hub_height_m"].dropna() if "hub_height_m" in g else pd.Series(dtype=float)
        weights.append(
            ClusterWeights(
                "area",
                i,
                i,
                g["lat"].to_numpy(np.float64),
                g["lon"].to_numpy(np.float64),
                g["share"].to_numpy(np.float64),
                float(hub.iloc[0]) if len(hub) else None,
            )
        )
    series, _coverage = builder.build(ds, weights)
    valid = pd.DatetimeIndex(ds.valid_times)
    wanted = set(features)
    rows = [
        pd.DataFrame(
            {
                "run_time": ds.run,
                "valid_time": valid,
                "feature": neighbour_feature(f"gefs:{zones[s.region_id]}:area", s.code),
                "value": s.values.astype(np.float32),
            }
        )
        for s in series
        if s.code in wanted
    ]
    out = pd.concat(rows, ignore_index=True) if rows else pd.DataFrame()
    if not out.empty:
        out["run_time"] = pd.to_datetime(out["run_time"], utc=True)
        out["valid_time"] = pd.to_datetime(out["valid_time"], utc=True)
        out["value"] = out["value"].astype("float32")
    return out


class NeighbourCache:
    """Nachbarwetter je Lauf aus GFS 0,5°, Cache ``cache/weather_nb``.

    @relation(SWR-PRD-025, scope=class)
    """

    def __init__(
        self, root: Path, gfs: GfsConfig, *, max_lead_h: int, timeout_s: float = 60.0
    ) -> None:
        self.root = root / "weather_nb"
        self.root.mkdir(parents=True, exist_ok=True)
        self.gfs = gfs
        self.steps = [s for s in gfs.step_list() if s <= max_lead_h]
        self.timeout_s = timeout_s

    def _path(self, run: datetime) -> Path:
        return self.root / f"{run:%Y%m%d%H}.parquet"

    def features_for(self, run: datetime, package: ModelPackage) -> pd.DataFrame | None:
        path = self._path(run)
        if path.exists():
            return pd.read_parquet(path)
        if package.neighbour_regions is None or package.wx_process_nb is None:
            raise ValueError(f"{package.version}: Paket ohne Zonen der Nachbarn")
        blobs = asyncio.run(
            download_run(self.gfs, run, self.steps, NEIGHBOUR_MESSAGES, self.timeout_s)
        )
        if blobs is None:
            return None
        decoded = {s: decode_step(b, s) for s, b in blobs.items()}
        ds = assemble(self.gfs.model, self.gfs.resolution, run, decoded).quantized()
        features = (package.info.get("neighbour_weather") or {}).get("features", [])
        df = neighbour_features(ds, package.neighbour_regions, package.wx_process_nb, features)
        df.to_parquet(path, index=False)
        log.info("weather.neighbour_features", run=f"{run:%Y%m%d%H}", rows=len(df))
        return df

    def prune(self) -> None:
        for old in sorted(self.root.glob("*.parquet"))[:-KEEP_RUNS]:
            old.unlink()


class WeatherCache:
    """@relation(SWR-PRD-023, scope=class)"""

    def __init__(
        self,
        root: Path,
        gfs: GfsConfig,
        *,
        run_hours: Sequence[int],
        max_lead_h: int,
        timeout_s: float = 60.0,
        neighbour: NeighbourCache | None = None,
    ) -> None:
        self.root = root / "weather"
        self.neighbour = neighbour
        self.root.mkdir(parents=True, exist_ok=True)
        self.gfs = gfs
        self.run_hours = list(run_hours)
        self.steps = [s for s in gfs.step_list() if s <= max_lead_h]
        self.timeout_s = timeout_s

    def _path(self, run: datetime) -> Path:
        return self.root / f"{run:%Y%m%d%H}.parquet"

    def wanted_runs(self, now: datetime) -> list[datetime]:
        """Die beiden jüngsten Läufe der Trainings-Laufzeiten, die schon bereitstehen sollten."""
        runs = self.gfs.runs_between(
            now - timedelta(hours=48), now - AVAILABLE_AFTER, self.run_hours
        )
        return runs[-2:]

    async def _download(self, run: datetime) -> dict[int, bytes] | None:
        return await download_run(self.gfs, run, self.steps, FEATURE_MESSAGES, self.timeout_s)

    def features_for(self, run: datetime, package: ModelPackage) -> pd.DataFrame | None:
        path = self._path(run)
        if path.exists():
            return pd.read_parquet(path)
        blobs = asyncio.run(self._download(run))
        if blobs is None:
            return None
        decoded = {s: decode_step(b, s) for s, b in blobs.items()}
        ds = assemble(self.gfs.model, self.gfs.resolution, run, decoded).quantized()
        if package.clusters is None or package.wx_process is None:
            raise ValueError(f"{package.version}: Paket ohne Cluster-Gewichte")
        cc = package.cluster_capacity if package.cluster_capacity is not None else pd.DataFrame()
        df = technology_features(ds, package.clusters, cc, package.wx_process)
        df.to_parquet(path, index=False)
        log.info("weather.features", run=f"{run:%Y%m%d%H}", rows=len(df))
        return df

    def update(self, package: ModelPackage, now: datetime | None = None) -> pd.DataFrame:
        """Features der jüngsten Läufe (fehlende werden geladen), Langform wie im Snapshot."""
        now = now or datetime.now(UTC)
        parts = []
        for run in self.wanted_runs(now):
            try:
                df = self.features_for(run, package)
            except Exception:
                log.exception("weather.run_failed", run=f"{run:%Y%m%d%H}")
                df = None
            if df is None:
                continue
            parts.append(df)
            if self.neighbour is not None and package.requires_neighbour:
                # derselbe Lauf wie das DE-Wetter (``FeatureBuilder._neighbour_weather``)
                try:
                    nb = self.neighbour.features_for(run, package)
                except Exception:
                    log.exception("weather.neighbour_failed", run=f"{run:%Y%m%d%H}")
                    nb = None
                if nb is not None and not nb.empty:
                    parts.append(nb)
        for old in sorted(self.root.glob("*.parquet"))[:-KEEP_RUNS]:
            old.unlink()
        if self.neighbour is not None:
            self.neighbour.prune()
        if not parts:
            return pd.DataFrame(columns=["run_time", "valid_time", "feature", "value"])
        return pd.concat(parts, ignore_index=True)
