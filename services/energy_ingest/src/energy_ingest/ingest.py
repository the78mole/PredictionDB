"""Abgleich (laufend) und Backfill (Historie) über alle aktiven Quellen.

Jede Quelle schreibt in eigene Reihen (``energy.series`` je Quelle und Code); so bleibt
die Redundanz erhalten und ``energy.value_best`` wählt je Zeitpunkt die bevorzugte Quelle.
Fehler einer Quelle oder eines Datensatzes werden protokolliert und halten die übrigen
nicht auf. Der Ausgang jedes Abrufs landet in ``meta.energy_status``; Grafana alarmiert bei
Formatabweichungen und anhaltenden Fehlern.

@relation(SWA-ENI-INGESTOR, scope=file)
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from itertools import zip_longest
from typing import Any, Literal, Protocol

from energy_ingest.config import EnergySettings
from energy_ingest.model import SeriesData
from energy_ingest.sources import FormatError, Source, SourceMeta
from energy_ingest.store import Deviation
from predictiondb_core.log import get_logger

log = get_logger(__name__)

# Fester Ursprung des Blockrasters, damit Backfill-Blöcke immer gleich geschnitten sind
ANCHOR = datetime(2015, 1, 1, tzinfo=UTC)

# format: Aufbau der Antwort weicht ab (Spalten, Einheiten), es wurde nichts geschrieben
#: Mindestabstand, bevor ein angebrochener Block fortgesetzt wird
MIN_RESUME = timedelta(days=1)
#: Datensätze, die der Abgleich vor allen anderen abruft
SYNC_FIRST = frozenset({"da_price_de_lu"})

Status = Literal["ok", "empty", "failed", "format"]


class Store(Protocol):
    def source_id(self, source: SourceMeta) -> int: ...
    def write(
        self, source_id: int, batch: list[SeriesData], fetched: datetime, *, prune: bool = False
    ) -> int: ...
    def done_chunks(self, source_id: int, dataset: str) -> dict[datetime, datetime]: ...
    def mark_chunk(
        self,
        source_id: int,
        dataset: str,
        start: datetime,
        end: datetime,
        n_values: int,
        *,
        resume: bool = False,
    ) -> None: ...
    def compare(self, start: datetime, end: datetime) -> list[Deviation]: ...
    def revised_at(self, source_id: int, dataset: str) -> datetime | None: ...
    def mark_revised(self, source_id: int, dataset: str, at: datetime) -> None: ...
    def record_status(
        self, source_id: int, dataset: str, status: str, n_values: int, error: str | None
    ) -> None: ...


@dataclass(frozen=True)
class Todo:
    source: Source
    dataset: str
    start: datetime  # abzurufender Bereich
    end: datetime
    chunk_start: datetime  # Rasterblock
    chunk_end: datetime
    resume: bool = False  # setzt einen angebrochenen Block fort


@dataclass
class DatasetResult:
    source: str
    dataset: str
    start: datetime
    end: datetime
    status: Status
    n_values: int = 0
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["start"] = self.start.isoformat()
        d["end"] = self.end.isoformat()
        return d


def chunks(size: timedelta, start: datetime, end: datetime) -> list[tuple[datetime, datetime]]:
    """Blöcke des festen Rasters ab ``ANCHOR``, die ``[start, end)`` überdecken.

    @relation(SWR-ENI-013, scope=function)
    """
    k = (start - ANCHOR) // size
    out = []
    t = ANCHOR + k * size
    while t < end:
        out.append((t, t + size))
        t += size
    return out


def day_window(now: datetime, back: int, ahead: int) -> tuple[datetime, datetime]:
    """@relation(SWR-ENI-010, scope=function)"""
    day = now.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    return day - timedelta(days=back), day + timedelta(days=ahead)


def sync_lookback(source: Source, dataset: str) -> timedelta:
    """Zusätzlicher Rückblick im Abgleich für Datensätze, die erst lange nach der Periode
    erscheinen (z. B. Monatsmarktwerte); Quellen geben ihn optional in ``sync_lookback`` an.

    @relation(SWR-ENI-011, scope=function)
    """
    extra: dict[str, timedelta] = getattr(source, "sync_lookback", {})
    return extra.get(dataset, timedelta(0))


class EnergyIngestor:
    def __init__(self, sources: list[Source], store: Store, settings: EnergySettings) -> None:
        self.sources = sources
        self.store = store
        self.settings = settings

    def fetch(
        self,
        source: Source,
        dataset: str,
        start: datetime,
        end: datetime,
        now: datetime,
        *,
        prune: bool = False,
    ) -> DatasetResult:
        """@relation(SWR-ENI-010, SWR-ENI-013, scope=function)"""
        try:
            batch = source.fetch(dataset, start, end)
            n = self.store.write(self.store.source_id(source), batch, now, prune=prune)
        except Exception as exc:
            status: Status = "format" if isinstance(exc, FormatError) else "failed"
            log.warning(
                "energy.format_changed" if status == "format" else "energy.fetch_failed",
                source=source.code,
                dataset=dataset,
                start=start.isoformat(),
                end=end.isoformat(),
                error=f"{type(exc).__name__}: {exc}",
            )
            res = DatasetResult(source.code, dataset, start, end, status, error=str(exc))
            self._record(source, res)
            return res
        log.info(
            "energy.fetched",
            source=source.code,
            dataset=dataset,
            start=start.isoformat(),
            end=end.isoformat(),
            series=len(batch),
            values=n,
        )
        res = DatasetResult(source.code, dataset, start, end, "ok" if n else "empty", n)
        self._record(source, res)
        return res

    def _record(self, source: Source, res: DatasetResult) -> None:
        """@relation(SWR-ENI-030, scope=function)"""
        try:
            self.store.record_status(
                self.store.source_id(source), res.dataset, res.status, res.n_values, res.error
            )
        except Exception as exc:  # Status ist Beiwerk, der Abruf zählt
            log.warning("energy.status_failed", source=source.code, error=str(exc))

    def sync(self, now: datetime | None = None) -> list[DatasetResult]:
        """Laufender Abgleich des Fensters um heute (Ist-Werte, Preise, Folgetag-Prognosen).

        @relation(SWR-ENI-005, SWR-ENI-010, scope=function)
        """
        now = now or datetime.now(UTC)
        start, end = day_window(now, self.settings.lookback_days, self.settings.horizon_days)
        # Day-Ahead-Preise aller Quellen zuerst: Ein Zyklus dauert auf der VM über eine halbe
        # Stunde, der Preis des Folgetags soll aber kurz nach der Auktion da sein.
        jobs = sorted(
            ((src, ds) for src in self.sources for ds in src.datasets()),
            key=lambda job: job[1] not in SYNC_FIRST,
        )
        return [self.fetch(src, ds, start - sync_lookback(src, ds), end, now) for src, ds in jobs]

    def revise(self, now: datetime | None = None, *, due_only: bool = False) -> list[DatasetResult]:
        """Fragt die letzten ``revision_days`` Tage erneut ab, damit nachträgliche Korrekturen
        der Quellen (z. B. Ausreißer in der Ist-Last) übernommen werden. Von der Quelle
        entfernte Werte werden geleert. Datensätze, deren Abgleich ohnehin so weit zurückreicht,
        bleiben außen vor; das Fenster des Abgleichs selbst ebenfalls.

        ``due_only`` überspringt Datensätze, die innerhalb von ``revision_interval_hours``
        schon vollständig erneut abgefragt wurden (``meta.energy_revise``); so setzt ein
        Neustart dort fort, wo der vorige Lauf aufgehört hat.

        @relation(SWR-ENI-012, scope=function)
        """
        now = now or datetime.now(UTC)
        interval = timedelta(hours=self.settings.revision_interval_hours)
        sync_start, _ = day_window(now, self.settings.lookback_days, 0)
        start, _ = day_window(now, self.settings.revision_days, 0)
        results = []
        for src in self.sources:
            sid = self.store.source_id(src)
            for ds in src.datasets():
                end = sync_start - sync_lookback(src, ds)
                if end <= start:
                    continue
                last = self.store.revised_at(sid, ds) if due_only else None
                if last is not None and now - last < interval:
                    continue
                res = [
                    self.fetch(src, ds, max(a, start), min(b, end), now, prune=True)
                    for a, b in chunks(src.max_chunk(ds), start, end)
                ]
                results.extend(res)
                if all(r.status in ("ok", "empty") for r in res):
                    self.store.mark_revised(sid, ds, now)
        return results

    def pending(
        self,
        start: datetime,
        end: datetime,
        *,
        now: datetime,
        datasets: list[str] | None = None,
        force: bool = False,
        final_only: bool = False,
    ) -> list[Todo]:
        """Noch nicht vollständig geladene Blöcke, neueste zuerst, reihum je Quelle.

        Ein Block, der noch über die Grenze ``final_after_days`` reicht, wird bis zu dieser
        Grenze geladen und später ab dort fortgesetzt. Sonst bliebe zwischen dem Fenster des
        laufenden Abgleichs und dem jüngsten vollständigen Block eine Lücke von bis zu einer
        Blocklänge (bei SMARD 91 Tage)."""
        final = now - timedelta(days=self.settings.final_after_days)
        per_source = []
        for src in self.sources:
            items = []
            sid = self.store.source_id(src)
            for ds in src.datasets():
                if datasets and ds not in datasets:
                    continue
                done = {} if force else self.store.done_chunks(sid, ds)
                for a, b in chunks(src.max_chunk(ds), start, end):
                    loaded = done.get(a)
                    if loaded is not None and loaded >= b:
                        continue
                    lo = max(loaded or a, start)
                    hi = min(b, end, final) if final_only else min(b, end)
                    # angebrochene Blöcke höchstens einmal am Tag fortsetzen
                    if hi <= lo or (loaded is not None and hi < b and hi - lo < MIN_RESUME):
                        continue
                    items.append(Todo(src, ds, lo, hi, a, b, resume=lo == loaded))
            items.sort(key=lambda t: t.chunk_start, reverse=True)
            per_source.append(items)
        return [t for group in zip_longest(*per_source) for t in group if t is not None]

    def backfill(
        self,
        start: datetime,
        end: datetime,
        *,
        now: datetime | None = None,
        datasets: list[str] | None = None,
        force: bool = False,
        limit: int | None = None,
        final_only: bool = False,
        deadline: float | None = None,
    ) -> list[DatasetResult]:
        """Lädt fehlende Blöcke in ``[start, end)``. Mit ``deadline`` (``time.monotonic()``)
        endet der Lauf nach dem Block, in dem die Frist abläuft; mindestens ein Block wird
        immer geladen, damit die Historie auch bei langsamen Quellen vorankommt.

        @relation(SWR-ENI-013, scope=function)
        """
        now = now or datetime.now(UTC)
        final = now - timedelta(days=self.settings.final_after_days)
        todo = self.pending(
            start, end, now=now, datasets=datasets, force=force, final_only=final_only
        )
        results: list[DatasetResult] = []
        for t in todo[:limit]:
            if results and deadline is not None and time.monotonic() >= deadline:
                log.info(
                    "energy.backfill_deadline", done=len(results), left=len(todo) - len(results)
                )
                break
            res = self.fetch(t.source, t.dataset, t.start, t.end, now)
            results.append(res)
            # Als geladen gilt nur, was lückenlos ab Blockbeginn (oder ab dem bisher geladenen
            # Stand) reicht und älter als die Grenze ``final_after_days`` ist
            loaded = min(t.end, final)
            contiguous = t.start == t.chunk_start or t.resume
            if res.status in ("ok", "empty") and contiguous and loaded > t.start:
                self.store.mark_chunk(
                    self.store.source_id(t.source),
                    t.dataset,
                    t.chunk_start,
                    loaded,
                    res.n_values,
                    resume=t.resume,
                )
        return results

    def backfill_step(
        self, now: datetime | None = None, deadline: float | None = None
    ) -> list[DatasetResult]:
        """Ein Häppchen Historie im Dauerbetrieb (``backfill_chunks_per_cycle`` Blöcke).

        ``deadline`` ist das Ende des Abgleich-Intervalls: Der Backfill darf den nächsten
        Abgleich nicht verzögern, sonst kommen z. B. die Day-Ahead-Preise erst eine Stunde
        nach der Auktion an (2026-10-26: 48 Blöcke à ~35 s hielten den Zyklus über 60 min).

        @relation(SWR-ENI-014, scope=function)
        """
        now = now or datetime.now(UTC)
        return self.backfill(
            self.settings.backfill_from,
            now,
            now=now,
            limit=self.settings.backfill_chunks_per_cycle,
            final_only=True,
            deadline=deadline,
        )

    def check(self, now: datetime | None = None, days: int = 7) -> list[Deviation]:
        """Plausibilitätsabgleich zwischen den Quellen (Kapitel 3.3).

        @relation(SWR-ENI-015, scope=function)
        """
        now = now or datetime.now(UTC)
        end = now.astimezone(UTC).replace(minute=0, second=0, microsecond=0)
        devs = self.store.compare(end - timedelta(days=days), end)
        for d in devs:
            if d.rel is not None and d.rel > self.settings.check_rel_threshold:
                log.warning("energy.sources_disagree", **asdict(d))
        return devs
