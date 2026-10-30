"""Importeur auf der VM: Silber-Läufe aus GHCR nach ``wx-silver`` holen.

Liest die Tag-Liste, wählt die Manifeste im Zeitraum und für die gewünschten Laufzeiten und
holt jeden Lauf, der lokal nicht als vollständiges Silber der aktuellen ``SILVER_VERSION``
vorliegt. Ohne Bronze rechnet ``wx-process`` aus dem importierten Silber nur die Gold-Features
(``WxPipeline.imported_silver``); liegt auch Bronze vor (``GFS_BACKFILL_BRONZE=true``), baut
``wx-process`` das Silber daraus selbst, und ``verify`` vergleicht es mit dem aus GHCR.

Mit ``ledger`` (``wx_process.store.Store``) meldet der Import den lokalen Bestand nach
``meta.silver_run`` für das Status-Dashboard: Zu Beginn eines Durchgangs werden alle Läufe,
die er holen wird, ``loading``, jeder geholte wird ``present``, fehlgeschlagene fallen wieder
heraus. Vorhandenes Silber ohne Eintrag wird nachgetragen.

@relation(SWA-WXX-IMPORTER, scope=file)
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

import obstore
from obstore.store import ObjectStore

from predictiondb_core.log import get_logger
from wx_exchange.artifact import (
    Layer,
    MonthArtifact,
    compare,
    files_from_tar,
    month_bounds,
    pack,
    parse_tag,
    read_files,
    unpack,
)
from wx_exchange.oci import Registry, digest
from wx_process.silver import Silver
from wx_process.store import Store
from wx_process.variables import SILVER_VERSION

log = get_logger(__name__)

ImportStatus = Literal["imported", "present", "other_version", "failed"]


VerifyStatus = Literal["equal", "different", "imported", "local_missing"]


@dataclass
class VerifyResult:
    runs: dict[str, VerifyStatus] = field(default_factory=dict)
    details: dict[str, dict[str, Any]] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "counts": dict(Counter(self.runs.values())),
            "different": sorted(r for r, s in self.runs.items() if s == "different"),
            "details": self.details,
        }


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
        self,
        registry: Registry,
        silver: ObjectStore,
        model: str = "gfs",
        resolution: str = "0p25",
        *,
        ledger: Store | None = None,
        source: str = "gfs_0p25",
    ) -> None:
        self.registry = registry
        self.store = silver
        self.silver = Silver(silver)
        self.model = model
        self.resolution = resolution
        self.ledger = ledger
        self.source = source
        self._source_id: int | None = None

    @property
    def source_id(self) -> int:
        if self._source_id is None:
            assert self.ledger is not None
            self._source_id = self.ledger.source_id(self.source)
        return self._source_id

    def present(self, run: datetime) -> bool:
        """@relation(SWR-WXX-020, scope=function)"""
        attrs = self.silver.attrs(self.model, self.resolution, run)
        return attrs is not None and attrs.get("silver_version") == SILVER_VERSION

    def _clear(self, prefix: str) -> None:
        for batch in obstore.list(self.store, prefix=prefix + "/"):
            obstore.delete(self.store, [m["path"] for m in batch])

    def layers(
        self, start: datetime, end: datetime, hours: list[int] | None = None
    ) -> Iterator[tuple[str, Layer]]:
        """Alle Layer im Zeitraum mit ihrem Tag.

        Laufzeiten in der Reihenfolge von ``hours`` (erst alle 00Z, dann 12Z …), darin die
        jüngsten Monate und Läufe zuerst.
        """
        tags = []
        for t in self.registry.tags():
            parsed = parse_tag(t)
            if parsed is None or (hours is not None and parsed[1] not in hours):
                continue
            a, b = month_bounds(parsed[0])
            if b >= start and a <= end:
                tags.append((t, *parsed))
        priority = {h: i for i, h in enumerate(hours or [])}
        tags.sort(key=lambda e: e[1], reverse=True)
        tags.sort(key=lambda e: priority.get(e[2], 0))
        for t, month, hour in tags:
            art = MonthArtifact.from_manifest(month, hour, self.registry.manifest(t))
            for run in sorted(art.layers, reverse=True):
                if start <= run <= end:
                    yield t, art.layers[run]

    def run(
        self,
        start: datetime,
        end: datetime,
        hours: list[int] | None = None,
        *,
        force: bool = False,
        workers: int = 4,
    ) -> ImportResult:
        """@relation(SWR-WXX-020, SWR-WXX-021, scope=function)"""
        result = ImportResult()
        known = self.ledger.silver_runs(self.source_id) if self.ledger is not None else {}
        todo: list[tuple[str, Layer]] = []
        unrecorded: list[datetime] = []
        for t, layer in self.layers(start, end, hours):
            run = layer.run
            name = f"{run:%Y%m%d%H}"
            if layer.silver_version != SILVER_VERSION:
                result.runs[name] = "other_version"
            elif not force and known.get(run) == "present":
                # Bestand schon gemeldet: kein GET je Lauf (≈ 5 000 Layer, ≈ 70 ms je GET)
                result.runs[name] = "present"
            elif not force and self.present(run):
                result.runs[name] = "present"
                unrecorded.append(run)
            else:
                todo.append((t, layer))
        if self.ledger is not None:
            # Reste eines abgebrochenen Durchgangs verwerfen, dann den neuen ankündigen
            self.ledger.clear_silver_loading(self.source_id)
            self.ledger.mark_silver(self.source_id, unrecorded, "present", "ghcr")
            self.ledger.mark_silver(self.source_id, [lay.run for _, lay in todo], "loading", "ghcr")
        try:
            # Mehrere Läufe zugleich: Je Lauf wartet der Import fast nur (Download, viele
            # kleine Puts nach Garage); einzeln kam GFS Europa auf ≈ 30 Läufe pro Stunde.
            with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
                for name, status, nbytes in pool.map(lambda e: self._fetch(*e), todo):
                    result.runs[name] = status
                    result.bytes += nbytes
        finally:
            if self.ledger is not None:
                self.ledger.clear_silver_loading(self.source_id)
        return result

    def _fetch(self, tag: str, layer: Layer) -> tuple[str, ImportStatus, int]:
        run = layer.run
        name = f"{run:%Y%m%d%H}"
        try:
            data = self.registry.blob(layer.digest)
            prefix = self.silver.key(self.model, self.resolution, run)
            if self.silver.attrs(self.model, self.resolution, run) is not None:
                # nur bei vorhandenem Lauf (andere Version, --force): das List kostet unter Last
                # mehrere Sekunden, neue Läufe haben nichts zu löschen
                self._clear(prefix)
            unpack(data, self.store, prefix)
        except Exception as exc:  # ein Lauf (z. B. Garage-Timeout) darf den Import nicht beenden
            log.warning("import_failed", run=name, error=str(exc))
            if self.ledger is not None:
                self.ledger.clear_silver_loading(self.source_id, [run])
            return name, "failed", 0
        if self.ledger is not None:
            self.ledger.mark_silver(self.source_id, [run], "present", "ghcr")
        log.info("run_imported", run=name, tag=tag, bytes=len(data))
        return name, "imported", len(data)

    def verify(
        self,
        start: datetime,
        end: datetime,
        hours: list[int] | None = None,
        *,
        details: int = 5,
    ) -> VerifyResult:
        """Vergleicht lokales Silber mit GHCR über den Inhalts-Hash (ohne Download).

        Für die ersten ``details`` abweichenden Läufe wird der Layer geladen und Datei für
        Datei verglichen, damit sichtbar wird, welche Variablen oder Attribute abweichen.

        @relation(SWR-WXX-022, scope=function)
        """
        result = VerifyResult()
        for _, layer in self.layers(start, end, hours):
            name = f"{layer.run:%Y%m%d%H}"
            if not self.present(layer.run):
                result.runs[name] = "local_missing"
                continue
            prefix = self.silver.key(self.model, self.resolution, layer.run)
            tar, content = pack(self.store, prefix)
            if digest(tar) == layer.digest:
                # byte-gleich, also eine Kopie aus GHCR und kein eigener Bau aus Bronze
                result.runs[name] = "imported"
                continue
            if content == layer.content:
                result.runs[name] = "equal"
                continue
            result.runs[name] = "different"
            if len(result.details) < details:
                remote = files_from_tar(self.registry.blob(layer.digest))
                result.details[name] = compare(read_files(self.store, prefix), remote)
        return result
