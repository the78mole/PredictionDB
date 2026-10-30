"""Silber-Läufe als OCI-Artefakt: Tar je Lauf, Manifest je Monat und Laufzeit.

Adressierung (Abweichung von ARCHITEKTUR.md 5.5.3, dort ein Manifest je Monat)::

    ghcr.io/the78mole/predictiondb-data/gfs-0p25-silver:2026-10-00z   # alle 00Z-Läufe im Oktober

Je Monat und Laufzeit ein eigener Tag, damit jeder Matrix-Job genau ein Manifest schreibt und
sich parallele Jobs nie in die Quere kommen. Die Manifeste sind zugleich der Stand des
Austauschs: Welche Läufe enthalten sind, aus welchen Eingaben sie gebaut wurden und welche im
Archiv fehlen, steht in ihren Annotationen. Ein eigener Index ist deshalb nicht nötig, die
Tag-Liste der Registry reicht.

Der Tar eines Laufs ist reproduzierbar (sortiert, ohne Zeitstempel und Besitzer). Weil Silber
aber den Zeitpunkt des Bronze-Abschlusses trägt, unterscheiden sich zwei Bauten desselben Laufs
trotzdem; verglichen wird deshalb der Inhalts-Hash (``content``), der dieses Attribut auslässt.

@relation(SWA-WXX-ARTIFACT, scope=file)
"""

from __future__ import annotations

import hashlib
import io
import json
import tarfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import obstore
from obstore.store import ObjectStore

from wx_exchange.oci import EMPTY, MANIFEST
from wx_ingest.config import GfsConfig
from wx_process.variables import SILVER_VERSION

ARTIFACT_TYPE = "application/vnd.predictiondb.wx-silver.v1"
LAYER_TYPE = "application/vnd.predictiondb.zarr.v3.tar"
LICENSE = "CC-BY-4.0"
ATTRIBUTION = "NOAA/NCEP Global Forecast System (GFS), verdichtet von PredictionDB"
#: Läufe vor GFS v16 stammen aus NCAR GDEX d084001 (CC BY 4.0, Zitat mit DOI)
ATTRIBUTION_GDEX = (
    "NOAA/NCEP Global Forecast System (GFS); Läufe vor 2021-03-22 12Z aus NCEP GFS 0.25 Degree"
    " Global Forecast Grids Historical Archive, Research Data Archive at NCAR,"
    " https://doi.org/10.5065/D65D8PWK; verdichtet von PredictionDB"
)
#: Letzter Monat mit Läufen aus GDEX
GDEX_UNTIL = "2021-03"
PREFIX = "org.predictiondb."
#: Silber-Attribute, die sich bei jedem Bau ändern und nicht zum Inhalt zählen
VOLATILE = ("bronze_completed_at",)


def tag(month: str, hour: int) -> str:
    """``2026-10`` + 0 → ``2026-10-00z``.

    @relation(SWR-WXX-002, scope=function)
    """
    return f"{month}-{hour:02d}z"


def parse_tag(text: str) -> tuple[str, int] | None:
    parts = text.rsplit("-", 1)
    if len(parts) != 2 or not parts[1].endswith("z"):
        return None
    try:
        hour = int(parts[1][:-1])
        datetime.strptime(parts[0], "%Y-%m")  # noqa: DTZ007 - nur Formatprüfung
    except ValueError:
        return None
    return (parts[0], hour) if hour in (0, 6, 12, 18) else None


def month_bounds(month: str) -> tuple[datetime, datetime]:
    """Erster und letzter möglicher Lauf eines Monats (UTC)."""
    start = datetime.strptime(month, "%Y-%m").replace(tzinfo=UTC)
    nxt = (start + timedelta(days=32)).replace(day=1)
    return start, nxt - timedelta(hours=6)


def fingerprint(config: GfsConfig) -> str:
    """Hash der Eingaben eines Silber-Baus: gleiche Eingaben ergeben denselben Inhalt.

    @relation(SWR-WXX-002, scope=function)
    """
    data = {
        "silver_version": SILVER_VERSION,
        "product": config.product,
        "specs": sorted(s.label for s in config.specs()),
        "bbox": config.bbox.model_dump(),
        "steps": config.step_list(),
    }
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()[:16]


# --- Packen ----------------------------------------------------------------------------------


def read_files(store: ObjectStore, prefix: str) -> dict[str, bytes]:
    """Alle Dateien eines Stores unter ``prefix``, Schlüssel relativ dazu."""
    out: dict[str, bytes] = {}
    for batch in obstore.list(store, prefix=prefix + "/"):
        for meta in batch:
            key = meta["path"]
            out[key[len(prefix) + 1 :]] = bytes(obstore.get(store, key).bytes())
    return out


def content_hash(files: dict[str, bytes]) -> str:
    """Hash über alle Dateien des Stores, im Wurzel-``zarr.json`` ohne ``VOLATILE``.

    @relation(SWR-WXX-003, scope=function)
    """
    h = hashlib.sha256()
    for name in sorted(files):
        data = files[name]
        if name == "zarr.json":
            meta = json.loads(data)
            for key in VOLATILE:
                meta.get("attributes", {}).pop(key, None)
            data = json.dumps(meta, sort_keys=True).encode()
        h.update(name.encode() + b"\0" + hashlib.sha256(data).digest())
    return h.hexdigest()


def files_from_tar(data: bytes) -> dict[str, bytes]:
    with tarfile.open(fileobj=io.BytesIO(data), mode="r") as tar:
        out = {}
        for m in tar.getmembers():
            f = tar.extractfile(m) if m.isfile() else None
            if f is not None:
                out[m.name] = f.read()
        return out


def compare(local: dict[str, bytes], remote: dict[str, bytes]) -> dict[str, Any]:
    """Unterschiede zweier Stores: fehlende und abweichende Dateien, abweichende Attribute.

    @relation(SWR-WXX-022, scope=function)
    """

    def attrs(files: dict[str, bytes]) -> dict[str, Any]:
        meta = json.loads(files.get("zarr.json", b"{}"))
        out: dict[str, Any] = meta.get("attributes", {})
        return {k: v for k, v in out.items() if k not in VOLATILE}

    def data(files: dict[str, bytes]) -> dict[str, str]:
        return {
            k: hashlib.sha256(v).hexdigest() for k, v in files.items() if not k.endswith("json")
        }

    a, b = attrs(local), attrs(remote)
    da, db = data(local), data(remote)
    return {
        "attributes": sorted(k for k in a.keys() | b.keys() if a.get(k) != b.get(k)),
        "only_local": sorted(da.keys() - db.keys()),
        "only_remote": sorted(db.keys() - da.keys()),
        "different": sorted(k for k in da.keys() & db.keys() if da[k] != db[k]),
        "metadata": sorted(
            k
            for k in local.keys() | remote.keys()
            if k.endswith("json") and k != "zarr.json" and local.get(k) != remote.get(k)
        ),
    }


def pack(store: ObjectStore, prefix: str) -> tuple[bytes, str]:
    """Silber-Store unter ``prefix`` als reproduzierbarer Tar plus Inhalts-Hash.

    @relation(SWR-WXX-003, scope=function)
    """
    files = read_files(store, prefix)
    if "zarr.json" not in files:
        raise FileNotFoundError(f"kein Silber unter {prefix}")
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for name in sorted(files):
            info = tarfile.TarInfo(name)
            info.size = len(files[name])
            info.mode = 0o644
            tar.addfile(info, io.BytesIO(files[name]))
    return buf.getvalue(), content_hash(files)


def unpack(data: bytes, store: ObjectStore, prefix: str, *, workers: int = 16) -> int:
    """Schreibt den Tar nach ``prefix``; das Wurzel-``zarr.json`` (``complete``) kommt zuletzt.

    Die Dateien gehen parallel hinaus (``workers``): Ein Put nach Garage dauert unter Last
    ≈ 0,3 s, ein GFS-Europa-Lauf hat ≈ 100 Chunks.
    """
    with tarfile.open(fileobj=io.BytesIO(data), mode="r") as tar:
        files: list[tuple[str, bytes]] = []
        for m in tar.getmembers():
            if not m.isfile():
                continue
            if m.name.startswith("/") or ".." in m.name.split("/"):
                raise ValueError(f"unzulässiger Pfad im Tar: {m.name}")
            f = tar.extractfile(m)
            assert f is not None
            files.append((m.name, f.read()))
    root = [f for f in files if f[0] == "zarr.json"]
    rest = [f for f in files if f[0] != "zarr.json"]
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        # list() wartet alle ab und reicht Fehler weiter, bevor zarr.json geschrieben wird
        list(pool.map(lambda f: obstore.put(store, f"{prefix}/{f[0]}", f[1]), rest))
    for name, body in root:
        obstore.put(store, f"{prefix}/{name}", body)
    return len(files)


# --- Manifest --------------------------------------------------------------------------------


@dataclass
class Layer:
    run: datetime
    digest: str
    size: int
    content: str
    fingerprint: str
    silver_version: str = SILVER_VERSION
    steps: int = 0

    def descriptor(self) -> dict[str, Any]:
        """@relation(SWR-WXX-002, scope=function)"""
        return {
            "mediaType": LAYER_TYPE,
            "digest": self.digest,
            "size": self.size,
            "annotations": {
                "org.opencontainers.image.title": f"{self.run:%Y%m%d%H}.zarr.tar",
                PREFIX + "run": self.run.isoformat(),
                PREFIX + "content": self.content,
                PREFIX + "fingerprint": self.fingerprint,
                PREFIX + "silver_version": self.silver_version,
                PREFIX + "steps": str(self.steps),
            },
        }

    @classmethod
    def from_descriptor(cls, d: dict[str, Any]) -> Layer:
        a = d.get("annotations", {})
        return cls(
            run=datetime.fromisoformat(a[PREFIX + "run"]),
            digest=d["digest"],
            size=int(d["size"]),
            content=a.get(PREFIX + "content", ""),
            fingerprint=a.get(PREFIX + "fingerprint", ""),
            silver_version=a.get(PREFIX + "silver_version", ""),
            steps=int(a.get(PREFIX + "steps", 0)),
        )


@dataclass
class MonthArtifact:
    """Ein Manifest: alle Läufe einer Laufzeit in einem Monat."""

    month: str
    hour: int
    model: str = "gfs"
    resolution: str = "0p25"
    layers: dict[datetime, Layer] = field(default_factory=dict)
    #: Läufe, die im Archiv fehlen (nicht bei jedem Start neu versuchen)
    missing: set[datetime] = field(default_factory=set)

    @property
    def tag(self) -> str:
        return tag(self.month, self.hour)

    def done(self, fp: str) -> set[datetime]:
        """Läufe, die mit diesen Eingaben schon vorliegen oder endgültig fehlen."""
        return {r for r, layer in self.layers.items() if layer.fingerprint == fp} | self.missing

    def manifest(self, source: str) -> dict[str, Any]:
        """@relation(SWR-WXX-002, scope=function)"""
        layers = [self.layers[r].descriptor() for r in sorted(self.layers)]
        return {
            "schemaVersion": 2,
            "mediaType": MANIFEST,
            "artifactType": ARTIFACT_TYPE,
            "config": EMPTY,
            "layers": layers,
            "annotations": {
                "org.opencontainers.image.source": source,
                "org.opencontainers.image.created": datetime.now(UTC).isoformat(timespec="seconds"),
                "org.opencontainers.image.licenses": LICENSE,
                PREFIX + "attribution": (
                    ATTRIBUTION_GDEX if self.month <= GDEX_UNTIL else ATTRIBUTION
                ),
                PREFIX + "model": self.model,
                PREFIX + "grid": self.resolution,
                PREFIX + "month": self.month,
                PREFIX + "hour": str(self.hour),
                PREFIX + "missing": ",".join(f"{r:%Y%m%d%H}" for r in sorted(self.missing)),
            },
        }

    @classmethod
    def from_manifest(cls, month: str, hour: int, m: dict[str, Any] | None) -> MonthArtifact:
        art = cls(month, hour)
        if m is None:
            return art
        a = m.get("annotations", {})
        art.model = a.get(PREFIX + "model", art.model)
        art.resolution = a.get(PREFIX + "grid", art.resolution)
        for d in m.get("layers", []):
            if d.get("mediaType") == LAYER_TYPE:
                layer = Layer.from_descriptor(d)
                art.layers[layer.run] = layer
        for text in filter(None, a.get(PREFIX + "missing", "").split(",")):
            art.missing.add(datetime.strptime(text, "%Y%m%d%H").replace(tzinfo=UTC))
        return art
