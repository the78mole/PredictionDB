"""Preisabweichung Liefertag 29.10.2026: Tabellen und Grafiken aus ``daten/``.

Auftrag in ``job.yaml``. Die Rohdaten in ``daten/`` stammen aus der Datenbank der VM
(``abfragen.sql``) und aus den Modellen der Registry (``merkmalsbeitraege.py``); dieses Skript
liest nur diese Dateien und schreibt ``tabellen/`` und ``grafiken/``. Aufruf aus dem
Repository-Wurzelverzeichnis::

    uv run --with matplotlib python docs/analysen/preisabweichung-2026-10-29/auswertung.py

Datenquellen und Lizenzen: Day-Ahead-Preise DE-LU und Nachbarzonen Energy-Charts (CC BY 4.0) bzw.
SMARD (CC BY 4.0, Bundesnetzagentur | SMARD.de), Last/Erzeugung/Residuallast SMARD,
Monatspreise Gas/Öl/Kohle Weltbank Pink Sheet (CC BY 4.0), ULSD EIA (gemeinfrei), Heizöl EU
Weekly Oil Bulletin (CC BY 4.0), EUR/USD EZB.
"""

from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

HERE = Path(__file__).parent
DATA = HERE / "daten"
TAB = HERE / "tabellen"
FIG = HERE / "grafiken"
MMBTU_MWH = 3.412  # 1 MWh = 3,412 MMBtu

#: Kurzname je Version für Tabellen und Legenden
SHORT = {
    "de_lu_v1-kalender-202610242054-9a18ba": "v1 kalender 24.10. (previous)",
    "de_lu_v1-kalender-202610270115-7e355b": "v1 kalender 27.10. (Champion)",
    "de_lu_v2-frei-202610251049-7e1594": "v2 frei 25.10.",
    "de_lu_v2-frei-202610270147-6b979e": "v2 frei 27.10.",
    "de_lu_v2-wetter_frei-202610271848-df539f": "v2 wetter_frei 27.10.",
}


def gruppe(merkmal: str) -> str:
    """Merkmalsgruppe eines Merkmals des Preismodells (für die Summe der Beiträge)."""
    if merkmal.startswith("lag_da_price_de_lu"):
        return "Preisgedächtnis DE-LU"
    if merkmal.startswith("lag_da_price_"):
        return "Nachbarpreise"
    if merkmal.startswith(("s1_", "lag_residual", "lag_load", "lag_gen", "fc_")):
        return "Last, Wind, PV, Residuallast"
    if merkmal.startswith(("asof_mw_", "asof_jw_")):
        return "Marktwerte"
    if merkmal.startswith("asof_"):
        return "Brennstoffe"
    if merkmal.startswith("cap_"):
        return "Bestand"
    if merkmal.startswith("wx_"):
        return "Wetter"
    return "Kalender"


def stunden() -> pd.DataFrame:
    p = pd.read_csv(DATA / "preis_prognose_29_10.csv")
    p["fehler"] = p["ist"] - p["p50"]
    p["im_band"] = (p["ist"] >= p["p10"]) & (p["ist"] <= p["p90"])
    wide = p.pivot(index="stunde", columns="version", values="p50").rename(columns=SHORT)
    wide.insert(0, "ist", p.groupby("stunde")["ist"].first())
    wide.round(1).to_csv(TAB / "stunden_29_10.csv")
    z = p.groupby("version").agg(
        mae=("fehler", lambda x: x.abs().mean()),
        bias=("fehler", "mean"),
        max_fehler=("fehler", "max"),
        stunde_max=("fehler", lambda x: int(p.loc[x.idxmax(), "stunde"])),
        im_band_p10_p90=("im_band", "mean"),
        p50_mittel=("p50", "mean"),
    )
    z.insert(0, "ist_mittel", p["ist"].groupby(p["stunde"]).first().mean())
    z.index = z.index.map(SHORT)
    z.round(2).to_csv(TAB / "zusammenfassung.csv")
    return p


def beitraege() -> pd.DataFrame:
    b = pd.read_csv(DATA / "merkmalsbeitraege_29_10.csv")
    b["gruppe"] = b["merkmal"].map(gruppe)
    g = b.pivot_table(index="version", columns="gruppe", values="beitrag_eur_mwh", aggfunc="sum")
    g = g.fillna(0.0)
    g.index = g.index.map(SHORT)
    g.round(1).to_csv(TAB / "beitraege_gruppen.csv")
    return g


def monate() -> pd.DataFrame:
    t = pd.read_csv(DATA / "tageswerte.csv", parse_dates=["d"]).set_index("d")
    b = pd.read_csv(DATA / "brennstoffe_monatlich.csv", parse_dates=["ts"])
    d = pd.read_csv(DATA / "brennstoffe_taeglich_woechentlich.csv", parse_dates=["ts"])
    m = pd.DataFrame(
        {
            "strom_de_lu": t["da_price_de_lu"].resample("MS").mean(),
            "residuallast_gw": t["residual_load"].resample("MS").mean() / 1000,
            "wind_gw": (t["gen_wind_onshore"] + t["gen_wind_offshore"]).resample("MS").mean()
            / 1000,
        }
    )
    for code in ["gas_eu_m", "oil_brent_m", "coal_au_m", "lng_japan_m", "fx_usd_eur_m"]:
        m[code.removesuffix("_m")] = b[b["code"] == code].set_index("ts")["value"]
    m["ulsd_nyh"] = d[d["code"] == "oil_ulsd_nyh"].set_index("ts")["value"].resample("MS").mean()
    m["heizoel_de_netto"] = (
        d[d["code"] == "retail_de_heating_oil_net"].set_index("ts")["value"].resample("MS").mean()
    )
    m["gas_eur_mwh"] = m["gas_eu"] / m["fx_usd_eur"] * MMBTU_MWH
    m["kohle_eur_t"] = m["coal_au"] / m["fx_usd_eur"]
    m.loc["2021-01-01":].round(2).to_csv(TAB / "brennstoffe_monat.csv")
    return m


def residuallast() -> pd.DataFrame:
    t = pd.read_csv(DATA / "tageswerte.csv", parse_dates=["d"]).set_index("d")
    df = pd.DataFrame({"preis": t["da_price_de_lu"], "rl": t["residual_load"] / 1000}).dropna()
    ym = df.index.year * 100 + df.index.month
    df["zeitraum"] = pd.cut(
        ym,
        [0, 202108, 202212, 202602, 202607, 202612],
        labels=[
            "2018-10 bis 2021-08",
            "2021-09 bis 2022-12",
            "2023-01 bis 2026-02",
            "2026-03 bis 2026-07",
            "2026-08 bis 2026-10",
        ],
    )
    df["klasse"] = pd.cut(
        df["rl"],
        [-30, 10, 20, 30, 35, 40, 60],
        labels=["<10", "10-20", "20-30", "30-35", "35-40", ">40"],
    )
    mean = df.pivot_table(
        index="klasse", columns="zeitraum", values="preis", aggfunc="mean", observed=False
    )
    n = df.pivot_table(
        index="klasse", columns="zeitraum", values="preis", aggfunc="size", observed=False
    )
    mean.round(0).to_csv(TAB / "preis_je_residuallast.csv")
    n.to_csv(TAB / "tage_je_residuallast.csv")
    return mean


def rueckfall() -> pd.DataFrame:
    """Tage, deren Tagesmittel mindestens 1,7-mal so hoch war wie das 7-Tage-Mittel (und
    >= 100 EUR/MWh): Was passierte am Folgetag? Das ist die Lage am 28.10. (249 zu 121)."""
    t = pd.read_csv(DATA / "tageswerte.csv", parse_dates=["d"]).set_index("d")
    p = t["da_price_de_lu"].dropna()
    df = pd.DataFrame({"preis": p, "mittel_7d": p.rolling(7).mean(), "folgetag": p.shift(-1)})
    df["verhaeltnis"] = df["preis"] / df["mittel_7d"]
    df["aenderung"] = df["folgetag"] - df["preis"]
    sel = df[(df["verhaeltnis"] >= 1.7) & (df["preis"] >= 100)].dropna()
    sel.round(1).to_csv(TAB / "rueckfall_faelle.csv")
    s = sel.groupby(sel.index.year)["aenderung"].agg(
        faelle="size", mittel="mean", median="median", anteil_steigend=lambda x: (x > 0).mean()
    )
    s.loc["alle"] = [
        len(sel),
        sel["aenderung"].mean(),
        sel["aenderung"].median(),
        (sel["aenderung"] > 0).mean(),
    ]
    s.round(2).to_csv(TAB / "rueckfall_jahre.csv")
    return s


def trainingsabdeckung(m: pd.DataFrame) -> pd.DataFrame:
    """Anteil der Trainingstage (Ausgabetage) je Gaspreisniveau, mit dem Monatswert, der am
    jeweiligen Tag veröffentlicht war (Weltbank ca. am 8. des Folgemonats)."""
    t = pd.read_csv(DATA / "tageswerte.csv", parse_dates=["d"]).set_index("d")
    g = m["gas_eur_mwh"].copy()
    g.index = g.index + pd.offsets.MonthBegin(1) + pd.Timedelta(days=7)
    rows = []
    for name, start in [("v1/v2 kalender, frei", "2018-10-08"), ("v2 wetter_frei", "2021-03-23")]:
        days = pd.date_range(start, "2026-10-16")
        asof = g.reindex(days, method="ffill")
        price = t["da_price_de_lu"].reindex(days)
        for lo, hi in [(0, 30), (30, 50), (50, 70), (70, 1000)]:
            sel = (asof >= lo) & (asof < hi)
            rows.append(
                {
                    "modelle": name,
                    "gas_eur_mwh": f"{lo}-{hi if hi < 1000 else ''}",
                    "tage": int(sel.sum()),
                    "anteil": round(sel.mean(), 3),
                    "strompreis_mittel": round(price[sel].mean(), 1),
                }
            )
    out = pd.DataFrame(rows)
    out.to_csv(TAB / "trainingsabdeckung_gas.csv", index=False)
    return out


# Grafiken ------------------------------------------------------------------------------------


def fig_stunden(p: pd.DataFrame) -> None:
    fig, ax = plt.subplots(figsize=(10, 5))
    ist = p.groupby("stunde")["ist"].first()
    ax.plot(ist.index, ist.values, color="black", lw=2.5, label="Day-Ahead-Preis (Ist)")
    colors = plt.cm.tab10.colors
    for i, (v, g) in enumerate(p.groupby("version")):
        g = g.sort_values("stunde")
        ax.plot(g["stunde"], g["p50"], color=colors[i], lw=1.5, label=f"P50 {SHORT[v]}")
        if "Champion" in SHORT[v] or "wetter" in v:
            ax.fill_between(g["stunde"], g["p10"], g["p90"], color=colors[i], alpha=0.15)
    ax.set_xlabel("Stunde 29.10.2026 (Ortszeit)")
    ax.set_ylabel("€/MWh")
    ax.set_xticks(range(0, 24, 2))
    ax.set_title(
        "Liefertag 29.10.2026: Ist gegen P50 (Bänder P10-P90 für Champion und wetter_frei)"
    )
    ax.grid(alpha=0.3)
    ax.legend(fontsize=8, loc="upper left")
    fig.tight_layout()
    fig.savefig(FIG / "preis_29_10.png", dpi=120)
    plt.close(fig)


def fig_beitraege(g: pd.DataFrame) -> None:
    fig, ax = plt.subplots(figsize=(10, 4.8))
    cols = [c for c in g.columns if g[c].abs().max() >= 0.5]
    y = np.arange(len(g))
    h = 0.8 / len(cols)
    colors = plt.cm.tab10.colors
    for i, c in enumerate(cols):
        ax.barh(y + i * h, g[c], height=h, color=colors[i % 10], label=c)
    ax.set_yticks(y + 0.4 - h / 2)
    ax.set_yticklabels(g.index, fontsize=8)
    ax.axvline(0, color="black", lw=0.8)
    ax.set_xlabel("mittlerer Beitrag zum P50 am 29.10. gegenüber dem Vortagesmittel (€/MWh)")
    ax.set_title("Wer zieht die Preisprognose wohin? (Merkmalsbeiträge, Summe je Gruppe)")
    ax.grid(axis="x", alpha=0.3)
    ax.legend(fontsize=8, loc="lower left")
    fig.tight_layout()
    fig.savefig(FIG / "beitraege_29_10.png", dpi=120)
    plt.close(fig)


def fig_brennstoffe(m: pd.DataFrame) -> None:
    m = m.loc["2021-01-01":]
    fig, ax = plt.subplots(figsize=(10, 5))
    ax.plot(m.index, m["strom_de_lu"], color="black", lw=2, label="Strom DE-LU Day-Ahead (€/MWh)")
    ax.plot(m.index, m["gas_eur_mwh"], color="tab:red", lw=1.8, label="Gas Europa/TTF (€/MWh)")
    ax.plot(m.index, m["oil_brent"], color="tab:brown", lw=1.2, ls="--", label="Brent ($/bbl)")
    ax.plot(
        m.index, m["kohle_eur_t"], color="tab:gray", lw=1.2, ls=":", label="Kohle Australien (€/t)"
    )
    ax.set_ylabel("Preis")
    ax.set_title("Monatsmittel 2021-2026: Strom und fossile Brennstoffe")
    ax.axvspan(
        pd.Timestamp("2026-08-01"),
        m.index.max() + pd.Timedelta(days=31),
        color="tab:orange",
        alpha=0.1,
    )
    ax.grid(alpha=0.3)
    ax.legend(fontsize=8, loc="upper right")
    fig.tight_layout()
    fig.savefig(FIG / "brennstoffe_verlauf.png", dpi=120)
    plt.close(fig)


def fig_residuallast(mean: pd.DataFrame) -> None:
    fig, ax = plt.subplots(figsize=(9, 4.8))
    for c in mean.columns:
        ax.plot(range(len(mean)), mean[c].values, marker="o", label=str(c))
    ax.set_xticks(range(len(mean)))
    ax.set_xticklabels([f"{k} GW" for k in mean.index])
    ax.set_xlabel("Residuallast (Tagesmittel)")
    ax.set_ylabel("Day-Ahead-Preis DE-LU, Tagesmittel (€/MWh)")
    ax.set_title("Gleiche Residuallast, anderer Preis: Merit-Order-Kurve je Zeitraum")
    ax.grid(alpha=0.3)
    ax.legend(fontsize=8)
    fig.tight_layout()
    fig.savefig(FIG / "preis_je_residuallast.png", dpi=120)
    plt.close(fig)


def main() -> None:
    TAB.mkdir(exist_ok=True)
    FIG.mkdir(exist_ok=True)
    p = stunden()
    g = beitraege()
    m = monate()
    r = residuallast()
    rueckfall()
    trainingsabdeckung(m)
    fig_stunden(p)
    fig_beitraege(g)
    fig_brennstoffe(m)
    fig_residuallast(r)


if __name__ == "__main__":
    main()
