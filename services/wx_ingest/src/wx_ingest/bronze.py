"""Ablage zugeschnittener GRIB2-Schritte in Garage (Bronze).

Schlüssel nach ``docs/ARCHITEKTUR.md`` 5.1::

    gfs/0p25/2026/10/20/12/f042.grib2   # GRIB2-Nachrichten des Schritts
    gfs/0p25/2026/10/20/12/f042.json    # Manifest (Quelle, SHA-256, Nachrichten)
    gfs/0p25/2026/10/20/12/run.json     # Laufmanifest, sobald alle Schritte liegen

Das Manifest wird erst nach der GRIB-Datei geschrieben. Ein Schritt gilt nur
als gespeichert, wenn sein Manifest existiert; ein abgebrochener Upload wird
beim nächsten Zyklus also neu geladen. Bis die Tabelle ``meta.grib_message``
existiert, sind die Manifeste die Zustandsführung.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

import obstore
from obstore.store import ObjectStore


@dataclass
class StepManifest:
    model: str
    resolution: str
    run: str
    step: int
    endpoint: str
    url: str
    sha256: str
    size: int
    n_messages: int
    #: angeforderte Spezifikationen (``VAR:LEVEL[:fcst]``)
    specs: list[str]
    #: Spezifikationen, die die Quelle für diesen Schritt nicht führt (z. B. Akkumulationen in f000)
    absent: list[str] = field(default_factory=list)
    #: ``.idx``-Schlüssel der übernommenen Nachrichten (nur bei ``idx_range``)
    messages: list[str] = field(default_factory=list)
    bbox: dict[str, float] = field(default_factory=dict)
    stored_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat(timespec="seconds"))


class Bronze:
    def __init__(self, store: ObjectStore, model: str = "gfs", resolution: str = "0p25") -> None:
        self.store = store
        self.model = model
        self.resolution = resolution

    def run_prefix(self, run: datetime) -> str:
        return f"{self.model}/{self.resolution}/{run:%Y/%m/%d/%H}"

    def step_key(self, run: datetime, step: int) -> str:
        return f"{self.run_prefix(run)}/f{step:03d}.grib2"

    def manifest_key(self, run: datetime, step: int) -> str:
        return f"{self.run_prefix(run)}/f{step:03d}.json"

    def run_key(self, run: datetime) -> str:
        return f"{self.run_prefix(run)}/run.json"

    async def _get_json(self, key: str) -> dict[str, Any] | None:
        try:
            result = await obstore.get_async(self.store, key)
        except FileNotFoundError:
            return None
        data: dict[str, Any] = json.loads(bytes(await result.bytes_async()))
        return data

    async def step_manifest(self, run: datetime, step: int) -> StepManifest | None:
        data = await self._get_json(self.manifest_key(run, step))
        return None if data is None else StepManifest(**data)

    async def run_manifest(self, run: datetime) -> dict[str, Any] | None:
        return await self._get_json(self.run_key(run))

    async def put_step(
        self, run: datetime, step: int, grib: bytes, manifest: StepManifest
    ) -> StepManifest:
        manifest.sha256 = hashlib.sha256(grib).hexdigest()
        manifest.size = len(grib)
        await obstore.put_async(self.store, self.step_key(run, step), grib)
        await obstore.put_async(
            self.store,
            self.manifest_key(run, step),
            json.dumps(asdict(manifest), indent=1).encode(),
        )
        return manifest

    async def put_run(self, run: datetime, summary: dict[str, Any]) -> None:
        await obstore.put_async(
            self.store, self.run_key(run), json.dumps(summary, indent=1).encode()
        )


class SilverMarker:
    """Prüft nur, ob für einen Lauf schon ein vollständiges Silber liegt (Bucket ``wx-silver``).

    Liest das Attribut ``complete`` aus ``zarr.json`` des Laufs, ohne von ``wx_process``
    abzuhängen (das Silber-Layout gehört dorthin, siehe ``wx_process.silver``).
    """

    def __init__(self, store: ObjectStore, config: Any) -> None:
        self.store = store
        self.model = config.model
        self.resolution = config.resolution

    def key(self, run: datetime) -> str:
        return f"{self.model}/{self.resolution}/{run:%Y/%m/%d/%H}.zarr/zarr.json"

    async def complete(self, run: datetime) -> bool:
        try:
            result = await obstore.get_async(self.store, self.key(run))
        except FileNotFoundError:
            return False
        meta = json.loads(bytes(await result.bytes_async()))
        return bool(meta.get("attributes", {}).get("complete"))
