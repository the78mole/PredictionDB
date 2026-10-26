"""Prüft ``energy_ingest.store`` gegen das migrierte Schema (Upsert, Rangfolge, Abgleich).

Läuft nur mit ``PDB_INTEGRATION=1`` nach ``migrate``; legt eigene Testquellen an und
räumt sie wieder ab.
"""

import os
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import Engine, text

from energy_ingest.model import HOUR, QUARTER_HOUR, SeriesData, series
from energy_ingest.store import EnergyStore
from predictiondb_core.db import create_db_engine
from predictiondb_core.settings import Settings

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.environ.get("PDB_INTEGRATION") != "1", reason="PDB_INTEGRATION != 1"),
]

T0 = datetime(2020, 3, 2, 12, 0, tzinfo=UTC)


class Src:
    license = "test"
    free = True

    def __init__(self, priority: int) -> None:
        self.code = f"test_{uuid.uuid4().hex[:8]}"
        self.priority = priority


@pytest.fixture
def engine() -> Iterator[Engine]:
    eng = create_db_engine(Settings())
    yield eng
    with eng.begin() as conn:
        ids = "SELECT source_id FROM meta.source WHERE code LIKE 'test\\_%'"
        conn.execute(
            text(
                "DELETE FROM energy.value WHERE series_id IN "
                f"(SELECT series_id FROM energy.series WHERE source_id IN ({ids}))"
            )
        )
        conn.execute(
            text(
                "DELETE FROM energy.value_revision WHERE series_id IN "
                f"(SELECT series_id FROM energy.series WHERE source_id IN ({ids}))"
            )
        )
        conn.execute(text(f"DELETE FROM energy.series WHERE source_id IN ({ids})"))
        conn.execute(text(f"DELETE FROM meta.energy_fetch WHERE source_id IN ({ids})"))
        conn.execute(text("DELETE FROM meta.source WHERE code LIKE 'test\\_%'"))
    eng.dispose()


def load(values: list[float]) -> SeriesData:
    pts = [(T0 + i * QUARTER_HOUR, v) for i, v in enumerate(values)]
    return SeriesData(series("load_actual"), QUARTER_HOUR, pts)


def test_upsert_keeps_earliest_published(engine: Engine) -> None:
    store = EnergyStore(engine)
    src = Src(priority=5)
    sid = store.source_id(src)

    late = T0 + timedelta(days=400)
    assert store.write(sid, [load([100, 200])], late) == 2
    early = T0 + timedelta(minutes=20)
    assert store.write(sid, [load([110])], early) == 1
    assert store.write(sid, [load([120])], late) == 1

    with engine.connect() as conn:
        rows = conn.execute(
            text("""
                SELECT v.ts, v.value, v.published FROM energy.value v
                  JOIN energy.series s USING (series_id)
                 WHERE s.source_id = :sid ORDER BY v.ts"""),
            {"sid": sid},
        ).all()
    assert [(r.value, r.published) for r in rows] == [
        (120, early),
        (200, T0 + 2 * QUARTER_HOUR + HOUR),
    ]


def revisions(engine: Engine, sid: int) -> list[tuple[datetime, float | None, float | None]]:
    with engine.connect() as conn:
        rows = conn.execute(
            text("""
                SELECT r.ts, r.old_value, r.new_value FROM energy.value_revision r
                  JOIN energy.series s USING (series_id)
                 WHERE s.source_id = :sid ORDER BY r.revised_at, r.ts"""),
            {"sid": sid},
        ).all()
    return [(r.ts, r.old_value, r.new_value) for r in rows]


def test_corrections_are_logged(engine: Engine) -> None:
    store = EnergyStore(engine)
    sid = store.source_id(Src(priority=5))
    fetched = T0 + timedelta(days=1)
    store.write(sid, [load([100, 200, 300])], fetched)
    assert revisions(engine, sid) == []
    # unveränderter Wert wird nicht protokolliert, korrigierter schon
    store.write(sid, [load([100, 250, 300])], fetched)
    assert revisions(engine, sid) == [(T0 + QUARTER_HOUR, 200, 250)]


def test_prune_clears_removed_values(engine: Engine) -> None:
    store = EnergyStore(engine)
    sid = store.source_id(Src(priority=5))
    fetched = T0 + timedelta(days=1)
    store.write(sid, [load([100.0] * 100)], fetched)
    # Quelle liefert den Wert an Position 50 nicht mehr: ohne prune bleibt er stehen
    gap = load([100.0] * 100)
    del gap.points[50]
    store.write(sid, [gap], fetched)
    assert revisions(engine, sid) == []
    store.write(sid, [gap], fetched, prune=True)
    ts = T0 + 50 * QUARTER_HOUR
    assert revisions(engine, sid) == [(ts, 100.0, None)]
    with engine.connect() as conn:
        value = conn.execute(
            text("""
                SELECT v.value FROM energy.value v JOIN energy.series s USING (series_id)
                 WHERE s.source_id = :sid AND v.ts = :ts"""),
            {"sid": sid, "ts": ts},
        ).scalar_one()
    assert value is None

    # viele fehlende Werte auf einmal: eher gestörte Antwort, nichts leeren
    short = load([100.0] * 100)
    del short.points[10:40]
    store.write(sid, [short], fetched, prune=True)
    assert len(revisions(engine, sid)) == 1


def test_value_best_and_compare(engine: Engine) -> None:
    store = EnergyStore(engine)
    a, b = Src(priority=1), Src(priority=2)
    fetched = T0 + timedelta(days=1)
    store.write(store.source_id(a), [load([100.0] * 8)], fetched)
    store.write(store.source_id(b), [load([110.0] * 8)], fetched)

    with engine.connect() as conn:
        best = conn.execute(
            text("""
                SELECT value, source FROM energy.value_best
                 WHERE code = 'load_actual' AND ts = :ts AND source IN (:a, :b)"""),
            {"ts": T0, "a": a.code, "b": b.code},
        ).one()
    assert (best.value, best.source) == (100.0, a.code)

    devs = [
        d
        for d in store.compare(T0, T0 + timedelta(hours=2))
        if {d.source_a, d.source_b} == {a.code, b.code}
    ]
    assert len(devs) == 1
    assert devs[0].n == 2
    assert devs[0].mae == pytest.approx(10.0)


def test_value_best_skips_disabled_sources(engine: Engine) -> None:
    store = EnergyStore(engine)
    a, b = Src(priority=1), Src(priority=2)
    fetched = T0 + timedelta(days=1)
    store.write(store.source_id(a), [load([100.0] * 4)], fetched)
    store.write(store.source_id(b), [load([110.0] * 4)], fetched)
    store.set_enabled({a.code: False})

    with engine.connect() as conn:
        best = conn.execute(
            text("""
                SELECT value, source FROM energy.value_best
                 WHERE code = 'load_actual' AND ts = :ts AND source IN (:a, :b)"""),
            {"ts": T0, "a": a.code, "b": b.code},
        ).one()
    assert (best.value, best.source) == (110.0, b.code)


def test_fetch_log(engine: Engine) -> None:
    store = EnergyStore(engine)
    sid = store.source_id(Src(priority=3))
    store.mark_chunk(sid, "x", T0, T0 + timedelta(days=10), 5)
    store.mark_chunk(sid, "y", T0, T0 + timedelta(days=10), 0)
    assert store.done_chunks(sid, "x") == {T0: T0 + timedelta(days=10)}
    # leere Blöcke gelten vorerst als erledigt, werden aber später erneut versucht
    assert store.done_chunks(sid, "y") == {T0: T0 + timedelta(days=10)}
    # angebrochener Block: Stand und Wertezahl wachsen beim Fortsetzen
    store.mark_chunk(sid, "z", T0, T0 + timedelta(days=3), 4)
    store.mark_chunk(sid, "z", T0, T0 + timedelta(days=4), 2, resume=True)
    assert store.done_chunks(sid, "z") == {T0: T0 + timedelta(days=4)}
    with engine.connect() as conn:
        n = conn.execute(
            text("SELECT n_values FROM meta.energy_fetch WHERE source_id = :s AND dataset = 'z'"),
            {"s": sid},
        ).scalar_one()
    assert n == 6
