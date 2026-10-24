"""Abgleich (laufend) und Backfill (Historie) über alle aktiven Quellen.

Jede Quelle schreibt in eigene Reihen (``energy.series`` je Quelle und Code); so bleibt
die Redundanz erhalten und ``energy.value_best`` wählt je Zeitpunkt die bevorzugte Quelle.
Fehler einer Quelle oder eines Datensatzes werden protokolliert und halten die übrigen
nicht auf. Der Ausgang jedes Abrufs landet in ``meta.energy_status``; Grafana alarmiert bei
Formatabweichungen und anhaltenden Fehlern.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from itertools import zip_longest
from typing import Any, Literal, Protocol

from energy_ingest.config import EnergySettings
from energy_ingest.model import SeriesData
from energy_ingest.sources import FormatError, Source, SourceMeta
from energy_ingest.store import EMPTY_RETRY, Chunk, Deviation
from predictiondb_core.log import get_logger

log = get_logger(__name__)

# Fester Ursprung des Blockrasters, damit Backfill-Blöcke immer gleich geschnitten sind
ANCHOR = datetime(2015, 1, 1, tzinfo=UTC)

# Angebrochener Block an der Grenze ``final_after_days``: Den schon endgültigen Teil holt der
# Backfill höchstens so oft. Sonst bliebe zwischen dem letzten vollen Block und dem Fenster
# des laufenden Abgleichs eine Lücke von bis zu einer Blocklänge.
TAIL_REFRESH = timedelta(days=1)

# format: Aufbau der Antwort weicht ab (Spalten, Einheiten), es wurde nichts geschrieben
Status = Literal["ok", "empty", "failed", "format"]


class Store(Protocol):
    def source_id(self, source: SourceMeta) -> int: ...
    def write(self, source_id: int, batch: list[SeriesData], fetched: datetime) -> int: ...
    def done_chunks(
        self, source_id: int, dataset: str, empty_retry: timedelta = ...
    ) -> dict[datetime, Chunk]: ...
    def mark_chunk(
        self, source_id: int, dataset: str, start: datetime, end: datetime, n_values: int
    ) -> None: ...
    def compare(self, start: datetime, end: datetime) -> list[Deviation]: ...
    def record_status(
        self, source_id: int, dataset: str, status: str, n_values: int, error: str | None
    ) -> None: ...


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
    """Blöcke des festen Rasters ab ``ANCHOR``, die ``[start, end)`` überdecken."""
    k = (start - ANCHOR) // size
    out = []
    t = ANCHOR + k * size
    while t < end:
        out.append((t, t + size))
        t += size
    return out


def day_window(now: datetime, back: int, ahead: int) -> tuple[datetime, datetime]:
    day = now.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    return day - timedelta(days=back), day + timedelta(days=ahead)


def sync_lookback(source: Source, dataset: str) -> timedelta:
    """Zusätzlicher Rückblick im Abgleich für Datensätze, die erst lange nach der Periode
    erscheinen (z. B. Monatsmarktwerte); Quellen geben ihn optional in ``sync_lookback`` an."""
    extra: dict[str, timedelta] = getattr(source, "sync_lookback", {})
    return extra.get(dataset, timedelta(0))


def empty_retry(source: Source) -> timedelta:
    """Wartezeit, bis ein leer geladener Block erneut versucht wird; Quellen, die aus eigenen
    Daten rechnen, setzen sie über ``empty_retry`` kürzer."""
    retry: timedelta = getattr(source, "empty_retry", EMPTY_RETRY)
    return retry


class EnergyIngestor:
    def __init__(self, sources: list[Source], store: Store, settings: EnergySettings) -> None:
        self.sources = sources
        self.store = store
        self.settings = settings

    def fetch(
        self, source: Source, dataset: str, start: datetime, end: datetime, now: datetime
    ) -> DatasetResult:
        try:
            batch = source.fetch(dataset, start, end)
            n = self.store.write(self.store.source_id(source), batch, now)
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
        try:
            self.store.record_status(
                self.store.source_id(source), res.dataset, res.status, res.n_values, res.error
            )
        except Exception as exc:  # Status ist Beiwerk, der Abruf zählt
            log.warning("energy.status_failed", source=source.code, error=str(exc))

    def sync(self, now: datetime | None = None) -> list[DatasetResult]:
        """Laufender Abgleich des Fensters um heute (Ist-Werte, Preise, Folgetag-Prognosen)."""
        now = now or datetime.now(UTC)
        start, end = day_window(now, self.settings.lookback_days, self.settings.horizon_days)
        return [
            self.fetch(src, ds, start - sync_lookback(src, ds), end, now)
            for src in self.sources
            for ds in src.datasets()
        ]

    def pending(
        self,
        start: datetime,
        end: datetime,
        *,
        now: datetime,
        datasets: list[str] | None = None,
        force: bool = False,
        final_only: bool = False,
    ) -> list[tuple[Source, str, datetime, datetime]]:
        """Noch nicht vollständig geladene Blöcke, neueste zuerst, reihum je Quelle.

        Mit ``final_only`` nur endgültige Zeiträume: volle Blöcke vor der Grenze und vom
        angebrochenen Block an der Grenze der Teil davor (höchstens alle ``TAIL_REFRESH``)."""
        final = now - timedelta(days=self.settings.final_after_days)
        per_source = []
        for src in self.sources:
            items = []
            sid = self.store.source_id(src)
            for ds in src.datasets():
                if datasets and ds not in datasets:
                    continue
                done = {} if force else self.store.done_chunks(sid, ds, empty_retry(src))
                for a, b in chunks(src.max_chunk(ds), start, end):
                    rec = done.get(a)
                    if rec and rec.end >= b:
                        continue
                    upto = b
                    if final_only and b > final:
                        if a >= final or (rec and rec.fetched_at > now - TAIL_REFRESH):
                            continue
                        upto = final
                    items.append((src, ds, max(a, start), min(upto, end), a, b))
            items.sort(key=lambda i: i[4], reverse=True)
            per_source.append(items)
        return [
            (i[0], i[1], i[2], i[3])
            for group in zip_longest(*per_source)
            for i in group
            if i is not None
        ]

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
    ) -> list[DatasetResult]:
        now = now or datetime.now(UTC)
        final = now - timedelta(days=self.settings.final_after_days)
        todo = self.pending(
            start, end, now=now, datasets=datasets, force=force, final_only=final_only
        )
        results = []
        for src, ds, a, b in todo[:limit]:
            res = self.fetch(src, ds, a, b, now)
            results.append(res)
            # Erledigt ist, was ab einem Rasterbeginn endgültig geladen ist; endet es vor dem
            # Rasterende, steht das in chunk_end und der Rest folgt später
            full = chunks(src.max_chunk(ds), a, b)
            aligned = len(full) == 1 and full[0][0] == a and b <= final
            if res.status in ("ok", "empty") and aligned:
                self.store.mark_chunk(self.store.source_id(src), ds, a, b, res.n_values)
        return results

    def backfill_step(self, now: datetime | None = None) -> list[DatasetResult]:
        """Ein Häppchen Historie im Dauerbetrieb (``backfill_chunks_per_cycle`` Blöcke)."""
        now = now or datetime.now(UTC)
        return self.backfill(
            self.settings.backfill_from,
            now,
            now=now,
            limit=self.settings.backfill_chunks_per_cycle,
            final_only=True,
        )

    def check(self, now: datetime | None = None, days: int = 7) -> list[Deviation]:
        """Plausibilitätsabgleich zwischen den Quellen (Kapitel 3.3)."""
        now = now or datetime.now(UTC)
        end = now.astimezone(UTC).replace(minute=0, second=0, microsecond=0)
        devs = self.store.compare(end - timedelta(days=days), end)
        for d in devs:
            if d.rel is not None and d.rel > self.settings.check_rel_threshold:
                log.warning("energy.sources_disagree", **asdict(d))
        return devs
