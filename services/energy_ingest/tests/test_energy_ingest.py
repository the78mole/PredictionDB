from datetime import UTC, datetime, timedelta

from energy_ingest.config import EnergySettings
from energy_ingest.ingest import ANCHOR, TAIL_REFRESH, EnergyIngestor, chunks, day_window
from energy_ingest.model import QUARTER_HOUR, SeriesData, series
from energy_ingest.sources import FormatError, SourceMeta
from energy_ingest.store import EMPTY_RETRY, Chunk, Deviation

NOW = datetime(2026, 10, 20, 23, 0, tzinfo=UTC)


class FakeSource:
    license = "test"
    free = True

    def __init__(
        self, code: str, priority: int = 50, fail: bool = False, bad_format: bool = False
    ) -> None:
        self.code = code
        self.priority = priority
        self.fail = fail
        self.bad_format = bad_format
        self.calls: list[tuple[str, datetime, datetime]] = []

    def datasets(self) -> list[str]:
        return ["a", "b"]

    def max_chunk(self, dataset: str) -> timedelta:
        return timedelta(days=10)

    def fetch(self, dataset: str, start: datetime, end: datetime) -> list[SeriesData]:
        self.calls.append((dataset, start, end))
        if self.fail:
            raise RuntimeError("kaputt")
        if self.bad_format:
            raise FormatError("Spalte fehlt")
        return [SeriesData(series("load_actual"), QUARTER_HOUR, [(start, 1.0)])]


class FakeStore:
    def __init__(self) -> None:
        self.ids: dict[str, int] = {}
        self.written: list[tuple[int, int]] = []
        self.done: dict[tuple[int, str], dict[datetime, Chunk]] = {}
        self.retry: dict[tuple[int, str], timedelta] = {}
        self.clock = NOW
        self.deviations: list[Deviation] = []
        self.status: dict[tuple[int, str], tuple[str, int, str | None]] = {}

    def source_id(self, source: SourceMeta) -> int:
        return self.ids.setdefault(source.code, len(self.ids) + 1)

    def write(self, source_id: int, batch: list[SeriesData], fetched: datetime) -> int:
        n = sum(len(d.points) for d in batch)
        self.written.append((source_id, n))
        return n

    def done_chunks(
        self, source_id: int, dataset: str, empty_retry: timedelta = EMPTY_RETRY
    ) -> dict[datetime, Chunk]:
        self.retry[(source_id, dataset)] = empty_retry
        return dict(self.done.get((source_id, dataset), {}))

    def mark_chunk(
        self, source_id: int, dataset: str, start: datetime, end: datetime, n_values: int
    ) -> None:
        self.done.setdefault((source_id, dataset), {})[start] = Chunk(end, self.clock)

    def compare(self, start: datetime, end: datetime) -> list[Deviation]:
        return self.deviations

    def record_status(
        self, source_id: int, dataset: str, status: str, n_values: int, error: str | None
    ) -> None:
        self.status[(source_id, dataset)] = (status, n_values, error)


def test_chunks_follow_fixed_grid() -> None:
    size = timedelta(days=10)
    out = chunks(size, ANCHOR + timedelta(days=15), ANCHOR + timedelta(days=31))
    assert out == [
        (ANCHOR + timedelta(days=10), ANCHOR + timedelta(days=20)),
        (ANCHOR + timedelta(days=20), ANCHOR + timedelta(days=30)),
        (ANCHOR + timedelta(days=30), ANCHOR + timedelta(days=40)),
    ]


def test_day_window() -> None:
    assert day_window(NOW, 3, 2) == (
        datetime(2026, 10, 17, tzinfo=UTC),
        datetime(2026, 10, 22, tzinfo=UTC),
    )


def test_sync_isolates_failures() -> None:
    good, bad = FakeSource("good"), FakeSource("bad", fail=True)
    ing = EnergyIngestor([bad, good], FakeStore(), EnergySettings())
    results = ing.sync(NOW)
    assert [(r.source, r.dataset, r.status) for r in results] == [
        ("bad", "a", "failed"),
        ("bad", "b", "failed"),
        ("good", "a", "ok"),
        ("good", "b", "ok"),
    ]
    assert good.calls[0][1:] == day_window(NOW, 3, 2)


def test_sync_lookback_per_dataset() -> None:
    src = FakeSource("monthly")
    src.sync_lookback = {"b": timedelta(days=95)}  # type: ignore[attr-defined]
    EnergyIngestor([src], FakeStore(), EnergySettings()).sync(NOW)
    start, end = day_window(NOW, 3, 2)
    assert [c[1:] for c in src.calls] == [(start, end), (start - timedelta(days=95), end)]


def test_backfill_marks_only_final_full_chunks() -> None:
    src = FakeSource("s")
    store = FakeStore()
    ing = EnergyIngestor([src], store, EnergySettings())
    start = NOW - timedelta(days=40)
    ing.backfill(start, NOW, now=NOW)
    done = store.done[(1, "a")]
    # Erster Block ist angeschnitten, letzter noch nicht endgültig
    assert all(d >= start for d in done)
    assert all(d + timedelta(days=10) <= NOW - timedelta(days=7) for d in done)
    assert len(done) >= 2

    # Zweiter Lauf lädt nur noch, was nicht als erledigt gilt
    src.calls.clear()
    ing.backfill(start, NOW, now=NOW)
    assert len(src.calls) == 2 * (len(chunks(timedelta(days=10), start, NOW)) - len(done))


def test_backfill_step_newest_first_round_robin() -> None:
    a, b = FakeSource("a"), FakeSource("b")
    settings = EnergySettings(backfill_from=NOW - timedelta(days=100), backfill_chunks_per_cycle=4)
    ing = EnergyIngestor([a, b], FakeStore(), settings)
    results = ing.backfill_step(NOW)
    assert [r.source for r in results] == ["a", "b", "a", "b"]
    # nur endgültige Blöcke, neueste zuerst
    assert results[0].end <= NOW - timedelta(days=7)
    assert results[0].start >= results[2].start


def test_failed_chunk_not_marked() -> None:
    src = FakeSource("x", fail=True)
    store = FakeStore()
    EnergyIngestor([src], store, EnergySettings()).backfill(
        ANCHOR, ANCHOR + timedelta(days=20), now=NOW
    )
    assert store.done == {}


def test_check_returns_deviations() -> None:
    store = FakeStore()
    store.deviations = [Deviation("load_actual", "smard", "entsoe", 48, 900.0, 0.02)]
    devs = EnergyIngestor([], store, EnergySettings()).check(NOW)
    assert devs == store.deviations


def test_status_recorded_per_dataset() -> None:
    good, bad = FakeSource("good"), FakeSource("bad", fail=True)
    store = FakeStore()
    EnergyIngestor([good, bad], store, EnergySettings()).sync(NOW)
    g, b = store.ids["good"], store.ids["bad"]
    assert store.status[(g, "a")] == ("ok", 1, None)
    assert store.status[(b, "a")] == ("failed", 0, "kaputt")


def test_format_error_reported_and_chunk_not_marked() -> None:
    src = FakeSource("x", bad_format=True)
    store = FakeStore()
    results = EnergyIngestor([src], store, EnergySettings()).backfill(
        ANCHOR, ANCHOR + timedelta(days=20), now=NOW
    )
    assert {r.status for r in results} == {"format"}
    assert store.done == {}
    assert store.status[(store.ids["x"], "a")][0] == "format"


def test_backfill_step_loads_final_part_of_boundary_chunk() -> None:
    src = FakeSource("s")
    store = FakeStore()
    final = NOW - timedelta(days=7)
    a, b = chunks(timedelta(days=10), final, final + timedelta(seconds=1))[0]
    settings = EnergySettings(backfill_from=a, backfill_chunks_per_cycle=10)
    ing = EnergyIngestor([src], store, settings)

    ing.backfill_step(NOW)
    assert ("a", a, final) in src.calls
    assert store.done[(1, "a")][a].end == final

    # Am selben Tag nicht noch einmal, nach TAIL_REFRESH bis zur neuen Grenze
    src.calls.clear()
    ing.backfill_step(NOW + timedelta(hours=1))
    assert src.calls == []
    later = NOW + TAIL_REFRESH + timedelta(hours=1)
    store.clock = later
    ing.backfill_step(later)
    assert ("a", a, later - timedelta(days=7)) in src.calls

    # Ist der Block endgültig, wird er ganz geladen und gilt als erledigt
    src.calls.clear()
    done_at = b + timedelta(days=7)
    store.clock = done_at
    ing.backfill_step(done_at)
    assert ("a", a, b) in src.calls
    assert store.done[(1, "a")][a].end == b
    src.calls.clear()
    ing.backfill_step(done_at + timedelta(hours=1))
    assert all(c[1] != a for c in src.calls)


def test_empty_retry_per_source() -> None:
    plain, own = FakeSource("plain"), FakeSource("own")
    own.empty_retry = timedelta(days=1)  # type: ignore[attr-defined]
    store = FakeStore()
    EnergyIngestor([plain, own], store, EnergySettings()).backfill(
        ANCHOR, ANCHOR + timedelta(days=10), now=NOW
    )
    assert store.retry[(store.ids["plain"], "a")] == EMPTY_RETRY
    assert store.retry[(store.ids["own"], "a")] == timedelta(days=1)
