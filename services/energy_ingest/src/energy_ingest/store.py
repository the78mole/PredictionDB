"""Schreiben nach ``energy.*`` (Gold) und Abrufprotokoll in ``meta.energy_fetch``.

Werte gehen per ``COPY`` in eine temporäre Tabelle und von dort per Upsert nach
``energy.value``. ``published`` behält beim Überschreiben den frühesten Zeitpunkt, damit
der Leakage-Schutz nicht durch spätere Abrufe aufgeweicht wird. Jede Änderung eines schon
gespeicherten Werts wird mit altem und neuem Wert in ``energy.value_revision`` protokolliert.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import Engine, text

from energy_ingest.model import SeriesData, published_at
from energy_ingest.sources import SourceMeta
from predictiondb_core.log import get_logger

log = get_logger(__name__)

# Leere Blöcke (Quelle hatte nichts oder war gestört) später noch einmal versuchen
EMPTY_RETRY = timedelta(days=30)

# Vor dem Upsert: geänderte Werte mit altem und neuem Stand protokollieren
_LOG_REVISIONS = """
    INSERT INTO energy.value_revision (series_id, ts, old_value, new_value)
    SELECT v.series_id, v.ts, v.value, i.value
      FROM _energy_in i
      JOIN energy.value v USING (series_id, ts)
     WHERE v.value IS DISTINCT FROM i.value
"""

# Werte innerhalb der gelieferten Spanne einer Reihe, die die Quelle nicht mehr liefert
_GONE = """
    WITH span AS (SELECT series_id, min(ts) AS lo, max(ts) AS hi FROM _energy_in GROUP BY 1)
    SELECT v.series_id, v.ts, v.value
      FROM energy.value v
      JOIN span s ON s.series_id = v.series_id AND v.ts BETWEEN s.lo AND s.hi
     WHERE v.value IS NOT NULL
       AND NOT EXISTS (SELECT 1 FROM _energy_in i WHERE i.series_id = v.series_id
                                                   AND i.ts = v.ts)
"""

_UPSERT = """
    INSERT INTO energy.value (series_id, ts, value, published)
    SELECT series_id, ts, value, published FROM _energy_in
    ON CONFLICT (series_id, ts) DO UPDATE
       SET value = EXCLUDED.value,
           published = LEAST(energy.value.published, EXCLUDED.published)
     WHERE energy.value.value IS DISTINCT FROM EXCLUDED.value
        OR energy.value.published IS NULL
        OR EXCLUDED.published < energy.value.published
"""

# Stundenmittel je Reihen-Code und Quelle, paarweise verglichen; nur freigegebene Quellen,
# damit gesperrte Altwerte (meta.source.enabled = false) nicht mehr gelesen werden
_COMPARE = """
    WITH h AS (
      SELECT s.code, src.code AS source, time_bucket('1 hour', v.ts) AS hour,
             avg(v.value) AS value
        FROM energy.value v
        JOIN energy.series s USING (series_id)
        JOIN meta.source src ON src.source_id = s.source_id
       WHERE v.ts >= :start AND v.ts < :end AND v.value IS NOT NULL AND src.enabled
       GROUP BY 1, 2, 3
    )
    SELECT a.code, a.source AS source_a, b.source AS source_b, count(*) AS n,
           avg(abs(a.value - b.value)) AS mae,
           avg(abs(a.value - b.value)) / nullif(avg(abs(a.value)), 0) AS rel
      FROM h a JOIN h b ON a.code = b.code AND a.hour = b.hour AND a.source < b.source
     GROUP BY 1, 2, 3
     ORDER BY 1, 2, 3
"""


@dataclass
class Deviation:
    code: str
    source_a: str
    source_b: str
    n: int
    mae: float
    rel: float | None


#: Entfernte Werte nur leeren, wenn es höchstens so viele sind (Anteil an der Lieferung);
#: mehr deutet auf eine gestörte Antwort statt auf eine Korrektur
PRUNE_MAX_SHARE = 0.05


class EnergyStore:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self._sources: dict[str, int] = {}
        self._series: dict[tuple[int, str], int] = {}

    def source_id(self, source: SourceMeta) -> int:
        if source.code not in self._sources:
            with self.engine.begin() as conn:
                sid = conn.execute(
                    text("""
                        INSERT INTO meta.source (code, kind, license, priority, free)
                        VALUES (:code, 'energy', :license, :priority, :free)
                        ON CONFLICT (code) DO UPDATE
                           SET license = EXCLUDED.license, priority = EXCLUDED.priority,
                               free = EXCLUDED.free
                        RETURNING source_id"""),
                    {
                        "code": source.code,
                        "license": source.license,
                        "priority": source.priority,
                        "free": source.free,
                    },
                ).scalar_one()
            self._sources[source.code] = int(sid)
        return self._sources[source.code]

    def set_enabled(self, flags: dict[str, bool]) -> None:
        """Freigabe je Quelle nach ``meta.source.enabled``; ``energy.value_best`` liest nur
        freigegebene Quellen. Noch unbekannte Quellen werden übergangen."""
        with self.engine.begin() as conn:
            for code, enabled in flags.items():
                conn.execute(
                    text("UPDATE meta.source SET enabled = :e WHERE code = :c"),
                    {"c": code, "e": enabled},
                )

    def _series_id(self, conn: Any, source_id: int, data: SeriesData) -> int:
        key = (source_id, data.code)
        if key not in self._series:
            sid = conn.execute(
                text("""
                    INSERT INTO energy.series (source_id, code, unit, resolution)
                    VALUES (:source_id, :code, :unit, :resolution)
                    ON CONFLICT (source_id, code) DO UPDATE
                       SET unit = EXCLUDED.unit, resolution = EXCLUDED.resolution
                    RETURNING series_id"""),
                {
                    "source_id": source_id,
                    "code": data.code,
                    "unit": data.info.unit,
                    "resolution": data.resolution,
                },
            ).scalar_one()
            self._series[key] = int(sid)
        return self._series[key]

    def write(
        self, source_id: int, batch: list[SeriesData], fetched: datetime, *, prune: bool = False
    ) -> int:
        """Schreibt alle Punkte; gibt die Zahl der gelieferten Werte zurück.

        ``prune`` setzt Werte auf NULL, die innerhalb der gelieferten Spanne einer Reihe
        gespeichert sind, aber nicht mehr geliefert werden (von der Quelle entfernt)."""
        with self.engine.begin() as conn:
            rows: dict[tuple[int, datetime], tuple[float, datetime]] = {}
            for data in batch:
                sid = self._series_id(conn, source_id, data)
                for ts, value in data.points:
                    pub = published_at(data.info.kind, ts, data.resolution, fetched)
                    rows[(sid, ts)] = (value, pub)
            if not rows:
                return 0
            conn.execute(
                text(
                    "CREATE TEMP TABLE IF NOT EXISTS _energy_in "
                    "(series_id int, ts timestamptz, value float8, published timestamptz) "
                    "ON COMMIT DELETE ROWS"
                )
            )
            pg = conn.connection.driver_connection
            assert pg is not None
            with (
                pg.cursor() as cur,
                cur.copy("COPY _energy_in (series_id, ts, value, published) FROM STDIN") as cp,
            ):
                for (sid, ts), (value, pub) in rows.items():
                    cp.write_row((sid, ts, value, pub))
            conn.execute(text(_LOG_REVISIONS))
            conn.execute(text(_UPSERT))
            if prune:
                self._prune(conn, source_id, len(rows))
        return len(rows)

    def _prune(self, conn: Any, source_id: int, n_rows: int) -> None:
        gone = conn.execute(text(_GONE)).all()
        if not gone:
            return
        if len(gone) > max(4, PRUNE_MAX_SHARE * n_rows):
            log.warning("energy.prune_skipped", source_id=source_id, gone=len(gone), n=n_rows)
            return
        params = [{"s": r.series_id, "ts": r.ts, "old": r.value} for r in gone]
        conn.execute(
            text("""
                INSERT INTO energy.value_revision (series_id, ts, old_value, new_value)
                VALUES (:s, :ts, :old, NULL)"""),
            params,
        )
        conn.execute(
            text("UPDATE energy.value SET value = NULL WHERE series_id = :s AND ts = :ts"),
            params,
        )
        log.info("energy.values_removed", source_id=source_id, n=len(gone))

    def done_chunks(self, source_id: int, dataset: str) -> dict[datetime, datetime]:
        """Blockbeginn -> geladen bis (bei vollständigen Blöcken das Blockende); leere
        Blöcke werden nach ``EMPTY_RETRY`` erneut versucht."""
        with self.engine.connect() as conn:
            rows = conn.execute(
                text("""
                    SELECT chunk_start, chunk_end FROM meta.energy_fetch
                     WHERE source_id = :s AND dataset = :d
                       AND (n_values > 0 OR fetched_at > now() - :retry)"""),
                {"s": source_id, "d": dataset, "retry": EMPTY_RETRY},
            ).all()
        return {r.chunk_start: r.chunk_end for r in rows}

    def mark_chunk(
        self,
        source_id: int,
        dataset: str,
        start: datetime,
        end: datetime,
        n_values: int,
        *,
        resume: bool = False,
    ) -> None:
        """Block als bis ``end`` geladen vermerken; ``resume`` zählt die Werte zum bisherigen
        Stand eines angebrochenen Blocks hinzu."""
        with self.engine.begin() as conn:
            conn.execute(
                text("""
                    INSERT INTO meta.energy_fetch
                      (source_id, dataset, chunk_start, chunk_end, n_values, fetched_at)
                    VALUES (:s, :d, :start, :end, :n, now())
                    ON CONFLICT (source_id, dataset, chunk_start) DO UPDATE
                       SET chunk_end = EXCLUDED.chunk_end,
                           n_values = EXCLUDED.n_values
                                      + CASE WHEN :resume THEN meta.energy_fetch.n_values
                                             ELSE 0 END,
                           fetched_at = EXCLUDED.fetched_at"""),
                {
                    "s": source_id,
                    "d": dataset,
                    "start": start,
                    "end": end,
                    "n": n_values,
                    "resume": resume,
                },
            )

    def revised_at(self, source_id: int, dataset: str) -> datetime | None:
        """Zeitpunkt des letzten vollständigen Revisionslaufs (``meta.energy_revise``)."""
        with self.engine.connect() as conn:
            at = conn.execute(
                text("""SELECT revised_at FROM meta.energy_revise
                         WHERE source_id = :s AND dataset = :d"""),
                {"s": source_id, "d": dataset},
            ).scalar_one_or_none()
        return at

    def mark_revised(self, source_id: int, dataset: str, at: datetime) -> None:
        with self.engine.begin() as conn:
            conn.execute(
                text("""
                    INSERT INTO meta.energy_revise (source_id, dataset, revised_at)
                    VALUES (:s, :d, :at)
                    ON CONFLICT (source_id, dataset) DO UPDATE
                       SET revised_at = EXCLUDED.revised_at"""),
                {"s": source_id, "d": dataset, "at": at},
            )

    def record_status(
        self, source_id: int, dataset: str, status: str, n_values: int, error: str | None
    ) -> None:
        """Letzter Ausgang je Quelle und Datensatz (``meta.energy_status``, für Alarme)."""
        with self.engine.begin() as conn:
            conn.execute(
                text("""
                    INSERT INTO meta.energy_status
                      (source_id, dataset, status, n_values, error, checked_at, last_ok_at)
                    VALUES (:s, :d, :status, :n, :error, now(),
                            CASE WHEN :status IN ('ok', 'empty') THEN now() END)
                    ON CONFLICT (source_id, dataset) DO UPDATE
                       SET status = EXCLUDED.status, n_values = EXCLUDED.n_values,
                           error = EXCLUDED.error, checked_at = EXCLUDED.checked_at,
                           last_ok_at = COALESCE(EXCLUDED.last_ok_at,
                                                 meta.energy_status.last_ok_at)"""),
                {"s": source_id, "d": dataset, "status": status, "n": n_values, "error": error},
            )

    def compare(self, start: datetime, end: datetime) -> list[Deviation]:
        with self.engine.connect() as conn:
            rows = conn.execute(text(_COMPARE), {"start": start, "end": end}).all()
        return [
            Deviation(
                r.code,
                r.source_a,
                r.source_b,
                int(r.n),
                float(r.mae),
                None if r.rel is None else float(r.rel),
            )
            for r in rows
        ]
