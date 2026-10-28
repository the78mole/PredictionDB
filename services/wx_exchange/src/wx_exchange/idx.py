"""Vollständige ``.idx`` für Archive ohne Inventar (NCAR GDEX d084001, GFS vor v16).

Je GRIB-Datei entsteht aus den Nachrichtenköpfen eine ``.idx`` im Format von ``wgrib2 -s``
mit allen Nachrichten (:meth:`wx_ingest.gfs.Endpoint.complete_inventory`), nicht nur den für
Silber gewählten. Abgelegt wird alles in einem OCI-Artefakt::

    ghcr.io/the78mole/predictiondb-data/gfs-0p25-gdex-idx:v1

Ein Layer je Monat und Laufzeit (``tar.gz`` mit den ``.idx``, Pfade wie im Archiv, z. B.
``2019/20190701/gfs.0p25.2019070100.f003.grib2.idx``). Die Annotationen eines Layers nennen
Monat und Laufzeit und halten den Stand fest: fehlende Dateien im Archiv (``absent``, gelten als
erledigt), fehlgeschlagene (``failed``, holt ein Folgelauf nach) und Zeilen, deren Name oder
Fläche nicht in den Tabellen steht (``unknown``). Wer nur einen Monat braucht, sucht den Layer
im Manifest und lädt genau diesen Blob (``oras blob fetch …@sha256:…``).

Ablauf im Workflow ``gfs-idx.yml``: :func:`plan` wählt Monat-Laufzeit-Paare ohne vollständigen
Layer, je Matrix-Job baut :func:`build_layer` einen Layer und lädt den Blob hoch, der
Abschlussjob schreibt mit :func:`merge_manifest` das eine Manifest neu. Vorhandene Layer bleiben
liegen; ein Folgelauf ergänzt nur, was fehlt, und übernimmt dabei die schon gebauten ``.idx``
eines unvollständigen Layers.
"""

from __future__ import annotations

import asyncio
import gzip
import io
import json
import tarfile
import time
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from gribkit.headers import idx_line, is_known
from gribkit.idx import parse_idx
from predictiondb_core.log import get_logger
from wx_exchange.artifact import ATTRIBUTION_GDEX, LICENSE, PREFIX, month_bounds
from wx_exchange.oci import EMPTY, MANIFEST, Registry
from wx_exchange.publish import months_between
from wx_ingest.config import GfsConfig
from wx_ingest.gfs import Endpoint, StepUnavailable

log = get_logger(__name__)

DEFAULT_REPOSITORY = "the78mole/predictiondb-data/gfs-0p25-gdex-idx"
TAG = "v1"
ARTIFACT_TYPE = "application/vnd.predictiondb.gfs-idx.v1"
LAYER_TYPE = "application/vnd.predictiondb.gfs-idx.tar+gzip"
#: Unbekannte Namen höchstens so oft einzeln in den Annotationen nennen
_MAX_LISTED = 20


def layer_key(month: str, hour: int) -> str:
    """``2019-07`` + 0 → ``2019-07-00z`` (wie die Silber-Tags)."""
    return f"{month}-{hour:02d}z"


def file_code(run: datetime, step: int) -> str:
    """Kurzform für die Annotationen: ``2019070100f003``."""
    return f"{run:%Y%m%d%H}f{step:03d}"


def file_name(config: GfsConfig, run: datetime, step: int) -> str:
    """Pfad der ``.idx`` im Tar: Pfad der GRIB-Datei im Archiv plus ``.idx``."""
    ep = next(e for e in config.endpoints if e.kind == "header_scan")
    assert ep.path is not None
    return ep.path.format(run=run, step=step) + ".idx"


@dataclass
class IdxLayer:
    """Stand eines Layers (Monat und Laufzeit) aus seinen Annotationen."""

    month: str
    hour: int
    digest: str = ""
    size: int = 0
    runs: int = 0
    files: int = 0
    #: Dateien als :func:`file_code`
    absent: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    unknown: int = 0
    unknown_names: list[str] = field(default_factory=list)
    built: str = ""

    @property
    def key(self) -> str:
        return layer_key(self.month, self.hour)

    @property
    def complete(self) -> bool:
        return bool(self.digest) and not self.failed

    def descriptor(self) -> dict[str, Any]:
        return {
            "mediaType": LAYER_TYPE,
            "digest": self.digest,
            "size": self.size,
            "annotations": {
                "org.opencontainers.image.title": f"gfs-0p25-gdex-idx-{self.key}.tar.gz",
                PREFIX + "month": self.month,
                PREFIX + "hour": str(self.hour),
                PREFIX + "runs": str(self.runs),
                PREFIX + "files": str(self.files),
                PREFIX + "complete": str(self.complete).lower(),
                PREFIX + "absent": ",".join(self.absent),
                PREFIX + "failed": ",".join(self.failed),
                PREFIX + "unknown": str(self.unknown),
                PREFIX + "unknown_names": json.dumps(self.unknown_names, ensure_ascii=False),
                PREFIX + "built": self.built,
            },
        }

    @classmethod
    def from_descriptor(cls, d: dict[str, Any]) -> IdxLayer:
        a = d.get("annotations", {})

        def items(name: str) -> list[str]:
            return [x for x in a.get(PREFIX + name, "").split(",") if x]

        return cls(
            month=a[PREFIX + "month"],
            hour=int(a[PREFIX + "hour"]),
            digest=d["digest"],
            size=int(d["size"]),
            runs=int(a.get(PREFIX + "runs", 0)),
            files=int(a.get(PREFIX + "files", 0)),
            absent=items("absent"),
            failed=items("failed"),
            unknown=int(a.get(PREFIX + "unknown", 0)),
            unknown_names=json.loads(a.get(PREFIX + "unknown_names", "[]")),
            built=a.get(PREFIX + "built", ""),
        )


def read_layers(registry: Registry) -> dict[str, IdxLayer]:
    """Layer des Manifests nach Schlüssel (leer, wenn es das Artefakt noch nicht gibt)."""
    m = registry.manifest(TAG)
    out = {}
    for d in (m or {}).get("layers", []):
        if d.get("mediaType") == LAYER_TYPE:
            layer = IdxLayer.from_descriptor(d)
            out[layer.key] = layer
    return out


def _runs(
    config: GfsConfig, month: str, hour: int, start: datetime, end: datetime
) -> list[datetime]:
    a, b = month_bounds(month)
    return config.runs_between(max(a, start), min(b, end), [hour])


def plan(
    registry: Registry,
    config: GfsConfig,
    start: datetime,
    end: datetime,
    hours: list[int],
    *,
    limit: int = 256,
    rebuild: bool = False,
) -> list[dict[str, Any]]:
    """Matrix der Monat-Laufzeit-Paare ohne vollständigen Layer.

    Unvollständig heißt: kein Layer, fehlgeschlagene Dateien oder weniger Läufe als im
    Zeitraum. Reihenfolge erst alle Monate der ersten Laufzeit in ``hours`` (jüngste zuerst),
    dann der nächsten; höchstens ``limit`` Einträge (Matrix-Grenze von Actions), den Rest
    nimmt der nächste Start.
    """
    layers = {} if rebuild else read_layers(registry)
    out = []
    for hour in hours:
        for month in months_between(start, end)[::-1]:
            runs = _runs(config, month, hour, start, end)
            if not runs:
                continue
            layer = layers.get(layer_key(month, hour))
            if layer and layer.complete and layer.runs >= len(runs):
                continue
            out.append({"month": month, "hour": hour, "runs": len(runs)})
    return out[:limit]


def pack(files: dict[str, bytes]) -> bytes:
    """Reproduzierbares ``tar.gz`` (sortiert, ohne Zeitstempel und Besitzer)."""
    buf = io.BytesIO()
    with (
        gzip.GzipFile(fileobj=buf, mode="wb", mtime=0) as gz,
        tarfile.open(fileobj=gz, mode="w", format=tarfile.PAX_FORMAT) as tar,
    ):
        for name in sorted(files):
            info = tarfile.TarInfo(name)
            info.size = len(files[name])
            info.mode = 0o644
            tar.addfile(info, io.BytesIO(files[name]))
    return buf.getvalue()


def unpack(data: bytes) -> dict[str, bytes]:
    out = {}
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        for m in tar.getmembers():
            if m.name.startswith("/") or ".." in m.name.split("/"):
                raise ValueError(f"unzulässiger Pfad im Tar: {m.name}")
            f = tar.extractfile(m) if m.isfile() else None
            if f is not None:
                out[m.name] = f.read()
    return out


async def _inventory(endpoints: list[Endpoint], run: datetime, step: int) -> str | None:
    """``.idx``-Text der Datei; ``None``, wenn sie auf keinem Endpunkt liegt.

    Fehler auf einem Endpunkt führen zum nächsten; scheitern alle, folgt der letzte Fehler.
    """
    unavailable = 0
    error: Exception | None = None
    for ep in endpoints:
        try:
            entries = await ep.complete_inventory(run, step)
        except StepUnavailable:
            unavailable += 1
            continue
        except Exception as exc:
            log.warning(
                "idx_endpoint_failed",
                endpoint=ep.name,
                run=f"{run:%Y%m%d%H}",
                step=step,
                error=str(exc),
            )
            error = exc
            continue
        if not entries:
            raise ValueError("Datei ohne Nachrichten")
        return "".join(idx_line(e) + "\n" for e in entries)
    if error is not None:
        raise error
    return None


def _unknown(text: str) -> Iterable[str]:
    for e in parse_idx(text):
        if not is_known(e):
            yield f"{e.var}:{e.level}"


async def build_layer(
    registry: Registry,
    config: GfsConfig,
    endpoints: list[Endpoint],
    month: str,
    hour: int,
    start: datetime,
    end: datetime,
    *,
    parallel_files: int = 12,
    deadline: float | None = None,
    rebuild: bool = False,
) -> IdxLayer:
    """Baut den Layer eines Monats und einer Laufzeit und lädt den Blob hoch.

    Ein vorhandener Layer wird übernommen; neu gelesen werden nur Dateien, die darin weder
    als ``.idx`` noch als im Archiv fehlend stehen. Ab ``deadline`` (``time.monotonic()``)
    beginnt keine neue Datei mehr; der Rest gilt als fehlgeschlagen und kommt beim nächsten
    Start dran. Das Manifest schreibt erst :func:`merge_manifest`.
    """
    runs = _runs(config, month, hour, start, end)
    files: dict[str, bytes] = {}
    absent: set[str] = set()
    old = None if rebuild else read_layers(registry).get(layer_key(month, hour))
    if old is not None:
        files = unpack(registry.blob(old.digest))
        absent = set(old.absent)
    todo = [
        (run, step, file_name(config, run, step))
        for run in runs
        for step in config.step_list()
        if file_name(config, run, step) not in files and file_code(run, step) not in absent
    ]
    log.info(
        "idx_layer_start",
        layer=layer_key(month, hour),
        runs=len(runs),
        present=len(files),
        todo=len(todo),
    )
    failed: list[str] = []
    sem = asyncio.Semaphore(parallel_files)
    done = 0

    async def one(run: datetime, step: int, name: str) -> None:
        nonlocal done
        async with sem:
            if deadline is not None and time.monotonic() > deadline:
                failed.append(file_code(run, step))
                return
            try:
                text = await _inventory(endpoints, run, step)
            except Exception as exc:
                log.warning("idx_failed", file=name, error=str(exc))
                failed.append(file_code(run, step))
                return
            if text is None:
                absent.add(file_code(run, step))
            else:
                files[name] = text.encode()
            done += 1
            if done % 93 == 0 or done == len(todo):
                log.info(
                    "idx_progress",
                    layer=layer_key(month, hour),
                    done=done,
                    todo=len(todo),
                    absent=len(absent),
                    failed=len(failed),
                )

    await asyncio.gather(*(one(*t) for t in todo))
    unknown: Counter[str] = Counter()
    for data in files.values():
        unknown.update(_unknown(data.decode()))
    blob = await asyncio.to_thread(pack, files)
    digest = await asyncio.to_thread(registry.push_blob, blob)
    layer = IdxLayer(
        month=month,
        hour=hour,
        digest=digest,
        size=len(blob),
        runs=len(runs),
        files=len(files),
        absent=sorted(absent),
        failed=sorted(failed),
        unknown=sum(unknown.values()),
        unknown_names=[k for k, _ in unknown.most_common(_MAX_LISTED)],
        built=datetime.now(UTC).isoformat(timespec="seconds"),
    )
    log.info(
        "idx_layer_done",
        layer=layer.key,
        files=layer.files,
        absent=len(layer.absent),
        failed=len(layer.failed),
        unknown=layer.unknown,
        size=layer.size,
    )
    return layer


def manifest(layers: Iterable[IdxLayer], source: str) -> dict[str, Any]:
    ordered = sorted(layers, key=lambda x: (x.month, x.hour))
    return {
        "schemaVersion": 2,
        "mediaType": MANIFEST,
        "artifactType": ARTIFACT_TYPE,
        "config": EMPTY,
        "layers": [x.descriptor() for x in ordered],
        "annotations": {
            "org.opencontainers.image.source": source,
            "org.opencontainers.image.created": datetime.now(UTC).isoformat(timespec="seconds"),
            "org.opencontainers.image.licenses": LICENSE,
            "org.opencontainers.image.description": (
                "Vollständige .idx (Format wgrib2 -s) zu NCAR GDEX d084001, GFS 0,25°; ein"
                " Layer je Monat und Laufzeit, Annotation org.predictiondb.month/.hour"
            ),
            PREFIX + "attribution": ATTRIBUTION_GDEX.replace("; verdichtet", "; Inventare"),
        },
    }


def merge_manifest(registry: Registry, new: Iterable[IdxLayer], source: str) -> dict[str, Any]:
    """Schreibt das Manifest neu: vorhandene Layer bleiben, neue ersetzen gleiche Schlüssel.

    Neue Layer, deren Blob die Registry nicht kennt, bleiben draußen (der Monat wird dann neu
    geplant). Liefert eine Zusammenfassung.
    """
    layers = read_layers(registry)
    replaced, skipped = [], []
    for layer in new:
        if not registry.has_blob(layer.digest):
            skipped.append(layer.key)
            continue
        old = layers.get(layer.key)
        if old is not None and old.digest == layer.digest and old.failed == layer.failed:
            continue
        layers[layer.key] = layer
        replaced.append(layer.key)
    if replaced or not registry.manifest(TAG):
        registry.put_manifest(TAG, manifest(layers.values(), source))
    return {
        "layers": len(layers),
        "complete": sum(1 for x in layers.values() if x.complete),
        "updated": sorted(replaced),
        "skipped": sorted(skipped),
        "failed_files": sum(len(x.failed) for x in layers.values()),
        "absent_files": sum(len(x.absent) for x in layers.values()),
        "unknown_lines": sum(x.unknown for x in layers.values()),
    }
