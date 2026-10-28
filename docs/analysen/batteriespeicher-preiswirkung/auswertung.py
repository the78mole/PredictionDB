"""Preiswirkung von Batteriespeichern am Day-Ahead-Markt DE-LU.

Einmalige Auswertung vom 28.10.2026. Drei Teile:

1. **Bestand und Ausbau** aus dem Marktstammdatenregister (öffentliche Einheitenübersicht,
   ``mastr_abruf.py``): Großspeicher ab 1 MW, Gewerbespeicher 30 kW bis 1 MW, Heimspeicher bis
   30 kW, jeweils mit Inbetriebnahme, Stilllegung und gemeldeter Planung.
2. **Simulation**: Ein zusätzlicher, am Markt optimierter Speicherpark wird in die Stunden des
   letzten Jahres (SMARD, ``smard_abruf.py``) gelegt. Er lädt, solange der Preis dadurch weniger
   steigt, als er beim Entladen sinkt (Wohlfahrtsoptimum bei vollständigem Wettbewerb, lineares
   Programm je Woche mit perfekter Voraussicht). Wie stark eine zusätzliche Last oder Einspeisung
   den Preis verschiebt, kommt aus einer von zwei Angebotskurven:

   - *empirisch*: monotone Kurve Preis über Residuallast, je Monat aus den SMARD-Stunden des
     Monats und seiner Nachbarmonate geschätzt. Sie enthält den Austausch mit den Nachbarländern
     und flacht durch Streuung eher ab (untere Schranke der Wirkung).
   - *Merit-Order*: die Treppe aus ``docs/analysen/merit-order`` (steuerbare Kraftwerke ohne
     Austausch, steiler, obere Schranke).

   Beide Kurven verschieben nur den gemessenen Preis (``p_neu = p_alt + g(R + x) - g(R)``), das
   Niveau bleibt also das gemessene. Der Bestand des Basisjahres steckt bereits in den Preisen,
   simuliert wird der Zubau darüber hinaus.
3. **Zeitachse**: Ausbaupfade für den marktgeführten Park aus den MaStR-Daten, daraus das Jahr,
   in dem die Wirkung die Merklichkeitsschwellen überschreitet.

Aufruf aus dem Repository-Wurzelverzeichnis::

    uv run --no-project --with pandas --with numpy --with scipy --with matplotlib \\
        python docs/analysen/batteriespeicher-preiswirkung/auswertung.py --daten ./speicher-daten
"""

from __future__ import annotations

import argparse
import gzip
import json
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from scipy import sparse
from scipy.optimize import linprog

HIER = Path(__file__).resolve().parent
TREPPE = HIER.parent / "merit-order" / "tabellen"
TZ = "Europe/Berlin"
STICHTAG = pd.Timestamp("2026-10-28")

# ---------------------------------------------------------------- Annahmen
BASIS = ("2025-10-27", "2026-10-27")  # Basisjahr, 365 Tage, Ende ausschließlich
ETA = 0.86  # Wirkungsgrad Laden + Entladen (Netz zu Netz) neuer Lithium-Großspeicher
DAUER = 2.0  # h, Energie / Leistung des Zubaus (MaStR: Bestand 1,8 h, Planung 2,9 h)
MO_KORREKTUR = 9.25  # GW, Kalibrierung der Merit-Order-Treppe (docs/analysen/merit-order)
ZUBAU_GW = [0, 2, 5, 10, 15, 20, 30, 40]
DAUER_VARIANTEN = [1.0, 4.0]  # bei 10 GW
#: Anteil der Leistung, der am Day-Ahead-Markt geführt wird
MARKTANTEIL = {"gross": 1.0, "gewerbe": 0.3, "heim": 0.0}
#: Merklichkeitsschwellen gegenüber dem Basisjahr
SCHWELLEN = {
    "spanne_rel": (-0.10, "mittlere Tagesspanne -10 %"),
    "abend_diff": (-5.0, "Abendpreis 17-21 Uhr -5 €/MWh"),
    "mittag_diff": (5.0, "Mittagspreis 11-15 Uhr +5 €/MWh"),
    "neg_rel": (-0.25, "Stunden mit negativem Preis -25 %"),
}
#: EE-Zubau-Szenario: zusätzliche Einspeisung als Anteil der gemessenen (etwa Stand 2029/30)
EE_PLUS = {"solar": 0.5, "wind": 0.25}
FARBE = {
    "empirisch": "#2f6db3",
    "merit-order": "#c4562b",
    "gross": "#2f6db3",
    "gewerbe": "#7aa457",
    "heim": "#d9a521",
    "plan": "#888888",
}


# ---------------------------------------------------------------- Daten
def lade_smard(ordner: Path) -> pd.DataFrame:
    reihen = {}
    for f in sorted(ordner.glob("*.json")):
        d = json.loads(f.read_text())
        reihen[f.stem] = pd.Series({int(k): v for k, v in d.items()}, dtype=float)
    df = pd.DataFrame(reihen)
    df.index = pd.to_datetime(df.index, unit="ms", utc=True).tz_convert(TZ)
    df = df.sort_index()
    for c in df.columns:
        if c != "price":
            df[c] = df[c] / 1000.0  # MWh je Stunde -> GW
    return df


def _json_gz(pfad: Path) -> Any:
    with gzip.open(pfad, "rt") as f:
        return json.load(f)


def _ms_datum(s: pd.Series) -> pd.Series:
    ms = s.fillna("").astype(str).str.extract(r"(-?\d+)")[0].astype(float)
    return pd.to_datetime(ms, unit="ms")


def lade_mastr(ordner: Path) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    g = pd.DataFrame(_json_gz(ordner / "gross.json.gz"))
    for c in [
        "InbetriebnahmeDatum",
        "GeplantesInbetriebsnahmeDatum",
        "EndgueltigeStilllegungDatum",
    ]:
        g[c] = _ms_datum(g[c])
    g = g[g.StromspeichertechnologieBezeichnung == "Batterie"].copy()
    g["klasse"] = np.where(g.Nettonennleistung >= 1000, "gross", "gewerbe")
    anz = json.loads((ordner / "klein_anzahl.json").read_text())
    k = pd.DataFrame(anz["counts"])
    k["monat"] = pd.PeriodIndex(k.monat, freq="M")
    s = pd.DataFrame(_json_gz(ordner / "klein_stichprobe.json.gz"))
    s["InbetriebnahmeDatum"] = _ms_datum(s.InbetriebnahmeDatum)
    s = s[s.StromspeichertechnologieBezeichnung == "Batterie"]
    return g, k, s


# ---------------------------------------------------------------- Teil 1: Bestand
def bestand(g: pd.DataFrame, k: pd.DataFrame, s: pd.DataFrame) -> pd.DataFrame:
    """Monatsende-Bestand in GW / GWh je Klasse (in Betrieb, gemessen am MaStR-Stand)."""
    monate = pd.period_range("2012-01", "2026-10", freq="M")
    ende = monate.to_timestamp(how="end")
    zeilen = []
    lauf = g[g.BetriebsStatusName != "In Planung"]
    for kl in ["gross", "gewerbe"]:
        t = lauf[lauf.klasse == kl]
        for m, e in zip(monate, ende, strict=True):
            an = (t.InbetriebnahmeDatum <= e) & ~(t.EndgueltigeStilllegungDatum <= e)
            zeilen.append(
                dict(
                    monat=m,
                    klasse=kl,
                    gw=t.Nettonennleistung[an].sum() / 1e6,
                    gwh=t.NutzbareSpeicherkapazitaet[an].sum() / 1e6,
                    anzahl=int(an.sum()),
                )
            )
    # Heimspeicher: Anzahl je Inbetriebnahmemonat x mittlere Größe der Stichprobe des Jahres
    s = s.assign(jahr=s.InbetriebnahmeDatum.dt.year)
    mittel = s.groupby("jahr")[["Nettonennleistung", "NutzbareSpeicherkapazitaet"]].mean()
    k = k.assign(jahr=k.monat.dt.year)
    k = k.join(mittel, on="jahr")
    k["kw"] = k.anzahl * k.Nettonennleistung
    k["kwh"] = k.anzahl * k.NutzbareSpeicherkapazitaet
    k = k.set_index("monat").reindex(monate).fillna(0)
    for m, kw, kwh, n in zip(monate, k.kw.cumsum(), k.kwh.cumsum(), k.anzahl.cumsum(), strict=True):
        zeilen.append(dict(monat=m, klasse="heim", gw=kw / 1e6, gwh=kwh / 1e6, anzahl=int(n)))
    return pd.DataFrame(zeilen)


def planung(g: pd.DataFrame) -> pd.DataFrame:
    p = g[g.BetriebsStatusName == "In Planung"].copy()
    jahr = p.GeplantesInbetriebsnahmeDatum.dt.year
    # überfällige oder fehlende Termine: frühestens im nächsten Jahr
    p["jahr"] = jahr.where(p.GeplantesInbetriebsnahmeDatum >= STICHTAG, 2027).astype(int)
    return (
        p.groupby(["jahr", "klasse"])
        .agg(
            anzahl=("MaStRNummer", "size"),
            gw=("Nettonennleistung", lambda x: x.sum() / 1e6),
            gwh=("NutzbareSpeicherkapazitaet", lambda x: x.sum() / 1e6),
        )
        .reset_index()
    )


# ---------------------------------------------------------------- Teil 2: Angebotskurven
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


GITTER = np.arange(-60.0, 120.01, 0.5)  # GW Residuallast


def kurve_empirisch(df: pd.DataFrame, monat: pd.Period) -> np.ndarray:
    """Monotone Kurve Preis über Residuallast auf GITTER, Monat ± 1."""
    nah = df[
        (df.index.tz_localize(None).to_period("M") >= monat - 1)
        & (df.index.tz_localize(None).to_period("M") <= monat + 1)
    ].dropna(subset=["price", "residual"])
    o = np.argsort(nah.residual.values)
    r, p = nah.residual.values[o], nah.price.values[o]
    f = _isoton(p, np.ones_like(p))
    # Fächer der Stützstellen: Mittel je 1-GW-Klasse, dann lineare Interpolation
    kl = np.floor(r)
    u, inv = np.unique(kl, return_inverse=True)
    fx = np.bincount(inv, f) / np.bincount(inv)
    rx = u + 0.5
    # Ränder: Steigung der äußeren 5 GW fortsetzen
    lo = (fx[min(5, len(fx) - 1)] - fx[0]) / max(rx[min(5, len(rx) - 1)] - rx[0], 1)
    hi = (fx[-1] - fx[max(-6, -len(fx))]) / max(rx[-1] - rx[max(-6, -len(rx))], 1)
    g = np.interp(GITTER, rx, fx)
    g = np.where(rx[0] > GITTER, fx[0] + lo * (GITTER - rx[0]), g)
    g = np.where(rx[-1] < GITTER, fx[-1] + hi * (GITTER - rx[-1]), g)
    return np.asarray(g, dtype=float)


def lade_treppe() -> dict[str, tuple[np.ndarray, np.ndarray]]:
    t = {}
    for jz in ["sommer", "jetzt", "winter"]:
        d = pd.read_csv(TREPPE / f"treppe_{jz}.csv.gz").sort_values("c")
        cum = d.P.cumsum().values / 1000.0
        # Auf 50 MW auflösen und über 1 GW glätten: Verfügbarkeit und Gebote einzelner Blöcke
        # sind unsicher, der Sprung Kohle -> Gas soll keine Stufe von 80 €/MWh in 1 MW sein.
        x = np.arange(0, cum[-1], 0.05)
        c = np.interp(x, cum, d.c.values)
        c = pd.Series(c).rolling(21, center=True, min_periods=1).mean().values
        t[jz] = (x, c)
    return t


def jahreszeit(m: int) -> str:
    return "sommer" if m in (5, 6, 7, 8, 9) else "winter" if m in (12, 1, 2) else "jetzt"


class Preis:
    """Preis je Stunde als Funktion der Zusatzlast x (GW, positiv = Laden)."""

    def __init__(
        self, df: pd.DataFrame, variante: str, treppe: dict[str, tuple[np.ndarray, np.ndarray]]
    ) -> None:
        self.p = df.price.values
        self.r = df.residual.values
        mon = df.index.tz_localize(None).to_period("M")
        self.kurven = {m: kurve_empirisch(df_all, m) for m in mon.unique()}
        self.mon = np.asarray(mon)
        self.var = variante
        # zusätzliche Last je Stunde vor dem Speicher (EE-Zubau < 0)
        self.shift = np.zeros(len(df))
        if variante == "merit-order":
            # Bedarf an steuerbarer Leistung:
            # Residuallast - Biomasse - Wasser - sonstige EE + Korrektur
            self.d = (
                df.residual - df.biomass - df.hydro - df.other_re.fillna(0)
            ).values + MO_KORREKTUR
            self.jz = np.array([jahreszeit(m.month) for m in mon])
            self.treppe = treppe

    def _g(self, idx: np.ndarray, x: np.ndarray) -> np.ndarray:
        """g(R + s + x) - g(R) für Stunden idx (n) und Zusatzlasten x (n, k), s = self.shift."""
        x = x + self.shift[idx][:, None]
        out = np.empty_like(x)
        for m in np.unique(self.mon[idx]):
            j = np.where(self.mon[idx] == m)[0]
            g = self.kurven[m]
            r = self.r[idx[j]][:, None]
            emp = np.interp(r + x[j], GITTER, g) - np.interp(r, GITTER, g)
            if self.var == "empirisch":
                out[j] = emp
                continue
            # Merit-Order: Treppe für den steuerbaren Bereich, darunter die empirische Kurve
            for jz in np.unique(self.jz[idx[j]]):
                jj = j[self.jz[idx[j]] == jz]
                cum, c = self.treppe[jz]
                d = self.d[idx[jj]][:, None]
                y = d + x[jj]

                def tr(v: np.ndarray, cum: np.ndarray = cum, c: np.ndarray = c) -> np.ndarray:
                    # oberhalb der Treppe: 50 €/MWh je GW (Knappheit, Importe)
                    out: np.ndarray = np.interp(np.clip(v, 0, cum[-1]), cum, c)
                    return np.asarray(out + np.maximum(v - cum[-1], 0) * 50)

                # unterhalb 0 (Überschuss): empirische Steigung, an 0 angesetzt
                r0 = self.r[idx[jj]][:, None]
                e_y = np.interp(r0 + x[jj], GITTER, g)
                e_0 = np.interp(r0 - d, GITTER, g)  # Residuallast, bei der d = 0
                h_y = np.where(y >= 0, tr(y), tr(np.zeros_like(y)) + (e_y - e_0))
                h_d = np.where(
                    d >= 0, tr(d), tr(np.zeros_like(d)) + (np.interp(r0, GITTER, g) - e_0)
                )
                out[jj] = h_y - h_d
        return out

    def bei(self, idx: np.ndarray, x: np.ndarray) -> np.ndarray:
        return np.asarray(self.p[idx][:, None] + self._g(idx, x))


df_all: pd.DataFrame  # für kurve_empirisch (Nachbarmonate über das Basisjahr hinaus)


# ---------------------------------------------------------------- Teil 2: Optimierung
def woche_optimieren(preis: Preis, idx: np.ndarray, P: float, E: float, K: int = 40) -> np.ndarray:
    """Wohlfahrtsoptimaler Fahrplan x_t (GW, + Laden) für die Stunden idx."""
    n = len(idx)
    w = P / K
    mitte = (np.arange(K) + 0.5) * w
    cu = preis.bei(idx, np.tile(mitte, (n, 1)))  # Preis der zusätzlichen Last
    cd = preis.bei(idx, -np.tile(mitte, (n, 1)))  # Preis bei zusätzlicher Einspeisung
    nu = n * K
    c = np.concatenate([cu.ravel(), -cd.ravel(), np.zeros(n)])
    ec = ed = np.sqrt(ETA)
    zeilen = np.repeat(np.arange(n), K)
    A_u = sparse.csr_matrix((np.full(nu, -ec), (zeilen, np.arange(nu))), shape=(n, nu))
    A_d = sparse.csr_matrix((np.full(nu, 1 / ed), (zeilen, np.arange(nu))), shape=(n, nu))
    A_e = sparse.eye(n) - sparse.eye(n, k=-1)
    A = sparse.hstack([A_u, A_d, A_e]).tocsr()
    b = np.zeros(n)
    b[0] = E / 2
    bounds = [(0, w)] * (2 * nu) + [(0, E)] * (n - 1) + [(E / 2, E)]
    res = linprog(c, A_eq=A, b_eq=b, bounds=bounds, method="highs")
    if res.status != 0:
        raise RuntimeError(res.message)
    u = res.x[:nu].reshape(n, K).sum(1)
    d = res.x[nu : 2 * nu].reshape(n, K).sum(1)
    return np.asarray(u - d)


def simulieren(preis: Preis, basis: pd.DataFrame, P: float, E: float) -> np.ndarray:
    x = np.zeros(len(basis))
    if P <= 0:
        return x
    tage = basis.index.normalize()
    start = tage[0]
    woche = ((tage - start).days // 7).values
    for wk in np.unique(woche):
        idx = np.where(woche == wk)[0]
        x[idx] = woche_optimieren(preis, idx, P, E)
    return x


def kennzahlen(basis: pd.DataFrame, p: np.ndarray, x: np.ndarray) -> dict[str, Any]:
    s = pd.Series(p, index=basis.index)
    h = basis.index.hour
    tag = s.groupby(basis.index.date)
    last = basis.load.values
    return dict(
        mittel=s.mean(),
        lastgewichtet=float((s.values * last).sum() / last.sum()),
        abend=s[(h >= 17) & (h < 21)].mean(),
        mittag=s[(h >= 11) & (h < 15)].mean(),
        spanne=(tag.max() - tag.min()).mean(),
        p05=s.quantile(0.05),
        p95=s.quantile(0.95),
        std=s.std(),
        neg_h=int((s < 0).sum()),
        ueber_200_h=int((s > 200).sum()),
        zyklen=float(np.clip(x, 0, None).sum() * np.sqrt(ETA)),  # GWh geladen (Speicherseite)
        erloes=float((-x * s.values).sum()),  # € je MW... (GW x €/MWh x h = 1000 €)
    )


# ---------------------------------------------------------------- Teil 3: Zeitachse
#: Anteil des Parks, der am Day-Ahead-Preis wirkt (Regelleistung, unvollständige Voraussicht)
WIRKSAM = 0.7
JAHRE = range(2026, 2031)


def schwelle_gw(xs: np.ndarray, y: np.ndarray, wert: float) -> float:
    """Kleinster Zubau, bei dem y (steigend gedacht) den Wert erreicht, linear interpoliert."""
    hit = np.where(y >= wert)[0]
    if len(hit) == 0:
        return float("nan")
    i = hit[0]
    if i == 0:
        return float(xs[0])
    return float(xs[i - 1] + (wert - y[i - 1]) / (y[i] - y[i - 1]) * (xs[i] - xs[i - 1]))


def markt_gw(best: pd.DataFrame) -> pd.Series:
    b = best.pivot_table(index="monat", columns="klasse", values="gw")
    return sum(MARKTANTEIL[k] * b[k] for k in MARKTANTEIL)


def pfade(best: pd.DataFrame, plan: pd.DataFrame, zub: pd.DataFrame) -> pd.DataFrame:
    """Marktgeführte Leistung (GW) zum Jahresende je Ausbaupfad."""
    m = markt_gw(best)
    jetzt = m.iloc[-1]
    monate_2026 = best.monat.max().month
    z = zub.reset_index()
    z["markt"] = z.gw * z.klasse.map(MARKTANTEIL)
    zj = z.groupby("jahr").markt.sum()
    z26 = zj.get(2026, 0) * 12 / monate_2026
    z25 = zj.get(2025, 0)
    f = min(max(z26 / z25, 1.0), 1.5) if z25 else 1.0
    pl = plan.assign(markt=plan.gw * plan.klasse.map(MARKTANTEIL)).groupby("jahr").markt.sum()
    zeilen = []
    for j in JAHRE:
        rest = (12 - monate_2026) / 12 if j >= 2026 else 0
        konst = jetzt + z26 * (rest + max(j - 2026, 0))
        wachs = jetzt + z26 * rest + sum(z26 * f ** (k - 2026) for k in range(2027, j + 1))
        planpf = jetzt + pl[pl.index <= j].sum()
        zeilen += [
            dict(jahr=j, pfad="Zubau wie 2026", markt_gw=konst),
            dict(jahr=j, pfad=f"Zubau wächst x{f:.2f}/a", markt_gw=wachs),
            dict(jahr=j, pfad="MaStR-Planung termingerecht", markt_gw=planpf),
        ]
    return pd.DataFrame(zeilen), dict(jetzt=jetzt, zubau_2026=z26, zubau_2025=z25, faktor=f)


def zeitachse(
    best: pd.DataFrame, plan: pd.DataFrame, zub: pd.DataFrame, basis: pd.DataFrame, e: pd.DataFrame
) -> pd.DataFrame:
    m = markt_gw(best)
    mon = basis.index.tz_localize(None).to_period("M").unique()
    b0 = m.reindex(mon).mean()
    pf, info = pfade(best, plan, zub)
    print("Zeitachse", info, "Basis-Mittel", round(b0, 2))
    out = []
    for (sz, v), r in e[e.dauer_h == DAUER].groupby(["szenario", "variante"]):
        r = r.sort_values("zubau_gw")
        for _, z in pf.iterrows():
            d = max(WIRKSAM * (z.markt_gw - b0), 0)
            row = dict(
                jahr=z.jahr,
                pfad=z.pfad,
                szenario=sz,
                variante=v,
                markt_gw=z.markt_gw,
                zusatz_wirksam_gw=d,
            )
            for c in [
                "spanne_rel",
                "abend_diff",
                "mittag_diff",
                "neg_rel",
                "lastgewichtet_diff",
                "erloes_eur_kw_a",
            ]:
                row[c] = float(np.interp(d, r.zubau_gw, r[c]))
            out.append(row)
    return pd.DataFrame(out)


# ---------------------------------------------------------------- Teil 4: Rückblick
def rueckblick(df: pd.DataFrame) -> pd.DataFrame:
    """Tagesspanne des Preises je Jahr bei vergleichbarer Spanne der Residuallast."""
    d = df.dropna(subset=["price", "residual"])
    t = d.groupby(d.index.date).agg(
        pmax=("price", "max"),
        pmin=("price", "min"),
        rmax=("residual", "max"),
        rmin=("residual", "min"),
        pmit=("price", "mean"),
    )
    t.index = pd.to_datetime(t.index)
    t["spanne"] = t.pmax - t.pmin
    t["r_spanne"] = t.rmax - t.rmin
    t["rel"] = t.spanne / t.pmit.abs().clip(lower=20)
    t["klasse"] = pd.cut(t.r_spanne, [0, 15, 20, 25, 30, 35, 40, 60], right=False)
    t["jahr"] = t.index.year
    return (
        t[t.jahr >= 2021]
        .groupby(["klasse", "jahr"], observed=True)
        .agg(
            tage=("spanne", "size"),
            spanne_median=("spanne", "median"),
            relativ_median=("rel", "median"),
        )
        .reset_index()
    )


def ueberschuss(df: pd.DataFrame) -> pd.DataFrame:
    """Januar bis September je Jahr: Überschussstunden und wie oft der Preis dabei negativ war."""
    d = df.dropna(subset=["price", "residual"])
    d = d[(d.index.month <= 9) & (d.index.year >= 2021)]
    zeilen = []
    for y, s in d.groupby(d.index.year):
        knapp = s.residual < 5
        zeilen.append(
            dict(
                jahr=y,
                pv_twh=s.solar.sum() / 1000,
                h_residual_unter_0=int((s.residual < 0).sum()),
                h_residual_unter_5=int(knapp.sum()),
                h_preis_negativ=int((s.price < 0).sum()),
                anteil_negativ_bei_r_unter_5=float((s.price[knapp] < 0).mean())
                if knapp.any()
                else np.nan,
                median_preis_bei_r_unter_5=float(s.price[knapp].median())
                if knapp.any()
                else np.nan,
                h_preis_unter_minus_5=int((s.price <= -5).sum()),
            )
        )
    return pd.DataFrame(zeilen)


# ---------------------------------------------------------------- Hauptprogramm
def main() -> None:
    global df_all
    ap = argparse.ArgumentParser()
    ap.add_argument("--daten", type=Path, required=True)
    ap.add_argument("--schnell", action="store_true", help="nur jede vierte Woche simulieren")
    a = ap.parse_args()
    tab, gra = HIER / "tabellen", HIER / "grafiken"

    # Teil 1
    g, k, s = lade_mastr(a.daten / "mastr")
    best = bestand(g, k, s)
    plan = planung(g)
    best.to_csv(tab / "bestand_monat.csv", index=False, float_format="%.3f")
    plan.to_csv(tab / "planung_mastr.csv", index=False, float_format="%.3f")
    jahr = best[best.monat.dt.month == 12].copy()
    jahr = pd.concat([jahr, best[best.monat == best.monat.max()]])
    jahr["jahr"] = jahr.monat.dt.year
    tj = jahr.pivot_table(index="jahr", columns="klasse", values=["gw", "gwh"]).round(2)
    tj.columns = [f"{k}_{v}" for v, k in tj.columns]
    tj.to_csv(tab / "bestand_jahr.csv")
    print(tj.tail(8))
    print(plan)

    groesse = (
        s.assign(jahr=s.InbetriebnahmeDatum.dt.year)
        .groupby("jahr")
        .agg(
            n=("MaStRNummer", "size"),
            kw=("Nettonennleistung", "mean"),
            kwh=("NutzbareSpeicherkapazitaet", "mean"),
        )
    )
    groesse.round(2).to_csv(tab / "heimspeicher_stichprobe.csv")

    # Zubau Großspeicher je Jahr (Inbetriebnahme)
    lauf = g[(g.BetriebsStatusName != "In Planung")]
    zub = (
        lauf.assign(jahr=lauf.InbetriebnahmeDatum.dt.year)
        .groupby(["jahr", "klasse"])[["Nettonennleistung", "NutzbareSpeicherkapazitaet"]]
        .sum()
        / 1e6
    )
    zub.columns = ["gw", "gwh"]
    zub.round(3).to_csv(tab / "zubau_jahr.csv")

    fig, ax = plt.subplots(1, 2, figsize=(12, 4.5))
    for kl in ["heim", "gewerbe", "gross"]:
        b = best[best.klasse == kl]
        ax[0].plot(b.monat.dt.to_timestamp(), b.gw, label=kl, color=FARBE[kl])
        ax[1].plot(b.monat.dt.to_timestamp(), b.gwh, label=kl, color=FARBE[kl])
    ax[0].set_ylabel("GW in Betrieb")
    ax[1].set_ylabel("GWh in Betrieb")
    for x in ax:
        x.grid(alpha=0.3)
        x.legend(["Heim ≤ 30 kW", "Gewerbe 30 kW-1 MW", "Groß ≥ 1 MW"])
    fig.suptitle(
        "Batteriespeicher im MaStR (Stand 28.10.2026, Nachmeldungen fehlen am aktuellen Rand)"
    )
    fig.tight_layout()
    fig.savefig(gra / "bestand.png", dpi=130)
    plt.close(fig)

    # Teil 2
    df_all = lade_smard(a.daten / "smard")
    basis = (
        df_all.loc[BASIS[0] : BASIS[1]]
        .iloc[:-1]
        .dropna(subset=["price", "residual", "load"])
        .copy()
    )
    basis["biomass"] = basis.biomass.ffill()
    basis["hydro"] = basis.hydro.ffill()
    print("Basis", basis.index[0], basis.index[-1], len(basis))
    treppe = lade_treppe()
    preise = {v: Preis(basis, v, treppe) for v in ["empirisch", "merit-order"]}

    # Angebotskurven zur Ansicht (drei Monate)
    fig, ax = plt.subplots(figsize=(7, 4.5))
    for m, ls in [("2026-01", "-"), ("2026-04", "--"), ("2026-07", ":")]:
        gk = kurve_empirisch(df_all, pd.Period(m, "M"))
        sel = (GITTER > -20) & (GITTER < 70)
        ax.plot(GITTER[sel], gk[sel], ls, color=FARBE["empirisch"], label=f"empirisch {m}")
    for jz, ls in [("winter", "-"), ("jetzt", "--"), ("sommer", ":")]:
        cum, c = treppe[jz]
        ax.plot(
            cum - MO_KORREKTUR + 8,
            c,
            ls,
            color=FARBE["merit-order"],
            lw=1,
            label=f"Merit-Order {jz} (Biomasse + Wasser ≈ 8 GW angesetzt)",
        )
    ax.set_ylim(-150, 400)
    ax.set_xlabel("Residuallast GW")
    ax.set_ylabel("€/MWh")
    ax.grid(alpha=0.3)
    ax.legend(fontsize=8)
    fig.tight_layout()
    fig.savefig(gra / "angebotskurven.png", dpi=130)
    plt.close(fig)

    if a.schnell:
        wk = (basis.index.normalize() - basis.index.normalize()[0]).days // 7
        basis = basis[wk % 4 == 0]
        preise = {v: Preis(basis, v, treppe) for v in preise}

    erg, profile = [], {}
    laeufe = [("heute", P, DAUER) for P in ZUBAU_GW] + [("heute", 10, d) for d in DAUER_VARIANTEN]
    laeufe += [("ee_plus", P, DAUER) for P in ZUBAU_GW]
    ee_plus = -(
        EE_PLUS["solar"] * basis.solar + EE_PLUS["wind"] * (basis.wind_on + basis.wind_off)
    ).values
    for v, preis in preise.items():
        for sz, P, h in laeufe:
            preis.shift = ee_plus if sz == "ee_plus" else np.zeros(len(basis))
            x = simulieren(preis, basis, P, P * h)
            p_neu = preis.p + preis._g(np.arange(len(basis)), x[:, None])[:, 0]
            kz = kennzahlen(basis, p_neu, x)
            kz.update(szenario=sz, variante=v, zubau_gw=P, dauer_h=h)
            kz["erloes_eur_kw_a"] = kz.pop("erloes") * 1000 / (P * 1e6) if P else 0.0
            erg.append(kz)
            profile[(sz, v, P, h)] = (p_neu, x)
            print(
                sz,
                v,
                P,
                h,
                {k: round(val, 1) for k, val in kz.items() if isinstance(val, float)},
                flush=True,
            )
        preis.shift = np.zeros(len(basis))
    e = pd.DataFrame(erg)
    ref = e[e.zubau_gw == 0].set_index(["szenario", "variante"])
    key = pd.MultiIndex.from_frame(e[["szenario", "variante"]])
    for spalte in ["spanne", "neg_h"]:
        e[spalte.split("_")[0] + "_rel"] = e[spalte].values / ref[spalte].reindex(key).values - 1
    for spalte in ["abend", "mittag", "mittel", "lastgewichtet", "p95", "p05"]:
        e[spalte + "_diff"] = e[spalte].values - ref[spalte].reindex(key).values
    e.round(3).to_csv(tab / "simulation.csv", index=False)

    # Schwellen: Zubau (GW, 2 h), ab dem die Schwelle erreicht ist
    sch = []
    for (sz, v), r in e[e.dauer_h == DAUER].groupby(["szenario", "variante"]):
        r = r.sort_values("zubau_gw")
        for key, (wert, text) in SCHWELLEN.items():
            sg = np.sign(wert)
            sch.append(
                dict(
                    szenario=sz,
                    variante=v,
                    schwelle=text,
                    zubau_gw=round(
                        schwelle_gw(r.zubau_gw.values, sg * r[key].values, sg * wert), 1
                    ),
                )
            )
    sch = pd.DataFrame(sch)
    sch.to_csv(tab / "schwellen.csv", index=False)
    print(sch)

    # Teil 3: Zeitachse
    za = zeitachse(best, plan, zub, basis, e)
    za.round(3).to_csv(tab / "zeitachse.csv", index=False)
    print(za)

    # Grafiken Simulation
    fig, ax = plt.subplots(2, 2, figsize=(12, 8))
    r2 = e[(e.dauer_h == DAUER) & (e.szenario == "heute")]
    for v in preise:
        r = r2[r2.variante == v]
        ax[0, 0].plot(
            r.zubau_gw, r.abend_diff, "o-", color=FARBE[v], label=f"Abend 17-21 Uhr ({v})"
        )
        ax[0, 0].plot(
            r.zubau_gw, r.mittag_diff, "s--", color=FARBE[v], label=f"Mittag 11-15 Uhr ({v})"
        )
        ax[0, 1].plot(r.zubau_gw, 100 * r.spanne_rel, "o-", color=FARBE[v], label=v)
        ax[1, 0].plot(r.zubau_gw, r.neg_h, "o-", color=FARBE[v], label=v)
        ax[1, 1].plot(
            r.zubau_gw[r.zubau_gw > 0],
            r.erloes_eur_kw_a[r.zubau_gw > 0],
            "o-",
            color=FARBE[v],
            label=v,
        )
    r3 = e[(e.szenario == "ee_plus") & (e.variante == "empirisch")]
    ax[0, 1].plot(r3.zubau_gw, 100 * r3.spanne_rel, "o:", color="k", label="empirisch, EE+")
    ax[1, 0].plot(r3.zubau_gw, r3.neg_h, "o:", color="k", label="empirisch, EE+")
    ax[1, 1].plot(
        r3.zubau_gw[r3.zubau_gw > 0],
        r3.erloes_eur_kw_a[r3.zubau_gw > 0],
        "o:",
        color="k",
        label="empirisch, EE+",
    )
    ax[0, 0].axhline(0, color="k", lw=0.5)
    ax[0, 0].set_ylabel("Änderung €/MWh")
    ax[0, 1].set_ylabel("Änderung Tagesspanne %")
    ax[1, 0].set_ylabel("Stunden mit negativem Preis")
    ax[1, 1].set_ylabel("Arbitrageerlös €/kW/a")
    for x in ax.ravel():
        x.set_xlabel("zusätzliche Speicherleistung GW (2 h)")
        x.grid(alpha=0.3)
        x.legend(fontsize=8)
    fig.suptitle(f"Simulierte Wirkung auf das Basisjahr {BASIS[0]} bis {BASIS[1]}")
    fig.tight_layout()
    fig.savefig(gra / "simulation.png", dpi=130)
    plt.close(fig)

    fig, ax = plt.subplots(1, 2, figsize=(12, 4.5))
    for pf, grp in za[(za.variante == "empirisch") & (za.szenario == "heute")].groupby("pfad"):
        ax[0].plot(grp.jahr, grp.markt_gw, "o-", label=pf)
        ax[1].plot(grp.jahr, 100 * grp.spanne_rel, "o-", label=pf)
    ax[0].set_ylabel("marktgeführte Batterieleistung GW (Jahresende)")
    ax[1].axhline(100 * SCHWELLEN["spanne_rel"][0], color="k", lw=0.5, ls="--")
    ax[1].set_ylabel("Tagesspanne ggü. Basisjahr % (empirisch, EE wie heute)")
    for x in ax:
        x.grid(alpha=0.3)
        x.legend(fontsize=8)
        x.set_xticks(list(JAHRE))
    fig.tight_layout()
    fig.savefig(gra / "zeitachse.png", dpi=130)
    plt.close(fig)

    # Tagesprofil: mittlerer Preis je Stunde, Sommer/Winter
    fig, ax = plt.subplots(1, 2, figsize=(12, 4.2), sharey=True)
    for i, (name, mon) in enumerate([("Mai-Aug", [5, 6, 7, 8]), ("Nov-Feb", [11, 12, 1, 2])]):
        sel = basis.index.month.isin(mon)
        for P, ls in [(0, "-"), (10, "--"), (20, "-."), (40, ":")]:
            p_neu, _ = profile[("heute", "empirisch", P, DAUER)]
            ax[i].plot(
                pd.Series(p_neu[sel]).groupby(basis.index.hour[sel]).mean(),
                ls,
                color=FARBE["empirisch"],
                label=f"+{P} GW" if P else "Basis",
            )
        ax[i].set_title(name)
        ax[i].set_xlabel("Stunde (MEZ/MESZ)")
        ax[i].grid(alpha=0.3)
    ax[0].set_ylabel("mittlerer Day-Ahead-Preis €/MWh (empirisch)")
    ax[0].legend()
    fig.tight_layout()
    fig.savefig(gra / "tagesprofil.png", dpi=130)
    plt.close(fig)

    # Preisdauerlinie
    fig, ax = plt.subplots(figsize=(8, 4.5))
    for P, ls in [(0, "-"), (10, "--"), (40, ":")]:
        for v in preise:
            p_neu, _ = profile[("heute", v, P, DAUER)]
            ax.plot(np.sort(p_neu)[::-1], ls, color=FARBE[v], lw=1, label=f"{v} +{P} GW")
    ax.set_ylim(-150, 500)
    ax.set_xlabel("Stunden im Jahr, absteigend")
    ax.set_ylabel("€/MWh")
    ax.grid(alpha=0.3)
    ax.legend(fontsize=8)
    fig.tight_layout()
    fig.savefig(gra / "preisdauerlinie.png", dpi=130)
    plt.close(fig)

    # Teil 4
    rb = rueckblick(df_all)
    rb.round(2).to_csv(tab / "rueckblick_tagesspanne.csv", index=False)
    ub = ueberschuss(df_all)
    ub.round(3).to_csv(tab / "rueckblick_ueberschuss.csv", index=False)
    print(ub)
    fig, ax = plt.subplots(figsize=(8, 4.5))
    for kl, grp in rb.groupby("klasse", observed=True):
        ax.plot(grp.jahr, grp.relativ_median, "o-", label=f"Residuallast-Spanne {kl} GW")
    ax.set_ylabel("Median Tagesspanne / Tagesmittel")
    ax.grid(alpha=0.3)
    ax.legend(fontsize=8)
    fig.tight_layout()
    fig.savefig(gra / "rueckblick.png", dpi=130)
    plt.close(fig)


if __name__ == "__main__":
    main()
