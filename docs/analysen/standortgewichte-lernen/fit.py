"""Standortgewichte je Zone aus Ist-Erzeugung lernen und gegen Flächenmittel/MaStR prüfen."""

import gzip
import json
import os
import sys
from pathlib import Path

import numpy as np
from scipy.optimize import nnls

DATA, EC, REG, OUT = sys.argv[1:5]
lat = np.arange(71.5, 34.49, -0.5)
lon = np.arange(-10.5, 45.01, 0.5)
days = np.array([np.datetime64("2024-01-01") + np.timedelta64(d, "D") for d in range(731)])
times = (
    days[:, None].astype("datetime64[h]") + np.arange(3, 25, 3).astype("timedelta64[h]")
).ravel()  # Schrittzeit (UTC)
ok = np.load(f"{DATA}/ok.npy")


def L(v):
    return np.load(f"{DATA}/{v}.npy", mmap_mode="r")


u, v, ghi, t2m = L("u100"), L("v100"), L("dswrf"), L("t2m")
ws = np.hypot(u, v).reshape(-1, 75 * 112)
ghi = np.asarray(ghi).reshape(-1, 75 * 112)
tc = np.asarray(t2m).reshape(-1, 75 * 112) - 273.15


def wind_cf(v, ci=3.0, r=12.0, co=25.0):
    cf = np.clip((v**3 - ci**3) / (r**3 - ci**3), 0, 1)
    return np.where((v < ci) | (v >= co), 0.0, cf)


def pv_cf(g, t):
    tcell = t + g * 25 / 800
    return np.clip(0.85 * g / 1000 * (1 - 0.004 * (tcell - 25)), 0, None)


X = {"wind": wind_cf(ws), "pv": pv_cf(ghi, tc)}
regions = {
    r["code"]: np.array(r["cells"]).reshape(-1, 3)
    for r in json.loads(gzip.decompress(Path(REG).read_bytes()))["regions"]
}


def cell_ids(cells):
    i = np.rint((71.5 - cells[:, 0]) / 0.5).astype(int)
    j = np.rint((cells[:, 1] + 10.5) / 0.5).astype(int)
    return i * 112 + j


def target(c, tech):
    name = {"wind": "Wind onshore", "pv": "Solar"}[tech]
    ts, val = [], []
    for y in (2024, 2025):
        d = json.loads(Path(f"{EC}/pp_{c}_{y}.json").read_text())
        p = next(p for p in d["production_types"] if p["name"] == name)
        ts += d["unix_seconds"]
        val += [np.nan if x is None else x for x in p["data"]]
    ts = np.array(ts, "datetime64[s]")
    val = np.array(val, float)
    cap = json.loads(Path(f"{EC}/ipy_{c}.json").read_text())
    yrs = np.array(cap["time"], int)
    capname = {"wind": "Wind onshore", "pv": "Solar AC"}[tech]
    if c == "de":  # monatlich vorhanden, aber jährliche Reihe genügt
        pass
    pc = next(p for p in cap["production_types"] if p["name"] == capname)["data"]
    # Jahresendwerte linear interpolieren
    keep = np.array([x is not None for x in pc])
    tcap = (
        np.array([np.datetime64(f"{y}-12-31") for y in yrs[keep]])
        .astype("datetime64[s]")
        .astype(float)
    )
    capt = (
        np.interp(ts.astype(float), tcap, np.array([x for x in pc if x is not None], float)) * 1000
    )  # MW
    cf = val / capt
    # auf Schrittzeiten: Wind ±1,5 h, PV Mittel der 3 h davor
    out = np.full(len(times), np.nan)
    tsf = ts.astype("datetime64[m]").astype(np.int64)
    tt = times.astype("datetime64[m]").astype(np.int64)
    lo, hi = (tt - 90, tt + 90) if tech == "wind" else (tt - 180, tt)
    a = np.searchsorted(tsf, lo, "left")
    b = np.searchsorted(tsf, hi, "left")
    cs = np.concatenate([[0], np.nancumsum(cf)])
    cn = np.concatenate([[0], np.cumsum(~np.isnan(cf))])
    n = cn[b] - cn[a]
    m = (cs[b] - cs[a]) / np.maximum(n, 1)
    out[n >= 2] = m[n >= 2]
    return out


def coarsen(ids):
    """Zellen zu 1°-Blöcken (2x2) zusammenfassen: Gruppen-Index je Zelle."""
    i, j = ids // 112, ids % 112
    key = (i // 2) * 1000 + (j // 2)
    _, g = np.unique(key, return_inverse=True)
    return g


def r2(y, p):
    return 1 - np.nansum((y - p) ** 2) / np.nansum((y - np.nanmean(y)) ** 2)


def fit(Xz, y, prior, lam):
    """NNLS mit Ridge zur skalierten Vorab-Verteilung (Flächenanteile)."""
    alpha = np.nansum((Xz @ prior) * y) / np.nansum((Xz @ prior) ** 2)
    k = Xz.shape[1]
    s = np.sqrt(lam * len(y))
    A = np.vstack([Xz, s * np.eye(k)])
    b = np.concatenate([y, s * alpha * prior])
    w, _ = nnls(A, b, maxiter=50 * k)
    return w, alpha


valid_day = np.repeat(ok, 8)

SWAP = os.environ.get("SWAP") == "1"
LAMS = (
    [float(x) for x in os.environ["LAMS"].split(",")]
    if os.environ.get("LAMS")
    else (1e-4, 3e-4, 1e-3, 3e-3, 1e-2, 3e-2, 1e-1, 1.0)
)
early = times < np.datetime64("2025-01-01")
train = (~early if SWAP else early) & valid_day
test = (early if SWAP else ~early) & valid_day
res = {}
for c in sys.argv[5].split(","):
    area = regions[f"{c.upper()}:area"]
    ids = cell_ids(area)
    share = area[:, 2] / area[:, 2].sum()
    g = coarsen(ids)
    ng = g.max() + 1
    for tech in ("wind", "pv"):
        y = target(c, tech)
        Xc = X[tech][:, ids]
        # PV nur zu Tagstunden, sonst dominiert die Nacht
        day = np.nanmax(Xc, axis=1) > 0.02 if tech == "pv" else np.ones(len(y), bool)
        Xg = np.zeros((len(y), ng))
        np.add.at(Xg.T, g, (Xc * share).T)  # Block = Summe Flächenanteil * CF
        pg = np.bincount(g, share, ng)
        Xg = Xg / np.maximum(pg, 1e-12)  # Block-Mittel
        m = ~np.isnan(y) & ~np.isnan(Xg).any(1) & day
        tr, te = m & train, m & test
        # Lambda per Blöcken (Monate) in 2024: jeder 4. Monat Validierung
        mon = times.astype("datetime64[M]").astype(int) % 12
        va = tr & (mon % 4 == 3)
        fi = tr & (mon % 4 != 3)
        best = None
        for lam in LAMS:
            w, _ = fit(Xg[fi], y[fi], pg, lam)
            e = np.nanmean((Xg[va] @ w - y[va]) ** 2)
            if best is None or e < best[0]:
                best = (e, lam)
        lam = best[1]
        w, alpha = fit(Xg[tr], y[tr], pg, lam)
        p_area = alpha * (Xg @ pg)
        p_fit = Xg @ w
        row = {
            "lam": lam,
            "scale_area": alpha,
            "scale_fit": w.sum(),
            "r2_area": r2(y[te], p_area[te]),
            "r2_fit": r2(y[te], p_fit[te]),
            "rmse_area": float(np.sqrt(np.nanmean((y[te] - p_area[te]) ** 2))),
            "rmse_fit": float(np.sqrt(np.nanmean((y[te] - p_fit[te]) ** 2))),
            "n_train": int(tr.sum()),
            "n_test": int(te.sum()),
            "blocks": int(ng),
            "mean_cf_test": float(np.nanmean(y[te])),
        }
        wb = w / w.sum()
        # zurück auf Zellen: Blockgewicht nach Flächenanteil verteilen
        wc = wb[g] * share / pg[g]
        if c == "de":
            mtech = {"wind": "DE:wind_on", "pv": "DE:pv"}[tech]
            mc = regions[mtech]
            mids = cell_ids(mc)
            mw = np.zeros(75 * 112)
            np.add.at(mw, mids, mc[:, 2])
            mz = mw[ids]
            mz = mz / mz.sum()
            mg = np.bincount(g, mz, ng)
            a2 = np.nansum((Xg[tr] @ mg) * y[tr]) / np.nansum((Xg[tr] @ mg) ** 2)
            p_m = a2 * (Xg @ mg)
            row.update(
                r2_mastr=r2(y[te], p_m[te]),
                rmse_mastr=float(np.sqrt(np.nanmean((y[te] - p_m[te]) ** 2))),
                corr_weights_vs_mastr=float(np.corrcoef(wb, mg)[0, 1]),
                corr_area_vs_mastr=float(np.corrcoef(pg, mg)[0, 1]),
                overlap_fit_mastr=float(np.minimum(wb, mg).sum()),
                overlap_area_mastr=float(np.minimum(pg, mg).sum()),
            )
            np.save(f"{OUT}/w_mastr_{c}_{tech}.npy", np.stack([ids, mz]))
        res[f"{c}:{tech}"] = row
        np.save(f"{OUT}/w_{c}_{tech}.npy", np.stack([ids, wc, share]))
        np.save(f"{OUT}/wb_{c}_{tech}.npy", wb)
        print(
            c,
            tech,
            json.dumps({k: round(v, 4) if isinstance(v, float) else v for k, v in row.items()}),
            flush=True,
        )
Path(f"{OUT}/ergebnis.json").write_text(json.dumps(res, indent=1))
