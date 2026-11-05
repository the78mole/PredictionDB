"""Prognosepaket: alles, was eine Instanz ohne Datenbank für die Inferenz einer Version braucht.

Der schlanke Prognose-Stack (``predict/``) lädt ein Paket aus einem lokalen Ordner oder später
als ORAS-Artefakt aus GHCR (ARCHITEKTUR.md 7.11.2) und ruft die Eingänge selbst bei den freien
Quellen ab. Was er dort nicht bekommt, liegt im Paket: die installierte Leistung aus dem MaStR
und für Wetter-Varianten die Gitter-Gewichte der Cluster, mit denen das Modell trainiert wurde.
Damit rechnet der Stack dieselben Wetter-Features wie ``wx-process`` (gleiche Clusterung).

Die Dateien liegen flach nebeneinander, damit sie später 1:1 Layer des Artefakts werden:

============================ ==================================================================
``package.json``             Format, Version, Variante, benötigte Reihen, Wetterläufe, Lizenz
``config.yaml``              Modellkonfiguration (``config/ml/<name>.yaml``)
``model.json``, ``*_qNN.txt``  Modell wie in der Registry
``manifest.json``            Manifest der Registry (Kennzahlen, Datenstand, Commit)
``capacity.parquet``         installierte Leistung DE je Technologie und Tag (``day``,
                             ``technology``, ``capacity_mw``), letzte ``CAPACITY_DAYS`` Tage
``clusters.parquet``         nur Wetter-Varianten: ``technology``, ``cluster_no``, ``lat``,
                             ``lon``, ``share``, ``capacity_mw`` je Gitterzelle und
                             ``hub_height_m`` je Cluster
``cluster_capacity.parquet`` nur Wetter-Varianten: Leistung je Cluster (``technology``,
                             ``cluster_no``, ``day``, ``capacity_mw``), letzte 30 Tage
``wx_process.yaml``          nur Wetter-Varianten: Feature-Definitionen (Leistungskurven, PV)
``neighbour_regions.parquet`` nur Gruppe ``nachbarwetter``: Zellen je Nachbarzone (``zone``,
                             ``lat``, ``lon``, ``share``, ``hub_height_m``) aus
                             ``config/gefs/regions_v2.json.gz`` (Regionen ``<zone>:area``)
``wx_process_nb.yaml``       nur Gruppe ``nachbarwetter``: Technik ``area`` und PV-Modell aus
                             ``config/process/gfs_0p5.yaml``
============================ ==================================================================

Backtest-Prognosen (``backtest.parquet``) gehören nicht ins Paket, weil sie Ist-Preise enthalten
(7.11.1, Regel 2); die Kennzahlen stehen im Manifest.

@relation(SWA-MLT-BUNDLE, scope=file)
"""

from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
import yaml

from ml_train.config import ModelConfig, Variant
from ml_train.predict import lookback_days
from ml_train.registry import Registry
from ml_train.snapshot import Snapshot

FORMAT = "predictiondb.model-package.v1"
#: artifactType des ORAS-Artefakts (ARCHITEKTUR.md 7.11.2)
ARTIFACT_TYPE = "application/vnd.predictiondb.model.v1"
LICENSE = "CC-BY-4.0"
ATTRIBUTION = "PredictionDB (the78mole)"
#: Bestand im Paket; der Stack schreibt den letzten Tag fort wie im Training (ffill)
CAPACITY_DAYS = 400


def required_codes(cfg: ModelConfig, variant: Variant) -> list[str]:
    """Energiereihen, die die Merkmale der Variante brauchen (ohne Benchmarks).

    @relation(SWR-MLT-070, scope=function)
    """
    codes = [*cfg.targets, *cfg.lag_series]
    for name in variant.groups:
        g = cfg.series_groups.get(name)
        if g is not None:
            codes += g.stored_codes
    return list(dict.fromkeys(codes))


def write_package(
    out: Path,
    *,
    cfg: ModelConfig,
    config_yaml: bytes,
    model_dir: Path,
    manifest: dict[str, Any],
    capacity: pd.DataFrame,
    clusters: pd.DataFrame | None = None,
    cluster_capacity: pd.DataFrame | None = None,
    wx_process: dict[str, Any] | None = None,
    run_hours: list[int] | None = None,
    neighbour_regions: pd.DataFrame | None = None,
    wx_process_nb: dict[str, Any] | None = None,
    regions_version: str | None = None,
) -> dict[str, Any]:
    """Schreibt das Paket nach ``out`` und gibt ``package.json`` zurück.

    @relation(SWR-MLT-070, SWR-MLT-071, scope=function)
    """
    spec = json.loads((model_dir / "model.json").read_text())
    variant = Variant.model_validate(spec["variant"])
    needs_nb = "nachbarwetter" in variant.groups
    if needs_nb:
        if not variant.requires_weather:
            raise ValueError(f"Variante {variant.name}: nachbarwetter braucht requires_weather")
        if cfg.neighbour_weather is None:
            raise ValueError(f"Variante {variant.name}: nachbarwetter ohne neighbour_weather")
        if neighbour_regions is None or neighbour_regions.empty or wx_process_nb is None:
            raise ValueError(f"Variante {variant.name}: Nachbarwetter braucht die Zonenzellen")
        missing = set(cfg.neighbour_weather.zones) - set(neighbour_regions["zone"].astype(str))
        if missing:
            raise ValueError(f"Variante {variant.name}: Zonen ohne Zellen: {sorted(missing)}")
    out.mkdir(parents=True, exist_ok=True)
    files = ["model.json"]
    for stage in ("stage1", "stage2"):
        for m in spec[stage].values():
            files += list(m["files"].values())
    for f in files:
        shutil.copyfile(model_dir / f, out / f)
    (out / "config.yaml").write_bytes(config_yaml)
    man = {k: v for k, v in manifest.items() if k != "files"}
    (out / "manifest.json").write_text(json.dumps(man, indent=2, default=str))

    cap = capacity.copy()
    if not cap.empty:
        cap["day"] = pd.to_datetime(cap["day"])
        cap = cap[cap["day"] > cap["day"].max() - timedelta(days=CAPACITY_DAYS)]
        cap["day"] = cap["day"].dt.date
    cap.to_parquet(out / "capacity.parquet", index=False)
    files += ["config.yaml", "manifest.json", "capacity.parquet"]

    if variant.requires_weather:
        if clusters is None or clusters.empty or wx_process is None:
            raise ValueError(f"Variante {variant.name}: Wetter braucht Cluster-Gewichte")
        clusters.to_parquet(out / "clusters.parquet", index=False)
        cc = cluster_capacity if cluster_capacity is not None else pd.DataFrame()
        cc.to_parquet(out / "cluster_capacity.parquet", index=False)
        (out / "wx_process.yaml").write_text(yaml.safe_dump(wx_process, sort_keys=False))
        files += ["clusters.parquet", "cluster_capacity.parquet", "wx_process.yaml"]
    nb_info = None
    if needs_nb:
        assert cfg.neighbour_weather is not None and neighbour_regions is not None
        neighbour_regions.to_parquet(out / "neighbour_regions.parquet", index=False)
        (out / "wx_process_nb.yaml").write_text(yaml.safe_dump(wx_process_nb, sort_keys=False))
        files += ["neighbour_regions.parquet", "wx_process_nb.yaml"]
        nb_info = {
            "source": cfg.neighbour_weather.source,
            "zones": list(cfg.neighbour_weather.zones),
            "features": list(cfg.neighbour_weather.features),
            "regions_version": regions_version,
        }

    pkg = {
        "format": FORMAT,
        "artifact_type": ARTIFACT_TYPE,
        "version": manifest.get("version"),
        "config": cfg.name,
        "variant": variant.name,
        "requires_weather": variant.requires_weather,
        "codes": required_codes(cfg, variant),
        "lookback_days": lookback_days(cfg, variant),
        "weather": (
            {
                "source": "gfs_0p25",
                "run_hours": run_hours or [0, 12],
                "max_lead_h": 24 * (max(cfg.issue.lead_days) + 2),
                "clustering_version": manifest.get("clustering_version"),
            }
            if variant.requires_weather
            else None
        ),
        "neighbour_weather": nb_info,
        "capacity_until": str(cap["day"].max()) if not cap.empty else None,
        "license": LICENSE,
        "attribution": ATTRIBUTION,
        "created": datetime.now(UTC).isoformat(timespec="seconds"),
        "files": [*files, "package.json"],
    }
    (out / "package.json").write_text(json.dumps(pkg, indent=2, default=str))
    return pkg


# --- Export aus Registry und Datenbank --------------------------------------------------------

CLUSTERS_SQL = """
    SELECT g.technology, g.cluster_no, g.lat, g.lon, g.share, g.capacity_mw
    FROM assets.grid_weight g
    JOIN assets.cluster cl USING (clustering_version, technology, cluster_no)
    WHERE g.clustering_version = :v AND g.grid = :g AND g.share > 0
      AND cl.region_id IS NOT NULL
    ORDER BY 1, 2"""

CLUSTER_CAPACITY_SQL = """
    SELECT technology, cluster_no, day, capacity_mw FROM assets.capacity_daily
    WHERE clustering_version = :v AND day > (
        SELECT max(day) FROM assets.capacity_daily WHERE clustering_version = :v) - 30
    ORDER BY 1, 2, 3"""


def export_package(
    registry: Registry,
    version: str,
    out: Path,
    *,
    config_path: Path,
    snapshot: Snapshot | None = None,
    wx_process_config: Path = Path("config/process/gfs_0p25.yaml"),
    wx_process_nb_config: Path = Path("config/process/gfs_0p5.yaml"),
    regions: Path = Path("config/gefs/regions_v2.json.gz"),
    cache: Path = Path("/tmp/ml-train-cache"),
) -> dict[str, Any]:
    """Paket einer Registry-Version; Bestand aus dem Snapshot oder der Datenbank, Gitter-Gewichte
    (nur Wetter-Varianten) aus der Datenbank mit der Clusterung der Version.

    @relation(SWR-MLT-070, scope=function)
    """
    cfg = registry.cfg
    manifest = registry.manifest(version)
    model = registry.load(version, cache)
    model_dir = cache / version
    engine = None
    if snapshot is not None:
        capacity = snapshot.capacity
    else:
        from ml_train.snapshot import CAPACITY_SQL, latest_clustering
        from predictiondb_core.db import create_db_engine

        engine = create_db_engine()
        cv = manifest.get("clustering_version") or latest_clustering(engine)
        with engine.connect() as c:
            capacity = pd.read_sql(CAPACITY_SQL, c, params={"cv": cv})
    clusters = cluster_capacity = None
    wx: dict[str, Any] | None = None
    run_hours = None
    if snapshot is not None:
        run_hours = snapshot.manifest.get("weather", {}).get("run_hours")
    if model.variant.requires_weather:
        from sqlalchemy import text

        from predictiondb_core.db import create_db_engine
        from wx_process.store import Store

        engine = engine or create_db_engine()
        cv = manifest.get("clustering_version")
        if not cv:
            raise ValueError(f"{version}: Manifest ohne clustering_version")
        wx = yaml.safe_load(wx_process_config.read_text())
        grid = wx.get("weight_grid", "gfs_0p25")
        with engine.connect() as c:
            clusters = pd.read_sql(text(CLUSTERS_SQL), c, params={"v": cv, "g": grid})
            cluster_capacity = pd.read_sql(text(CLUSTER_CAPACITY_SQL), c, params={"v": cv})
            hubs = Store._hub_heights(c, cv)
        clusters["hub_height_m"] = [
            hubs.get((t, int(n)))
            for t, n in zip(clusters["technology"], clusters["cluster_no"], strict=True)
        ]
        wx = {"technologies": wx["technologies"], "pv": wx.get("pv", {})}
    nb_regions = wx_nb = None
    regions_version = None
    if "nachbarwetter" in model.variant.groups and cfg.neighbour_weather is not None:
        nb_regions, regions_version = neighbour_regions(regions, cfg.neighbour_weather.zones)
        raw = yaml.safe_load(wx_process_nb_config.read_text())
        wx_nb = {"technologies": {"area": raw["technologies"]["area"]}, "pv": raw.get("pv", {})}
    return write_package(
        out,
        cfg=cfg,
        config_yaml=config_path.read_bytes(),
        model_dir=model_dir,
        manifest=manifest,
        capacity=capacity,
        clusters=clusters,
        cluster_capacity=cluster_capacity,
        wx_process=wx,
        run_hours=run_hours,
        neighbour_regions=nb_regions,
        wx_process_nb=wx_nb,
        regions_version=regions_version,
    )


def neighbour_regions(path: Path, zones: list[str]) -> tuple[pd.DataFrame, str]:
    """Zellen der Regionen ``<zone>:area`` für das Paket und die Version der Regionsdatei.

    @relation(SWR-MLT-071, scope=function)
    """
    from wx_process.regions import RegionSet

    rs = RegionSet.load(path)
    by_code = {r.code: r for r in rs.regions}
    parts = []
    for zone in zones:
        r = by_code.get(f"{zone}:area")
        if r is None:
            raise ValueError(f"{path}: keine Region {zone}:area")
        parts.append(
            pd.DataFrame(
                {
                    "zone": zone,
                    "lat": r.lat,
                    "lon": r.lon,
                    "share": r.share,
                    "hub_height_m": r.hub_height_m,
                }
            )
        )
    return pd.concat(parts, ignore_index=True), rs.version
