"""Preisniveau-Schätzung: Backtest eines Monatspreis-Modells aus freien Daten.

Schätzt an jedem Monatsersten 2016-2026 die Monatsmittel des Day-Ahead-Preises (Base) und
die Marktwerte Solar und Wind an Land für die nächsten 18 Monate, nur mit Daten, die an
diesem Tag vorlagen. Vergleicht mit dem später realisierten Wert und mit naiven Schätzern.
Konzept: Claude Doc "Synthetisches Preisniveau statt Terminpreisen"; Messlatte nach
docs/analysen/terminpreis-treffsicherheit.

Modell (je Stichtag t):

1. Brennstoffniveau C: Median des Preises in Stunden hoher Residuallast (oberes Viertel der
   Residuallast der 365 Tage vor t) in den 30 Tagen vor t. In diesen Stunden setzen Gas- oder
   Steinkohlekraftwerke den Preis, C enthält also Brennstoff- und CO2-Kosten zusammen.
2. Merit-Order-Form g: in den 365 Tagen vor t der Preis jeder Stunde geteilt durch das
   C der 30 Tage davor, als Median je Residuallast-Klasse (2 GW).
3. Erwartete Residuallast je Zielmonat: dieselben Kalendermonate der bis zu drei Vorjahre,
   Wind und Solar mit der installierten Leistung skaliert (Stand vor t, linear bis zum
   Zielmonat fortgeschrieben), Last unverändert.
4. Preis je simulierter Stunde = C * g(Residuallast); Base = Mittel, Marktwert = mit der
   simulierten Erzeugung gewichtetes Mittel. Je Zielmonat Mittel über die Vorjahre.

Aufruf aus dem Repository-Wurzelverzeichnis (lädt beim ersten Lauf etwa 12 Jahre
Stundendaten von Energy-Charts, einige Minuten)::

    uv run --no-project --with pandas --with matplotlib --with httpx \\
        python docs/analysen/preisniveau-schaetzung/auswertung.py --cache /tmp/pn
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import httpx
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

HIER = Path(__file__).resolve().parent
sys.path.insert(0, str(HIER))
import brennstoff  # noqa: E402
import daten  # noqa: E402

HORIZONTE = 18
VORJAHRE = 3
RL_KLASSE = 2000.0  # MW
HOCH_QUANTIL = 0.75
NIVEAU_TAGE = 30
FORM_TAGE = 365
KRISE = (pd.Period("2021-07", "M"), pd.Period("2023-06", "M"))
TECHNIK = {"solar": "Solar DC", "wind_onshore": "Wind onshore", "wind_offshore": "Wind offshore"}
MARKTWERTE = ["solar", "wind_onshore"]


# ---------------------------------------------------------------------------------------------
# Daten
# ---------------------------------------------------------------------------------------------


def leistung() -> pd.DataFrame:
    """Installierte Leistung in GW je Monat (Energy-Charts ``/installed_power``)."""
    r = httpx.get(
        f"{daten.API}/installed_power",
        params={"country": "de", "time_step": "monthly", "installation_decommission": "false"},
        timeout=120,
    )
    r.raise_for_status()
    d = r.json()
    idx = pd.PeriodIndex([pd.Period(f"{t[3:]}-{t[:2]}", "M") for t in d["time"]])
    out = {}
    for code, name in TECHNIK.items():
        werte = next(p["data"] for p in d["production_types"] if p["name"] == name)
        out[code] = pd.Series(werte, index=idx, dtype=float)
    return pd.DataFrame(out).ffill().fillna(0.0)


def vorbereiten(df: pd.DataFrame) -> pd.DataFrame:
    df = df.dropna(subset=["preis", "last", "solar", "wind_onshore"]).copy()
    df["wind_offshore"] = df["wind_offshore"].fillna(0.0)
    df["rl"] = df["last"] - df["solar"] - df["wind_onshore"] - df["wind_offshore"]
    df["monat"] = df.index.tz_localize(None).to_period("M")
    return df


# ---------------------------------------------------------------------------------------------
# Modell
# ---------------------------------------------------------------------------------------------


def niveau(fenster: pd.DataFrame, schwelle: float) -> float:
    """Brennstoffniveau C: Median des Preises in Stunden hoher Residuallast."""
    hoch = fenster.loc[fenster["rl"] >= schwelle, "preis"]
    return float(hoch.median()) if len(hoch) >= 24 else float("nan")


def form(vorher: pd.DataFrame, schwelle: float) -> pd.Series:
    """Merit-Order-Form g(RL): Preis / C der 30 Tage davor, Median je RL-Klasse."""
    hoch = vorher["preis"].where(vorher["rl"] >= schwelle)
    c = hoch.rolling(f"{NIVEAU_TAGE}D", min_periods=24).median().shift(1)
    ok = c > 5.0  # sehr kleines C macht das Verhältnis instabil
    r = (vorher["preis"] / c)[ok]
    klasse = np.floor(vorher.loc[ok, "rl"] / RL_KLASSE)
    g = r.groupby(klasse).median()
    n = r.groupby(klasse).size()
    return g[n >= 10]


def g_auswerten(g: pd.Series, rl: np.ndarray) -> np.ndarray:
    k = np.floor(rl / RL_KLASSE)
    out: np.ndarray = np.interp(k, g.index.to_numpy(dtype=float), g.to_numpy(dtype=float))
    return out


def kapazitaet(cap: pd.DataFrame, stand: pd.Period, ziel: pd.Period) -> pd.Series:
    """Installierte Leistung im Zielmonat, linear aus den 12 Monaten vor dem Stand."""
    hist = cap.loc[stand - 12 : stand]
    steigung = (hist.iloc[-1] - hist.iloc[0]) / 12
    return hist.iloc[-1] + steigung * (ziel - stand).n


def prognose(df: pd.DataFrame, cap: pd.DataFrame, t: pd.Timestamp) -> list[dict[str, Any]]:
    vorher = df[(df.index >= t - pd.Timedelta(days=FORM_TAGE + NIVEAU_TAGE)) & (df.index < t)]
    jahr = vorher[vorher.index >= t - pd.Timedelta(days=FORM_TAGE)]
    schwelle = float(jahr["rl"].quantile(HOCH_QUANTIL))
    c = niveau(jahr[jahr.index >= t - pd.Timedelta(days=NIVEAU_TAGE)], schwelle)
    g = form(vorher, schwelle)
    stand = t.tz_localize(None).to_period("M") - 1  # letzter vollständiger Monat
    zeilen = []
    for h in range(0, HORIZONTE + 1):
        ziel = stand + h
        cap_ziel = kapazitaet(cap, stand, ziel)
        base: list[float] = []
        mw: dict[str, list[float]] = {k: [] for k in MARKTWERTE}
        for v in range(1, VORJAHRE + 1):
            profil_monat = ziel - 12 * v
            if profil_monat > stand:
                continue
            p = df[df["monat"] == profil_monat]
            if len(p) < 600:
                continue
            gen = {
                k: p[k].to_numpy() * cap_ziel[k] / max(cap.loc[profil_monat, k], 1e-3)
                for k in TECHNIK
            }
            rl = p["last"].to_numpy() - sum(gen.values())
            preis = c * g_auswerten(g, rl)
            base.append(preis.mean())
            for k in MARKTWERTE:
                mw[k].append(float((preis * gen[k]).sum() / gen[k].sum()))
        if not base:
            continue
        zeilen.append(
            {
                "stichtag": t.tz_localize(None).to_period("M"),
                "ziel": ziel,
                "h": h,
                "C": c,
                "modell_base": float(np.mean(base)),
                **{f"modell_mw_{k}": float(np.mean(v)) for k, v in mw.items()},
            }
        )
    return zeilen


# ---------------------------------------------------------------------------------------------
# Ist-Werte und naive Schätzer
# ---------------------------------------------------------------------------------------------


def ist_monate(df: pd.DataFrame) -> pd.DataFrame:
    g = df.groupby("monat")
    out = pd.DataFrame({"base": g["preis"].mean(), "stunden": g.size()})
    for k in MARKTWERTE:
        out[f"mw_{k}"] = g.apply(lambda x, k=k: (x["preis"] * x[k]).sum() / x[k].sum())
        out[f"wf_{k}"] = out[f"mw_{k}"] / out["base"]
    soll = out.index.days_in_month.to_numpy() * 24
    return out[out["stunden"] >= soll - 2]  # nur vollständige Monate (Zeitumstellung: +-1)


def naive(ist: pd.DataFrame, stand: pd.Period, ziel: pd.Period) -> dict[str, float]:
    def wert(m: pd.Period, col: str) -> float:
        return float(ist.at[m, col]) if m in ist.index else float("nan")

    vorjahr = ziel - 12
    while vorjahr > stand:
        vorjahr -= 12
    letzte12 = ist.loc[stand - 11 : stand, "base"]
    out = {
        "naiv_letzter_monat": wert(stand, "base"),
        "naiv_12_monate": float(letzte12.mean()) if len(letzte12) >= 11 else float("nan"),
        "naiv_vorjahresmonat": wert(vorjahr, "base"),
    }
    # Marktwert naiv: Wertfaktor des Vorjahresmonats x Base des letzten Monats
    for k in MARKTWERTE:
        out[f"naiv_mw_{k}"] = wert(vorjahr, f"wf_{k}") * out["naiv_letzter_monat"]
    return out


# ---------------------------------------------------------------------------------------------
# Auswertung
# ---------------------------------------------------------------------------------------------


def backtest(df: pd.DataFrame, cap: pd.DataFrame) -> pd.DataFrame:
    ist = ist_monate(df)
    letzter = ist.index.max()
    zeilen = []
    for t in pd.date_range(
        "2016-01-01", (letzter + 1).to_timestamp(), freq="MS", tz="Europe/Berlin"
    ):
        for z in prognose(df, cap, t):
            stand = z["stichtag"] - 1
            z.update(naive(ist, stand, z["ziel"]))
            if z["ziel"] in ist.index:
                z["ist_base"] = ist.at[z["ziel"], "base"]
                for k in MARKTWERTE:
                    z[f"ist_mw_{k}"] = ist.at[z["ziel"], f"mw_{k}"]
            zeilen.append(z)
    bt = pd.DataFrame(zeilen)
    # Variante "Modell relativ": letzter Monat x Verhältnis Modell(Ziel) / Modell(letzter Monat).
    # Das gleicht ein falsches Brennstoffniveau aus und behält nur die Saison- und Ausbaueffekte.
    h0 = bt[bt["h"] == 0].set_index("stichtag")
    for col, ist_col in [
        ("modell_base", "base"),
        *[(f"modell_mw_{k}", f"mw_{k}") for k in MARKTWERTE],
    ]:
        basis = bt["stichtag"].map(h0[col])
        ist_stand = (bt["stichtag"] - 1).map(ist[ist_col])
        bt[col.replace("modell_", "relativ_")] = ist_stand * bt[col] / basis
    bt = bt[bt["h"] >= 1].reset_index(drop=True)
    bt["krise"] = (bt["ziel"] >= KRISE[0]) & (bt["ziel"] <= KRISE[1])
    return bt


MIN_TRAINING = 24
RIDGE = 1.0
MERKMALE_BRENNSTOFF = ["d_henry_hub", "d_brent"]
MERKMALE_ALLE = ["saison", "d_henry_hub", "d_brent", "speicher_abw", "gas_trend"]


def regression(bt: pd.DataFrame, merkmale: list[str]) -> pd.Series:
    """Prognose = letzter Monat x exp(Ridge-Regression der log-Änderung, ohne Achsenabschnitt).

    Je Stichtag und Horizont wird nur auf Zeilen desselben Horizonts trainiert, deren Zielmonat
    vor dem Stichtag lag (Ist bekannt). Ohne Achsenabschnitt: Ein im Training gelernter Trend
    (2016-2021 stiegen die Preise) würde sonst fortgeschrieben. Fehlen Merkmale (STEO reicht nur
    bis Ende Y+1) oder Trainingsdaten, bleibt es beim letzten Monat.
    """
    y = np.log(bt["ist_base"] / bt["naiv_letzter_monat"])
    ok = bt[merkmale].notna().all(axis=1) & (bt["naiv_letzter_monat"] > 0)
    out = bt["naiv_letzter_monat"].copy()
    for (stichtag, h), zeilen in bt[ok].groupby(["stichtag", "h"]):
        stand = stichtag - 1
        train = ok & y.notna() & np.isfinite(y) & (bt["h"] == h) & (bt["ziel"] <= stand)
        if train.sum() < MIN_TRAINING:
            continue
        x = bt.loc[train, merkmale].to_numpy(dtype=float)
        yt = y[train].to_numpy()
        beta = np.linalg.solve(x.T @ x + RIDGE * np.eye(len(merkmale)), x.T @ yt)
        xn = zeilen[merkmale].to_numpy(dtype=float)
        out[zeilen.index] = zeilen["naiv_letzter_monat"] * np.exp(xn @ beta)
    return out


def brennstoff_spalten(bt: pd.DataFrame, cache: Path, bis: pd.Period) -> pd.DataFrame:
    ausgaben = brennstoff.steo(cache / "steo", 2015, bis)
    lager = brennstoff.speicher(cache)
    gas = brennstoff.gas_europa(cache)
    m = pd.DataFrame(
        [
            brennstoff.merkmale(r.stichtag - 1, r.ziel, ausgaben, lager, gas)
            for r in bt[["stichtag", "ziel"]].itertuples()
        ],
        index=bt.index,
    )
    bt = bt.join(m)
    bt["saison"] = np.log(bt["relativ_base"] / bt["naiv_letzter_monat"])
    bt["brennstoff_base"] = regression(bt, MERKMALE_BRENNSTOFF)
    bt["alle_base"] = regression(bt, MERKMALE_ALLE)
    return bt


SCHAETZER_BASE = {
    "modell_base": "Modell",
    "relativ_base": "Modell relativ",
    "brennstoff_base": "STEO-Regression",
    "alle_base": "Regression alle Merkmale",
    "naiv_letzter_monat": "letzter Monat",
    "naiv_12_monate": "12-Monats-Mittel",
    "naiv_vorjahresmonat": "Vorjahresmonat",
}


def kennzahlen(bt: pd.DataFrame, ziel: str, schaetzer: dict[str, str]) -> pd.DataFrame:
    d = bt.dropna(subset=[ziel, *schaetzer])
    zeilen = []
    for teil, maske in [("alle", d["krise"] | ~d["krise"]), ("ohne Krise", ~d["krise"])]:
        for gruppe, hs in [
            ("1-3", (1, 3)),
            ("4-6", (4, 6)),
            ("7-12", (7, 12)),
            ("13-18", (13, 18)),
        ]:
            x = d[maske & d["h"].between(*hs)]
            z = {"teil": teil, "horizont": gruppe, "n": len(x)}
            for col, name in schaetzer.items():
                f = x[col] - x[ziel]
                z[f"{name} MAE"] = round(f.abs().mean(), 2)
                z[f"{name} Bias"] = round(f.mean(), 2)
            zeilen.append(z)
    return pd.DataFrame(zeilen)


GRAFIK_BASE = {
    k: SCHAETZER_BASE[k]
    for k in ("modell_base", "brennstoff_base", "naiv_letzter_monat", "naiv_12_monate")
}


def grafik_horizont(bt: pd.DataFrame, ziel: Path) -> None:
    d = bt.dropna(subset=["ist_base", *SCHAETZER_BASE])
    fig, axes = plt.subplots(1, 2, figsize=(11, 4.2), sharey=False)
    for ax, (titel, maske) in zip(
        axes,
        [
            ("alle Zielmonate", d["krise"] | ~d["krise"]),
            ("ohne Krise 07/2021-06/2023", ~d["krise"]),
        ],
        strict=True,
    ):
        x = d[maske]
        for col, name in GRAFIK_BASE.items():
            mae = (x[col] - x["ist_base"]).abs().groupby(x["h"]).mean()
            ax.plot(
                mae.index,
                mae.to_numpy(),
                marker="o",
                ms=3,
                lw=2.2 if col == "brennstoff_base" else 1.2,
                label=name,
            )
        ax.set_title(titel)
        ax.set_xlabel("Horizont (Monate)")
        ax.grid(alpha=0.3)
    axes[0].set_ylabel("MAE Base (EUR/MWh)")
    axes[1].legend(fontsize=8)
    fig.suptitle("Fehler der Monats-Base-Prognose nach Horizont")
    fig.tight_layout()
    fig.savefig(ziel, dpi=150)
    plt.close(fig)


def grafik_verlauf(bt: pd.DataFrame, ist: pd.DataFrame, ziel: Path) -> None:
    fig, ax = plt.subplots(figsize=(11, 4.5))
    ax.plot(
        ist.index.to_timestamp(), ist["base"], color="black", lw=1.5, label="Ist (Monatsmittel)"
    )
    for h, farbe in [(3, "tab:blue"), (12, "tab:orange")]:
        x = bt[bt["h"] == h].set_index("ziel").sort_index()
        ax.plot(
            x.index.to_timestamp(),
            x["modell_base"],
            color=farbe,
            lw=1.2,
            label=f"Modell, {h} Monate vorher",
        )
    ax.set_yscale("log")
    ax.set_yticks([25, 50, 100, 200, 400])
    ax.yaxis.set_major_formatter(matplotlib.ticker.ScalarFormatter())
    ax.yaxis.set_minor_formatter(matplotlib.ticker.NullFormatter())
    ax.set_ylabel("EUR/MWh")
    ax.grid(alpha=0.3)
    ax.legend(fontsize=8)
    ax.set_title("Monats-Base: Ist gegen Modellprognose")
    fig.tight_layout()
    fig.savefig(ziel, dpi=150)
    plt.close(fig)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--cache", type=Path, required=True, help="Ordner für die Stundendaten")
    ap.add_argument("--bis", type=int, default=pd.Timestamp.now().year)
    args = ap.parse_args()

    df = vorbereiten(daten.lade(args.cache, args.bis))
    cap = leistung()
    bt = backtest(df, cap)
    ist = ist_monate(df)
    bt = brennstoff_spalten(bt, args.cache, ist.index.max() + 1)

    tab = HIER / "tabellen"
    gra = HIER / "grafiken"
    tab.mkdir(exist_ok=True)
    gra.mkdir(exist_ok=True)
    bt.round({c: 2 for c in bt.select_dtypes("number").columns}).to_csv(
        tab / "prognosen.csv", index=False
    )
    ist.round(3).to_csv(tab / "ist_monate.csv")
    kb = kennzahlen(bt, "ist_base", SCHAETZER_BASE)
    kb.to_csv(tab / "kennzahlen_base.csv", index=False)
    for k in MARKTWERTE:
        km = kennzahlen(
            bt,
            f"ist_mw_{k}",
            {
                f"modell_mw_{k}": "Modell",
                f"relativ_mw_{k}": "Modell relativ",
                f"naiv_mw_{k}": "naiv",
            },
        )
        km.to_csv(tab / f"kennzahlen_mw_{k}.csv", index=False)
    grafik_horizont(bt, gra / "fehler_horizont.png")
    grafik_verlauf(bt, ist, gra / "verlauf.png")
    lauf = {
        "stunden": len(df),
        "von": str(df.index.min()),
        "bis": str(df.index.max()),
        "prognosen": len(bt),
    }
    (tab / "lauf.json").write_text(json.dumps(lauf, indent=2) + "\n")
    with pd.option_context("display.width", 250, "display.max_columns", 40):
        print(kb)


if __name__ == "__main__":
    main()
