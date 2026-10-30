"""Speicherbelegung je Wettergruppe in Garage und PostgreSQL (``meta.storage_usage``).

Grafana sieht Garage nicht. ``wx-process storage`` listet daher ``wx-bronze`` und ``wx-silver``
vollständig und summiert Größe und Zahl der Objekte nach den ersten beiden Pfadteilen
(``modell/auflösung``): ``gfs/0p25`` ist GFS DE, ``gfs/0p5`` GFS Europa, ``gefs/0p5`` GEFS, alles
andere zählt als ``sonstige``. Für PostgreSQL wird ``hypertable_size('feat.nwp_value')`` nach dem
Anteil der Gruppe an den Feature-Werten aufgeteilt (je Lauf die jüngste Verarbeitung in
``meta.nwp_feature_run``); die Chunks mischen alle Quellen, eine genaue Größe je Quelle gibt
TimescaleDB nicht her.

@relation(SWA-WXP-STORAGE, scope=file)
"""

from __future__ import annotations

import time
from collections import defaultdict
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import NamedTuple

import obstore
from obstore.store import ObjectStore
from sqlalchemy import Engine, text

from predictiondb_core.log import get_logger

log = get_logger(__name__)

#: Präfix ``modell/auflösung`` in Bronze und Silber → Code der Quelle in ``meta.source``
GROUPS = {"gfs/0p25": "gfs_0p25", "gfs/0p5": "gfs_0p5", "gefs/0p5": "gefs_0p5"}
OTHER = "sonstige"


class Usage(NamedTuple):
    store: str
    grp: str
    bytes: int
    objects: int


def group_of(path: str) -> str:
    """Gruppe eines Objekts nach seinen ersten beiden Pfadteilen."""
    parts = path.split("/", 2)
    return GROUPS.get("/".join(parts[:2]), OTHER) if len(parts) > 2 else OTHER


def measure_bucket(name: str, store: ObjectStore) -> list[Usage]:
    """Größe und Zahl der Objekte je Gruppe in einem Bucket (eine volle Auflistung).

    @relation(SWR-WXP-050, scope=function)
    """
    sums: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    t0 = time.monotonic()
    for batch in obstore.list(store, chunk_size=1000):
        for meta in batch:
            s = sums[group_of(meta["path"])]
            s[0] += int(meta["size"])
            s[1] += 1
    log.info("storage.bucket", store=name, seconds=round(time.monotonic() - t0, 1))
    return [Usage(name, g, b, n) for g, (b, n) in sorted(sums.items())]


def measure_postgres(engine: Engine) -> list[Usage]:
    """Anteil je Quelle an ``feat.nwp_value`` (Bytes geschätzt, ``objects`` = Werte).

    @relation(SWR-WXP-050, scope=function)
    """
    with engine.connect() as c:
        rows = c.execute(
            text("""
                WITH runs AS (
                  SELECT DISTINCT ON (source_id, run_time) source_id, n_values
                    FROM meta.nwp_feature_run
                   ORDER BY source_id, run_time, processed_at DESC),
                per AS (SELECT source_id, sum(n_values)::bigint AS n FROM runs GROUP BY 1)
                SELECT s.code, per.n,
                       (hypertable_size('feat.nwp_value')::numeric * per.n
                          / nullif(sum(per.n) OVER (), 0))::bigint
                  FROM per JOIN meta.source s USING (source_id)""")
        ).all()
    return [Usage("postgres", code, int(b or 0), int(n)) for code, n, b in rows]


def record(engine: Engine, rows: list[Usage], at: datetime | None = None) -> datetime:
    """Eine Messung in ``meta.storage_usage`` ablegen.

    @relation(SWR-WXP-050, scope=function)
    """
    at = at or datetime.now(UTC)
    with engine.begin() as c:
        c.execute(
            text("""INSERT INTO meta.storage_usage (measured_at, store, grp, bytes, objects)
                    VALUES (:at, :store, :grp, :bytes, :objects)"""),
            [{"at": at, **r._asdict()} for r in rows],
        )
    return at


def measure(engine: Engine, buckets: Mapping[str, ObjectStore]) -> list[Usage]:
    """Alle Ablagen messen und ablegen; ``buckets`` bildet ``bronze``/``silver`` auf Stores ab.

    @relation(SWR-WXP-050, scope=function)
    """
    rows = measure_postgres(engine)
    for name, store in buckets.items():
        rows += measure_bucket(name, store)
    record(engine, rows)
    return rows
