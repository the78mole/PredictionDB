"""Ingest-Zyklus für GFS: fehlende Schritte finden, laden, nach Bronze schreiben.

Ablauf nach ``docs/ARCHITEKTUR.md`` 6.4. Idempotent: Schritte mit passendem
Manifest werden übersprungen, ein Neustart lädt nur Fehlendes nach.
"""

from __future__ import annotations

import asyncio
from collections import Counter
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

import httpx

from gribkit import IdxParseError
from predictiondb_core.log import get_logger
from wx_ingest.bronze import Bronze, StepManifest
from wx_ingest.config import GfsConfig
from wx_ingest.gfs import Endpoint, FetchError, StepUnavailable

log = get_logger(__name__)

StepStatus = Literal["stored", "skipped", "pending", "failed"]


@dataclass
class StepResult:
    step: int
    status: StepStatus
    endpoint: str | None = None
    size: int = 0
    errors: list[str] = field(default_factory=list)


@dataclass
class RunResult:
    run: datetime
    steps: list[StepResult]
    complete: bool = False

    def counts(self) -> dict[str, int]:
        return dict(Counter(s.status for s in self.steps))


class GfsIngestor:
    def __init__(
        self,
        config: GfsConfig,
        bronze: Bronze,
        client: httpx.AsyncClient,
        *,
        archive_only: bool = False,
        retry_attempts: int = 4,
        retry_wait_max: float = 30.0,
    ) -> None:
        self.config = config
        self.bronze = bronze
        self.bbox = config.bbox.bbox()
        self.specs = config.specs()
        self.labels = sorted(s.label for s in self.specs)
        self.endpoints = [
            Endpoint(e, client, retry_attempts=retry_attempts, retry_wait_max=retry_wait_max)
            for e in config.endpoints_for(archive_only)
        ]
        if not self.endpoints:
            raise ValueError("keine Endpunkte für diesen Modus konfiguriert")

    def _bbox_dict(self) -> dict[str, float]:
        return self.config.bbox.model_dump()

    def _manifest_current(self, m: StepManifest | None) -> bool:
        return m is not None and sorted(m.specs) == self.labels and m.bbox == self._bbox_dict()

    async def ingest_step(self, run: datetime, step: int) -> StepResult:
        if self._manifest_current(await self.bronze.step_manifest(run, step)):
            return StepResult(step, "skipped")

        errors: list[str] = []
        for ep in self.endpoints:
            try:
                data = await ep.fetch_step(self.config.product, run, step, self.specs, self.bbox)
            except StepUnavailable:
                continue
            except (FetchError, IdxParseError, httpx.HTTPError) as exc:
                log.warning(
                    "step_failed",
                    run=f"{run:%Y%m%d%H}",
                    step=step,
                    endpoint=ep.name,
                    error=str(exc),
                )
                errors.append(f"{ep.name}: {exc}")
                continue
            manifest = StepManifest(
                model=self.config.model,
                resolution=self.config.resolution,
                run=run.isoformat(),
                step=step,
                endpoint=ep.name,
                url=data.url,
                sha256="",
                size=0,
                n_messages=data.n_messages,
                specs=self.labels,
                absent=data.absent,
                messages=data.messages,
                bbox=self._bbox_dict(),
            )
            manifest = await self.bronze.put_step(run, step, data.grib, manifest)
            log.info(
                "step_stored",
                run=f"{run:%Y%m%d%H}",
                step=step,
                endpoint=ep.name,
                bytes=manifest.size,
            )
            return StepResult(step, "stored", ep.name, manifest.size)
        return StepResult(step, "failed" if errors else "pending", errors=errors)

    async def ingest_run(
        self, run: datetime, steps: list[int] | None = None, *, stop_on_pending: bool = True
    ) -> RunResult:
        """Lädt alle (bzw. die angegebenen) Schritte eines Laufs.

        GFS veröffentlicht die Schritte nacheinander. Mit ``stop_on_pending``
        werden nach dem ersten noch nicht veröffentlichten Schritt keine weiteren
        angefragt; sie bleiben ``pending`` und kommen im nächsten Zyklus dran.
        """
        run = run.astimezone(UTC)
        all_steps = self.config.step_list()
        wanted = sorted(steps) if steps is not None else all_steps
        full_run = steps is None or set(wanted) >= set(all_steps)

        if full_run:
            summary = await self.bronze.run_manifest(run)
            if (
                summary
                and sorted(summary.get("specs", [])) == self.labels
                and summary.get("bbox") == self._bbox_dict()
            ):
                return RunResult(run, [StepResult(s, "skipped") for s in wanted], complete=True)

        sem = asyncio.Semaphore(self.config.step_concurrency)
        stop = asyncio.Event()
        results: dict[int, StepResult] = {}

        async def one(step: int) -> None:
            try:
                res = await self.ingest_step(run, step)
                results[step] = res
                if res.status == "pending" and stop_on_pending:
                    stop.set()
            finally:
                sem.release()

        tasks = []
        for step in wanted:
            await sem.acquire()
            if stop.is_set():
                sem.release()
                break
            tasks.append(asyncio.create_task(one(step)))
        await asyncio.gather(*tasks)

        ordered = [results.get(s, StepResult(s, "pending")) for s in wanted]
        result = RunResult(run, ordered)
        result.complete = full_run and all(r.status in ("stored", "skipped") for r in ordered)
        if result.complete:
            await self._finish_run(run, ordered)
        log.info("run_checked", run=f"{run:%Y%m%d%H}", complete=result.complete, **result.counts())
        return result

    async def _finish_run(self, run: datetime, steps: list[StepResult]) -> None:
        manifests = await asyncio.gather(*(self.bronze.step_manifest(run, s.step) for s in steps))
        await self.bronze.put_run(
            run,
            {
                "model": self.config.model,
                "resolution": self.config.resolution,
                "run": run.isoformat(),
                "steps": len(steps),
                "bytes": sum(m.size for m in manifests if m),
                "endpoints": dict(Counter(m.endpoint for m in manifests if m)),
                "specs": self.labels,
                "bbox": self._bbox_dict(),
                "completed_at": datetime.now(UTC).isoformat(timespec="seconds"),
            },
        )

    async def ingest_recent(self, now: datetime | None = None) -> list[RunResult]:
        """Live-Modus: alle konfigurierten Läufe im Rückblickfenster, älteste zuerst."""
        now = now or datetime.now(UTC)
        return [await self.ingest_run(run) for run in self.config.recent_runs(now)]

    async def backfill(
        self,
        start: datetime,
        end: datetime,
        *,
        hours: Iterable[int] | None = None,
        newest_first: bool = False,
        parallel_runs: int = 1,
    ) -> list[RunResult]:
        """Archiv-Modus: alle (bzw. die in ``hours`` gewählten) Läufe im Zeitraum.

        Vollständige Läufe kosten nur den Abruf ihres ``run.json``, ein Abbruch kann also
        jederzeit mit demselben Aufruf fortgesetzt werden. ``parallel_runs`` lädt mehrere
        Läufe gleichzeitig; die Grenzen je Endpunkt gelten weiterhin für alle zusammen.
        """
        runs = self.config.runs_between(start, end, hours)
        if newest_first:
            runs.reverse()
        sem = asyncio.Semaphore(max(1, parallel_runs))

        async def one(run: datetime) -> RunResult:
            async with sem:
                return await self.ingest_run(run, stop_on_pending=False)

        return list(await asyncio.gather(*(one(r) for r in runs)))


def result_dict(r: RunResult) -> dict[str, Any]:
    return {
        "run": r.run.isoformat(),
        "complete": r.complete,
        "counts": r.counts(),
        "failed": [asdict(s) for s in r.steps if s.status == "failed"],
    }
