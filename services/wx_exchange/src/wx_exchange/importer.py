"""Importeur auf der VM: Silber-Läufe aus GHCR nach ``wx-silver`` holen.

Liest die Tag-Liste, wählt die Manifeste im Zeitraum und für die gewünschten Laufzeiten und
holt jeden Lauf, der lokal nicht als vollständiges Silber der aktuellen ``SILVER_VERSION``
vorliegt. Bronze bleibt dabei leer; ``wx-process`` rechnet aus dem importierten Silber nur die
Gold-Features (``WxPipeline.imported_silver``).
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

import obstore
from obstore.store import ObjectStore

from predictiondb_core.log import get_logger
from wx_exchange.artifact import MonthArtifact, month_bounds, parse_tag, unpack
from wx_exchange.oci import Registry, RegistryError
from wx_process.silver import Silver
from wx_process.variables import SILVER_VERSION

log = get_logger(__name__)

ImportStatus = Literal["imported", "present", "other_version", "failed"]


@dataclass
class ImportResult:
    runs: dict[str, ImportStatus] = field(default_factory=dict)
    bytes: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "counts": dict(Counter(self.runs.values())),
            "failed": sorted(r for r, s in self.runs.items() if s == "failed"),
            "bytes": self.bytes,
        }


class Importer:
    def __init__(
        self, registry: Registry, silver: ObjectStore, model: str = "gfs", resolution: str = "0p25"
    ) -> None:
        self.registry = registry
        self.store = silver
        self.silver = Silver(silver)
        self.model = model
        self.resolution = resolution

    def present(self, run: datetime) -> bool:
        attrs = self.silver.attrs(self.model, self.resolution, run)
        return attrs is not None and attrs.get("silver_version") == SILVER_VERSION

    def _clear(self, prefix: str) -> None:
        for batch in obstore.list(self.store, prefix=prefix + "/"):
            obstore.delete(self.store, [m["path"] for m in batch])

    def run(
        self,
        start: datetime,
        end: datetime,
        hours: list[int] | None = None,
        *,
        force: bool = False,
    ) -> ImportResult:
        result = ImportResult()
        tags = []
        for t in self.registry.tags():
            parsed = parse_tag(t)
            if parsed is None or (hours is not None and parsed[1] not in hours):
                continue
            a, b = month_bounds(parsed[0])
            if b >= start and a <= end:
                tags.append((t, *parsed))
        for t, month, hour in sorted(tags, reverse=True):  # jüngste Monate zuerst
            art = MonthArtifact.from_manifest(month, hour, self.registry.manifest(t))
            for run in sorted(art.layers, reverse=True):
                if not start <= run <= end:
                    continue
                name = f"{run:%Y%m%d%H}"
                layer = art.layers[run]
                if layer.silver_version != SILVER_VERSION:
                    result.runs[name] = "other_version"
                    continue
                if not force and self.present(run):
                    result.runs[name] = "present"
                    continue
                try:
                    data = self.registry.blob(layer.digest)
                    prefix = self.silver.key(self.model, self.resolution, run)
                    self._clear(prefix)
                    unpack(data, self.store, prefix)
                except (RegistryError, ValueError, OSError) as exc:
                    log.warning("import_failed", run=name, error=str(exc))
                    result.runs[name] = "failed"
                    continue
                result.runs[name] = "imported"
                result.bytes += len(data)
                log.info("run_imported", run=name, tag=t, bytes=len(data))
        return result
