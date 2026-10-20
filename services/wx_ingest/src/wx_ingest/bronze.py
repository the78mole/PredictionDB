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

import obstore
from obstore.store import ObjectStore, S3Store
from wx_ingest.config import StorageSettings


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


def make_store(settings: StorageSettings) -> ObjectStore:
    return S3Store(
        settings.bucket_bronze,
        endpoint=settings.endpoint_url,
        region=settings.region,
        access_key_id=settings.access_key_id,
        secret_access_key=settings.secret_access_key,
        virtual_hosted_style_request=False,
        client_options={"allow_http": settings.endpoint_url.startswith("http://")},
    )


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

    async def _get_json(self, key: str) -> dict | None:
        try:
            result = await obstore.get_async(self.store, key)
        except FileNotFoundError:
            return None
        return json.loads(bytes(await result.bytes_async()))

    async def step_manifest(self, run: datetime, step: int) -> StepManifest | None:
        data = await self._get_json(self.manifest_key(run, step))
        return None if data is None else StepManifest(**data)

    async def run_manifest(self, run: datetime) -> dict | None:
        return await self._get_json(self.run_key(run))

    async def put_step(self, run: datetime, step: int, grib: bytes, manifest: StepManifest) -> StepManifest:
        manifest.sha256 = hashlib.sha256(grib).hexdigest()
        manifest.size = len(grib)
        await obstore.put_async(self.store, self.step_key(run, step), grib)
        await obstore.put_async(
            self.store, self.manifest_key(run, step), json.dumps(asdict(manifest), indent=1).encode()
        )
        return manifest

    async def put_run(self, run: datetime, summary: dict) -> None:
        await obstore.put_async(self.store, self.run_key(run), json.dumps(summary, indent=1).encode())
