"""Im- und Export und Preis DE aus DE-Erzeugung und EE-Erzeugung der Nachbarländer.

Liest den Cache von abruf.py. Auf 3-h-Blöcken des 00Z-GFS-Laufs (f003 bis f024) werden der
Netto-Import DE (Energy-Charts /cbet, + = Import), der Handel je Grenze und der Day-Ahead-Preis
DE-LU mit vier Merkmalssätzen erklärt: nur DE, dazu Wind und PV der Nachbarn als Ist-Wert
(obere Schranke) und aus dem GFS geschätzt (Flächenmittel bzw. gelernte Gewichte wie in
docs/analysen/standortgewichte-lernen). Training 2024, Test 2025 und vertauscht.
Schreibt tabellen/*.csv, tabellen/lauf.json und grafiken/*.png.

Aufruf (siehe README):
    uv run --no-project --with numpy --with scipy --with scikit-learn --with lightgbm \
        --with matplotlib --with pyyaml python -I \
        docs/analysen/austausch-nachbarlaender/auswertung.py --cache /tmp/an
"""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import json
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import yaml
from lightgbm import LGBMRegressor
from numpy.typing import NDArray
from scipy.optimize import nnls

F = NDArray[np.float64]
B = NDArray[np.bool_]
HIER = Path(__file__).resolve().parent
WURZEL = HIER.parents[2]
REGIONEN = WURZEL / "config" / "gefs" / "regions_v2.json.gz"
JOB = yaml.safe_load((HIER / "job.yaml").read_text())
NACHBARN = ("at", "be", "ch", "cz", "dk", "fr", "nl", "no", "pl", "se")
#: Land -> Zonen in regions_v2 (Teilzonen werden nach Zellenzahl zusammengelegt)
ZONEN = {
    "de": ["DE"],
    "at": ["AT"],
    "be": ["BE"],
    "ch": ["CH"],
    "cz": ["CZ"],
    "dk": ["DK1", "DK2"],
    "fr": ["FR"],
    "nl": ["NL"],
    "no": ["NO1", "NO2", "NO3", "NO4", "NO5"],
    "pl": ["PL"],
    "se": ["SE1", "SE2", "SE3", "SE4"],
}
#: Seegebiete für Wind auf See: (Süd, Nord, West, Ost) in Grad
SEE = {
    "fr": (46.5, 50.5, -4.0, 1.5),
    "be": (51.0, 52.0, 2.0, 3.5),
    "nl": (51.5, 54.5, 2.5, 7.0),
    "dk": (54.5, 57.0, 6.5, 13.0),
}
#: Technik -> (Reihe in /public_power, Reihe in /installed_power)
TECHNIK = {
    "wind_on": ("Wind onshore", "Wind onshore"),
    "wind_off": ("Wind offshore", "Wind offshore"),
    "pv": ("Solar", "Solar AC"),
}
GRENZEN = {
    "Austria": "at",
    "Belgium": "be",
    "Czech Republic": "cz",
    "Denmark": "dk",
    "France": "fr",
    "Netherlands": "nl",
    "Norway": "no",
    "Poland": "pl",
    "Sweden": "se",
    "Switzerland": "ch",
}
NX, NY = 112, 75
LAT0, LON0, D = 71.5, -10.5, 0.5
LAM = 0.03
ALPHAS = (0.1, 1.0, 10.0, 100.0, 1000.0)

TAGE = np.arange(np.datetime64("2024-01-01"), np.datetime64("2026-01-01"))
ZEITEN = (
    TAGE[:, None].astype("datetime64[h]") + np.arange(3, 25, 3).astype("timedelta64[h]")
).ravel()
#: Block (Schritt - 3 h, Schritt]; 2024 = Jahr, in dem der Block beginnt
FRUEH = np.datetime64("2025-01-01") > ZEITEN - np.timedelta64(3, "h")


def wind_cf(v: F, ein: float = 3.0, nenn: float = 12.0, aus: float = 25.0) -> F:
    """Leistungskurve wie wx_process.features.PowerCurve."""
    cf = np.clip((v**3 - ein**3) / (nenn**3 - ein**3), 0, 1)
    return np.where((v < ein) | (v >= aus), 0.0, cf)


def pv_cf(ghi: F, t_c: F) -> F:
    """PV-Modell wie wx_process.features.PvModel."""
    t_zelle = t_c + ghi * 25 / 800
    return np.clip(0.85 * ghi / 1000 * (1 - 0.004 * (t_zelle - 25)), 0, None)


def r2(y: F, p: F) -> float:
    return float(1 - np.sum((y - p) ** 2) / np.sum((y - np.mean(y)) ** 2))


def mae(y: F, p: F) -> float:
    return float(np.mean(np.abs(y - p)))


def sha256(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def blockmittel(ts: NDArray[np.int64], val: F) -> F:
    """Mittel der Werte, deren Intervall in (Schritt - 3 h, Schritt] beginnt, je Block."""
    z = ZEITEN.astype("datetime64[s]").astype(np.int64)
    a = np.searchsorted(ts, z - 3 * 3600, "left")
    b = np.searchsorted(ts, z, "left")
    cs = np.concatenate([[0.0], np.nancumsum(val)])
    cn = np.concatenate([[0], np.cumsum(~np.isnan(val))])
    n = cn[b] - cn[a]
    out = np.full(len(ZEITEN), np.nan)
    gut = n >= 2
    out[gut] = (cs[b] - cs[a])[gut] / n[gut]
    return out


class Energiedaten:
    def __init__(self, ec: Path) -> None:
        self.ec = ec
        self.fehlend: dict[str, int] = {}

    def _reihe(self, prefix: str, feld: str, name: str) -> tuple[NDArray[np.int64], F] | None:
        ts_l: list[int] = []
        werte: list[float] = []
        for jahr in (2024, 2025):
            d = json.loads((self.ec / f"{prefix}_{jahr}.json").read_text())
            reihe = next((p for p in d[feld] if p["name"] == name), None)
            if reihe is None:
                return None
            ts_l += d["unix_seconds"]
            werte += [np.nan if x is None else float(x) for x in reihe["data"]]
        ts = np.array(ts_l, dtype=np.int64)
        val = np.array(werte, dtype=np.float64)
        self.fehlend[f"{prefix}:{name}"] = int(np.isnan(val).sum())
        return ts, val

    def erzeugung(self, land: str, name: str) -> F | None:
        r = self._reihe(f"pp_{land}", "production_types", name)
        return None if r is None else blockmittel(r[0], r[1])

    def pflicht(self, land: str, name: str) -> F:
        r = self.erzeugung(land, name)
        assert r is not None, (land, name)
        return r

    def kapazitaet(self, land: str, name: str) -> F | None:
        leistung = json.loads((self.ec / f"ipy_{land}.json").read_text())
        reihe = next((p for p in leistung["production_types"] if p["name"] == name), None)
        if reihe is None:
            return None
        paare = [
            (int(j), float(x))
            for j, x in zip(leistung["time"], reihe["data"], strict=True)
            if x is not None and float(x) > 0
        ]
        if not paare:
            return None
        t_kap = np.array([np.datetime64(f"{j}-12-31").astype("datetime64[h]") for j, _ in paare])
        t = ZEITEN.astype("datetime64[h]").astype(np.int64)
        werte = np.array([x for _, x in paare])
        return np.asarray(np.interp(t, t_kap.astype(np.int64), werte) * 1000, dtype=np.float64)

    def handel(self) -> dict[str, F]:
        out: dict[str, F] = {}
        for name in [*GRENZEN, "sum"]:
            r = self._reihe("cbet_de", "countries", name)
            assert r is not None, name
            out[GRENZEN.get(name, "netto")] = blockmittel(r[0], r[1] * 1000)
        return out

    def preis(self) -> F:
        ts_l: list[int] = []
        werte: list[float] = []
        for jahr in (2024, 2025):
            d = json.loads((self.ec / f"price_de_lu_{jahr}.json").read_text())
            ts_l += d["unix_seconds"]
            werte += [np.nan if x is None else float(x) for x in d["price"]]
        return blockmittel(np.array(ts_l, dtype=np.int64), np.array(werte))


@dataclass
class Gebiet:
    ids: NDArray[np.int64]
    anteil: F
    block: NDArray[np.int64]
    nb: int
    pb: F

    @classmethod
    def aus_zellen(cls, ids: NDArray[np.int64], anteil: F) -> Gebiet:
        schluessel = (ids // NX // 2) * 1000 + (ids % NX) // 2
        _, block = np.unique(schluessel, return_inverse=True)
        nb = int(block.max()) + 1
        anteil = anteil / anteil.sum()
        return cls(ids, anteil, block, nb, np.bincount(block, anteil, nb))

    def blockmittel(self, x: F) -> F:
        xb = np.zeros((x.shape[0], self.nb))
        np.add.at(xb.T, self.block, (x[:, self.ids] * self.anteil).T)
        return xb / np.maximum(self.pb, 1e-12)


def land_gebiet(regionen: dict[str, F], land: str) -> Gebiet:
    ids_l, anteil_l = [], []
    for z in ZONEN[land]:
        c = regionen[f"{z}:area"]
        i = np.rint((LAT0 - c[:, 0]) / D).astype(np.int64)
        j = np.rint((c[:, 1] - LON0) / D).astype(np.int64)
        ids_l.append(i * NX + j)
        anteil_l.append(c[:, 2] / c[:, 2].sum() * len(c))  # Teilzone ~ Zellenzahl
    ids = np.concatenate(ids_l)
    anteil = np.concatenate(anteil_l)
    u, inv = np.unique(ids, return_inverse=True)
    return Gebiet.aus_zellen(u, np.bincount(inv, anteil))


def see_gebiet(land: str) -> Gebiet:
    s, n, w, o = SEE[land]
    lat = LAT0 - D * np.arange(NY)
    lon = LON0 + D * np.arange(NX)
    ii, jj = np.meshgrid(
        np.flatnonzero((lat >= s) & (lat <= n)), np.flatnonzero((lon >= w) & (lon <= o))
    )
    ids = (ii * NX + jj).ravel().astype(np.int64)
    return Gebiet.aus_zellen(ids, np.ones(len(ids)))


def anpassen(x: F, y: F, prior: F, lam: float) -> tuple[F, float]:
    """NNLS mit Ridge zur skalierten Vorgabeverteilung; gibt Gewichte und Flächenskala."""
    alpha = float(np.sum((x @ prior) * y) / np.sum((x @ prior) ** 2))
    k = x.shape[1]
    s = np.sqrt(lam * len(y))
    w, _ = nnls(
        np.vstack([x, s * np.eye(k)]), np.concatenate([y, s * alpha * prior]), maxiter=50 * k
    )
    return np.asarray(w, dtype=np.float64), alpha


def kalender() -> F:
    blk = (ZEITEN.astype("datetime64[h]").astype(np.int64) % 24) // 3
    tag = ZEITEN - np.timedelta64(3, "h")
    wt = (tag.astype("datetime64[D]").astype(np.int64) + 3) % 7  # 0 = Montag
    mon = tag.astype("datetime64[M]").astype(np.int64) % 12
    return np.column_stack([blk, wt, mon]).astype(np.float64)


def dummies(k: F) -> F:
    teile = []
    for spalte, n in ((0, 8), (1, 7), (2, 12)):
        teile.append((k[:, spalte][:, None] == np.arange(1, n)).astype(np.float64))
    return np.hstack(teile)


@dataclass
class RidgeModell:
    mu: F
    sd: F
    koeff: F
    achse: float

    def vorhersage(self, x: F) -> F:
        return np.asarray(((x - self.mu) / self.sd) @ self.koeff + self.achse, dtype=np.float64)


def ridge(x: F, y: F, alpha: float) -> RidgeModell:
    """Ridge auf standardisierten Merkmalen, Achsenabschnitt ohne Strafe."""
    mu, sd = x.mean(0), x.std(0) + 1e-9
    xs = (x - mu) / sd
    yc = y - y.mean()
    k = np.linalg.solve(xs.T @ xs + alpha * np.eye(x.shape[1]), xs.T @ yc)
    return RidgeModell(mu, sd, np.asarray(k, dtype=np.float64), float(y.mean()))


def ridge_cv(x: F, y: F, tr: B, monat: NDArray[np.int64]) -> RidgeModell:
    """Alpha aus Validierung: jeder vierte Monat des Trainingsjahres."""
    va, fi = tr & (monat % 4 == 3), tr & (monat % 4 != 3)
    fehler = [
        float(np.mean((ridge(x[fi], y[fi], a).vorhersage(x[va]) - y[va]) ** 2)) for a in ALPHAS
    ]
    return ridge(x[tr], y[tr], ALPHAS[int(np.argmin(fehler))])


def gbm(x: F, y: F, tr: B) -> LGBMRegressor:
    return LGBMRegressor(
        n_estimators=400,
        learning_rate=0.05,
        num_leaves=31,
        min_child_samples=40,
        subsample=0.8,
        subsample_freq=1,
        colsample_bytree=0.8,
        random_state=0,
        verbose=-1,
    ).fit(x[tr], y[tr])


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--cache", type=Path, required=True)
    args = p.parse_args()
    gestartet = datetime.now(UTC)
    gfs_dir, ec_dir = args.cache / "gfs", args.cache / "ec"
    ed = Energiedaten(ec_dir)
    ok = np.repeat(np.load(gfs_dir / "ok.npy"), 8)

    # GFS je Block: Wind = Mittel der CF an beiden Blockgrenzen, PV aus dswrf (Mittel der 3 h)
    def lade(g: str) -> F:
        return np.asarray(np.load(gfs_dir / f"{g}.npy"), dtype=np.float64)

    cfw = wind_cf(np.hypot(lade("u100"), lade("v100")))
    x_wind = ((cfw[:, :-1] + cfw[:, 1:]) / 2).reshape(len(ZEITEN), -1)
    del cfw
    t2m = lade("t2m") - 273.15
    x_pv = pv_cf(lade("dswrf")[:, 1:], (t2m[:, :-1] + t2m[:, 1:]) / 2).reshape(len(ZEITEN), -1)
    del t2m
    ok &= ~np.isnan(x_wind).all(1) & ~np.isnan(x_pv).all(1)
    regionen = {
        r["code"]: np.array(r["cells"], dtype=np.float64).reshape(-1, 3)
        for r in json.loads(gzip.decompress(REGIONEN.read_bytes()))["regions"]
    }
    monat = (ZEITEN - np.timedelta64(3, "h")).astype("datetime64[M]").astype(np.int64) % 12

    # Ist-Werte
    de = {
        "last": ed.pflicht("de", "Load"),
        "wind_on": ed.pflicht("de", "Wind onshore"),
        "wind_off": ed.pflicht("de", "Wind offshore"),
        "pv": ed.pflicht("de", "Solar"),
    }
    handel = ed.handel()
    preis = ed.preis()
    ist: dict[str, dict[str, F]] = {}
    kap: dict[str, dict[str, F]] = {}
    ohne_kap: list[str] = []
    for land in NACHBARN:
        ist[land], kap[land] = {}, {}
        for tech, (erz, kname) in TECHNIK.items():
            reihe = ed.erzeugung(land, erz)
            # Technik im Land praktisch nicht vorhanden, oder Wind auf See ohne Seegebiet
            if reihe is None or np.nanmax(reihe) < 50 or (tech == "wind_off" and land not in SEE):
                continue
            ist[land][tech] = reihe
            k = ed.kapazitaet(land, kname)
            if k is None and tech == "pv":
                k = ed.kapazitaet(land, "Solar DC")
            # ohne Kapazitätsangabe: konstante Skala (Gewichte nehmen die Leistung auf)
            if k is None:
                ohne_kap.append(f"{land}_{tech}")
            kap[land][tech] = k if k is not None else np.full(len(ZEITEN), np.nanmax(reihe))

    # Wind/PV der Nachbarn aus dem GFS: Flächenmittel und gelernt, je Trainingsjahr
    wetter_tab: list[dict[str, Any]] = []

    def gfs_schaetzung(frueh_training: bool) -> tuple[dict[str, F], dict[str, F]]:
        flaeche: dict[str, F] = {}
        gelernt: dict[str, F] = {}
        for land in NACHBARN:
            sw_f = np.zeros(len(ZEITEN))
            sw_g = np.zeros(len(ZEITEN))
            sp_f = np.zeros(len(ZEITEN))
            sp_g = np.zeros(len(ZEITEN))
            for tech, y_mw in ist[land].items():
                geb = see_gebiet(land) if tech == "wind_off" else land_gebiet(regionen, land)
                x = x_pv if tech == "pv" else x_wind
                xb = geb.blockmittel(x)
                prior = np.full(geb.nb, 1.0 / geb.nb) if tech == "wind_off" else geb.pb
                y = y_mw / kap[land][tech]
                tag = (
                    np.nanmax(x[:, geb.ids], axis=1) > 0.02
                    if tech == "pv"
                    else np.ones(len(y), bool)
                )
                m = ~np.isnan(y) & ~np.isnan(xb).any(1) & ok
                tr = m & (FRUEH if frueh_training else ~FRUEH)
                te = m & (~FRUEH if frueh_training else FRUEH)
                w, alpha = anpassen(xb[tr & tag], y[tr & tag], prior, LAM)
                pf = np.nan_to_num(alpha * (xb @ prior)) * kap[land][tech]
                pg = np.nan_to_num(xb @ w) * kap[land][tech]
                if tech == "pv":
                    sp_f, sp_g = sp_f + pf, sp_g + pg
                else:
                    sw_f, sw_g = sw_f + pf, sw_g + pg
                tt = te & tag
                wetter_tab.append(
                    {
                        "land": land,
                        "technik": tech,
                        "training": 2024 if frueh_training else 2025,
                        "n_test": int(tt.sum()),
                        "mittel_mw_test": round(float(np.mean(y_mw[tt])), 1),
                        "r2_flaeche": round(r2(y_mw[tt], pf[tt]), 3),
                        "r2_gelernt": round(r2(y_mw[tt], pg[tt]), 3),
                    }
                )
            flaeche[f"{land}_wind"], flaeche[f"{land}_pv"] = sw_f, sp_f
            gelernt[f"{land}_wind"], gelernt[f"{land}_pv"] = sw_g, sp_g
        return flaeche, gelernt

    def ist_ee() -> dict[str, F]:
        out: dict[str, F] = {}
        for land in NACHBARN:
            z = np.zeros(len(ZEITEN))
            out[f"{land}_wind"] = z + sum(
                (ist[land][t] for t in ("wind_on", "wind_off") if t in ist[land]), z
            )
            out[f"{land}_pv"] = ist[land].get("pv", z.copy())
        return out

    kal = kalender()
    x_de = np.column_stack([de["last"], de["wind_on"], de["wind_off"], de["pv"]])
    ee_ist = ist_ee()
    namen_ee = [f"{land}_{t}" for land in NACHBARN for t in ("wind", "pv")]
    ziele = {"netto_import": handel["netto"], "preis": preis}
    ziele |= {f"grenze_{land}": handel[land] for land in GRENZEN.values()}
    basis = ok & ~np.isnan(x_de).any(1) & ~np.isnan(np.column_stack(list(ee_ist.values()))).any(1)
    basis &= ~np.isnan(np.column_stack(list(ziele.values()))).any(1)

    ergebnis: list[dict[str, Any]] = []
    grenzen: list[dict[str, Any]] = []
    for frueh in (True, False):
        flaeche, gelernt = gfs_schaetzung(frueh)
        saetze = {
            "s0_de": x_de,
            "s1_ee_ist": np.column_stack([x_de, *[ee_ist[n] for n in namen_ee]]),
            "s2a_ee_gfs_flaeche": np.column_stack([x_de, *[flaeche[n] for n in namen_ee]]),
            "s2b_ee_gfs_gelernt": np.column_stack([x_de, *[gelernt[n] for n in namen_ee]]),
        }
        tr, te = basis & (FRUEH if frueh else ~FRUEH), basis & (~FRUEH if frueh else FRUEH)
        variante = "training_2024" if frueh else "training_2025"
        for zname, y in ziele.items():
            for sname, xs in saetze.items():
                x_hgb = np.column_stack([kal, xs])
                p_h = np.asarray(gbm(x_hgb, y, tr).predict(x_hgb[te]), dtype=np.float64)
                if zname.startswith("grenze_"):
                    if sname in ("s0_de", "s1_ee_ist", "s2b_ee_gfs_gelernt"):
                        grenzen.append(
                            {
                                "grenze": zname[7:],
                                "variante": variante,
                                "merkmale": sname,
                                "mittel_mw_test": round(float(np.mean(y[te])), 0),
                                "std_mw_test": round(float(np.std(y[te])), 0),
                                "r2": round(r2(y[te], p_h), 3),
                                "mae_mw": round(mae(y[te], p_h), 0),
                            }
                        )
                    continue
                x_r = np.column_stack([dummies(kal), xs])
                p_r = ridge_cv(x_r, y, tr, monat).vorhersage(x_r[te])
                for modell, pr in (("ridge", p_r), ("gbm", p_h)):
                    ergebnis.append(
                        {
                            "ziel": zname,
                            "merkmale": sname,
                            "modell": modell,
                            "variante": variante,
                            "n_training": int(tr.sum()),
                            "n_test": int(te.sum()),
                            "r2": round(r2(y[te], pr), 3),
                            "mae": round(mae(y[te], pr), 1),
                        }
                    )

    # Ländergewichte: lineares Modell auf Kalender + DE-Last + EE je Land (Ist), je Jahr;
    # Unsicherheit per Wochen-Bootstrap
    ee_de = de["wind_on"] + de["wind_off"] + de["pv"]
    ee_land = np.column_stack([ee_ist[f"{land}_wind"] + ee_ist[f"{land}_pv"] for land in NACHBARN])
    dk = dummies(kal)
    x_lin = np.column_stack(
        [np.ones(len(ZEITEN)), dk, de["last"] / 1000, ee_de / 1000, ee_land / 1000]
    )
    i_ee = 1 + dk.shape[1] + 1
    woche = ((ZEITEN - np.timedelta64(3, "h")).astype("datetime64[D]").astype(np.int64) + 3) // 7
    rng = np.random.default_rng(0)
    gew_tab: list[dict[str, Any]] = []
    for zname in ("preis", "netto_import"):
        y = ziele[zname]
        for jahr, sel in ((2024, FRUEH), (2025, ~FRUEH)):
            m = basis & sel
            xm, ym, wm = x_lin[m], y[m], woche[m]

            def koeff(xx: F, yy: F) -> F:
                return np.asarray(np.linalg.lstsq(xx, yy, rcond=None)[0], dtype=np.float64)

            c = koeff(xm, ym)
            wochen = np.unique(wm)
            boot = []
            for _ in range(200):
                zug = rng.choice(wochen, len(wochen))
                idx = np.concatenate([np.flatnonzero(wm == w_) for w_ in zug])
                boot.append(koeff(xm[idx], ym[idx]))
            bb = np.array(boot)
            for i_l, land in enumerate(["de", *NACHBARN]):
                spalte = i_ee + i_l
                g = bb[:, spalte] / bb[:, i_ee]
                gew_tab.append(
                    {
                        "ziel": zname,
                        "jahr": jahr,
                        "land": land,
                        "koeffizient_je_gw_ee": round(float(c[spalte]), 3),
                        "einheit": "EUR/MWh" if zname == "preis" else "MW",
                        "gewicht_relativ_de": round(float(c[spalte] / c[i_ee]), 3),
                        "gewicht_p05": round(float(np.percentile(g, 5)), 3),
                        "gewicht_p95": round(float(np.percentile(g, 95)), 3),
                        "mittel_ee_gw": round(
                            float(
                                np.mean((ee_de if land == "de" else ee_land[:, i_l - 1])[m]) / 1000
                            ),
                            2,
                        ),
                    }
                )

    tab = HIER / "tabellen"
    tab.mkdir(exist_ok=True)
    for name, zeilen in (
        ("ergebnis", ergebnis),
        ("grenzen", grenzen),
        ("laendergewichte", gew_tab),
        ("wetter_nachbarn", wetter_tab),
    ):
        with (tab / f"{name}.csv").open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(zeilen[0]))
            w.writeheader()
            w.writerows(zeilen)

    grafiken(ergebnis, gew_tab)

    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=False, cwd=WURZEL
    )
    lauf = {
        "job": JOB["id"],
        "job_sha256": sha256(HIER / "job.yaml"),
        "git_commit": commit.stdout.strip(),
        "gestartet": gestartet.isoformat(timespec="seconds"),
        "beendet": datetime.now(UTC).isoformat(timespec="seconds"),
        "zeitraum": {"von": "2024-01-01", "bis": "2026-01-01"},
        "gfs": {
            "quelle": JOB["inputs"]["forecast"]["source"],
            "laeufe": int(np.load(gfs_dir / "ok.npy").sum()),
            "laeufe_erwartet": len(TAGE),
            "lauf_stunde": 0,
            "schritte_h": JOB["inputs"]["forecast"]["steps_h"],
            "layer_digests_sha256": sha256(gfs_dir / "layer.json"),
        },
        "eingaben_sha256": {f.name: sha256(f) for f in sorted(ec_dir.glob("*.json"))},
        "regionen_sha256": sha256(REGIONEN),
        "bloecke_gesamt": len(ZEITEN),
        "bloecke_vollstaendig": int(basis.sum()),
        "bloecke_2024": int((basis & FRUEH).sum()),
        "bloecke_2025": int((basis & ~FRUEH).sum()),
        "laender": ["de", *NACHBARN],
        "techniken_je_land": {land: sorted(ist[land]) for land in NACHBARN},
        "ohne_kapazitaetsangabe": ohne_kap,
        "fehlende_rohwerte_energy_charts": {k: v for k, v in ed.fehlend.items() if v},
        "schritte_zeitlich_interpoliert": {},
        "annahmen": {
            "bloecke": "Ist-Mittel über Intervalle mit Beginn in (Schritt - 3 h, Schritt]",
            "kapazitaet": "Jahresendwerte /installed_power linear interpoliert",
            "gfs_wind": "CF an beiden Blockgrenzen gemittelt, Leistungskurve 3/12/25 m/s",
            "gfs_pv": "PvModel auf dswrf (3-h-Mittel) und t2m, Gewichte nur auf Tagblöcken gelernt",
            "gewichte": f"NNLS auf 1°-Blöcke, λ = {LAM}, im Trainingsjahr gelernt",
            "luxemburg": "gehört zur Gebotszone DE-LU, nicht als Nachbar geführt",
        },
    }
    (tab / "lauf.json").write_text(json.dumps(lauf, indent=1, ensure_ascii=False) + "\n")


def grafiken(ergebnis: list[dict[str, Any]], gew: list[dict[str, Any]]) -> None:
    gr = HIER / "grafiken"
    gr.mkdir(exist_ok=True)
    saetze = ["s0_de", "s1_ee_ist", "s2a_ee_gfs_flaeche", "s2b_ee_gfs_gelernt"]
    beschr = [
        "nur DE",
        "+ EE Nachbarn (Ist)",
        "+ EE Nachbarn (GFS, Fläche)",
        "+ EE Nachbarn (GFS, gelernt)",
    ]
    farben = ["#9aa5b1", "#2a6fdb", "#e3a33b", "#2f9e6e"]
    fig, axes = plt.subplots(1, 2, figsize=(10, 4), sharey=True)
    for ax, ziel, titel in zip(
        axes, ("netto_import", "preis"), ("Netto-Import DE", "Preis DE-LU"), strict=True
    ):
        for k, s in enumerate(saetze):
            r2_werte = [
                next(
                    e["r2"]
                    for e in ergebnis
                    if e["ziel"] == ziel
                    and e["merkmale"] == s
                    and e["modell"] == "gbm"
                    and e["variante"] == v
                )
                for v in ("training_2024", "training_2025")
            ]
            ax.bar(np.arange(2) + (k - 1.5) * 0.2, r2_werte, 0.2, color=farben[k], label=beschr[k])
        ax.set_xticks([0, 1], ["Test 2025", "Test 2024"])
        ax.set_title(titel)
        ax.set_ylim(0, 1)
        ax.grid(axis="y", alpha=0.3)
    axes[0].set_ylabel("R² im Testjahr (LightGBM)")
    axes[1].legend(loc="lower right", fontsize=8)
    fig.tight_layout()
    fig.savefig(gr / "ergebnis.png", dpi=130)
    plt.close(fig)

    laender = ["de", *NACHBARN]
    fig, axes = plt.subplots(1, 2, figsize=(10, 4))
    for ax, ziel, titel in zip(
        axes, ("preis", "netto_import"), ("Preis DE-LU", "Netto-Import DE"), strict=True
    ):
        for k, jahr in enumerate((2024, 2025)):
            z = {g["land"]: g for g in gew if g["ziel"] == ziel and g["jahr"] == jahr}
            werte = np.array([z[land]["gewicht_relativ_de"] for land in laender])
            lo = werte - np.array([z[land]["gewicht_p05"] for land in laender])
            hi = np.array([z[land]["gewicht_p95"] for land in laender]) - werte
            ax.bar(
                np.arange(len(laender)) + (k - 0.5) * 0.38,
                werte,
                0.38,
                yerr=np.vstack([lo, hi]),
                capsize=2,
                color=["#2a6fdb", "#2f9e6e"][k],
                label=str(jahr),
            )
        ax.axhline(0, color="black", lw=0.6)
        ax.set_xticks(range(len(laender)), [x.upper() for x in laender])
        ax.set_title(f"{titel}: Wirkung 1 GW EE je Land, relativ zu 1 GW EE in DE")
        ax.title.set_fontsize(9)
        ax.grid(axis="y", alpha=0.3)
        ax.legend(fontsize=8)
    fig.tight_layout()
    fig.savefig(gr / "laendergewichte.png", dpi=130)
    plt.close(fig)


if __name__ == "__main__":
    main()
