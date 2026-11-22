"""Tägliche Prognose ohne Datenbank und ohne VM: Champion und Eingänge aus GHCR, Ergebnis nach GHCR.

Läuft in GitHub Actions (``.github/workflows/forecast.yml``) nach dem Energiedaten-Workflow, wenn
die Day-Ahead-Auktion für morgen im Paket ``energy:JJJJ`` steht:

1. **Modell:** Tag ``champion`` aus ``ghcr.io/the78mole/predictiondb-models/<markt>`` auf das
   Manifest auflösen und nur die Modelldateien holen (``*.txt``, ``*.json``, kein
   ``backtest.parquet``). Version und SHA-256 des Manifests stehen in der Prognose.
2. **Eingänge:** ein kleiner Snapshot um den Ausgabezeitpunkt mit
   :func:`ml_train.snapshot_ghcr.ghcr_snapshot`, also mit denselben Paketen und derselben Auswahl
   je Reihe wie der Trainings-Snapshot in Actions (Energie, Gold DE und Europa, GEFS-Silber,
   Cluster-Gewichte). Nur freie Quellen; ein Snapshot mit anderen bricht ab.
   Das Gold DE in GHCR hinkt etwa 30 Stunden hinterher (die GFS-Historie in Actions lässt das
   Live-Fenster der VM aus). Den jüngsten Lauf (00Z oder 12Z) lädt der Job deshalb selbst aus
   dem AWS-Archiv, mit derselben Kette wie die GFS-Historie (``archive_only``, Silber im
   Arbeitsordner) und denselben Gewichten wie das Gold (:func:`live_weather`).
3. **Rechnen** wie ``ml-train predict`` (:func:`ml_train.predict.predict_model`).
4. **Ablage:** ``latest.json`` und ``latest.csv`` im Format des Prognose-Stacks
   (``predictiondb.forecast.v1``, wie ``predict/`` sie schreibt), dazu ``<JJJJ-MM-TT>.json``.
   Als Artefakt nach ``ghcr.io/the78mole/predictiondb-forecasts/<markt>`` mit den Tags
   ``JJJJ-MM-TT`` (Ausgabetag, ein erneuter Lauf am selben Tag ersetzt ihn) und ``latest``.

Die Pages-Seite holt beim Bau mit :func:`pull_recent` die jüngsten Tage nach ``prognose/<markt>/``
(``latest.json``, ``latest.csv``, ``<JJJJ-MM-TT>.json``, ``index.json``). So überlebt die
Prognose jeden Neubau der Seite, und ältere Stände bleiben für den Vergleich erreichbar.

@relation(SWA-MLT-FORECAST-GHCR, scope=file)
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

from energy_ingest.model import SERIES
from ml_train.config import ENSEMBLE_PREFIX, ModelConfig
from ml_train.dataset import FeatureBuilder, FeatureFrame
from ml_train.model_ghcr import REPOSITORY as MODELS
from ml_train.models import TwoStageModel
from ml_train.predict import lookback_days, predict_model
from ml_train.snapshot import Snapshot
from predictiondb_core.log import get_logger

log = get_logger(__name__)

#: Repository der Prognosen eines Marktes, über ``PDB_FORECAST_REPOSITORY`` änderbar
REPOSITORY = os.environ.get("PDB_FORECAST_REPOSITORY", "the78mole/predictiondb-forecasts/de_lu")
ARTIFACT = "application/vnd.predictiondb.forecast.v1"
FILE_MEDIA = "application/vnd.predictiondb.file.v1"
TITLE = "org.opencontainers.image.title"
#: dasselbe Schema wie ``pdb_predict.service`` (Client liest beide gleich)
SCHEMA = "predictiondb.forecast.v1"
LATEST = "latest"
INDEX_SCHEMA = "predictiondb.forecast-index.v1"
#: Ziele auf der Pages-Seite: Preis, Last und die Erzeugung aus Stufe 1 (Wind an Land, Wind auf
#: See, PV), die in die Residuallast der Preisprognose eingeht
TARGETS = ("da_price_de_lu", "load_actual", "gen_wind_onshore", "gen_wind_offshore", "gen_solar")
#: ohne diese Ziele gilt der Lauf als gescheitert; fehlende Erzeugung steht nur in inputs_missing
REQUIRED = ("da_price_de_lu", "load_actual")
#: so viele Ausgabetage holt der Bau der Seite
KEEP_DAYS = 15
LICENSE = "CC BY 4.0 (Modell und Prognose)"
ATTRIBUTION = "PredictionDB (the78mole)"
MASTR = "Marktstammdatenregister, Datenlizenz Deutschland - Namensnennung 2.0"


def connect(client: Any, repository: str = REPOSITORY, *, read_only: bool = False) -> Any:
    """Registry-Client; Anmeldung über ``WX_EXCHANGE_*`` wie bei Silber und Modellen."""
    from wx_exchange.oci import Registry

    return Registry(
        os.environ.get("WX_EXCHANGE_REGISTRY", "ghcr.io"),
        repository,
        client,
        username=os.environ.get("WX_EXCHANGE_USERNAME"),
        password=os.environ.get("WX_EXCHANGE_TOKEN") or None,
        read_only=read_only,
    )


def manifest_sha(manifest: dict[str, Any]) -> str:
    """SHA-256 über das Manifest als JSON mit sortierten Schlüsseln (wie ``Packages.note``)."""
    from wx_exchange.oci import digest

    return digest(json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode())


# --- Modell ----------------------------------------------------------------------------------


@dataclass
class Champion:
    version: str
    tag: str
    manifest_sha256: str
    manifest: dict[str, Any]
    model: TwoStageModel

    def describe(self) -> dict[str, Any]:
        m = self.manifest
        return {
            "version": self.version,
            "config": m.get("config"),
            "variant": (m.get("variant") or {}).get("name"),
            "source": f"ghcr.io/{MODELS}:{self.tag}",
            "tag": self.tag,
            "manifest_sha256": self.manifest_sha256,
            "train_issue_days": m.get("train_issue_days"),
            "license": "CC BY 4.0",
        }


def pull_model(oci: Any, cfg: ModelConfig, tag: str, dest: Path) -> Champion:
    """Version hinter ``tag`` holen, nur Modelldateien, und laden.

    @relation(SWR-MLT-084, scope=function)
    """
    m = oci.manifest(tag)
    if m is None:
        raise KeyError(f"{tag} fehlt in {getattr(oci, 'repository', 'GHCR')}")
    files: dict[str, bytes] = {}
    for layer in m.get("layers", []):
        name = (layer.get("annotations") or {}).get(TITLE)
        if not name or "/" in name or name.startswith("."):
            raise ValueError(f"{tag}: Layer ohne gültigen Dateinamen: {name!r}")
        if name.endswith((".txt", ".json")):
            files[name] = oci.blob(layer["digest"])
    if "manifest.json" not in files or "model.json" not in files:
        raise ValueError(f"{tag}: manifest.json oder model.json fehlt im Artefakt")
    man = dict(json.loads(files["manifest.json"]))
    if man.get("config") != cfg.name:
        raise ValueError(f"{tag} gehört zu {man.get('config')}, nicht zu {cfg.name}")
    if man.get("publishable") is not True:
        raise ValueError(f"{tag}: Version nicht als frei ausgewiesen (publishable)")
    version = str(man["version"])
    local = dest / version
    local.mkdir(parents=True, exist_ok=True)
    for name, data in files.items():
        (local / name).write_bytes(data)
    model = TwoStageModel.load(cfg, local)
    log.info("forecast.model", tag=tag, version=version, variant=model.variant.name)
    return Champion(version, tag, manifest_sha(m), man, model)


# --- Eingänge --------------------------------------------------------------------------------


def snapshot_codes(cfg: ModelConfig) -> list[str]:
    """Reihen wie ``ml-train snapshot`` (Ziele, Lags, Gruppen, Benchmarks)."""
    codes = list(dict.fromkeys(cfg.targets + cfg.lag_series + cfg.series_codes))
    codes += sorted({b for t in cfg.stage1 + cfg.stage2 for b in t.benchmarks})
    return list(dict.fromkeys(codes))


def snapshot_window(
    cfg: ModelConfig, champion: Champion, issue: datetime
) -> tuple[datetime, datetime, datetime, datetime]:
    """``start, end, weather_from, weather_to`` des Snapshots um ``issue``.

    Energie so weit zurück, wie die Merkmale der Variante brauchen, bis zum Ende des letzten
    Vorlauftags plus Reserve; Wetterläufe der letzten zwei Tage bis zum Ausgabezeitpunkt (wie
    :func:`ml_train.predict.live_snapshot`).

    @relation(SWR-MLT-084, scope=function)
    """
    back = lookback_days(cfg, champion.model.variant)
    day = issue.replace(hour=0, minute=0, second=0, microsecond=0)
    start = day - timedelta(days=back + 1)
    end = day + timedelta(days=max(cfg.issue.lead_days) + 3)
    return start, end, issue - timedelta(days=2), issue + timedelta(hours=1)


def build_snapshot(
    packages: Any,
    cfg: ModelConfig,
    champion: Champion,
    issue: datetime,
    *,
    weights: str = "latest",
    run_hours: list[int] | None = None,
) -> Snapshot:
    """@relation(SWR-MLT-084, scope=function)"""
    from ml_train.snapshot_ghcr import ghcr_snapshot

    start, end, wx_from, wx_to = snapshot_window(cfg, champion, issue)
    snap = ghcr_snapshot(
        packages,
        snapshot_codes(cfg),
        start,
        end,
        weather_from=wx_from,
        weather_to=wx_to,
        run_hours=run_hours or [0, 12],
        max_lead_h=24 * (max(cfg.issue.lead_days) + 2),
        weights_ref=weights,
        # Monate ohne Paket (Monatswechsel) machen den Lauf nicht unmöglich; welche fehlen,
        # steht in der Prognose
        allow_missing=True,
        neighbour=cfg.neighbour_weather,
        sites=cfg.site_weather,
        ensemble=cfg.ensemble_weather,
    )
    bad = sorted(k for k, v in snap.manifest["sources"].items() if v != "frei")
    if bad or not snap.manifest.get("publishable"):
        raise ValueError(f"nicht freie Quellen im Snapshot: {bad}")
    return snap


# --- Wetter des jüngsten Laufs ---------------------------------------------------------------

#: Konfiguration der Quelle und der Features für GFS 0,25° DE (wie gfs-history.yml/Gold)
GFS_CONFIG = Path("config/sources/gfs_0p25.yaml")
PROCESS_CONFIG = Path("config/process/gfs_0p25.yaml")


def wanted_runs(issue: datetime, run_hours: list[int], delay_h: float) -> list[datetime]:
    """Läufe, die zum Ausgabezeitpunkt verarbeitet vorliegen dürfen, jüngster zuerst (zwei).

    @relation(SWR-MLT-084, scope=function)
    """
    t = issue - timedelta(hours=delay_h)
    day = t.replace(hour=0, minute=0, second=0, microsecond=0)
    out: list[datetime] = []
    while len(out) < 2:
        for h in sorted(run_hours, reverse=True):
            r = day + timedelta(hours=h)
            if r <= t and len(out) < 2:
                out.append(r)
        day -= timedelta(days=1)
    return out


async def _silver_run(gfs: Any, process: Any, run: datetime, workdir: Path) -> Any:
    """Bronze aus dem AWS-Archiv, Silber im Arbeitsordner; ``None``, wenn der Lauf unvollständig
    ist (wie ``wx_exchange.publish.Publisher``)."""
    import asyncio

    from obstore.store import LocalStore

    from wx_exchange.idx import archive_client
    from wx_ingest.bronze import Bronze
    from wx_ingest.ingest import GfsIngestor
    from wx_process.pipeline import WxPipeline
    from wx_process.silver import Silver

    (workdir / "bronze").mkdir(parents=True, exist_ok=True)
    (workdir / "silver").mkdir(parents=True, exist_ok=True)
    bronze_store = LocalStore(workdir / "bronze")
    silver_store = LocalStore(workdir / "silver")
    bronze = Bronze(bronze_store, gfs.model, gfs.resolution)
    silver = Silver(
        silver_store, step_chunk=process.silver.step_chunk, zstd_level=process.silver.zstd_level
    )
    async with archive_client(gfs) as client:
        result = await GfsIngestor(gfs, bronze, client, archive_only=True).ingest_run(
            run, stop_on_pending=False
        )
    if not result.complete:
        log.warning("forecast.weather_incomplete", run=f"{run:%Y%m%d%H}")
        return None
    pipeline = WxPipeline(process, gfs, bronze_store, silver, None)
    outcome = await asyncio.to_thread(pipeline.process, run, force=True)
    if outcome.status == "failed":
        log.error("forecast.silver_failed", run=f"{run:%Y%m%d%H}", error=outcome.error)
        return None
    return silver.read(gfs.model, gfs.resolution, run)


def gold_rows(ds: Any, weights: Any, process: Any) -> pd.DataFrame:
    """Gold eines Laufs (``run_time, valid_time, region, feature, value``) wie ``GoldBuilder``.

    @relation(SWR-MLT-084, scope=function)
    """
    from wx_exchange.gold import run_table
    from wx_process.features import FeatureBuilder as WxFeatures

    builder = WxFeatures(process.technologies, process.pv)
    table = weights.capacity_table()
    days = [v.date() for v in ds.valid_times]

    def capacity(tech: str, no: int) -> Any:
        return None if table is None else table.at(tech, no, days)

    series, _ = builder.build(ds, weights.cluster_weights(), capacity)
    df = run_table(ds.run, list(ds.valid_times), series, weights.regions).to_pandas()
    for col in ("run_time", "valid_time"):
        df[col] = pd.to_datetime(df[col], utc=True)
    return df


def live_weather(
    snap: Snapshot,
    packages: Any,
    cfg: ModelConfig,
    issue: datetime,
    *,
    weights: str = "latest",
    run_hours: list[int] | None = None,
    workdir: Path,
) -> datetime | None:
    """Jüngsten GFS-Lauf, der dem Gold fehlt, laden und an ``snap.weather`` anhängen.

    Gibt den angehängten Lauf zurück; ``None``, wenn das Gold ihn schon hat oder kein Lauf
    vollständig im Archiv liegt (dann rechnet die Prognose mit dem jüngsten Lauf im Gold).

    @relation(SWR-MLT-084, scope=function)
    """
    import asyncio

    from ml_train.snapshot import compact_weather
    from ml_train.snapshot_ghcr import cluster_capacity, weather_de
    from wx_exchange.gold import load_weights
    from wx_ingest.config import GfsConfig
    from wx_process.config import ProcessConfig

    hours = run_hours or [0, 12]
    w = snap.weather
    de = w[~w["feature"].astype(str).str.startswith(("en_", "nb_", "st_"))]
    have = set(pd.to_datetime(de["run_time"], utc=True).unique()) if not de.empty else set()
    gfs = GfsConfig.from_yaml(GFS_CONFIG)
    process = ProcessConfig.from_yaml(PROCESS_CONFIG)
    wts = load_weights(packages.weights, weights)
    max_lead_h = 24 * (max(cfg.issue.lead_days) + 2)
    for run in wanted_runs(issue, hours, cfg.issue.nwp_delay_h):
        if pd.Timestamp(run) in have:
            log.info("forecast.weather_in_gold", run=f"{run:%Y%m%d%H}")
            return None
        ds = asyncio.run(_silver_run(gfs, process, run, workdir / f"{run:%Y%m%d%H}"))
        if ds is None:
            continue
        gold = gold_rows(ds, wts, process)
        rows = weather_de(
            gold, cluster_capacity(wts), run, run + timedelta(hours=1), [run.hour], max_lead_h
        )
        new = compact_weather(rows)
        merged = pd.concat([w, new], ignore_index=True) if not w.empty else new
        for col in ("run_time", "valid_time"):
            merged[col] = pd.to_datetime(merged[col], utc=True)
        merged["feature"] = merged["feature"].astype(object)
        merged["value"] = merged["value"].astype("float32")
        snap.weather = merged.sort_values(
            ["feature", "run_time", "valid_time"], kind="stable"
        ).reset_index(drop=True)
        log.info("forecast.weather_live", run=f"{run:%Y%m%d%H}", rows=len(rows))
        return run
    return None


# --- Rechnen und Dokument --------------------------------------------------------------------


def run_time(frame: FeatureFrame, column: str) -> datetime | None:
    """Wetterlauf, den die Merkmale nutzen (Gültigkeit minus Vorlauf der ersten Zeile)."""
    if column not in frame.X.columns:
        return None
    lead = frame.X[column].dropna()
    if lead.empty:
        return None
    ts = pd.Timestamp(frame.meta.loc[lead.index[0], "ts"])
    out: datetime = (ts - pd.Timedelta(hours=float(lead.iloc[0]))).to_pydatetime()
    return out


def unit(code: str) -> str | None:
    s = SERIES.get(code)
    return s.unit if s else None


def _iso(t: datetime | pd.Timestamp) -> str:
    return str(pd.Timestamp(t).tz_convert("UTC").strftime("%Y-%m-%dT%H:%M:%SZ"))


def to_document(
    df: pd.DataFrame,
    champion: Champion,
    snap: Snapshot,
    issue: datetime,
    *,
    weather_run: datetime | None,
    ensemble_run: datetime | None,
    targets: Iterable[str] = TARGETS,
) -> dict[str, Any]:
    """Prognose im Format von ``pdb_predict`` (``predictiondb.forecast.v1``).

    @relation(SWR-MLT-085, scope=function)
    """
    wanted = list(targets)
    out: dict[str, list[dict[str, Any]]] = {}
    sel = df[df["target"].isin(wanted)] if not df.empty else df
    if not sel.empty:
        wide = sel.pivot_table(
            index=["target", "valid_time"], columns="quantile", values="value"
        ).sort_index()
        for (target, valid), row in wide.iterrows():
            out.setdefault(str(target), []).append(
                {
                    "valid_time": _iso(valid),
                    **{f"p{round(q * 100)}": round(float(v), 3) for q, v in row.items()},
                }
            )
    out = {t: out[t] for t in wanted if t in out}
    man = snap.manifest
    attr = man.get("attribution") or {}
    missing = {t: "keine Prognose (Eingänge fehlen?)" for t in wanted if t not in out}
    for tag in (man.get("ghcr") or {}).get("missing", []):
        missing[f"ghcr:{tag}"] = "Paket fehlt in GHCR"
    sources = [{"code": k, "license": v} for k, v in sorted(man["sources"].items())]
    sources += [
        {"code": "mastr", "license": MASTR},
        {"code": "noaa_gfs", "license": "NOAA GFS, gemeinfrei (Werk der US-Regierung)"},
    ]
    if ensemble_run is not None:
        sources.append({"code": "noaa_gefs", "license": "NOAA GEFS, gemeinfrei"})
    attribution = "; ".join([ATTRIBUTION, *(f"{k}: {v}" for k, v in sorted(attr.items()) if v)])
    return {
        "schema": SCHEMA,
        "issue_time": _iso(issue),
        "created": datetime.now(UTC).isoformat(timespec="seconds"),
        "model": champion.describe(),
        "weather_run": weather_run.isoformat() if weather_run else None,
        "ensemble_run": ensemble_run.isoformat() if ensemble_run else None,
        "units": {t: unit(t) for t in out},
        "targets": out,
        "inputs_missing": missing,
        "inputs": {
            "snapshot_id": man.get("snapshot_id"),
            "clustering_version": man.get("clustering_version"),
            "packages": (man.get("ghcr") or {}).get("packages", {}),
        },
        "sources": sources,
        "license": LICENSE,
        "attribution": attribution,
    }


def forecast(
    cfg: ModelConfig, champion: Champion, snap: Snapshot, issue: datetime
) -> dict[str, Any]:
    """@relation(SWR-MLT-084, scope=function)"""
    ts = pd.Timestamp(issue)
    frame = FeatureBuilder(snap, cfg).build_at([ts])
    df = predict_model(frame, champion.model, champion.version, ts)
    doc = to_document(
        df,
        champion,
        snap,
        issue,
        weather_run=run_time(frame, "wx_lead_h"),
        ensemble_run=run_time(frame, f"wx_{ENSEMBLE_PREFIX}lead_h"),
        # nur Ziele, die das Modell kennt; sonst stünde ein nie trainiertes Ziel als fehlend da
        targets=[t for t in TARGETS if t in cfg.targets],
    )
    log.info(
        "forecast.done",
        issue=doc["issue_time"],
        version=champion.version,
        rows={t: len(v) for t, v in doc["targets"].items()},
        missing=sorted(doc["inputs_missing"]),
    )
    return doc


# --- Dateien ---------------------------------------------------------------------------------


def to_csv(doc: dict[str, Any]) -> str:
    """@relation(SWR-MLT-085, scope=function)"""
    lines = ["target,valid_time,p10,p50,p90"]
    for t, rows in doc["targets"].items():
        for r in rows:
            lines.append(
                f"{t},{r['valid_time']},{r.get('p10', '')},{r.get('p50', '')},{r.get('p90', '')}"
            )
    return "\n".join(lines) + "\n"


def issue_day(doc: dict[str, Any]) -> str:
    return str(doc["issue_time"])[:10]


def files_of(doc: dict[str, Any]) -> dict[str, bytes]:
    """Dateien einer Tagesprognose: ``latest.json``, ``latest.csv``, ``<JJJJ-MM-TT>.json``.

    @relation(SWR-MLT-085, scope=function)
    """
    body = json.dumps(doc, indent=1, ensure_ascii=False).encode()
    return {
        "latest.json": body,
        "latest.csv": to_csv(doc).encode(),
        f"{issue_day(doc)}.json": body,
    }


def write_files(files: dict[str, bytes], out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    for name, data in files.items():
        tmp = out / f".{name}.tmp"
        tmp.write_bytes(data)
        tmp.replace(out / name)


# --- GHCR ------------------------------------------------------------------------------------


def push(oci: Any, doc: dict[str, Any]) -> list[str]:
    """Tagesprognose als Artefakt mit den Tags ``JJJJ-MM-TT`` und ``latest``.

    @relation(SWR-MLT-085, scope=function)
    """
    from wx_exchange.oci import EMPTY, MANIFEST

    files = files_of(doc)
    layers = []
    for name in sorted(files):
        d = oci.push_blob(files[name])
        layers.append(
            {
                "mediaType": FILE_MEDIA,
                "digest": d,
                "size": len(files[name]),
                "annotations": {TITLE: name},
            }
        )
    model = doc.get("model") or {}
    manifest = {
        "schemaVersion": 2,
        "mediaType": MANIFEST,
        "artifactType": ARTIFACT,
        "config": EMPTY,
        "layers": layers,
        "annotations": {
            "org.opencontainers.image.title": f"Prognose {issue_day(doc)}",
            "org.opencontainers.image.description": (
                f"Last DE und Day-Ahead-Preis DE-LU, D+0 bis D+15, Ausgabe {doc['issue_time']},"
                f" Modell {model.get('version')}"
            ),
            "org.opencontainers.image.source": "https://github.com/the78mole/PredictionDB",
            "org.opencontainers.image.licenses": "CC-BY-4.0",
            "org.opencontainers.image.created": str(doc["created"]),
            "io.predictiondb.issue_time": str(doc["issue_time"]),
            "io.predictiondb.model_version": str(model.get("version")),
            "org.predictiondb.attribution": str(doc["attribution"])[:1000],
        },
    }
    tags = [issue_day(doc), LATEST]
    for t in tags:
        oci.put_manifest(t, manifest)
    log.info("forecast.pushed", tags=tags, repository=getattr(oci, "repository", None))
    return tags


def day_tags(tags: Iterable[str]) -> list[str]:
    """Tags der Form ``JJJJ-MM-TT``, aufsteigend."""
    out = []
    for t in tags:
        try:
            date.fromisoformat(t)
        except ValueError:
            continue
        if len(t) == 10:
            out.append(t)
    return sorted(out)


def exists(oci: Any, day: date) -> bool:
    """Ob es für ``day`` schon eine Prognose gibt (Ersatzlauf am Nachmittag überspringen)."""
    return oci.manifest(day.isoformat()) is not None


def _files(oci: Any, tag: str) -> dict[str, bytes]:
    m = oci.manifest(tag)
    if m is None:
        return {}
    out = {}
    for layer in m.get("layers", []):
        name = (layer.get("annotations") or {}).get(TITLE)
        if not name or "/" in name or name.startswith("."):
            continue
        out[name] = oci.blob(layer["digest"])
    return out


def pull_recent(oci: Any, out: Path, days: int = KEEP_DAYS) -> list[str]:
    """Die jüngsten ``days`` Tagesprognosen nach ``out`` (für den Bau der Pages-Seite).

    Schreibt ``<JJJJ-MM-TT>.json`` je Tag, ``latest.json``/``latest.csv`` vom jüngsten und
    ``index.json`` mit Ausgabezeit und Modell je Tag.

    @relation(SWR-MLT-085, scope=function)
    """
    tags = day_tags(oci.tags())[-days:]
    index = []
    newest: dict[str, bytes] = {}
    for t in tags:
        files = _files(oci, t)
        body = files.get(f"{t}.json")
        if body is None:
            continue
        doc = json.loads(body)
        write_files({f"{t}.json": body}, out)
        index.append(
            {
                "day": t,
                "file": f"{t}.json",
                "issue_time": doc.get("issue_time"),
                "model": (doc.get("model") or {}).get("version"),
            }
        )
        newest = files
    if newest:
        write_files({k: v for k, v in newest.items() if k.startswith("latest.")}, out)
    write_files(
        {
            "index.json": json.dumps(
                {
                    "schema": "predictiondb.forecast-index.v1",
                    "latest": "latest.json",
                    "days": index,
                },
                indent=1,
            ).encode()
        },
        out,
    )
    log.info("forecast.pulled", days=len(index), out=str(out))
    return [d["day"] for d in index]


def run(
    cfg: ModelConfig,
    models_oci: Any,
    packages: Any,
    issue: datetime,
    *,
    tag: str = "champion",
    weights: str = "latest",
    cache: Path | None = None,
) -> dict[str, Any]:
    """Champion holen, Snapshot bauen, rechnen.

    @relation(SWR-MLT-084, scope=function)
    """
    with tempfile.TemporaryDirectory(dir=cache) as tmp:
        champion = pull_model(models_oci, cfg, tag, Path(tmp))
        snap = build_snapshot(packages, cfg, champion, issue, weights=weights)
        if champion.model.variant.requires_weather:
            live_weather(snap, packages, cfg, issue, weights=weights, workdir=Path(tmp) / "gfs")
        return forecast(cfg, champion, snap, issue)
