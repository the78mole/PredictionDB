"""Trainings-Snapshot ohne Datenbank: Energie, Wetter-Gold und Bestand aus GHCR.

Baut denselben Snapshot wie :func:`ml_train.snapshot.export_snapshot`, aber aus den
öffentlichen Paketen unter ``ghcr.io/the78mole/predictiondb-data`` statt aus der Datenbank der
VM. So laufen Training und Backtest in GitHub Actions ohne Zugang zur VM.

=========================== ===================================================================
Teil des Snapshots          Quelle in GHCR (Datenbank-Gegenstück)
=========================== ===================================================================
``energy.parquet``          ``energy:JJJJ``: ``value_best`` über die Quellen eines Jahres nach
                            Rang, dann Stundenmittel und spätestes ``published``
                            (``energy.value_best``)
``weather.parquet`` DE      ``gfs-0p25-gold:<Clusterung>-JJJJ-MM-HHz``: je Cluster, über die
                            Cluster einer Technologie zusammengefasst (``*_power`` als Summe,
                            sonst mit der Leistung der Cluster gewichtet; ``feat.nwp_value``)
``nb_*``, ``st_*``          ``gfs-0p5-gold:<Regionen>-JJJJ-MM-HHz`` (feste Regionen)
``en_*``                    ``gefs-0p5-silver:JJJJ-MM-00z``: Mittel, Streuung, P10/P90 über die
                            Mitglieder wie das GEFS-Gold der VM
``capacity.parquet``        Bestand je Cluster aus ``gfs-0p25-weights``, täglich je Technologie
                            summiert (``assets.capacity_daily``)
=========================== ===================================================================

Unterschiede zur VM: Das Gold rechnet alle Läufe mit den Gewichten einer Clusterung, die VM
behält für ältere Läufe die Gewichte ihres Rechenzeitpunkts. Energiedaten enthalten nur freie
Quellen; ``manifest.json`` nennt zusätzlich die verwendeten Pakete samt Digest
(``ghcr``) und fehlende Monate.

@relation(SWA-MLT-SNAPSHOT-GHCR, scope=file)
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import numpy as np
import pandas as pd
import pyarrow as pa

from ml_train.config import (
    EnsembleWeather,
    NeighbourWeather,
    SiteWeather,
    ensemble_feature,
    neighbour_feature,
    site_feature,
)
from ml_train.jobs import Progress, no_progress
from ml_train.snapshot import Snapshot, _months, compact_weather, source_classes
from predictiondb_core.log import get_logger
from wx_exchange import energy as energy_pkg
from wx_exchange.artifact import tag as month_tag
from wx_exchange.gefs import GefsMonth, unpack
from wx_exchange.gefs_vm import ensemble_stats
from wx_exchange.gold import LATEST, Weights, gold_tag, load_month, load_weights, parse_gold_tag
from wx_exchange.oci import Registry, digest

log = get_logger(__name__)

OWNER = "the78mole/predictiondb-data"
REPOSITORIES = {
    "energy": f"{OWNER}/energy",
    "weights": f"{OWNER}/gfs-0p25-weights",
    "gold_de": f"{OWNER}/gfs-0p25-gold",
    "gold_eu": f"{OWNER}/gfs-0p5-gold",
    "gefs": f"{OWNER}/gefs-0p5-silver",
}
WEATHER_COLUMNS = ["run_time", "valid_time", "feature", "value"]


@dataclass
class Packages:
    """Die Repositories der Eingänge; ``used`` sammelt Tag → Digest je Repository."""

    energy: Registry
    weights: Registry
    gold_de: Registry
    gold_eu: Registry
    gefs: Registry
    used: dict[str, dict[str, str]] = field(default_factory=dict)
    missing: list[str] = field(default_factory=list)

    @classmethod
    def connect(
        cls,
        client: httpx.Client,
        *,
        host: str = "ghcr.io",
        username: str | None = None,
        password: str | None = None,
    ) -> Packages:
        def reg(key: str) -> Registry:
            return Registry(
                host,
                REPOSITORIES[key],
                client,
                username=username,
                password=password,
                read_only=True,
            )

        return cls(
            energy=reg("energy"),
            weights=reg("weights"),
            gold_de=reg("gold_de"),
            gold_eu=reg("gold_eu"),
            gefs=reg("gefs"),
        )

    def note(self, registry: Registry, tag: str, manifest: dict[str, Any] | None) -> None:
        """Merkt sich den Digest eines gelesenen Manifests (fürs ``manifest.json``)."""
        if manifest is None:
            self.missing.append(f"{registry.repository}:{tag}")
            return
        raw = json.dumps(manifest, sort_keys=True).encode()
        self.used.setdefault(registry.repository, {})[tag] = digest(raw)


# --- Bausteine (rein, testbar) ---------------------------------------------------------------


def cluster_capacity(weights: Weights) -> dict[str, tuple[str, float]]:
    """Region des Clusters -> (Technologie, Leistung in MW zum letzten Stand des Bestands).

    Entspricht ``sum(assets.grid_weight.capacity_mw)`` je Cluster in ``WEATHER_SQL``: die
    Leistung aller Anlagen des Clusters im MaStR-Export der Clusterung.

    @relation(SWR-MLT-011, scope=function)
    """
    if weights.capacity is None:
        raise ValueError(f"Gewichte {weights.version}: kein Bestand je Cluster")
    cap = weights.capacity.to_pandas().sort_values("day")
    last = cap.groupby(["technology", "cluster_no"])["capacity_mw"].last()
    cells = weights.clusters.select(["region", "technology", "cluster_no"]).to_pandas()
    cells = cells.drop_duplicates("region")
    out = {}
    for region, tech, no in cells.itertuples(index=False):
        out[str(region)] = (str(tech), float(last.get((tech, int(no)), np.nan)))
    return out


def capacity_daily(weights: Weights) -> pd.DataFrame:
    """Bestand je Tag und Technologie aus den Änderungstagen je Cluster (fortgeschrieben).

    @relation(SWR-MLT-011, scope=function)
    """
    if weights.capacity is None:
        return pd.DataFrame(columns=["day", "technology", "capacity_mw"])
    cap = weights.capacity.to_pandas()
    cap["day"] = pd.to_datetime(cap["day"])
    days = pd.date_range(cap["day"].min(), cap["day"].max(), freq="D")
    wide = cap.pivot_table(
        index="day", columns=["technology", "cluster_no"], values="capacity_mw", aggfunc="last"
    )
    # vor dem ersten Änderungstag gibt es den Cluster noch nicht (0 MW, wie fehlende Zeilen)
    wide = wide.reindex(days).ffill()
    per_tech = wide.T.groupby(level="technology").sum(min_count=1).T
    out = per_tech.stack().rename("capacity_mw").reset_index()
    out.columns = ["day", "technology", "capacity_mw"]
    out = out.dropna(subset=["capacity_mw"])
    out = out.sort_values(["day", "technology"], kind="stable").reset_index(drop=True)
    out["day"] = out["day"].dt.date
    return out


def _select(
    df: pd.DataFrame,
    a: datetime,
    b: datetime,
    valid_to: datetime,
    hours: list[int],
    max_lead_h: int,
) -> pd.DataFrame:
    """Filter wie in ``WEATHER_SQL``/``NEIGHBOUR_SQL`` (Lauf, Laufstunde, Gültigkeit, Vorlauf)."""
    rt, vt = df["run_time"], df["valid_time"]
    ok = (
        (rt >= a)
        & (rt < b)
        & (vt >= a)
        & (vt < valid_to)
        & rt.dt.hour.isin(hours)
        & (vt <= rt + pd.Timedelta(hours=max_lead_h))
    )
    return df[ok]


def _frame(table: pa.Table) -> pd.DataFrame:
    df = table.to_pandas()
    for col in ("run_time", "valid_time"):
        df[col] = pd.to_datetime(df[col], utc=True)
    return df


def weather_de(
    gold: pd.DataFrame,
    caps: dict[str, tuple[str, float]],
    a: datetime,
    b: datetime,
    hours: list[int],
    max_lead_h: int,
) -> pd.DataFrame:
    """Cluster-Gold eines Monats zu Features je Technologie (``<technologie>_<feature>``).

    @relation(SWR-MLT-011, scope=function)
    """
    df = _select(gold, a, b, b + timedelta(hours=max_lead_h), hours, max_lead_h)
    region = df["region"].astype(str)
    known = region.isin(caps)
    if not known.all():
        unknown = sorted(set(region[~known]))
        raise ValueError(f"Gold enthält Cluster ohne Gewichte: {unknown[:5]}")
    tech = region.map(lambda r: caps[r][0])
    cap = region.map(lambda r: caps[r][1]).astype("float64")
    value = df["value"].astype("float64")
    work = pd.DataFrame(
        {
            "run_time": df["run_time"],
            "valid_time": df["valid_time"],
            "feature": tech + "_" + df["feature"].astype(str),
            "v": value,
            "vw": value * cap,
            "w": cap.where(value.notna()),
        }
    )
    g = work.groupby(["run_time", "valid_time", "feature"], sort=True, observed=True)[
        ["v", "vw", "w"]
    ].sum(min_count=1)
    g = g.reset_index()
    power = g["feature"].str.endswith("_power")
    w = g["w"].where(g["w"] != 0)
    g["value"] = np.where(power, g["v"], g["vw"] / w)
    return compact_weather(g[WEATHER_COLUMNS])


def fixed_rows(
    gold: pd.DataFrame,
    regions: list[str],
    features: list[str],
    name: Callable[[str, str], str],
    a: datetime,
    b: datetime,
    hours: list[int],
    max_lead_h: int,
) -> pd.DataFrame:
    """Feste Regionen (Nachbarzonen, Standorte) wie ``_fixed_rows`` in der Datenbank-Fassung.

    @relation(SWR-MLT-011, scope=function)
    """
    df = gold[gold["region"].isin(regions) & gold["feature"].isin(features)]
    df = _select(df, a, b, b + timedelta(hours=max_lead_h), hours, max_lead_h)
    names = {
        (r, f): name(r, f)
        for r, f in df[["region", "feature"]].astype(str).drop_duplicates().itertuples(index=False)
    }
    out = pd.DataFrame(
        {
            "run_time": df["run_time"],
            "valid_time": df["valid_time"],
            "feature": [
                names[(r, f)]
                for r, f in zip(df["region"].astype(str), df["feature"].astype(str), strict=True)
            ],
            "value": df["value"],
        }
    )
    return compact_weather(out)


def ensemble_run(
    meta: dict[str, Any],
    values: np.ndarray[Any, Any],
    en: EnsembleWeather,
    a: datetime,
    b: datetime,
    max_lead_h: int,
) -> pd.DataFrame:
    """GEFS-Kennzahlen eines Laufs (Silber, alle Mitglieder) wie das GEFS-Gold der VM.

    @relation(SWR-MLT-011, scope=function)
    """
    run = datetime.fromisoformat(str(meta["run"]))
    run = run if run.tzinfo else run.replace(tzinfo=UTC)
    stats = ensemble_stats(values.astype(np.float32))
    steps = [int(s) for s in meta["steps"]]
    regions = [f"gefs:{r}" for r in meta["regions"]]
    names = list(meta["variables"])
    wanted = en.wanted()
    valid_to = b + timedelta(hours=max_lead_h)
    rows_t: list[datetime] = []
    rows_f: list[str] = []
    rows_v: list[float] = []
    for stat, arr in stats.items():  # Schritt x Region x Größe
        for ri, region in enumerate(regions):
            for vi, var in enumerate(names):
                feat = ensemble_feature(region, f"{var}_{stat}")
                if feat not in wanted:
                    continue
                for si, step in enumerate(steps):
                    v = float(arr[si, ri, vi])
                    t = run + timedelta(hours=step)
                    if np.isfinite(v) and a <= t < valid_to and step <= max_lead_h:
                        rows_t.append(t)
                        rows_f.append(feat)
                        rows_v.append(v)
    df = pd.DataFrame(
        {"run_time": [run] * len(rows_t), "valid_time": rows_t, "feature": rows_f, "value": rows_v}
    )
    return compact_weather(df)


def energy_hourly(
    best: pd.DataFrame, codes: list[str], start: datetime, end: datetime
) -> pd.DataFrame:
    """Stundenmittel wie ``ENERGY_SQL`` (``time_bucket``, ``avg``, ``max(published)``).

    @relation(SWR-MLT-011, scope=function)
    """
    parts = []
    for code in codes:
        sel = best[(best["code"] == code) & (best["ts"] >= start) & (best["ts"] < end)]
        if sel.empty:
            parts.append(pd.DataFrame(columns=["ts", "value", "published", "code"]))
            continue
        g = sel.groupby(sel["ts"].dt.floor("h")).agg(
            value=("value", "mean"), published=("published", "max")
        )
        g.index.name = "ts"
        g = g.reset_index()
        g["code"] = code
        parts.append(g)
        log.info("snapshot.energy", code=code, rows=len(g))
    energy = pd.concat(parts, ignore_index=True)
    energy["ts"] = pd.to_datetime(energy["ts"], utc=True)
    energy["published"] = pd.to_datetime(energy["published"], utc=True)
    energy["value"] = energy["value"].astype("float64")
    return energy


def energy_sources(meta: Iterable[dict[str, Any]], codes: list[str]) -> dict[str, str]:
    """Lizenzklasse aller Quellen, die eine der Reihen liefern (wie ``SOURCES_SQL``)."""
    want = set(codes)
    rows: dict[str, tuple[str, bool, bool]] = {}
    for year in meta:
        for code, info in year.items():
            if want & set(info.get("series", {})):
                rows[code] = (code, bool(info.get("free")), bool(info.get("train")))
    return source_classes(sorted(rows.values()))


def _chunked[T](items: list[T], n: int) -> Iterator[list[T]]:
    for i in range(0, len(items), n):
        yield items[i : i + n]


def _clip(
    month: tuple[datetime, datetime], lo: datetime, hi: datetime
) -> tuple[datetime, datetime]:
    return max(month[0], lo), min(month[1], hi)


# --- Aufbau ----------------------------------------------------------------------------------


def single_version(registry: Registry) -> str:
    """Version der festen Regionen im EU-Gold (genau eine erwartet)."""
    versions = {p[0] for t in registry.tags() if (p := parse_gold_tag(t)) is not None}
    if len(versions) != 1:
        raise LookupError(f"{registry.repository}: Version nicht eindeutig: {sorted(versions)}")
    return versions.pop()


def ghcr_snapshot(
    packages: Packages,
    codes: list[str],
    start: datetime,
    end: datetime,
    *,
    weather_from: datetime | None = None,
    weather_to: datetime | None = None,
    run_hours: list[int] | None = None,
    max_lead_h: int = 216,
    weights_ref: str = LATEST,
    eu_version: str | None = None,
    neighbour: NeighbourWeather | None = None,
    sites: SiteWeather | None = None,
    ensemble: EnsembleWeather | None = None,
    workers: int = 4,
    progress: Progress = no_progress,
) -> Snapshot:
    """Snapshot für ``[start, end)`` wie :func:`ml_train.snapshot.export_snapshot`, aus GHCR.

    @relation(SWR-MLT-011, scope=function)
    """
    hours = run_hours or [0, 12]
    # --- Energie
    first, last = energy_pkg.year_of(start), energy_pkg.year_of(end - timedelta(microseconds=1))
    progress("Energie")
    parts = []
    metas = []
    for y in range(first, last + 1):
        m = packages.energy.manifest(energy_pkg.tag(y))
        packages.note(packages.energy, energy_pkg.tag(y), m)
        if m is None:
            continue
        values, meta = energy_pkg.load_year(packages.energy, y)
        metas.append(meta)
        ranked = []
        for src, df in values.items():
            prio = int(meta.get(src, {}).get("priority", 100))
            ranked.append((prio, df[df["code"].isin(codes)]))
        parts.append(energy_pkg.value_best(ranked))
        log.info("snapshot.energy_year", year=y, sources=len(values))
    best = (
        pd.concat(parts, ignore_index=True).drop_duplicates(["code", "ts"], keep="last")
        if parts
        else energy_pkg.empty_rows()
    )
    energy = energy_hourly(best, codes, start, end)
    sources = energy_sources(metas, codes)

    # --- Gewichte und Bestand
    wm = packages.weights.manifest(weights_ref)
    packages.note(packages.weights, weights_ref, wm)
    weights = load_weights(packages.weights, weights_ref)
    cv = weights.version
    caps = cluster_capacity(weights)
    capacity = capacity_daily(weights)

    # --- Wetter
    weather_parts: list[pd.DataFrame] = []
    wx_start = max(start, weather_from or start)
    wx_end = min(end, weather_to) if weather_to is not None else end
    months = list(_months(wx_start, wx_end)) if wx_start < wx_end else []
    eu = None
    if neighbour is not None or sites is not None:
        eu = eu_version or single_version(packages.gold_eu)
    site_start = wx_start - timedelta(days=sites.history_days) if sites is not None else wx_start
    eu_months = list(_months(min(site_start, wx_start), wx_end)) if months else []

    def de_month(item: tuple[datetime, datetime, int]) -> pd.DataFrame:
        a, b, hour = item
        t = gold_tag(cv, f"{a:%Y-%m}", hour)
        m = packages.gold_de.manifest(t)
        packages.note(packages.gold_de, t, m)
        if m is None:
            return pd.DataFrame(columns=WEATHER_COLUMNS)
        gold, gw, _ = load_month(packages.gold_de, t)
        if gw.version != cv:
            raise ValueError(f"{t}: Gold mit Gewichten {gw.version}, erwartet {cv}")
        return weather_de(_frame(gold), caps, a, b, [hour], max_lead_h)

    def eu_month(item: tuple[datetime, datetime, int]) -> pd.DataFrame:
        a, b, hour = item
        assert eu is not None
        t = gold_tag(eu, f"{a:%Y-%m}", hour)
        m = packages.gold_eu.manifest(t)
        packages.note(packages.gold_eu, t, m)
        if m is None:
            return pd.DataFrame(columns=WEATHER_COLUMNS)
        gold = _frame(load_month(packages.gold_eu, t)[0])
        out = []
        if neighbour is not None and b > wx_start:
            na, nb = _clip((a, b), wx_start, wx_end)
            out.append(
                fixed_rows(
                    gold,
                    neighbour.region_codes(),
                    neighbour.features,
                    neighbour_feature,
                    na,
                    nb,
                    [hour],
                    max_lead_h,
                )
            )
        if sites is not None:
            sa, sb = _clip((a, b), site_start, wx_end)
            out.append(
                fixed_rows(
                    gold,
                    sites.region_codes(),
                    sites.features,
                    site_feature,
                    sa,
                    sb,
                    [hour],
                    max_lead_h,
                )
            )
        return pd.concat(out, ignore_index=True) if out else pd.DataFrame(columns=WEATHER_COLUMNS)

    def gefs_month(item: tuple[datetime, datetime, int]) -> pd.DataFrame:
        a, b, hour = item
        assert ensemble is not None
        t = month_tag(f"{a:%Y-%m}", hour)
        m = packages.gefs.manifest(t)
        packages.note(packages.gefs, t, m)
        art = GefsMonth.from_manifest(f"{a:%Y-%m}", hour, m)
        out = []
        for run in sorted(art.layers):
            if not a <= run < b:
                continue
            meta, values = unpack(packages.gefs.blob(art.layers[run].digest))
            out.append(ensemble_run(meta, values, ensemble, a, b, max_lead_h))
        return pd.concat(out, ignore_index=True) if out else pd.DataFrame(columns=WEATHER_COLUMNS)

    jobs: list[tuple[str, Callable[[tuple[datetime, datetime, int]], pd.DataFrame], Any]] = []
    jobs += [("DE", de_month, (a, b, h)) for a, b in months for h in hours]
    if eu is not None:
        lo = min(site_start, wx_start)
        jobs += [("EU", eu_month, (*_clip(m, lo, wx_end), h)) for m in eu_months for h in hours]
    if ensemble is not None:
        jobs += [("GEFS", gefs_month, (a, b, h)) for a, b in months for h in ensemble.run_hours]
    done = 0
    with ThreadPoolExecutor(max(1, workers)) as pool:
        for chunk in _chunked(jobs, max(1, workers)):
            results = list(pool.map(lambda j: j[1](j[2]), chunk))
            for (kind, _, item), df in zip(chunk, results, strict=True):
                done += 1
                progress(f"Wetter {done}/{len(jobs)} · {kind} {item[0]:%Y-%m} {item[2]:02d}z")
                log.info("snapshot.weather", part=kind, month=f"{item[0]:%Y-%m}", rows=len(df))
                if not df.empty:
                    weather_parts.append(compact_weather(df))

    weather = (
        pd.concat(weather_parts, ignore_index=True)
        if weather_parts
        else pd.DataFrame(columns=WEATHER_COLUMNS)
    )
    for col in ("run_time", "valid_time"):
        weather[col] = pd.to_datetime(weather[col], utc=True)
    weather["feature"] = weather["feature"].astype(object)
    weather["value"] = weather["value"].astype("float32")
    weather = weather.sort_values(["feature", "run_time", "valid_time"], kind="stable")
    weather = weather.reset_index(drop=True)

    manifest: dict[str, Any] = {
        "created": datetime.now(UTC).isoformat(),
        "range": [start.isoformat(), end.isoformat()],
        "codes": codes,
        "sources": sources,
        "publishable": "unfrei" not in sources.values(),
        "clustering_version": cv,
        "weather": {
            "source": "gfs_0p25",
            "grid": "gfs_0p25",
            "run_hours": hours,
            "max_lead_h": max_lead_h,
        },
        "ghcr": {
            "registry": packages.energy.host,
            "packages": packages.used,
            "missing": sorted(packages.missing),
            "eu_version": eu,
        },
    }
    if neighbour is not None:
        manifest["neighbour_weather"] = neighbour.model_dump()
    if sites is not None:
        manifest["site_weather"] = sites.model_dump()
    if ensemble is not None:
        manifest["ensemble_weather"] = ensemble.model_dump()
    return Snapshot(energy, weather, capacity, manifest)
