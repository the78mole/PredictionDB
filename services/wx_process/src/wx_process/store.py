"""Datenbankzugriff: Gitter-Gewichte und Bestand lesen (``assets``), Features schreiben (``feat``).

Gitterzellen werden wie in ``mastr_ingest.store`` über ganzzahlige Indizes
``floor(x / res + 0.5)`` verglichen. Feature-Werte gehen per ``COPY`` nach ``feat.nwp_value``;
ein Lauf wird vorher für die betroffenen Regionen gelöscht, damit Neuberechnungen
idempotent sind.

@relation(SWA-WXP-STORE, scope=file)
"""

from __future__ import annotations

import json
import time
from collections import defaultdict
from collections.abc import Iterable
from datetime import date, datetime
from typing import Any, Literal, NamedTuple, cast

import numpy as np
from numpy.typing import NDArray
from psycopg import Connection as PgConnection
from psycopg.errors import LockNotAvailable, UniqueViolation
from sqlalchemy import Engine, text
from sqlalchemy.exc import IntegrityError, OperationalError

from predictiondb_core.log import get_logger
from wx_process.features import ClusterWeights

log = get_logger(__name__)

#: Höchstens so lange auf die exklusive Sperre eines Chunks warten (Entpacken, Packen)
CHUNK_LOCK_TIMEOUT = "5s"
#: Ein gesperrter Chunk wird frühestens nach so vielen Sekunden erneut versucht
BUSY_RETRY_S = 600.0

SilverStatus = Literal["loading", "present"]
SilverOrigin = Literal["bronze", "ghcr"]
BronzeStatus = Literal["loading", "present", "deleted"]


class Chunk(NamedTuple):
    name: str
    start: datetime
    end: datetime


def _idx(col: str, res_param: str = ":res") -> str:
    return f"floor({col}::float8 / {res_param} + 0.5)::int"


class CapacityTable:
    """Installierte Leistung je Cluster und Tag; nach dem letzten Tag gilt der letzte Wert."""

    def __init__(self, rows: Iterable[tuple[str, int, date, float]]) -> None:
        series: dict[tuple[str, int], list[tuple[int, float]]] = defaultdict(list)
        for tech, no, day, mw in rows:
            series[(tech, int(no))].append((day.toordinal(), float(mw)))
        self._series = {
            k: (np.array([d for d, _ in v]), np.array([m for _, m in v]))
            for k, v in ((k, sorted(v)) for k, v in series.items())
        }

    def at(self, technology: str, cluster_no: int, days: list[date]) -> NDArray[np.float64] | None:
        """@relation(SWR-WXP-034, scope=function)"""
        s = self._series.get((technology, cluster_no))
        if s is None:
            return None
        ords, mw = s
        pos = np.searchsorted(ords, [d.toordinal() for d in days], side="right") - 1
        out = np.where(pos >= 0, mw[np.clip(pos, 0, None)], np.nan)
        return out


class Store:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self._busy: dict[str, float] = {}

    def source_id(self, code: str) -> int:
        with self.engine.connect() as c:
            sid = c.execute(
                text("SELECT source_id FROM meta.source WHERE code = :c"), {"c": code}
            ).scalar_one_or_none()
        if sid is None:
            raise LookupError(f"Quelle {code!r} fehlt in meta.source (alembic upgrade head?)")
        return int(sid)

    # --- Cluster und Gewichte ----------------------------------------------------------------

    def region_ids(self, codes: Iterable[str]) -> dict[str, int]:
        """``meta.region.region_id`` je Code; fehlende Regionen werden angelegt."""
        out: dict[str, int] = {}
        with self.engine.begin() as c:
            for code in codes:
                out[code] = int(
                    c.execute(
                        text("""
                        INSERT INTO meta.region (code) VALUES (:c)
                        ON CONFLICT (code) DO UPDATE SET code = EXCLUDED.code
                        RETURNING region_id"""),
                        {"c": code},
                    ).scalar_one()
                )
        return out

    def latest_clustering(self, grid: str) -> str | None:
        """Jüngste Clusterung, für die Gewichte auf ``grid`` vorliegen."""
        with self.engine.connect() as c:
            v = c.execute(
                text("""
                SELECT c.clustering_version FROM assets.clustering c
                WHERE EXISTS (SELECT 1 FROM assets.grid_weight g
                              WHERE g.clustering_version = c.clustering_version AND g.grid = :g)
                ORDER BY c.created_at DESC LIMIT 1"""),
                {"g": grid},
            ).scalar_one_or_none()
        return None if v is None else str(v)

    def cluster_weights(self, version: str, grid: str) -> list[ClusterWeights]:
        with self.engine.connect() as c:
            rows = c.execute(
                text("""
                SELECT g.technology, g.cluster_no, cl.region_id, g.lat, g.lon, g.share
                FROM assets.grid_weight g
                JOIN assets.cluster cl USING (clustering_version, technology, cluster_no)
                WHERE g.clustering_version = :v AND g.grid = :g AND g.share > 0
                  AND cl.region_id IS NOT NULL
                ORDER BY 1, 2"""),
                {"v": version, "g": grid},
            ).all()
            hubs = self._hub_heights(c, version)
        grouped: dict[tuple[str, int, int], list[tuple[float, float, float]]] = defaultdict(list)
        for tech, no, region, lat, lon, share in rows:
            grouped[(tech, int(no), int(region))].append((float(lat), float(lon), float(share)))
        out = []
        for (tech, no, region), cells in grouped.items():
            arr = np.array(cells, dtype=np.float64)
            out.append(
                ClusterWeights(
                    tech, no, region, arr[:, 0], arr[:, 1], arr[:, 2], hubs.get((tech, no))
                )
            )
        return out

    @staticmethod
    def _hub_heights(c: Any, version: str) -> dict[tuple[str, int], float]:
        """Leistungsgewichtete Nabenhöhe je Wind-Cluster aus dem Snapshot der Gewichte."""
        rows = c.execute(
            text(f"""
            WITH s AS (
              SELECT max(snapshot_id) AS id FROM assets.grid_weight
              WHERE clustering_version = :v)
            SELECT u.technology, cc.cluster_no,
                   sum(u.capacity_kw * u.hub_height_m) / NULLIF(sum(u.capacity_kw), 0)
            FROM assets.unit u
            JOIN assets.clustering k ON k.clustering_version = :v
            JOIN assets.cluster_cell cc
              ON cc.clustering_version = :v AND cc.technology = u.technology
             AND {_idx("cc.lat", "k.grid_res")} = {_idx("u.lat", "k.grid_res")}
             AND {_idx("cc.lon", "k.grid_res")} = {_idx("u.lon", "k.grid_res")}
            WHERE u.snapshot_id = (SELECT id FROM s) AND u.lat IS NOT NULL
              AND u.hub_height_m BETWEEN 10 AND 300
              AND u.technology IN ('wind_on', 'wind_off')
            GROUP BY 1, 2"""),
            {"v": version},
        ).all()
        return {(t, int(n)): float(h) for t, n, h in rows if h is not None}

    def capacity(self, version: str, start: date, end: date) -> CapacityTable:
        """Bestand von ``start`` bis ``end`` plus den letzten Tag davor je Cluster."""
        with self.engine.connect() as c:
            rows = c.execute(
                text("""
                SELECT technology, cluster_no, day, capacity_mw FROM assets.capacity_daily
                WHERE clustering_version = :v AND day BETWEEN :a AND :b
                UNION ALL
                (SELECT DISTINCT ON (technology, cluster_no) technology, cluster_no, day,
                        capacity_mw
                 FROM assets.capacity_daily WHERE clustering_version = :v AND day < :a
                 ORDER BY technology, cluster_no, day DESC)"""),
                {"v": version, "a": start, "b": end},
            ).all()
        return CapacityTable((str(t), int(n), d, float(m)) for t, n, d, m in rows)

    # --- Features ----------------------------------------------------------------------------

    def feature_ids(
        self, source_id: int, aggregation: str, units: dict[str, str]
    ) -> dict[str, int]:
        """Legt fehlende Merkmale in ``meta.feature`` an und liefert ``code → feature_id``."""
        out: dict[str, int] = {}
        with self.engine.begin() as c:
            for code, unit in sorted(units.items()):
                out[code] = int(
                    c.execute(
                        text("""
                        INSERT INTO meta.feature (source_id, code, aggregation, unit)
                        VALUES (:s, :c, :a, :u)
                        ON CONFLICT (source_id, code, aggregation)
                          DO UPDATE SET unit = EXCLUDED.unit
                        RETURNING feature_id"""),
                        {"s": source_id, "c": code, "a": aggregation, "u": unit},
                    ).scalar_one()
                )
        return out

    def write_values(
        self,
        run: datetime,
        valid_until: datetime,
        feature_ids: list[int],
        region_ids: list[int],
        rows: Iterable[tuple[int, int, datetime, float]],
        *,
        replace: bool = True,
    ) -> int:
        """Ersetzt die Werte eines Laufs für diese Merkmale und Regionen.

        ``replace=False`` lässt das DELETE weg (Lauf hatte noch nie Features); das spart in
        komprimierten Chunks einen Durchgang. Liegen doch Werte vor, wird ersetzt.

        @relation(SWR-WXP-036, scope=function)
        """
        rows = list(rows)
        if not replace:
            try:
                return self._write_values(run, valid_until, feature_ids, region_ids, rows, False)
            except (IntegrityError, UniqueViolation):
                log.warning("features.values_present", run=f"{run:%Y%m%d%H}")
        return self._write_values(run, valid_until, feature_ids, region_ids, rows, True)

    def _write_values(
        self,
        run: datetime,
        valid_until: datetime,
        feature_ids: list[int],
        region_ids: list[int],
        rows: list[tuple[int, int, datetime, float]],
        replace: bool,
    ) -> int:
        n = 0
        with self.engine.begin() as c:
            # Nicht ewig auf Sperren warten (z. B. hinter einer verwaisten Sitzung): Der Lauf
            # schlägt dann fehl und kommt im nächsten Durchgang wieder dran.
            c.execute(text("SET LOCAL lock_timeout = '10min'"))
            # Live- und Backfill-Dienst schreiben denselben Lauf nie gleichzeitig
            c.execute(
                text("SELECT pg_advisory_xact_lock(hashtext('feat.nwp_value'), hashtext(:k))"),
                {"k": run.isoformat()},
            )
            if replace:
                c.execute(
                    text("""
                DELETE FROM feat.nwp_value
                WHERE run_time = :r AND valid_time BETWEEN :r AND :u
                  AND feature_id = ANY(:f) AND region_id = ANY(:g)"""),
                    {"r": run, "u": valid_until, "f": feature_ids, "g": region_ids},
                )
            pg = cast(PgConnection[Any], c.connection.driver_connection)
            with pg.cursor().copy(
                "COPY feat.nwp_value (feature_id, region_id, run_time, valid_time, value) "
                "FROM STDIN"
            ) as copy:
                for feature_id, region_id, valid, value in rows:
                    copy.write_row((feature_id, region_id, run, valid, value))
                    n += 1
        return n

    # --- Kompression (Backfill in alte, schon komprimierte Chunks) ----------------------------

    def decompress_values(self, start: datetime, end: datetime) -> list[Chunk]:
        """Entpackt die komprimierten Chunks von ``feat.nwp_value``, die ``[start, end]`` berühren.

        Ein COPY in einen komprimierten Chunk entpackt für die Prüfung des Primärschlüssels
        jedes betroffene Segment und lässt den Chunk teilweise komprimiert zurück; ein
        Backfill schreibt deshalb besser in entpackte Chunks und packt sie danach einmal.

        @relation(SWR-WXP-041, scope=function)
        """
        with self.engine.connect() as c:
            rows = c.execute(
                text("""
                SELECT format('%I.%I', chunk_schema, chunk_name), range_start, range_end
                  FROM timescaledb_information.chunks
                 WHERE hypertable_schema = 'feat' AND hypertable_name = 'nwp_value'
                   AND is_compressed AND range_start <= :e AND range_end > :s
                 ORDER BY range_start"""),
                {"s": start, "e": end},
            ).all()
        chunks = [Chunk(str(r[0]), r[1], r[2]) for r in rows]
        now = time.monotonic()
        done = []
        for ch in chunks:
            if now - self._busy.get(ch.name, -BUSY_RETRY_S) < BUSY_RETRY_S:
                continue  # eben noch gesperrt: in komprimierte Chunks schreiben (langsamer)
            if self._chunk_op("decompress_chunk", ch):
                done.append(ch)
                log.info("chunk.decompressed", chunk=ch.name, start=f"{ch.start:%Y-%m-%d}")
        return done

    def compress_values(self, chunks: Iterable[Chunk]) -> None:
        """Packt die Chunks wieder; gesperrte bleiben für die Kompressions-Policy liegen.

        @relation(SWR-WXP-041, scope=function)
        """
        for ch in chunks:
            if self._chunk_op("compress_chunk", ch):
                log.info("chunk.compressed", chunk=ch.name, start=f"{ch.start:%Y-%m-%d}")

    def _chunk_op(self, fn: Literal["decompress_chunk", "compress_chunk"], ch: Chunk) -> bool:
        """Entpacken und Packen brauchen eine exklusive Sperre auf den Chunk.

        Wartet die Anweisung auf einen Leser (z. B. ``ml-train snapshot`` in einer langen
        Transaktion), stellen sich alle weiteren Leser des Chunks hinter ihr an. Deshalb nur
        kurz warten und bei Erfolglosigkeit den Chunk vorerst so lassen, wie er ist.

        @relation(SWR-WXP-041, scope=function)
        """
        try:
            with self.engine.begin() as c:
                c.execute(text(f"SET LOCAL lock_timeout = '{CHUNK_LOCK_TIMEOUT}'"))
                c.execute(text(f"SELECT {fn}(CAST(:n AS regclass), true)"), {"n": ch.name})
        except OperationalError as exc:
            if not isinstance(exc.orig, LockNotAvailable):
                raise
            self._busy[ch.name] = time.monotonic()
            log.warning("chunk.locked", op=fn, chunk=ch.name, start=f"{ch.start:%Y-%m-%d}")
            return False
        return True

    # --- Zustand -----------------------------------------------------------------------------

    def has_features(self, source_id: int, run: datetime) -> bool:
        """Hatte der Lauf schon einmal Features (gleich welche Clusterung oder Version)?"""
        with self.engine.connect() as c:
            return bool(
                c.execute(
                    text("""
                    SELECT EXISTS (SELECT 1 FROM meta.nwp_feature_run
                    WHERE source_id = :s AND run_time = :r)"""),
                    {"s": source_id, "r": run},
                ).scalar_one()
            )

    def processed(self, source_id: int, run: datetime, version: str, features_version: str) -> bool:
        with self.engine.connect() as c:
            return bool(
                c.execute(
                    text("""
                    SELECT EXISTS (SELECT 1 FROM meta.nwp_feature_run
                    WHERE source_id = :s AND run_time = :r AND clustering_version = :v
                      AND features_version = :fv)"""),
                    {"s": source_id, "r": run, "v": version, "fv": features_version},
                ).scalar_one()
            )

    def mark_processed(
        self,
        source_id: int,
        run: datetime,
        version: str,
        features_version: str,
        silver_key: str,
        n_values: int,
        stats: dict[str, Any],
        bronze: dict[str, Any],
    ) -> None:
        """Verarbeitungsstand speichern, ``meta.nwp_run`` nachführen, ``features_ready`` melden.

        @relation(SWR-WXP-036, scope=function)
        """
        endpoints: dict[str, int] = bronze.get("endpoints") or {}
        endpoint = max(endpoints, key=lambda k: endpoints[k]) if endpoints else None
        payload = json.dumps(
            {"source_id": source_id, "run_time": run.isoformat(), "clustering_version": version}
        )
        with self.engine.begin() as c:
            c.execute(
                text("""
                INSERT INTO meta.nwp_feature_run
                  (source_id, run_time, clustering_version, features_version, silver_key,
                   n_values, stats)
                VALUES (:s, :r, :v, :fv, :k, :n, CAST(:st AS jsonb))
                ON CONFLICT (source_id, run_time, clustering_version) DO UPDATE SET
                  features_version = EXCLUDED.features_version, silver_key = EXCLUDED.silver_key,
                  n_values = EXCLUDED.n_values, stats = EXCLUDED.stats, processed_at = now()"""),
                {
                    "s": source_id,
                    "r": run,
                    "v": version,
                    "fv": features_version,
                    "k": silver_key,
                    "n": n_values,
                    "st": json.dumps(stats, default=str),
                },
            )
            c.execute(
                text("""
                INSERT INTO meta.nwp_run
                  (source_id, run_time, status, endpoint, n_expected, n_stored, bytes_stored)
                VALUES (:s, :r, 'processed', :e, :n, :n, :b)
                ON CONFLICT (source_id, run_time) DO UPDATE SET status = 'processed',
                  endpoint = EXCLUDED.endpoint, n_expected = EXCLUDED.n_expected,
                  n_stored = EXCLUDED.n_stored, bytes_stored = EXCLUDED.bytes_stored,
                  updated_at = now()"""),
                {
                    "s": source_id,
                    "r": run,
                    "e": endpoint,
                    "n": bronze.get("steps"),
                    "b": bronze.get("bytes"),
                },
            )
            c.execute(text("SELECT pg_notify('features_ready', :p)"), {"p": payload})

    # --- Silber-Bestand (meta.silver_run, nur für das Status-Dashboard) ---------------------

    def silver_runs(self, source_id: int) -> dict[datetime, str]:
        with self.engine.connect() as c:
            rows = c.execute(
                text("SELECT run_time, status FROM meta.silver_run WHERE source_id = :s"),
                {"s": source_id},
            ).all()
        return {r[0]: str(r[1]) for r in rows}

    def mark_silver(
        self,
        source_id: int,
        runs: Iterable[datetime],
        status: SilverStatus,
        origin: SilverOrigin,
    ) -> None:
        """@relation(SWR-WXP-023, scope=function)"""
        params = [{"s": source_id, "r": r, "st": status, "o": origin} for r in runs]
        if not params:
            return
        with self.engine.begin() as c:
            c.execute(
                text("""
                INSERT INTO meta.silver_run (source_id, run_time, status, origin)
                VALUES (:s, :r, :st, :o)
                ON CONFLICT (source_id, run_time) DO UPDATE SET status = EXCLUDED.status,
                  origin = EXCLUDED.origin, updated_at = now()"""),
                params,
            )

    # --- Bronze-Bestand (meta.bronze_run, GEFS auf der VM, nur für das Status-Dashboard) -----

    def bronze_runs(self, source_id: int) -> dict[datetime, str]:
        with self.engine.connect() as c:
            rows = c.execute(
                text("SELECT run_time, status FROM meta.bronze_run WHERE source_id = :s"),
                {"s": source_id},
            ).all()
        return {r[0]: str(r[1]) for r in rows}

    def mark_bronze(
        self,
        source_id: int,
        runs: Iterable[datetime],
        status: BronzeStatus,
        nbytes: int | None = None,
    ) -> None:
        """@relation(SWR-WXP-023, scope=function)"""
        params = [{"s": source_id, "r": r, "st": status, "b": nbytes} for r in runs]
        if not params:
            return
        with self.engine.begin() as c:
            c.execute(
                text("""
                INSERT INTO meta.bronze_run (source_id, run_time, status, bytes)
                VALUES (:s, :r, :st, :b)
                ON CONFLICT (source_id, run_time) DO UPDATE SET status = EXCLUDED.status,
                  bytes = coalesce(EXCLUDED.bytes, meta.bronze_run.bytes), updated_at = now()"""),
                params,
            )

    def local_bronze_runs(self, source_id: int, start: datetime, end: datetime) -> set[datetime]:
        """Läufe von ``start`` bis vor ``end``, deren Bronze laut Datenbank lokal liegen müsste:
        ein Download hat sie abgelegt oder vorgefunden (``meta.backfill_run`` complete/skipped)
        oder das Silber wurde aus Bronze gerechnet; ohne die schon als gelöscht gemeldeten.

        @relation(SWR-WXP-024, scope=function)
        """
        with self.engine.connect() as c:
            rows = c.execute(
                text("""
                SELECT r.run_time FROM meta.backfill_run r JOIN meta.backfill_job j USING (job_id)
                 WHERE j.source_id = :s AND r.status IN ('complete', 'skipped')
                   AND r.run_time >= :a AND r.run_time < :b
                UNION
                SELECT run_time FROM meta.silver_run
                 WHERE source_id = :s AND origin = 'bronze' AND status = 'present'
                   AND run_time >= :a AND run_time < :b
                EXCEPT
                SELECT run_time FROM meta.bronze_run
                 WHERE source_id = :s AND status = 'deleted'"""),
                {"s": source_id, "a": start, "b": end},
            ).all()
        return {r[0] for r in rows}

    def clear_silver_loading(self, source_id: int, runs: Iterable[datetime] | None = None) -> None:
        """``loading`` zurücknehmen: für ``runs`` oder alle (neuer Import-Durchgang)."""
        sql = "DELETE FROM meta.silver_run WHERE source_id = :s AND status = 'loading'"
        params: dict[str, Any] = {"s": source_id}
        if runs is not None:
            params["r"] = list(runs)
            if not params["r"]:
                return
            sql += " AND run_time = ANY(:r)"
        with self.engine.begin() as c:
            c.execute(text(sql), params)
