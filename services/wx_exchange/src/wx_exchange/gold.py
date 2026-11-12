"""Gold-Pakete im Runner: Features je Cluster aus dem Silber in GHCR, je Monat und Laufzeit.

Gold sind die Wetter-Features je MaStR-Cluster (GFS 0,25° DE) bzw. je fester Region (GFS 0,5°:
Gebotszonen und Kraftwerksstandorte), wie sie ``wx-process`` auf der VM nach
``feat.nwp_value`` schreibt. Hier entstehen sie ohne Datenbank: Das Silber kommt aus GHCR, die
Gewichte der Cluster aus einem kleinen eigenen Artefakt, das die VM aus ``assets`` exportiert
(``gold weights-publish``); feste Regionen liegen ohnehin im Repo. Adressierung::

    ghcr.io/the78mole/predictiondb-data/gfs-0p25-weights:latest          # Gewichte (VM)
    ghcr.io/the78mole/predictiondb-data/gfs-0p25-gold:v2026Q1-2026-10-00z  # Gold (Runner)

Ein Gold-Manifest trägt drei Layer: ``gold.parquet`` mit allen Läufen des Monats in Langform
(``run_time, valid_time, region, feature, value``, sortiert nach Feature, Region, Lauf und
Zeit, damit zstd die Reihen gut packt) und die Gewichte, mit denen gerechnet wurde
(``clusters.parquet``, bei Clustern ``capacity.parquet``). Die Gewichte sind byte-gleich mit dem
Gewichte-Artefakt; die Registry hält jeden Blob nur einmal. Der Tag beginnt mit der Version der
Clusterung bzw. der Regionen, eine neue Clusterung ergibt also neue Pakete neben den alten.

Ob ein Monat neu gebaut werden muss, entscheidet der Fingerabdruck in den Annotationen: Hash aus
``FEATURES_VERSION``, Feature-Konfiguration, Inhalt der Gewichte und den Inhalts-Hashes aller
Silber-Läufe des Monats. Kommt ein Silber-Lauf hinzu (laufender Monat) oder ändern sich die
Gewichte (neuer MaStR-Snapshot), wird der Monat neu gerechnet.

Anders als auf der VM rechnet ein Paket alle Läufe mit demselben Stand der Gewichte; die VM
behält für schon gerechnete Läufe die Gewichte ihres Rechenzeitpunkts.

@relation(SWA-WXX-GOLD, scope=file)
"""

from __future__ import annotations

import hashlib
import io
import json
import re
from collections.abc import Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

import numpy as np
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq
from obstore.store import MemoryStore
from sqlalchemy import Engine, text

from predictiondb_core.log import get_logger
from wx_exchange.artifact import MonthArtifact, files_from_tar, parse_tag, unpack
from wx_exchange.artifact import tag as silver_tag
from wx_exchange.oci import EMPTY, MANIFEST, Registry
from wx_exchange.safe import ArtifactError, annotations_of, check_digest
from wx_exchange.safe import layers as manifest_layers
from wx_process.config import ProcessConfig
from wx_process.features import FEATURES_VERSION, ClusterWeights, FeatureBuilder
from wx_process.regions import REGION_PREFIX, RegionSet
from wx_process.silver import Silver
from wx_process.store import CapacityTable, Store

log = get_logger(__name__)

ARTIFACT_TYPE = "application/vnd.predictiondb.wx-gold.v1"
WEIGHTS_ARTIFACT_TYPE = "application/vnd.predictiondb.wx-weights.v1"
GOLD_TYPE = "application/vnd.predictiondb.wx-gold.parquet"
CLUSTERS_TYPE = "application/vnd.predictiondb.wx-clusters.parquet"
CAPACITY_TYPE = "application/vnd.predictiondb.wx-capacity.parquet"
#: Zellzuordnung und Zentren der Clusterung (nur im Gewichte-Artefakt, zum Fortschreiben)
CELLS_TYPE = "application/vnd.predictiondb.wx-cluster-cells.parquet"
TITLES = {
    GOLD_TYPE: "gold.parquet",
    CLUSTERS_TYPE: "clusters.parquet",
    CAPACITY_TYPE: "capacity.parquet",
    CELLS_TYPE: "cells.parquet",
}
PREFIX = "org.predictiondb."
LICENSE = "CC-BY-4.0 AND DL-DE-BY-2.0"
ATTRIBUTION = (
    "NOAA/NCEP Global Forecast System (GFS); Anlagenbestand aus dem Marktstammdatenregister der"
    " Bundesnetzagentur (Datenlizenz Deutschland - Namensnennung - Version 2.0); verdichtet von"
    " PredictionDB"
)
#: Gold je Quelle; Gewichte nur, wo sie aus der Clusterung der Datenbank kommen
REPOSITORIES = {
    "gfs_0p25": "the78mole/predictiondb-data/gfs-0p25-gold",
    "gfs_0p5": "the78mole/predictiondb-data/gfs-0p5-gold",
}
WEIGHTS_REPOSITORIES = {"gfs_0p25": "the78mole/predictiondb-data/gfs-0p25-weights"}
LATEST = "latest"
#: zstd-Stufe der Parquet-Dateien (höher spart kaum, kostet aber deutlich Zeit)
ZSTD_LEVEL = 9

_VERSION = r"[A-Za-z0-9_.]{1,64}"
CELL_COLUMNS = ("technology", "cluster_no", "lat", "lon", "centroid_lat", "centroid_lon", "region")
_GOLD_TAG = re.compile(rf"\A({_VERSION})-([0-9]{{4}}-[0-9]{{2}}-[0-9]{{2}}z)\Z")


# --- Tags ------------------------------------------------------------------------------------


def gold_tag(version: str, month: str, hour: int) -> str:
    """``v2026Q1`` + ``2026-10`` + 0 → ``v2026Q1-2026-10-00z``.

    @relation(SWR-WXX-071, scope=function)
    """
    if not re.fullmatch(_VERSION, version):
        raise ValueError(f"ungültige Version für einen Tag: {version!r}")
    return f"{version}-{silver_tag(month, hour)}"


def parse_gold_tag(text_: str) -> tuple[str, str, int] | None:
    """Umkehrung von ``gold_tag``; fremde Tags ergeben ``None``.

    @relation(SWR-WXX-071, scope=function)
    """
    m = _GOLD_TAG.match(text_)
    if m is None:
        return None
    parsed = parse_tag(m.group(2))
    return None if parsed is None else (m.group(1), *parsed)


# --- Gewichte --------------------------------------------------------------------------------


def _parquet(table: pa.Table) -> bytes:
    buf = io.BytesIO()
    pq.write_table(table, buf, compression="zstd", compression_level=ZSTD_LEVEL)
    return buf.getvalue()


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def change_points(
    rows: Iterable[tuple[str, int, date, float]],
) -> list[tuple[str, int, date, float]]:
    """Bestand je Cluster nur an den Tagen, an denen er sich ändert (``CapacityTable`` setzt den
    letzten Wert fort, das Ergebnis ist also gleichwertig).

    @relation(SWR-WXX-070, scope=function)
    """
    out = []
    last: dict[tuple[str, int], float] = {}
    for tech, no, day, mw in sorted(rows, key=lambda r: (r[0], r[1], r[2])):
        key = (tech, int(no))
        if last.get(key) != mw:
            out.append((tech, int(no), day, float(mw)))
            last[key] = mw
    return out


@dataclass
class Weights:
    """Gewichte aller Regionen eines Gold-Pakets.

    ``clusters``: je Gitterzelle ``region``, ``technology``, ``cluster_no``, ``lat``, ``lon``,
    ``share`` (Summe je Region 1) und ``hub_height_m`` je Region; ``capacity``: Bestand je
    Cluster an den Tagen seiner Änderung (nur bei Clustern, sonst ``None``); ``cells``: Stand der
    Clusterung (Zelle → Cluster samt Zentrum und Region), mit dem der nächste Lauf sie
    fortschreibt (nur im Gewichte-Artefakt).
    """

    version: str
    clusters: pa.Table
    capacity: pa.Table | None = None
    info: dict[str, str] = field(default_factory=dict)
    cells: pa.Table | None = None

    # --- Aufbau ------------------------------------------------------------------------------

    @classmethod
    def from_rows(
        cls,
        version: str,
        cells: Sequence[tuple[str, str, int, float, float, float, float | None]],
        capacity: Iterable[tuple[str, int, date, float]] | None = None,
        info: dict[str, str] | None = None,
    ) -> Weights:
        """``cells``: ``(region, technology, cluster_no, lat, lon, share, hub_height_m)``."""
        cells = sorted(cells, key=lambda c: (c[0], c[3], c[4]))
        clusters = pa.table(
            {
                "region": pa.array([c[0] for c in cells], pa.string()),
                "technology": pa.array([c[1] for c in cells], pa.string()),
                "cluster_no": pa.array([c[2] for c in cells], pa.int32()),
                "lat": pa.array([c[3] for c in cells], pa.float64()),
                "lon": pa.array([c[4] for c in cells], pa.float64()),
                "share": pa.array([c[5] for c in cells], pa.float64()),
                "hub_height_m": pa.array([c[6] for c in cells], pa.float64()),
            }
        )
        cap = None
        if capacity is not None:
            points = change_points(capacity)
            cap = pa.table(
                {
                    "technology": pa.array([p[0] for p in points], pa.string()),
                    "cluster_no": pa.array([p[1] for p in points], pa.int32()),
                    "day": pa.array([p[2] for p in points], pa.date32()),
                    "capacity_mw": pa.array([p[3] for p in points], pa.float64()),
                }
            )
        return cls(version, clusters, cap, dict(info or {}))

    @classmethod
    def from_region_sets(cls, sets: Sequence[RegionSet]) -> Weights:
        """Feste Regionen (Zonen, Standorte) wie ``RegionSet.clusters``: Code ``gefs:<code>``.

        @relation(SWR-WXX-070, scope=function)
        """
        if not sets:
            raise ValueError("keine festen Regionen")
        cells = []
        for rs in sets:
            for no, r in enumerate(rs.regions):
                for lat, lon, share in zip(r.lat, r.lon, r.share, strict=True):
                    cells.append(
                        (
                            REGION_PREFIX + r.code,
                            r.technology,
                            no,
                            float(lat),
                            float(lon),
                            float(share),
                            r.hub_height_m,
                        )
                    )
        version = ".".join(rs.version for rs in sets)
        return cls.from_rows(version, cells, None, {"source": "config"})

    @classmethod
    def from_db(cls, engine: Engine, grid: str, version: str | None = None) -> Weights:
        """Gewichte einer Clusterung aus ``assets`` (Standard: die jüngste auf ``grid``).

        @relation(SWR-WXX-070, scope=function)
        """
        store = Store(engine)
        version = version or store.latest_clustering(grid)
        if version is None:
            raise LookupError(f"keine Clusterung mit Gewichten auf {grid}")
        weights = store.cluster_weights(version, grid)
        if not weights:
            raise LookupError(f"Clusterung {version}: keine Gewichte auf {grid}")
        with engine.connect() as c:
            codes: dict[int, str] = dict(
                c.execute(
                    text("SELECT region_id, code FROM meta.region WHERE region_id = ANY(:ids)"),
                    {"ids": sorted({w.region_id for w in weights})},
                ).all()
            )
            snap = c.execute(
                text("""
                SELECT s.snapshot_id, s.export_date FROM assets.snapshot s
                WHERE s.snapshot_id = (SELECT max(snapshot_id) FROM assets.grid_weight
                                       WHERE clustering_version = :v AND grid = :g)"""),
                {"v": version, "g": grid},
            ).one_or_none()
        cells = [
            (
                str(codes[w.region_id]),
                w.technology,
                w.cluster_no,
                float(lat),
                float(lon),
                float(share),
                w.hub_height_m,
            )
            for w in weights
            for lat, lon, share in zip(w.lat, w.lon, w.share, strict=True)
        ]
        with engine.connect() as c:
            cap_rows = c.execute(
                text("""
                SELECT technology, cluster_no, day, capacity_mw FROM assets.capacity_daily
                WHERE clustering_version = :v"""),
                {"v": version},
            ).all()
            meta = c.execute(
                text("""
                SELECT k.grid, k.grid_res, k.params, s.export_date
                FROM assets.clustering k LEFT JOIN assets.snapshot s USING (snapshot_id)
                WHERE k.clustering_version = :v"""),
                {"v": version},
            ).one()
            state = c.execute(
                text("""
                SELECT cc.technology, cc.cluster_no, cc.lat, cc.lon, cl.centroid_lat,
                       cl.centroid_lon, r.code
                FROM assets.cluster_cell cc
                JOIN assets.cluster cl USING (clustering_version, technology, cluster_no)
                LEFT JOIN meta.region r ON r.region_id = cl.region_id
                WHERE cc.clustering_version = :v
                ORDER BY 1, 2, 3, 4"""),
                {"v": version},
            ).all()
        rows = [(str(t), int(n), d, float(m)) for t, n, d, m in cap_rows]
        info = {
            "source": "db",
            "grid": grid,
            "clustering_grid": str(meta[0]),
            "clustering_grid_res": str(meta[1]),
            "clustering_params": json.dumps(meta[2] or {}, sort_keys=True),
        }
        if meta[3] is not None:
            info["clustering_export_date"] = meta[3].isoformat()
        if snap is not None:
            info["mastr_export_date"] = snap[1].isoformat()
        out = cls.from_rows(version, cells, rows, info)
        out.cells = pa.table(
            {
                "technology": pa.array([r[0] for r in state], pa.string()),
                "cluster_no": pa.array([int(r[1]) for r in state], pa.int32()),
                "lat": pa.array([float(r[2]) for r in state], pa.float64()),
                "lon": pa.array([float(r[3]) for r in state], pa.float64()),
                "centroid_lat": pa.array([float(r[4]) for r in state], pa.float64()),
                "centroid_lon": pa.array([float(r[5]) for r in state], pa.float64()),
                "region": pa.array([r[6] for r in state], pa.string()),
            }
        )
        return out

    # --- Nutzung -----------------------------------------------------------------------------

    @property
    def regions(self) -> list[str]:
        """Region-Codes in fester Reihenfolge; der Index ist die ``region_id`` im Builder."""
        return sorted(set(self.clusters.column("region").to_pylist()))

    def cluster_weights(self) -> list[ClusterWeights]:
        """@relation(SWR-WXX-072, scope=function)"""
        index = {code: i for i, code in enumerate(self.regions)}
        cols = self.clusters.to_pydict()
        grouped: dict[str, list[int]] = {}
        for i, code in enumerate(cols["region"]):
            grouped.setdefault(code, []).append(i)
        out = []
        for code in self.regions:
            rows = grouped[code]
            hub = cols["hub_height_m"][rows[0]]
            out.append(
                ClusterWeights(
                    cols["technology"][rows[0]],
                    int(cols["cluster_no"][rows[0]]),
                    index[code],
                    np.array([cols["lat"][i] for i in rows], dtype=np.float64),
                    np.array([cols["lon"][i] for i in rows], dtype=np.float64),
                    np.array([cols["share"][i] for i in rows], dtype=np.float64),
                    None if hub is None else float(hub),
                )
            )
        return out

    def capacity_table(self) -> CapacityTable | None:
        if self.capacity is None:
            return None
        c = self.capacity.to_pydict()
        return CapacityTable(
            zip(c["technology"], c["cluster_no"], c["day"], c["capacity_mw"], strict=True)
        )

    # --- Artefakt ----------------------------------------------------------------------------

    def layers(self, *, state: bool = False) -> list[tuple[str, bytes]]:
        """``(mediaType, Daten)`` der Gewichte; dieselben Bytes im Gewichte- und Gold-Paket.

        ``state`` nimmt den Stand der Clusterung (``cells``) dazu, nur fürs Gewichte-Artefakt.
        """
        out = [(CLUSTERS_TYPE, _parquet(self.clusters))]
        if self.capacity is not None:
            out.append((CAPACITY_TYPE, _parquet(self.capacity)))
        if state and self.cells is not None:
            out.append((CELLS_TYPE, _parquet(self.cells)))
        return out

    @property
    def digest(self) -> str:
        """Inhalt der Gewichte (Hash über die Layer), Teil des Gold-Fingerabdrucks."""
        h = hashlib.sha256()
        for media, data in self.layers():
            h.update(media.encode() + b"\0" + _sha(data).encode())
        return h.hexdigest()[:16]

    @classmethod
    def from_layers(cls, version: str, layers: dict[str, bytes], info: dict[str, str]) -> Weights:
        """@relation(SWR-WXX-072, scope=function)"""
        if CLUSTERS_TYPE not in layers:
            raise ArtifactError("Gewichte ohne clusters.parquet")
        try:
            clusters = pq.read_table(io.BytesIO(layers[CLUSTERS_TYPE]))
            cap = layers.get(CAPACITY_TYPE)
            capacity = pq.read_table(io.BytesIO(cap)) if cap is not None else None
            raw = layers.get(CELLS_TYPE)
            cells = pq.read_table(io.BytesIO(raw)) if raw is not None else None
        except (pa.ArrowException, OSError) as exc:
            raise ArtifactError(f"Gewichte unlesbar: {exc}") from exc
        need = {"region", "technology", "cluster_no", "lat", "lon", "share", "hub_height_m"}
        if not need <= set(clusters.column_names):
            raise ArtifactError(
                f"clusters.parquet: Spalten fehlen: {need - set(clusters.column_names)}"
            )
        if capacity is not None and not {"technology", "cluster_no", "day", "capacity_mw"} <= set(
            capacity.column_names
        ):
            raise ArtifactError("capacity.parquet: Spalten fehlen")
        if cells is not None and not set(CELL_COLUMNS) <= set(cells.column_names):
            raise ArtifactError("cells.parquet: Spalten fehlen")
        return cls(version, clusters, capacity, info, cells)


def _annotations(info: dict[str, str]) -> dict[str, str]:
    return {PREFIX + "weights_" + k: v for k, v in sorted(info.items())}


def _info(annotations: dict[str, str]) -> dict[str, str]:
    p = PREFIX + "weights_"
    return {k[len(p) :]: v for k, v in annotations.items() if k.startswith(p)}


def _descriptor(media: str, digest_: str, size: int) -> dict[str, Any]:
    return {
        "mediaType": media,
        "digest": digest_,
        "size": size,
        "annotations": {"org.opencontainers.image.title": TITLES[media]},
    }


def publish_weights(registry: Registry, weights: Weights, source: str) -> list[str]:
    """Gewichte als Artefakt; Tags ``<version>``, ``<version>-<MaStR-Export>`` und ``latest``.

    @relation(SWR-WXX-070, scope=function)
    """
    descriptors = []
    for media, data in weights.layers(state=True):
        descriptors.append(_descriptor(media, registry.push_blob(data), len(data)))
    manifest = {
        "schemaVersion": 2,
        "mediaType": MANIFEST,
        "artifactType": WEIGHTS_ARTIFACT_TYPE,
        "config": EMPTY,
        "layers": descriptors,
        "annotations": {
            "org.opencontainers.image.source": source,
            "org.opencontainers.image.created": datetime.now(UTC).isoformat(timespec="seconds"),
            "org.opencontainers.image.licenses": "DL-DE-BY-2.0",
            PREFIX + "attribution": (
                "Marktstammdatenregister der Bundesnetzagentur (Datenlizenz Deutschland -"
                " Namensnennung - Version 2.0), Cluster und Gewichte von PredictionDB"
            ),
            PREFIX + "version": weights.version,
            PREFIX + "content": weights.digest,
            **_annotations(weights.info),
        },
    }
    tags = [weights.version]
    if "mastr_export_date" in weights.info:
        tags.append(f"{weights.version}-{weights.info['mastr_export_date']}")
    tags.append(LATEST)
    for t in tags:
        registry.put_manifest(t, manifest)
    return tags


def _layer_blobs(registry: Registry, manifest: dict[str, Any], types: set[str]) -> dict[str, bytes]:
    out = {}
    for d in manifest_layers(manifest):
        media = d.get("mediaType")
        if media in types:
            out[str(media)] = registry.blob(check_digest(d.get("digest")))
    return out


def load_weights(registry: Registry, ref: str = LATEST) -> Weights:
    """Gewichte aus dem Gewichte-Artefakt (Tag ``ref``).

    @relation(SWR-WXX-072, scope=function)
    """
    m = registry.manifest(ref)
    if m is None:
        raise LookupError(
            f"keine Gewichte {registry.repository}:{ref} (Workflow mastr-weights.yml oder auf"
            " der VM wx-exchange gold weights-publish)"
        )
    a = annotations_of(m)
    version = a.get(PREFIX + "version", "")
    if not re.fullmatch(_VERSION, version):
        raise ArtifactError(f"Gewichte: ungültige Version {version!r}")
    blobs = _layer_blobs(registry, m, {CLUSTERS_TYPE, CAPACITY_TYPE, CELLS_TYPE})
    return Weights.from_layers(version, blobs, _info(a))


# --- Fingerabdruck und Plan ------------------------------------------------------------------


def silver_digest(art: MonthArtifact) -> str:
    """Hash über die Inhalts-Hashes aller Silber-Läufe eines Monats."""
    h = hashlib.sha256()
    for run in sorted(art.layers):
        h.update(f"{run:%Y%m%d%H}:{art.layers[run].content}\n".encode())
    return h.hexdigest()[:16]


def fingerprint(process: ProcessConfig, weights: Weights, silver: str) -> str:
    """Eingaben eines Gold-Monats: gleiche Eingaben ergeben dasselbe Gold.

    @relation(SWR-WXX-071, scope=function)
    """
    data = {
        "features_version": FEATURES_VERSION,
        "technologies": {k: v.model_dump() for k, v in sorted(process.technologies.items())},
        "pv": process.pv.model_dump(),
        "weights": weights.digest,
        "silver": silver,
    }
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()[:16]


def plan(
    gold: Registry,
    silver: Registry,
    process: ProcessConfig,
    weights: Weights,
    start: datetime,
    end: datetime,
    hours: list[int],
    *,
    rebuild: bool = False,
) -> list[dict[str, Any]]:
    """Monat-Laufzeit-Paare mit Silber im Zeitraum, deren Gold fehlt oder veraltet ist.

    Reihenfolge: Laufzeiten wie in ``hours``, darin die jüngsten Monate zuerst.

    @relation(SWR-WXX-073, scope=function)
    """
    present = {p for p in map(parse_tag, silver.tags()) if p is not None}
    existing = set(gold.tags())
    first, last = start.strftime("%Y-%m"), end.strftime("%Y-%m")
    out = []
    for hour in hours:
        for month in sorted({m for m, h in present if h == hour}, reverse=True):
            if not first <= month <= last:
                continue
            art = MonthArtifact.from_manifest(month, hour, silver.manifest(silver_tag(month, hour)))
            if not art.layers:
                continue
            t = gold_tag(weights.version, month, hour)
            fp = fingerprint(process, weights, silver_digest(art))
            if not rebuild and t in existing:
                m = gold.manifest(t)
                if m is not None and annotations_of(m).get(PREFIX + "fingerprint") == fp:
                    continue
            out.append({"month": month, "hour": hour, "runs": len(art.layers)})
    return out


# --- Bau -------------------------------------------------------------------------------------


GOLD_SCHEMA = pa.schema(
    [
        ("run_time", pa.timestamp("s", tz="UTC")),
        ("valid_time", pa.timestamp("s", tz="UTC")),
        ("region", pa.string()),
        ("feature", pa.string()),
        ("value", pa.float32()),
    ]
)


def run_table(
    ds_run: datetime,
    valid: list[datetime],
    series: Iterable[Any],
    regions: list[str],
) -> pa.Table:
    """Features eines Laufs in Langform, ohne NaN (wie ``feat.nwp_value``)."""
    run_col: list[datetime] = []
    valid_col: list[datetime] = []
    region_col: list[str] = []
    feature_col: list[str] = []
    value_col: list[np.ndarray] = []
    for s in series:
        v = np.asarray(s.values, dtype=np.float64)
        ok = ~np.isnan(v)
        n = int(ok.sum())
        if not n:
            continue
        run_col.extend([ds_run] * n)
        valid_col.extend(t for t, keep in zip(valid, ok, strict=True) if keep)
        region_col.extend([regions[s.region_id]] * n)
        feature_col.extend([s.code] * n)
        value_col.append(v[ok].astype(np.float32))
    values = np.concatenate(value_col) if value_col else np.array([], dtype=np.float32)
    return pa.table(
        [
            pa.array(run_col, GOLD_SCHEMA.field("run_time").type),
            pa.array(valid_col, GOLD_SCHEMA.field("valid_time").type),
            pa.array(region_col, pa.string()),
            pa.array(feature_col, pa.string()),
            pa.array(values, pa.float32()),
        ],
        schema=GOLD_SCHEMA,
    )


def gold_parquet(tables: list[pa.Table]) -> tuple[bytes, int]:
    """Alle Läufe eines Monats sortiert nach Feature, Region, Lauf, Zeit; Codes als Dictionary.

    @relation(SWR-WXX-071, scope=function)
    """
    table = pa.concat_tables(tables) if tables else GOLD_SCHEMA.empty_table()
    table = table.sort_by(
        [
            ("feature", "ascending"),
            ("region", "ascending"),
            ("run_time", "ascending"),
            ("valid_time", "ascending"),
        ]
    )
    table = table.set_column(2, "region", pc.dictionary_encode(table.column("region")))
    table = table.set_column(3, "feature", pc.dictionary_encode(table.column("feature")))
    buf = io.BytesIO()
    pq.write_table(
        table,
        buf,
        compression="zstd",
        compression_level=ZSTD_LEVEL,
        row_group_size=4_000_000,
        use_byte_stream_split=["value"],
    )
    return buf.getvalue(), table.num_rows


@dataclass
class GoldResult:
    tag: str
    status: str
    runs: int = 0
    rows: int = 0
    bytes: int = 0
    coverage_min: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return vars(self).copy()


class GoldBuilder:
    """Rechnet Gold-Monate im Runner und schreibt sie nach GHCR."""

    def __init__(
        self,
        gold: Registry,
        silver: Registry,
        process: ProcessConfig,
        weights: Weights,
        *,
        source_url: str,
        workers: int = 4,
    ) -> None:
        self.gold = gold
        self.silver = silver
        self.process = process
        self.weights = weights
        self.source_url = source_url
        self.workers = max(1, workers)
        self.builder = FeatureBuilder(process.technologies, process.pv)
        self.clusters = weights.cluster_weights()
        self.capacity = weights.capacity_table()
        self.regions = weights.regions

    def _run(self, art: MonthArtifact, run: datetime) -> tuple[pa.Table, float | None]:
        layer = art.layers[run]
        data = self.silver.blob(layer.digest)
        store = MemoryStore()
        silver = Silver(store)
        unpack(files_tar(data), store, silver.key(art.model, art.resolution, run))
        ds = silver.read(art.model, art.resolution, run)
        valid = ds.valid_times
        capacity = None
        if self.capacity is not None:
            table = self.capacity
            days = [v.date() for v in valid]

            def capacity(tech: str, no: int) -> np.ndarray | None:
                return table.at(tech, no, days)

        series, coverage = self.builder.build(ds, self.clusters, capacity)
        return run_table(run, valid, series, self.regions), (
            min(coverage.values()) if coverage else None
        )

    def build_month(self, month: str, hour: int, *, rebuild: bool = False) -> GoldResult:
        """@relation(SWR-WXX-072, scope=function)"""
        t = gold_tag(self.weights.version, month, hour)
        art = MonthArtifact.from_manifest(
            month, hour, self.silver.manifest(silver_tag(month, hour))
        )
        if not art.layers:
            log.info("gold.no_silver", tag=t)
            return GoldResult(t, "no_silver")
        sdigest = silver_digest(art)
        fp = fingerprint(self.process, self.weights, sdigest)
        old = self.gold.manifest(t)
        if (
            not rebuild
            and old is not None
            and annotations_of(old).get(PREFIX + "fingerprint") == fp
        ):
            log.info("gold.unchanged", tag=t)
            return GoldResult(t, "unchanged", len(art.layers))
        runs = sorted(art.layers)
        log.info("gold.month_start", tag=t, runs=len(runs))
        with ThreadPoolExecutor(self.workers) as pool:
            results = list(pool.map(lambda r: self._run(art, r), runs))
        data, rows = gold_parquet([tbl for tbl, _ in results])
        covs = [c for _, c in results if c is not None]
        coverage = round(min(covs), 4) if covs else None

        descriptors = [
            _descriptor(GOLD_TYPE, self.gold.push_blob(data), len(data)),
            *(
                _descriptor(media, self.gold.push_blob(blob), len(blob))
                for media, blob in self.weights.layers()
            ),
        ]
        manifest = {
            "schemaVersion": 2,
            "mediaType": MANIFEST,
            "artifactType": ARTIFACT_TYPE,
            "config": EMPTY,
            "layers": descriptors,
            "annotations": {
                "org.opencontainers.image.source": self.source_url,
                "org.opencontainers.image.created": datetime.now(UTC).isoformat(timespec="seconds"),
                "org.opencontainers.image.licenses": LICENSE,
                PREFIX + "attribution": ATTRIBUTION,
                PREFIX + "source": self.process.source,
                PREFIX + "model": art.model,
                PREFIX + "grid": art.resolution,
                PREFIX + "month": month,
                PREFIX + "hour": str(hour),
                PREFIX + "fingerprint": fp,
                PREFIX + "silver": sdigest,
                PREFIX + "features_version": FEATURES_VERSION,
                PREFIX + "aggregation": self.process.aggregation,
                PREFIX + "version": self.weights.version,
                PREFIX + "weights": self.weights.digest,
                PREFIX + "runs": ",".join(f"{r:%Y%m%d%H}" for r in runs),
                PREFIX + "missing": ",".join(f"{r:%Y%m%d%H}" for r in sorted(art.missing)),
                PREFIX + "rows": str(rows),
                PREFIX + "coverage_min": "" if coverage is None else str(coverage),
                **_annotations(self.weights.info),
            },
        }
        self.gold.put_manifest(t, manifest)
        log.info("gold.published", tag=t, runs=len(runs), rows=rows, bytes=len(data))
        return GoldResult(t, "published", len(runs), rows, len(data), coverage)


def files_tar(data: bytes) -> bytes:
    """Prüft den Silber-Tar (Pfade, Größen) und gibt ihn unverändert weiter."""
    files_from_tar(data)
    return data


def load_month(registry: Registry, tag_: str) -> tuple[pa.Table, Weights, dict[str, str]]:
    """Gold eines Monats samt Gewichten und Annotationen (für Snapshots ohne Datenbank).

    @relation(SWR-WXX-072, scope=function)
    """
    m = registry.manifest(tag_)
    if m is None:
        raise LookupError(f"kein Gold {registry.repository}:{tag_}")
    a = annotations_of(m)
    blobs = _layer_blobs(registry, m, {GOLD_TYPE, CLUSTERS_TYPE, CAPACITY_TYPE})
    if GOLD_TYPE not in blobs:
        raise ArtifactError(f"{tag_}: kein gold.parquet")
    try:
        gold = pq.read_table(io.BytesIO(blobs[GOLD_TYPE]))
    except (pa.ArrowException, OSError) as exc:
        raise ArtifactError(f"{tag_}: gold.parquet unlesbar: {exc}") from exc
    if gold.schema.names != GOLD_SCHEMA.names:
        raise ArtifactError(f"{tag_}: unerwartete Spalten {gold.schema.names}")
    weights = Weights.from_layers(a.get(PREFIX + "version", ""), blobs, _info(a))
    return gold, weights, a
