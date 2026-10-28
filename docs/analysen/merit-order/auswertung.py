"""Merit-Order Deutschland aus MaStR-Einheiten, aktuellen Brennstoffpreisen und KWK-Wärmegutschrift.

Einmalige Auswertung vom 27.10.2026. Jede Verbrennungseinheit aus dem MaStR bekommt einen
geschätzten Wirkungsgrad (Technologie, Baujahr, Größe) und damit Grenzkosten zu den
Brennstoff- und CO2-Preisen des Stichtags. KWK-Einheiten erhalten eine Gutschrift für die
Wärme, die einen Gaskessel ersetzt. Die aufsteigend sortierte Treppe wird für die letzten 30
Tage mit der gemessenen Last und Einspeisung geschnitten und mit dem Börsenpreis verglichen.

Eingaben (``--daten``): die CSVs aus ``mastr_export.py`` und ``db_export.sh``. Aufruf aus dem
Repository-Wurzelverzeichnis::

    uv run --no-project --with pandas --with numpy --with matplotlib \\
        python docs/analysen/merit-order/auswertung.py --daten ./meritorder-daten \\
        [--html /tmp/merit-order.html]
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

HIER = Path(__file__).resolve().parent

# ---------------------------------------------------------------- Annahmen (Stand 27.10.2026)
# TTF, EUA und API2 gibt es nicht als freie Tagesreihe; die drei Werte sind einmalig von Hand
# übernommen (Fundstellen in README.md), alle übrigen kommen aus der Datenbank des Stacks.
FX = 1.115  # USD je EUR, EZB 26.10.2026 (ecb.fx_usd_eur)
CO2_ETS = 86.0  # EUR/t, EUA Dez-26, Settlement 23.10.2026
CO2_BEHG = 60.0  # EUR/t, nationaler CO2-Preis 2026 (Korridor 55-65) für Anlagen < 20 MW FWL
TTF = 94.6  # EUR/MWh_Hs, 27.10.2026
API2_USD = 137.0  # USD/t, letzter gefundener Wert (18.09.2026)
ULSD_USD_GAL = 5.265  # USD/gal, EIA NY Harbor ULSD 19.10.2026 (eia.oil_ulsd_nyh)
BRENT_USD = 116.8  # USD/bbl, Weltbank August 2026 (worldbank.oil_brent_m)

FUEL = {  # EUR/MWh_th frei Kraftwerk
    "gas": TTF + 2.5,
    "gas_small": TTF + 14.0,  # Netzentgelte und Umlagen kleiner Abnehmer
    "coal": API2_USD / FX / 6.978 + 2.5,  # 6000 kcal/kg = 6,978 MWh/t, + Transport
    "lignite": 4.0,  # variable Tagebaukosten
    "oil_light": ULSD_USD_GAL / FX / 3.785 / 9.96 * 1000,
    "oil_heavy": BRENT_USD / FX / 1.70 * 0.85,
    "waste": -10.0,  # Entsorgungserlös, verbrennt ohnehin
    "byproduct": 8.0,  # Gicht-, Koksofen-, Raffineriegas
    "heat": 0.0,  # Dampfteil ohne eigene Feuerung, eigene Regel in bloecke()
}
EF = {  # t CO2 / MWh_th
    "gas": 0.201,
    "gas_small": 0.201,
    "coal": 0.335,
    "lignite": 0.400,
    "oil_light": 0.266,
    "oil_heavy": 0.280,
    "waste": 0.0,
    "byproduct": 0.0,
    "heat": 0.0,
}
WAERMEWERT = (FUEL["gas"] + EF["gas"] * CO2_ETS) / 0.92  # ersetzter Gaskessel, EUR/MWh_th
#: Anteil der KWK-Wärmeleistung, der abgesetzt wird
JAHRESZEITEN = {"sommer": 0.25, "jetzt": 0.6, "winter": 1.0}
JZ_NAME = {"sommer": "Sommer", "jetzt": "Ende Oktober", "winter": "Winter"}
VERFUEGBAR = 0.88  # thermische Anlagen: Revisionen und Ausfälle
#: Untergrenze der KWK-Grenzkosten: Wärmespeicher, E-Kessel und Spitzenkessel machen tiefere
#: Gebote unwirtschaftlich
KWK_UNTERGRENZE = -30.0
#: Suchbereich der Kalibrierung (zusätzliche Nachfrage in MW)
ZUSATZ = np.arange(-5000, 25001, 250)

GRUPPE = {
    "lignite": "Braunkohle",
    "coal": "Steinkohle",
    "gas": "Erdgas",
    "gas_small": "Erdgas",
    "oil_light": "Öl",
    "oil_heavy": "Öl",
    "waste": "Abfall, Kuppelgas",
    "byproduct": "Abfall, Kuppelgas",
    "heat": "Erdgas",
}
FARBE = {
    "Wind + PV": "#1baf7a",
    "Biomasse, Wasser": "#008300",
    "Abfall, Kuppelgas": "#e87ba4",
    "Braunkohle": "#eb6834",
    "Steinkohle": "#6250d6",
    "Erdgas": "#2a78d6",
    "Pumpspeicher": "#eda100",
    "Öl": "#e34948",
}
FESTE_KOPPLUNG = (
    "Gegendruck",
    "Verbrennungsmotor",
    "Dampfmotor",
    "Stirling",
    "Brennstoffzelle",
    "ORC",
    "Sonstige",
    "Kalina",
)
BEISPIELE = [  # Name, Brennstoff, Wirkungsgrad, variable Betriebskosten
    ("Braunkohle, BoA-Block 2012", "lignite", 0.435, 4.5),
    ("Braunkohle, 500-MW-Block 1975", "lignite", 0.345, 4.5),
    ("Steinkohle 2015", "coal", 0.46, 4.5),
    ("Steinkohle 1985", "coal", 0.386, 4.5),
    ("GuD 2016", "gas", 0.605, 3.0),
    ("GuD 2000", "gas", 0.515, 3.0),
    ("Offene Gasturbine 2010", "gas", 0.3925, 5.0),
    ("Öl-Gasturbine 1975", "oil_light", 0.27, 5.0),
]


def lin(jahr: int, j0: int, e0: float, steigung: float, lo: float, hi: float) -> float:
    return float(np.clip(e0 + steigung * (jahr - j0), lo, hi))


def eta_el(tech: str, brennstoff: str, jahr: int, p_mw: float) -> float:
    """Elektrischer Nettowirkungsgrad im Kondensationsbetrieb aus Technologie und Baujahr."""
    t = tech or ""
    if brennstoff == "lignite":
        return lin(jahr, 1960, 0.30, 0.0030, 0.30, 0.435)
    if brennstoff == "coal":
        if "Gegendruck" in t:
            return lin(jahr, 1965, 0.30, 0.0025, 0.30, 0.40)
        return lin(jahr, 1965, 0.33, 0.0028, 0.33, 0.46)
    if brennstoff == "waste":
        return 0.24
    if brennstoff == "byproduct":
        return 0.36
    # Gas und Öl nach Technik
    if "nachgeschalteter Dampfturbine" in t or ("Abhitzekessel" in t and p_mw >= 50):
        e = lin(jahr, 1990, 0.45, 0.0065, 0.45, 0.605)
        return e - (0.04 if p_mw < 50 else 0.0)
    if "ohne Abhitzekessel" in t or "Abhitzekessel" in t:
        return lin(jahr, 1975, 0.27, 0.0035, 0.27, 0.41)
    if "Verbrennungsmotor" in t:
        return float(np.clip(0.40 + 0.035 * math.log10(max(p_mw, 0.005)), 0.28, 0.48))
    if "Brennstoffzelle" in t:
        return 0.45
    if "Stirling" in t or "Dampfmotor" in t or "ORC" in t:
        return 0.15
    if "Gegendruck" in t:
        return lin(jahr, 1965, 0.25, 0.0015, 0.25, 0.32)
    if "Kondensation" in t:
        return lin(jahr, 1965, 0.33, 0.0020, 0.33, 0.42)
    return 0.30


def betriebskosten(tech: str, brennstoff: str, p_mw: float) -> float:
    """Variable Betriebskosten ohne Brennstoff, EUR/MWh_el."""
    if brennstoff in ("lignite", "coal"):
        return 4.5
    if brennstoff in ("waste", "byproduct"):
        return 3.0
    t = tech or ""
    if "Verbrennungsmotor" in t or "Stirling" in t or "Brennstoffzelle" in t:
        return 8.0 if p_mw < 1 else 6.0
    if "ohne Abhitzekessel" in t:
        return 5.0
    return 3.0


def brennstoff(row: Any) -> str | None:
    et, hb = row.Energietraeger, str(row.Hauptbrennstoff)
    if et == "Braunkohle":
        return "lignite"
    if et == "Steinkohle":
        return "coal"
    if et in ("Erdgas", "Wasserstoff"):
        return "gas" if row.P >= 8 else "gas_small"
    if et == "Mineralölprodukte":
        return "oil_heavy" if "schwer" in hb else "oil_light"
    if et == "nicht biogener Abfall":
        return "waste"
    if et == "andere Gase":
        return "gas_small" if "Erdgas" in hb else "byproduct"
    if et == "Wärme":
        return "heat"
    return None


# ---------------------------------------------------------------- Einheiten


def einheiten(daten: Path) -> tuple[pd.DataFrame, dict[str, float]]:
    """Marktaktive Verbrennungseinheiten mit Brennstoff, Baujahr und Wärme-Strom-Verhältnis r."""
    u = pd.read_csv(daten / "mastr_verbrennung.csv", sep=";", low_memory=False)
    k = pd.read_csv(daten / "mastr_kwk.csv", sep=";", low_memory=False)
    u["P"] = pd.to_numeric(u.Nettonennleistung, errors="coerce") / 1000
    u = u[u.P > 0]
    notstrom = u.Notstromaggregat == 1
    netzres = ~notstrom & (u.NetzreserveZugeordnet == 1)
    kapres = ~notstrom & ~netzres & (u.KapazitaetsreserveZugeordnet == 1)
    raus = {
        "Notstrom": u[notstrom].P.sum(),
        "Netzreserve": u[netzres].P.sum(),
        "Kapazitätsreserve": u[kapres].P.sum(),
    }
    m = u[~notstrom & ~netzres & ~kapres].copy()
    raus["vorübergehend stillgelegt"] = m[m.EinheitBetriebsstatus != "In Betrieb"].P.sum()
    m = m[m.EinheitBetriebsstatus == "In Betrieb"].copy()
    m["fuel"] = m.apply(brennstoff, axis=1)
    m = m[m.fuel.notna()]
    m["year"] = pd.to_datetime(m.Inbetriebnahmedatum, errors="coerce").dt.year
    m["year"] = m.year.fillna(m.groupby("fuel").year.transform("median")).fillna(2000).astype(int)

    # KWK: Wärmeleistung anteilig nach elektrischer Leistung auf die verknüpften Einheiten
    k["Q"] = pd.to_numeric(k.ThermischeNutzleistung, errors="coerce") / 1000
    k = k[k.AnlageBetriebsstatus == "In Betrieb"]
    m = m.merge(
        k[["KwkMastrNummer", "Q"]], left_on="KwkMaStRNummer", right_on="KwkMastrNummer", how="left"
    )
    m["P_kwk_sum"] = m.groupby("KwkMaStRNummer").P.transform("sum")
    m["Q_unit"] = (m.Q * m.P / m.P_kwk_sum).fillna(0)
    m["r"] = (m.Q_unit / m.P).clip(0, 3)
    return m, raus


def bloecke(row: Any) -> list[tuple[float, dict[str, float], str, float]]:
    """[(Leistung, {Jahreszeit: Grenzkosten}, Typ, Wirkungsgrad)]; Entnahme-KWK in zwei Blöcken."""
    f, t, p = row.fuel, row.Technologie if isinstance(row.Technologie, str) else "", row.P
    klein = p < 8
    co2 = CO2_BEHG if (klein and f in ("gas_small", "oil_light")) else CO2_ETS
    fc = FUEL[f] + EF[f] * co2
    eta = eta_el(t, f, row.year, p)
    if f == "heat":  # Dampfteil einer GuD oder fremde Dampfquelle: Kosten wie mittlere GuD
        fc, eta = FUEL["gas"] + EF["gas"] * CO2_ETS, 0.52
    v = betriebskosten(t, f, p)
    kond = fc / eta + v
    r = row.r if f != "heat" else 0.0
    if r <= 0.02:
        return [(p, dict.fromkeys(JAHRESZEITEN, kond), "kond", eta)]
    if any(x in t for x in FESTE_KOPPLUNG) or ("Abhitzekessel" in t and p < 50 and "ohne" not in t):
        eta_ges = (
            0.85 if ("motor" in t.lower() or "Abhitzekessel" in t or "Brennstoff" in t) else 0.80
        )
        brennstoff_je_el = max((1 + r) / eta_ges, 1 / max(eta, 0.2))
        c = {s: brennstoff_je_el * fc + v - r * WAERMEWERT * a for s, a in JAHRESZEITEN.items()}
        # ohne Wärmeabsatz läuft eine Gegendruckanlage über den Notkühler: Kosten wie Kondensation
        c = {s: min(w, kond) if JAHRESZEITEN[s] < 0.3 else w for s, w in c.items()}
        return [(p, {s: max(w, KWK_UNTERGRENZE) for s, w in c.items()}, "kwk_fest", eta)]
    # Entnahme-Kondensation: wärmegebundener Teil und freier Kondensationsteil
    beta = 0.15  # Stromverlust je MWh entnommener Wärme
    q = min(row.Q_unit, 3 * p)
    pb = min(0.5 * p, 0.6 * q)
    c_b = {
        s: max(((pb + beta * q) / eta * fc - q * WAERMEWERT * a) / pb + v, KWK_UNTERGRENZE)
        for s, a in JAHRESZEITEN.items()
    }
    return [
        (pb, c_b, "kwk_entnahme", eta),
        (p - pb, dict.fromkeys(JAHRESZEITEN, kond), "kond", eta),
    ]


def bloecke_alle(m: pd.DataFrame) -> pd.DataFrame:
    zeilen = []
    for row in m.itertuples():
        name = row.NameKraftwerk if isinstance(row.NameKraftwerk, str) else ""
        if isinstance(row.NameKraftwerksblock, str) and row.NameKraftwerksblock != name:
            name = (name + " " + row.NameKraftwerksblock).strip()
        for p, c, typ, eta in bloecke(row):
            zeilen.append(
                dict(
                    name=name,
                    group=GRUPPE[row.fuel],
                    fuel=row.fuel,
                    tech=row.Technologie,
                    year=int(row.year),
                    P=p * VERFUEGBAR,
                    P_inst=p,
                    eta=eta,
                    typ=typ,
                    big=row.P >= 10,
                    **{f"c_{s}": c[s] for s in JAHRESZEITEN},
                )
            )
    return pd.DataFrame(zeilen)


# ---------------------------------------------------------------- Stunden, Treppe, Abgleich


def stunden(daten: Path) -> pd.DataFrame:
    """Die letzten 30 vollständigen Tage: Last, Wind+PV, Biomasse+Wasser, Preis."""
    h = pd.read_csv(daten / "energy_hourly_400d.csv", sep=";", parse_dates=["ts"])
    p = h.pivot_table(index="ts", columns="series", values="value")
    voll = p[["load_actual", "da_price_de_lu", "gen_wind_onshore", "gen_solar"]].notna().all(axis=1)
    ende = p.index[voll].max()
    w = p[(p.index > ende - pd.Timedelta(days=30)) & (p.index <= ende)].copy()

    def spalte(c: str) -> pd.Series:
        return w[c].fillna(0) if c in w else pd.Series(0.0, index=w.index)

    w["ee"] = spalte("gen_wind_onshore") + spalte("gen_wind_offshore") + spalte("gen_solar")
    w["bio_hydro"] = spalte("gen_biomass") + spalte("gen_hydro") + spalte("gen_other_renewable")
    return w


def pumpspeicher(daten: Path) -> tuple[float, str]:
    ps = pd.read_csv(daten / "mastr_speicher.csv", sep=";", low_memory=False)
    ps = ps[ps.EinheitBetriebsstatus == "In Betrieb"]
    mw = pd.to_numeric(ps.Nettonennleistung, errors="coerce").sum() / 1000
    return float(mw), f"MaStR ({len(ps)} Einheiten in Betrieb)"


def treppe(b: pd.DataFrame, jz: str, ps_mw: float, ps_preis: float) -> pd.DataFrame:
    s = b[["P", f"c_{jz}", "group", "name", "big", "eta", "year", "fuel", "typ", "P_inst"]]
    s = s.rename(columns={f"c_{jz}": "c"})
    ps = pd.DataFrame(
        [
            dict(
                P=ps_mw * 0.9,
                c=ps_preis,
                group="Pumpspeicher",
                name="Pumpspeicher",
                big=True,
                eta=0.75,
                year=0,
                fuel="ps",
                typ="ps",
                P_inst=ps_mw,
            )
        ]
    )
    s = pd.concat([s, ps]).sort_values("c").reset_index(drop=True)
    s["cum"] = s.P.cumsum()
    return s


def modellpreis(st: pd.DataFrame, bedarf: np.ndarray) -> np.ndarray:
    """Grenzkosten der Einheit, bei der die kumulierte Leistung den Bedarf erreicht."""
    i = np.searchsorted(st.cum.values, bedarf)
    preis = np.where(i >= len(st), 500.0, st.c.values[np.minimum(i, len(st) - 1)])
    return np.where(bedarf <= 0, 0.0, preis)


def abgleich(st: pd.DataFrame, w: pd.DataFrame) -> tuple[pd.DataFrame, float, pd.DataFrame]:
    bedarf = (w.load_actual - w.ee - w.bio_hydro).values
    ist = w.da_price_de_lu.values
    mae = np.array([np.nanmean(np.abs(modellpreis(st, bedarf + z) - ist)) for z in ZUSATZ])
    zusatz = float(ZUSATZ[int(np.argmin(mae))])
    w = w.assign(model0=modellpreis(st, bedarf), model_kal=modellpreis(st, bedarf + zusatz))
    kurve = pd.DataFrame({"zusatz_mw": ZUSATZ, "mae": mae.round(2)})
    return w, zusatz, kurve


def kennzahlen(w: pd.DataFrame, zusatz: float) -> pd.DataFrame:
    zeilen = []
    for name, col in (("ohne Kalibrierung", "model0"), (f"+{zusatz / 1000:.2f} GW", "model_kal")):
        d = w[col] - w.da_price_de_lu
        zeilen.append(
            dict(
                variante=name,
                mae=round(d.abs().mean(), 1),
                bias=round(d.mean(), 1),
                rmse=round(float(np.sqrt((d**2).mean())), 1),
                korrelation=round(float(np.corrcoef(w.da_price_de_lu, w[col])[0, 1]), 3),
                n=len(d),
            )
        )
    return pd.DataFrame(zeilen)


# ---------------------------------------------------------------- Grafiken


def grafik_treppe(st: pd.DataFrame, h: pd.Series, zusatz: float, titel: str, ziel: Path) -> None:
    teile = [("Wind + PV", 0.0, h.ee), ("Biomasse, Wasser", 0.0, h.bio_hydro)]
    teile += list(zip(st.group, st.c, st.P, strict=True))
    fig, ax = plt.subplots(figsize=(11, 5.2))
    x = 0.0
    for g, c, p in teile:
        # Blöcke mit Grenzkosten um null als schmales Band, sonst wären sie unsichtbar
        unten, hoehe = (-6.0, 12.0) if abs(c) < 6 else (0.0, c)
        ax.bar(
            x / 1000, hoehe, bottom=unten, width=p / 1000, align="edge", color=FARBE[g], linewidth=0
        )
        x += p
    bedarf = h.load_actual + zusatz
    ax.axvline(h.load_actual / 1000, color="0.1", lw=1.2, label="Last")
    ax.axvline(bedarf / 1000, color="0.1", lw=1.2, ls="--", label=f"Last + {zusatz / 1000:.1f} GW")
    ax.axhline(h.da_price_de_lu, color="#b4321f", lw=1.2, label="Börsenpreis")
    ax.set_xlim(0, x / 1000 * 1.01)
    ax.set_ylim(-50, 450)
    ax.set_xlabel("verfügbare Leistung (GW)")
    ax.set_ylabel("Grenzkosten (€/MWh)")
    ax.set_title(titel)
    hand = [plt.Rectangle((0, 0), 1, 1, color=c) for c in FARBE.values()]
    leg1 = ax.legend(hand, FARBE.keys(), loc="upper left", fontsize=8, ncol=2, frameon=False)
    ax.add_artist(leg1)
    ax.legend(loc="upper center", fontsize=8, frameon=False)
    ax.grid(axis="y", color="0.9")
    fig.tight_layout()
    fig.savefig(ziel, dpi=150)
    plt.close(fig)


def grafik_jahreszeiten(treppen: dict[str, pd.DataFrame], ziel: Path) -> None:
    fig, ax = plt.subplots(figsize=(10, 4.8))
    for jz, farbe in (("sommer", "#eda100"), ("jetzt", "#2a78d6"), ("winter", "#6250d6")):
        st = treppen[jz]
        x = np.concatenate([[0], st.cum.values]) / 1000
        y = np.concatenate([st.c.values[:1], st.c.values])
        ax.step(x, y, where="pre", color=farbe, lw=1.5, label=JZ_NAME[jz])
    ax.set_ylim(-50, 450)
    ax.set_xlabel("verfügbare thermische Leistung und Pumpspeicher (GW)")
    ax.set_ylabel("Grenzkosten (€/MWh)")
    ax.set_title("Wärmeabsatz der KWK verschiebt die Treppe (ohne Wind, PV, Biomasse, Wasser)")
    ax.legend(frameon=False)
    ax.grid(color="0.9")
    fig.tight_layout()
    fig.savefig(ziel, dpi=150)
    plt.close(fig)


def grafik_wirkungsgrade(b: pd.DataFrame, ziel: Path) -> None:
    e = b[
        b.big
        & (b.typ != "kwk_entnahme")
        & b.fuel.isin(["lignite", "coal", "gas", "oil_light", "oil_heavy"])
    ]
    e = (
        e.groupby(["name", "fuel", "tech", "year"], dropna=False)
        .agg(P=("P_inst", "sum"), eta=("eta", "first"))
        .reset_index()
    )
    fig, ax = plt.subplots(figsize=(10, 4.8))
    for f, g, farbe in (
        ("lignite", "Braunkohle", FARBE["Braunkohle"]),
        ("coal", "Steinkohle", FARBE["Steinkohle"]),
        ("gas", "Erdgas", FARBE["Erdgas"]),
        ("oil", "Öl", FARBE["Öl"]),
    ):
        d = e[e.fuel.str.startswith(f)]
        ax.scatter(
            d.year, d.eta * 100, s=np.sqrt(d.P) * 2, color=farbe, alpha=0.6, label=g, linewidths=0
        )
    ax.set_xlabel("Inbetriebnahme")
    ax.set_ylabel("elektrischer Wirkungsgrad (%)")
    ax.set_title("Geschätzte Wirkungsgrade der Einheiten ab 10 MW (Fläche ~ Leistung)")
    ax.legend(frameon=False, ncol=4)
    ax.grid(color="0.9")
    fig.tight_layout()
    fig.savefig(ziel, dpi=150)
    plt.close(fig)


def grafik_abgleich(w: pd.DataFrame, zusatz: float, ziel: Path) -> None:
    fig, ax = plt.subplots(figsize=(11, 4.2))
    t = w.index.tz_convert("Europe/Berlin")
    ax.plot(t, w.da_price_de_lu, color="#b4321f", lw=1, label="Börsenpreis DE-LU")
    ax.plot(t, w.model_kal, color="#1c5cab", lw=1, label=f"Modell, Last + {zusatz / 1000:.1f} GW")
    ax.plot(t, w.model0, color="#1c5cab", lw=0.8, ls=":", label="Modell ohne Kalibrierung")
    ax.set_ylabel("€/MWh")
    ax.set_title("Modellpreis gegen Börsenpreis, letzte 30 Tage (Brennstoffpreise vom Stichtag)")
    ax.legend(frameon=False, ncol=3, fontsize=8)
    ax.grid(color="0.9")
    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig(ziel, dpi=150)
    plt.close(fig)


# ---------------------------------------------------------------- interaktive Seite


def html_daten(
    b: pd.DataFrame,
    treppen: dict[str, pd.DataFrame],
    w: pd.DataFrame,
    raus: dict[str, float],
    m: pd.DataFrame,
    daten: Path,
    zusatz: float,
    k: pd.DataFrame,
    ps: tuple[float, str, float],
) -> dict[str, Any]:
    """Datenblock für interaktiv.template.html (Platzhalter ``__DATA__``)."""

    def buendeln(st: pd.DataFrame) -> list[list[Any]]:
        gross = st[st.big]
        klein = st[~st.big].copy()
        klein["cb"] = (klein.c / 2).round() * 2
        sm = (
            klein.groupby(["group", "cb"])
            .agg(P=("P", "sum"), n=("P", "size"), eta=("eta", "mean"))
            .reset_index()
        )
        sm["name"] = sm.n.astype(str) + " kleine Einheiten"
        out = pd.concat(
            [
                gross[["group", "c", "P", "name", "eta", "year"]],
                sm.rename(columns={"cb": "c"})[["group", "c", "P", "name", "eta"]],
            ]
        ).sort_values("c")
        return [
            [
                r.group,
                round(r.c, 1),
                round(r.P, 1),
                r.name,
                round(r.eta, 3),
                int(r.year) if not pd.isna(r.year) and r.year else None,
            ]
            for r in out.itertuples()
        ]

    e = b[
        b.big
        & (b.typ != "kwk_entnahme")
        & b.fuel.isin(["lignite", "coal", "gas", "oil_light", "oil_heavy"])
    ]
    e = (
        e.groupby(["name", "fuel", "tech", "year"], dropna=False)
        .agg(P=("P_inst", "sum"), eta=("eta", "first"))
        .reset_index()
    )
    cap = pd.read_csv(daten / "capacity_de.csv", sep=";")

    def get(r: pd.Series, c: str) -> int:
        return round(r.get(c, 0) or 0)

    return dict(
        asof=str(w.index.max()),
        stand_mastr="2026-10-20 (Release 26.2)",
        assumptions=dict(
            FX=FX,
            CO2_ETS=CO2_ETS,
            CO2_BEHG=CO2_BEHG,
            TTF=TTF,
            API2_USD=API2_USD,
            ULSD=ULSD_USD_GAL,
            BRENT=BRENT_USD,
            fuel={a: round(v, 1) for a, v in FUEL.items()},
            ef=EF,
            heat_value=round(WAERMEWERT, 1),
            seasons=JAHRESZEITEN,
            avail=VERFUEGBAR,
            ps_mw=round(ps[0]),
            ps_src=ps[1],
            ps_price=round(ps[2], 1),
            import_offset=-zusatz,
            mae0=float(k.mae.iloc[0]),
            mae_best=float(k.mae.iloc[1]),
        ),
        excluded={a: round(v) for a, v in raus.items()},
        stacks={jz: buendeln(st) for jz, st in treppen.items()},
        hours=[
            [
                t.strftime("%Y-%m-%dT%H:%MZ"),
                round(r.load_actual),
                round(r.ee),
                round(r.bio_hydro),
                round(r.da_price_de_lu, 1),
                round(r.model0, 1),
                round(r.model_kal, 1),
                get(r, "gen_lignite"),
                get(r, "gen_hard_coal"),
                get(r, "gen_gas"),
            ]
            for t, r in w.iterrows()
        ],
        eff=[
            [r.fuel, r.tech, int(r.year), round(r.P), round(r.eta, 3), r.name]
            for r in e.itertuples()
        ],
        capacity_inst=b.groupby("group").P_inst.sum().round().astype(int).to_dict(),
        ee_inst={r.technology: r.capacity_mw_de for r in cap.itertuples()},
        n_units=len(m),
        n_kwk=int((m.r > 0.02).sum()),
    )


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--daten", type=Path, required=True, help="Ordner mit den exportierten CSVs")
    ap.add_argument("--html", type=Path, help="zusätzlich die interaktive Seite schreiben")
    args = ap.parse_args()

    m, raus = einheiten(args.daten)
    b = bloecke_alle(m)
    w = stunden(args.daten)
    ps_mw, ps_quelle = pumpspeicher(args.daten)
    ps_preis = float(w.da_price_de_lu.quantile(0.8))  # Opportunitätskosten ~ obere Preise
    treppen = {jz: treppe(b, jz, ps_mw, ps_preis) for jz in JAHRESZEITEN}
    w, zusatz, kurve = abgleich(treppen["jetzt"], w)
    k = kennzahlen(w, zusatz)

    tab, gra = HIER / "tabellen", HIER / "grafiken"
    tab.mkdir(exist_ok=True)
    gra.mkdir(exist_ok=True)
    k.to_csv(tab / "kennzahlen.csv", index=False)
    kurve.to_csv(tab / "kalibrierung.csv", index=False)
    pd.DataFrame(
        [
            dict(
                beispiel=n,
                eta=eta,
                brennstoff=round(FUEL[f] / eta, 1),
                co2=round(EF[f] * CO2_ETS / eta, 1),
                grenzkosten=round((FUEL[f] + EF[f] * CO2_ETS) / eta + v, 1),
            )
            for n, f, eta, v in BEISPIELE
        ]
    ).to_csv(tab / "beispielanlagen.csv", index=False)
    pd.DataFrame(
        [dict(brennstoff=a, eur_mwh_th=round(v, 1), t_co2_mwh_th=EF[a]) for a, v in FUEL.items()]
    ).to_csv(tab / "brennstoffe.csv", index=False)
    flotte = (
        b.groupby(["group", "typ"]).agg(mw_netto=("P_inst", "sum"), bloecke=("P", "size")).round()
    )
    flotte.to_csv(tab / "kraftwerkspark.csv")
    pd.Series(raus).round().rename("mw_netto").to_csv(
        tab / "ausgeschlossen.csv", index_label="grund"
    )
    for jz, st in treppen.items():
        st[["group", "c", "P", "cum"]].assign(
            c=lambda d: d.c.round(1), P=lambda d: d.P.round(1), cum=lambda d: d.cum.round()
        ).to_csv(
            tab / f"treppe_{jz}.csv.gz", index=False, compression={"method": "gzip", "mtime": 0}
        )

    # Beispielstunde: die Stunde mit dem Median-Bedarf an steuerbarer Leistung am Abend
    abend = w[w.index.tz_convert("Europe/Berlin").hour == 18]
    bedarf = abend.load_actual - abend.ee - abend.bio_hydro
    t = (bedarf - bedarf.median()).abs().idxmin()
    zeit = t.tz_convert("Europe/Berlin").strftime("%d.%m.%Y %H Uhr")
    grafik_treppe(
        treppen["jetzt"],
        w.loc[t],
        zusatz,
        f"Merit-Order {zeit} (Wärmeabsatz Ende Oktober)",
        gra / "merit_order_stunde.png",
    )
    grafik_jahreszeiten(treppen, gra / "merit_order_jahreszeiten.png")
    grafik_wirkungsgrade(b, gra / "wirkungsgrade.png")
    grafik_abgleich(w, zusatz, gra / "abgleich.png")

    if args.html:
        daten = html_daten(
            b, treppen, w, raus, m, args.daten, zusatz, k, (ps_mw, ps_quelle, ps_preis)
        )
        vorlage = (HIER / "interaktiv.template.html").read_text(encoding="utf-8")
        args.html.write_text(
            vorlage.replace("__DATA__", json.dumps(daten, ensure_ascii=False)), encoding="utf-8"
        )

    print(k.to_string(index=False))
    print(
        f"Beispielstunde {zeit}, Zusatznachfrage {zusatz:.0f} MW, "
        f"Wärmewert {WAERMEWERT:.1f} €/MWh_th"
    )
    print(pd.Series(raus).round())
    print(flotte)


if __name__ == "__main__":
    main()
