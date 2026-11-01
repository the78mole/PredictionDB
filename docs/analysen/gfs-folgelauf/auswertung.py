"""GFS-Folgelauf: Wie viel genauer ist ein Lauf als der 6 h ältere, zur selben Zielzeit?

Auftrag und Parameter stehen in ``job.yaml`` (docs/ARCHITEKTUR.md 7.9), die Ergebnisse liegen
daneben (README.md, tabellen/, grafiken/). Aufruf aus dem Repository-Wurzelverzeichnis::

    uv run --with matplotlib python docs/analysen/gfs-folgelauf/auswertung.py

Für jeden Lauf R und Horizont h wird die Prognose von R für R+h mit der des Laufs R-6h
(Horizont h+6) für dieselbe Zielzeit verglichen, an denselben Stationen und Stunden. Kennzahl ist
der Gewinn 1 - MSE_neu/MSE_alt mit Bootstrap über die Läufe.

GFS kommt aus dem AWS-Archiv (``.idx`` + Range, zugeschnitten zwischengespeichert, nur native
Schritte, Zeitinterpolation erst beim Abruf), die Messungen aus den DWD-Stundenwerten.
Datenzugriff wie in ``docs/analysen/gfs-v16-jahre``.
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
    period: tuple[datetime, datetime]
    runs: tuple[int, ...]
    leads: tuple[int, ...]
    shift: int
    min_completeness: float
    day_ahead: tuple[int, tuple[int, ...]]
    sha256: str

    @classmethod
    def load(cls, path: Path) -> Job:
        raw = path.read_bytes()
        j = yaml.safe_load(raw)
        fc, me = j["inputs"]["forecast"], j["method"]
        a, b = fc["period"]["from"], fc["period"]["to"]
        return cls(
            period=(
                datetime(a.year, a.month, a.day, tzinfo=UTC),
                datetime(b.year, b.month, b.day, 18, tzinfo=UTC),
            ),
            runs=tuple(fc["runs"]),
            leads=tuple(me["horizons_h"]),
            shift=int(me["pairs"]["shift_h"]),
            min_completeness=j["inputs"]["observations"]["stations"]["min_completeness"],
            day_ahead=(int(me["day_ahead"]["run"]), tuple(me["day_ahead"]["horizons_h"])),
            sha256=hashlib.sha256(raw).hexdigest(),
        )


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
            # atomar ersetzen: derselbe Schritt kann als "neu" und als "vorher" gleichzeitig
            # angefragt werden
            for path, content in ((grib, data), (keys, json.dumps(names).encode())):
                tmp = path.with_name(f"{path.name}.{id(content)}.tmp")
                tmp.write_bytes(content)
                tmp.replace(path)
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

#: je Gruppe und (neuem) Lauf: n, Summe Fehler neu, Summe Fehler² neu, Summe Fehler alt,
#: Summe Fehler² alt, jeweils auf denselben Paaren
Acc = dict[tuple[Any, ...], dict[str, NDArray[np.float64]]]
ALL = "alle"
DAY_AHEAD = "Day-Ahead"
MEAN = "Deutschlandmittel"


def runs_between(a: datetime, b: datetime, hours: tuple[int, ...]) -> list[datetime]:
    out, t = [], a
    while t <= b:
        if t.hour in hours:
            out.append(t)
        t += timedelta(hours=6)
    return out


def add(acc: Acc, key: tuple[Any, ...], tag: str, new: NDArray[Any], old: NDArray[Any]) -> None:
    ok = np.isfinite(new) & np.isfinite(old)
    if not ok.any():
        return
    a, b = new[ok], old[ok]
    rec = acc[key].setdefault(tag, np.zeros(5))
    rec += [a.size, a.sum(), (a**2).sum(), b.sum(), (b**2).sum()]


def model_values(
    f: dict[str, Field],
    lead: int,
    met: tuple[NDArray[Any], NDArray[Any], NDArray[Any]],
    sol: tuple[NDArray[Any], NDArray[Any]],
) -> dict[str, NDArray[np.float64]]:
    mlat, mlon, elev = met
    out: dict[str, NDArray[np.float64]] = {}
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
        out["t2m"] = at["t"] + LAPSE * (at["z"] - elev) - 273.15
        out["td2m"] = at["td"] - 273.15
        out["ff10"] = np.hypot(at["u"], at["v"])
        out["mslp"] = at["p"] / 100
    g = pick(f, "DSWRF", "surface", lead) if lead >= 6 else None
    if g is not None:
        out["ghi"] = g.at(*sol)
    return out


async def collect(job: Job, cache: Path) -> dict[str, Any]:
    a, b = job.period
    clock = Clock(a - timedelta(days=1), b + timedelta(hours=max(job.leads) + 6))
    periods = {"zeitraum": job.period}
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(180.0, connect=20.0), limits=httpx.Limits(max_connections=48)
    ) as client:
        met_all, sol_all, obs = await load_observations(client, cache, clock)
        q = job.min_completeness
        met = [
            s
            for sid, s in sorted(met_all.items())
            if BBOX.south <= s.lat <= BBOX.north
            and all(complete_enough(obs[c][sid], clock, periods, q) for c in MET)
        ]
        sol = [
            s
            for sid, s in sorted(sol_all.items())
            if BBOX.south <= s.lat <= BBOX.north
            and complete_enough(obs["ST"][sid], clock, periods, q)
        ]
        print(f"Stationen: {len(met)} Wetter, {len(sol)} Strahlung", flush=True)
        mpos = (
            np.array([s.lat for s in met]),
            np.array([s.lon for s in met]),
            np.array([s.elev for s in met]),
        )
        spos = (np.array([s.lat for s in sol]), np.array([s.lon for s in sol]))
        mobs: dict[str, NDArray[Any]] = {
            var: np.stack([obs[code][s.sid] for s in met]) for var, code in MET_VARS
        }
        mobs["ghi"] = np.stack([obs["ST"][s.sid] for s in sol]) * 10000 / 3600  # J/cm² -> W/m²

        gfs = GfsArchive(client, cache)
        acc: Acc = defaultdict(dict)
        da_run, da_leads = job.day_ahead

        def observed(var: str, vi: int) -> NDArray[Any]:
            if var != "ghi":
                return mobs[var][:, vi]
            win = mobs["ghi"][:, vi - 5 : vi + 1]
            o = np.nanmean(win, axis=1)
            o[np.isnan(win).any(axis=1)] = np.nan
            return o

        async def one(run: datetime, lead: int) -> None:
            prev = run - timedelta(hours=job.shift)
            fn, fo = await asyncio.gather(
                gfs.at(run, lead, "neu"), gfs.at(prev, lead + job.shift, "vorher")
            )
            if fn is None or fo is None:
                return
            valid = run + timedelta(hours=lead)
            vi = clock.idx(valid)
            new = model_values(fn, lead, mpos, spos)
            old = model_values(fo, lead + job.shift, mpos, spos)
            tag = f"{run:%Y%m%d%H}"
            subs = [ALL, SEASONS[valid.month], f"{run:%H}Z"]
            for var in VARIABLES:
                if var not in new or var not in old:
                    continue
                o = observed(var, vi)
                en, eo = new[var] - o, old[var] - o
                if var == "ghi":  # reine Nachtfenster zählen nicht
                    day = (o > 0) | (new[var] > 0) | (old[var] > 0)
                    en, eo = np.where(day, en, np.nan), np.where(day, eo, np.nan)
                # Mittel über alle Stationen: lokale Fehler heben sich auf, übrig bleibt der
                # großräumige Teil, wie bei einer Prognose für ganz Deutschland
                both = np.isfinite(en) & np.isfinite(eo)
                mn, mo = (np.array([e[both].mean()]) if both.any() else en[:0] for e in (en, eo))
                for sub in subs:
                    add(acc, (var, lead, sub), tag, en, eo)
                add(acc, (var, lead, MEAN), tag, mn, mo)
                if run.hour == da_run and lead in da_leads:
                    for sub in (ALL, SEASONS[valid.month]):
                        add(acc, (var, DAY_AHEAD, sub), tag, en, eo)
                    add(acc, (var, DAY_AHEAD, MEAN), tag, mn, mo)

        jobs = [(r, lead) for r in runs_between(a, b, job.runs) for lead in job.leads]
        for i in range(0, len(jobs), 48):
            await asyncio.gather(*(one(*j) for j in jobs[i : i + 48]))
            if i % 2400 == 0:
                print(f"{i}/{len(jobs)} Paare", flush=True)
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
    n, sn, ssn, so, sso = m.sum(axis=0)
    out: dict[str, Any] = {
        "n": int(n),
        "rmse_neu": float(np.sqrt(ssn / n)),
        "rmse_vorher": float(np.sqrt(sso / n)),
        "bias_neu": sn / n,
        "bias_vorher": so / n,
        "gewinn": 1 - ssn / sso,
    }
    boot = m[rng.integers(0, len(m), (1000, len(m)))].sum(axis=1)
    with np.errstate(invalid="ignore", divide="ignore"):
        g = 1 - boot[:, 2] / boot[:, 4]
    out["gewinn_ki_unten"], out["gewinn_ki_oben"] = np.nanpercentile(g, [2.5, 97.5])
    return out


def summarize(acc: Acc) -> list[dict[str, Any]]:
    rng = np.random.default_rng(42)
    rows = [
        {"groesse": var, "horizont_h": lead, "gruppe": sub, **stats(per_run, rng)}
        for (var, lead, sub), per_run in acc.items()
    ]

    def order(r: dict[str, Any]) -> tuple[Any, ...]:
        sub = r["gruppe"]
        rank = (
            0
            if sub == ALL
            else 1
            if sub == MEAN
            else 2 + SEASON_ORDER.index(sub)
            if sub in SEASON_ORDER
            else 9
        )
        lead = r["horizont_h"]
        return (
            r["groesse"],
            isinstance(lead, str),
            lead if isinstance(lead, int) else 0,
            rank,
            sub,
        )

    return sorted(rows, key=order)


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


def plot(job: Job, rows: list[dict[str, Any]]) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    out = OUT / "grafiken"
    out.mkdir(exist_ok=True)
    leads = list(job.leads)

    def series(var: str, sub: str, key: str) -> list[float]:
        idx = {
            r["horizont_h"]: r
            for r in rows
            if r["groesse"] == var and r["gruppe"] == sub and isinstance(r["horizont_h"], int)
        }
        return [float(idx[h][key]) if h in idx else np.nan for h in leads]

    # Gewinn je Horizont, alle Läufe und je Jahreszeit
    fig, axes = plt.subplots(1, 5, figsize=(20, 4.2), constrained_layout=True)
    colors = dict(zip(SEASON_ORDER, ("#2471a3", "#27ae60", "#e67e22", "#8e44ad"), strict=True))
    for ax, (var, (name, _)) in zip(axes, VARIABLES.items(), strict=True):
        g, lo, hi = (series(var, ALL, k) for k in ("gewinn", "gewinn_ki_unten", "gewinn_ki_oben"))
        ax.fill_between(leads, lo, hi, color="black", alpha=0.12)
        ax.plot(leads, g, "o-", color="black", lw=2, label="alle Stationen")
        ax.plot(leads, series(var, MEAN, "gewinn"), "o--", color="#c0392b", lw=2, label=MEAN)
        for season, col in colors.items():
            ax.plot(leads, series(var, season, "gewinn"), "-", color=col, lw=1, label=season)
        ax.axhline(0, color="grey", lw=0.8)
        ax.set_title(name, fontsize=10)
        ax.set_xlabel("Horizont des neuen Laufs (h)")
        ax.grid(alpha=0.3)
    axes[0].set_ylabel("Gewinn 1 - MSE_neu/MSE_vorher")
    axes[0].legend(fontsize=8)
    fig.savefig(out / "gewinn.png", dpi=110)
    plt.close(fig)

    # RMSE neu gegen vorher
    fig, axes = plt.subplots(1, 5, figsize=(20, 4.2), constrained_layout=True)
    for ax, (var, (name, unit)) in zip(axes, VARIABLES.items(), strict=True):
        ax.plot(leads, series(var, ALL, "rmse_neu"), "o-", color="#c0392b", label="neuer Lauf")
        ax.plot(
            leads, series(var, ALL, "rmse_vorher"), "s--", color="#2471a3", label="Lauf 6 h älter"
        )
        ax.set_title(name, fontsize=10)
        ax.set_xlabel("Horizont des neuen Laufs (h)")
        ax.set_ylabel(unit)
        ax.grid(alpha=0.3)
    axes[0].legend(fontsize=8)
    fig.savefig(out / "rmse.png", dpi=110)
    plt.close(fig)


def main() -> None:
    p = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    p.add_argument("--cache", type=Path, default=Path.home() / ".cache/predictiondb/gfs-analysen")
    args = p.parse_args()
    job = Job.load(JOB)
    started = datetime.now(UTC)
    result = asyncio.run(collect(job, args.cache))
    rows = summarize(result["acc"])
    tab = OUT / "tabellen"
    tab.mkdir(exist_ok=True)
    write_csv(tab / "gewinn.csv", rows)
    for kind in ("met", "sol"):
        with (tab / f"stationen_{kind}.csv").open("w", newline="") as fh:
            sw = csv.writer(fh)
            sw.writerow(["dwd_id", "name", "lat", "lon", "hoehe_m"])
            for s in result[kind]:
                sw.writerow([s.sid, s.name, s.lat, s.lon, s.elev])
    plot(job, rows)
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=False
    )
    info = {
        "job": "gfs-folgelauf",
        "job_sha256": job.sha256,
        "git_commit": commit.stdout.strip(),
        "gestartet": started.isoformat(timespec="seconds"),
        "beendet": datetime.now(UTC).isoformat(timespec="seconds"),
        "zeitraum": [t.isoformat() for t in job.period],
        "laeufe_utc": job.runs,
        "horizonte_h": job.leads,
        "versatz_h": job.shift,
        "stationen_wetter": len(result["met"]),
        "stationen_strahlung": len(result["sol"]),
        "laufpaare_mal_horizonte": result["n_jobs"],
        "schritte_nicht_im_archiv": result["missing"],
        "schritte_zeitlich_interpoliert": result["interpolated"],
        "fehlerpaare": int(
            sum(
                rec[0]
                for (_, lead, sub), per_run in result["acc"].items()
                if sub == ALL and isinstance(lead, int)
                for rec in per_run.values()
            )
        ),
    }
    (tab / "lauf.json").write_text(json.dumps(info, indent=1, ensure_ascii=False))
    print(json.dumps(info, indent=1, ensure_ascii=False))


if __name__ == "__main__":
    main()
