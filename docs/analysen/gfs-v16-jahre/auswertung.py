"""GFS v16 über die Jahre: Prognosefehler an DWD-Stationen, früh gegen mittel gegen jüngst.

Auftrag und Parameter stehen in ``job.yaml`` (docs/ARCHITEKTUR.md 7.9), die Ergebnisse liegen
daneben (README.md, tabellen/, grafiken/). Aufruf aus dem Repository-Wurzelverzeichnis::

    uv run --with matplotlib python docs/analysen/gfs-v16-jahre/auswertung.py

Gegenüber ``docs/analysen/gfs-v15-v16`` kommen dazu:

- eine Persistenz-Referenz (letzte Messung zur gleichen Tageszeit vor dem Lauf), um die
  Schwierigkeit der Wetterlage vom Modellfehler zu trennen: Skill = 1 - MSE_Modell/MSE_Persistenz,
- Auswertung je Jahreszeit (nach Gültigkeitszeit),
- eine Monatsreihe über die ganze v16-Zeit mit den Modell-Updates als Marken.

GFS kommt aus dem AWS-Archiv (``.idx`` + Range, zugeschnitten zwischengespeichert, nur native
Schritte, Zeitinterpolation erst beim Abruf), die Messungen aus den DWD-Stundenwerten
(historisch bis Ende des Vorjahres, ``recent`` für die letzten rund 500 Tage).
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import io
import json
import re
import subprocess
import zipfile
from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import numpy as np
import yaml
from gribkit import BBox, MessageSpec, crop, iter_messages, merge_ranges, parse_idx, select
from numpy.typing import NDArray

GFS = "https://noaa-gfs-bdp-pds.s3.amazonaws.com"
DWD = "https://opendata.dwd.de/climate_environment/CDC/observations_germany/climate/hourly"
V16_START = datetime(2021, 3, 22, 12, tzinfo=UTC)
OUT = Path(__file__).parent
JOB = OUT / "job.yaml"

FIELDS = [
    MessageSpec("TMP", "2 m above ground"),
    MessageSpec("DPT", "2 m above ground"),
    MessageSpec("UGRD", "10 m above ground"),
    MessageSpec("VGRD", "10 m above ground"),
    MessageSpec("PRMSL", "mean sea level"),
    MessageSpec("HGT", "surface"),
    MessageSpec("DSWRF", "surface", "ave"),
]
VARIABLES = {
    "t2m": ("Temperatur 2 m", "K"),
    "td2m": ("Taupunkt 2 m", "K"),
    "ff10": ("Windgeschwindigkeit 10 m", "m/s"),
    "mslp": ("Luftdruck auf Meeresniveau", "hPa"),
    "ghi": ("Globalstrahlung, 6-h-Mittel", "W/m²"),
}
#: DWD-Produkte: Kürzel -> (Verzeichnis, Spalte, Variable)
PRODUCTS = {
    "TU": ("air_temperature", "TT_TU", "t2m"),
    "TD": ("dew_point", "TD", "td2m"),
    "FF": ("wind", "F", "ff10"),
    "P0": ("pressure", "P", "mslp"),
    "ST": ("solar", "FG_LBERG", "ghi"),
}
MET = ("TU", "TD", "FF", "P0")
MET_VARS = (("t2m", "TU"), ("td2m", "TD"), ("ff10", "FF"), ("mslp", "P0"))
SEASONS = {12: "Winter", 1: "Winter", 2: "Winter", 3: "Frühling", 4: "Frühling", 5: "Frühling"}
SEASONS |= {6: "Sommer", 7: "Sommer", 8: "Sommer", 9: "Herbst", 10: "Herbst", 11: "Herbst"}
SEASON_ORDER = ("Winter", "Frühling", "Sommer", "Herbst")
LAPSE = 0.0065
BBOX = BBox(55.5, 47.0, 5.5, 15.5)
LEAD_LABEL = {0: "Analyse", 240: "10 Tage"}


@dataclass(frozen=True)
class Station:
    sid: str
    name: str
    lat: float
    lon: float
    elev: float


@dataclass(frozen=True)
class Job:
    periods: dict[str, tuple[datetime, datetime]]
    runs: tuple[int, ...]
    leads: tuple[int, ...]
    min_completeness: float
    monthly: tuple[datetime, datetime, tuple[int, ...], tuple[int, ...]]
    markers: list[tuple[datetime, str]]
    sha256: str

    @classmethod
    def load(cls, path: Path) -> Job:
        raw = path.read_bytes()
        j = yaml.safe_load(raw)

        def ts(d: Any, end: bool = False) -> datetime:
            if isinstance(d, str):
                d = datetime.fromisoformat(d)
            if isinstance(d, datetime):
                return d.replace(tzinfo=UTC)
            t = datetime(d.year, d.month, d.day, tzinfo=UTC)
            return t + timedelta(hours=18) if end else t

        fc = j["inputs"]["forecast"]
        ms = j["method"]["monthly_series"]
        return cls(
            periods={p["label"]: (ts(p["from"]), ts(p["to"], end=True)) for p in fc["periods"]},
            runs=tuple(fc["runs"]),
            leads=tuple(j["method"]["horizons_h"]),
            min_completeness=j["inputs"]["observations"]["stations"]["min_completeness"],
            monthly=(
                ts(ms["from"]),
                ts(ms["to"], end=True),
                tuple(ms["runs"]),
                tuple(ms["horizons_h"]),
            ),
            markers=[(ts(m["date"]), m["label"]) for m in j["method"]["markers"]],
            sha256=hashlib.sha256(raw).hexdigest(),
        )

    @property
    def first(self) -> datetime:
        return min([a for a, _ in self.periods.values()] + [self.monthly[0]])

    @property
    def last(self) -> datetime:
        return max([b for _, b in self.periods.values()] + [self.monthly[1]])


# --- Messungen (DWD) als Stundenreihen ----------------------------------------------------------


class Clock:
    """Stundenindex ab ``t0``; Messreihen sind float32-Arrays mit NaN für fehlende Werte."""

    def __init__(self, t0: datetime, t1: datetime) -> None:
        self.t0 = t0
        self.n = int((t1 - t0).total_seconds() // 3600) + 1

    def idx(self, t: datetime) -> int:
        return int((t - self.t0).total_seconds() // 3600)


def parse_stations(text: str) -> dict[str, Station]:
    lines = text.splitlines()
    tail = 2 if "Abgabe" in lines[0] else 1
    out = {}
    for line in lines[2:]:
        p = line.split()
        if len(p) < 7 + tail or not p[0].isdigit():
            continue
        out[p[0]] = Station(p[0], " ".join(p[6:-tail]), float(p[4]), float(p[5]), float(p[3]))
    return out


def parse_into(text: str, column: str, clock: Clock, arr: NDArray[np.float32]) -> None:
    """Spalte ``column`` in die Stundenreihe ``arr`` eintragen (vektorisiert, Fehlwert -999)."""
    head, _, rest = text.partition("\n")
    col = [c.strip() for c in head.split(";")].index(column)
    pos = rest.find(f";{clock.t0:%Y%m%d}")
    if pos >= 0:  # nur ab Beginn des Auswertezeitraums lesen
        rest = rest[rest.rfind("\n", 0, pos) + 1 :]
    rows = [p for p in (line.split(";") for line in rest.splitlines()) if len(p) > col]
    if not rows:
        return
    stamps = [p[1].strip() for p in rows]
    hours = np.array(
        [f"{s[:4]}-{s[4:6]}-{s[6:8]}T{s[8:10]}" for s in stamps], dtype="datetime64[h]"
    )
    # Solar: Ende des Stundenintervalls mit Minuten (JJJJMMTTHH:MM), auf die volle Stunde runden
    hours = hours + np.array([len(s) > 10 and int(s[11:13]) >= 30 for s in stamps])
    idx = (hours - np.datetime64(clock.t0.replace(tzinfo=None), "h")).astype(int)
    values = np.array([float(p[col]) for p in rows], dtype=np.float32)
    ok = (idx >= 0) & (idx < clock.n) & (values > -999)
    arr[idx[ok]] = values[ok]


def _unzip_into(data: bytes, column: str, clock: Clock, arr: NDArray[np.float32]) -> None:
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        name = next(n for n in zf.namelist() if n.startswith("produkt_"))
        parse_into(zf.read(name).decode("latin-1"), column, clock, arr)


async def load_observations(
    client: httpx.AsyncClient, cache: Path, clock: Clock
) -> tuple[dict[str, Station], dict[str, Station], dict[str, dict[str, NDArray[np.float32]]]]:
    sem = asyncio.Semaphore(6)
    urls: dict[str, dict[str, list[str]]] = {c: defaultdict(list) for c in PRODUCTS}
    meta: dict[str, dict[str, Station]] = {}
    first = f"{clock.t0:%Y%m%d}"
    for code, (folder, _, _) in PRODUCTS.items():
        subs = [folder] if code == "ST" else [f"{folder}/historical", f"{folder}/recent"]
        for sub in subs:
            text = (await client.get(f"{DWD}/{sub}/")).text
            names = re.findall(r'href="([^"]+\.(?:zip|txt))"', text)
            if code not in meta:
                desc = next(n for n in names if n.endswith("Stationen.txt"))
                resp = await client.get(f"{DWD}/{sub}/{desc}")
                meta[code] = parse_stations(resp.content.decode("latin-1"))
            for n in names:
                m = re.fullmatch(
                    rf"stundenwerte_{code}_(\d{{5}})_(?:(\d{{8}})_(\d{{8}})_hist|akt|row)\.zip", n
                )
                if not m or m[1] not in meta[code]:
                    continue
                if m[2] and m[3] < first:  # historische Datei endet vor dem Auswertezeitraum
                    continue
                urls[code][m[1]].append(f"{DWD}/{sub}/{n}")

    met_ids = sorted(set.intersection(*(set(urls[c]) for c in MET)))
    obs: dict[str, dict[str, NDArray[np.float32]]] = {c: {} for c in PRODUCTS}

    async def one(code: str, sid: str) -> None:
        path = cache / "dwd" / f"{code}_{sid}_{clock.t0:%Y%m%d}_{clock.n}.npy"
        if not path.exists():
            arr = np.full(clock.n, np.nan, dtype=np.float32)
            # Laden und Auswerten unter derselben Sperre, damit nicht Hunderte entpackte
            # Dateien gleichzeitig im Speicher liegen
            async with sem:
                for url in urls[code][sid]:  # historisch zuerst, recent überschreibt
                    resp = await client.get(url)
                    resp.raise_for_status()
                    await asyncio.to_thread(
                        _unzip_into, resp.content, PRODUCTS[code][1], clock, arr
                    )
            path.parent.mkdir(parents=True, exist_ok=True)
            np.save(path, arr)
        obs[code][sid] = np.load(path)

    await asyncio.gather(*(one(c, s) for c in MET for s in met_ids))
    await asyncio.gather(*(one("ST", s) for s in urls["ST"]))
    met = {s: meta["TU"][s] for s in met_ids}
    sol = {s: meta["ST"][s] for s in urls["ST"]}
    return met, sol, obs


def complete_enough(
    arr: NDArray[np.float32], clock: Clock, periods: dict[str, tuple[datetime, datetime]], q: float
) -> bool:
    for a, b in periods.values():
        seg = arr[clock.idx(a) : clock.idx(b) + 1]
        if np.isfinite(seg).mean() < q:
            return False
    return True


# --- GFS (wie gfs-v15-v16) -----------------------------------------------------------------------


def gfs_url(run: datetime, step: int) -> str:
    sub = "atmos/" if run >= V16_START else ""
    return f"{GFS}/gfs.{run:%Y%m%d}/{run:%H}/{sub}gfs.t{run:%H}z.pgrb2.0p25.f{step:03d}"


@dataclass
class Field:
    values: NDArray[np.float64]
    lat0: float
    lon0: float
    dlat: float
    dlon: float

    def at(self, lats: NDArray[Any], lons: NDArray[Any]) -> NDArray[np.float64]:
        nj, ni = self.values.shape
        y = np.clip((self.lat0 - lats) / self.dlat, 0, nj - 1.000001)
        x = np.clip((np.mod(lons, 360) - self.lon0) / self.dlon, 0, ni - 1.000001)
        j, i = np.floor(y).astype(int), np.floor(x).astype(int)
        fy, fx = y - j, x - i
        v = self.values
        out: NDArray[np.float64] = (
            v[j, i] * (1 - fy) * (1 - fx)
            + v[j, i + 1] * (1 - fy) * fx
            + v[j + 1, i] * fy * (1 - fx)
            + v[j + 1, i + 1] * fy * fx
        )
        return out


def decode(grib: bytes, keys: list[str]) -> dict[str, Field]:
    import eccodes as ec

    out = {}
    for msg, key in zip(iter_messages(grib), keys, strict=True):
        h = ec.codes_new_from_message(msg)
        try:
            out[key] = Field(
                ec.codes_get_values(h).reshape(ec.codes_get(h, "Nj"), ec.codes_get(h, "Ni")),
                ec.codes_get(h, "latitudeOfFirstGridPointInDegrees"),
                ec.codes_get(h, "longitudeOfFirstGridPointInDegrees"),
                ec.codes_get(h, "jDirectionIncrementInDegrees"),
                ec.codes_get(h, "iDirectionIncrementInDegrees"),
            )
        finally:
            ec.codes_release(h)
    return out


class GfsArchive:
    def __init__(self, client: httpx.AsyncClient, cache: Path) -> None:
        self.client = client
        self.cache = cache / "gfs"
        self.sem = asyncio.Semaphore(24)
        self.missing: dict[str, int] = defaultdict(int)
        self.interpolated: dict[str, int] = defaultdict(int)

    async def _get(self, url: str, **kw: Any) -> httpx.Response:
        for attempt in range(6):
            try:
                resp = await self.client.get(url, **kw)
                if resp.status_code < 500:
                    return resp
            except httpx.TransportError:
                pass
            await asyncio.sleep(2**attempt)
        raise RuntimeError(f"{url} nicht erreichbar")

    async def native(self, run: datetime, step: int) -> dict[str, Field] | None:
        base = self.cache / f"{run:%Y%m%d%H}" / f"f{step:03d}"
        grib, keys = base.with_suffix(".grib2"), base.with_suffix(".json")
        if not keys.exists():
            url = gfs_url(run, step)
            async with self.sem:
                resp = await self._get(url + ".idx")
                if resp.status_code in (403, 404):
                    data, names = b"", []
                else:
                    resp.raise_for_status()
                    ranges = merge_ranges(select(parse_idx(resp.text), FIELDS))
                    parts = await asyncio.gather(
                        *(self._get(url, headers={"Range": r.header}) for r in ranges)
                    )
                    raw = b"".join(r.extract(p.content) for r, p in zip(ranges, parts, strict=True))
                    data = await asyncio.to_thread(crop, raw, BBOX)
                    names = [e.key for r in ranges for e in r.entries]
            base.parent.mkdir(parents=True, exist_ok=True)
            grib.write_bytes(data)
            keys.write_text(json.dumps(names))
        names = json.loads(keys.read_text())
        if not names:
            return None
        return await asyncio.to_thread(decode, grib.read_bytes(), names)

    async def at(self, run: datetime, lead: int, group: str) -> dict[str, Field] | None:
        """Felder zum Horizont; fehlt der Schritt, Momentanwerte linear zwischen den nativen
        Nachbarschritten im 3-h-Raster (Mittelwerte nicht)."""
        f = await self.native(run, lead)
        if f is not None:
            return f
        lo, hi = lead // 3 * 3, -(-lead // 3) * 3
        a, b = (None, None)
        if lo != hi:
            a, b = await asyncio.gather(self.native(run, lo), self.native(run, hi))
        if a is None or b is None:
            self.missing[group] += 1
            return None
        self.interpolated[group] += 1
        w = (lead - lo) / (hi - lo)
        return {
            k: replace(a[k], values=(1 - w) * a[k].values + w * b[k].values)
            for k in a
            if k in b and "ave" not in k
        }


def pick(fields: dict[str, Field], var: str, level: str, lead: int | None = None) -> Field | None:
    for key, f in fields.items():
        v, lv, fc = key.split(":", 2)
        if v != var or lv != level:
            continue
        if lead is None and "ave" not in fc and "acc" not in fc:
            return f
        if lead is not None and fc == f"{lead - 6}-{lead} hour ave fcst":
            return f
    return None


# --- Auswertung ----------------------------------------------------------------------------------

#: je Gruppe und Lauf: n, Summe Fehler, Summe |Fehler|, Summe Fehler², n_p, Fehler² Modell und
#: Persistenz auf den Paaren mit Persistenzwert
Acc = dict[tuple[Any, ...], dict[str, NDArray[np.float64]]]


def runs_between(a: datetime, b: datetime, hours: tuple[int, ...]) -> list[datetime]:
    out, t = [], a
    while t <= b:
        if t.hour in hours:
            out.append(t)
        t += timedelta(hours=6)
    return out


def persistence_index(clock: Clock, run: datetime, lead: int) -> int:
    """Letzte Messung zur gleichen Tageszeit wie die Gültigkeitszeit, die zum Laufzeitpunkt
    schon vorlag."""
    valid = run + timedelta(hours=lead)
    back = 24 * max(1, -(-lead // 24))
    return clock.idx(valid - timedelta(hours=back))


def add(acc: Acc, key: tuple[Any, ...], tag: str, err: NDArray[Any], perr: NDArray[Any]) -> None:
    ok = np.isfinite(err)
    if not ok.any():
        return
    e = err[ok]
    both = ok & np.isfinite(perr)
    rec = acc[key].setdefault(tag, np.zeros(7))
    rec += [
        e.size,
        e.sum(),
        np.abs(e).sum(),
        (e**2).sum(),
        both.sum(),
        (err[both] ** 2).sum(),
        (perr[both] ** 2).sum(),
    ]


async def collect(job: Job, cache: Path) -> dict[str, Any]:
    clock = Clock(job.first - timedelta(days=11), job.last + timedelta(hours=max(job.leads) + 6))
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(180.0, connect=20.0), limits=httpx.Limits(max_connections=48)
    ) as client:
        met_all, sol_all, obs = await load_observations(client, cache, clock)
        q = job.min_completeness
        met = [
            s
            for sid, s in sorted(met_all.items())
            if BBOX.south <= s.lat <= BBOX.north
            and all(complete_enough(obs[c][sid], clock, job.periods, q) for c in MET)
        ]
        sol = [
            s
            for sid, s in sorted(sol_all.items())
            if BBOX.south <= s.lat <= BBOX.north
            and complete_enough(obs["ST"][sid], clock, job.periods, q)
        ]
        print(f"Stationen: {len(met)} Wetter, {len(sol)} Strahlung", flush=True)
        mlat, mlon = np.array([s.lat for s in met]), np.array([s.lon for s in met])
        slat, slon = np.array([s.lat for s in sol]), np.array([s.lon for s in sol])
        elev = np.array([s.elev for s in met])
        mobs = {code: np.stack([obs[code][s.sid] for s in met]) for code in MET}
        sobs = np.stack([obs["ST"][s.sid] for s in sol]) * 10000 / 3600  # J/cm² -> W/m²

        gfs = GfsArchive(client, cache)
        acc: Acc = defaultdict(dict)

        async def one(group: str, run: datetime, lead: int, monthly: bool) -> None:
            f = await gfs.at(run, lead, group)
            if f is None:
                return
            valid = run + timedelta(hours=lead)
            vi, pi = clock.idx(valid), persistence_index(clock, run, lead)
            tag = f"{run:%Y%m%d%H}"
            keyb = ("monat", f"{run:%Y-%m}") if monthly else (group, SEASONS[valid.month])
            got = {
                k: pick(f, *spec)
                for k, spec in {
                    "t": ("TMP", "2 m above ground"),
                    "td": ("DPT", "2 m above ground"),
                    "u": ("UGRD", "10 m above ground"),
                    "v": ("VGRD", "10 m above ground"),
                    "p": ("PRMSL", "mean sea level"),
                    "z": ("HGT", "surface"),
                }.items()
            }
            if all(g is not None for g in got.values()):
                at = {k: g.at(mlat, mlon) for k, g in got.items() if g is not None}
                model = {
                    "t2m": at["t"] + LAPSE * (at["z"] - elev) - 273.15,
                    "td2m": at["td"] - 273.15,
                    "ff10": np.hypot(at["u"], at["v"]),
                    "mslp": at["p"] / 100,
                }
                for var, code in MET_VARS:
                    o = mobs[code][:, vi]
                    pers = mobs[code][:, pi] - o if lead > 0 else np.full_like(o, np.nan)
                    add(acc, (keyb[0], var, lead, keyb[1]), tag, model[var] - o, pers)
            # Die Monatsreihe nutzt nur 00Z-Läufe; deren 24-h- und 72-h-Fenster (18-24 UTC)
            # liegen in der Nacht, Strahlung bleibt dort außen vor.
            g = pick(f, "DSWRF", "surface", lead) if lead >= 6 and keyb[0] != "monat" else None
            if g is not None and sol:
                o = np.nanmean(sobs[:, vi - 5 : vi + 1], axis=1)
                o[np.isnan(sobs[:, vi - 5 : vi + 1]).any(axis=1)] = np.nan
                po = sobs[:, pi - 5 : pi + 1]
                p = po.mean(axis=1) if po.shape[1] == 6 else np.full_like(o, np.nan)
                m = g.at(slat, slon)
                day = (o > 0) | (m > 0)
                err = np.where(day, m - o, np.nan)
                add(acc, (keyb[0], "ghi", lead, keyb[1]), tag, err, np.where(day, p - o, np.nan))

        jobs: list[tuple[str, datetime, int, bool]] = [
            (label, r, lead, False)
            for label, (a, b) in job.periods.items()
            for r in runs_between(a, b, job.runs)
            for lead in job.leads
        ]
        ma, mb, mruns, mleads = job.monthly
        jobs += [("monat", r, lead, True) for r in runs_between(ma, mb, mruns) for lead in mleads]
        for i in range(0, len(jobs), 48):
            await asyncio.gather(*(one(*j) for j in jobs[i : i + 48]))
            if i % 2400 == 0:
                print(f"{i}/{len(jobs)} Schritte", flush=True)
        return {
            "met": met,
            "sol": sol,
            "acc": acc,
            "missing": dict(gfs.missing),
            "interpolated": dict(gfs.interpolated),
            "n_jobs": len(jobs),
        }


def stats(per_run: dict[str, NDArray[np.float64]], rng: np.random.Generator) -> dict[str, Any]:
    m = np.array(list(per_run.values()))
    tot = m.sum(axis=0)
    n, s, sa, ss, n_p, ssm, ssp = tot
    out: dict[str, Any] = {
        "n": int(n),
        "bias": s / n,
        "mae": sa / n,
        "rmse": float(np.sqrt(ss / n)),
    }
    picks = rng.integers(0, len(m), (500, len(m)))
    boot = m[picks].sum(axis=1)
    out["rmse_ki_unten"], out["rmse_ki_oben"] = np.percentile(
        np.sqrt(boot[:, 3] / boot[:, 0]), [2.5, 97.5]
    )
    if n_p > 0 and ssp > 0:
        out["rmse_persistenz"] = float(np.sqrt(ssp / n_p))
        out["skill"] = 1 - ssm / ssp
        with np.errstate(invalid="ignore", divide="ignore"):
            sk = 1 - boot[:, 5] / boot[:, 6]
        out["skill_ki_unten"], out["skill_ki_oben"] = np.nanpercentile(sk, [2.5, 97.5])
    return out


def summarize(
    acc: Acc, job: Job
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    rng = np.random.default_rng(42)
    by_period: dict[tuple[str, str, int], dict[str, NDArray[np.float64]]] = defaultdict(dict)
    season_rows, month_rows = [], []
    for (group, var, lead, sub), per_run in sorted(acc.items()):
        if group == "monat":
            month_rows.append(
                {"monat": sub, "groesse": var, "horizont_h": lead, **stats(per_run, rng)}
            )
            continue
        season_rows.append(
            {
                "zeitraum": group,
                "jahreszeit": sub,
                "groesse": var,
                "horizont_h": lead,
                **stats(per_run, rng),
            }
        )
        merged = by_period[(group, var, lead)]
        for tag, rec in per_run.items():
            merged[tag] = merged.get(tag, np.zeros(7)) + rec
    period_rows = [
        {"zeitraum": p, "groesse": v, "horizont_h": lead, **stats(per_run, rng)}
        for (p, v, lead), per_run in by_period.items()
    ]
    order = {p: i for i, p in enumerate(job.periods)}
    period_rows.sort(key=lambda r: (r["groesse"], r["horizont_h"], order[r["zeitraum"]]))
    season_rows.sort(
        key=lambda r: (
            r["groesse"],
            r["horizont_h"],
            SEASON_ORDER.index(r["jahreszeit"]),
            order[r["zeitraum"]],
        )
    )
    return period_rows, season_rows, month_rows


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    keys: list[str] = []
    for r in rows:
        keys += [k for k in r if k not in keys]
    with path.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=keys)
        w.writeheader()
        for r in rows:
            w.writerow(
                {
                    k: round(float(v), 4) if isinstance(v, (float, np.floating)) else v
                    for k, v in r.items()
                }
            )


def trend(month_rows: list[dict[str, Any]], job: Job) -> list[dict[str, Any]]:
    """Trend und Sprünge an den Update-Marken in der Monatsreihe, ohne Jahresgang.

    Je Kalendermonat wird der Mittelwert über alle Jahre abgezogen (Anomalie). Der Trend ist die
    OLS-Steigung pro Jahr; der Standardfehler ist für Autokorrelation 1. Ordnung korrigiert
    (effektive Stichprobe n·(1-r)/(1+r)). Der Sprung an einer Marke ist das Mittel der Anomalien
    in den 12 Monaten danach minus davor.
    """
    out: list[dict[str, Any]] = []
    keys = sorted({(r["groesse"], int(r["horizont_h"])) for r in month_rows})
    for var, lead in keys:
        rows = sorted(
            (r for r in month_rows if r["groesse"] == var and int(r["horizont_h"]) == lead),
            key=lambda r: r["monat"],
        )
        months = [datetime.strptime(r["monat"] + "+0000", "%Y-%m%z") for r in rows]
        t = np.array([m.year + (m.month - 0.5) / 12 for m in months])
        cal = np.array([m.month for m in months])
        for metric in ("rmse", "rmse_persistenz", "skill", "bias"):
            y = np.array([float(r[metric]) if r[metric] != "" else np.nan for r in rows])
            ok = np.isfinite(y)
            if ok.sum() < 24:
                continue
            anom = y.copy()
            for m in range(1, 13):
                sel = ok & (cal == m)
                anom[sel] -= y[sel].mean()
            tt, aa = t[ok], anom[ok]
            slope, icept = np.polyfit(tt, aa, 1)
            res = aa - (slope * tt + icept)
            r1 = float(np.corrcoef(res[:-1], res[1:])[0, 1])
            n_eff = max(3.0, len(aa) * (1 - r1) / (1 + r1))
            se = float(np.sqrt(res.var(ddof=2) / ((tt - tt.mean()) ** 2).sum() * len(aa) / n_eff))
            row: dict[str, Any] = {
                "groesse": var,
                "horizont_h": lead,
                "kennzahl": metric,
                "monate": int(ok.sum()),
                "mittel": float(y[ok].mean()),
                "trend_pro_jahr": float(slope),
                "trend_ki_unten": float(slope - 1.96 * se),
                "trend_ki_oben": float(slope + 1.96 * se),
            }
            for when, label in job.markers:
                d = np.array([(m.year - when.year) * 12 + m.month - when.month for m in months])
                before = ok & (d < 0) & (d >= -12)
                after = ok & (d > 0) & (d <= 12)
                row[f"sprung_{label}"] = float(anom[after].mean() - anom[before].mean())
            out.append(row)
    return out


def lead_label(lead: int) -> str:
    return LEAD_LABEL.get(lead, f"{lead} h")


def plot(
    job: Job,
    period_rows: list[dict[str, Any]],
    season_rows: list[dict[str, Any]],
    month_rows: list[dict[str, Any]],
) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt

    out = OUT / "grafiken"
    out.mkdir(exist_ok=True)
    colors = dict(zip(job.periods, ("#7fb3d5", "#2471a3", "#0b2e59"), strict=False))
    by = {(r["zeitraum"], r["groesse"], r["horizont_h"]): r for r in period_rows}
    x = np.arange(len(job.leads))
    for var, (name, unit) in VARIABLES.items():
        fig, axes = plt.subplots(1, 3, figsize=(15, 4), constrained_layout=True)
        for period, col in colors.items():
            pts = [by.get((period, var, lead)) for lead in job.leads]

            def s(key: str, pts: list[dict[str, Any] | None] = pts) -> list[float]:
                return [float(p[key]) if p and key in p else np.nan for p in pts]

            axes[0].plot(x, s("rmse"), "o-", color=col, label=period)
            axes[0].fill_between(x, s("rmse_ki_unten"), s("rmse_ki_oben"), color=col, alpha=0.2)
            axes[1].plot(x, s("skill"), "o-", color=col, label=period)
            axes[1].fill_between(x, s("skill_ki_unten"), s("skill_ki_oben"), color=col, alpha=0.2)
            axes[2].plot(x, s("bias"), "o-", color=col, label=period)
        titles = ("RMSE (95-%-Intervall)", "Skill gegen Persistenz", "Bias (Modell - Messung)")
        for ax, title, yl in zip(axes, titles, (unit, "1 - MSE/MSE_Pers.", unit), strict=True):
            ax.set_xticks(x, [lead_label(lead) for lead in job.leads], rotation=30)
            ax.set_title(f"{name}: {title}", fontsize=10)
            ax.set_ylabel(yl)
            ax.grid(alpha=0.3)
        axes[1].axhline(0, color="black", lw=0.8)
        axes[2].axhline(0, color="black", lw=0.8)
        axes[0].legend()
        fig.savefig(out / f"{var}.png", dpi=110)
        plt.close(fig)

    # Jahreszeiten, Horizont 24 h
    fig, axes = plt.subplots(1, len(VARIABLES), figsize=(18, 3.8), constrained_layout=True)
    sb = {(r["zeitraum"], r["groesse"], r["horizont_h"], r["jahreszeit"]): r for r in season_rows}
    for ax, (var, (name, unit)) in zip(axes, VARIABLES.items(), strict=True):
        w = 0.8 / len(job.periods)
        for k, (period, col) in enumerate(colors.items()):
            vals = [sb.get((period, var, 24, se), {}).get("rmse", np.nan) for se in SEASON_ORDER]
            ax.bar(np.arange(4) + k * w - 0.4 + w / 2, vals, w, color=col, label=period)
        ax.set_xticks(np.arange(4), SEASON_ORDER)
        ax.set_title(f"{name}\nRMSE 24 h", fontsize=9)
        ax.set_ylabel(unit)
        ax.grid(alpha=0.3, axis="y")
    axes[0].legend(fontsize=8, loc="lower left", framealpha=0.9)
    fig.savefig(out / "jahreszeiten_24h.png", dpi=110)
    plt.close(fig)

    # Monatsreihe
    mb = defaultdict(list)
    for r in month_rows:
        mb[(r["groesse"], r["horizont_h"])].append(r)
    for var, (name, unit) in VARIABLES.items():
        if not any(v == var for v, _ in mb):
            continue
        fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(13, 6), sharex=True, constrained_layout=True)
        for lead, col in zip(job.monthly[3], ("#2471a3", "#c0392b"), strict=False):
            rows = sorted(mb.get((var, lead), []), key=lambda r: r["monat"])
            t = [datetime.strptime(r["monat"] + "-15+0000", "%Y-%m-%d%z") for r in rows]
            ax1.plot(t, [r["rmse"] for r in rows], "-", color=col, label=f"GFS {lead} h")
            ax1.plot(
                t,
                [r.get("rmse_persistenz", np.nan) for r in rows],
                ":",
                color=col,
                label=f"Persistenz {lead} h",
            )
            ax2.plot(t, [r.get("skill", np.nan) for r in rows], "-", color=col, label=f"{lead} h")
        for ax in (ax1, ax2):
            for when, label in job.markers:
                ax.axvline(when, color="grey", ls="--", lw=0.8)
                if ax is ax1:
                    ax.annotate(
                        label,
                        (when, 1),
                        xycoords=("data", "axes fraction"),
                        fontsize=8,
                        rotation=90,
                        va="top",
                        ha="right",
                        color="grey",
                    )
            ax.grid(alpha=0.3)
        ax1.set_title(f"{name}: RMSE je Monat (00Z-Läufe)", fontsize=10)
        ax1.set_ylabel(unit)
        ax1.legend(fontsize=8, ncol=2)
        ax2.set_title("Skill gegen Persistenz", fontsize=10)
        ax2.set_ylabel("1 - MSE/MSE_Pers.")
        ax2.legend(fontsize=8)
        ax2.xaxis.set_major_locator(mdates.YearLocator())
        fig.savefig(out / f"monatlich_{var}.png", dpi=110)
        plt.close(fig)


def main() -> None:
    p = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    p.add_argument("--cache", type=Path, default=Path.home() / ".cache/predictiondb/gfs-analysen")
    args = p.parse_args()
    job = Job.load(JOB)
    started = datetime.now(UTC)
    result = asyncio.run(collect(job, args.cache))
    period_rows, season_rows, month_rows = summarize(result["acc"], job)
    tab = OUT / "tabellen"
    tab.mkdir(exist_ok=True)
    write_csv(tab / "fehler.csv", period_rows)
    write_csv(tab / "fehler_jahreszeit.csv", season_rows)
    write_csv(tab / "monatlich.csv", month_rows)
    write_csv(tab / "trend.csv", trend(month_rows, job))
    for kind in ("met", "sol"):
        with (tab / f"stationen_{kind}.csv").open("w", newline="") as fh:
            sw = csv.writer(fh)
            sw.writerow(["dwd_id", "name", "lat", "lon", "hoehe_m"])
            for s in result[kind]:
                sw.writerow([s.sid, s.name, s.lat, s.lon, s.elev])
    plot(job, period_rows, season_rows, month_rows)
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=False
    )
    info = {
        "job": "gfs-v16-jahre",
        "job_sha256": job.sha256,
        "git_commit": commit.stdout.strip(),
        "gestartet": started.isoformat(timespec="seconds"),
        "beendet": datetime.now(UTC).isoformat(timespec="seconds"),
        "zeitraeume": {k: [a.isoformat(), b.isoformat()] for k, (a, b) in job.periods.items()},
        "laeufe_utc": job.runs,
        "horizonte_h": job.leads,
        "stationen_wetter": len(result["met"]),
        "stationen_strahlung": len(result["sol"]),
        "modellschritte": result["n_jobs"],
        "schritte_nicht_im_archiv": result["missing"],
        "schritte_zeitlich_interpoliert": result["interpolated"],
        "fehlerpaare": int(
            sum(rec[0] for per_run in result["acc"].values() for rec in per_run.values())
        ),
    }
    (tab / "lauf.json").write_text(json.dumps(info, indent=1, ensure_ascii=False))
    print(json.dumps(info, indent=1, ensure_ascii=False))


if __name__ == "__main__":
    main()
