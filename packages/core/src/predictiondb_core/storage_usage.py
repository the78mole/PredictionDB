"""Speicherbelegung je Wettergruppe (GFS DE, GFS Europa, GEFS) in Garage und Postgres.

Schreibt periodisch eine Messung nach ``meta.storage_usage``, die das Wetter-Dashboard nur noch
liest; Grafana selbst listet nie Buckets.

- **Garage:** Bucket-Gesamtwerte kommen billig aus der Admin-API (``GetBucketInfo``). Die
  Aufteilung je Gruppe braucht ein Listing; das läuft je Monatspräfix
  (``gfs/0p25/2024/05/`` …) und wird in ``meta.storage_s3_month`` zwischengespeichert. Neu
  gelistet wird ein Monat nur, wenn er noch nie gemessen wurde, der laufende oder vorige Monat
  ist, sich laut ``meta.silver_run``/``meta.bronze_run``/``meta.backfill_run`` dort seit der
  letzten Messung etwas geändert hat oder die Messung älter als ``--max-age-days`` ist. Ein
  Zeitbudget je Durchgang verteilt die erste Vollmessung auf mehrere Durchgänge; solange etwas
  offen ist, folgt der nächste Durchgang schon nach ``--catch-up`` Sekunden.
- **Postgres:** Gold (``feat.nwp_value``) je Gruppe über die Merkmale (``meta.feature``) der
  Quelle: Größe je Chunk anteilig nach Zeilen je Quelle, die Zählung je Chunk zwischengespeichert
  in ``meta.storage_pg_chunk`` (neu nur bei geänderter Chunkgröße). Dazu die Metatabellen je
  Quelle anteilig nach Zeilen und der Rest der Datenbank als ``sonstige``.
- **Garage-Metadaten:** Größe des LMDB-Verzeichnisses (inkl. Snapshots), wenn es ins
  Messverzeichnis eingebunden ist.

@relation(SWA-CORE-USAGE, scope=file)
"""

import argparse
import json
import os
import sys
import time
from collections.abc import Iterable, Iterator
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx
import obstore
from obstore.store import ObjectStore
from sqlalchemy import Engine, text

from predictiondb_core.db import create_db_engine
from predictiondb_core.garage_init import GarageAdminSettings
from predictiondb_core.log import configure_logging, get_logger
from predictiondb_core.s3 import s3_store
from predictiondb_core.settings import Settings

log = get_logger(__name__)


@dataclass(frozen=True)
class Group:
    """Eine Wettergruppe: Quelle in ``meta.source`` und Präfix in wx-bronze/wx-silver."""

    name: str
    source: str
    prefix: str


GROUPS = (
    Group("gfs-de", "gfs_0p25", "gfs/0p25"),
    Group("gfs-eu", "gfs_0p5", "gfs/0p5"),
    Group("gefs", "gefs_0p5", "gefs/0p5"),
)
OTHER = "sonstige"
#: Teil für hochgerechnetes Silber noch nicht gelisteter Monate
SILVER_ESTIMATE = "Silber (hochgerechnet)"

_BY_SOURCE = (
    "SELECT s.code, count(*) AS n FROM {t} r JOIN meta.source s USING (source_id) GROUP BY 1"
)
#: Metatabellen je Lauf bzw. Datei mit der Zählung je Quelle; Größe anteilig nach Zeilen
META_TABLES = {
    "meta.nwp_run": _BY_SOURCE,
    "meta.grib_message": _BY_SOURCE,
    "meta.nwp_feature_run": _BY_SOURCE,
    "meta.silver_run": _BY_SOURCE,
    "meta.bronze_run": _BY_SOURCE,
    "meta.backfill_run": (
        "SELECT s.code, count(*) AS n FROM {t} r JOIN meta.backfill_job j USING (job_id)"
        " JOIN meta.source s USING (source_id) GROUP BY 1"
    ),
}


def list_size(store: ObjectStore, prefix: str) -> tuple[int, int]:
    """Summe der Objektgrößen und Anzahl unter einem Präfix (vollständiges Listing)."""
    size = n = 0
    for batch in obstore.list(store, prefix=prefix, chunk_size=1000):
        for meta in batch:
            size += meta["size"]
            n += 1
    return size, n


class Usage:
    """@relation(SWR-CORE-006, scope=class)"""

    def __init__(self, engine: Engine, settings: Settings) -> None:
        self.engine = engine
        self.settings = settings
        #: Nach einem Durchgang: Zeitbudget erschöpft, es sind noch Monate bzw. Chunks offen
        self.pending = False

    # --- Garage je Monat -----------------------------------------------------------------------

    def _known_months(self, bucket: str, group: Group) -> list[tuple[date, str]]:
        """Monatspräfixe ``<prefix>/JJJJ/MM/``, in denen laut Datenbank Objekte liegen können.

        Kommt aus den Bestandstabellen der Dienste statt aus Delimiter-Listings: Die brauchen
        in wx-silver mit über einer Million Objekten mehrere Minuten. Dazu kommen der laufende
        und vorige Monat sowie alle schon gemessenen Monate (leere fallen beim Listing raus).
        """
        if bucket == self.settings.buckets.silver:
            sql = """SELECT DISTINCT date_trunc('month', r.run_time AT TIME ZONE 'UTC')::date AS m
                       FROM meta.silver_run r JOIN meta.source s USING (source_id)
                      WHERE s.code = :q"""
        else:
            sql = """SELECT date_trunc('month', r.run_time AT TIME ZONE 'UTC')::date AS m
                       FROM meta.bronze_run r JOIN meta.source s USING (source_id)
                      WHERE s.code = :q AND r.status <> 'deleted'
                     UNION
                     SELECT date_trunc('month', r.run_time AT TIME ZONE 'UTC')::date
                       FROM meta.backfill_run r JOIN meta.backfill_job j USING (job_id)
                       JOIN meta.source s USING (source_id)
                      WHERE s.code = :q AND r.status IN ('complete', 'skipped', 'running')
                     UNION
                     SELECT date_trunc('month', r.run_time AT TIME ZONE 'UTC')::date
                       FROM meta.silver_run r JOIN meta.source s USING (source_id)
                      WHERE s.code = :q AND r.origin = 'bronze'"""
        today = datetime.now(UTC).date().replace(day=1)
        months = {today, (today - timedelta(days=1)).replace(day=1)}
        with self.engine.connect() as c:
            months |= {r.m for r in c.execute(text(sql), {"q": group.source})}
            months |= {
                r.month
                for r in c.execute(
                    text("SELECT month FROM meta.storage_s3_month WHERE bucket = :b AND grp = :g"),
                    {"b": bucket, "g": group.name},
                )
            }
        return [(m, f"{group.prefix}/{m:%Y/%m}/") for m in sorted(months)]

    def _due_months(
        self, bucket: str, group: Group, months: list[tuple[date, str]], max_age: timedelta
    ) -> list[tuple[date, str, datetime | None]]:
        """Monate, die neu gelistet werden müssen, mit ihrer letzten Messung."""
        today = datetime.now(UTC).date().replace(day=1)
        recent = {today, (today - timedelta(days=1)).replace(day=1)}
        layer_table = "meta.silver_run" if bucket == self.settings.buckets.silver else None
        with self.engine.connect() as c:
            scanned = {
                r.month: r.scanned_at
                for r in c.execute(
                    text("""SELECT month, scanned_at FROM meta.storage_s3_month
                             WHERE bucket = :b AND grp = :g"""),
                    {"b": bucket, "g": group.name},
                )
            }
            # Letzte Änderung je Monat laut den Bestandstabellen der Dienste
            changed: dict[date, datetime] = {}
            sources = [layer_table] if layer_table else ["meta.bronze_run", "meta.backfill_run"]
            for table in sources:
                if table == "meta.backfill_run":
                    sql = """SELECT date_trunc('month', r.run_time AT TIME ZONE 'UTC')::date AS mon,
                                    max(r.updated_at) AS last_change
                               FROM meta.backfill_run r JOIN meta.backfill_job j USING (job_id)
                               JOIN meta.source s USING (source_id)
                              WHERE s.code = :q GROUP BY 1"""
                else:
                    sql = f"""SELECT date_trunc('month', r.run_time AT TIME ZONE 'UTC')::date
                                     AS mon,
                                     max(r.updated_at) AS last_change
                                FROM {table} r JOIN meta.source s USING (source_id)
                               WHERE s.code = :q GROUP BY 1"""
                for r in c.execute(text(sql), {"q": group.source}):
                    if r.mon not in changed or r.last_change > changed[r.mon]:
                        changed[r.mon] = r.last_change
        now = datetime.now(UTC)
        due: list[tuple[date, str, datetime | None]] = []
        for month, prefix in months:
            last = scanned.get(month)
            if (
                last is None
                or month in recent
                or now - last > max_age
                or (month in changed and changed[month] > last)
            ):
                due.append((month, prefix, last))
        return due

    def scan_s3(self, budget_s: float, max_age: timedelta, parallel: int = 4) -> int:
        """Listet fällige Monate, bis das Zeitbudget aufgebraucht ist; gibt die Anzahl zurück.

        Über alle Buckets und Gruppen gemeinsam: nie gemessene Monate zuerst, Silber vor
        Bronze, dann die älteste Messung. So kommt jede Gruppe bei der ersten Vollmessung
        früh dran, und laufend geänderte Bronze-Monate (Löschen nach Frist) halten Silber
        nicht auf. ``parallel`` Monate werden gleichzeitig gelistet.
        """
        never = datetime.min.replace(tzinfo=UTC)
        tasks: list[tuple[int, int, datetime, int, str, Group, date, str]] = []
        stores: dict[str, ObjectStore] = {}
        rank = {self.settings.buckets.silver: 0, self.settings.buckets.bronze: 1}
        for bucket in (self.settings.buckets.silver, self.settings.buckets.bronze):
            stores[bucket] = s3_store(bucket, self.settings)
            for group in GROUPS:
                months = self._known_months(bucket, group)
                with self.engine.connect() as c:
                    scanned = {
                        r.month
                        for r in c.execute(
                            text("""SELECT month FROM meta.storage_s3_month
                                     WHERE bucket = :b AND grp = :g"""),
                            {"b": bucket, "g": group.name},
                        )
                    }
                due = self._due_months(bucket, group, months, max_age)
                # jüngste Monate zuerst; i verzahnt die Gruppen bei der ersten Vollmessung
                for i, (month, prefix, last) in enumerate(reversed(due)):
                    first = 0 if month not in scanned else 1
                    tasks.append(
                        (first, rank[bucket], last or never, i, bucket, group, month, prefix)
                    )
        tasks.sort(key=lambda t: t[:4])
        # Das Budget zählt erst ab hier: Die Monatsliste kommt aus der Datenbank
        deadline = time.monotonic() + budget_s

        def work(bucket: str, group: Group, month: date, prefix: str) -> None:
            t0 = time.monotonic()
            size, objects = list_size(stores[bucket], prefix)
            with self.engine.begin() as c:
                if objects == 0:
                    # Monat leer (z. B. Bronze nach Frist gelöscht)
                    c.execute(
                        text("""DELETE FROM meta.storage_s3_month
                                 WHERE bucket = :b AND grp = :g AND month = :m"""),
                        {"b": bucket, "g": group.name, "m": month},
                    )
                    return
                c.execute(
                    text("""
                    INSERT INTO meta.storage_s3_month
                           (bucket, grp, month, prefix, bytes, objects, scanned_at)
                    VALUES (:b, :g, :m, :p, :s, :n, now())
                    ON CONFLICT (bucket, grp, month) DO UPDATE
                       SET prefix = EXCLUDED.prefix, bytes = EXCLUDED.bytes,
                           objects = EXCLUDED.objects, scanned_at = now()"""),
                    {
                        "b": bucket,
                        "g": group.name,
                        "m": month,
                        "p": prefix,
                        "s": size,
                        "n": objects,
                    },
                )
            log.info(
                "storage.s3.month",
                bucket=bucket,
                prefix=prefix,
                bytes=size,
                objects=objects,
                seconds=round(time.monotonic() - t0, 1),
            )

        n = 0
        queue = iter(tasks)
        with ThreadPoolExecutor(max_workers=max(parallel, 1)) as pool:
            running: set[Future[None]] = set()
            for task in queue:
                if time.monotonic() > deadline:
                    self.pending = True
                    log.info("storage.s3.budget", open=len(tasks) - n - len(running))
                    break
                running.add(pool.submit(work, *task[4:]))
                if len(running) >= max(parallel, 1):
                    done, running = wait(running, return_when=FIRST_COMPLETED)
                    for f in done:
                        f.result()
                        n += 1
            for f in running:
                f.result()
                n += 1
        return n

    # --- Messung -------------------------------------------------------------------------------

    def bucket_totals(self, client: httpx.Client | None) -> dict[str, tuple[int, int]]:
        """Bytes und Objekte je Bucket aus der Garage-Admin-API (ohne Listing)."""
        if client is None:
            return {}
        out: dict[str, tuple[int, int]] = {}
        for bucket in self.settings.buckets.all():
            r = client.get("/v2/GetBucketInfo", params={"globalAlias": bucket})
            if r.status_code != httpx.codes.OK:
                log.warning("storage.bucket.info", bucket=bucket, status=r.status_code)
                continue
            body = r.json()
            out[bucket] = (int(body.get("bytes", 0)), int(body.get("objects", 0)))
        return out

    def s3_rows(
        self, totals: dict[str, tuple[int, int]]
    ) -> Iterator[tuple[str, str, str, int, int]]:
        """(Speicher, Teil, Gruppe, Bytes, Objekte) aus dem Monats-Zwischenspeicher und den
        Bucket-Gesamtwerten; was keiner Gruppe gehört, zählt als ``sonstige``."""
        layer = {self.settings.buckets.bronze: "Bronze", self.settings.buckets.silver: "Silber"}
        with self.engine.connect() as c:
            per = {
                (r.bucket, r.grp): (int(r.bytes), int(r.objects))
                for r in c.execute(
                    text("""SELECT bucket, grp, sum(bytes) AS bytes, sum(objects) AS objects
                              FROM meta.storage_s3_month GROUP BY 1, 2""")
                )
            }
        for (bucket, grp), (size, objects) in per.items():
            yield "s3", layer.get(bucket, bucket), grp, size, objects
        # Silber noch nicht gelisteter Monate hochrechnen: mittlere Größe je Lauf der schon
        # gelisteten Monate der Gruppe mal Läufe laut meta.silver_run
        estimated = dict(self.silver_estimate())
        for grp, size in estimated.items():
            yield "s3", SILVER_ESTIMATE, grp, size, 0
        for bucket, (size, objects) in totals.items():
            known = [v for (b, _), v in per.items() if b == bucket]
            rest_b = size - sum(v[0] for v in known)
            if bucket == self.settings.buckets.silver:
                rest_b -= sum(estimated.values())
            rest_n = objects - sum(v[1] for v in known)
            if rest_b > 0 or rest_n > 0:
                yield "s3", layer.get(bucket, bucket), OTHER, max(rest_b, 0), max(rest_n, 0)

    def silver_estimate(self) -> Iterator[tuple[str, int]]:
        """(Gruppe, Bytes) für Silber in Monaten, die noch nicht gelistet sind."""
        with self.engine.connect() as c:
            for group in GROUPS:
                r = c.execute(
                    text("""
                    WITH runs AS (
                      SELECT date_trunc('month', r.run_time AT TIME ZONE 'UTC')::date AS month,
                             count(*) AS n
                        FROM meta.silver_run r JOIN meta.source s USING (source_id)
                       WHERE s.code = :q AND r.status = 'present' GROUP BY 1),
                    m AS (SELECT month, bytes FROM meta.storage_s3_month
                           WHERE bucket = :b AND grp = :g)
                    SELECT sum(m.bytes) AS b_known,
                           sum(runs.n) FILTER (WHERE m.month IS NOT NULL) AS n_known,
                           sum(runs.n) FILTER (WHERE m.month IS NULL) AS n_open
                      FROM runs LEFT JOIN m USING (month)"""),
                    {"q": group.source, "b": self.settings.buckets.silver, "g": group.name},
                ).one()
                if r.n_known and r.n_open and r.b_known:
                    yield group.name, int(r.b_known) * int(r.n_open) // int(r.n_known)

    def postgres_rows(self, budget_s: float) -> Iterator[tuple[str, str, str, int, int]]:
        """Gold und Metatabellen je Gruppe, Rest der Datenbank als ``sonstige``."""
        groups = {g.source: g.name for g in GROUPS}
        gold: dict[str, int] = dict.fromkeys(groups.values(), 0)
        rows_gold: dict[str, int] = dict.fromkeys(groups.values(), 0)
        for code, (size, n) in self.gold_by_source(budget_s).items():
            g = groups.get(code, OTHER)
            gold[g] = gold.get(g, 0) + size
            rows_gold[g] = rows_gold.get(g, 0) + n
        for g, size in gold.items():
            yield "postgres", "Gold", g, size, rows_gold[g]
        with self.engine.connect() as c:
            meta_total: dict[str, int] = {}
            for table, sql in META_TABLES.items():
                tsize = c.execute(
                    text("SELECT pg_total_relation_size(to_regclass(:t))"), {"t": table}
                ).scalar()
                if not tsize:
                    continue
                shares = c.execute(text(sql.format(t=table))).all()
                total = sum(r.n for r in shares) or 1
                for r in shares:
                    g = groups.get(r.code, OTHER)
                    meta_total[g] = meta_total.get(g, 0) + int(tsize) * r.n // total
            for g, size in meta_total.items():
                yield "postgres", "Metadaten", g, size, 0

            db = int(c.execute(text("SELECT pg_database_size(current_database())")).scalar_one())
            rest = db - sum(gold.values()) - sum(meta_total.values())
            yield "postgres", "übrige Tabellen", OTHER, max(rest, 0), 0

    def gold_by_source(self, budget_s: float) -> dict[str, tuple[int, int]]:
        """Bytes und Zeilen von ``feat.nwp_value`` je Quelle.

        Je Chunk wird die Gesamtgröße (``chunks_detailed_size``: Tabelle, Indizes, TOAST, auch
        der spaltenweise komprimierte Teil) nach der Zeilenzahl je Quelle verteilt. Die Zählung
        je Chunk liegt in ``meta.storage_pg_chunk`` und wird nur wiederholt, wenn sich die
        Chunkgröße geändert hat, also praktisch nur für die laufenden Wochen und frisch
        komprimierte oder nachgefüllte Chunks. Noch ungezählte Chunks (Zeitbudget) verteilt
        es nach den Zeilen je Quelle aus ``meta.nwp_feature_run``.
        """
        deadline = time.monotonic() + budget_s
        out: dict[str, list[int]] = {}
        with self.engine.connect() as c:
            chunks = c.execute(text(CHUNKS_SQL)).all()
            cached = {
                r.chunk: (int(r.bytes), r.shares)
                for r in c.execute(text("SELECT chunk, bytes, shares FROM meta.storage_pg_chunk"))
            }
            fallback = {
                r.code: [int(r.n), 0]
                for r in c.execute(
                    text("""
                    SELECT s.code, sum(r.n_values) AS n FROM meta.nwp_feature_run r
                      JOIN meta.source s USING (source_id) GROUP BY 1""")
                )
            }
        for ch in chunks:
            size = int(ch.bytes)
            if ch.chunk in cached and cached[ch.chunk][0] == size:
                shares = cached[ch.chunk][1]
            elif time.monotonic() < deadline:
                shares = self._chunk_shares(ch.chunk)
                with self.engine.begin() as c:
                    c.execute(
                        text("""INSERT INTO meta.storage_pg_chunk
                                       (chunk, bytes, shares, measured_at)
                                VALUES (:c, :b, CAST(:s AS jsonb), now())
                                ON CONFLICT (chunk) DO UPDATE SET bytes = EXCLUDED.bytes,
                                   shares = EXCLUDED.shares, measured_at = now()"""),
                        {"c": ch.chunk, "b": size, "s": json.dumps(shares)},
                    )
            else:
                # Zeilen unbekannt: nur die Größe verteilen
                self.pending = True
                shares = cached[ch.chunk][1] if ch.chunk in cached else fallback
                shares = {k: [v[0], 0] for k, v in shares.items()}
            total = sum(v[0] for v in shares.values())
            if not total:
                continue
            for code, (n, rows) in shares.items():
                acc = out.setdefault(code, [0, 0])
                acc[0] += size * n // total
                acc[1] += rows
        with self.engine.begin() as c:
            c.execute(
                text("DELETE FROM meta.storage_pg_chunk WHERE NOT (chunk = ANY(:s))"),
                {"s": [ch.chunk for ch in chunks]},
            )
        return {k: (v[0], v[1]) for k, v in out.items()}

    def _chunk_shares(self, chunk: str) -> dict[str, list[int]]:
        """Zeilen je Quelle in einem Chunk: {code: [zeilen, zeilen]} (Gewicht und Anzahl)."""
        t0 = time.monotonic()
        with self.engine.connect() as c:
            rows = c.execute(
                text(f"""
                SELECT s.code, count(*) AS n FROM {chunk} v
                  JOIN meta.feature f USING (feature_id)
                  JOIN meta.source s ON s.source_id = f.source_id
                 GROUP BY 1""")
            ).all()
        log.info("storage.pg.chunk", chunk=chunk, seconds=round(time.monotonic() - t0, 1))
        return {r.code: [int(r.n), int(r.n)] for r in rows}

    def garage_meta_rows(self, meta_dir: Path | None) -> Iterator[tuple[str, str, str, int, int]]:
        if meta_dir is None or not meta_dir.is_dir():
            return
        size = n = 0
        for root, _, files in os.walk(meta_dir):
            for f in files:
                try:
                    st = os.stat(os.path.join(root, f))
                except OSError:
                    continue
                size += st.st_blocks * 512  # belegter Platz (LMDB ist dünn besetzt)
                n += 1
        yield "garage", "Metadaten (LMDB)", OTHER, size, n

    def write(self, rows: Iterable[tuple[str, str, str, int, int]]) -> int:
        rows = list(rows)
        with self.engine.begin() as c:
            measured = c.execute(text("SELECT now()")).scalar_one()
            for store, part, grp, size, objects in rows:
                c.execute(
                    text("""INSERT INTO meta.storage_usage
                                   (measured_at, store, part, grp, bytes, objects)
                            VALUES (:t, :s, :p, :g, :b, :n)"""),
                    {"t": measured, "s": store, "p": part, "g": grp, "b": size, "n": objects},
                )
        return len(rows)


# Chunks von feat.nwp_value mit Gesamtgröße (seit TimescaleDB 2.2x spaltenweise im Chunk selbst)
CHUNKS_SQL = """
SELECT format('%I.%I', chunk_schema, chunk_name) AS chunk, total_bytes AS bytes
  FROM chunks_detailed_size('feat.nwp_value')"""


def measure(
    usage: Usage,
    client: httpx.Client | None,
    meta_dir: Path | None,
    budget_s: float,
    max_age: timedelta,
    parallel: int = 4,
) -> None:
    """@relation(SWR-CORE-006, scope=function)"""
    t0 = time.monotonic()
    usage.pending = False
    scanned = usage.scan_s3(budget_s, max_age, parallel)
    totals = usage.bucket_totals(client)
    rows = [
        *usage.s3_rows(totals),
        *usage.postgres_rows(budget_s),
        *usage.garage_meta_rows(meta_dir),
    ]
    usage.write(rows)
    log.info(
        "storage.measured",
        months_scanned=scanned,
        rows=len(rows),
        pending=usage.pending,
        seconds=round(time.monotonic() - t0, 1),
    )


def main(argv: list[str] | None = None) -> int:
    """@relation(SWR-CORE-006, scope=function)"""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--watch", type=int, default=0, help="alle N Sekunden wiederholen")
    parser.add_argument(
        "--budget",
        type=float,
        default=300.0,
        help="Sekunden fürs Listing (Garage) und Zählen (Postgres) je Durchgang (Standard 300)",
    )
    parser.add_argument(
        "--parallel", type=int, default=4, help="Monate gleichzeitig listen (Standard 4)"
    )
    parser.add_argument(
        "--catch-up",
        type=int,
        default=60,
        help="Pause in Sekunden statt --watch, solange noch Monate oder Chunks offen sind",
    )
    parser.add_argument(
        "--max-age-days",
        type=float,
        default=7.0,
        help="Monate spätestens nach so vielen Tagen neu listen",
    )
    parser.add_argument(
        "--garage-meta-dir",
        type=Path,
        default=None,
        help="eingebundenes Garage-Metadatenverzeichnis (nur lesend)",
    )
    args = parser.parse_args(argv)
    settings = Settings()
    configure_logging(settings.log_level, settings.log_json)
    usage = Usage(create_db_engine(settings), settings)
    admin = GarageAdminSettings()
    token = admin.admin_token.get_secret_value()
    client = (
        httpx.Client(
            base_url=admin.admin_url, timeout=30, headers={"Authorization": f"Bearer {token}"}
        )
        if token
        else None
    )
    max_age = timedelta(days=args.max_age_days)
    while True:
        try:
            measure(usage, client, args.garage_meta_dir, args.budget, max_age, args.parallel)
        except Exception:
            if not args.watch:
                raise
            log.exception("storage.failed")
        if not args.watch:
            return 0
        # Erste Vollmessung (Stunden bei Millionen Objekten) in kurzen Durchgängen, damit das
        # Dashboard zwischendurch schon Teilstände zeigt; danach im normalen Takt
        time.sleep(args.catch_up if usage.pending else args.watch)


if __name__ == "__main__":
    sys.exit(main())
