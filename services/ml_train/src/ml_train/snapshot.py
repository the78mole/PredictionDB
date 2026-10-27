"""Trainings-Snapshot: alle Eingänge eines Trainings als Parquet plus ``manifest.json``.

Der Snapshot ist die einzige Eingabe von Training und Backtest (ARCHITEKTUR.md 7.7). Er wird
auf der VM aus der Datenbank exportiert und kann ohne Datenbankzugang überall verarbeitet
werden (lokal, im LAN oder auf einer Miet-GPU).

========================= ===================================================================
``energy.parquet``        ``ts`` (UTC, Stundenbeginn), ``code``, ``value``, ``published``:
                          Stundenmittel aus ``energy.value_best`` (15-min-Werte werden
                          gemittelt), ``published`` als spätester Wert der Stunde
``weather.parquet``       ``run_time``, ``valid_time``, ``feature``, ``value``: Wetter-Features
                          je Technologie, über die Cluster zusammengefasst (``*_power`` als
                          Summe, alles andere leistungsgewichtet gemittelt), Schritte nativ
``capacity.parquet``      ``day``, ``technology``, ``capacity_mw``: installierte Leistung DE
``manifest.json``         Zeiträume, Clusterung, Zeilenzahlen, SHA-256 je Datei, ``snapshot_id``
========================= ===================================================================
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
from sqlalchemy import Engine, text

from ml_train.jobs import Progress, no_progress
from predictiondb_core.log import get_logger

log = get_logger(__name__)

FILES = ("energy.parquet", "weather.parquet", "capacity.parquet")


@dataclass
class Snapshot:
    energy: pd.DataFrame
    weather: pd.DataFrame
    capacity: pd.DataFrame
    manifest: dict[str, Any] = field(default_factory=dict)

    @property
    def snapshot_id(self) -> str:
        return str(self.manifest.get("snapshot_id", "unsaved"))

    def energy_wide(self) -> pd.DataFrame:
        """Stundenwerte je Reihen-Code als Spalten, Index ``ts`` (UTC, lückenlos)."""
        wide = self.energy.pivot_table(index="ts", columns="code", values="value", aggfunc="mean")
        if wide.empty:
            return wide
        full = pd.date_range(wide.index.min(), wide.index.max(), freq="h", tz="UTC")
        wide = wide.reindex(full)
        wide.index.name = "ts"
        wide.columns.name = None
        return wide

    def capacity_wide(self) -> pd.DataFrame:
        """Installierte Leistung je Technologie (MW) als Spalten, Index Tag (``datetime64``)."""
        if self.capacity.empty:
            return pd.DataFrame()
        wide = self.capacity.pivot_table(
            index="day", columns="technology", values="capacity_mw", aggfunc="sum"
        )
        wide.index = pd.to_datetime(wide.index)
        wide.columns = [f"cap_{c}" for c in wide.columns]
        return wide.sort_index()

    def save(self, out_dir: str | Path) -> str:
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        hashes: dict[str, str] = {}
        for name, df in zip(FILES, (self.energy, self.weather, self.capacity), strict=True):
            path = out / name
            df.to_parquet(path, index=False, compression="zstd")
            hashes[name] = hashlib.sha256(path.read_bytes()).hexdigest()
        sid = hashlib.sha256("".join(hashes[n] for n in FILES).encode()).hexdigest()[:16]
        self.manifest = {
            **self.manifest,
            "files": hashes,
            "rows": {
                "energy": len(self.energy),
                "weather": len(self.weather),
                "capacity": len(self.capacity),
            },
            "snapshot_id": sid,
        }
        (out / "manifest.json").write_text(json.dumps(self.manifest, indent=2, default=str))
        return sid


def load_snapshot(path: str | Path) -> Snapshot:
    p = Path(path)
    manifest = json.loads((p / "manifest.json").read_text())
    for name, digest in manifest.get("files", {}).items():
        actual = hashlib.sha256((p / name).read_bytes()).hexdigest()
        if actual != digest:
            raise ValueError(f"{name}: Prüfsumme passt nicht zum Manifest ({actual[:12]}…)")
    energy = pd.read_parquet(p / "energy.parquet")
    weather = pd.read_parquet(p / "weather.parquet")
    capacity = pd.read_parquet(p / "capacity.parquet")
    for col in ("ts", "published"):
        if col in energy.columns:
            energy[col] = pd.to_datetime(energy[col], utc=True)
    for col in ("run_time", "valid_time"):
        weather[col] = pd.to_datetime(weather[col], utc=True)
    return Snapshot(energy, weather, capacity, manifest)


# --- Export aus der Datenbank -----------------------------------------------------------------


def _months(start: datetime, end: datetime) -> Iterable[tuple[datetime, datetime]]:
    cur = start.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    while cur < end:
        nxt = (cur + timedelta(days=32)).replace(day=1)
        yield max(cur, start), min(nxt, end)
        cur = nxt


ENERGY_SQL = text("""
    SELECT time_bucket('1 hour', ts) AS ts, avg(value) AS value, max(published) AS published
    FROM energy.value_best
    WHERE code = :code AND ts >= :a AND ts < :b
    GROUP BY 1 ORDER BY 1""")

# *_power als Summe über die Cluster, sonst nach Clusterleistung (Gitter-Gewichte) gemittelt
WEATHER_SQL = text("""
    WITH w AS (
      SELECT c.region_id, c.technology, sum(g.capacity_mw) AS cap
      FROM assets.cluster c
      JOIN assets.grid_weight g USING (clustering_version, technology, cluster_no)
      WHERE c.clustering_version = :cv AND g.grid = :grid AND c.region_id IS NOT NULL
      GROUP BY 1, 2)
    SELECT n.run_time, n.valid_time, w.technology || '_' || f.code AS feature,
           CASE WHEN right(f.code, 6) = '_power' THEN sum(n.value)
                ELSE sum(n.value * w.cap) / NULLIF(sum(w.cap), 0) END AS value
    FROM feat.nwp_value n
    JOIN w USING (region_id)
    JOIN meta.feature f USING (feature_id)
    JOIN meta.source s ON s.source_id = f.source_id AND s.code = :source
    WHERE n.run_time >= :a AND n.run_time < :b
      AND extract(hour FROM n.run_time AT TIME ZONE 'UTC') = ANY(:hours)
      AND n.valid_time <= n.run_time + make_interval(hours => :max_lead)
    GROUP BY n.run_time, n.valid_time, w.technology, f.code""")

CAPACITY_SQL = text("""
    SELECT day, technology, sum(capacity_mw) AS capacity_mw
    FROM assets.capacity_daily WHERE clustering_version = :cv
    GROUP BY 1, 2 ORDER BY 1, 2""")


def latest_clustering(engine: Engine) -> str | None:
    with engine.connect() as c:
        v = c.execute(
            text(
                "SELECT clustering_version FROM assets.clustering ORDER BY created_at DESC LIMIT 1"
            )
        ).scalar_one_or_none()
    return None if v is None else str(v)


def export_steps(
    codes: list[str], start: datetime, end: datetime, weather_from: datetime | None = None
) -> int:
    """Zahl der Rückmeldungen von :func:`export_snapshot` (Reihen, Wettermonate, Bestand)."""
    return len(codes) + len(list(_months(max(start, weather_from or start), end))) + 1


def export_snapshot(
    engine: Engine,
    codes: list[str],
    start: datetime,
    end: datetime,
    *,
    weather_from: datetime | None = None,
    run_hours: list[int] | None = None,
    max_lead_h: int = 216,
    clustering_version: str | None = None,
    source: str = "gfs_0p25",
    grid: str = "gfs_0p25",
    progress: Progress = no_progress,
) -> Snapshot:
    """Liest Energie, Wetter-Features und Bestand für ``[start, end)`` aus der Datenbank."""
    cv = clustering_version or latest_clustering(engine)
    hours = run_hours or [0, 12]
    energy_parts: list[pd.DataFrame] = []
    weather_parts: list[pd.DataFrame] = []
    with engine.connect() as c:
        for i, code in enumerate(codes):
            progress(f"Energie {i + 1}/{len(codes)} · {code}")
            df = pd.read_sql(ENERGY_SQL, c, params={"code": code, "a": start, "b": end})
            df["code"] = code
            energy_parts.append(df)
            log.info("snapshot.energy", code=code, rows=len(df))
        if cv is not None:
            months = list(_months(max(start, weather_from or start), end))
            for j, (a, b) in enumerate(months):
                progress(f"Wetter {j + 1}/{len(months)} · {a:%Y-%m}")
                df = pd.read_sql(
                    WEATHER_SQL,
                    c,
                    params={
                        "cv": cv,
                        "grid": grid,
                        "source": source,
                        "a": a,
                        "b": b,
                        "hours": hours,
                        "max_lead": max_lead_h,
                    },
                )
                weather_parts.append(df)
                log.info("snapshot.weather", month=a.strftime("%Y-%m"), rows=len(df))
            progress("Bestand")
            capacity = pd.read_sql(CAPACITY_SQL, c, params={"cv": cv})
        else:
            capacity = pd.DataFrame(columns=["day", "technology", "capacity_mw"])
    energy = pd.concat(energy_parts, ignore_index=True)
    energy["ts"] = pd.to_datetime(energy["ts"], utc=True)
    energy["published"] = pd.to_datetime(energy["published"], utc=True)
    weather = (
        pd.concat(weather_parts, ignore_index=True)
        if weather_parts
        else pd.DataFrame(columns=["run_time", "valid_time", "feature", "value"])
    )
    for col in ("run_time", "valid_time"):
        weather[col] = pd.to_datetime(weather[col], utc=True)
    weather["value"] = weather["value"].astype("float32")
    capacity["day"] = pd.to_datetime(capacity["day"]).dt.date
    manifest = {
        "created": datetime.now(UTC).isoformat(),
        "range": [start.isoformat(), end.isoformat()],
        "codes": codes,
        "clustering_version": cv,
        "weather": {"source": source, "grid": grid, "run_hours": hours, "max_lead_h": max_lead_h},
    }
    return Snapshot(energy, weather, capacity, manifest)


def capacity_days(snapshot: Snapshot) -> tuple[date, date] | None:
    if snapshot.capacity.empty:
        return None
    days = pd.to_datetime(snapshot.capacity["day"])
    return days.min().date(), days.max().date()
