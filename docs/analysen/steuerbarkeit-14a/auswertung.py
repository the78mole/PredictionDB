"""Steuerbarkeit nach § 14a EnWG: Reichen die Anreize, um Lastspitzen zu senken?

Teil A (Haushalt): Was spart ein Haushalt mit Wärmepumpe oder Wallbox 2026 bei zwölf
Netzbetreibern mit Modul 1, Modul 2 und Modul 1+3, passiv und mit Verschiebung? Zum
Vergleich der Wert eines dynamischen Stromtarifs (Day-Ahead 2024-2025).

Teil B (System): Auf der Hochrechnung "Vollausbau" aus ../elektrifizierung-last (alle Pkw
elektrisch, Wärmepumpen statt fossiler Heizungen) wird ergänzt: bivalente Luft-Wärme-
pumpen mit Heizstab unterhalb des Bivalenzpunkts und die Reaktion von Haushalten auf die
Zeitfenster aus Modul 3 (HT 17-20 Uhr, NT 0-6 Uhr) bei verschiedenen Teilnahmequoten.

Aufruf aus dem Repository-Wurzelverzeichnis (nutzt denselben Cache wie
elektrifizierung-last)::

    uv run --no-project --with pandas --with matplotlib --with httpx --with holidays \\
        python docs/analysen/steuerbarkeit-14a/auswertung.py --cache /tmp/el
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

HIER = Path(__file__).resolve().parent
EL_DIR = HIER.parent / "elektrifizierung-last"


def _lade(name: str, pfad: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, pfad)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


# Reihenfolge wichtig: elektrifizierung-last importiert sein eigenes "daten"
el = _lade("el_auswertung", EL_DIR / "auswertung.py")
pb_daten = _lade("daten_14a", HIER / "daten.py")

FARBEN = el.FARBEN
UST = 1.19

# Haushalt
E_WP = 4500.0  # kWh/a, Wärmepumpe Einfamilienhaus
E_EV = 2000.0  # kWh/a, Laden zu Hause
ZAEHLER_MODUL2 = 25.0  # €/a netto, zweiter Zählpunkt für Modul 2 (Annahme)
WP_NT_ANTEIL = 0.10  # Anteil des Tagesbedarfs, den eine Wärmepumpe ins NT legen kann
WP_FLEX_ANTEIL = 0.20  # dynamischer Tarif: verschiebbarer Anteil je Tag

# System
HT_STUNDEN = (17, 18, 19)  # repräsentatives Winterfenster Modul 3
VORHEIZEN = (14, 15, 16)
NT_STUNDEN = (0, 1, 2, 3, 4, 5)
SYNCHRON = np.array([0.5, 0.3, 0.15, 0.05, 0.0, 0.0])  # Anteil Ladeenergie je NT-Stunde
VERTEILT = np.full(6, 1 / 6)
KAP_GRADIENT = 0.025  # Leistungsabfall Luft-WP je Kelvin unter dem Bivalenzpunkt
WALLBOX_KW = 11.0
DIMM_KW = 4.2


# --------------------------------------------------------------------------- Teil A


def profil_wp(st: pd.DataFrame) -> np.ndarray:
    """Anteil des Jahresverbrauchs je Quartal und Stunde (4 x 24), Mittel der Wetterjahre."""
    lok = el.lokal(st.index)
    df = pd.DataFrame({"q": lok.quarter, "h": lok.hour, "e": st.to_numpy().sum(axis=1)})
    m = df.groupby(["q", "h"])["e"].sum().unstack().to_numpy()
    return np.asarray(m / m.sum())


def profil_ev() -> np.ndarray:
    p = 5 / 7 * el._verschmiert(el.ANKUNFT_HAUSE_WT) + 2 / 7 * el._verschmiert(el.ANKUNFT_HAUSE_WE)
    q = np.array([0.26, 0.24, 0.24, 0.26])
    return np.asarray(q[:, None] * p[None, :])


def kosten_haushalt(pb: Any, prof: np.ndarray, e: float, art: str) -> dict[str, float]:
    """Netzentgelt (Arbeitspreis-Anteil) des Verbrauchers in €/a netto je Modul."""
    ohne = pb.ap * e / 100
    m1 = ohne - pb.modul1
    m2 = pb.modul2_ap * e / 100 + pb.modul2_gp + ZAEHLER_MODUL2
    passiv = aktiv = 0.0
    for q in range(4):
        preis = pb_daten.preis_stunde(pb, q + 1)
        eq = prof[q] * e
        passiv += float(eq @ preis) / 100
        nt = pb_daten.anteil_stunde(pb, q + 1, "NT")
        ht = pb_daten.anteil_stunde(pb, q + 1, "HT")
        if art == "ev":
            nacht = [h for h in (*range(18, 24), *range(0, 7))]
            aktiv += eq.sum() * min(preis[nacht]) / 100
        else:
            # HT-Bedarf wird vorgeheizt (ST), ein Teil des Tagesbedarfs ins NT gelegt
            ohne_ht = eq * (1 - ht)
            ht_e = float((eq * ht).sum())
            basis = float(ohne_ht @ preis) / 100 + ht_e * pb.st / 100
            if nt.sum() > 0:
                basis -= WP_NT_ANTEIL * eq.sum() * (pb.st - pb.nt) / 100
            aktiv += basis
    return {
        "ohne": ohne,
        "modul1": m1,
        "modul2": m2,
        "modul3_passiv": passiv - pb.modul1,
        "modul3_aktiv": aktiv - pb.modul1,
    }


def teil_a(st: pd.DataFrame) -> pd.DataFrame:
    prof = {"wp": profil_wp(st), "ev": profil_ev()}
    energie = {"wp": E_WP, "ev": E_EV}
    zeilen = []
    for pb in pb_daten.PREISBLAETTER:
        for art in ("wp", "ev"):
            k = kosten_haushalt(pb, prof[art], energie[art], art)
            erspar = {m: (k["ohne"] - v) * UST for m, v in k.items() if m != "ohne"}
            beste_12 = max(erspar["modul1"], erspar["modul2"])
            zeilen.append(
                {
                    "vnb": pb.vnb,
                    "art": art,
                    "stand": pb.stand,
                    "netzentgelt_ohne_14a": k["ohne"] * UST,
                    **{f"ersparnis_{m}": v for m, v in erspar.items()},
                    "mehrwert_modul3_aktiv": erspar["modul3_aktiv"] - beste_12,
                    "beste_wahl": (
                        "modul3_aktiv"
                        if erspar["modul3_aktiv"] - beste_12 >= 1
                        else ("modul2" if erspar["modul2"] > erspar["modul1"] else "modul1")
                    ),
                    "winterfenster": any(
                        s == "HT" and 1 in q and 4 in q for s, _, _, q in pb.fenster
                    ),
                }
            )
    return pd.DataFrame(zeilen)


def dynamischer_tarif(preis: pd.Series, prof_wp: np.ndarray) -> dict[str, float]:
    """Ersparnis Energiepreis (€/a brutto) durch Verschieben nach Day-Ahead 2024-2025."""
    p = preis / 1000 * 100  # ct/kWh
    lok = el.lokal(p.index)
    df = pd.DataFrame({"p": p.to_numpy(), "tag": lok.normalize(), "h": lok.hour, "q": lok.quarter})
    tab = df.pivot_table(index="tag", columns="h", values="p", aggfunc="mean").dropna()
    quartal = pd.DatetimeIndex(tab.index).quarter.to_numpy()
    arr = tab.to_numpy()
    # E-Auto: Ladung ab Ankunft (Profil) gegen die billigste Stunde 18-7 Uhr (Folgetag)
    pe = profil_ev().sum(axis=0)
    pe = pe / pe.sum()
    ungesteuert = arr @ pe
    nacht = np.concatenate([arr[:-1, 18:], arr[1:, :7]], axis=1).min(axis=1)
    ev_ct = float(np.mean(ungesteuert[:-1] - nacht))
    # Wärmepumpe: 20 % des Tagesbedarfs aus den 4 teuersten in die 4 billigsten Stunden
    s = np.sort(arr, axis=1)
    spanne = s[:, -4:].mean(axis=1) - s[:, :4].mean(axis=1)
    gew = prof_wp.sum(axis=1)[quartal - 1]
    wp_ct = float(np.average(spanne, weights=gew))
    return {
        "ev_ct_je_kwh": ev_ct,
        "ev_eur_a": ev_ct * E_EV / 100 * UST,
        "wp_spanne_ct": wp_ct,
        "wp_eur_a": WP_FLEX_ANTEIL * E_WP * wp_ct / 100 * UST,
    }


# --------------------------------------------------------------------------- Teil B


@dataclass
class Grundlage:
    idx: pd.DatetimeIndex
    stunde: np.ndarray
    tag_nr: np.ndarray
    quartal: np.ndarray
    th: np.ndarray
    t_boden: np.ndarray
    rw: np.ndarray  # Raumwärme GW (thermisch), alle WP-Gebäude
    ww: np.ndarray
    k: float  # GWh Raumwärme je Heizgradtag
    ev_fix: np.ndarray
    ev_hause: np.ndarray
    ev_arbeit: np.ndarray


def grundlage(
    t: pd.Series, tag: pd.DataFrame, pkw: pd.DataFrame, a: Any, hdd_ref: float
) -> Grundlage:
    """Wärme und Ladeenergie stündlich, gerechnet wie in elektrifizierung-last.szenario."""
    hw = el.daten.HANDWERTE
    idx = t.index
    lok = el.lokal(idx)
    tage = pd.DatetimeIndex(lok.date)
    stunde = lok.hour.to_numpy()
    td = tag.reindex(tage)
    th = t.to_numpy()
    t_boden = (8 + 0.25 * (t.rolling(24 * 30, min_periods=24).mean() - 9)).bfill().to_numpy()
    pj = hw["ageb_pj"]
    fossil = ("oel", "gas", "kohle")
    rw_pj = sum(
        pj[s].get(f, 0.0)
        for s in ("hh_raumwaerme", "ghd_raumwaerme", "ind_raumwaerme")
        for f in fossil
    )
    ww_pj = sum(pj[s].get(f, 0.0) for s in ("hh_warmwasser_2023", "ghd_warmwasser") for f in fossil)
    hdd_2024 = float(tag["hdd"][tag.index.year == 2024].sum())
    rw_twh = rw_pj * el.PJ_TWH * a.kesselwirkungsgrad * (1 - a.sanierung) * hdd_ref / hdd_2024
    ww_twh = ww_pj * el.PJ_TWH * a.kesselwirkungsgrad
    k = rw_twh * 1000 / hdd_ref
    rw_prof = 1 + 0.4 * (el.RW_PROFIL / el.RW_PROFIL.mean() - 1)
    ww_prof = el.WW_PROFIL / el.WW_PROFIL.mean()
    rw = k * td["hdd"].to_numpy() / 24 * rw_prof[stunde]
    ww = ww_twh * 1000 / 8760 * ww_prof[stunde]

    letzte = pkw.dropna(subset=["bestand_bev"]).iloc[-1]
    heute_el = (letzte["bestand_bev"] + 0.5 * letzte["bestand_phev"]) / letzte["bestand_gesamt"]
    ev_twh = hw["pkw_fahrleistung_km"] * a.kwh_je_100km / 100 / 1e9 * (1 - heute_el)
    tag_index = pd.DatetimeIndex(sorted(set(tage)))
    typ = el.tagtyp(tag_index)
    wochenfaktor = np.where(typ == 0, 1.05, np.where(typ == 1, 0.95, 0.80))
    winter = 1 + 0.012 * np.clip(16 - tag["t"].reindex(tag_index).to_numpy(), 0, None)
    tf = wochenfaktor * winter
    tf = tf / tf.mean()
    e_tag = pd.Series(ev_twh * 1000 / 365.25 * tf, index=tag_index).reindex(tage).to_numpy()
    we = el.tagtyp(tage) > 0
    p_hause = np.where(
        we,
        el._verschmiert(el.ANKUNFT_HAUSE_WE)[stunde],
        el._verschmiert(el.ANKUNFT_HAUSE_WT)[stunde],
    )
    p_arbeit = np.where(we, 0.0, el._verschmiert(el.ANKUNFT_ARBEIT)[stunde])
    p_oeff = (el.OEFFENTLICH / el.OEFFENTLICH.sum())[stunde]
    p_schnell = (el.SCHNELL / el.SCHNELL.sum())[stunde]
    a_rest = 1 - a.anteil_zuhause - a.anteil_arbeit - a.anteil_oeffentlich
    anteil_arbeit = np.where(we, 0.0, a.anteil_arbeit)
    anteil_hause = a.anteil_zuhause + a.anteil_arbeit - anteil_arbeit
    return Grundlage(
        idx=idx,
        stunde=stunde,
        tag_nr=(tage - tage[0]).days.to_numpy(),
        quartal=lok.quarter.to_numpy(),
        th=th,
        t_boden=t_boden,
        rw=rw,
        ww=ww,
        k=k,
        ev_fix=e_tag * (a.anteil_oeffentlich * p_oeff + a_rest * p_schnell),
        ev_hause=e_tag * anteil_hause * p_hause,
        ev_arbeit=e_tag * anteil_arbeit * p_arbeit,
    )


def waermepumpen(
    g: Grundlage,
    rw: np.ndarray,
    a: Any,
    bivalenz: tuple[float, ...] | None,
    vorheizen: bool = False,
) -> tuple[np.ndarray, np.ndarray]:
    """Strom der Wärmepumpen und der Heizstäbe (GW).

    Luft-Wärmepumpen sind so ausgelegt, dass sie beim Bivalenzpunkt (Tagesmittel) den
    Bedarf gerade decken. Darunter fällt ihre Heizleistung um 2,5 % je Kelvin, den Rest
    liefert der Heizstab (COP 1). Die Bivalenzpunkte sind gleich über die Gebäude verteilt.
    Sole-Wärmepumpen bleiben monovalent.

    Mit ``vorheizen`` (Modul 3, nur Q1/Q4) wird Raumwärme aus dem Hochtarif 17-20 Uhr
    in den Stunden 14-17 Uhr vorab gedeckt, aber nur so weit, wie der Verdichter dort
    Leistung frei hat. Mit dem Heizstab vorzuheizen lohnt sich nicht (COP 1).
    """
    win = np.isin(g.quartal, (1, 4))
    htm = win & np.isin(g.stunde, HT_STUNDEN)
    vorm = win & np.isin(g.stunde, VORHEIZEN)
    n_tage = int(g.tag_nr.max()) + 1
    th = g.th
    senke_hk = 40 - 1.0 * th
    senke_fl = 30 - 0.5 * th
    hk = a.anteil_heizkoerper
    strom = np.zeros_like(th)
    heizstab = np.zeros_like(th)
    for quelle, anteil, kurve, luft in (
        (th, a.anteil_luft, el.cop_luft, True),
        (g.t_boden, 1 - a.anteil_luft, el.cop_sole, False),
    ):
        cop_hk = kurve(np.maximum(senke_hk - quelle, 15)) * a.cop_korrektur
        cop_fl = kurve(np.maximum(senke_fl - quelle, 15)) * a.cop_korrektur
        cop_ww = kurve(np.maximum(50 - quelle, 15)) * a.cop_korrektur
        w_rw, w_ww = anteil * rw, anteil * g.ww
        voll = w_rw * (hk / cop_hk + (1 - hk) / cop_fl) + w_ww / cop_ww
        if not luft or bivalenz is None:
            if vorheizen:
                # ohne Leistungsgrenze: Raumwärme im HT vollständig vorab
                w_rw = anteil * rw_modul3(g)
                voll = w_rw * (hk / cop_hk + (1 - hk) / cop_fl) + w_ww / cop_ww
            strom += voll
            continue
        bedarf = w_rw + w_ww
        cop_eff = np.where(voll > 0, bedarf / np.maximum(voll, 1e-12), 1.0)
        ww_mittel = float(w_ww.mean())
        for b in bivalenz:
            gew = 1 / len(bivalenz)
            kap = (anteil * g.k / 24 * (el.HEIZGRENZE - b) + ww_mittel) * np.clip(
                1 + KAP_GRADIENT * (th - b), 0.4, 1.5
            )
            b_neu = bedarf
            if vorheizen:
                ht_tag = np.bincount(g.tag_nr, weights=np.where(htm, w_rw, 0.0), minlength=n_tage)
                frei = np.where(vorm, np.maximum(kap - bedarf, 0), 0.0)
                frei_tag = np.bincount(g.tag_nr, weights=frei, minlength=n_tage)
                x = np.minimum(ht_tag, frei_tag)
                f_ht = np.divide(x, ht_tag, out=np.zeros_like(x), where=ht_tag > 0)
                f_vor = np.divide(x, frei_tag, out=np.zeros_like(x), where=frei_tag > 0)
                b_neu = (
                    bedarf
                    - np.where(htm, w_rw * f_ht[g.tag_nr], 0.0)
                    + np.where(vorm, frei * f_vor[g.tag_nr], 0.0)
                )
            defizit = np.maximum(b_neu - kap, 0)
            heizstab += gew * defizit
            strom += gew * (b_neu - defizit) / cop_eff
    return strom, heizstab


def _naechste_nacht(g: Grundlage) -> dict[tuple[int, int], int]:
    return {(int(d), int(h)): i for i, (d, h) in enumerate(zip(g.tag_nr, g.stunde, strict=True))}


def ev_modul3(
    g: Grundlage, anteil: float, verteilung: np.ndarray, pos: dict[tuple[int, int], int]
) -> np.ndarray:
    """Ladung zu Hause: der teilnehmende Anteil lädt nur im NT 0-6 Uhr der Folgenacht."""
    hause = g.ev_hause.copy()
    if anteil <= 0:
        return np.asarray(g.ev_fix + g.ev_arbeit + hause)
    tag_ab = (g.stunde >= 7) & np.isin(g.quartal, (1, 4))
    verschieben = anteil * np.where(tag_ab, hause, 0.0)
    hause = hause - verschieben
    je_tag = np.bincount(g.tag_nr, weights=verschieben, minlength=g.tag_nr.max() + 2)
    for d in np.nonzero(je_tag)[0]:
        for h, w in zip(NT_STUNDEN, verteilung, strict=True):
            i = pos.get((int(d) + 1, h))
            if i is not None and w > 0:
                hause[i] += je_tag[d] * w
    return np.asarray(g.ev_fix + g.ev_arbeit + hause)


def rw_modul3(g: Grundlage) -> np.ndarray:
    """Raumwärme mit Vorheizen: Bedarf 17-20 Uhr wird 14-17 Uhr gedeckt (nur Q1/Q4)."""
    rw = g.rw.copy()
    win = np.isin(g.quartal, (1, 4))
    ht = win & np.isin(g.stunde, HT_STUNDEN)
    vor = win & np.isin(g.stunde, VORHEIZEN)
    je_tag = np.bincount(g.tag_nr, weights=np.where(ht, rw, 0.0), minlength=g.tag_nr.max() + 1)
    n_vor = np.bincount(g.tag_nr, weights=vor.astype(float), minlength=g.tag_nr.max() + 1)
    rw[ht] = 0.0
    rw[vor] += (je_tag / np.maximum(n_vor, 1))[g.tag_nr[vor]]
    return rw


def spitzen(gesamt: np.ndarray, idx: pd.DatetimeIndex) -> dict[str, float]:
    s = pd.Series(gesamt, index=idx)
    j = el.lokal(idx).year
    jm = s.groupby(j).max()
    n = s.groupby(j).size()
    jm = jm[n >= 8700]
    return {"median": float(jm.median()), "max": float(jm.max()), "1997": float(jm.get(1997))}


# --------------------------------------------------------------------------- Grafiken


def _achsen(ax: plt.Axes) -> None:
    el._achsen(ax)


def grafik_haushalt(df: pd.DataFrame, ziel: Path) -> None:
    fig, achsen = plt.subplots(1, 2, figsize=(11, 5.4), sharey=True)
    reihen = [
        ("ersparnis_modul1", "Modul 1", FARBEN["basis"]),
        ("ersparnis_modul2", "Modul 2", FARBEN["gwp"]),
        ("ersparnis_modul3_aktiv", "Modul 1+3, verschoben", FARBEN["wp"]),
    ]
    for ax, art, titel in zip(
        achsen,
        ("wp", "ev"),
        ("Wärmepumpe, 4.500 kWh/a", "Wallbox, 2.000 kWh/a zu Hause"),
        strict=True,
    ):
        d = df[df["art"] == art].set_index("vnb")
        y = np.arange(len(d))
        for i, (spalte, name, farbe) in enumerate(reihen):
            ax.barh(y - (i - 1) * 0.26, d[spalte], height=0.24, color=farbe, label=name)
        ax.set_yticks(y, d.index)
        ax.set_title(titel, fontsize=11, color=FARBEN["text"], loc="left")
        ax.set_xlabel("Ersparnis gegenüber Standardnetzentgelt (€/a brutto)")
        _achsen(ax)
        ax.grid(axis="y", visible=False)
    achsen[0].invert_yaxis()
    fig.legend(
        *achsen[0].get_legend_handles_labels(), loc="upper right", ncol=3, frameon=False, fontsize=9
    )
    fig.suptitle(
        "§ 14a 2026: Was die Module einem Haushalt bringen", x=0.01, ha="left", fontsize=12
    )
    fig.tight_layout(rect=(0, 0, 1, 0.95))
    fig.savefig(ziel / "ersparnis_module.png", dpi=150)
    plt.close(fig)


def grafik_fenster(sp_tag: np.ndarray, ziel: Path) -> None:
    """Winterzeitfenster der Netzbetreiber (Q1) über dem Tagesgang der Höchstlasttage."""
    pbs = pb_daten.PREISBLAETTER
    fig, (ax0, ax1) = plt.subplots(
        2, 1, figsize=(11, 6.6), sharex=True, gridspec_kw={"height_ratios": [1.1, 2]}
    )
    x = np.arange(24)
    ax0.plot(x + 0.5, sp_tag, color=FARBEN["wp"], lw=2)
    ax0.set_ylabel("GW")
    ax0.set_title(
        "Last am Tag der Jahresspitze (Vollausbau, Median der Wetterjahre, bivalent)",
        fontsize=10,
        color=FARBEN["text"],
        loc="left",
    )
    _achsen(ax0)
    farbe = {"HT": FARBEN["wp"], "NT": FARBEN["basis"], "ST": "#f1f0ec"}
    for i, pb in enumerate(pbs):
        st = pb_daten.stufen(pb, 1)
        for v in range(96):
            ax1.barh(i, 0.25, left=v / 4, color=farbe[st[v]], height=0.8, linewidth=0)
    ax1.set_yticks(range(len(pbs)), [p.vnb for p in pbs])
    ax1.invert_yaxis()
    ax1.set_xlim(0, 24)
    ax1.set_xticks(range(0, 25, 3))
    ax1.set_xlabel("Uhrzeit")
    ax1.set_title(
        "Modul 3 im 1. Quartal 2026: Hochtarif (orange), Niedertarif (blau)",
        fontsize=10,
        color=FARBEN["text"],
        loc="left",
    )
    for s in ax1.spines.values():
        s.set_visible(False)
    ax1.tick_params(colors=FARBEN["text"])
    fig.tight_layout()
    fig.savefig(ziel / "zeitfenster_winter.png", dpi=150)
    plt.close(fig)


def grafik_teilnahme(kurven: pd.DataFrame, ziel: Path) -> None:
    fig, ax = plt.subplots(figsize=(10, 5))
    stil = {
        "E-Autos, Start bei NT-Beginn": (FARBEN["ev"], "-"),
        "E-Autos, über das NT verteilt": (FARBEN["gesteuert"], "-"),
        "Wärmepumpen, Vorheizen": (FARBEN["wp"], "-"),
    }
    for name, (farbe, ls) in stil.items():
        d = kurven[kurven["variante"] == name]
        ax.plot(d["teilnahme"] * 100, d["spitze_median_gw"], color=farbe, ls=ls, lw=2, label=name)
    ax.set_xlabel("Anteil der Anlagen in Modul 3 (%)")
    ax.set_ylabel("Jahreshöchstlast, Median der Wetterjahre (GW)")
    ax.legend(frameon=False, fontsize=9)
    ax.set_title(
        "Vollausbau mit Heizstab: Höchstlast je nach Teilnahme an Modul 3",
        fontsize=11,
        color=FARBEN["text"],
        loc="left",
    )
    _achsen(ax)
    fig.tight_layout()
    fig.savefig(ziel / "teilnahme.png", dpi=150)
    plt.close(fig)


def grafik_woche(
    reihen: dict[str, np.ndarray], idx: pd.DatetimeIndex, mitte: str, ziel: Path
) -> None:
    lok = el.lokal(idx)
    t0 = pd.Timestamp(mitte) - pd.Timedelta(days=2)
    t1 = pd.Timestamp(mitte) + pd.Timedelta(days=2)
    naiv = lok.tz_localize(None)
    m = (naiv >= t0) & (naiv < t1)
    fig, ax = plt.subplots(figsize=(11, 5))
    farben = [FARBEN["wp"], FARBEN["ev"], FARBEN["gesteuert"], FARBEN["basis"]]
    for (name, r), f in zip(reihen.items(), farben, strict=False):
        ax.plot(naiv[m], r[m], color=f, lw=1.8, label=name)
    ax.set_ylabel("GW")
    ax.legend(frameon=False, fontsize=9, loc="upper left")
    ax.xaxis.set_major_formatter(matplotlib.dates.DateFormatter("%d.%m. %H h"))
    ax.set_title(
        f"Kälteste Tage um {mitte} (Wetter 1997), Vollausbau mit Heizstab",
        fontsize=11,
        color=FARBEN["text"],
        loc="left",
    )
    _achsen(ax)
    fig.tight_layout()
    fig.savefig(ziel / "kaeltewelle_modul3.png", dpi=150)
    plt.close(fig)


# --------------------------------------------------------------------------- Ablauf


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--cache", type=Path, required=True)
    args = ap.parse_args()
    tab, graf = HIER / "tabellen", HIER / "grafiken"
    tab.mkdir(exist_ok=True)
    graf.mkdir(exist_ok=True)

    pb_daten.pruefe_preisblaetter().to_csv(tab / "preisblaetter_pruefung.csv")
    pd.DataFrame(
        [
            {
                "vnb": p.vnb,
                "stand": p.stand,
                "gp_eur_a": p.gp,
                "ap_ct": p.ap,
                "modul1_eur_a": p.modul1,
                "modul2_ap_ct": p.modul2_ap,
                "ht_ct": p.ht,
                "st_ct": p.st,
                "nt_ct": p.nt,
                "fenster": "; ".join(
                    f"{s} {a}-{b} Q{','.join(map(str, q))}" for s, a, b, q in p.fenster
                ),
                "hinweis": p.hinweis,
                "quelle": p.quelle,
            }
            for p in pb_daten.PREISBLAETTER
        ]
    ).to_csv(tab / "preisblaetter_2026.csv", index=False)

    t = el.daten.temperatur(args.cache)
    lst = el.daten.last(args.cache, 2026)
    pkw = el.daten.pkw(args.cache)
    tag = el.tagesmerkmale(t)
    lst_h = lst.reindex(t.index).dropna()
    beta, niveau, _, _ = el.basismodell(lst_h, tag)
    wetter = t[el.lokal(t.index).year <= 2025]
    tag_w = tag[tag.index.year <= 2025]
    hdd_ref = float(tag_w["hdd"].groupby(tag_w.index.year).sum().mean())
    a = el.Annahmen()
    sz = el.szenario(wetter, tag, beta, niveau, pkw, a, hdd_ref)
    st = sz.stunden
    g = grundlage(wetter, tag, pkw, a, hdd_ref)

    # Kontrolle: identisch zur Hochrechnung aus elektrifizierung-last
    wp_mono, _ = waermepumpen(g, g.rw, a, None)
    assert np.allclose(wp_mono, st["wp"].to_numpy())
    assert np.allclose(g.ev_fix + g.ev_hause + g.ev_arbeit, st["ev_frei"].to_numpy())

    basis = st["basis"].to_numpy() + st["gwp"].to_numpy()
    biv = {
        "monovalent (wie elektrifizierung-last)": None,
        "bivalent, Bivalenzpunkt -5 bis 0 °C": tuple(np.linspace(-5, 0, 6)),
        "bivalent, Bivalenzpunkt -10 bis -5 °C": tuple(np.linspace(-10, -5, 6)),
    }
    BIV = biv["bivalent, Bivalenzpunkt -5 bis 0 °C"]
    wp_b, hs_b = waermepumpen(g, g.rw, a, BIV)
    wp_b3, hs_b3 = waermepumpen(g, g.rw, a, BIV, vorheizen=True)
    pos = _naechste_nacht(g)
    ev_frei = st["ev_frei"].to_numpy()

    # Fensterabdeckung: Anteil der Netzbetreiber der Stichprobe mit HT um 17-19 Uhr im Winter
    abdeckung = float(
        np.mean(
            [pb_daten.anteil_stunde(p, 1, "HT")[17:19].mean() > 0.5 for p in pb_daten.PREISBLAETTER]
        )
    )

    # Teil A
    ha = teil_a(st[["wp"]].assign(hs=hs_b))
    ha.round(2).to_csv(tab / "haushalt_module.csv", index=False)
    preis = pb_daten.day_ahead(args.cache)
    dyn = dynamischer_tarif(preis, profil_wp(st[["wp"]]))

    # Teil B: Bivalenz
    zeilen = []
    for name, b in biv.items():
        w, h = waermepumpen(g, g.rw, a, b)
        sp = spitzen(basis + w + h + ev_frei, g.idx)
        i = int(np.argmax(basis + w + h + ev_frei))
        zeilen.append(
            {
                "variante": name,
                "spitze_median_gw": sp["median"],
                "spitze_max_gw": sp["max"],
                "heizstab_bei_spitze_gw": float(h[i]),
                "heizstab_twh_mittel": float(h.sum() / 1000 / 35),
                "wp_twh_mittel": float(w.sum() / 1000 / 35),
            }
        )
    bivalenz = pd.DataFrame(zeilen).set_index("variante")
    bivalenz.round(2).to_csv(tab / "bivalenz.csv")

    def gesamt(r_ev: float, vert: np.ndarray, r_wp: float) -> np.ndarray:
        ev = ev_modul3(g, r_ev * abdeckung, vert, pos)
        r = r_wp * abdeckung
        w = (1 - r) * (wp_b + hs_b) + r * (wp_b3 + hs_b3)
        return np.asarray(basis + w + ev)

    # Szenarien
    referenz = basis + wp_b + hs_b + ev_frei
    i_spitze = int(np.argmax(referenz))
    szen = {
        "bivalent, ungesteuert": referenz,
        "Modul 3: 30 % der E-Autos, Start bei NT-Beginn": gesamt(0.3, SYNCHRON, 0),
        "Modul 3: 30 % der E-Autos, über das NT verteilt": gesamt(0.3, VERTEILT, 0),
        "Modul 3: alle E-Autos, Start bei NT-Beginn": gesamt(1, SYNCHRON, 0),
        "Modul 3: alle E-Autos, über das NT verteilt": gesamt(1, VERTEILT, 0),
        "Modul 3: 30 % der Wärmepumpen heizen vor": gesamt(0, SYNCHRON, 0.3),
        "Modul 3: alle Wärmepumpen heizen vor": gesamt(0, SYNCHRON, 1),
        "E-Autos systemdienlich (Lastglättung wie elektrifizierung-last)": basis
        + wp_b
        + hs_b
        + st["ev_gesteuert"].to_numpy(),
        "Obergrenze Dimmung: Heizstab aus, Wallboxen auf 4,2 kW": referenz
        - hs_b
        - g.ev_hause * (1 - DIMM_KW / WALLBOX_KW),
    }
    sz_tab = pd.DataFrame(
        [
            {"szenario": k, **{f"spitze_{m}_gw": v for m, v in spitzen(r, g.idx).items()}}
            for k, r in szen.items()
        ]
    ).set_index("szenario")
    sz_tab["aenderung_median_gw"] = sz_tab["spitze_median_gw"] - sz_tab.iloc[0]["spitze_median_gw"]
    sz_tab.round(1).to_csv(tab / "szenarien.csv")

    kurven = []
    for r in np.linspace(0, 1, 11):
        for name, args_ in (
            ("E-Autos, Start bei NT-Beginn", (r, SYNCHRON, 0)),
            ("E-Autos, über das NT verteilt", (r, VERTEILT, 0)),
            ("Wärmepumpen, Vorheizen", (0, SYNCHRON, r)),
            ("E-Autos synchron + Wärmepumpen", (r, SYNCHRON, r)),
        ):
            kurven.append(
                {
                    "variante": name,
                    "teilnahme": r,
                    "spitze_median_gw": spitzen(gesamt(*args_), g.idx)["median"],
                }
            )
    kurven_df = pd.DataFrame(kurven)
    kurven_df.round(2).to_csv(tab / "teilnahme.csv", index=False)

    # Grafiken
    grafik_haushalt(ha, graf)
    j = el.lokal(g.idx).year
    jm = pd.Series(referenz, index=g.idx).groupby(j).idxmax()
    lok = el.lokal(g.idx)
    tage_sp = [el.lokal(pd.DatetimeIndex([ts]))[0].normalize() for ts in jm.to_numpy()]
    tagesgang = []
    for d in tage_sp:
        m = lok.normalize() == d
        if m.sum() == 24:
            tagesgang.append(referenz[m])
    grafik_fenster(np.median(np.array(tagesgang), axis=0), graf)
    grafik_teilnahme(kurven_df, graf)
    zp = el.lokal(pd.DatetimeIndex([g.idx[i_spitze]]))[0].strftime("%Y-%m-%d %H:00")
    grafik_woche(
        {
            "ohne Reaktion": referenz,
            "alle E-Autos in Modul 3, Start bei NT-Beginn": szen[
                "Modul 3: alle E-Autos, Start bei NT-Beginn"
            ],
            "alle E-Autos in Modul 3, über das NT verteilt": szen[
                "Modul 3: alle E-Autos, über das NT verteilt"
            ],
            "alle Wärmepumpen heizen vor": szen["Modul 3: alle Wärmepumpen heizen vor"],
        },
        g.idx,
        zp,
        graf,
    )

    i_ref = i_spitze
    stunden_spitze = {}
    for name, b in biv.items():
        w, h = waermepumpen(g, g.rw, a, b)
        r = pd.Series(basis + w + h + ev_frei, index=g.idx)
        jj = el.lokal(g.idx).year
        n = r.groupby(jj).size()
        ix = r.groupby(jj).idxmax()[n >= 8700]
        stunden_spitze[name] = (
            pd.Series([el.lokal(pd.DatetimeIndex([v]))[0].hour for v in ix])
            .value_counts()
            .sort_index()
            .to_dict()
        )
    lauf = {
        "fensterabdeckung_winter": round(abdeckung, 3),
        "spitze_zeitpunkt": zp,
        "spitze_gw": round(float(referenz[i_ref]), 1),
        "heizstab_bei_spitze_gw": round(float(hs_b[i_ref]), 1),
        "wp_bei_spitze_gw": round(float(wp_b[i_ref]), 1),
        "ev_hause_bei_spitze_gw": round(float(g.ev_hause[i_ref]), 1),
        **{k: round(v, 2) for k, v in dyn.items()},
        "stunde_jahresspitze": {
            k: {int(h): int(c) for h, c in v.items()} for k, v in stunden_spitze.items()
        },
        "annahmen": {
            "e_wp_kwh": E_WP,
            "e_ev_kwh": E_EV,
            "zaehler_modul2_eur": ZAEHLER_MODUL2,
            "wp_nt_anteil": WP_NT_ANTEIL,
            "wp_flex_anteil_dynamisch": WP_FLEX_ANTEIL,
            "kap_gradient": KAP_GRADIENT,
        },
    }
    (tab / "lauf.json").write_text(json.dumps(lauf, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps(lauf, indent=2, ensure_ascii=False))
    pd.set_option("display.width", 200)
    print(ha.round(0).to_string())
    print(bivalenz.round(1).to_string())
    print(sz_tab.round(1).to_string())
    print(
        kurven_df.pivot(index="teilnahme", columns="variante", values="spitze_median_gw")
        .round(1)
        .to_string()
    )


if __name__ == "__main__":
    main()
