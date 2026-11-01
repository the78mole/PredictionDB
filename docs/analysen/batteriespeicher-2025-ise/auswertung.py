"""Nachrechnung: 20 GW / 40 GWh zusätzliche Batteriespeicher im Day-Ahead-Markt DE-LU 2025.

Einmalige Auswertung vom 01.11.2026, Auftrag und Parameter in ``job.yaml`` (docs/ARCHITEKTUR.md
7.9), Laufprotokoll in ``tabellen/lauf.json``. Vergleich mit dem Strompreissimulator des
Fraunhofer ISE (Energy-Charts, Pressemitteilung vom 01.10.2026): Stunden über 200 €/MWh
169 -> 20, Stunden mit negativem Preis 575 -> 271, Tagesspanne 130 -> 53 €/MWh.

Das ISE verschiebt die echten aggregierten Kauf- und Verkaufskurven der EPEX. Die sind nicht
frei. Hier wird die Preisreaktion stattdessen aus freien Daten geschätzt: eine monotone Kurve
Preis über Mengengröße Q je Monat (Nachbarmonate eingeschlossen), und der gemessene Preis wird um
``g(Q + x) - g(Q)`` verschoben (x = Laden > 0, Entladen < 0). Varianten:

- ``A``: Q = Residuallast (Last - Wind - PV). Der Außenhandel steckt in der Kurve, reagiert
  also mit (wie PR #150).
- ``B``: Q = Residuallast - Nettoimport, also der Bedarf an inländischer steuerbarer Erzeugung.
  Der Außenhandel bleibt fest wie beim ISE.
- ``C``: wie B, aber die Kurve wird mit Tages-Festeffekten geschätzt: Nur Unterschiede innerhalb
  eines Tages zählen, Brennstoffpreis, Verfügbarkeit und Wetterlage eines Tages verschieben die
  Kurve also nicht mehr und verflachen sie nicht (Regressionsverdünnung).
- ``D``: Steigung nach Preisniveau. Aus Paaren aufeinanderfolgender Stunden wird geschätzt, wie
  stark sich der Preis je GW Änderung von Q (wie B) ändert, getrennt nach dem Preisniveau
  (``s(p)`` in €/MWh je GW). Der Preis jeder Stunde wandert dann entlang ``dp/dx = s(p)`` von
  ihrem eigenen Ist-Preis aus. So behält jede Stunde ihre eigene Lage auf der Kurve: Eine Stunde
  bei 583 €/MWh liegt im steilen Knappheitsast, eine Stunde bei -0,01 €/MWh auf dem flachen
  Abregelungsplateau. A bis C mitteln diese Lagen über einen Monat weg.

Der Speicher maximiert je Woche seinen Gewinn mit perfekter Voraussicht und berücksichtigt den
eigenen Preiseinfluss (lineares Programm mit K Teilstücken je Zeitschritt, das entspricht einem
Speicherpark aus vielen preisnehmenden Betreibern, die den Gewinn untereinander aufteilen bis die
Spanne ausgeschöpft ist: Wohlfahrtsoptimum). Januar bis September stündlich, ab 1. Oktober 2025
viertelstündlich wie die Auktion. Wirkungsgrad 86 % (Netz zu Netz) wie beim ISE.

Aufruf aus dem Repository-Wurzelverzeichnis::

    uv run --no-project --with requests \\
        python docs/analysen/batteriespeicher-2025-ise/abruf.py ./daten-2025
    uv run --no-project --with pandas --with numpy --with scipy --with matplotlib --with pyyaml \\
        python docs/analysen/batteriespeicher-2025-ise/auswertung.py --daten ./daten-2025
"""

from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import yaml
from scipy import sparse
from scipy.optimize import linprog
from scipy.sparse import linalg as splinalg

HIER = Path(__file__).resolve().parent
TZ = "Europe/Berlin"
#: Auftrag und Parameter (docs/ARCHITEKTUR.md 7.9)
JOB_PFAD = HIER / "job.yaml"
JOB: dict[str, Any] = yaml.safe_load(JOB_PFAD.read_text())
_M = JOB["method"]
JAHR = (str(_M["jahr"]["from"]), str(_M["jahr"]["to"]))
VIERTEL_AB = pd.Timestamp(str(_M["aufloesung"]["bis"]), tz=TZ)  # Day-Ahead in 15-min-Produkten
ETA = float(_M["speicher"]["wirkungsgrad"])  # Netz zu Netz
P_GW = float(_M["speicher"]["leistung_gw"])
E_GWH = float(_M["speicher"]["energie_gwh"])
K = int(_M["fahrplan"]["leistungsstufen"])  # Teilstücke der Leistung im LP
#: Alterungskosten je MWh Entladung (ISE nennt Alterungskosten, aber keinen Wert)
ALTERUNG = [float(x) for x in _M["alterung_eur_mwh"]]
#: Ergebnisse des Fraunhofer ISE (Pressemitteilung vom 01.10.2026, ZfK vom 28.09.2026)
ISE = {k: tuple(v) for k, v in JOB["inputs"]["vergleich"]["ise"].items()}
GITTER = np.arange(-80.0, 120.01, 0.5)
FARBE = {
    "A": "#2f6db3",
    "B": "#7aa457",
    "C": "#c4562b",
    "D": "#8e44ad",
    "E": "#d9a521",
    "E2": "#8a6d1a",
    "ISE": "#444444",
}
VARIANTEN = list(_M["varianten"])
#: mittlerer Prognosefehler Wind + PV (Prognose - Ist) in Stunden mit Preis > 30 €/MWh, 10-16 Uhr,
#: April bis September 2025 (gemessen, siehe README)
PROGNOSEFEHLER_GW = float(_M["prognosefehler_gw"])
#: Preisklassen für die Steigung s(p) in Variante D (€/MWh)
KLASSEN = [float(x) for x in _M["preisklassen_eur_mwh"]]
PGITTER = np.arange(-1000.0, 5000.01, 0.05)


# ---------------------------------------------------------------- Daten
#: fehlende Werte, die beim Laden ersetzt wurden (für tabellen/lauf.json)
LUECKEN: dict[str, int] = {}


def lade(ordner: Path) -> pd.DataFrame:
    pr = json.loads((ordner / "price.json").read_text())
    pp = json.loads((ordner / "public_power.json").read_text())
    t = pd.to_datetime(pp["unix_seconds"], unit="s", utc=True).tz_convert(TZ)
    reihen = {x["name"]: x["data"] for x in pp["production_types"]}
    df = (
        pd.DataFrame(
            {
                "load": reihen["Load"],
                "residual": reihen["Residual load"],
                "import": reihen["Cross border electricity trading"],
                "solar": reihen["Solar"],
            },
            index=t,
            dtype=float,
        )
        / 1000.0
    )  # MW -> GW
    p = pd.Series(
        pr["price"],
        index=pd.to_datetime(pr["unix_seconds"], unit="s", utc=True).tz_convert(TZ),
        dtype=float,
    )
    p15 = p.reindex(df.index)
    im_jahr = (df.index >= pd.Timestamp(JAHR[0], tz=TZ)) & (df.index < pd.Timestamp(JAHR[1], tz=TZ))
    ab_okt = im_jahr & (df.index >= VIERTEL_AB)
    LUECKEN["preis_viertelstunden_ab_oktober_fehlend"] = int(p15[ab_okt].isna().sum())
    df["price"] = p15.ffill(limit=3)  # Stundenpreis auf die Viertelstunden
    df["steuerbar"] = df.residual - df["import"]
    # Abregelung bei negativen Preisen, geschätzt: Day-Ahead-Prognose Wind + PV minus Ist,
    # abzüglich des mittleren Prognosefehlers in Stunden mit normalem Preis
    fc = (
        pd.DataFrame(
            {
                n: pd.Series(
                    (d := json.loads((ordner / f"fc_{n}.json").read_text()))["forecast_values"],
                    index=pd.to_datetime(d["unix_seconds"], unit="s", utc=True).tz_convert(TZ),
                    dtype=float,
                )
                for n in ["solar", "wind_onshore", "wind_offshore"]
            }
        ).reindex(df.index)
        / 1000.0
    )
    ist = (
        df.solar
        + pd.Series(reihen["Wind onshore"], index=t, dtype=float) / 1000
        + pd.Series(reihen["Wind offshore"], index=t, dtype=float) / 1000
    )
    df["prog_minus_ist"] = fc.sum(axis=1, min_count=3) - ist
    df = df.dropna(subset=["price", "load", "residual", "import"])
    jahr = (df.index >= pd.Timestamp(JAHR[0], tz=TZ)) & (df.index < pd.Timestamp(JAHR[1], tz=TZ))
    LUECKEN["prognose_viertelstunden_fehlend_als_0"] = int(df.prog_minus_ist[jahr].isna().sum())
    df["prog_minus_ist"] = df.prog_minus_ist.fillna(0)
    return df


def produkte(df: pd.DataFrame) -> pd.DataFrame:
    """Auflösung der Auktion: Stunden bis 30.09.2025, Viertelstunden ab 01.10.2025."""
    alt = df[df.index < VIERTEL_AB]
    std = alt.resample("1h").mean()
    std["dt"] = 1.0
    neu = df[df.index >= VIERTEL_AB].copy()
    neu["dt"] = 0.25
    out = pd.concat([std, neu]).dropna()
    a, b = (pd.Timestamp(t, tz=TZ) for t in JAHR)
    return out[(out.index >= a) & (out.index < b)]


# ---------------------------------------------------------------- Angebotskurven
def _isoton(y: np.ndarray, w: np.ndarray) -> np.ndarray:
    """Pool-Adjacent-Violators: monoton steigende Anpassung."""
    wert, gew, lang = [], [], []
    for yi, wi in zip(y, w, strict=True):
        wert.append(yi)
        gew.append(wi)
        lang.append(1)
        while len(wert) > 1 and wert[-2] > wert[-1]:
            g2 = gew[-2] + gew[-1]
            wert[-2] = (wert[-2] * gew[-2] + wert[-1] * gew[-1]) / g2
            gew[-2] = g2
            lang[-2] += lang[-1]
            wert.pop()
            gew.pop()
            lang.pop()
    return np.repeat(wert, lang)


def _auf_gitter(rx: np.ndarray, fx: np.ndarray) -> np.ndarray:
    """Stützstellen auf GITTER, Ränder mit der Steigung der äußeren 5 GW fortgesetzt."""
    i5 = np.searchsorted(rx, rx[0] + 5)
    j5 = np.searchsorted(rx, rx[-1] - 5)
    lo = (fx[min(i5, len(fx) - 1)] - fx[0]) / max(rx[min(i5, len(rx) - 1)] - rx[0], 1)
    hi = (fx[-1] - fx[max(j5, 0)]) / max(rx[-1] - rx[max(j5, 0)], 1)
    g = np.interp(GITTER, rx, fx)
    g = np.where(rx[0] > GITTER, fx[0] + max(lo, 0) * (GITTER - rx[0]), g)
    g = np.where(rx[-1] < GITTER, fx[-1] + max(hi, 0) * (GITTER - rx[-1]), g)
    return np.asarray(g, dtype=float)


def kurve_isoton(q: np.ndarray, p: np.ndarray) -> np.ndarray:
    o = np.argsort(q)
    q, p = q[o], p[o]
    f = _isoton(p, np.ones_like(p))
    u, inv = np.unique(np.floor(q), return_inverse=True)
    fx = np.bincount(inv, f) / np.bincount(inv)
    return _auf_gitter(u + 0.5, fx)


def kurve_tageseffekt(
    q: np.ndarray, p: np.ndarray, tag: np.ndarray, schritt: float = 2.0
) -> np.ndarray:
    """p = a_tag + f(q), f stückweise linear (Knoten alle ``schritt`` GW), dann monoton."""
    kn = np.arange(np.floor(q.min() / schritt) * schritt, q.max() + schritt, schritt)
    nk = len(kn)
    # Hutfunktionen
    i = np.clip(np.searchsorted(kn, q) - 1, 0, nk - 2)
    w = (q - kn[i]) / schritt
    n = len(q)
    rows = np.repeat(np.arange(n), 2)
    cols = np.stack([i, i + 1], 1).ravel()
    vals = np.stack([1 - w, w], 1).ravel()
    Xf = sparse.csr_matrix((vals, (rows, cols)), shape=(n, nk))
    ut, ti = np.unique(tag, return_inverse=True)
    Xd = sparse.csr_matrix((np.ones(n), (np.arange(n), ti)), shape=(n, len(ut)))
    # Glättung: zweite Differenzen von f klein, f am ersten Knoten = 0 (Identifikation)
    lam = 3.0 * np.sqrt(n / nk)
    D = sparse.diags([1, -2, 1], [0, 1, 2], shape=(nk - 2, nk))
    X = sparse.vstack(
        [
            sparse.hstack([Xf, Xd]),
            sparse.hstack([lam * D, sparse.csr_matrix((nk - 2, len(ut)))]),
            sparse.hstack(
                [
                    sparse.csr_matrix(([1.0], ([0], [0])), shape=(1, nk)),
                    sparse.csr_matrix((1, len(ut))),
                ]
            ),
        ]
    ).tocsr()
    y = np.concatenate([p, np.zeros(nk - 1)])
    sol = splinalg.lsqr(X, y, atol=1e-10, btol=1e-10, iter_lim=20000)[0]
    f = sol[:nk]
    gew = np.asarray(Xf.sum(0)).ravel() + 1e-3
    f = _isoton(f, gew)
    # Pegel: mittlerer Tagesfesteffekt, nur für die Ansicht (Differenzen zählen)
    f = f + np.average(sol[nk:][ti], weights=None) if len(ut) else f
    belegt = gew > 3
    return _auf_gitter(kn[belegt], f[belegt])


def kurven(stunden: pd.DataFrame) -> dict[str, dict[pd.Period, np.ndarray]]:
    """Je Variante und Monat eine Kurve auf GITTER, aus Stundenwerten Monat ± 1."""
    mon = stunden.index.tz_localize(None).to_period("M")
    out: dict[str, dict[pd.Period, np.ndarray]] = {"A": {}, "B": {}, "C": {}}
    for m in pd.period_range("2025-01", "2025-12", freq="M"):
        s = stunden[(mon >= m - 1) & (mon <= m + 1)]
        p = s.price.values
        out["A"][m] = kurve_isoton(s.residual.values, p)
        out["B"][m] = kurve_isoton(s.steuerbar.values, p)
        out["C"][m] = kurve_tageseffekt(
            s.steuerbar.values, p, s.index.date.astype("datetime64[D]").astype(int)
        )
    return out


# ---------------------------------------------------------------- Simulation
def steigung_preisniveau(stunden: pd.DataFrame) -> pd.DataFrame:
    """s(p): OLS durch den Ursprung von dp auf dQ je Preisklasse.

    Paare aufeinanderfolgender Stunden, |dQ| > 0,3 GW.
    """
    st = stunden[
        (stunden.index >= pd.Timestamp(JAHR[0], tz=TZ))
        & (stunden.index < pd.Timestamp(JAHR[1], tz=TZ))
    ]
    p, q = st.price.values, st.steuerbar.values
    ok = (st.index[1:] - st.index[:-1]).total_seconds().values == 3600
    dp, dq, pm = np.diff(p), np.diff(q), (p[1:] + p[:-1]) / 2
    zeilen = []
    for lo, hi in itertools.pairwise(KLASSEN):
        m = ok & (pm >= lo) & (pm < hi) & (np.abs(dq) > 0.3)
        zeilen.append(
            dict(
                von=lo,
                bis=hi,
                paare=int(m.sum()),
                preis_median=float(np.median(pm[m])),
                steigung=float((dp[m] * dq[m]).sum() / (dq[m] ** 2).sum()),
                steigung_median=float(np.median(dp[m] / dq[m])),
            )
        )
    return pd.DataFrame(zeilen)


class PreisD:
    """Variante D: dp/dx = s(p), von jedem Ist-Preis aus integriert."""

    def __init__(
        self, df: pd.DataFrame, s: pd.DataFrame, plateau: np.ndarray | None = None
    ) -> None:
        self.p = df.price.values
        # GW, die in Stunden mit negativem Preis abgeregelt sind und zusätzliche Last zum Ist-Preis
        # decken, bevor der Preis steigt (Varianten E, E2)
        self.plateau = np.zeros(len(df)) if plateau is None else plateau
        sp = np.interp(PGITTER, s.preis_median.values, s.steigung.values)
        sp = np.maximum(sp, 0.2)
        # F(p) = Menge (GW), die den Preis von PGITTER[0] auf p hebt
        self.F = np.concatenate([[0.0], np.cumsum(np.diff(PGITTER) / ((sp[1:] + sp[:-1]) / 2))])
        self.F0 = np.interp(self.p, PGITTER, self.F)

    def delta(self, idx: np.ndarray, x: np.ndarray) -> np.ndarray:
        pl = self.plateau[idx][:, None]
        x = np.where(x > 0, np.maximum(x - pl, 0), x)
        f = self.F0[idx][:, None] + x
        return np.asarray(np.interp(f, self.F, PGITTER) - self.p[idx][:, None])

    def bei(self, idx: np.ndarray, x: np.ndarray) -> np.ndarray:
        return np.asarray(self.p[idx][:, None] + self.delta(idx, x))


class Preis:
    def __init__(self, df: pd.DataFrame, variante: str, kv: dict[pd.Period, np.ndarray]) -> None:
        self.p = df.price.values
        self.q = (df.residual if variante == "A" else df.steuerbar).values
        self.mon = np.asarray(df.index.tz_localize(None).to_period("M"))
        self.kv = kv

    def delta(self, idx: np.ndarray, x: np.ndarray) -> np.ndarray:
        out = np.empty_like(x)
        for m in np.unique(self.mon[idx]):
            j = np.where(self.mon[idx] == m)[0]
            g = self.kv[m]
            q = self.q[idx[j]][:, None]
            out[j] = np.interp(q + x[j], GITTER, g) - np.interp(q, GITTER, g)
        return out

    def bei(self, idx: np.ndarray, x: np.ndarray) -> np.ndarray:
        return np.asarray(self.p[idx][:, None] + self.delta(idx, x))


def block_optimieren(
    preis: Preis | PreisD, idx: np.ndarray, dt: np.ndarray, alterung: float
) -> np.ndarray:
    """Wohlfahrtsoptimaler Fahrplan x (GW, + Laden) im Block idx, Speicher Anfang/Ende halb voll."""
    n = len(idx)
    w = P_GW / K
    mitte = (np.arange(K) + 0.5) * w
    cu = preis.bei(idx, np.tile(mitte, (n, 1)))
    cd = preis.bei(idx, -np.tile(mitte, (n, 1)))
    nu = n * K
    dtk = np.repeat(dt, K)
    c = np.concatenate([cu.ravel() * dtk, (-cd.ravel() + alterung) * dtk, np.zeros(n)])
    e = np.sqrt(ETA)
    z = np.repeat(np.arange(n), K)
    A_u = sparse.csr_matrix((-e * dtk, (z, np.arange(nu))), shape=(n, nu))
    A_d = sparse.csr_matrix((dtk / e, (z, np.arange(nu))), shape=(n, nu))
    A_e = sparse.eye(n) - sparse.eye(n, k=-1)
    A = sparse.hstack([A_u, A_d, A_e]).tocsr()
    b = np.zeros(n)
    b[0] = E_GWH / 2
    bounds = [(0, w)] * (2 * nu) + [(0, E_GWH)] * (n - 1) + [(E_GWH / 2, E_GWH)]
    res = linprog(c, A_eq=A, b_eq=b, bounds=bounds, method="highs")
    if res.status != 0:
        raise RuntimeError(res.message)
    u = res.x[:nu].reshape(n, K).sum(1)
    d = res.x[nu : 2 * nu].reshape(n, K).sum(1)
    return np.asarray(u - d)


def simulieren(preis: Preis | PreisD, df: pd.DataFrame, alterung: float) -> np.ndarray:
    x = np.zeros(len(df))
    tage = df.index.normalize()
    woche = ((tage - tage[0]).days // 7).values * 2 + (df.index >= VIERTEL_AB)
    for wk in np.unique(woche):
        idx = np.where(woche == wk)[0]
        x[idx] = block_optimieren(preis, idx, df.dt.values[idx], alterung)
    return x


def kennzahlen(df: pd.DataFrame, p: np.ndarray) -> dict[str, Any]:
    dt = df.dt.values
    s = pd.Series(p, index=df.index)
    tag = s.groupby(df.index.date)
    pv = df.solar.values * dt
    return dict(
        ueber_200_h=float(dt[p > 200].sum()),
        ueber_180_h=float(dt[p > 180].sum()),
        ueber_220_h=float(dt[p > 220].sum()),
        neg_h=float(dt[p < 0].sum()),
        null_oder_neg_h=float(dt[p <= 0].sum()),
        spanne=float((tag.max() - tag.min()).mean()),
        mittel=float(np.average(p, weights=dt)),
        pv_marktwert=float((p * pv).sum() / pv.sum()),
        max=float(p.max()),
        min=float(p.min()),
    )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--daten", type=Path, required=True)
    a = ap.parse_args()
    gestartet = datetime.now(UTC).isoformat(timespec="seconds")
    tab, gra = HIER / "tabellen", HIER / "grafiken"
    tab.mkdir(exist_ok=True)
    gra.mkdir(exist_ok=True)

    roh = lade(a.daten)
    stunden = roh.resample("1h").mean().dropna()
    df = produkte(roh)
    print("Zeitschritte", len(df), "Stunden", df.dt.sum())
    kv = kurven(stunden)

    zeilen = [dict(variante="Ist 2025", alterung=np.nan, **kennzahlen(df, df.price.values))]
    profile: dict[str, tuple[np.ndarray, np.ndarray]] = {}
    sd = steigung_preisniveau(stunden)
    sd.round(2).to_csv(tab / "steigung_preisniveau.csv", index=False)
    print(sd)
    for v in VARIANTEN:
        if v in ("D", "E", "E2"):
            pl = np.where(
                df.price.values < 0,
                np.clip(df.prog_minus_ist.values - PROGNOSEFEHLER_GW, 0, None),
                0,
            )
            faktor = {"D": 0.0, "E": 1.0, "E2": 2.0}[v]
            preis: Preis | PreisD = PreisD(df, sd, faktor * pl)
            if v == "E":
                print(
                    "Abregelung geschätzt: Mittel",
                    pl[pl > 0].mean(),
                    "GW in",
                    (df.dt.values * (df.price.values < 0)).sum(),
                    "h",
                )
        else:
            preis = Preis(df, v, kv[v])
        for alt in ALTERUNG:
            x = simulieren(preis, df, alt)
            p_neu = preis.p + preis.delta(np.arange(len(df)), x[:, None])[:, 0]
            kz = kennzahlen(df, p_neu)
            kz["zyklen"] = float(np.clip(x, 0, None) @ df.dt.values * np.sqrt(ETA) / E_GWH)
            zeilen.append(dict(variante=v, alterung=alt, **kz))
            if alt == 0:
                profile[v] = (p_neu, x)
            print(zeilen[-1], flush=True)
    erg = pd.DataFrame(zeilen)
    pd.DataFrame(
        {"dt_h": df.dt.values, "ist": df.price.values}
        | {f"preis_{v}": profile[v][0] for v in VARIANTEN}
        | {f"speicher_gw_{v}": profile[v][1] for v in ["D", "E"]},
        index=df.index,
    ).round(2).to_csv(tab / "profil_2025.csv.gz")
    ise = pd.DataFrame(
        [
            dict(variante="ISE Ist", **{k: v[0] for k, v in ISE.items()}),
            dict(variante="ISE 20 GW / 40 GWh", **{k: v[1] for k, v in ISE.items()}),
        ]
    )
    pd.concat([ise, erg]).to_csv(tab / "ergebnis.csv", index=False, float_format="%.2f")

    # Steigung der Kurven bei typischen Lagen: €/MWh je GW zwischen Q-5 und Q+5 GW
    st = []
    for v in "ABC":
        for m, g in kv[v].items():
            for q0 in [0, 10, 20, 30, 40]:
                st.append(
                    dict(
                        variante=v,
                        monat=str(m),
                        q=q0,
                        steigung=(np.interp(q0 + 5, GITTER, g) - np.interp(q0 - 5, GITTER, g)) / 10,
                    )
                )
    pd.DataFrame(st).pivot_table(index=["variante", "q"], columns="monat", values="steigung").round(
        1
    ).to_csv(tab / "steigung.csv")

    # Profile ausgewählter Tage
    p0 = df.price.values
    for tag in ["2025-01-20", "2025-05-11"]:
        sel = (df.index >= pd.Timestamp(tag, tz=TZ)) & (
            df.index < pd.Timestamp(tag, tz=TZ) + pd.Timedelta("1D")
        )
        z = {"ist": p0[sel]}
        for v in VARIANTEN:
            z[v] = profile[v][0][sel]
            z[f"x_{v}"] = profile[v][1][sel]
        pd.DataFrame(z, index=df.index[sel].strftime("%H:%M")).round(1).to_csv(
            tab / f"tag_{tag}.csv"
        )

    # Grafik 1: Kurven Januar, Mai
    fig, ax = plt.subplots(1, 2, figsize=(12, 4.5))
    for i, m in enumerate([pd.Period("2025-01", "M"), pd.Period("2025-05", "M")]):
        for v, lab in [
            ("A", "A: Residuallast"),
            ("B", "B: Austausch fest"),
            ("C", "C: B mit Tageseffekt"),
        ]:
            g = kv[v][m]
            sel = (GITTER > -30) & (GITTER < 75)
            ax[i].plot(GITTER[sel], g[sel], color=FARBE[v], label=lab)
        ax[i].set_title(f"{m} (Monat ± 1)")
        ax[i].set_xlabel("Q in GW (A: Residuallast, B/C: Residuallast - Nettoimport)")
        ax[i].set_ylabel("€/MWh")
        ax[i].set_ylim(-150, 350)
        ax[i].grid(alpha=0.3)
        ax[i].legend(fontsize=8)
    fig.tight_layout()
    fig.savefig(gra / "kurven.png", dpi=130)
    plt.close(fig)

    # Grafik: Steigung nach Preisniveau (Variante D)
    fig, ax = plt.subplots(figsize=(7, 4.2))
    ax.plot(sd.preis_median, sd.steigung, "o-", color=FARBE["D"], label="OLS je Preisklasse")
    ax.plot(
        sd.preis_median,
        sd.steigung_median,
        "s:",
        color=FARBE["D"],
        alpha=0.6,
        label="Median der Quotienten",
    )
    ax.set_xlabel("Preisniveau €/MWh (Median der Klasse)")
    ax.set_ylabel("€/MWh je GW")
    ax.set_xlim(-100, 330)
    ax.grid(alpha=0.3)
    ax.legend()
    ax.set_title("Preisänderung je GW inländischer Bedarfsänderung, 2025")
    fig.tight_layout()
    fig.savefig(gra / "steigung_preisniveau.png", dpi=130)
    plt.close(fig)

    # Grafik: zwei Extremtage
    fig, ax = plt.subplots(1, 2, figsize=(13, 4.4))
    for i, (tag, ise_wert, titel) in enumerate(
        [
            ("2025-01-20", 221, "20.01.2025 (teuerster Tag)"),
            ("2025-05-11", -20, "11.05.2025 (billigster Tag)"),
        ]
    ):
        sel = (df.index >= pd.Timestamp(tag, tz=TZ)) & (
            df.index < pd.Timestamp(tag, tz=TZ) + pd.Timedelta("1D")
        )
        h = df.index[sel].hour + df.index[sel].minute / 60
        ax[i].step(h, p0[sel], where="post", color="#999999", label="Ist")
        for v in ["A", "B", "E"]:
            ax[i].step(h, profile[v][0][sel], where="post", color=FARBE[v], label=f"Variante {v}")
        ax[i].axhline(
            ise_wert, color=FARBE["ISE"], ls="--", lw=1, label=f"ISE: Extremwert {ise_wert} €/MWh"
        )
        ax[i].set_title(titel)
        ax[i].set_xlabel("Uhrzeit")
        ax[i].set_ylabel("€/MWh")
        ax[i].grid(alpha=0.3)
        ax[i].legend(fontsize=8)
    fig.tight_layout()
    fig.savefig(gra / "extremtage.png", dpi=130)
    plt.close(fig)

    # Grafik 2: Vergleich der Stunden
    fig, ax = plt.subplots(1, 3, figsize=(15, 4.4))
    namen = ["Ist", "ISE", *VARIANTEN]
    farben = ["#999999", FARBE["ISE"]] + [FARBE[v] for v in VARIANTEN]
    e0 = erg[(erg.variante.isin(list(VARIANTEN))) & (erg.alterung == 0)].set_index("variante")
    for i, (k, titel) in enumerate(
        [
            ("ueber_200_h", "Stunden > 200 €/MWh"),
            ("neg_h", "Stunden < 0 €/MWh"),
            ("spanne", "Tagesspanne €/MWh"),
        ]
    ):
        werte = [erg.iloc[0][k], ISE[k][1]] + [e0.loc[v, k] for v in VARIANTEN]
        ax[i].bar(namen, werte, color=farben)
        for j, w in enumerate(werte):
            ax[i].text(j, w, f"{w:.0f}", ha="center", va="bottom", fontsize=9)
        ax[i].set_title(titel)
        ax[i].grid(alpha=0.3, axis="y")
    fig.suptitle("2025 mit 20 GW / 40 GWh zusätzlichen Batteriespeichern")
    fig.tight_layout()
    fig.savefig(gra / "vergleich.png", dpi=130)
    plt.close(fig)

    # Grafik 3: Preisdauerlinie
    fig, ax = plt.subplots(figsize=(8, 4.5))
    dt = df.dt.values
    for lab, p, f in [("Ist 2025", p0, "#999999")] + [
        (f"Variante {v}", profile[v][0], FARBE[v]) for v in VARIANTEN
    ]:
        o = np.argsort(-p)
        ax.plot(np.cumsum(dt[o]), p[o], color=f, label=lab, lw=1.2)
    ax.axhline(200, color="k", lw=0.5, ls="--")
    ax.axhline(0, color="k", lw=0.5)
    ax.set_ylim(-150, 400)
    ax.set_xlabel("Stunden im Jahr")
    ax.set_ylabel("€/MWh")
    ax.grid(alpha=0.3)
    ax.legend()
    fig.tight_layout()
    fig.savefig(gra / "preisdauerlinie.png", dpi=130)
    plt.close(fig)

    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"], capture_output=True, text=True, cwd=HIER
    ).stdout.strip()
    lauf = {
        "job": JOB["id"],
        "job_sha256": hashlib.sha256(JOB_PFAD.read_bytes()).hexdigest(),
        "git_commit": commit,
        "gestartet": gestartet,
        "beendet": datetime.now(UTC).isoformat(timespec="seconds"),
        "eingaben_sha256": {
            f.name: hashlib.sha256(f.read_bytes()).hexdigest()
            for f in sorted(a.daten.glob("*.json"))
        },
        "zeitraum": {"von": JAHR[0], "bis": JAHR[1]},
        "zeitschritte": {
            "stunden_bis_30_09": int((df.dt == 1.0).sum()),
            "viertelstunden_ab_01_10": int((df.dt == 0.25).sum()),
            "stunden_gesamt": float(df.dt.sum()),
        },
        "fehlende_werte_ersetzt": LUECKEN,
        "schritte_zeitlich_interpoliert": {},
        "varianten": VARIANTEN,
        "alterung_eur_mwh": ALTERUNG,
    }
    (tab / "lauf.json").write_text(json.dumps(lauf, indent=1, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main()
