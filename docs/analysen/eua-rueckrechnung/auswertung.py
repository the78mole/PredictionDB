"""EUA-Rückrechnung aus dem Strommarkt: Merkmale, Modelle, Out-of-Sample-Test, Tagesreihe.

Idee: In Stunden, in denen Braunkohle den Preis setzt, gilt näherungsweise

    Preis = (Brennstoff_Braunkohle + EF_Braunkohle * EUA) / eta_Braunkohle.

Braunkohle hat keinen gehandelten Brennstoffpreis (Tagebau am Kraftwerk). Ihre Grenzkosten
bestehen überwiegend aus CO2. Deshalb lässt sich der EUA-Preis ohne Gaspreis aus dem
Strompreis dieser Stunden schätzen. Gas- und Steinkohlestunden dienen zur gemeinsamen
Schätzung von Gas- und CO2-Preis und zur Gegenprobe.

Ablauf:

1. Stundenklassen je Liefertag (SMARD): Auslastung jeder fossilen Technologie relativ zum
   5-%- und 95-%-Quantil der letzten 30 Tage. "Braunkohle preissetzend": Braunkohle in
   Teillast (20-80 %), Steinkohle und Gas nahe Minimum (< 20 %), Preis > 0. Analog
   Steinkohle und Gas.
2. Für jeden Handelstag T: Median des Preises dieser Stunden über die Liefertage T-5 bis T+1
   (der Day-Ahead-Preis für T+1 steht am Mittag von T fest). Mindestens 6 Stunden, sonst
   14 und 28 Tage.
3. Modelle (siehe README): Fortschreibung (Pflicht-Baseline), Strukturformel, kalibrierte
   Strukturformel, verankerte Änderung, gemeinsame Gas/CO2-Schätzung, Zusatzvariante ACER.
4. Out-of-Sample-Test 01/2024-06/2025: Kalibrierung jeweils am Monatsersten nur mit
   Auktionen aus Berichten, die da schon veröffentlicht waren (PDF-Datum).
5. Tägliche Ausgabereihe bis heute mit Intervall, Datenstand und Qualitätskennzeichen.

Aufruf aus dem Repository-Wurzelverzeichnis (nach ``auktionen.py``)::

    uv run --no-project --with pandas --with matplotlib --with httpx --with openpyxl \\
        --with pyarrow python docs/analysen/eua-rueckrechnung/auswertung.py --cache /tmp/eua
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

HIER = Path(__file__).resolve().parent
sys.path.insert(0, str(HIER))
import daten  # noqa: E402

# --- Annahmen (Quellen im README) ----------------------------------------------------
# IPCC 2006 Guidelines, Vol. 2, Kap. 1, Tabelle 1.4 (Standardwerte, Heizwert-Basis):
# Erdgas 56 100 kg/TJ (54 300-58 300), sonstige bituminöse Kohle 94 600 (89 500-99 700),
# Braunkohle 101 000 (90 900-115 000). 1 TJ = 277,78 MWh.
TJ_MWH = 277.7778
EF = {"braunkohle": 101.0 / TJ_MWH, "steinkohle": 94.6 / TJ_MWH, "gas": 56.1 / TJ_MWH}
EF_SPANNE = {
    "braunkohle": (90.9 / TJ_MWH, 115.0 / TJ_MWH),
    "steinkohle": (89.5 / TJ_MWH, 99.7 / TJ_MWH),
    "gas": (54.3 / TJ_MWH, 58.3 / TJ_MWH),
}
# Nettowirkungsgrade wie in docs/analysen/merit-order (Formel je Baujahr): Braunkohle
# 0,30 + 0,003 * (Jahr - 1960), Bestwert 0,435 -> Bestand der Baujahre 1975-2012: 0,345-0,435.
ETA = {"braunkohle": 0.39, "steinkohle": 0.42, "gas": 0.55}
ETA_SPANNE = {"braunkohle": (0.345, 0.435), "steinkohle": (0.36, 0.46), "gas": (0.45, 0.60)}
# Variable Brennstoffkosten Braunkohle frei Kraftwerk (Annahme aus merit-order): 4 EUR/MWh_th
BRAUNKOHLE_EUR = 4.0
BRAUNKOHLE_SPANNE = (0.0, 10.0)
# Erdgas: Heizwert = 0,9 x Brennwert (IPCC 2006, Kap. 1.4.1.2: "NCV is about 10 percent less")
GAS_HS_HI = 1 / 0.9
MMBTU_MWH = 0.293071  # 1 MMBtu = 0,293071 MWh (Definition)

TEST = (pd.Timestamp("2024-01-01"), pd.Timestamp("2025-06-30"))
KRISE = (pd.Timestamp("2021-07-01"), pd.Timestamp("2023-06-30"))
KALIB_MONATE = 24
MIN_STUNDEN = 6
FENSTER = (14, 28, 56)
QUANTILE = (0.05, 0.95)
LAG_KLASSEN = [0, 30, 90, 180, 365, 10_000]
ANKER_TAGE = 10
GAS_KRISE_EUR = 50.0  # ab diesem Weltbank-Gaspreis (EUR/MWh) versagte die Strukturformel 2021/22


# --- Merkmale ---------------------------------------------------------------------------


def stundenklassen(h: pd.DataFrame) -> pd.DataFrame:
    h = h.copy()
    for col in ("braunkohle", "steinkohle", "erdgas"):
        hi = h[col].rolling("30D").quantile(0.95)
        lo = h[col].rolling("30D").quantile(0.05)
        h[f"u_{col}"] = ((h[col] - lo) / (hi - lo)).clip(-0.5, 1.5)
    pos = h.preis > 0
    h["k_braunkohle"] = (
        h.u_braunkohle.between(0.2, 0.8) & (h.u_steinkohle < 0.2) & (h.u_erdgas < 0.2) & pos
    )
    h["k_steinkohle"] = (
        h.u_steinkohle.between(0.2, 0.8) & (h.u_erdgas < 0.3) & (h.u_braunkohle > 0.8) & pos
    )
    h["k_gas"] = h.u_erdgas.between(0.3, 0.9) & (h.u_braunkohle > 0.85) & (h.u_steinkohle > 0.85)
    return h


def tagesmerkmale(h: pd.DataFrame) -> pd.DataFrame:
    """Merkmale je Handelstag T aus den Liefertagen bis T+1."""
    h = stundenklassen(h)
    h["liefertag"] = h.index.tz_localize(None).normalize()
    tage = pd.DatetimeIndex(sorted(h.liefertag.unique()))
    out = pd.DataFrame(index=tage[:-1])  # Handelstag T = Liefertag - 1
    for k in ("braunkohle", "steinkohle", "gas"):
        maske = h[f"k_{k}"]
        werte = np.full(len(out), np.nan)
        anzahl = np.zeros(len(out))
        fenster = np.zeros(len(out))
        p = h.preis.where(maske)
        for f in FENSTER[::-1]:
            med = p.rolling(f"{f}D", min_periods=MIN_STUNDEN).median()
            n = maske.astype(float).rolling(f"{f}D").sum()
            # letzter Stundenwert des Liefertags T+1
            med_tag = med.groupby(h.liefertag).last().reindex(out.index + pd.Timedelta(days=1))
            n_tag = n.groupby(h.liefertag).last().reindex(out.index + pd.Timedelta(days=1))
            ok = med_tag.notna().to_numpy()
            werte[ok] = med_tag.to_numpy()[ok]
            anzahl[ok] = n_tag.to_numpy()[ok]
            fenster[ok] = f
        out[f"p_{k}"] = werte
        out[f"n_{k}"] = anzahl
        out[f"f_{k}"] = fenster
    tag = h.groupby("liefertag")
    out["preis_mittel"] = tag.preis.mean().rolling(7).mean().shift(-1).reindex(out.index)
    out["anteil_negativ"] = (
        tag.preis.apply(lambda x: (x < 0).mean()).rolling(7).mean().shift(-1).reindex(out.index)
    )
    return out


def brennstoffe(cache: Path, index: pd.DatetimeIndex) -> pd.DataFrame:
    """Weltbank-Monatswerte, wie sie am Tag T bekannt waren (Vormonat ab dem 5. Tag)."""
    ps = daten.pink_sheet(cache)
    fx = daten.usd_je_eur(cache)
    mo = pd.DataFrame(
        {
            "gas_monat": ps.gas_eu_usd_mmbtu / fx / MMBTU_MWH,  # EUR/MWh Brennwert
            "kohle_monat": ps.kohle_za_usd_t / fx,  # EUR/t
        }
    ).dropna()
    bekannt = []
    for t in index:
        m = t.to_period("M") - (1 if t.day >= 5 else 2)
        bekannt.append(mo.loc[m] if m in mo.index else mo[mo.index <= m].iloc[-1])
    df = pd.DataFrame(bekannt, index=index)
    acer = daten.acer_lng(cache)
    df["lng_acer"] = acer.reindex(index).ffill(limit=4)
    return df


# --- Modelle ----------------------------------------------------------------------------


def struktur(p_lig: pd.Series, eta: float, ef: float, brennstoff: float) -> pd.Series:
    return (eta * p_lig - brennstoff) / ef


def ols(x: np.ndarray, y: np.ndarray) -> np.ndarray:
    a = np.column_stack([np.ones(len(x)), x])
    coef: np.ndarray = np.linalg.lstsq(a, y, rcond=None)[0]
    return coef


def bekannte_auktionen(eua: pd.DataFrame, berichte: pd.DataFrame, t: pd.Timestamp) -> pd.Series:
    """Auktionspreise, die am Tag t aus veröffentlichten Berichten bekannt waren."""
    bek = berichte[berichte.pdf_datum < t].bericht
    return eua[eua.bericht.isin(bek) & (eua.index < t)].preis


def lag_klasse(lag: float) -> int:
    for i in range(len(LAG_KLASSEN) - 1):
        if LAG_KLASSEN[i] <= lag < LAG_KLASSEN[i + 1]:
            return LAG_KLASSEN[i + 1]
    return LAG_KLASSEN[-1]


def kalibrieren(m: pd.DataFrame, bekannt: pd.Series, t: pd.Timestamp) -> dict[str, Any]:
    """Koeffizienten und Fehlerquantile aus den bis t bekannten Auktionen."""
    start = t - pd.DateOffset(months=KALIB_MONATE)
    d = m.join(bekannt.rename("eua"), how="inner")
    d = d[(d.index >= start)]
    k: dict[str, Any] = {
        "n": len(d),
        "letzte": bekannt.index.max(),
        "anker": float(bekannt.iloc[-1]),
    }
    # Anker der verankerten Modelle: Mittel der letzten bekannten Auktionstage mit Merkmal
    letzte = d.dropna(subset=["p_braunkohle"]).tail(ANKER_TAGE)
    k["anker_eua"] = letzte.eua.mean()
    k["anker_p"] = letzte.p_braunkohle.mean()
    k["offset"] = (
        letzte.eua
        - struktur(letzte.p_braunkohle, ETA["braunkohle"], EF["braunkohle"], BRAUNKOHLE_EUR)
    ).mean()
    gut = d.dropna(subset=["p_braunkohle"])
    k["b1"] = ols(gut[["p_braunkohle"]].to_numpy(), gut.eua.to_numpy())
    res = gut.eua - (k["b1"][0] + k["b1"][1] * gut.p_braunkohle)
    k["q_kalib"] = res.quantile(list(QUANTILE)).to_numpy()
    # gemeinsame Schätzung: EUA aus Braunkohle- und Gasstunden mit Weltbank-Gas (Vormonat)
    g = d.dropna(subset=["p_braunkohle", "p_gas", "gas_monat"])
    k["b_gem"] = ols(g[["p_braunkohle", "p_gas", "gas_monat"]].to_numpy(), g.eua.to_numpy())
    # Zusatzvariante ACER: EUA aus Gasstunden und täglichem LNG-Preis
    a = d.dropna(subset=["p_gas", "lng_acer", "p_braunkohle"])
    k["b_acer"] = (
        ols(a[["p_braunkohle", "p_gas", "lng_acer"]].to_numpy(), a.eua.to_numpy())
        if len(a) >= 60
        else None
    )
    # Fehlerquantile der Fortschreibung und der verankerten Änderung je Verzugsklasse
    s = bekannt[bekannt.index >= start]
    pl = m.p_braunkohle.reindex(s.index)
    fehler: dict[str, dict[int, list[float]]] = {"fort": {}, "anker": {}, "korr": {}}
    b_phys = ETA["braunkohle"] / EF["braunkohle"]
    for lag in (1, 7, 30, 60, 90, 120, 180, 270, 365):
        t0 = s.index - pd.Timedelta(days=lag)
        pos = s.index.searchsorted(t0, side="right") - 1
        ok = pos >= 0
        e0 = s.to_numpy()[pos[ok]]
        e1 = s.to_numpy()[ok]
        dl = pl.to_numpy()[ok] - pl.to_numpy()[pos[ok]]
        kl = lag_klasse(lag)
        fehler["fort"].setdefault(kl, []).extend(e1 - e0)
        ea = e1 - (e0 + k["b1"][1] * dl)
        fehler["anker"].setdefault(kl, []).extend(ea[~np.isnan(ea)])
        ek = e1 - (e0 + b_phys * dl)
        fehler["korr"].setdefault(kl, []).extend(ek[~np.isnan(ek)])
    k["q_lag"] = {
        name: {kl: np.quantile(v, QUANTILE) for kl, v in f.items() if len(v) > 20}
        for name, f in fehler.items()
    }
    return k


def schaetzen(m: pd.DataFrame, k: dict[str, Any], bekannt: pd.Series) -> pd.DataFrame:
    """Alle Modellschätzungen für die Tage in m mit den Koeffizienten k."""
    out = pd.DataFrame(index=m.index)
    t0 = k["letzte"]
    lag = (m.index - t0).days.to_numpy().astype(float)
    out["lag_tage"] = lag
    out["fortschreibung"] = k["anker"]
    out["struktur"] = struktur(m.p_braunkohle, ETA["braunkohle"], EF["braunkohle"], BRAUNKOHLE_EUR)
    out["kalibriert"] = k["b1"][0] + k["b1"][1] * m.p_braunkohle
    out["verankert"] = k["anker_eua"] + k["b1"][1] * (m.p_braunkohle - k["anker_p"])
    out["struktur_korr"] = out.struktur + k["offset"]
    # Regel: Strukturformel nur bei kurzem Merkmalsfenster und Gaspreis unter der Krisenschwelle
    ok = (m.f_braunkohle <= FENSTER[0]) & (m.gas_monat < GAS_KRISE_EUR) & out.struktur.notna()
    out["regel"] = np.where(ok, out.struktur, out.fortschreibung)
    b = k["b_gem"]
    out["gemeinsam"] = b[0] + b[1] * m.p_braunkohle + b[2] * m.p_gas + b[3] * m.gas_monat
    if k["b_acer"] is not None:
        b = k["b_acer"]
        out["acer"] = b[0] + b[1] * m.p_braunkohle + b[2] * m.p_gas + b[3] * m.lng_acer
    # implizierter Gaspreis (Brennwert) aus Gasstunden bei geschätztem EUA
    eta_g, ef_g = ETA["gas"], EF["gas"]
    out["gas_implizit"] = (eta_g * m.p_gas - ef_g * out.kalibriert) / GAS_HS_HI
    # Intervalle
    kl = [lag_klasse(x) for x in lag]
    for name, basis in (
        ("fort", "fortschreibung"),
        ("anker", "verankert"),
        ("korr", "struktur_korr"),
    ):
        q = k["q_lag"][name] or {0: (np.nan, np.nan)}
        lo = np.array([q.get(c, q[max(q)])[0] for c in kl])
        hi = np.array([q.get(c, q[max(q)])[1] for c in kl])
        out[f"{basis}_lo"] = out[basis] + lo
        out[f"{basis}_hi"] = out[basis] + hi
    out["kalibriert_lo"] = out.kalibriert + k["q_kalib"][0]
    out["kalibriert_hi"] = out.kalibriert + k["q_kalib"][1]
    return out


MODELLE = [
    "fortschreibung",
    "struktur",
    "struktur_korr",
    "regel",
    "kalibriert",
    "verankert",
    "gemeinsam",
    "acer",
]


def backtest(
    m: pd.DataFrame,
    eua: pd.DataFrame,
    berichte: pd.DataFrame,
    start: pd.Timestamp,
    ende: pd.Timestamp,
) -> pd.DataFrame:
    teile = []
    for monat in pd.date_range(start, ende, freq="MS"):
        bek = bekannte_auktionen(eua, berichte, monat)
        k = kalibrieren(m, bek, monat)
        tage = m[(m.index >= monat) & (m.index < monat + pd.DateOffset(months=1))]
        s = schaetzen(tage, k, bek)
        s["kalibriert_am"] = monat
        s["datenstand"] = k["letzte"]
        teile.append(s)
    return pd.concat(teile)


# --- Auswertung -------------------------------------------------------------------------


def regime(m: pd.DataFrame) -> pd.Series:
    r = pd.Series("normal", index=m.index)
    r[(m.index >= KRISE[0]) & (m.index <= KRISE[1])] = "Gaskrise 2021/22"
    r[m.anteil_negativ > 0.08] = "viele negative Preise"
    r[(m.f_braunkohle > FENSTER[0]) | (m.n_braunkohle < MIN_STUNDEN)] = (
        "kaum Braunkohle preissetzend"
    )
    return r


def kennzahlen(bt: pd.DataFrame, ist: pd.Series, gruppen: pd.Series) -> pd.DataFrame:
    d = bt.join(ist.rename("ist"), how="inner")
    d["gruppe"] = gruppen.reindex(d.index)
    zeilen = []
    for g, x in [("alle", d), *d.groupby("gruppe")]:
        for mod in MODELLE:
            if mod not in x or x[mod].isna().all():
                continue
            e = (x[mod] - x.ist).dropna()
            z = {
                "gruppe": g,
                "modell": mod,
                "tage": len(e),
                "mae": e.abs().mean(),
                "rmse": np.sqrt((e**2).mean()),
                "bias": e.mean(),
                "mape_%": (e.abs() / x.ist.reindex(e.index)).mean() * 100,
            }
            if f"{mod}_lo" in x:
                inn = (x.ist >= x[f"{mod}_lo"]) & (x.ist <= x[f"{mod}_hi"])
                z["abdeckung_90_%"] = inn[x[mod].notna()].mean() * 100
                z["breite"] = (x[f"{mod}_hi"] - x[f"{mod}_lo"])[x[mod].notna()].mean()
            zeilen.append(z)
    return pd.DataFrame(zeilen).round(2)


def feste_verzuege(
    m: pd.DataFrame, eua: pd.DataFrame, k_for: Callable[[pd.Timestamp], dict[str, Any]]
) -> pd.DataFrame:
    """Fortschreibung und verankerte Änderung bei festem Verzug L (ohne Berichtsverzug)."""
    s = eua.preis
    test = s[(s.index >= TEST[0]) & (s.index <= TEST[1])]
    zeilen = []
    for lag in (1, 7, 30, 90, 180, 365):
        t0 = test.index - pd.Timedelta(days=lag)
        pos = s.index.searchsorted(t0, side="right") - 1
        e0 = s.to_numpy()[pos]
        a0 = s.index[pos]
        b = np.array([k_for(t)["b1"][1] for t in test.index])
        pl = m.p_braunkohle
        dl = pl.reindex(test.index).to_numpy() - pl.reindex(a0).to_numpy()
        fort = e0 - test.to_numpy()
        anker = e0 + b * dl - test.to_numpy()
        zeilen.append(
            {
                "verzug_tage": lag,
                "mae_fortschreibung": np.nanmean(np.abs(fort)),
                "mae_verankert": np.nanmean(np.abs(anker)),
                "tage": int(np.isfinite(anker).sum()),
            }
        )
    return pd.DataFrame(zeilen).round(2)


def empfindlichkeit(m: pd.DataFrame, ist: pd.Series) -> pd.DataFrame:
    """Strukturformel ohne Kalibrierung: Fehler bei Variation einer Annahme."""
    d = m.join(ist.rename("ist"), how="inner").dropna(subset=["p_braunkohle"])
    d = d[(d.index >= TEST[0]) & (d.index <= TEST[1])]
    basis = {"eta": ETA["braunkohle"], "ef": EF["braunkohle"], "f": BRAUNKOHLE_EUR}
    varianten = [("Basis", basis)]
    for name, key, spanne in (
        ("Wirkungsgrad", "eta", ETA_SPANNE["braunkohle"]),
        ("Emissionsfaktor", "ef", EF_SPANNE["braunkohle"]),
        ("Brennstoffkosten", "f", BRAUNKOHLE_SPANNE),
    ):
        for w in spanne:
            varianten.append((f"{name} {w:.3f}", {**basis, key: w}))
    zeilen = []
    for name, p in varianten:
        e = struktur(d.p_braunkohle, p["eta"], p["ef"], p["f"]) - d.ist
        zeilen.append({"variante": name, "mae": e.abs().mean(), "bias": e.mean()})
    return pd.DataFrame(zeilen).round(2)


def plausibilitaet(m: pd.DataFrame, ist: pd.Series) -> pd.DataFrame:
    """Gegenprobe: implizierter Gaspreis gegen Weltbank (Monat) und ACER (Tag)."""
    d = m
    eua_struktur = struktur(m.p_braunkohle, ETA["braunkohle"], EF["braunkohle"], BRAUNKOHLE_EUR)
    gas = (ETA["gas"] * m.p_gas - EF["gas"] * eua_struktur) / GAS_HS_HI
    mo = (
        pd.DataFrame({"gas_implizit": gas, "eua_struktur": eua_struktur, "eua_ist": ist})
        .resample("ME")
        .mean()
    )
    ps = daten.pink_sheet(CACHE)
    fx = daten.usd_je_eur(CACHE)
    welt = (ps.gas_eu_usd_mmbtu / fx / MMBTU_MWH).rename("gas_weltbank")
    welt.index = welt.index.to_timestamp("M")
    mo = mo.join(welt)
    mo["lng_acer"] = d.lng_acer.resample("ME").mean()
    return mo.round(2)


# --- Weitere Varianten -------------------------------------------------------------------

AENDERUNG = {
    "Braunkohle-Stunden": ["p_braunkohle"],
    "Braunkohle + Weltbank-Gas": ["p_braunkohle", "gas_monat"],
    "Braunkohle + Weltbank-Gas + Kohle": ["p_braunkohle", "gas_monat", "kohle_eur_mwh"],
    "Braunkohle + Gas-Stunden + Weltbank-Gas": ["p_braunkohle", "p_gas", "gas_monat"],
    "Tagesmittel + Weltbank-Gas": ["preis_mittel", "gas_monat"],
    "ACER: Gas-Stunden + LNG": ["p_gas", "lng_acer"],
    "ACER: Braunkohle + Gas-Stunden + LNG": ["p_braunkohle", "p_gas", "lng_acer"],
}


def aenderungsmodell(
    m: pd.DataFrame,
    eua: pd.DataFrame,
    berichte: pd.DataFrame,
    merkmale: list[str],
    start: str,
    ende: str,
) -> pd.DataFrame:
    """Log-Änderung des EUA seit dem Anker aus Log-Änderungen der Merkmale (OLS ohne Konstante).

    Trainiert auf Paaren bekannter Auktionstage mit 30-365 Tagen Abstand im
    Kalibrierfenster; Anker ist das Mittel der letzten zehn bekannten Auktionstage.
    """
    x_alle = np.log(m[merkmale].ffill(limit=10).clip(lower=1))
    teile = []
    for monat in pd.date_range(start, ende, freq="MS"):
        bek = bekannte_auktionen(eua, berichte, monat)
        s = np.log(bek[bek.index >= monat - pd.DateOffset(months=KALIB_MONATE)])
        xs = x_alle.reindex(s.index)
        dx, dy = [], []
        for lag in (30, 60, 90, 180, 270, 365):
            pos = s.index.searchsorted(s.index - pd.Timedelta(days=lag), side="right") - 1
            ok = pos >= 0
            dx.append(xs.to_numpy()[ok] - xs.to_numpy()[pos[ok]])
            dy.append(s.to_numpy()[ok] - s.to_numpy()[pos[ok]])
        dxa, dya = np.vstack(dx), np.concatenate(dy)
        g = np.isfinite(dxa).all(1) & np.isfinite(dya)
        if g.sum() < 50:
            continue
        b, *_ = np.linalg.lstsq(dxa[g], dya[g], rcond=None)
        letzte = xs.dropna().tail(ANKER_TAGE)
        tage = m.index[(m.index >= monat) & (m.index < monat + pd.DateOffset(months=1))]
        pred = (
            s.reindex(letzte.index).mean()
            + (x_alle.reindex(tage).to_numpy() - letzte.mean().to_numpy()) @ b
        )
        teile.append(
            pd.DataFrame(
                {"modell": np.exp(pred), "fortschreibung": float(bek.iloc[-1])}, index=tage
            )
        )
    return pd.concat(teile).join(eua.preis.rename("ist"), how="inner") if teile else pd.DataFrame()


def varianten(m: pd.DataFrame, eua: pd.DataFrame, berichte: pd.DataFrame) -> pd.DataFrame:
    sofort = berichte.assign(pdf_datum=berichte.letzte_auktion + pd.Timedelta(days=1))
    zeilen = []
    for name, merkmale in AENDERUNG.items():
        acer = "lng_acer" in merkmale
        for zeitraum, (st, en) in {
            "2018-2023": ("2018-01-01", "2023-12-31"),
            "Test 2024-06/2025": ("2024-01-01", "2025-06-30"),
        }.items():
            if acer and zeitraum == "2018-2023":
                continue
            for verzug, ber in (("realer Berichtsverzug", berichte), ("Quartal sofort", sofort)):
                r = aenderungsmodell(m, eua, ber, merkmale, st, en)
                if r.empty:
                    continue
                zeilen.append(
                    {
                        "variante": name,
                        "zeitraum": zeitraum,
                        "verzug": verzug,
                        "tage": len(r),
                        "mae_modell": (r.modell - r.ist).abs().mean(),
                        "mae_fortschreibung": (r.fortschreibung - r.ist).abs().mean(),
                    }
                )
    out = pd.DataFrame(zeilen)
    out["verbesserung_%"] = (1 - out.mae_modell / out.mae_fortschreibung) * 100
    return out.round(2)


def parameter(
    m_fn: Callable[..., pd.DataFrame], eua: pd.DataFrame, berichte: pd.DataFrame
) -> pd.DataFrame:
    """Wahl von Merkmalsfenster und Kalibrierlänge, nur auf 2018-2023 bewertet."""
    global FENSTER, KALIB_MONATE
    alt = FENSTER, KALIB_MONATE
    zeilen = []
    for w in (7, 14, 28):
        FENSTER = (w, 2 * w, 4 * w)
        m = m_fn()
        for km in (24, 36, 60):
            KALIB_MONATE = km
            for verzug, ber in (
                ("realer Berichtsverzug", berichte),
                (
                    "Quartal sofort",
                    berichte.assign(pdf_datum=berichte.letzte_auktion + pd.Timedelta(days=1)),
                ),
            ):
                bt = backtest(m, eua, ber, pd.Timestamp("2018-01-01"), pd.Timestamp("2023-12-31"))
                d = bt.join(eua.preis.rename("ist"), how="inner")
                z = {"fenster_tage": w, "kalibrierung_monate": km, "verzug": verzug}
                for mod in ("fortschreibung", "kalibriert", "verankert", "gemeinsam"):
                    z[f"mae_{mod}"] = (d[mod] - d.ist).abs().mean()
                zeilen.append(z)
    FENSTER, KALIB_MONATE = alt
    return pd.DataFrame(zeilen).round(2)


def tagesreihe_plausibel(
    m: pd.DataFrame, eua: pd.DataFrame, berichte: pd.DataFrame
) -> pd.DataFrame:
    """Tägliche Plausibilitätsreihe bis heute (kein Schätzer, siehe README).

    Hauptwert ist die unkalibrierte Strukturformel aus den Braunkohle-Stunden. Das Band
    sind die 5-/95-%-Quantile ihrer Abweichung vom Auktionspreis in den letzten 36 Monaten
    bekannter Auktionen. Qualität: A = Merkmal aus 14 Tagen, B = 28 Tage, C = 56 Tage oder
    Gaspreis (Weltbank, Vormonat) über GAS_KRISE_EUR, "-" = keine Braunkohle-Stunden.
    """
    heute = m.index.max()
    bek = bekannte_auktionen(eua, berichte, heute + pd.Timedelta(days=1))
    roh = struktur(m.p_braunkohle, ETA["braunkohle"], EF["braunkohle"], BRAUNKOHLE_EUR)
    hist = (bek - roh.reindex(bek.index))[bek.index >= bek.index.max() - pd.DateOffset(months=36)]
    lo, hi = hist.quantile(list(QUANTILE))
    k = kalibrieren(m, bek, heute + pd.Timedelta(days=1))
    tage = m[m.index > k["letzte"] - pd.Timedelta(days=90)]
    s = schaetzen(tage, k, bek)
    out = pd.DataFrame(
        {
            "eua_struktur": s.struktur,
            "eua_struktur_lo": s.struktur + lo,
            "eua_struktur_hi": s.struktur + hi,
            "eua_verankert": s.verankert,
            "letzter_bekannter_auktionspreis": s.fortschreibung,
            "datenstand_auktionen": k["letzte"].date(),
            "verzug_tage": s.lag_tage,
            "braunkohle_stunden": tage.n_braunkohle,
            "fenster_tage": tage.f_braunkohle,
            "gas_weltbank_vormonat": tage.gas_monat,
        }
    )
    q = np.where(
        tage.f_braunkohle <= FENSTER[0], "A", np.where(tage.f_braunkohle <= FENSTER[1], "B", "C")
    )
    q = np.where(tage.gas_monat >= GAS_KRISE_EUR, "C", q)
    out["qualitaet"] = np.where(tage.p_braunkohle.isna(), "-", q)
    out["eua_ist"] = eua.preis.reindex(out.index)
    return out.round(2)


# --- Grafiken ---------------------------------------------------------------------------

FARBE = {"ist": "#0b0b0b", "s1": "#2a78d6", "s2": "#eb6834", "s3": "#1baf7a", "s4": "#eda100"}


def _achse(ax: Any, titel: str, einheit: str) -> None:
    ax.set_title(titel, loc="left", fontsize=11)
    ax.set_ylabel(einheit)
    ax.grid(axis="y", color="#e4e3df", lw=0.8)
    for sp in ("top", "right"):
        ax.spines[sp].set_visible(False)


def grafiken(
    m: pd.DataFrame,
    eua: pd.DataFrame,
    berichte: pd.DataFrame,
    bt: pd.DataFrame,
    pl: pd.DataFrame,
    fv: pd.DataFrame,
    tag: pd.DataFrame,
) -> None:
    g = HIER / "grafiken"
    # 1 Auktionsreihe und Veröffentlichungszeitpunkte
    fig, ax = plt.subplots(figsize=(10, 4.2))
    ax.plot(eua.index, eua.preis, color=FARBE["ist"], lw=1, label="Auktionspreis (Tageswert)")
    for _, r in berichte.dropna(subset=["pdf_datum"]).iterrows():
        ax.axvline(r.pdf_datum, color=FARBE["s1"], lw=0.6, alpha=0.5)
    ax.plot([], [], color=FARBE["s1"], lw=0.6, label="Bericht veröffentlicht (PDF-Datum)")
    _achse(ax, "EUA-Auktionspreise der gemeinsamen Plattform 2015-06/2025", "EUR/t")
    ax.legend(frameon=False, loc="upper left")
    fig.tight_layout()
    fig.savefig(g / "auktionsreihe.png", dpi=130)
    plt.close(fig)
    # 2 Test: Ist, Fortschreibung, verankert
    d = bt.join(eua.preis.rename("ist"))
    fig, ax = plt.subplots(figsize=(10, 4.5))
    ax.fill_between(
        d.index,
        d.verankert_lo,
        d.verankert_hi,
        color=FARBE["s2"],
        alpha=0.15,
        lw=0,
        label="verankert, 90-%-Intervall",
    )
    ax.plot(
        d.index,
        d.fortschreibung,
        color=FARBE["s1"],
        lw=2,
        label="Fortschreibung (letzter bekannter Preis)",
    )
    ax.plot(
        d.index,
        d.verankert,
        color=FARBE["s2"],
        lw=1.2,
        label="verankerte Änderung (Braunkohle-Stunden)",
    )
    ax.plot(
        d.index,
        d.struktur,
        color=FARBE["s3"],
        lw=1,
        alpha=0.8,
        label="Strukturformel ohne Kalibrierung",
    )
    ist = d.ist.dropna()
    ax.scatter(ist.index, ist, s=8, color=FARBE["ist"], label="Auktionspreis", zorder=3)
    _achse(ax, "Testzeitraum mit realem Berichtsverzug", "EUR/t")
    ax.set_ylim(20, 130)
    ax.legend(frameon=False, ncol=2, fontsize=8, loc="upper left")
    fig.tight_layout()
    fig.savefig(g / "test_verlauf.png", dpi=130)
    plt.close(fig)
    # 3 Plausibilität: Strukturformel gegen Auktionspreis (Monat)
    fig, axs = plt.subplots(1, 2, figsize=(11, 4))
    p = pl.dropna(subset=["eua_ist"])
    axs[0].plot(p.index, p.eua_ist, color=FARBE["ist"], lw=1.5, label="Auktionspreis")
    axs[0].plot(p.index, p.eua_struktur, color=FARBE["s3"], lw=1.5, label="aus Braunkohle-Stunden")
    _achse(axs[0], "EUA: Monatsmittel", "EUR/t")
    axs[0].legend(frameon=False, loc="upper left")
    q = pl.dropna(subset=["gas_weltbank"])
    q = q[q.index >= "2015-04-30"]
    axs[1].plot(q.index, q.gas_weltbank, color=FARBE["ist"], lw=1.5, label="Weltbank (TTF)")
    axs[1].plot(
        q.index, q.gas_implizit, color=FARBE["s2"], lw=1.5, label="aus Gas-Stunden abgeleitet"
    )
    axs[1].plot(q.index, q.lng_acer, color=FARBE["s1"], lw=1.5, label="ACER LNG (Zusatz)")
    _achse(axs[1], "Erdgas: Monatsmittel", "EUR/MWh (Brennwert)")
    axs[1].set_yscale("log")
    axs[1].legend(frameon=False, loc="upper left")
    fig.tight_layout()
    fig.savefig(g / "plausibilitaet.png", dpi=130)
    plt.close(fig)
    # 4 Fehler nach festem Verzug
    fig, ax = plt.subplots(figsize=(7, 3.8))
    ax.plot(
        fv.verzug_tage,
        fv.mae_fortschreibung,
        marker="o",
        ms=5,
        color=FARBE["s1"],
        lw=2,
        label="Fortschreibung",
    )
    ax.plot(
        fv.verzug_tage,
        fv.mae_verankert,
        marker="o",
        ms=5,
        color=FARBE["s2"],
        lw=2,
        label="verankerte Änderung",
    )
    ax.set_xscale("log")
    ax.set_xticks(fv.verzug_tage, [str(x) for x in fv.verzug_tage])
    ax.set_xlabel("Tage seit dem letzten bekannten Auktionspreis")
    _achse(ax, "Mittlerer absoluter Fehler 2024-06/2025 nach Verzug", "EUR/t")
    ax.legend(frameon=False)
    fig.tight_layout()
    fig.savefig(g / "fehler_verzug.png", dpi=130)
    plt.close(fig)
    # 5 Tagesreihe bis heute
    fig, ax = plt.subplots(figsize=(10, 4))
    ax.fill_between(
        tag.index,
        tag.eua_struktur_lo,
        tag.eua_struktur_hi,
        color=FARBE["s3"],
        alpha=0.15,
        lw=0,
        label="Band 5-95 % (Abweichung 36 Monate)",
    )
    ax.plot(
        tag.index,
        tag.eua_struktur,
        color=FARBE["s3"],
        lw=1.2,
        label="Strukturformel (Braunkohle-Stunden)",
    )
    ax.plot(
        tag.index,
        tag.letzter_bekannter_auktionspreis,
        color=FARBE["s1"],
        lw=2,
        label="letzter bekannter Auktionspreis",
    )
    i = tag.eua_ist.dropna()
    ax.scatter(i.index, i, s=8, color=FARBE["ist"], label="Auktionspreis", zorder=3)
    _achse(ax, "Plausibilitätsreihe seit dem letzten Auktionsbericht", "EUR/t")
    ax.legend(frameon=False, ncol=3, fontsize=8, loc="upper left")
    fig.tight_layout()
    fig.savefig(g / "tagesreihe.png", dpi=130)
    plt.close(fig)


CACHE = Path("/tmp/eua")


def main() -> None:
    global CACHE
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--cache", type=Path, default=CACHE)
    ap.add_argument(
        "--ohne-parameterwahl", action="store_true", help="Parametertabelle nicht neu rechnen"
    )
    args = ap.parse_args()
    CACHE = args.cache
    tab = HIER / "tabellen"
    eua = pd.read_csv(HIER / "daten" / "eua_tagesreihe.csv", parse_dates=["datum"]).set_index(
        "datum"
    )
    berichte = pd.read_csv(
        HIER / "daten" / "eua_berichte.csv", parse_dates=["pdf_datum", "letzte_auktion"]
    )
    sofort = berichte.assign(pdf_datum=berichte.letzte_auktion + pd.Timedelta(days=1))
    h = daten.stunden(CACHE).tz_convert("Europe/Berlin")

    def merkmale() -> pd.DataFrame:
        m = tagesmerkmale(h).join(brennstoffe(CACHE, tagesmerkmale(h).index))
        m["kohle_eur_mwh"] = m.kohle_monat / 6.978  # 6000 kcal/kg = 6,978 MWh/t
        return m

    if not args.ohne_parameterwahl:
        parameter(merkmale, eua, berichte).to_csv(tab / "parameterwahl_2018_2023.csv", index=False)
    m = merkmale()
    m.to_csv(tab / "merkmale_tag.csv", float_format="%.3f")
    ist = eua.preis
    reg = regime(m)
    jahr = pd.Series(m.index.year.astype(str), index=m.index)

    bt = backtest(m, eua, berichte, TEST[0], TEST[1])
    kz = pd.concat(
        [
            kennzahlen(bt, ist, reg).assign(aufteilung="regime"),
            kennzahlen(bt, ist, jahr).assign(aufteilung="jahr"),
        ]
    )
    kz.to_csv(tab / "kennzahlen_test.csv", index=False)
    bt.join(ist.rename("ist")).to_csv(tab / "backtest_tag.csv", float_format="%.2f")

    rueck = []
    for verzug, ber in (("realer Berichtsverzug", berichte), ("Quartal sofort", sofort)):
        hist = backtest(m, eua, ber, pd.Timestamp("2018-01-01"), pd.Timestamp("2025-06-30"))
        rueck += [
            kennzahlen(hist, ist, jahr).assign(aufteilung="jahr", verzug=verzug),
            kennzahlen(hist, ist, reg).assign(aufteilung="regime", verzug=verzug),
        ]
    kh = pd.concat(rueck)
    kh.to_csv(tab / "kennzahlen_2018_2025.csv", index=False)

    cache_k: dict[str, Any] = {}

    def k_for(t: pd.Timestamp) -> dict[str, Any]:
        mon = t.to_period("M").to_timestamp()
        if mon not in cache_k:
            cache_k[mon] = kalibrieren(m, bekannte_auktionen(eua, sofort, mon), mon)
        k: dict[str, Any] = cache_k[mon]
        return k

    fv = feste_verzuege(m, eua, k_for)
    fv.to_csv(tab / "fester_verzug.csv", index=False)
    emp = empfindlichkeit(m, ist)
    emp.to_csv(tab / "empfindlichkeit.csv", index=False)
    pl = plausibilitaet(m, ist)
    pl.to_csv(tab / "plausibilitaet_monat.csv")
    va = varianten(m, eua, berichte)
    va.to_csv(tab / "aenderungsmodelle.csv", index=False)
    tag = tagesreihe_plausibel(m, eua, berichte)
    tag.to_csv(tab / "plausibilitaetsreihe_tag.csv")
    grafiken(m, eua, berichte, bt, pl, fv, tag)

    with pd.option_context("display.width", 250, "display.max_columns", 30):
        print(kz.to_string())
        print(kh[kh.gruppe == "alle"].to_string())
        print(fv.to_string())
        print(emp.to_string())
        print(va.to_string())
        print(tag.tail(5).to_string())


if __name__ == "__main__":
    main()
