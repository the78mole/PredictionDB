"""Datenbankzugriff für das Schema ``assets`` (Revision ``0002_assets``).

Massendaten (Einheiten, Tagesbestand) gehen per ``COPY`` hinein; Verortung und Aggregation
auf Gitterzellen laufen als SQL in der Datenbank, nur die kleinen Zellsummen kommen für das
Clustering nach Python. Gitterzellen werden über ganzzahlige Indizes ``floor(x / res + 0.5)``
verglichen, damit es keine Gleitkomma-Gleichheit zwischen ``real`` und ``double`` braucht.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from dataclasses import astuple, fields
from datetime import date
from typing import Any, cast

import numpy as np
from numpy.typing import NDArray
from psycopg import Connection as PgConnection
from sqlalchemy import Connection, Engine, text

from mastr_ingest.units import GridConnection, Unit

UNIT_COLUMNS = [f.name for f in fields(Unit)]

#: Einheit ist am Tag :d in Betrieb
ACTIVE_AT = """
    u.commissioned <= :d
    AND (u.decommissioned IS NULL OR u.decommissioned > :d)
    AND NOT (u.paused_from IS NOT NULL AND u.paused_from <= :d
             AND (u.paused_until IS NULL OR u.paused_until > :d))
"""


def _idx(col: str, res_param: str = ":res") -> str:
    return f"floor({col}::float8 / {res_param} + 0.5)::int"


class Store:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    # --- Snapshots -------------------------------------------------------------------------

    def snapshot(self, export_date: date) -> dict[str, Any] | None:
        with self.engine.connect() as c:
            row = (
                c.execute(
                    text("SELECT * FROM assets.snapshot WHERE export_date = :d"), {"d": export_date}
                )
                .mappings()
                .first()
            )
        return dict(row) if row else None

    def upsert_snapshot(
        self,
        export_date: date,
        version: str,
        object_key: str,
        sha256: str,
        size: int,
        status: str = "stored",
    ) -> int:
        with self.engine.begin() as c:
            sid: int = c.execute(
                text("""
                    INSERT INTO assets.snapshot
                      (export_date, version, object_key, sha256, size_bytes, status)
                    VALUES (:d, :v, :k, :s, :n, :st)
                    ON CONFLICT (export_date) DO UPDATE SET
                      version = EXCLUDED.version, object_key = EXCLUDED.object_key,
                      sha256 = EXCLUDED.sha256, size_bytes = EXCLUDED.size_bytes,
                      status = CASE WHEN assets.snapshot.status = 'imported'
                                    THEN 'imported' ELSE EXCLUDED.status END,
                      updated_at = now()
                    RETURNING snapshot_id"""),
                {
                    "d": export_date,
                    "v": version,
                    "k": object_key,
                    "s": sha256,
                    "n": size,
                    "st": status,
                },
            ).scalar_one()
        return sid

    def set_snapshot_status(
        self,
        snapshot_id: int,
        status: str,
        n_units: int | None = None,
        stats: dict[str, Any] | None = None,
    ) -> None:
        with self.engine.begin() as c:
            c.execute(
                text("""
                    UPDATE assets.snapshot SET status = :st,
                      n_units = COALESCE(:n, n_units),
                      stats = COALESCE(CAST(:stats AS jsonb), stats), updated_at = now()
                    WHERE snapshot_id = :id"""),
                {
                    "st": status,
                    "n": n_units,
                    "id": snapshot_id,
                    "stats": None if stats is None else json.dumps(stats, default=str),
                },
            )

    def latest_imported(self, before: date | None = None) -> dict[str, Any] | None:
        with self.engine.connect() as c:
            row = (
                c.execute(
                    text("""
                    SELECT * FROM assets.snapshot
                    WHERE status IN ('imported', 'pruned')
                      AND (CAST(:b AS date) IS NULL OR export_date < :b)
                    ORDER BY export_date DESC LIMIT 1"""),
                    {"b": before},
                )
                .mappings()
                .first()
            )
        return dict(row) if row else None

    def has_snapshot_between(self, start: date, end: date) -> bool:
        with self.engine.connect() as c:
            return bool(
                c.execute(
                    text("""
                        SELECT EXISTS (SELECT 1 FROM assets.snapshot
                        WHERE status IN ('imported', 'pruned')
                          AND export_date BETWEEN :a AND :b)"""),
                    {"a": start, "b": end},
                ).scalar_one()
            )

    # --- Einheiten -------------------------------------------------------------------------

    def load_units(self, snapshot_id: int, units: Iterable[Unit]) -> int:
        """Ersetzt die Einheiten eines Snapshots (idempotent bei wiederholtem Import)."""
        cols = ", ".join(["snapshot_id", *UNIT_COLUMNS])
        n = 0
        with self.engine.begin() as c:
            c.execute(text("DELETE FROM assets.unit WHERE snapshot_id = :id"), {"id": snapshot_id})
            pg = cast(PgConnection[Any], c.connection.driver_connection)
            with pg.cursor().copy(f"COPY assets.unit ({cols}) FROM STDIN") as copy:
                for u in units:
                    copy.write_row((snapshot_id, *astuple(u)))
                    n += 1
        return n

    def geolocate(self, snapshot_id: int, sea_areas: dict[str, tuple[float, float]]) -> None:
        """Setzt ``lat``/``lon``/``geo_precision`` für alle Einheiten des Snapshots.

        Reihenfolge: eigene Koordinaten → Seegebiet (Offshore) → Gemeinde (Referenztabelle
        ``assets.gemeinde``, sonst Mittel der Einheiten mit Koordinaten derselben Gemeinde) →
        Postleitzahl → Kreis (AGS-Präfix) → Bundesland.
        """
        p = {"id": snapshot_id}
        steps = [
            # vertauschte Koordinaten korrigieren, Unplausibles verwerfen
            """UPDATE assets.unit SET lat = lon, lon = lat WHERE snapshot_id = :id
               AND lat BETWEEN 3 AND 16 AND lon BETWEEN 46 AND 56""",
            """UPDATE assets.unit SET lat = NULL, lon = NULL WHERE snapshot_id = :id
               AND NOT (lat BETWEEN 46 AND 56 AND lon BETWEEN 3 AND 16)""",
            """UPDATE assets.unit SET geo_precision = CASE WHEN lat IS NULL THEN NULL
               ELSE 'coord' END WHERE snapshot_id = :id""",
        ]
        with self.engine.begin() as c:
            for sql in steps:
                c.execute(text(sql), p)
            for area, (lat, lon) in sea_areas.items():
                c.execute(
                    text("""
                    UPDATE assets.unit SET lat = :lat, lon = :lon, geo_precision = 'seegebiet'
                    WHERE snapshot_id = :id AND lat IS NULL AND technology = 'wind_off'
                      AND sea_area = :area"""),
                    {**p, "lat": lat, "lon": lon, "area": area},
                )
            c.execute(
                text("""
                UPDATE assets.unit u SET lat = g.lat, lon = g.lon, geo_precision = 'gemeinde'
                FROM assets.gemeinde g
                WHERE u.snapshot_id = :id AND u.lat IS NULL AND u.ags = g.ags"""),
                p,
            )
            for precision, ref_key, unit_key in (
                ("gemeinde", "ags", "u.ags"),
                ("plz", "plz", "u.plz"),
                ("kreis", "left(ags, 5)", "left(u.ags, 5)"),
                ("land", "bundesland", "u.bundesland"),
            ):
                c.execute(
                    text(f"""
                    WITH ref AS (
                      SELECT {ref_key} AS k, avg(lat) AS lat, avg(lon) AS lon
                      FROM assets.unit
                      WHERE snapshot_id = :id AND geo_precision = 'coord'
                        AND {ref_key} IS NOT NULL
                      GROUP BY 1)
                    UPDATE assets.unit u
                    SET lat = ref.lat, lon = ref.lon, geo_precision = '{precision}'
                    FROM ref
                    WHERE u.snapshot_id = :id AND u.lat IS NULL AND {unit_key} = ref.k"""),
                    p,
                )

    def assign_zones(self, snapshot_id: int, connections: Iterable[GridConnection]) -> int:
        """Setzt ``regelzone``/``netzbetreiber_nr``/``regelzone_source`` aller Einheiten.

        Direkt über den Netzanschlusspunkt der Lokation (bei mehreren der Lokation der erste mit
        Regelzone). Einheiten ohne Netzanschlusspunkt übernehmen die nach Leistung häufigste
        Regelzone direkt zugeordneter Einheiten derselben Gemeinde → PLZ → Kreis → Seegebiet →
        Bundesland. Gibt die Zahl der gelesenen Netzanschlusspunkte zurück.
        """
        p = {"id": snapshot_id}
        n = 0
        with self.engine.begin() as c:
            c.execute(
                text("""CREATE TEMP TABLE nap (lokation_nr text, regelzone text,
                        netzbetreiber_nr text) ON COMMIT DROP""")
            )
            pg = cast(PgConnection[Any], c.connection.driver_connection)
            with pg.cursor().copy("COPY nap FROM STDIN") as copy:
                for gc in connections:
                    copy.write_row((gc.lokation_nr, gc.regelzone, gc.netzbetreiber_nr))
                    n += 1
            c.execute(
                text("""UPDATE assets.unit SET regelzone = NULL, netzbetreiber_nr = NULL,
                        regelzone_source = NULL WHERE snapshot_id = :id
                        AND (regelzone_source IS NOT NULL OR netzbetreiber_nr IS NOT NULL)"""),
                p,
            )
            c.execute(text("ANALYZE nap"))
            c.execute(
                text("""
                UPDATE assets.unit u
                SET regelzone = n.regelzone, netzbetreiber_nr = n.netzbetreiber_nr,
                    regelzone_source = CASE WHEN n.regelzone IS NOT NULL THEN 'nap' END
                FROM (SELECT DISTINCT ON (lokation_nr) * FROM nap
                      ORDER BY lokation_nr, regelzone NULLS LAST, netzbetreiber_nr) n
                WHERE u.snapshot_id = :id AND u.lokation_nr = n.lokation_nr"""),
                p,
            )
            for source, key, unit_key in (
                ("gemeinde", "ags", "u.ags"),
                ("plz", "plz", "u.plz"),
                ("kreis", "left(ags, 5)", "left(u.ags, 5)"),
                ("seegebiet", "sea_area", "u.sea_area"),
                ("land", "bundesland", "u.bundesland"),
            ):
                c.execute(
                    text(f"""
                    WITH ref AS (
                      SELECT DISTINCT ON (k) k, regelzone
                      FROM (SELECT {key} AS k, regelzone, sum(capacity_kw) AS kw
                            FROM assets.unit
                            WHERE snapshot_id = :id AND regelzone_source = 'nap'
                              AND {key} IS NOT NULL
                            GROUP BY 1, 2) s
                      ORDER BY k, kw DESC, regelzone)
                    UPDATE assets.unit u SET regelzone = ref.regelzone,
                                             regelzone_source = '{source}'
                    FROM ref
                    WHERE u.snapshot_id = :id AND u.regelzone IS NULL
                      AND {unit_key} = ref.k"""),
                    p,
                )
        return n

    def unit_stats(self, snapshot_id: int, export_date: date) -> dict[str, Any]:
        """Kennzahlen je Technologie: Anzahl, aktive Leistung, Verortungsgüte."""
        with self.engine.connect() as c:
            rows = (
                c.execute(
                    text(f"""
                SELECT technology, count(*) AS n,
                       sum(capacity_kw) FILTER (WHERE {ACTIVE_AT}) / 1000 AS active_mw,
                       count(*) FILTER (WHERE {ACTIVE_AT}) AS n_active
                FROM assets.unit u WHERE snapshot_id = :id GROUP BY technology"""),
                    {"id": snapshot_id, "d": export_date},
                )
                .mappings()
                .all()
            )
            geo = c.execute(
                text(f"""
                SELECT technology, COALESCE(geo_precision, 'none') AS p,
                       round((sum(capacity_kw) / 1000)::numeric, 1) AS mw
                FROM assets.unit u WHERE snapshot_id = :id AND {ACTIVE_AT}
                GROUP BY 1, 2"""),
                {"id": snapshot_id, "d": export_date},
            ).all()
        out: dict[str, Any] = {
            r["technology"]: {
                "n": r["n"],
                "n_active": r["n_active"],
                "active_mw": round(float(r["active_mw"] or 0), 1),
                "geo_mw": {},
            }
            for r in rows
        }
        for tech, prec, mw in geo:
            out[tech]["geo_mw"][prec] = float(mw)
        return out

    def zone_stats(self, snapshot_id: int, export_date: date) -> dict[str, Any]:
        """Aktive Leistung (MW) je Technologie, Regelzone und Herkunft der Zuordnung."""
        with self.engine.connect() as c:
            rows = c.execute(
                text(f"""
                SELECT technology, COALESCE(regelzone, 'unbekannt'),
                       COALESCE(regelzone_source, 'none'),
                       round((sum(capacity_kw) / 1000)::numeric, 1)
                FROM assets.unit u WHERE snapshot_id = :id AND {ACTIVE_AT}
                GROUP BY 1, 2, 3"""),
                {"id": snapshot_id, "d": export_date},
            ).all()
        out: dict[str, Any] = {}
        for tech, zone, source, mw in rows:
            t = out.setdefault(tech, {"zone_mw": {}, "source_mw": {}})
            t["zone_mw"][zone] = round(t["zone_mw"].get(zone, 0.0) + float(mw), 1)
            t["source_mw"][source] = round(t["source_mw"].get(source, 0.0) + float(mw), 1)
        return out

    def snapshot_diff(self, new_id: int, old_id: int, old_date: date) -> dict[str, Any]:
        """Unterschiede zum Vorgänger: neue, entfallene und nachgemeldete Einheiten."""
        with self.engine.connect() as c:
            rows = (
                c.execute(
                    text("""
                SELECT COALESCE(n.technology, o.technology) AS technology,
                  count(*) FILTER (WHERE o.mastr_nr IS NULL) AS n_new,
                  round((COALESCE(sum(n.capacity_kw) FILTER (WHERE o.mastr_nr IS NULL), 0)
                         / 1000)::numeric, 1) AS new_mw,
                  count(*) FILTER (WHERE o.mastr_nr IS NULL AND n.commissioned < :od)
                    AS n_late,
                  round((COALESCE(sum(n.capacity_kw) FILTER (
                    WHERE o.mastr_nr IS NULL AND n.commissioned < :od), 0) / 1000)::numeric, 1)
                    AS late_mw,
                  count(*) FILTER (WHERE n.mastr_nr IS NULL) AS n_gone,
                  count(*) FILTER (WHERE n.mastr_nr IS NOT NULL AND o.mastr_nr IS NOT NULL
                    AND n.commissioned IS DISTINCT FROM o.commissioned) AS n_date_changed
                FROM (SELECT * FROM assets.unit WHERE snapshot_id = :new) n
                FULL JOIN (SELECT * FROM assets.unit WHERE snapshot_id = :old) o
                  USING (mastr_nr)
                GROUP BY 1"""),
                    {"new": new_id, "old": old_id, "od": old_date},
                )
                .mappings()
                .all()
            )
        return {
            r["technology"]: {
                k: (float(v) if k.endswith("_mw") else v) for k, v in r.items() if k != "technology"
            }
            for r in rows
        }

    def prune(self, keep: int) -> list[int]:
        """Löscht Einheiten älterer Snapshots; die Snapshot-Zeile und das ZIP bleiben."""
        with self.engine.begin() as c:
            ids = list(
                c.execute(
                    text("""
                SELECT snapshot_id FROM assets.snapshot WHERE status = 'imported'
                ORDER BY export_date DESC OFFSET :k"""),
                    {"k": keep},
                ).scalars()
            )
            for sid in ids:
                c.execute(text("DELETE FROM assets.unit WHERE snapshot_id = :id"), {"id": sid})
                c.execute(
                    text("""UPDATE assets.snapshot SET status = 'pruned', updated_at = now()
                                  WHERE snapshot_id = :id"""),
                    {"id": sid},
                )
        return ids

    # --- Gitterzellen und Cluster ----------------------------------------------------------

    def cell_capacity(
        self, snapshot_id: int, technology: str, res: float, at: date
    ) -> tuple[NDArray[np.int64], NDArray[np.int64], NDArray[np.float64]]:
        """Zellindizes aller Zellen mit Einheiten und deren aktive Leistung (kW) am Tag ``at``."""
        with self.engine.connect() as c:
            rows = c.execute(
                text(f"""
                SELECT {_idx("lat")} AS i, {_idx("lon")} AS j,
                       COALESCE(sum(capacity_kw) FILTER (WHERE {ACTIVE_AT}), 0) AS kw
                FROM assets.unit u
                WHERE snapshot_id = :id AND technology = :t AND lat IS NOT NULL
                GROUP BY 1, 2 ORDER BY 1, 2"""),
                {"id": snapshot_id, "t": technology, "res": res, "d": at},
            ).all()
        if not rows:
            return np.zeros(0, np.int64), np.zeros(0, np.int64), np.zeros(0)
        arr = np.array(rows, dtype=float)
        return arr[:, 0].astype(np.int64), arr[:, 1].astype(np.int64), arr[:, 2]

    def latest_clustering(self) -> dict[str, Any] | None:
        with self.engine.connect() as c:
            row = (
                c.execute(
                    text(
                        "SELECT c.*, s.export_date FROM assets.clustering c "
                        "LEFT JOIN assets.snapshot s USING (snapshot_id) "
                        "ORDER BY c.created_at DESC LIMIT 1"
                    )
                )
                .mappings()
                .first()
            )
        return dict(row) if row else None

    def clustering_versions(self) -> list[str]:
        with self.engine.connect() as c:
            return list(
                c.execute(text("SELECT clustering_version FROM assets.clustering")).scalars()
            )

    def create_clustering(
        self, version: str, snapshot_id: int, grid: str, res: float, params: dict[str, Any]
    ) -> None:
        with self.engine.begin() as c:
            c.execute(
                text("""
                INSERT INTO assets.clustering (clustering_version, snapshot_id, grid, grid_res,
                                               params)
                VALUES (:v, :s, :g, :r, CAST(:p AS jsonb))"""),
                {"v": version, "s": snapshot_id, "g": grid, "r": res, "p": json.dumps(params)},
            )

    def write_clusters(
        self,
        version: str,
        technology: str,
        centroids: Iterable[tuple[float, float]],
        cells: Iterable[tuple[float, float, int]],
    ) -> None:
        """Cluster mit Region-Code ``<technologie>:<version>:cNN`` und Zellzuordnung anlegen."""
        with self.engine.begin() as c:
            for no, (lat, lon) in enumerate(centroids):
                code = f"{technology}:{version}:c{no:02d}"
                region_id = c.execute(
                    text("""
                    INSERT INTO meta.region (code) VALUES (:c)
                    ON CONFLICT (code) DO UPDATE SET code = EXCLUDED.code
                    RETURNING region_id"""),
                    {"c": code},
                ).scalar_one()
                c.execute(
                    text("""
                    INSERT INTO assets.cluster VALUES (:v, :t, :n, :lat, :lon, :r)"""),
                    {
                        "v": version,
                        "t": technology,
                        "n": no,
                        "lat": lat,
                        "lon": lon,
                        "r": region_id,
                    },
                )
            self._insert_cells(c, version, technology, cells)

    @staticmethod
    def _insert_cells(
        c: Connection, version: str, technology: str, cells: Iterable[tuple[float, float, int]]
    ) -> None:
        rows = [
            {"v": version, "t": technology, "lat": lat, "lon": lon, "n": n} for lat, lon, n in cells
        ]
        if rows:
            c.execute(
                text("""
                INSERT INTO assets.cluster_cell VALUES (:v, :t, :lat, :lon, :n)
                ON CONFLICT DO NOTHING"""),
                rows,
            )

    def add_cells(
        self, version: str, technology: str, cells: Iterable[tuple[float, float, int]]
    ) -> None:
        with self.engine.begin() as c:
            self._insert_cells(c, version, technology, cells)

    def clusters(self, version: str, technology: str) -> list[tuple[int, float, float]]:
        with self.engine.connect() as c:
            rows = c.execute(
                text("""
                SELECT cluster_no, centroid_lat, centroid_lon FROM assets.cluster
                WHERE clustering_version = :v AND technology = :t ORDER BY cluster_no"""),
                {"v": version, "t": technology},
            ).all()
        return [(int(n), float(lat), float(lon)) for n, lat, lon in rows]

    def known_cells(self, version: str, technology: str, res: float) -> set[tuple[int, int]]:
        with self.engine.connect() as c:
            return {
                (int(i), int(j))
                for i, j in c.execute(
                    text(f"""
                SELECT {_idx("lat")}, {_idx("lon")} FROM assets.cluster_cell
                WHERE clustering_version = :v AND technology = :t"""),
                    {"v": version, "t": technology, "res": res},
                ).all()
            }

    # Einheit → Cluster über ihre Zelle im Clustergitter
    _UNIT_CLUSTER = f"""
        FROM assets.unit u
        JOIN assets.cluster_cell cc
          ON cc.clustering_version = :v AND cc.technology = u.technology
         AND {_idx("cc.lat")} = {_idx("u.lat")} AND {_idx("cc.lon")} = {_idx("u.lon")}
        WHERE u.snapshot_id = :id AND u.lat IS NOT NULL
    """

    def write_grid_weights(
        self, version: str, snapshot_id: int, res: float, grid: str, wres: float, at: date
    ) -> int:
        """Leistung je Zelle des Gitters ``grid`` (Auflösung ``wres``) und Cluster am Tag ``at``."""
        with self.engine.begin() as c:
            c.execute(
                text("""DELETE FROM assets.grid_weight
                              WHERE clustering_version = :v AND grid = :g"""),
                {"v": version, "g": grid},
            )
            n = c.execute(
                text(f"""
                INSERT INTO assets.grid_weight
                SELECT :v, technology, cluster_no, :g, wi * :wres, wj * :wres, mw,
                       mw / NULLIF(sum(mw) OVER (PARTITION BY technology, cluster_no), 0), :id
                FROM (
                  SELECT u.technology, cc.cluster_no,
                         {_idx("u.lat", ":wres")} AS wi, {_idx("u.lon", ":wres")} AS wj,
                         sum(u.capacity_kw) / 1000 AS mw
                  {self._UNIT_CLUSTER} AND {ACTIVE_AT}
                  GROUP BY 1, 2, 3, 4) s"""),
                {"v": version, "id": snapshot_id, "res": res, "g": grid, "wres": wres, "d": at},
            ).rowcount
        return int(n)

    def write_zone_weights(self, version: str, snapshot_id: int, res: float, at: date) -> int:
        """Leistung je Cluster, Regelzone und Zelle des Clustergitters am Tag ``at``."""
        with self.engine.begin() as c:
            c.execute(
                text("DELETE FROM assets.zone_weight WHERE clustering_version = :v"), {"v": version}
            )
            n = c.execute(
                text(f"""
                INSERT INTO assets.zone_weight
                SELECT :v, u.technology, cc.cluster_no, COALESCE(u.regelzone, 'unbekannt'),
                       min(cc.lat), min(cc.lon), sum(u.capacity_kw) / 1000, :id
                {self._UNIT_CLUSTER} AND {ACTIVE_AT}
                GROUP BY u.technology, cc.cluster_no, COALESCE(u.regelzone, 'unbekannt'),
                         {_idx("u.lat")}, {_idx("u.lon")}"""),
                {"v": version, "id": snapshot_id, "res": res, "d": at},
            ).rowcount
        return int(n)

    def capacity_events(
        self, version: str, snapshot_id: int, res: float
    ) -> Iterator[tuple[str, int, date, float]]:
        """Zu- und Abgänge ``(Technologie, Cluster, Tag, ΔkW)`` aller verorteten Einheiten."""
        with self.engine.connect() as c:
            yield from c.execute(
                text(f"""
                WITH m AS (SELECT u.*, cc.cluster_no {self._UNIT_CLUSTER}
                           AND u.commissioned IS NOT NULL),
                ev AS (
                  SELECT technology, cluster_no, commissioned AS day, capacity_kw AS kw FROM m
                  UNION ALL
                  SELECT technology, cluster_no, decommissioned, -capacity_kw FROM m
                    WHERE decommissioned IS NOT NULL AND decommissioned > commissioned
                  UNION ALL
                  SELECT technology, cluster_no, paused_from, -capacity_kw FROM m
                    WHERE paused_from IS NOT NULL AND paused_from >= commissioned
                      AND (decommissioned IS NULL OR paused_from < decommissioned)
                  UNION ALL
                  SELECT technology, cluster_no, paused_until, capacity_kw FROM m
                    WHERE paused_from IS NOT NULL AND paused_from >= commissioned
                      AND paused_until IS NOT NULL AND paused_until > paused_from
                      AND (decommissioned IS NULL OR paused_until < decommissioned))
                SELECT technology, cluster_no, day, sum(kw) FROM ev GROUP BY 1, 2, 3
                ORDER BY 1, 2, 3"""),
                {"v": version, "id": snapshot_id, "res": res},
            )

    def write_capacity_daily(
        self,
        version: str,
        snapshot_id: int,
        series: Iterable[tuple[str, int, date, NDArray[np.float64]]],
    ) -> int:
        """Ersetzt den Tagesbestand einer Clusterung: je Cluster eine Reihe ab Starttag (MW)."""
        n = 0
        with self.engine.begin() as c:
            c.execute(
                text("DELETE FROM assets.capacity_daily WHERE clustering_version = :v"),
                {"v": version},
            )
            pg = cast(PgConnection[Any], c.connection.driver_connection)
            with pg.cursor().copy(
                "COPY assets.capacity_daily (clustering_version, technology, cluster_no, day, "
                "capacity_mw, snapshot_id) FROM STDIN"
            ) as copy:
                for technology, cluster_no, start, values in series:
                    for offset, mw in enumerate(values.tolist()):
                        copy.write_row(
                            (
                                version,
                                technology,
                                cluster_no,
                                date.fromordinal(start.toordinal() + offset),
                                mw,
                                snapshot_id,
                            )
                        )
                        n += 1
        return n
