"""Energy Sharing (§42c EnWG): Ü20- und Bestandsanlagen, mobilisierbare Speicher.

Ergänzung zur Speicher-Analyse. Rechnet stündlich mit Preis und PV-Form von SMARD (Kalenderjahr
2025), dem dynamisierten H0-Profil aus ``../pv-speicher-szenario-2027/daten`` und den
PV-Jahrgängen aus dem MaStR (``mastr_pv_jahrgaenge.py``). Die Preiswirkung zusätzlicher Speicher
liest das Skript aus ``tabellen/simulation.csv`` und ``tabellen/zeitachse.csv`` der Hauptanalyse.
Aufruf::

    uv run --no-project --with pandas --with numpy --with matplotlib \\
        python esc.py --daten ./speicher-daten
"""

import argparse
import json
from pathlib import Path
from typing import Any

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

HIER = Path(__file__).parent
TAB = HIER / "tabellen"
GRAF = HIER / "grafiken"
H0 = HIER.parent / "pv-speicher-szenario-2027" / "daten" / "h0_dyn_2025.csv"
TZ = "Europe/Berlin"

# Preisniveau 2027 (Base, EUR/MWh), siehe README. 2025 = Kalenderjahresmittel SMARD.
NIVEAU = {"2025": None, "2027 STEO-Richtung": 116.0, "2027 Niveau Aug-Okt 2026": 145.0}
# Beschaffung und Vertrieb im Haushaltstarif 2026 (BDEW Strompreisanalyse 08/2026), netto ct/kWh
BESCHAFFUNG_2026 = 15.2
HAUSHALT_2026 = 37.0  # ct/kWh brutto, Mittel 3 500 kWh
MWST = 1.19

ESC_PREIS = [8.0, 9.0, 11.0]  # ct/kWh, Erzeugerpreis in der Gemeinschaft (11 ct: Spielraum)
KOSTEN = [0.5, 1.0]  # ct/kWh Direktvermarktung und Abwicklung
ABZUG_23B = 0.7  # ct/kWh Vermarktungskostenabzug §23b (2025: 0,715 ct)
MESSUNG_EUR = 100.0  # EUR/a intelligentes Messsystem an der Anlage, Spanne 50-130

ETA = 0.9  # Rundlaufwirkungsgrad Heimspeicher
ERTRAG_UE20 = 850.0  # kWh/kWp, alte Module
ERTRAG_NEU = 950.0
HAUSHALT_KWH = 3500.0
MITGLIEDER = [0, 1, 2, 5, 10, 20, 50]

# Bestandsanlage: 10 kWp mit 10 kWh, Einspeisevergütung nach Jahrgang (ct/kWh, <= 10 kWp)
EEG_SAETZE = {
    "Jan 2020": 9.87,
    "Jan 2021": 8.16,
    "Jul 2021": 7.47,
    "Jan 2022": 6.83,
    "Jul 2022": 6.24,
    "EEG 2023 Aug 2022": 8.20,
    "Feb 2026": 7.78,
}

# Mobilisierung
UE20_KLASSEN = ["0-10", "10-30", "30-100"]
NACHRUESTQUOTE = [0.1, 0.25, 0.5]
KWH_JE_KWP = 1.0
KW_JE_KWP = 0.5
MARKTFAKTOR = {"lastfolgend in der Gemeinschaft": 0.3, "preisgeführt über Direktvermarkter": 1.0}
WIRKSAM = 0.7


def lade_smard(ordner: Path, jahr: int) -> pd.DataFrame:
    reihen = {}
    for name in ("price", "solar"):
        d = json.loads((ordner / f"{name}.json").read_text())
        reihen[name] = pd.Series({int(k): v for k, v in d.items()}, dtype=float)
    df = pd.DataFrame(reihen)
    df.index = pd.to_datetime(df.index, unit="ms", utc=True).tz_convert(TZ)
    df = df.sort_index()
    return df.loc[str(jahr)].interpolate(limit=3).fillna(0.0)


def lade_h0(index: pd.DatetimeIndex) -> np.ndarray:
    h0 = pd.read_csv(H0)
    # Lokalzeit ohne Zeitzone; über Monat, Tag und Stunde zuordnen (Zeitumstellung: Lücke/Doppel)
    lt = pd.to_datetime(h0["lokalzeit"])
    s = pd.Series(h0["h0_kwh"].to_numpy(), index=lt.dt.strftime("%m-%d %H")).groupby(level=0).mean()
    schluessel = index.strftime("%m-%d %H")
    werte = s.reindex(schluessel).interpolate().to_numpy()
    return np.asarray(werte / werte.sum())


def preise(df: pd.DataFrame, niveau: float | None) -> np.ndarray:
    """ct/kWh je Stunde. Positive Preise skaliert auf das Niveau, negative bleiben."""
    p = df["price"].to_numpy() / 10.0
    if niveau is None:
        return np.asarray(p)
    k = niveau / 10.0 / p.mean()
    return np.asarray(np.where(p > 0, p * k, p))


def fahren(
    pv: np.ndarray,
    eigen: np.ndarray,
    gemeinschaft: np.ndarray,
    kap: float,
    leistung: float,
    netzwert: np.ndarray | None = None,
    teilwert: float = 0.0,
) -> dict[str, np.ndarray]:
    """Stündlich: Eigenverbrauch, Speicher für den eigenen Bedarf laden, teilen, Speicher für die
    Gemeinschaft laden, Rest ins Netz.

    Der eigene Bedarf ist der Verbrauch der nächsten 12 Stunden (perfekte Voraussicht). Für ihn
    lädt der Speicher zuerst, weil Eigenverbrauch den vollen Haushaltspreis spart. Für die
    Gemeinschaft lädt er nur, wenn der Netzerlös der Stunde (``netzwert``, ct/kWh) unter dem
    Erlös fürs spätere Teilen nach Speicherverlust liegt (``teilwert`` x Wirkungsgrad). An die
    Gemeinschaft gibt er nur ab, was über dem eigenen Bedarf liegt.
    """
    w = np.sqrt(ETA)
    soc = 0.0
    n = len(pv)
    reserve = pd.Series(eigen[::-1]).rolling(12, min_periods=1).sum().to_numpy()[::-1]
    out = {
        k: np.zeros(n) for k in ("eigen", "geteilt", "rest", "laden", "entladen", "aus_speicher")
    }
    for t in range(n):
        g, lo, lc = pv[t], eigen[t], gemeinschaft[t]
        e = min(g, lo)
        g -= e
        lo -= e
        bedarf = reserve[t + 1] if t + 1 < n else 0.0
        lade = min(g, leistung, (kap - soc) / w, max(0.0, bedarf - soc * w) / w) if kap else 0.0
        soc += lade * w
        g -= lade
        sh = min(g, lc)
        g -= sh
        lc -= sh
        if kap and netzwert is not None and netzwert[t] < teilwert * ETA:
            lade2 = min(g, leistung - lade, (kap - soc) / w)
            soc += lade2 * w
            g -= lade2
            lade += lade2
        ent = min(leistung, soc * w) if kap else 0.0
        d_eigen = min(lo, ent)
        frei = soc * w - d_eigen - bedarf
        d_sh = max(0.0, min(lc, ent - d_eigen, frei))
        soc -= (d_eigen + d_sh) / w
        out["eigen"][t] = e + d_eigen
        out["geteilt"][t] = sh + d_sh
        out["rest"][t] = g
        out["laden"][t] = lade
        out["entladen"][t] = d_eigen + d_sh
        out["aus_speicher"][t] = d_sh
    return out


def haushaltspreis(niveau: float | None, h0_base_2025: float, h0_base: float) -> float:
    """Arbeitspreis brutto 2027: BDEW 2026 plus Änderung der H0-gewichteten Beschaffung."""
    if niveau is None:
        return HAUSHALT_2026
    return HAUSHALT_2026 + (h0_base - h0_base_2025) * MWST


# Ü20-Fälle: Name, kWp, eigener Haushalt am Anlagenzähler, Messkosten EUR/a im ESC-Fall
UE20_FAELLE = [
    ("5 kWp mit Haushalt", 5.0, True, MESSUNG_EUR),
    ("5 kWp mit Haushalt, iMSys vorhanden", 5.0, True, 0.0),
    ("5 kWp Volleinspeisung", 5.0, False, MESSUNG_EUR),
    ("30 kWp Volleinspeisung", 30.0, False, MESSUNG_EUR),
]


def ue20(df: pd.DataFrame, h0: np.ndarray) -> pd.DataFrame:
    """Ü20-Anlagen ohne/mit Speicher (1 kWh und 0,5 kW je kWp), ohne ESC (§23b) und mit ESC."""
    form = df["solar"].to_numpy() / df["solar"].sum()
    p25 = preise(df, None)
    h0_25 = float((p25 * h0).sum())
    zeilen = []
    for fall, kwp, haushalt, messung in UE20_FAELLE:
        pv = form * kwp * ERTRAG_UE20
        eigen = h0 * HAUSHALT_KWH if haushalt else np.zeros_like(pv)
        for name, niveau in NIVEAU.items():
            p = preise(df, niveau)
            h0_base = float((p * h0).sum())
            ap = haushaltspreis(niveau, h0_25, h0_base)
            jmw = float((p * form).sum())
            for n in MITGLIEDER:
                gem = h0 * HAUSHALT_KWH * n
                for kap in (0.0, kwp * KWH_JE_KWP):
                    for preis in ESC_PREIS if n else [None]:
                        for kosten in KOSTEN if n else [None]:
                            if n:
                                assert preis is not None and kosten is not None
                                f = fahren(
                                    pv,
                                    eigen,
                                    gem,
                                    kap,
                                    kwp * KW_JE_KWP,
                                    np.maximum(p, 0) - kosten,
                                    preis - kosten,
                                )
                            else:
                                f = fahren(pv, eigen, gem, kap, kwp * KW_JE_KWP)
                            eigen_eur = f["eigen"].sum() * ap / 100
                            if n:
                                assert preis is not None and kosten is not None
                                teil_eur = f["geteilt"].sum() * (preis - kosten) / 100
                                rest_eur = (f["rest"] * (np.maximum(p, 0) - kosten)).sum() / 100
                                mess = messung
                            else:
                                teil_eur = 0.0
                                rest_eur = f["rest"].sum() * (min(jmw, 10.0) - ABZUG_23B) / 100
                                mess = 0.0
                            geteilt = f["geteilt"].sum()
                            zeilen.append(
                                dict(
                                    fall=fall,
                                    preisniveau=name,
                                    mitglieder=n,
                                    speicher_kwh=kap,
                                    esc_preis=preis,
                                    kosten=kosten,
                                    pv_kwh=pv.sum(),
                                    eigen_kwh=f["eigen"].sum(),
                                    geteilt_kwh=geteilt,
                                    geteilt_aus_speicher_kwh=f["aus_speicher"].sum(),
                                    rest_kwh=f["rest"].sum(),
                                    jmw_solar=jmw,
                                    arbeitspreis=ap,
                                    spotwert_geteilt=(
                                        float((f["geteilt"] * p).sum() / geteilt)
                                        if geteilt
                                        else np.nan
                                    ),
                                    messung_eur=mess,
                                    erloes_eur=eigen_eur + teil_eur + rest_eur - mess,
                                )
                            )
    t = pd.DataFrame(zeilen)
    basis = t[(t["mitglieder"] == 0) & (t["speicher_kwh"] == 0)].set_index(["fall", "preisniveau"])
    t["mehr_als_heute_eur"] = t["erloes_eur"] - pd.Series(
        basis["erloes_eur"]
        .reindex(pd.MultiIndex.from_frame(t[["fall", "preisniveau"]]))
        .to_numpy(),
        index=t.index,
    )
    return t


def speicherwert(t: pd.DataFrame) -> pd.DataFrame:
    """Was der Speicher mit und ohne ESC zusätzlich bringt (EUR/a und EUR/kWh über 15 Jahre)."""
    schl = ["fall", "preisniveau", "mitglieder", "esc_preis", "kosten"]
    t = t.fillna({"esc_preis": 0, "kosten": 0})
    a = t[t["speicher_kwh"] == 0].set_index(schl)["erloes_eur"]
    s = t[t["speicher_kwh"] > 0].set_index(schl)
    out = (s["erloes_eur"] - a).rename("speicher_mehr_eur").reset_index()
    out["speicher_kwh"] = s["speicher_kwh"].to_numpy()
    out["darf_kosten_eur_kwh"] = out["speicher_mehr_eur"] * 15 / out["speicher_kwh"]
    return out


def kurz(t: pd.DataFrame, sw: pd.DataFrame) -> pd.DataFrame:
    """Übersicht 2027 (STEO-Richtung): Mehrerlös gegenüber §23b ohne Speicher, je kWh Erzeugung."""
    t = t[(t["preisniveau"] == "2027 STEO-Richtung") & (t["mitglieder"].isin([0, 2, 10, 50]))]
    t = t.fillna({"esc_preis": 0, "kosten": 0}).assign(speicher=t["speicher_kwh"] > 0)
    zeilen = t.pivot_table(
        index=["fall", "esc_preis", "kosten"],
        columns=["mitglieder", "speicher"],
        values="mehr_als_heute_eur",
    )
    zeilen.columns = [
        f"n{n}_{'mit' if k else 'ohne'}_speicher" for n, k in zeilen.columns.to_flat_index()
    ]
    sw = sw[(sw["preisniveau"] == "2027 STEO-Richtung") & (sw["mitglieder"].isin([0, 2, 10, 50]))]
    d = sw.pivot_table(
        index=["fall", "esc_preis", "kosten"], columns="mitglieder", values="darf_kosten_eur_kwh"
    )
    d.columns = [f"speicher_eur_kwh_n{n}" for n in d.columns]
    return zeilen.join(d).reset_index()


def bestand(df: pd.DataFrame, h0: np.ndarray, ap: float) -> pd.DataFrame:
    """Bestandsanlage 10 kWp, 10 kWh/5 kW: Wechsel in ESC (geteilt sonstige DV, Rest Marktprämie).

    Messkosten fallen nicht zusätzlich an: Anlagen über 7 kW bekommen ohnehin ein intelligentes
    Messsystem (Pflichteinbau).
    """
    form = df["solar"].to_numpy() / df["solar"].sum()
    pv = form * 10 * ERTRAG_NEU
    eigen = h0 * HAUSHALT_KWH
    f0 = fahren(pv, eigen, np.zeros_like(pv), 10.0, 5.0)
    einsp0 = f0["rest"].sum()
    zeilen = []
    for n in MITGLIEDER[1:]:
        for jahrgang, satz in EEG_SAETZE.items():
            for preis in ESC_PREIS:
                for kosten in KOSTEN:
                    # Netzerlös des Rests: anzulegender Wert, solange er über dem Spotpreis liegt
                    f = fahren(
                        pv,
                        eigen,
                        h0 * HAUSHALT_KWH * n,
                        10.0,
                        5.0,
                        np.full_like(pv, satz + 0.4 - kosten),
                        preis - kosten,
                    )
                    geteilt, rest = f["geteilt"].sum(), f["rest"].sum()
                    # Rest im Marktprämienmodell: anzulegender Wert = Vergütung + 0,4 ct
                    neu = geteilt * (preis - kosten) + rest * (satz + 0.4 - kosten)
                    neu += (f["eigen"].sum() - f0["eigen"].sum()) * ap
                    alt = einsp0 * satz
                    zeilen.append(
                        dict(
                            jahrgang=jahrgang,
                            verguetung=satz,
                            mitglieder=n,
                            esc_preis=preis,
                            kosten=kosten,
                            einspeisung_kwh=einsp0,
                            geteilt_kwh=geteilt,
                            geteilt_anteil=geteilt / einsp0,
                            mehr_eur=(neu - alt) / 100,
                            schwelle_verguetung=preis - kosten + rest / geteilt * (0.4 - kosten),
                        )
                    )
    return pd.DataFrame(zeilen)


def mitglied(df: pd.DataFrame, h0: np.ndarray) -> pd.DataFrame:
    """Ersparnis je geteilter kWh beim Mitglied gegenüber Beschaffung und Vertrieb im Tarif."""
    p25 = preise(df, None)
    h0_25 = float((p25 * h0).sum())
    zeilen = []
    for name, niveau in NIVEAU.items():
        h0_base = float((preise(df, niveau) * h0).sum())
        tarif = BESCHAFFUNG_2026 + (h0_base - h0_25 if niveau else 0.0)
        for preis in ESC_PREIS:
            zeilen.append(
                dict(
                    preisniveau=name,
                    h0_base=h0_base,
                    beschaffung_vertrieb=tarif,
                    esc_preis=preis,
                    ersparnis_netto=tarif - preis,
                    ersparnis_brutto=(tarif - preis) * MWST,
                    mit_stromsteuer_brutto=(tarif - preis + 2.05) * MWST,
                )
            )
    return pd.DataFrame(zeilen)


def ue20_welle(daten: Path) -> pd.DataFrame:
    """PV-Leistung, die bis Ende eines Jahres aus der EEG-Förderung gefallen ist (MW, MaStR)."""
    j = pd.DataFrame(json.loads((daten / "mastr" / "pv_jahrgaenge.json").read_text()))
    j["ende"] = j["jahr"] + 20
    zeilen = []
    for ende in range(2020, 2033):
        sel = j[j["ende"] <= ende]
        klein = sel[sel["klasse"].isin(UE20_KLASSEN)]
        zeilen.append(
            dict(
                ende_jahr=ende,
                alle_mw=sel["mw"].sum(),
                bis_100kw_mw=klein["mw"].sum(),
                bis_100kw_anzahl=int(klein["anzahl"].sum()),
                im_jahr_bis_100kw_mw=klein[klein["ende"] == ende]["mw"].sum(),
            )
        )
    return pd.DataFrame(zeilen)


def _interp(sim: pd.DataFrame, x: float, spalte: str) -> float:
    return float(np.interp(x, sim["zubau_gw"], sim[spalte]))


def mobilisierung(welle: pd.DataFrame) -> pd.DataFrame:
    """Zusätzliche Speicher bei Ü20-Nachrüstung und ihre Wirkung auf die Tagesspanne."""
    sim = pd.read_csv(TAB / "simulation.csv")
    sim = sim[
        (sim["szenario"] == "heute") & (sim["variante"] == "empirisch") & (sim["dauer_h"] == 2.0)
    ].sort_values("zubau_gw")
    za = pd.read_csv(TAB / "zeitachse.csv")
    za = za[(za["variante"] == "empirisch") & (za["pfad"] == "Zubau wie 2026")]
    za = za.drop_duplicates("jahr").set_index("jahr")
    zeilen = []
    for jahr in (2027, 2030):
        mw = float(welle.set_index("ende_jahr").loc[jahr - 1, "bis_100kw_mw"])
        x0 = float(za.loc[jahr, "zusatz_wirksam_gw"])
        for quote in NACHRUESTQUOTE:
            gw = mw / 1000 * quote * KW_JE_KWP
            for art, faktor in MARKTFAKTOR.items():
                dx = gw * faktor * WIRKSAM
                zeilen.append(
                    dict(
                        jahr=jahr,
                        ue20_bis_100kw_gw=mw / 1000,
                        nachruestquote=quote,
                        speicher_gw=gw,
                        speicher_gwh=gw * KWH_JE_KWP / KW_JE_KWP,
                        betrieb=art,
                        wirksam_gw=dx,
                        spanne_rel_pfad=_interp(sim, x0, "spanne_rel"),
                        spanne_rel_mit_esc=_interp(sim, x0 + dx, "spanne_rel"),
                        abend_diff_zusatz=_interp(sim, x0 + dx, "abend_diff")
                        - _interp(sim, x0, "abend_diff"),
                        mittag_diff_zusatz=_interp(sim, x0 + dx, "mittag_diff")
                        - _interp(sim, x0, "mittag_diff"),
                    )
                )
    t = pd.DataFrame(zeilen)
    t["spanne_zusatz_pp"] = (t["spanne_rel_mit_esc"] - t["spanne_rel_pfad"]) * 100
    return t


def grafik(t: pd.DataFrame, welle: pd.DataFrame) -> None:
    fig, ax = plt.subplots(1, 2, figsize=(12, 4.5))
    sel = t[
        (t["preisniveau"] == "2027 STEO-Richtung")
        & (t["esc_preis"].isna() | (t["esc_preis"] == 8.0))
    ]
    sel = sel[sel["kosten"].isna() | (sel["kosten"] == 1.0)]
    farben = {
        "5 kWp mit Haushalt": "#2a6fdb",
        "5 kWp Volleinspeisung": "#9aa4b1",
        "30 kWp Volleinspeisung": "#e07b39",
    }
    for fall, farbe in farben.items():
        for mit, stil in ((False, "o-"), (True, "s--")):
            s = sel[(sel["fall"] == fall) & ((sel["speicher_kwh"] > 0) == mit)].sort_values(
                "mitglieder"
            )
            s = s[s["mitglieder"] > 0]
            ax[0].plot(
                s["mitglieder"],
                s["mehr_als_heute_eur"] / s["pv_kwh"] * 100,
                stil,
                color=farbe,
                label=fall + (" + Speicher" if mit else ""),
            )
    ax[0].axhline(0, color="#444", lw=0.8)
    ax[0].set_xscale("log")
    ax[0].set_xlabel("Haushalte in der Gemeinschaft (je 3 500 kWh)")
    ax[0].set_ylabel("ct je erzeugter kWh gegenüber §23b ohne Speicher")
    ax[0].set_title("Ü20 in einer ESC 2027 (8 ct, 1 ct Kosten)")
    ax[0].legend(fontsize=8)
    ax[1].bar(welle["ende_jahr"], welle["im_jahr_bis_100kw_mw"] / 1000, color="#2a6fdb")
    ax[1].set_xlabel("Ende der EEG-Förderung (31.12.)")
    ax[1].set_ylabel("GW, Anlagen bis 100 kW")
    ax[1].set_title("Ü20-Welle aus dem MaStR-Bestand")
    for a in ax:
        a.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(GRAF / "esc.png", dpi=120)
    plt.close(fig)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--daten", type=Path, required=True)
    args = ap.parse_args()
    df = lade_smard(args.daten / "smard", 2025)
    h0 = lade_h0(pd.DatetimeIndex(df.index))
    t = ue20(df, h0)
    t.round(3).to_csv(TAB / "esc_ue20.csv", index=False)
    sw = speicherwert(t)
    sw.round(1).to_csv(TAB / "esc_speicherwert.csv", index=False)
    kurz(t, sw).round(1).to_csv(TAB / "esc_kurz.csv", index=False)
    p25 = preise(df, None)
    p27 = preise(df, NIVEAU["2027 STEO-Richtung"])
    ap27 = haushaltspreis(1.0, float((p25 * h0).sum()), float((p27 * h0).sum()))
    bestand(df, h0, ap27).round(1).to_csv(TAB / "esc_bestand.csv", index=False)
    mitglied(df, h0).round(2).to_csv(TAB / "esc_mitglied.csv", index=False)
    welle = ue20_welle(args.daten)
    welle.round(1).to_csv(TAB / "esc_ue20_welle.csv", index=False)
    mob = mobilisierung(welle)
    mob.round(3).to_csv(TAB / "esc_mobilisierung.csv", index=False)
    grafik(t, welle)
    info: dict[str, Any] = {
        "base_2025": float(df["price"].mean()),
        "h0_base_2025": float((preise(df, None) * h0).sum()),
        "jmw_solar_2025": float(
            (preise(df, None) * df["solar"].to_numpy()).sum() / df["solar"].sum()
        ),
    }
    print(json.dumps(info, indent=1))


if __name__ == "__main__":
    main()
