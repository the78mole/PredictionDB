"""GFS v15 gegen v16: Prognosefehler an DWD-Stationen je Vorhersagehorizont.

Einmalige Auswertung, die Ergebnisse liegen neben diesem Skript (README.md, tabellen/,
grafiken/). Aufruf aus dem Repository-Wurzelverzeichnis::

    uv run --with matplotlib python docs/analysen/gfs-v15-v16/auswertung.py

Daten:

- GFS 0,25° aus dem AWS-Archiv ``noaa-gfs-bdp-pds`` per ``.idx`` und Range-Anfragen, nur die
  ausgewerteten Felder und Schritte, auf Deutschland zugeschnitten. v15 liegt dort vom 01.01.
  bis 22.03.2021 06Z (ohne ``atmos/`` im Pfad), v16 ab 22.03.2021 12Z.
- Messungen: DWD Climate Data Center, Stundenwerte (opendata.dwd.de): Lufttemperatur,
  Taupunkt, Wind, Luftdruck und Globalstrahlung.

Der Zwischenspeicher enthält nur native Modellschritte (zugeschnittenes GRIB2). Räumlich wird
bilinear auf die Stationen interpoliert; fehlt ein Horizont im Archiv (v15 hatte bis Ende
Februar 2021 teils nur 3-stündliche Schritte), wird erst beim Abruf linear in der Zeit zwischen
den nativen Nachbarschritten interpoliert.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import io
import json
import re
import zipfile
from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import numpy as np
from numpy.typing import NDArray

from gribkit import BBox, MessageSpec, crop, iter_messages, merge_ranges, parse_idx, select

GFS = "https://noaa-gfs-bdp-pds.s3.amazonaws.com"
DWD = "https://opendata.dwd.de/climate_environment/CDC/observations_germany/climate/hourly"
V16_START = datetime(2021, 3, 22, 12, tzinfo=UTC)
OUT = Path(__file__).parent

#: Auswertezeiträume in derselben Jahreszeit; v15 gibt es im AWS-Archiv nur 2021
PERIODS: dict[str, tuple[datetime, datetime]] = {
    "v15 2021": (datetime(2021, 1, 1, tzinfo=UTC), datetime(2021, 3, 22, 0, tzinfo=UTC)),
    "v16 2022": (datetime(2022, 1, 1, tzinfo=UTC), datetime(2022, 3, 22, 0, tzinfo=UTC)),
    "v16 2023": (datetime(2023, 1, 1, tzinfo=UTC), datetime(2023, 3, 22, 0, tzinfo=UTC)),
}
RUN_HOURS = (0, 12)
LEADS = (0, 6, 12, 24, 36, 72, 96, 240)
LEAD_LABEL = {0: "Analyse", 240: "10 Tage"}

FIELDS = [
    MessageSpec("TMP", "2 m above ground"),
    MessageSpec("DPT", "2 m above ground"),
    MessageSpec("UGRD", "10 m above ground"),
    MessageSpec("VGRD", "10 m above ground"),
    MessageSpec("PRMSL", "mean sea level"),
    MessageSpec("HGT", "surface"),
    MessageSpec("DSWRF", "surface", "ave"),
]
#: ausgewertete Größen: Name, Einheit
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
LAPSE = 0.0065  # K/m, Höhenkorrektur der 2-m-Temperatur auf Stationshöhe
BBOX = BBox(55.5, 47.0, 5.5, 15.5)
MIN_COVERAGE = 0.8  # Anteil vorhandener Stundenwerte je Zeitraum, damit eine Station mitzählt

Obs = dict[str, dict[str, float]]  # Stunde (JJJJMMTTHH) -> Wert, je Station


@dataclass(frozen=True)
class Station:
    sid: str
    name: str
    lat: float
    lon: float
    elev: float


# --- Messungen (DWD) -----------------------------------------------------------------------------


def obs_window() -> tuple[str, str]:
    first = min(a for a, _ in PERIODS.values())
    last = max(b for _, b in PERIODS.values()) + timedelta(hours=max(LEADS))
    return f"{first:%Y%m%d%H}", f"{last:%Y%m%d%H}"


def parse_stations(text: str) -> dict[str, tuple[str, str, Station]]:
    """``*_Beschreibung_Stationen.txt``: Kennung -> (von, bis, Station)."""
    out = {}
    lines = text.splitlines()
    tail = 2 if "Abgabe" in lines[0] else 1  # Bundesland (und Abgabe) nach dem Namen
    for line in lines[2:]:
        p = line.split()
        if len(p) < 7 + tail or not p[0].isdigit():
            continue
        name = " ".join(p[6:-tail])
        out[p[0]] = (p[1], p[2], Station(p[0], name, float(p[4]), float(p[5]), float(p[3])))
    return out


def parse_product(text: str, column: str, start: str, end: str) -> dict[str, float]:
    """Werte einer Spalte im Zeitraum; Solardaten (``JJJJMMTTHH:MM``) auf die volle Stunde."""
    lines = text.splitlines()
    head = [c.strip() for c in lines[0].split(";")]
    col = head.index(column)
    pos = text.find(f";{start[:8]}")
    body = text[pos:].splitlines() if pos >= 0 else lines[1:]
    out: dict[str, float] = {}
    for line in body[1:] if pos >= 0 else body:
        p = line.split(";")
        if len(p) <= col:
            continue
        stamp = p[1].strip()
        if ":" in stamp:  # Ende des Stundenintervalls, Minuten runden
            t = datetime.strptime(stamp + "+0000", "%Y%m%d%H:%M%z") + timedelta(minutes=30)
            stamp = f"{t:%Y%m%d%H}"
        if stamp < start:
            continue
        if stamp > end:
            break
        value = float(p[col])
        if value > -999:
            out[stamp] = value
    return out


async def dwd_listing(client: httpx.AsyncClient, path: str) -> list[str]:
    text = (await client.get(f"{DWD}/{path}/")).text
    return re.findall(r'href="([^"]+\.(?:zip|txt))"', text)


async def load_observations(
    client: httpx.AsyncClient, cache: Path
) -> tuple[list[Station], list[Station], dict[str, Obs]]:
    start, end = obs_window()
    sem = asyncio.Semaphore(12)
    meta: dict[str, dict[str, Station]] = {}
    files: dict[str, dict[str, str]] = {}
    for code, (folder, _, _) in PRODUCTS.items():
        sub = folder if code == "ST" else f"{folder}/historical"
        names = await dwd_listing(client, sub)
        desc = next(n for n in names if n.endswith("Stationen.txt"))
        resp = await client.get(f"{DWD}/{sub}/{desc}")
        stations = parse_stations(resp.content.decode("latin-1"))
        meta[code] = {}
        files[code] = {}
        for n in names:
            m = re.fullmatch(
                rf"stundenwerte_{code}_(\d{{5}})_(?:(\d{{8}})_(\d{{8}})_hist|row)\.zip", n
            )
            if not m or m[1] not in stations:
                continue
            if m[2] and not (m[2] <= start[:8] and m[3] >= end[:8]):
                continue
            files[code][m[1]] = f"{DWD}/{sub}/{n}"
            meta[code][m[1]] = stations[m[1]][2]

    met_ids = sorted(set.intersection(*(set(files[c]) for c in MET)))
    sol_ids = sorted(files["ST"])
    obs: dict[str, Obs] = {c: {} for c in PRODUCTS}

    async def one(code: str, sid: str) -> None:
        path = cache / "dwd" / f"{code}_{sid}.json"
        if not path.exists():
            async with sem:
                resp = await client.get(files[code][sid])
            resp.raise_for_status()
            with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
                name = next(n for n in zf.namelist() if n.startswith("produkt_"))
                text = zf.read(name).decode("latin-1")
            values = await asyncio.to_thread(parse_product, text, PRODUCTS[code][1], start, end)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(values))
        obs[code][sid] = json.loads(path.read_text())

    jobs = [one(c, s) for c in MET for s in met_ids] + [one("ST", s) for s in sol_ids]
    await asyncio.gather(*jobs)

    def covered(code: str, sid: str) -> bool:
        for a, b in PERIODS.values():
            hours = int((b - a).total_seconds() // 3600) + 1
            have = sum(1 for h in obs[code][sid] if f"{a:%Y%m%d%H}" <= h <= f"{b:%Y%m%d%H}")
            if have / hours < MIN_COVERAGE:
                return False
        return True

    met = [meta["TU"][s] for s in met_ids if all(covered(c, s) for c in MET)]
    sol = [meta["ST"][s] for s in sol_ids if covered("ST", s)]
    inside = [s for s in met + sol if BBOX.south <= s.lat <= BBOX.north]
    met = [s for s in met if s in inside]
    sol = [s for s in sol if s in inside]
    return met, sol, obs


# --- GFS -----------------------------------------------------------------------------------------


def gfs_url(run: datetime, step: int) -> str:
    sub = "atmos/" if run >= V16_START else ""
    return f"{GFS}/gfs.{run:%Y%m%d}/{run:%H}/{sub}gfs.t{run:%H}z.pgrb2.0p25.f{step:03d}"


@dataclass
class Field:
    values: NDArray[np.float64]
    lat0: float
    lon0: float
    dlat: float  # positiv, Zeilen Nord -> Süd
    dlon: float

    def at(self, lats: NDArray[Any], lons: NDArray[Any]) -> NDArray[np.float64]:
        """Bilinear an den Stationen."""
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
    """Native Schritte laden, zugeschnitten zwischenspeichern, beim Abruf interpolieren."""

    def __init__(self, client: httpx.AsyncClient, cache: Path) -> None:
        self.client = client
        self.cache = cache / "gfs"
        self.sem = asyncio.Semaphore(24)
        self.missing: dict[str, int] = defaultdict(int)

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

    async def at(self, run: datetime, lead: int, period: str) -> dict[str, Field] | None:
        """Felder zum Horizont ``lead``. Fehlt der Schritt, werden Momentanwerte linear
        zwischen den nativen Nachbarschritten im 3-h-Raster interpoliert (Mittelwerte nicht)."""
        f = await self.native(run, lead)
        if f is not None:
            return f
        self.missing[period] += 1
        lo, hi = lead // 3 * 3, -(-lead // 3) * 3
        if lo == hi:
            return None
        a, b = await asyncio.gather(self.native(run, lo), self.native(run, hi))
        if a is None or b is None:
            return None
        w = (lead - lo) / (hi - lo)
        return {
            k: replace(a[k], values=(1 - w) * a[k].values + w * b[k].values)
            for k in a
            if k in b and "ave" not in k
        }


def pick(fields: dict[str, Field], var: str, level: str, lead: int | None = None) -> Field | None:
    """Momentanwert (``lead`` None) oder 6-h-Mittel, das zum Horizont endet."""
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


def runs(start: datetime, end: datetime) -> list[datetime]:
    out, t = [], start
    while t <= end:
        if t.hour in RUN_HOURS:
            out.append(t)
        t += timedelta(hours=6)
    return out


def coords(stations: list[Station]) -> tuple[NDArray[np.float64], NDArray[np.float64]]:
    return np.array([s.lat for s in stations]), np.array([s.lon for s in stations])


def model_met(
    f: dict[str, Field], stations: list[Station]
) -> dict[str, NDArray[np.float64]] | None:
    lats, lons = coords(stations)
    names = {
        "t2m": ("TMP", "2 m above ground"),
        "td2m": ("DPT", "2 m above ground"),
        "u": ("UGRD", "10 m above ground"),
        "v": ("VGRD", "10 m above ground"),
        "mslp": ("PRMSL", "mean sea level"),
        "orog": ("HGT", "surface"),
    }
    got = {k: pick(f, *spec) for k, spec in names.items()}
    if any(v is None for v in got.values()):
        return None
    at = {k: v.at(lats, lons) for k, v in got.items() if v is not None}
    elev = np.array([s.elev for s in stations])
    return {
        "t2m": at["t2m"] + LAPSE * (at["orog"] - elev) - 273.15,
        "td2m": at["td2m"] - 273.15,
        "ff10": np.hypot(at["u"], at["v"]),
        "mslp": at["mslp"] / 100,
    }


def bootstrap_rmse(
    err: NDArray[np.float64], groups: NDArray[Any], rng: np.random.Generator
) -> tuple[float, float]:
    """95-%-Intervall der RMSE, Bootstrap über Modellläufe (Fehler eines Laufs sind korreliert)."""
    uniq, inv = np.unique(groups, return_inverse=True)
    sq = np.bincount(inv, weights=err**2)
    n = np.bincount(inv)
    picks = rng.integers(0, len(uniq), (500, len(uniq)))
    boot = np.sqrt(sq[picks].sum(axis=1) / n[picks].sum(axis=1))
    lo, hi = np.percentile(boot, [2.5, 97.5])
    return float(lo), float(hi)


async def collect(cache: Path) -> dict[str, Any]:
    timeout = httpx.Timeout(180.0, connect=20.0)
    async with httpx.AsyncClient(
        timeout=timeout, limits=httpx.Limits(max_connections=48)
    ) as client:
        met, sol, obs = await load_observations(client, cache)
        print(f"Stationen: {len(met)} Wetter, {len(sol)} Strahlung", flush=True)
        gfs = GfsArchive(client, cache)
        errors: dict[tuple[str, str, int], list[tuple[float, str]]] = defaultdict(list)
        sol_lats, sol_lons = coords(sol)

        async def one(period: str, run: datetime, lead: int) -> None:
            f = await gfs.at(run, lead, period)
            if f is None:
                return
            tag = f"{run:%Y%m%d%H}"
            valid = run + timedelta(hours=lead)
            stamp = f"{valid:%Y%m%d%H}"
            m = model_met(f, met)
            if m is not None:
                for var, code in (("t2m", "TU"), ("td2m", "TD"), ("ff10", "FF"), ("mslp", "P0")):
                    for k, s in enumerate(met):
                        o = obs[code][s.sid].get(stamp)
                        if o is not None and np.isfinite(m[var][k]):
                            errors[(period, var, lead)].append((float(m[var][k] - o), tag))
            g = pick(f, "DSWRF", "surface", lead) if lead >= 6 else None
            if g is not None:
                model = g.at(sol_lats, sol_lons)
                hours = [f"{valid - timedelta(hours=h):%Y%m%d%H}" for h in range(6)]
                for k, s in enumerate(sol):
                    vals = [obs["ST"][s.sid].get(h) for h in hours]
                    if any(v is None for v in vals):
                        continue
                    o = sum(v for v in vals if v is not None) / 6 * 10000 / 3600  # J/cm² -> W/m²
                    if o > 0 or model[k] > 0:  # reine Nachtfenster zählen nicht
                        errors[(period, "ghi", lead)].append((float(model[k] - o), tag))

        for period, (start, end) in PERIODS.items():
            jobs = [(period, r, lead) for r in runs(start, end) for lead in LEADS]
            for i in range(0, len(jobs), 48):
                await asyncio.gather(*(one(*j) for j in jobs[i : i + 48]))
            print(f"{period}: {len(jobs)} Schritte", flush=True)
        return {"met": met, "sol": sol, "errors": errors, "missing": dict(gfs.missing)}


def summarize(errors: dict[tuple[str, str, int], list[tuple[float, str]]]) -> list[dict[str, Any]]:
    rng = np.random.default_rng(42)
    out = []
    for (period, var, lead), vals in sorted(errors.items()):
        err = np.array([v for v, _ in vals])
        lo, hi = bootstrap_rmse(err, np.array([g for _, g in vals]), rng)
        out.append(
            {
                "zeitraum": period,
                "groesse": var,
                "horizont_h": lead,
                "n": int(err.size),
                "bias": float(err.mean()),
                "mae": float(np.abs(err).mean()),
                "rmse": float(np.sqrt(np.mean(err**2))),
                "rmse_ki_unten": lo,
                "rmse_ki_oben": hi,
            }
        )
    return out


def lead_label(lead: int) -> str:
    return LEAD_LABEL.get(lead, f"{lead} h")


def write_outputs(summary: list[dict[str, Any]], result: dict[str, Any]) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    (OUT / "tabellen").mkdir(exist_ok=True)
    (OUT / "grafiken").mkdir(exist_ok=True)
    with (OUT / "tabellen" / "fehler.csv").open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(summary[0]))
        w.writeheader()
        for r in summary:
            w.writerow({k: round(v, 3) if isinstance(v, float) else v for k, v in r.items()})
    for kind in ("met", "sol"):
        with (OUT / "tabellen" / f"stationen_{kind}.csv").open("w", newline="") as fh:
            sw = csv.writer(fh)
            sw.writerow(["dwd_id", "name", "lat", "lon", "hoehe_m"])
            for s in result[kind]:
                sw.writerow([s.sid, s.name, s.lat, s.lon, s.elev])

    by = {(r["zeitraum"], r["groesse"], r["horizont_h"]): r for r in summary}
    colors = {"v15 2021": "#c0392b", "v16 2022": "#2471a3", "v16 2023": "#5dade2"}
    x = np.arange(len(LEADS))
    for var, (name, unit) in VARIABLES.items():
        fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(11, 4), constrained_layout=True)
        for period in PERIODS:
            pts = [by.get((period, var, lead)) for lead in LEADS]
            col = colors[period]

            def series(key: str, pts: list[dict[str, Any] | None] = pts) -> list[float]:
                return [p[key] if p else np.nan for p in pts]

            ax1.plot(x, series("rmse"), "o-", color=col, label=period)
            ax1.fill_between(
                x, series("rmse_ki_unten"), series("rmse_ki_oben"), color=col, alpha=0.2
            )
            ax2.plot(x, series("bias"), "o-", color=col, label=period)
        for ax, title in ((ax1, "RMSE (95-%-Intervall)"), (ax2, "Bias (Modell - Messung)")):
            ax.set_xticks(x, [lead_label(lead) for lead in LEADS], rotation=30)
            ax.set_title(f"{name}: {title}", fontsize=10)
            ax.set_ylabel(unit)
            ax.grid(alpha=0.3)
        ax2.axhline(0, color="black", lw=0.8)
        ax1.legend()
        fig.savefig(OUT / "grafiken" / f"{var}.png", dpi=120)
        plt.close(fig)


def main() -> None:
    p = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    p.add_argument("--cache", type=Path, default=Path.home() / ".cache/predictiondb/gfs-v15-v16")
    args = p.parse_args()
    result = asyncio.run(collect(args.cache))
    summary = summarize(result["errors"])
    write_outputs(summary, result)
    info = {
        "erstellt": datetime.now(UTC).isoformat(timespec="seconds"),
        "zeitraeume": {k: [a.isoformat(), b.isoformat()] for k, (a, b) in PERIODS.items()},
        "laeufe_utc": RUN_HOURS,
        "horizonte_h": LEADS,
        "stationen_wetter": len(result["met"]),
        "stationen_strahlung": len(result["sol"]),
        "zeitlich_interpolierte_schritte": result["missing"],
        "fehlerpaare": sum(len(v) for v in result["errors"].values()),
    }
    (OUT / "tabellen" / "lauf.json").write_text(json.dumps(info, indent=1, ensure_ascii=False))
    print(json.dumps(info, indent=1, ensure_ascii=False))


if __name__ == "__main__":
    main()
