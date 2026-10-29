"""GEFS auf der VM: aktueller Monat live mit Bronze, Historie aus GHCR, Gold, Aufräumen.

Aufteilung (ARCHITEKTUR.md 5.6): Abgeschlossene Monate baut GitHub Actions, die VM importiert
sie aus GHCR. Den aktuellen Monat baut die VM selbst mit demselben Code (``GefsPublisher``) und
schiebt das Silber nach GHCR. Dabei hält sie die geladenen Nachrichten als Bronze, auf den
Ausschnitt zugeschnitten, und löscht sie erst nach ``bronze_days``, wenn das Silber des Laufs
lokal und in GHCR mit gleichem Inhalt liegt.

Ablage:

=========================================== =================================================
``wx-bronze/gefs/0p5/JJJJ/MM/TT/HH/<m>.fSSS.grib2``  zugeschnittene Nachrichten je Mitglied-Schritt
``wx-bronze/gefs/0p5/JJJJ/MM/TT/HH/run.json``        Lauf vollständig abgelegt (Zahlen)
``wx-silver/gefs/0p5/JJJJ/MM/TT/HH.tar.gz``          Silber des Laufs, byte-gleich mit dem Layer
``wx-silver/gefs/0p5/JJJJ/MM/TT/HH.json``            Digest, Inhalts-Hash, Fingerabdruck, Ursprung
=========================================== =================================================

Gold: je Region, Größe und Schritt Mittel, Streuung, P10 und P90 über die Mitglieder nach
``feat.nwp_value`` (Merkmal ``<größe>_<kennzahl>``, Aggregation ``ensemble``, Region
``gefs:<code>``), Stand in ``meta.nwp_feature_run`` (Version der Regionen, ``GOLD_VERSION``).
"""

from __future__ import annotations

import asyncio
import json
import threading
import warnings
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import httpx
import numpy as np
import obstore
from obstore.store import ObjectStore
from sqlalchemy import text

from gribkit.bbox import BBox as GribBBox
from gribkit.grib import crop_message
from predictiondb_core.log import get_logger
from wx_exchange.artifact import month_bounds, tag
from wx_exchange.gefs import (
    F32,
    VARIABLES,
    BBox,
    BronzeSink,
    GefsConfig,
    GefsMonth,
    GefsPublisher,
    MonthResult,
    Regions,
    RunSilver,
    fingerprint,
    months_between,
    unpack,
)
from wx_exchange.oci import Registry, RegistryError, digest
from wx_process.store import BronzeStatus, Store

log = get_logger(__name__)

PREFIX = "gefs/0p5"
SOURCE = "gefs_0p5"
AGGREGATION = "ensemble"
#: Version der Gold-Kennzahlen; eine Änderung rechnet alle Läufe neu
GOLD_VERSION = "1"
STATS = ("mean", "std", "p10", "p90")
#: weniger gültige Mitglieder: keine Kennzahl für diesen Schritt
MIN_MEMBERS = 10


def run_path(run: datetime) -> str:
    return f"{PREFIX}/{run:%Y/%m/%d/%H}"


def parse_run_path(path: str) -> datetime | None:
    """``gefs/0p5/JJJJ/MM/TT/HH…`` → Lauf."""
    parts = path.removeprefix(PREFIX + "/").split("/")
    if len(parts) < 4:
        return None
    try:
        y, m, d = int(parts[0]), int(parts[1]), int(parts[2])
        h = int(parts[3].split(".")[0])
        return datetime(y, m, d, h, tzinfo=UTC)
    except ValueError:
        return None


# --- Bronze ----------------------------------------------------------------------------------


class GefsBronze(BronzeSink):
    """Nachrichten je Mitglied-Schritt, auf den Ausschnitt zugeschnitten (verlustfrei, CCSDS)."""

    def __init__(self, store: ObjectStore, bbox: BBox) -> None:
        self.store = store
        self.bbox = GribBBox(bbox.north, bbox.south, bbox.west, bbox.east)
        # Meldung je Lauf an das Status-Dashboard (GefsVm setzt sie, wenn es eine Datenbank hat)
        self.on_state: Callable[[datetime, BronzeStatus, int | None], None] | None = None
        self._started: set[datetime] = set()
        self._lock = threading.Lock()

    def put(self, run: datetime, member: str, step: int, messages: dict[str, bytes]) -> int:
        with self._lock:
            first = run not in self._started
            self._started.add(run)
        if first and self.on_state is not None:
            self.on_state(run, "loading", None)
        # CCSDS ist verlustfrei; die NCEP-Packung (complex) quantisiert beim Umpacken neu und
        # verliert bis zu 0,01 Einheiten (gemessen 2026-10-29)
        data = b"".join(
            crop_message(messages[k], self.bbox, "grid_ccsds") for k in sorted(messages)
        )
        obstore.put(self.store, f"{run_path(run)}/{member}.f{step:03d}.grib2", data)
        return len(data)

    def complete(self, run: datetime, stats: dict[str, Any]) -> None:
        body = {"run": run.isoformat(), "stored_at": datetime.now(UTC).isoformat(), **stats}
        obstore.put(self.store, f"{run_path(run)}/run.json", json.dumps(body).encode())
        with self._lock:
            self._started.discard(run)
        if self.on_state is not None:
            self.on_state(run, "present", stats.get("bytes"))

    def runs(self, before: datetime | None = None) -> list[datetime]:
        """Läufe mit Bronze (auch unvollständige), optional nur vor ``before``."""
        out: list[datetime] = []
        level = [PREFIX + "/"]
        for _ in range(4):  # Jahr, Monat, Tag, Stunde
            nxt: list[str] = []
            for prefix in level:
                listing = obstore.list_with_delimiter(self.store, prefix=prefix)
                nxt += [p.rstrip("/") + "/" for p in listing["common_prefixes"]]
            level = nxt
        for prefix in level:
            run = parse_run_path(prefix)
            if run is not None and (before is None or run < before):
                out.append(run)
        return sorted(out)

    def delete(self, run: datetime) -> int:
        n = 0
        for batch in obstore.list(self.store, prefix=run_path(run) + "/"):
            paths = [m["path"] for m in batch]
            obstore.delete(self.store, paths)
            n += len(paths)
        return n


# --- lokales Silber --------------------------------------------------------------------------


Origin = Literal["bronze", "ghcr"]


@dataclass(frozen=True)
class LocalInfo:
    digest: str
    content: str
    fingerprint: str
    origin: Origin
    missing: int = 0


class GefsLocal:
    """Silber je Lauf in ``wx-silver``: derselbe Tar wie der Layer in GHCR."""

    def __init__(self, store: ObjectStore) -> None:
        self.store = store

    def save(self, run: datetime, data: bytes, info: LocalInfo) -> None:
        obstore.put(self.store, run_path(run) + ".tar.gz", data)
        obstore.put(self.store, run_path(run) + ".json", json.dumps(info.__dict__).encode())

    def info(self, run: datetime) -> LocalInfo | None:
        try:
            raw = obstore.get(self.store, run_path(run) + ".json").bytes()
        except FileNotFoundError:
            return None
        return LocalInfo(**json.loads(bytes(raw)))

    def load(self, run: datetime) -> bytes:
        return bytes(obstore.get(self.store, run_path(run) + ".tar.gz").bytes())

    def runs(self) -> dict[datetime, LocalInfo]:
        out: dict[datetime, LocalInfo] = {}
        for batch in obstore.list(self.store, prefix=PREFIX + "/"):
            for m in batch:
                if m["path"].endswith(".json"):
                    run = parse_run_path(m["path"])
                    info = self.info(run) if run is not None else None
                    if run is not None and info is not None:
                        out[run] = info
        return out


# --- Gold ------------------------------------------------------------------------------------


def ensemble_stats(values: F32) -> dict[str, F32]:
    """Kennzahlen über die Mitglieder (Achse 0); NaN, wo zu wenige Mitglieder gültig sind."""
    valid = np.isfinite(values).sum(axis=0)
    with warnings.catch_warnings():
        # Schritte ohne gültiges Mitglied (Mittel und Summen in f000) ergeben NaN
        warnings.simplefilter("ignore", RuntimeWarning)
        out = {
            "mean": np.nanmean(values, axis=0),
            "std": np.nanstd(values, axis=0),
            "p10": np.nanpercentile(values, 10, axis=0),
            "p90": np.nanpercentile(values, 90, axis=0),
        }
    for k in out:
        out[k] = np.where(valid >= MIN_MEMBERS, out[k], np.nan).astype(np.float32)
    return out


class GefsGold:
    """Gold aus lokalem Silber nach ``feat.nwp_value``."""

    def __init__(self, store: Store, local: GefsLocal) -> None:
        self.store = store
        self.local = local
        self.source_id = store.source_id(SOURCE)
        self._features: dict[str, int] | None = None
        self._regions: dict[str, int] = {}

    def done(self, version: str) -> set[datetime]:
        with self.store.engine.connect() as c:
            rows = c.execute(
                text("""
                SELECT run_time FROM meta.nwp_feature_run
                WHERE source_id = :s AND clustering_version = :v AND features_version = :fv"""),
                {"s": self.source_id, "v": version, "fv": GOLD_VERSION},
            ).all()
        return {r[0] for r in rows}

    def _feature_ids(self) -> dict[str, int]:
        if self._features is None:
            units = {f"{v}_{s}": u for v, u in VARIABLES.items() for s in STATS}
            self._features = self.store.feature_ids(self.source_id, AGGREGATION, units)
        return self._features

    def _region_ids(self, codes: list[str]) -> dict[str, int]:
        todo = [c for c in codes if c not in self._regions]
        if todo:
            with self.store.engine.begin() as c:
                for code in todo:
                    self._regions[code] = int(
                        c.execute(
                            text("""
                            INSERT INTO meta.region (code) VALUES (:c)
                            ON CONFLICT (code) DO UPDATE SET code = EXCLUDED.code
                            RETURNING region_id"""),
                            {"c": f"gefs:{code}"},
                        ).scalar_one()
                    )
        return {c: self._regions[c] for c in codes}

    def process(self, run: datetime) -> int:
        data = self.local.load(run)
        meta, values = unpack(data)
        stats = ensemble_stats(values)
        fids = self._feature_ids()
        rids = self._region_ids(list(meta["regions"]))
        steps = [int(s) for s in meta["steps"]]
        valid_times = [run + timedelta(hours=s) for s in steps]
        names = list(meta["variables"])
        rows: list[tuple[int, int, datetime, float]] = []
        for stat, arr in stats.items():  # Schritt x Region x Größe
            si, ri, vi = np.nonzero(np.isfinite(arr))
            for s, r, v in zip(si.tolist(), ri.tolist(), vi.tolist(), strict=True):
                rows.append(
                    (
                        fids[f"{names[v]}_{stat}"],
                        rids[meta["regions"][r]],
                        valid_times[s],
                        float(arr[s, r, v]),
                    )
                )
        replace = self.store.has_features(self.source_id, run)
        n = self.store.write_values(
            run,
            valid_times[-1],
            sorted(fids.values()),
            sorted(rids.values()),
            rows,
            replace=replace,
        )
        self.store.mark_processed(
            self.source_id,
            run,
            str(meta["regions_version"]),
            GOLD_VERSION,
            run_path(run) + ".tar.gz",
            n,
            {"members": len(meta["members"]), "missing": meta.get("missing", 0)},
            {"steps": len(meta["members"]) * len(steps), "bytes": len(data)},
        )
        return n


# --- Betrieb ---------------------------------------------------------------------------------


@dataclass
class CycleResult:
    live: list[dict[str, Any]] = field(default_factory=list)
    imported: Counter[str] = field(default_factory=Counter)
    gold: Counter[str] = field(default_factory=Counter)
    bronze_deleted: int = 0
    bronze_kept: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "live": self.live,
            "imported": dict(self.imported),
            "gold": dict(self.gold),
            "bronze_deleted": self.bronze_deleted,
            "bronze_kept": self.bronze_kept,
        }


def month_start(t: datetime) -> datetime:
    return t.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


class GefsVm:
    def __init__(
        self,
        registry: Registry,
        config: GefsConfig,
        regions: Regions,
        client: httpx.AsyncClient,
        *,
        bronze: GefsBronze,
        local: GefsLocal,
        ledger: Store | None,
        source_url: str,
        history_start: datetime,
        bronze_days: int = 183,
        catch_up_days: int = 3,
        parallel_runs: int = 1,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.registry = registry
        self.config = config
        self.regions = regions
        self.client = client
        self.bronze = bronze
        self.local = local
        self.ledger = ledger
        self.source_url = source_url
        self.history_start = history_start
        self.bronze_days = bronze_days
        self.catch_up_days = catch_up_days
        self.parallel_runs = parallel_runs
        self.now = now
        self.fp = fingerprint(config, regions)
        self.gold = GefsGold(ledger, local) if ledger is not None else None
        self._bronze_synced = False
        if ledger is not None:
            self.bronze.on_state = self._bronze_state

    @property
    def source_id(self) -> int:
        assert self.gold is not None
        return self.gold.source_id

    def _mark(self, runs: list[datetime], origin: Origin) -> None:
        if self.ledger is not None and runs:
            self.ledger.mark_silver(self.source_id, runs, "present", origin)

    def _bronze_state(self, run: datetime, status: BronzeStatus, nbytes: int | None) -> None:
        assert self.ledger is not None
        self.ledger.mark_bronze(self.source_id, [run], status, nbytes)
        if status == "loading":
            # Silber entsteht im selben Durchgang aus diesem Bronze
            self.ledger.mark_silver(self.source_id, [run], "loading", "bronze")

    def sync_bronze(self) -> int:
        """Bronze, das noch nicht gemeldet ist (vor dem Status-Dashboard geladen), nachtragen."""
        if self.ledger is None:
            return 0
        known = self.ledger.bronze_runs(self.source_id)
        new = [r for r in self.bronze.runs() if known.get(r) != "present"]
        self.ledger.mark_bronze(self.source_id, new, "present")
        return len(new)

    # aktueller Monat: selbst bauen, Bronze halten, nach GHCR
    def _packed(self, run: datetime, silver: RunSilver, data: bytes, content: str) -> None:
        info = LocalInfo(digest(data), content, self.fp, "bronze", silver.missing + silver.failed)
        self.local.save(run, data, info)
        self._mark([run], "bronze")

    async def live(self) -> list[dict[str, Any]]:
        now = self.now()
        start = max(self.config.first_run, month_start(now) - timedelta(days=self.catch_up_days))
        publisher = GefsPublisher(
            self.registry,
            self.config,
            self.regions,
            self.client,
            source_url=self.source_url,
            parallel_runs=self.parallel_runs,
            now=now,
            bronze=self.bronze,
            on_packed=self._packed,
        )
        local = self.local.runs()
        out = []
        for month in months_between(start, now):
            a, b = month_bounds(month)
            runs = self.config.runs_between(max(a, start), min(b, now), self.config.hours)
            # schon lokal mit aktuellem Fingerabdruck: nichts zu tun
            if all(r in local and local[r].fingerprint == self.fp for r in runs):
                continue
            for hour in self.config.hours:
                res: MonthResult = await publisher.publish_month(month, hour, start, now)
                out.append(res.as_dict())
        return out

    # abgeschlossene Monate: aus GHCR holen
    def import_history(self, limit: int | None = None) -> Counter[str]:
        """Abgeschlossene Monate aus GHCR holen, höchstens ``limit`` Läufe (danach ``more``)."""
        counts: Counter[str] = Counter()
        end = month_start(self.now()) - timedelta(hours=1)
        local = self.local.runs()
        existing = set(self.registry.tags())
        for month in months_between(max(self.history_start, self.config.first_run), end):
            for hour in self.config.hours:
                t = tag(month, hour)
                if t not in existing:
                    continue
                art = GefsMonth.from_manifest(month, hour, self.registry.manifest(t))
                imported: list[datetime] = []
                for run, layer in sorted(art.layers.items()):
                    if run < self.history_start or layer.fingerprint != self.fp:
                        counts["other_fingerprint" if run >= self.history_start else "early"] += 1
                        continue
                    have = local.get(run)
                    if limit is not None and counts["imported"] >= limit:
                        counts["more"] = 1
                        break
                    if have is not None and have.digest == layer.digest:
                        counts["present"] += 1
                        continue
                    if (
                        have is not None
                        and have.origin == "bronze"
                        and have.content == layer.content
                    ):
                        counts["present"] += 1
                        continue
                    try:
                        data = self.registry.blob(layer.digest)
                    except RegistryError as exc:
                        log.warning("gefs_import_failed", run=f"{run:%Y%m%d%H}", error=str(exc))
                        counts["failed"] += 1
                        continue
                    if digest(data) != layer.digest:
                        counts["failed"] += 1
                        continue
                    info = LocalInfo(layer.digest, layer.content, layer.fingerprint, "ghcr")
                    self.local.save(run, data, info)
                    imported.append(run)
                    counts["imported"] += 1
                self._mark(imported, "ghcr")
                if counts["more"]:
                    return counts
        return counts

    def process_gold(
        self, limit: int | None = None, before: datetime | None = None
    ) -> Counter[str]:
        counts: Counter[str] = Counter()
        if self.gold is None:
            return counts
        done = self.gold.done(self.regions.version)
        local = self.local.runs()
        todo = sorted(
            r
            for r, i in local.items()
            if i.fingerprint == self.fp and r not in done and (before is None or r < before)
        )
        # jüngste zuerst: der Live-Betrieb braucht den neuen Lauf sofort
        for run in todo[::-1][:limit]:
            try:
                n = self.gold.process(run)
            except Exception as exc:  # ein Lauf darf den Durchgang nicht abbrechen
                log.warning("gefs_gold_failed", run=f"{run:%Y%m%d%H}", error=str(exc))
                counts["failed"] += 1
                continue
            log.info("gefs_gold_written", run=f"{run:%Y%m%d%H}", values=n)
            counts["written"] += 1
        return counts

    def clean_bronze(self) -> tuple[int, int]:
        """Bronze älter als ``bronze_days`` löschen, wenn Silber lokal und in GHCR gleich ist."""
        cutoff = self.now() - timedelta(days=self.bronze_days)
        deleted = kept = 0
        manifests: dict[str, GefsMonth] = {}
        for run in self.bronze.runs(before=cutoff):
            month, t = f"{run:%Y-%m}", tag(f"{run:%Y-%m}", run.hour)
            if t not in manifests:
                manifests[t] = GefsMonth.from_manifest(month, run.hour, self.registry.manifest(t))
            layer = manifests[t].layers.get(run)
            info = self.local.info(run)
            if layer is None or info is None or layer.content != info.content:
                kept += 1
                log.warning("gefs_bronze_kept", run=f"{run:%Y%m%d%H}", ghcr=layer is not None)
                continue
            self.bronze.delete(run)
            if self.ledger is not None:
                self.ledger.mark_bronze(self.source_id, [run], "deleted")
            deleted += 1
        return deleted, kept

    async def cycle(
        self,
        *,
        gold_limit: int | None = None,
        history_every: float = 1800,
        gold_every: float = 300,
        import_limit: int | None = 64,
    ) -> CycleResult:
        """Live-Aufbau, Historie und Gold nebeneinander.

        Der erste Aufbau des laufenden Monats dauert Stunden (≈ 20 min je Lauf). Solange er läuft,
        rechnet der Durchgang alle ``gold_every`` Sekunden Gold für Läufe mit fertigem Silber,
        jüngste zuerst (höchstens ``gold_limit`` je Runde), damit jeder selbst gebaute Lauf sofort
        nutzbar ist. Alle ``history_every`` Sekunden importiert er, was Actions inzwischen nach
        GHCR geschoben hat, je Runde höchstens ``import_limit`` Läufe; liegt mehr bereit, geht
        der Import in der nächsten Runde weiter.
        """
        result = CycleResult()
        if not self._bronze_synced:
            await asyncio.to_thread(self.sync_bronze)
            self._bronze_synced = True
        before = set(self.local.runs())
        live = asyncio.create_task(self.live())
        last_import: float | None = None
        more = False
        loop = asyncio.get_running_loop()
        while True:
            # in Portionen, damit Gold nicht auf den ganzen Import wartet; solange GHCR mehr hat,
            # in jeder Runde weiter
            if more or last_import is None or loop.time() - last_import >= history_every:
                last_import = loop.time()
                counts = await asyncio.to_thread(self.import_history, import_limit)
                more = bool(counts.pop("more", 0))
                result.imported.update(counts)
            result.gold.update(await asyncio.to_thread(self.process_gold, gold_limit))
            if live.done():
                break
            if not more:
                await asyncio.wait({live}, timeout=min(gold_every, history_every))
        result.live = await live
        # Gold für die eben gebauten Läufe sofort, nicht erst im nächsten Durchgang
        built = len(set(self.local.runs()) - before)
        if built:
            result.gold.update(await asyncio.to_thread(self.process_gold, built))
        result.bronze_deleted, result.bronze_kept = await asyncio.to_thread(self.clean_bronze)
        return result
