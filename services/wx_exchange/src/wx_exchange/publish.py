"""Herausgeber im Runner: GFS-Läufe aus dem AWS-Archiv laden, zu Silber verdichten, nach GHCR.

Bronze und Silber liegen nur im Arbeitsverzeichnis des Runners und werden nach jedem Lauf
gelöscht. Hochgeladen wird ein Lauf nur, wenn GHCR ihn noch nicht mit denselben Eingaben
(``fingerprint``) führt; mit ``rebuild`` wird neu gebaut und nur hochgeladen, wenn sich der
Inhalt (``content``) geändert hat. Das Manifest wird nach jedem Lauf geschrieben, ein
abgebrochener Job verliert also höchstens die Läufe, die gerade in Arbeit waren.
"""

from __future__ import annotations

import asyncio
import shutil
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

import httpx
from obstore.store import LocalStore

from predictiondb_core.log import get_logger
from wx_exchange.artifact import Layer, MonthArtifact, fingerprint, month_bounds, pack, tag
from wx_exchange.oci import Registry
from wx_ingest.bronze import Bronze
from wx_ingest.config import GfsConfig
from wx_ingest.ingest import GfsIngestor
from wx_process.config import ProcessConfig
from wx_process.pipeline import WxPipeline
from wx_process.silver import Silver

log = get_logger(__name__)

RunStatus = Literal["uploaded", "unchanged", "missing", "failed"]


@dataclass
class MonthResult:
    tag: str
    runs: dict[str, RunStatus] = field(default_factory=dict)
    skipped: int = 0
    bytes_uploaded: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "tag": self.tag,
            "skipped": self.skipped,
            "counts": dict(Counter(self.runs.values())),
            "failed": sorted(r for r, s in self.runs.items() if s == "failed"),
            "bytes_uploaded": self.bytes_uploaded,
        }


def months_between(start: datetime, end: datetime) -> list[str]:
    out = []
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        out.append(f"{y:04d}-{m:02d}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def plan(
    registry: Registry,
    config: GfsConfig,
    start: datetime,
    end: datetime,
    hours: list[int],
    *,
    block_years: int = 3,
    blocks: int = 3,
    max_jobs: int = 256,
    rebuild: bool = False,
) -> list[list[dict[str, Any]]]:
    """Matrix je Block: nur Monat-Laufzeit-Paare, in denen Läufe fehlen.

    Blöcke zu ``block_years`` Jahren vom jüngsten Monat rückwärts, im Block erst alle Monate der
    ersten Laufzeit in ``hours`` (jüngste zuerst), dann der nächsten. Mehr Blöcke als ``blocks``
    werden dem letzten zugeschlagen. Mit ``rebuild`` zählen nur endgültig fehlende Läufe als
    erledigt (Neubau aller vorhandenen).
    """
    fp = fingerprint(config)
    existing = set(registry.tags())
    months = months_between(start, end)[::-1]
    size = block_years * 12
    chunks = [months[i : i + size] for i in range(0, len(months), size)] or [[]]
    chunks = [*chunks[: blocks - 1], [m for c in chunks[blocks - 1 :] for m in c]]
    out: list[list[dict[str, Any]]] = []
    for chunk in chunks:
        entries = []
        for hour in hours:
            for month in chunk:
                a, b = month_bounds(month)
                runs = set(config.runs_between(max(a, start), min(b, end), [hour]))
                if not runs:
                    continue
                t = tag(month, hour)
                art = MonthArtifact.from_manifest(
                    month, hour, registry.manifest(t) if t in existing else None
                )
                todo = len(runs - (art.missing if rebuild else art.done(fp)))
                if todo:
                    entries.append({"month": month, "hour": hour, "todo": todo})
        if len(entries) > max_jobs:
            raise ValueError(f"{len(entries)} Jobs in einem Block, höchstens {max_jobs}")
        out.append(entries)
    out += [[] for _ in range(blocks - len(out))]
    return out


class Publisher:
    def __init__(
        self,
        registry: Registry,
        config: GfsConfig,
        process: ProcessConfig,
        client: httpx.AsyncClient,
        workdir: Path,
        *,
        source_url: str,
        parallel_runs: int = 2,
    ) -> None:
        self.registry = registry
        self.config = config
        self.process = process
        self.workdir = workdir
        self.source_url = source_url
        self.parallel_runs = max(1, parallel_runs)
        self.fp = fingerprint(config)
        (workdir / "bronze").mkdir(parents=True, exist_ok=True)
        (workdir / "silver").mkdir(parents=True, exist_ok=True)
        self.bronze_store = LocalStore(workdir / "bronze")
        self.silver_store = LocalStore(workdir / "silver")
        self.bronze = Bronze(self.bronze_store, config.model, config.resolution)
        self.silver = Silver(
            self.silver_store,
            step_chunk=process.silver.step_chunk,
            zstd_level=process.silver.zstd_level,
        )
        self.ingestor = GfsIngestor(config, self.bronze, client, archive_only=True)
        self.pipeline = WxPipeline(process, config, self.bronze_store, self.silver, None)

    def _cleanup(self, run: datetime) -> None:
        shutil.rmtree(self.workdir / "bronze" / self.bronze.run_prefix(run), ignore_errors=True)
        key = self.silver.key(self.config.model, self.config.resolution, run)
        shutil.rmtree(self.workdir / "silver" / key, ignore_errors=True)

    async def _build(self, run: datetime) -> tuple[RunStatus, bytes, str, int]:
        """Lädt Bronze und baut Silber; liefert Status, Tar, Inhalts-Hash und Schrittzahl."""
        result = await self.ingestor.ingest_run(run, stop_on_pending=False)
        if not result.complete:
            failed = any(s.status == "failed" for s in result.steps)
            return ("failed" if failed else "missing"), b"", "", 0
        outcome = await asyncio.to_thread(self.pipeline.process, run, force=True)
        if outcome.status == "failed":
            log.error("silver_failed", run=f"{run:%Y%m%d%H}", error=outcome.error)
            return "failed", b"", "", 0
        key = self.silver.key(self.config.model, self.config.resolution, run)
        data, content = await asyncio.to_thread(pack, self.silver_store, key)
        steps = sum(1 for s in result.steps if s.status in ("stored", "skipped"))
        return "uploaded", data, content, steps

    async def publish_month(
        self,
        month: str,
        hour: int,
        start: datetime | None = None,
        end: datetime | None = None,
        *,
        rebuild: bool = False,
    ) -> MonthResult:
        a, b = month_bounds(month)
        a, b = max(a, start or a), min(b, end or b)
        t = tag(month, hour)
        art = MonthArtifact.from_manifest(
            month, hour, await asyncio.to_thread(self.registry.manifest, t)
        )
        art.model, art.resolution = self.config.model, self.config.resolution
        runs = self.config.runs_between(a, b, [hour])
        done = art.missing if rebuild else art.done(self.fp)
        todo = [r for r in runs if r not in done]
        result = MonthResult(t, skipped=len(runs) - len(todo))
        log.info("month_start", tag=t, runs=len(runs), todo=len(todo))
        sem = asyncio.Semaphore(self.parallel_runs)
        lock = asyncio.Lock()

        async def one(run: datetime) -> None:
            async with sem:
                try:
                    status, data, content, steps = await self._build(run)
                except Exception as exc:  # ein Lauf darf den Monat nicht abbrechen
                    log.exception("run_failed", run=f"{run:%Y%m%d%H}", error=str(exc))
                    status, data, content, steps = "failed", b"", "", 0
                finally:
                    self._cleanup(run)
            async with lock:
                old = art.layers.get(run)
                if status == "uploaded" and old is not None and old.content == content:
                    status = "unchanged"
                    if old.fingerprint == self.fp:
                        result.runs[f"{run:%Y%m%d%H}"] = status
                        return
                    old.fingerprint = self.fp
                elif status == "uploaded":
                    digest = await asyncio.to_thread(self.registry.push_blob, data)
                    result.bytes_uploaded += len(data)
                    art.layers[run] = Layer(run, digest, len(data), content, self.fp, steps=steps)
                    art.missing.discard(run)
                elif status == "missing":
                    if run in art.missing:
                        result.runs[f"{run:%Y%m%d%H}"] = status
                        return
                    art.missing.add(run)
                else:
                    result.runs[f"{run:%Y%m%d%H}"] = status
                    return
                await asyncio.to_thread(
                    self.registry.put_manifest, t, art.manifest(self.source_url)
                )
                result.runs[f"{run:%Y%m%d%H}"] = status
                log.info("run_published", run=f"{run:%Y%m%d%H}", status=status, tag=t)

        await asyncio.gather(*(one(r) for r in todo))
        return result
