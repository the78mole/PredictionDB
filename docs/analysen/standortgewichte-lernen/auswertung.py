"""Standortgewichte für Wind und PV je Zone aus der Ist-Erzeugung lernen.

Liest den Cache von abruf.py, lernt je Zone und Technik Gewichte auf 1°-Blöcken (NNLS mit
Ridge-Strafe zur skalierten Flächenverteilung) und vergleicht sie im jeweils anderen Jahr mit
dem Flächenmittel und in DE mit den MaStR-Gewichten aus config/gefs/regions_v2.json.gz.
Schreibt tabellen/*.csv, tabellen/lauf.json und grafiken/gewichte_wind.png.

Aufruf (siehe README):
    uv run --no-project --with numpy --with scipy --with matplotlib --with pyyaml python -I \
        docs/analysen/standortgewichte-lernen/auswertung.py --cache /tmp/sg
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

import numpy as np
from numpy.typing import NDArray
from scipy.optimize import nnls

F = NDArray[np.float64]
HIER = Path(__file__).resolve().parent
WURZEL = HIER.parents[2]
REGIONEN = WURZEL / "config" / "gefs" / "regions_v2.json.gz"
LAENDER = ("de", "pl", "at", "es")
TECHNIK = {"wind": ("Wind onshore", "Wind onshore"), "pv": ("Solar", "Solar AC")}
NX = 112
LAT0, LON0, D = 71.5, -10.5, 0.5
LAMS_CV = (1e-4, 3e-4, 1e-3, 3e-3, 1e-2, 3e-2, 1e-1, 1.0)
#: Variante -> (Training im Jahr 2024?, feste Glättung oder None = Kreuzvalidierung)
VARIANTEN: dict[str, tuple[bool, float | None]] = {
    "cv_2024": (True, None),
    "cv_2025": (False, None),
    "lam0.03_2024": (True, 0.03),
    "lam0.3_2024": (True, 0.3),
}

TAGE = np.arange(np.datetime64("2024-01-01"), np.datetime64("2026-01-01"))
ZEITEN = (
    TAGE[:, None].astype("datetime64[h]") + np.arange(3, 25, 3).astype("timedelta64[h]")
).ravel()
FRUEH = np.datetime64("2025-01-01") > ZEITEN


def wind_cf(v: F, ein: float = 3.0, nenn: float = 12.0, aus: float = 25.0) -> F:
    """Leistungskurve wie wx_process.features.PowerCurve."""
    cf = np.clip((v**3 - ein**3) / (nenn**3 - ein**3), 0, 1)
    return np.where((v < ein) | (v >= aus), 0.0, cf)


def pv_cf(ghi: F, t_c: F) -> F:
    """PV-Modell wie wx_process.features.PvModel."""
    t_zelle = t_c + ghi * 25 / 800
    return np.clip(0.85 * ghi / 1000 * (1 - 0.004 * (t_zelle - 25)), 0, None)


def zellen(cells: F) -> NDArray[np.int64]:
    i = np.rint((LAT0 - cells[:, 0]) / D).astype(np.int64)
    j = np.rint((cells[:, 1] - LON0) / D).astype(np.int64)
    return i * NX + j


def ziel(ec: Path, land: str, technik: str) -> tuple[F, int]:
    """Ist-CF auf den Schrittzeiten: Wind ±1,5 h, PV Mittel der 3 h davor (wie dswrf)."""
    erz, kap = TECHNIK[technik]
    ts_l: list[int] = []
    werte: list[float] = []
    for jahr in (2024, 2025):
        d = json.loads((ec / f"pp_{land}_{jahr}.json").read_text())
        reihe = next(p for p in d["production_types"] if p["name"] == erz)
        ts_l += d["unix_seconds"]
        werte += [np.nan if x is None else float(x) for x in reihe["data"]]
    ts = np.array(ts_l, dtype=np.int64)
    val = np.array(werte, dtype=np.float64)
    leistung = json.loads((ec / f"ipy_{land}.json").read_text())
    jahre = [int(j) for j in leistung["time"]]
    pc = next(p for p in leistung["production_types"] if p["name"] == kap)["data"]
    paare = [(j, float(x)) for j, x in zip(jahre, pc, strict=True) if x is not None]
    # Jahresendwerte linear interpoliert
    t_kap = np.array(
        [np.datetime64(f"{j}-12-31").astype("datetime64[s]").astype(np.int64) for j, _ in paare]
    )
    kap_mw = np.interp(ts, t_kap, np.array([x for _, x in paare])) * 1000
    cf = val / kap_mw
    t_min = ts // 60
    z = ZEITEN.astype("datetime64[m]").astype(np.int64)
    lo, hi = (z - 90, z + 90) if technik == "wind" else (z - 180, z)
    a = np.searchsorted(t_min, lo, "left")
    b = np.searchsorted(t_min, hi, "left")
    cs = np.concatenate([[0.0], np.nancumsum(cf)])
    cn = np.concatenate([[0], np.cumsum(~np.isnan(cf))])
    n = cn[b] - cn[a]
    out = np.full(len(ZEITEN), np.nan)
    gut = n >= 2
    out[gut] = (cs[b] - cs[a])[gut] / n[gut]
    return out, int(np.isnan(val).sum())


def r2(y: F, p: F) -> float:
    return float(1 - np.nansum((y - p) ** 2) / np.nansum((y - np.nanmean(y)) ** 2))


def rmse(y: F, p: F) -> float:
    return float(np.sqrt(np.nanmean((y - p) ** 2)))


def anpassen(x: F, y: F, prior: F, lam: float) -> tuple[F, float]:
    """NNLS mit Ridge zur skalierten Flächenverteilung; gibt Gewichte und Flächenskala."""
    alpha = float(np.sum((x @ prior) * y) / np.sum((x @ prior) ** 2))
    k = x.shape[1]
    s = np.sqrt(lam * len(y))
    w, _ = nnls(
        np.vstack([x, s * np.eye(k)]), np.concatenate([y, s * alpha * prior]), maxiter=50 * k
    )
    return np.asarray(w, dtype=np.float64), alpha


@dataclass
class Zone:
    land: str
    ids: NDArray[np.int64]
    anteil: F  # Flächenanteil je Zelle
    block: NDArray[np.int64]  # 1°-Block je Zelle
    nb: int
    pb: F  # Flächenanteil je Block

    @classmethod
    def aus(cls, cells: F, land: str) -> Zone:
        ids = zellen(cells)
        anteil = cells[:, 2] / cells[:, 2].sum()
        schluessel = (ids // NX // 2) * 1000 + (ids % NX) // 2
        _, block = np.unique(schluessel, return_inverse=True)
        nb = int(block.max()) + 1
        return cls(land, ids, anteil, block, nb, np.bincount(block, anteil, nb))

    def blockmittel(self, x: F) -> F:
        xb = np.zeros((x.shape[0], self.nb))
        np.add.at(xb.T, self.block, (x[:, self.ids] * self.anteil).T)
        return xb / np.maximum(self.pb, 1e-12)

    def auf_zellen(self, wb: F) -> F:
        return np.asarray(wb[self.block] * self.anteil / self.pb[self.block], dtype=np.float64)


def sha256(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--cache", type=Path, required=True)
    args = p.parse_args()
    gestartet = datetime.now(UTC)
    gfs, ec = args.cache / "gfs", args.cache / "ec"
    ok = np.load(gfs / "ok.npy")

    def lade(g: str) -> F:
        return np.asarray(np.load(gfs / f"{g}.npy"), dtype=np.float64).reshape(len(ZEITEN), -1)

    ws = np.hypot(lade("u100"), lade("v100"))
    x_tech = {"wind": wind_cf(ws), "pv": pv_cf(lade("dswrf"), lade("t2m") - 273.15)}
    del ws
    regionen = {
        r["code"]: np.array(r["cells"], dtype=np.float64).reshape(-1, 3)
        for r in json.loads(gzip.decompress(REGIONEN.read_bytes()))["regions"]
    }
    gueltig = np.repeat(ok, 8)
    monat = ZEITEN.astype("datetime64[M]").astype(np.int64) % 12
    ergebnis: list[dict[str, Any]] = []
    stabil: list[dict[str, Any]] = []
    gewichte: list[dict[str, Any]] = []
    fehlend: dict[str, int] = {}
    wb_alle: dict[tuple[str, str, str], F] = {}
    zonen = {land: Zone.aus(regionen[f"{land.upper()}:area"], land) for land in LAENDER}
    for land, zone in zonen.items():
        for technik in ("wind", "pv"):
            y, n_fehl = ziel(ec, land, technik)
            fehlend[f"{land}_{technik}"] = n_fehl
            xb = zone.blockmittel(x_tech[technik])
            tag = (
                np.nanmax(x_tech[technik][:, zone.ids], axis=1) > 0.02
                if technik == "pv"
                else np.ones(len(y), bool)
            )
            m = ~np.isnan(y) & ~np.isnan(xb).any(axis=1) & tag & gueltig
            mastr: F | None = None
            if land == "de":
                mc = regionen[{"wind": "DE:wind_on", "pv": "DE:pv"}[technik]]
                mw = np.zeros(75 * NX)
                np.add.at(mw, zellen(mc), mc[:, 2])
                mz = mw[zone.ids] / mw[zone.ids].sum()
                mastr = np.bincount(zone.block, mz, zone.nb)
            for name, (frueh, lam_fest) in VARIANTEN.items():
                tr = m & (FRUEH if frueh else ~FRUEH)
                te = m & (~FRUEH if frueh else FRUEH)
                lam = lam_fest
                if lam is None:  # jeder vierte Monat des Trainingsjahres als Validierung
                    va, fi = tr & (monat % 4 == 3), tr & (monat % 4 != 3)
                    fehler = []
                    for kandidat in LAMS_CV:
                        w, _ = anpassen(xb[fi], y[fi], zone.pb, kandidat)
                        fehler.append(float(np.mean((xb[va] @ w - y[va]) ** 2)))
                    lam = LAMS_CV[int(np.argmin(fehler))]
                w, alpha = anpassen(xb[tr], y[tr], zone.pb, lam)
                p_flaeche, p_gelernt = alpha * (xb @ zone.pb), xb @ w
                zeile: dict[str, Any] = {
                    "land": land,
                    "technik": technik,
                    "variante": name,
                    "lambda": lam,
                    "n_training": int(tr.sum()),
                    "n_test": int(te.sum()),
                    "bloecke": zone.nb,
                    "mittlerer_cf_test": round(float(np.mean(y[te])), 4),
                    "skala_flaeche": round(alpha, 4),
                    "skala_gelernt": round(float(w.sum()), 4),
                    "r2_flaeche": round(r2(y[te], p_flaeche[te]), 4),
                    "r2_gelernt": round(r2(y[te], p_gelernt[te]), 4),
                    "rmse_flaeche": round(rmse(y[te], p_flaeche[te]), 4),
                    "rmse_gelernt": round(rmse(y[te], p_gelernt[te]), 4),
                    "r2_mastr": "",
                    "rmse_mastr": "",
                    "korr_gewichte_mastr": "",
                    "korr_flaeche_mastr": "",
                }
                wb = w / w.sum()
                if mastr is not None:
                    a2 = float(np.sum((xb[tr] @ mastr) * y[tr]) / np.sum((xb[tr] @ mastr) ** 2))
                    p_m = a2 * (xb @ mastr)
                    zeile |= {
                        "r2_mastr": round(r2(y[te], p_m[te]), 4),
                        "rmse_mastr": round(rmse(y[te], p_m[te]), 4),
                        "korr_gewichte_mastr": round(float(np.corrcoef(wb, mastr)[0, 1]), 4),
                        "korr_flaeche_mastr": round(float(np.corrcoef(zone.pb, mastr)[0, 1]), 4),
                    }
                ergebnis.append(zeile)
                wb_alle[(land, technik, name)] = wb
                wz = zone.auf_zellen(wb)
                lon = LON0 + D * (zone.ids % NX)
                if technik == "wind":
                    for k in np.flatnonzero(wz > 1e-4):
                        gewichte.append(
                            {
                                "land": land,
                                "variante": name,
                                "lat": float(LAT0 - D * (zone.ids[k] // NX)),
                                "lon": float(lon[k]),
                                "gewicht": round(float(wz[k]), 5),
                                "flaechenanteil": round(float(zone.anteil[k]), 5),
                            }
                        )
            a, b = wb_alle[(land, technik, "cv_2024")], wb_alle[(land, technik, "cv_2025")]
            wa = zone.auf_zellen(a)
            lon = LON0 + D * (zone.ids % NX)
            stabil.append(
                {
                    "land": land,
                    "technik": technik,
                    "korr_2024_2025": round(float(np.corrcoef(a, b)[0, 1]), 4),
                    "ueberlappung_2024_2025": round(float(np.minimum(a, b).sum()), 4),
                    "anteil_top25_bloecke": round(
                        float(np.sort(a)[::-1][: max(1, zone.nb // 4)].sum()), 4
                    ),
                    "anteil_oestlich_15_5": round(float(wa[lon >= 15.5].sum()), 4)
                    if land == "at"
                    else "",
                    "flaeche_oestlich_15_5": round(float(zone.anteil[lon >= 15.5].sum()), 4)
                    if land == "at"
                    else "",
                }
            )
            print(land, technik, "fertig", flush=True)

    tab = HIER / "tabellen"
    tab.mkdir(exist_ok=True)
    for datei, zeilen in (
        ("ergebnis", ergebnis),
        ("stabilitaet", stabil),
        ("gewichte_wind", gewichte),
    ):
        with (tab / f"{datei}.csv").open("w", newline="") as f:
            schreiber = csv.DictWriter(f, fieldnames=list(zeilen[0]))
            schreiber.writeheader()
            schreiber.writerows(zeilen)
    karte(zonen, wb_alle, regionen)

    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=False, cwd=WURZEL
    )
    eingaben = {f.name: sha256(f) for f in sorted(ec.glob("*.json"))}
    lauf = {
        "job": "standortgewichte-lernen",
        "job_sha256": sha256(HIER / "job.yaml"),
        "git_commit": commit.stdout.strip(),
        "gestartet": gestartet.isoformat(timespec="seconds"),
        "beendet": datetime.now(UTC).isoformat(timespec="seconds"),
        "zeitraum": {"von": "2024-01-01", "bis": "2026-01-01"},
        "gfs": {
            "quelle": "ghcr.io/the78mole/predictiondb-data/gfs-0p5-silver",
            "laeufe": int(ok.sum()),
            "laeufe_erwartet": len(TAGE),
            "lauf_stunde": 0,
            "schritte_h": [3, 6, 9, 12, 15, 18, 21, 24],
            "layer_digests_sha256": sha256(gfs / "layer.json"),
        },
        "eingaben_sha256": eingaben,
        "regionen_sha256": sha256(REGIONEN),
        "zeitschritte_je_reihe": len(ZEITEN),
        "fehlende_rohwerte_energy_charts": fehlend,
        "schritte_zeitlich_interpoliert": {},
        "annahmen": {
            "kapazitaet": "Jahresendwerte /installed_power linear interpoliert",
            "wind": "Ist-CF ±1,5 h um den Schritt, Leistungskurve 3/12/25 m/s auf Wind 100 m",
            "pv": "Ist-CF als Mittel der 3 h vor dem Schritt, nur Schritte mit Zell-CF > 0,02",
            "bloecke": "2 x 2 Zellen (1°)",
        },
    }
    (tab / "lauf.json").write_text(json.dumps(lauf, indent=1, ensure_ascii=False) + "\n")


def karte(
    zonen: dict[str, Zone], wb: dict[tuple[str, str, str], F], regionen: dict[str, F]
) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    namen = {"de": "Deutschland", "pl": "Polen", "at": "Österreich", "es": "Spanien"}
    lat = LAT0 - D * np.arange(75)
    lon = LON0 + D * np.arange(NX)
    fig, ax = plt.subplots(4, 4, figsize=(14, 13))
    for r, (land, zone) in enumerate(zonen.items()):

        def gitter(werte: F, zone: Zone = zone) -> F:
            g = np.full(75 * NX, np.nan)
            g[zone.ids] = werte
            return g.reshape(75, NX)

        flaeche = gitter(zone.anteil)
        panels = [
            (zone.anteil, "Flächenmittel (heute)"),
            (zone.auf_zellen(wb[(land, "wind", "cv_2024")]), "gelernt, λ aus Kreuzvalidierung"),
            (
                zone.auf_zellen(wb[(land, "wind", "lam0.3_2024")]),
                "gelernt, stark geglättet (λ = 0,3)",
            ),
        ]
        if land == "de":
            mc = regionen["DE:wind_on"]
            mw = np.zeros(75 * NX)
            np.add.at(mw, zellen(mc), mc[:, 2])
            panels.append((mw[zone.ids] / mw[zone.ids].sum(), "MaStR (Referenz)"))
        i, j = zone.ids // NX, zone.ids % NX
        i0, i1, j0, j1 = int(i.min()), int(i.max()) + 1, int(j.min()), int(j.max()) + 1
        for k in range(4):
            a = ax[r, k]
            if k >= len(panels):
                a.axis("off")
                continue
            werte, titel = panels[k]
            g = gitter(werte)
            g = np.where(g > 0, g, np.nan)
            ausdehnung = (lon[j0] - 0.25, lon[j1 - 1] + 0.25, lat[i1 - 1] - 0.25, lat[i0] + 0.25)
            a.imshow(
                g[i0:i1, j0:j1], extent=ausdehnung, cmap="viridis", vmin=0, vmax=float(np.nanmax(g))
            )
            a.contour(
                lon[j0:j1],
                lat[i0:i1],
                np.isnan(flaeche[i0:i1, j0:j1]).astype(float),
                [0.5],
                colors="0.4",
                linewidths=0.6,
            )
            a.set_title(f"{namen[land]}: {titel}", fontsize=9)
            a.tick_params(labelsize=7)
    fig.suptitle(
        "Gewichte Wind an Land je GFS-Zelle (0,5°), Training 2024, Anteile je Zone", fontsize=12
    )
    fig.tight_layout()
    (HIER / "grafiken").mkdir(exist_ok=True)
    fig.savefig(HIER / "grafiken" / "gewichte_wind.png", dpi=110)


if __name__ == "__main__":
    main()
