"""Elektrifizierung und Last: E-Autos, Wärmepumpen, Klimageräte.

Teil A (Rückblick 2015-2025): Wie stark haben E-Autos, Wärmepumpen und Klimageräte die
deutsche Netzlast bisher verändert? Jahreswerte der Last gegen den geschätzten Verbrauch der
Bestände; Temperaturempfindlichkeit der Tageslast je Jahr (GW je Kelvin Heizgradtag und
Kühlgradtag) gegen den Zuwachs an Wärmepumpen.

Teil B (Hochrechnung Vollausbau): Alle Pkw fahren elektrisch. Jede Raumwärme und jedes
Warmwasser, das heute fossil erzeugt wird, kommt aus Wärmepumpen. Ausnahme sind Gebäude an
Wärmenetzen: Diese bleiben am Netz, der fossile Teil der Fernwärme wird nicht fossil ersetzt,
ein Teil davon durch Großwärmepumpen (Flusswasser, Abwasser). Für jedes Wetterjahr
1991-2025 ergibt das eine stündliche Last aus heutiger Grundlast, Wärmepumpen und E-Autos,
mit ungesteuertem und netzdienlich gesteuertem Laden.

Teil C (Klimaanlagen): Ausbau der Klimaanlagen in Haushalten in einem wärmeren Klima
(Trend der Sommertemperatur 1991-2026 fortgeschrieben), siehe klima.py. Dazu der Hitzesommer
2026 in der gemessenen Last.

Aufruf aus dem Repository-Wurzelverzeichnis (lädt beim ersten Lauf DWD-Temperaturen,
Energy-Charts-Last und Eurostat-Reihen, einige Minuten)::

    uv run --no-project --with pandas --with matplotlib --with httpx --with holidays \\
        python docs/analysen/elektrifizierung-last/auswertung.py --cache /tmp/el
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, replace
from pathlib import Path

import holidays
import matplotlib
import matplotlib.dates

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

HIER = Path(__file__).resolve().parent
sys.path.insert(0, str(HIER))
import daten  # noqa: E402
import klima  # noqa: E402

TZ = "Europe/Berlin"
HEIZGRENZE = 15.0  # °C, Tagesmittel der gedämpften Temperatur
KUEHLGRENZE = 18.0  # °C, Tagesmittel
RUECKBLICK = range(2015, 2026)
BASISJAHRE = (2023, 2024, 2025)
FARBEN = {
    "basis": "#2a78d6",
    "wp": "#eb6834",
    "gwp": "#1baf7a",
    "ev": "#eda100",
    "gesteuert": "#6250d6",
    "text": "#52514e",
    "grid": "#e4e3df",
}
PJ_TWH = 1 / 3.6


@dataclass(frozen=True)
class Annahmen:
    """Stellschrauben der Hochrechnung (Mittelwerte, Spannen im README)."""

    kesselwirkungsgrad: float = 0.90  # Nutzwärme je kWh Gas/Öl/Kohle
    anteil_luft: float = 0.85  # Luft-WP, Rest Sole/Erdreich
    anteil_heizkoerper: float = 0.75  # Rest Flächenheizung
    cop_korrektur: float = 0.85  # Praxisabschlag auf die Herstellerkurven
    sanierung: float = 0.0  # Minderung des Raumwärmebedarfs
    gwp_anteil_fw: float = 0.5  # Anteil der fossilen Fernwärme aus Großwärmepumpen
    kwh_je_100km: float = 20.0  # ab Netz, inkl. Ladeverlust
    anteil_zuhause: float = 0.60
    anteil_arbeit: float = 0.15
    anteil_oeffentlich: float = 0.10  # AC, tagsüber
    # Rest: Schnellladen entlang des Verkehrs


# --------------------------------------------------------------------------- Hilfen


def lokal(idx: pd.DatetimeIndex) -> pd.DatetimeIndex:
    return idx.tz_convert(TZ)


def tagesmerkmale(t_stunde: pd.Series) -> pd.DataFrame:
    """Tagesmittel, gedämpfte Temperatur (Gebäudeträgheit), Heiz- und Kühlgradtage."""
    t = t_stunde.copy()
    t.index = lokal(t.index)
    tag = t.groupby(t.index.date).mean()
    tag.index = pd.to_datetime(tag.index)
    gedaempft = (tag + 0.5 * tag.shift(1) + 0.25 * tag.shift(2) + 0.125 * tag.shift(3)) / 1.875
    gedaempft = gedaempft.fillna(tag)
    return pd.DataFrame(
        {
            "t": tag,
            "tg": gedaempft,
            "hdd": (HEIZGRENZE - gedaempft).clip(lower=0),
            "cdd": (tag - KUEHLGRENZE).clip(lower=0),
        }
    )


def tageslaenge(tage: pd.DatetimeIndex, breite: float = 51.0) -> np.ndarray:
    doy = tage.day_of_year.to_numpy()
    dekl = np.radians(23.44) * np.sin(2 * np.pi * (284 + doy) / 365)
    x = -np.tan(np.radians(breite)) * np.tan(dekl)
    return np.asarray(24 / np.pi * np.arccos(np.clip(x, -1, 1)))


def tagtyp(tage: pd.DatetimeIndex) -> np.ndarray:
    """0 Werktag, 1 Samstag, 2 Sonn- und Feiertag."""
    jahre = sorted(set(tage.year))
    ft = holidays.country_holidays("DE", years=jahre)
    feiertag = np.array([d.date() in ft for d in tage])
    typ = np.where(tage.day_of_week == 5, 1, np.where(tage.day_of_week == 6, 2, 0))
    return np.where(feiertag, 2, typ)


def weihnachten(tage: pd.DatetimeIndex) -> np.ndarray:
    return np.asarray(
        ((tage.month == 12) & (tage.day >= 24)) | ((tage.month == 1) & (tage.day <= 1))
    )


def ols(x: np.ndarray, y: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    beta, *_ = np.linalg.lstsq(x, y, rcond=None)
    res = y - x @ beta
    sigma2 = res @ res / (len(y) - x.shape[1])
    cov = sigma2 * np.linalg.pinv(x.T @ x)
    return beta, np.sqrt(np.diag(cov))


# --------------------------------------------------------------------------- Teil A


def rueckblick(
    lst: pd.Series, tag: pd.DataFrame, pkw: pd.DataFrame, hhq: pd.DataFrame
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Jahrestabelle und Temperaturempfindlichkeit je Jahr."""
    hw = daten.HANDWERTE
    l_lok = lst.copy()
    l_lok.index = lokal(l_lok.index)
    l_tag = l_lok.groupby(l_lok.index.date).mean()
    l_tag.index = pd.to_datetime(l_tag.index)
    df = tag.join(l_tag.rename("last"), how="inner").dropna()
    df = df[(df.index.year >= RUECKBLICK.start) & (df.index.year < RUECKBLICK.stop)]

    # Je Jahr eine Regression nur über Wintertage (Nov-Feb, kaum PV-Eigenverbrauch) mit dem
    # Heizgradtag und eine über Sommertage (Jun-Aug) mit dem Kühlgradtag; Wochentag,
    # Feiertag, Weihnachten und Tageslänge als Kontrollgrößen.
    jahre = list(RUECKBLICK)
    zeilen = []
    for y in jahre:
        zeile: dict[str, float] = {"jahr": y}
        for name, monate, gradtag in (
            ("hdd", (11, 12, 1, 2), "hdd"),
            ("cdd", (6, 7, 8), "cdd"),
        ):
            d = df[(df.index.year == y) & df.index.month.isin(monate)]
            typ = tagtyp(d.index)
            sp = [np.ones(len(d)), d[gradtag].to_numpy()]
            for k in range(1, 7):
                sp.append(((d.index.day_of_week == k) & (typ != 2)).astype(float))
            sp.append((typ == 2).astype(float))
            if name == "hdd":
                sp.append(weihnachten(d.index).astype(float))
            sp.append(tageslaenge(d.index))
            beta, se = ols(np.column_stack(sp), d["last"].to_numpy())
            zeile[f"gw_je_{name}"] = beta[1]
            zeile[f"se_{name}"] = se[1]
        zeilen.append(zeile)
    empf = pd.DataFrame(zeilen).set_index("jahr")

    # Erwarteter Beitrag der Wärmepumpen zur Heizsteigung: Strom der Haushalts-WP für
    # Raumwärme (Umweltwärme aus Eurostat / (JAZ - 1)) je Heizgradtag.
    jaz = 3.2
    hdd_jahr = tag["hdd"].groupby(tag.index.year).sum()
    jahrtab = pd.DataFrame(index=jahre)
    jahrtab.index.name = "jahr"
    jahrtab["last_twh"] = l_lok.groupby(l_lok.index.year).sum().reindex(jahre) / 1000
    jahrtab["heizgradtage"] = hdd_jahr.reindex(jahre)
    jahrtab["bev_bestand"] = pkw["bestand_bev"].reindex(jahre)
    jahrtab["phev_bestand"] = pkw["bestand_phev"].reindex(jahre)
    jahrtab["pkw_bestand"] = pkw["bestand_gesamt"].reindex(jahre)
    km = hw["pkw_fahrleistung_km"] / hw["pkw_bestand"]
    mittel_bestand = (
        jahrtab[["bev_bestand", "phev_bestand"]].fillna(0)
        + jahrtab[["bev_bestand", "phev_bestand"]].shift(1).fillna(0)
    ) / 2
    jahrtab["ev_twh"] = (
        (mittel_bestand["bev_bestand"] + 0.4 * mittel_bestand["phev_bestand"]) * km * 0.20 / 1e9
    )
    umwelt = hhq.get("sh_RA600", pd.Series(dtype=float)).reindex(jahre)
    umwelt_ww = hhq.get("wh_RA600", pd.Series(dtype=float)).reindex(jahre)
    jahrtab["wp_hh_umweltwaerme_twh"] = umwelt + umwelt_ww.fillna(0)
    jahrtab["wp_hh_strom_twh"] = jahrtab["wp_hh_umweltwaerme_twh"] / (jaz - 1)
    jahrtab["wp_absatz"] = pd.Series(hw["bwp_absatz"]).reindex(jahre)
    jahrtab["wp_absatz_kumuliert_ab_2007"] = pd.Series(hw["bwp_absatz"]).cumsum().reindex(jahre)
    jahrtab["kuehlung_hh_strom_twh"] = hhq.get("sc_E7000", pd.Series(dtype=float)).reindex(jahre)
    raumwaerme_strom = (umwelt / (jaz - 1)).reindex(jahre)
    empf["wp_erwartet_gw_je_hdd"] = raumwaerme_strom * 1000 / 24 / hdd_jahr.reindex(jahre)
    return jahrtab, empf


# --------------------------------------------------------------------------- Teil B


def basismodell(lst: pd.Series, tag: pd.DataFrame) -> tuple[np.ndarray, float, float, float]:
    """Stundenmodell der heutigen Last, angepasst auf 2023-2025, Niveau 2025."""
    m = merkmale(lst.index, tag)
    j = lst.index.tz_convert(TZ).year
    maske = np.isin(j, BASISJAHRE)
    x = np.column_stack([m] + [(j == y).astype(float) for y in BASISJAHRE[1:]])
    y = lst.to_numpy()
    beta, _ = ols(x[maske], y[maske])
    pred = x[maske] @ beta
    r2 = 1 - np.var(y[maske] - pred) / np.var(y[maske])
    rmse = float(np.sqrt(np.mean((y[maske] - pred) ** 2)))
    niveau_2025 = float(beta[-1])
    return beta[: m.shape[1]], niveau_2025, float(r2), rmse


def merkmale(idx: pd.DatetimeIndex, tag: pd.DataFrame) -> np.ndarray:
    lok = lokal(idx)
    tage = pd.DatetimeIndex(lok.date)
    typ = tagtyp(tage)
    stunde = lok.hour.to_numpy()
    t = tag.reindex(tage)
    hdd, cdd = t["hdd"].to_numpy(), t["cdd"].to_numpy()
    spalten = []
    for ty in range(3):
        for h in range(24):
            spalten.append(((typ == ty) & (stunde == h)).astype(float))
    for mo in range(2, 13):
        spalten.append((lok.month == mo).astype(float))
    gruppe = stunde // 6
    for g in range(4):
        spalten.append(hdd * (gruppe == g))
        spalten.append(cdd * (gruppe == g))
    xm = weihnachten(tage)
    for g in range(4):
        spalten.append((xm & (gruppe == g)).astype(float))
    spalten.append(tageslaenge(tage))
    return np.column_stack(spalten)


def cop_luft(dt: np.ndarray) -> np.ndarray:
    # Ruhnau, Hirth, Praktiknjo (2019): Time series of heat demand and heat pump
    # efficiency for energy system modeling, Scientific Data 6:189 (when2heat), Luft-WP
    return 6.08 - 0.09 * dt + 0.0005 * dt**2


def cop_sole(dt: np.ndarray) -> np.ndarray:
    return 10.29 - 0.21 * dt + 0.0012 * dt**2


def wp_strom(
    t_h: np.ndarray, t_boden: np.ndarray, waerme_rw: np.ndarray, waerme_ww: np.ndarray, a: Annahmen
) -> np.ndarray:
    senke_hk = 40 - 1.0 * t_h
    senke_fl = 30 - 0.5 * t_h
    strom = np.zeros_like(t_h)
    for quelle, anteil, kurve in (
        (t_h, a.anteil_luft, cop_luft),
        (t_boden, 1 - a.anteil_luft, cop_sole),
    ):
        cop_hk = kurve(np.maximum(senke_hk - quelle, 15)) * a.cop_korrektur
        cop_fl = kurve(np.maximum(senke_fl - quelle, 15)) * a.cop_korrektur
        cop_ww = kurve(np.maximum(50 - quelle, 15)) * a.cop_korrektur
        rw = waerme_rw * (a.anteil_heizkoerper / cop_hk + (1 - a.anteil_heizkoerper) / cop_fl)
        strom += anteil * (rw + waerme_ww / cop_ww)
    return strom


def gwp_strom(t_h: np.ndarray, t_14: np.ndarray, waerme: np.ndarray) -> np.ndarray:
    """Großwärmepumpe am Fluss: Gütegrad 0,5 auf Carnot, Vorlauf gleitend 70-95 °C."""
    quelle = np.clip(t_14 + 2, 3, 25)
    senke = np.clip(90 - 0.5 * (t_h + 10), 70, 95)
    cop = 0.5 * (senke + 273.15) / (senke - quelle)
    return np.asarray(waerme / cop)


RW_PROFIL = np.array(
    [
        0.80,
        0.80,
        0.80,
        0.82,
        0.88,
        1.00,
        1.22,
        1.28,
        1.22,
        1.10,
        1.02,
        0.98,
        0.96,
        0.96,
        0.96,
        0.98,
        1.04,
        1.12,
        1.16,
        1.14,
        1.08,
        1.00,
        0.92,
        0.86,
    ]
)
WW_PROFIL = np.array(
    [
        0.25,
        0.20,
        0.20,
        0.20,
        0.30,
        0.80,
        1.90,
        2.30,
        1.80,
        1.30,
        1.00,
        1.00,
        1.10,
        0.90,
        0.80,
        0.80,
        0.90,
        1.10,
        1.40,
        1.50,
        1.40,
        1.10,
        0.80,
        0.40,
    ]
)
# Ankunft zu Hause (Werktag / Wochenende) und am Arbeitsplatz, Anteil je lokale Stunde
ANKUNFT_HAUSE_WT = np.array(
    [
        0.02,
        0.01,
        0.0,
        0.0,
        0.0,
        0.0,
        0.0,
        0.01,
        0.01,
        0.01,
        0.02,
        0.02,
        0.03,
        0.04,
        0.05,
        0.07,
        0.10,
        0.13,
        0.13,
        0.10,
        0.08,
        0.06,
        0.05,
        0.03,
    ]
)
ANKUNFT_HAUSE_WE = np.array(
    [
        0.03,
        0.01,
        0.0,
        0.0,
        0.0,
        0.0,
        0.0,
        0.0,
        0.01,
        0.02,
        0.04,
        0.06,
        0.08,
        0.08,
        0.08,
        0.08,
        0.09,
        0.10,
        0.10,
        0.08,
        0.06,
        0.05,
        0.04,
        0.03,
    ]
)
ANKUNFT_ARBEIT = np.array([0.0] * 6 + [0.10, 0.25, 0.30, 0.20, 0.10, 0.05] + [0.0] * 12)
OEFFENTLICH = np.array([0.0] * 8 + [1.0] * 12 + [0.0] * 4)
SCHNELL = np.array(
    [
        0.2,
        0.1,
        0.1,
        0.1,
        0.2,
        0.4,
        0.7,
        1.0,
        1.1,
        1.2,
        1.3,
        1.3,
        1.3,
        1.3,
        1.3,
        1.4,
        1.5,
        1.5,
        1.3,
        1.0,
        0.8,
        0.6,
        0.4,
        0.3,
    ]
)
LADEDAUER_H = 3


def _verschmiert(ankunft: np.ndarray) -> np.ndarray:
    """Ankunftsverteilung -> Ladeprofil bei LADEDAUER_H Stunden konstanter Leistung."""
    p = np.zeros(24)
    for h, w in enumerate(ankunft):
        for k in range(LADEDAUER_H):
            p[(h + k) % 24] += w / LADEDAUER_H
    return p / p.sum()


def auffuellen(hinter: np.ndarray, energie: float) -> np.ndarray:
    """Wasserfüllung: verteilt energie so, dass das Maximum von hinter + Ladung minimal wird."""
    if energie <= 0:
        return np.zeros_like(hinter)
    s = np.sort(hinter)
    n = len(s)
    kum = np.cumsum(s)
    for k in range(1, n + 1):
        lam = (energie + kum[k - 1]) / k
        if k == n or lam <= s[k]:
            break
    return np.asarray(np.maximum(lam - hinter, 0))


@dataclass
class Szenario:
    stunden: pd.DataFrame  # UTC-Index: basis, wp, gwp, ev_frei, ev_gesteuert
    kennzahlen: dict[str, float]


def szenario(
    t: pd.Series,
    tag: pd.DataFrame,
    beta: np.ndarray,
    niveau: float,
    pkw: pd.DataFrame,
    a: Annahmen,
    hdd_ref: float,
) -> Szenario:
    hw = daten.HANDWERTE
    idx = t.index
    lok = lokal(idx)
    tage = pd.DatetimeIndex(lok.date)
    stunde = lok.hour.to_numpy()
    td = tag.reindex(tage)
    th = t.to_numpy()
    t_boden = (8 + 0.25 * (t.rolling(24 * 30, min_periods=24).mean() - 9)).bfill().to_numpy()
    t_14 = t.rolling(24 * 14, min_periods=24).mean().bfill().to_numpy()

    basis = merkmale(idx, tag) @ beta + niveau

    # Wärme: fossile Endenergie 2024 (AGEB) * Kesselwirkungsgrad, Raumwärme witterungs-
    # bereinigt auf das Mittel der Wetterjahre.
    pj = hw["ageb_pj"]
    fossil = ("oel", "gas", "kohle")
    rw_pj = sum(
        pj[s].get(f, 0.0)
        for s in ("hh_raumwaerme", "ghd_raumwaerme", "ind_raumwaerme")
        for f in fossil
    )
    ww_pj = sum(pj[s].get(f, 0.0) for s in ("hh_warmwasser_2023", "ghd_warmwasser") for f in fossil)
    hdd_2024 = float(tag["hdd"][tag.index.year == 2024].sum())
    rw_twh = rw_pj * PJ_TWH * a.kesselwirkungsgrad * (1 - a.sanierung) * hdd_ref / hdd_2024
    ww_twh = ww_pj * PJ_TWH * a.kesselwirkungsgrad
    k = rw_twh * 1000 / hdd_ref  # GWh je Heizgradtag
    # Wärmepumpen laufen ohne tiefe Nachtabsenkung: Tagesgang auf 40 % abgeflacht
    rw_prof = 1 + 0.4 * (RW_PROFIL / RW_PROFIL.mean() - 1)
    ww_prof = WW_PROFIL / WW_PROFIL.mean()
    waerme_rw = k * td["hdd"].to_numpy() / 24 * rw_prof[stunde]  # GW
    waerme_ww = ww_twh * 1000 / 8760 * ww_prof[stunde]
    wp = wp_strom(th, t_boden, waerme_rw, waerme_ww, a)

    fw_twh = hw["fernwaerme_twh"] * hw["fernwaerme_fossil_anteil"] * a.gwp_anteil_fw
    # Fernwärme: 70 % witterungsabhängig, 30 % Warmwasser und Netzverluste
    fw_rw = 0.7 * fw_twh * 1000 / hdd_ref * td["hdd"].to_numpy() / 24 * rw_prof[stunde]
    fw_rest = 0.3 * fw_twh * 1000 / 8760
    gwp = gwp_strom(th, t_14, fw_rw + fw_rest)

    # E-Autos: Rest der Pkw-Fahrleistung, die heute noch nicht elektrisch ist
    letzte = pkw.dropna(subset=["bestand_bev"]).iloc[-1]
    heute_el = (letzte["bestand_bev"] + 0.5 * letzte["bestand_phev"]) / letzte["bestand_gesamt"]
    ev_twh = hw["pkw_fahrleistung_km"] * a.kwh_je_100km / 100 / 1e9 * (1 - heute_el)
    typ = tagtyp(pd.DatetimeIndex(sorted(set(tage))))
    tag_index = pd.DatetimeIndex(sorted(set(tage)))
    wochenfaktor = np.where(typ == 0, 1.05, np.where(typ == 1, 0.95, 0.80))
    winter = 1 + 0.012 * np.clip(16 - tag["t"].reindex(tag_index).to_numpy(), 0, None)
    tagesfaktor = wochenfaktor * winter
    tagesfaktor = tagesfaktor / tagesfaktor.mean()
    ev_tag = pd.Series(ev_twh * 1000 / 365.25 * tagesfaktor, index=tag_index)  # GWh je Tag
    e_tag = ev_tag.reindex(tage).to_numpy()
    we = tagtyp(tage) > 0
    p_hause = np.where(
        we, _verschmiert(ANKUNFT_HAUSE_WE)[stunde], _verschmiert(ANKUNFT_HAUSE_WT)[stunde]
    )
    p_arbeit = np.where(we, 0.0, _verschmiert(ANKUNFT_ARBEIT)[stunde])
    p_oeff = (OEFFENTLICH / OEFFENTLICH.sum())[stunde]
    p_schnell = (SCHNELL / SCHNELL.sum())[stunde]
    a_rest = 1 - a.anteil_zuhause - a.anteil_arbeit - a.anteil_oeffentlich
    anteil_arbeit = np.where(we, 0.0, a.anteil_arbeit)
    anteil_hause = a.anteil_zuhause + a.anteil_arbeit - anteil_arbeit
    fix = e_tag * (a.anteil_oeffentlich * p_oeff + a_rest * p_schnell)
    ev_frei = fix + e_tag * (anteil_hause * p_hause + anteil_arbeit * p_arbeit)

    # Gesteuert: Laden zu Hause im Fenster 17-7 Uhr, am Arbeitsplatz 8-17 Uhr, jeweils so,
    # dass die Gesamtlast (Basis + WP + fixe Ladung) möglichst flach bleibt.
    hinter = basis + wp + gwp + fix
    ev_st = fix.copy()
    tag_nr = (tage - tage[0]).days.to_numpy()
    n_tage = tag_nr.max() + 1
    starts = np.searchsorted(tag_nr, np.arange(n_tage + 1))
    e_hause_tag = np.zeros(n_tage)
    e_arbeit_tag = np.zeros(n_tage)
    for d in range(n_tage):
        s, e = starts[d], starts[d + 1]
        if s == e:
            continue
        e_hause_tag[d] = float(np.sum(e_tag[s:e] * anteil_hause[s:e] * p_hause[s:e]))
        e_arbeit_tag[d] = float(np.sum(e_tag[s:e] * anteil_arbeit[s:e] * p_arbeit[s:e]))
    for d in range(n_tage):
        s, e = starts[d], starts[d + 1]
        if s == e:
            continue
        # Arbeitsplatz 8-17 Uhr lokal
        f = np.arange(s, e)[(stunde[s:e] >= 8) & (stunde[s:e] < 17)]
        if len(f) and e_arbeit_tag[d] > 0:
            add = auffuellen(hinter[f] + ev_st[f] - fix[f], e_arbeit_tag[d])
            ev_st[f] += add
        # zu Hause 17 Uhr bis 7 Uhr des Folgetags
        e2 = starts[min(d + 2, n_tage)]
        f = np.arange(s, e2)
        f = f[((tag_nr[f] == d) & (stunde[f] >= 17)) | ((tag_nr[f] == d + 1) & (stunde[f] < 7))]
        if len(f) and e_hause_tag[d] > 0:
            add = auffuellen(hinter[f] + ev_st[f] - fix[f], e_hause_tag[d])
            ev_st[f] += add

    st = pd.DataFrame(
        {"basis": basis, "wp": wp, "gwp": gwp, "ev_frei": ev_frei, "ev_gesteuert": ev_st},
        index=idx,
    )
    jaz = (waerme_rw.sum() + waerme_ww.sum()) / wp.sum()
    kz = {
        "waerme_rw_twh": rw_twh,
        "waerme_ww_twh": ww_twh,
        "waerme_fw_gwp_twh": fw_twh,
        "ev_twh_zusaetzlich": ev_twh,
        "ev_anteil_heute": heute_el,
        "jaz_wp": float(jaz),
    }
    return Szenario(st, kz)


def wetterjahre(sz: Szenario) -> pd.DataFrame:
    st = sz.stunden
    j = lokal(st.index).year
    zeilen = []
    for y in sorted(set(j)):
        d = st[j == y]
        if len(d) < 8700:
            continue
        frei = d["basis"] + d["wp"] + d["gwp"] + d["ev_frei"]
        gest = d["basis"] + d["wp"] + d["gwp"] + d["ev_gesteuert"]
        i_f = frei.idxmax()
        zeilen.append(
            {
                "wetterjahr": y,
                "basis_twh": d["basis"].sum() / 1000,
                "wp_twh": d["wp"].sum() / 1000,
                "gwp_twh": d["gwp"].sum() / 1000,
                "ev_twh": d["ev_frei"].sum() / 1000,
                "gesamt_twh": frei.sum() / 1000,
                "spitze_basis_gw": d["basis"].max(),
                "spitze_ungesteuert_gw": frei.max(),
                "spitze_gesteuert_gw": gest.max(),
                "zeitpunkt_spitze": lokal(pd.DatetimeIndex([i_f]))[0].strftime("%Y-%m-%d %H:00"),
                "wp_bei_spitze_gw": d.loc[i_f, "wp"] + d.loc[i_f, "gwp"],
                "ev_bei_spitze_gw": d.loc[i_f, "ev_frei"],
            }
        )
    return pd.DataFrame(zeilen).set_index("wetterjahr")


# --------------------------------------------------------------------------- Grafiken


def _achsen(ax: plt.Axes) -> None:
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    ax.spines["left"].set_color(FARBEN["grid"])
    ax.spines["bottom"].set_color(FARBEN["grid"])
    ax.tick_params(colors=FARBEN["text"])
    ax.grid(axis="y", color=FARBEN["grid"], linewidth=0.8)
    ax.set_axisbelow(True)


def grafik_rueckblick(jt: pd.DataFrame, ziel: Path) -> None:
    voll = jt.dropna(subset=["wp_hh_strom_twh"])
    fig, ax = plt.subplots(figsize=(9, 4.2))
    x = voll.index.to_numpy()
    ax.bar(x, voll["ev_twh"], color=FARBEN["ev"], label="E-Autos (geschätzt)", width=0.7)
    ax.bar(
        x,
        voll["wp_hh_strom_twh"],
        bottom=voll["ev_twh"],
        color=FARBEN["wp"],
        label="Wärmepumpen Haushalte (aus Eurostat-Umweltwärme)",
        width=0.7,
    )
    _achsen(ax)
    ax.set_ylabel("TWh je Jahr", color=FARBEN["text"])
    ax.set_title(
        "Geschätzter Stromverbrauch von E-Autos und Haushalts-Wärmepumpen", loc="left", fontsize=11
    )
    ax.legend(frameon=False, loc="upper left")
    fig.tight_layout()
    fig.savefig(ziel / "rueckblick_verbrauch.png", dpi=150)
    plt.close(fig)

    fig, ax = plt.subplots(figsize=(9, 3.6))
    ax.plot(
        jt.index.to_numpy(),
        jt["last_twh"],
        color=FARBEN["basis"],
        linewidth=2,
        marker="o",
        markersize=5,
    )
    _achsen(ax)
    ax.set_ylabel("TWh je Jahr", color=FARBEN["text"])
    ax.set_title("Netzlast Deutschland (Energy-Charts)", loc="left", fontsize=11)
    fig.tight_layout()
    fig.savefig(ziel / "rueckblick_last.png", dpi=150)
    plt.close(fig)


def grafik_empfindlichkeit(empf: pd.DataFrame, ziel: Path) -> None:
    fig, axs = plt.subplots(1, 2, figsize=(10, 3.8))
    x = empf.index.to_numpy()
    ax = axs[0]
    ax.errorbar(
        x,
        empf["gw_je_hdd"],
        yerr=1.96 * empf["se_hdd"],
        fmt="o",
        color=FARBEN["basis"],
        capsize=3,
        label="gemessen (95 %-Intervall)",
    )
    basis = empf["gw_je_hdd"].iloc[:3].mean() - empf["wp_erwartet_gw_je_hdd"].iloc[:3].mean()
    ax.plot(
        x,
        basis + empf["wp_erwartet_gw_je_hdd"],
        color=FARBEN["wp"],
        linewidth=2,
        label="erwartet aus WP-Bestand",
    )
    _achsen(ax)
    ax.set_title("Winter: GW je Heizgradtag (K)", loc="left", fontsize=11)
    ax.legend(frameon=False, fontsize=8)
    ax = axs[1]
    ax.errorbar(
        x, empf["gw_je_cdd"], yerr=1.96 * empf["se_cdd"], fmt="o", color=FARBEN["basis"], capsize=3
    )
    ax.axhline(0, color=FARBEN["text"], linewidth=0.8)
    _achsen(ax)
    ax.set_title("Sommer: GW je Kühlgradtag (K)", loc="left", fontsize=11)
    fig.tight_layout()
    fig.savefig(ziel / "temperaturempfindlichkeit.png", dpi=150)
    plt.close(fig)


def grafik_spitzen(wj: pd.DataFrame, ziel: Path) -> None:
    fig, ax = plt.subplots(figsize=(10, 4.2))
    x = wj.index.to_numpy()
    ax.plot(
        x,
        wj["spitze_ungesteuert_gw"],
        color=FARBEN["ev"],
        linewidth=2,
        marker="o",
        markersize=4,
        label="Vollausbau, ungesteuertes Laden",
    )
    ax.plot(
        x,
        wj["spitze_gesteuert_gw"],
        color=FARBEN["gesteuert"],
        linewidth=2,
        marker="o",
        markersize=4,
        label="Vollausbau, gesteuertes Laden",
    )
    ax.plot(
        x,
        wj["spitze_basis_gw"],
        color=FARBEN["basis"],
        linewidth=2,
        marker="o",
        markersize=4,
        label="heutige Last (Modell)",
    )
    _achsen(ax)
    ax.set_ylim(0, None)
    ax.set_ylabel("GW", color=FARBEN["text"])
    ax.set_title("Jahreshöchstlast je Wetterjahr", loc="left", fontsize=11)
    ax.legend(frameon=False, loc="lower left", fontsize=9)
    fig.tight_layout()
    fig.savefig(ziel / "spitzen_wetterjahre.png", dpi=150)
    plt.close(fig)


def grafik_woche(sz: Szenario, zeitpunkt: str, ziel: Path, name: str, titel: str) -> None:
    st = sz.stunden.copy()
    st.index = lokal(st.index)
    mitte = pd.Timestamp(zeitpunkt, tz=TZ)
    d = st[(st.index >= mitte - pd.Timedelta(days=3)) & (st.index < mitte + pd.Timedelta(days=4))]
    fig, axs = plt.subplots(1, 2, figsize=(11, 4), sharey=True)
    for ax, spalte, untertitel in (
        (axs[0], "ev_frei", "ungesteuertes Laden"),
        (axs[1], "ev_gesteuert", "gesteuertes Laden"),
    ):
        ax.stackplot(
            d.index,
            d["basis"],
            d["wp"],
            d["gwp"],
            d[spalte],
            colors=[FARBEN["basis"], FARBEN["wp"], FARBEN["gwp"], FARBEN["ev"]],
            labels=["heutige Last", "Wärmepumpen Gebäude", "Großwärmepumpen Fernwärme", "E-Autos"],
            edgecolor="white",
            linewidth=0.3,
        )
        _achsen(ax)
        ax.set_title(untertitel, loc="left", fontsize=10)
        ax.xaxis.set_major_formatter(matplotlib.dates.DateFormatter("%d.%m.", tz=TZ))
    axs[0].set_ylabel("GW", color=FARBEN["text"])
    griffe, namen = axs[0].get_legend_handles_labels()
    fig.legend(griffe, namen, frameon=False, loc="lower center", ncol=4, fontsize=9)
    fig.suptitle(titel, x=0.01, ha="left", fontsize=11)
    fig.tight_layout(rect=(0, 0.07, 1, 1))
    fig.savefig(ziel / name, dpi=150)
    plt.close(fig)


def grafik_klima_spitzen(kl: pd.DataFrame, ziel: Path) -> None:
    fig, ax = plt.subplots(figsize=(10, 4.2))
    farben = [FARBEN["basis"], FARBEN["wp"], FARBEN["gwp"], FARBEN["ev"]]
    for (name, d), farbe in zip(kl.groupby("szenario", sort=False), farben, strict=False):
        ax.plot(
            d["wetterjahr"],
            d["spitze_gw"],
            color=farbe,
            linewidth=2,
            marker="o",
            markersize=3,
            label=name,
        )
    _achsen(ax)
    ax.set_ylabel("GW", color=FARBEN["text"])
    ax.set_title(
        "Sommerhöchstlast (Mai-Sep) je Wetterjahr, Klima und Bestand verschoben",
        loc="left",
        fontsize=11,
    )
    fig.legend(frameon=False, loc="lower center", fontsize=8, ncol=4)
    fig.tight_layout(rect=(0, 0.07, 1, 1))
    fig.savefig(ziel / "klima_sommerspitzen.png", dpi=150)
    plt.close(fig)


def grafik_klima_woche(stunden: dict[str, pd.DataFrame], zeitpunkt: str, ziel: Path) -> None:
    mitte = pd.Timestamp(zeitpunkt, tz=TZ)
    namen = list(stunden)
    fig, axs = plt.subplots(1, 2, figsize=(11, 4), sharey=True)
    for ax, name in zip(axs, (namen[0], namen[1]), strict=True):
        d = stunden[name].copy()
        d.index = lokal(d.index)
        d = d[(d.index >= mitte - pd.Timedelta(days=3)) & (d.index < mitte + pd.Timedelta(days=4))]
        ax.stackplot(
            d.index,
            d["basis"],
            d["klima_zusatz"],
            colors=[FARBEN["basis"], FARBEN["wp"]],
            labels=["heutige Last im Szenarioklima", "zusätzliche Klimaanlagen"],
            edgecolor="white",
            linewidth=0.3,
        )
        _achsen(ax)
        ax.set_title(name, loc="left", fontsize=10)
        ax.xaxis.set_major_formatter(matplotlib.dates.DateFormatter("%d.%m.", tz=TZ))
    axs[0].set_ylabel("GW", color=FARBEN["text"])
    griffe, beschr = axs[0].get_legend_handles_labels()
    fig.legend(griffe, beschr, frameon=False, loc="lower center", ncol=2, fontsize=9)
    fig.suptitle(f"Hitzewoche um {zeitpunkt}", x=0.01, ha="left", fontsize=11)
    fig.tight_layout(rect=(0, 0.07, 1, 1))
    fig.savefig(ziel / "klima_hitzewoche.png", dpi=150)
    plt.close(fig)


def grafik_pv_tag(
    df: pd.DataFrame, pv_cf: pd.Series, stufen: list[klima.Ausbaustufe], ziel: Path
) -> str:
    """Heißester Tag (ab 2015, PV-Profil vorhanden): Last, Klimaanlagen, PV, Residuallast."""
    d = df.join(pv_cf.rename("cf"), how="inner")
    lok = lokal(d.index)
    tage = pd.DatetimeIndex(lok.date)
    # Tagesmitteltemperatur, weil die Klimalast an vielen Tagen in die Sättigung läuft
    tag = d.groupby(tage)["temperatur"].mean().idxmax()
    # Tag plus Folgetag, damit der Abend vollständig ist
    m = (tage >= tag) & (tage <= tag + pd.Timedelta(days=1))
    d, lok = d[m], lok[m]
    last = d["basis"] + d["klima_zusatz"]
    fig, axs = plt.subplots(1, 2, figsize=(11, 4), sharey=True)
    for ax, stufe in zip(axs, (stufen[0], stufen[2]), strict=True):
        pv = d["cf"] * stufe.pv_gw
        r = (last - pv).to_numpy()
        nach = r.copy()
        tg = pd.DatetimeIndex(lok.date)
        for t_ in np.unique(tg):
            mm = np.asarray(tg == t_)
            ent = klima._kappen(r[mm], stufe.speicher_gwh * 0.9, stufe.speicher_gw)
            lad = klima._laden(r[mm], ent.sum() / 0.9, stufe.speicher_gw)
            nach[mm] = r[mm] - ent + lad
        ax.plot(lok, last, color=FARBEN["basis"], linewidth=2, label="Last mit Klimaanlagen")
        ax.plot(
            lok,
            d["klima_gesamt"],
            color=FARBEN["wp"],
            linewidth=2,
            label="davon Klimaanlagen Haushalte",
        )
        ax.plot(lok, pv, color=FARBEN["ev"], linewidth=2, label="PV-Erzeugung")
        ax.plot(
            lok, nach, color=FARBEN["gesteuert"], linewidth=2, label="Last minus PV, nach Speicher"
        )
        ax.axhline(0, color=FARBEN["text"], linewidth=0.8)
        _achsen(ax)
        ax.set_title(
            f"{stufe.name}: PV {stufe.pv_gw:.0f} GW, Speicher {stufe.speicher_gw:.0f} GW"
            f" / {stufe.speicher_gwh:.0f} GWh",
            loc="left",
            fontsize=9,
        )
        ax.xaxis.set_major_locator(matplotlib.dates.HourLocator(byhour=(0, 6, 12, 18), tz=TZ))
        ax.xaxis.set_major_formatter(matplotlib.dates.DateFormatter("%d.%m.\n%H h", tz=TZ))
    axs[0].set_ylabel("GW", color=FARBEN["text"])
    griffe, beschr = axs[0].get_legend_handles_labels()
    fig.legend(griffe, beschr, frameon=False, loc="lower center", ncol=4, fontsize=9)
    datum = str(pd.Timestamp(tag).strftime("%d.%m.%Y"))
    fig.suptitle(
        f"Heißester Tag {datum}, Klima und Bestand 2050 (Trend fortgesetzt)",
        x=0.01,
        ha="left",
        fontsize=11,
    )
    fig.tight_layout(rect=(0, 0.07, 1, 1))
    fig.savefig(ziel / "pv_speicher_hitzetag.png", dpi=150)
    plt.close(fig)
    return datum


def hitze_2026(lst_h: pd.Series, pred: pd.Series, tag: pd.DataFrame) -> pd.DataFrame:
    """Abweichung Ist minus Lastmodell an heißen und milden Sommertagen je Jahr."""
    res = (lst_h - pred).copy()
    res.index = lokal(res.index)
    t_tag = tag["t"].reindex(pd.DatetimeIndex(res.index.date)).to_numpy()
    df = pd.DataFrame({"res": res.to_numpy(), "t": t_tag, "h": res.index.hour}, index=res.index)
    df = df[df.index.month.isin((6, 7, 8))]
    zeilen = []
    for y in range(2023, 2027):
        d = df[df.index.year == y]
        heiss, mild = d[d["t"] >= 24], d[d["t"] < 20]
        zeilen.append(
            {
                "jahr": y,
                "heisse_tage": len(heiss) // 24,
                "abweichung_heiss_gw": heiss["res"].mean(),
                "abweichung_mild_gw": mild["res"].mean(),
                "abweichung_heiss_18_22_uhr_gw": heiss[heiss["h"].between(18, 22)]["res"].mean(),
                "zusatz_heiss_gegen_mild_gw": heiss["res"].mean() - mild["res"].mean(),
            }
        )
    return pd.DataFrame(zeilen).set_index("jahr")


# --------------------------------------------------------------------------- Ablauf


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--cache", type=Path, required=True)
    args = ap.parse_args()
    tab, graf = HIER / "tabellen", HIER / "grafiken"
    tab.mkdir(exist_ok=True)
    graf.mkdir(exist_ok=True)

    t = daten.temperatur(args.cache)
    lst = daten.last(args.cache, 2026)
    pkw = daten.pkw(args.cache)
    hhq = daten.haushalte_waerme(args.cache)
    tag = tagesmerkmale(t)

    jt, empf = rueckblick(lst, tag, pkw, hhq)
    jt.round(3).to_csv(tab / "rueckblick_jahre.csv")
    empf.round(4).to_csv(tab / "temperaturempfindlichkeit.csv")
    grafik_rueckblick(jt, graf)
    grafik_empfindlichkeit(empf, graf)

    lst_h = lst.reindex(t.index).dropna()
    beta, niveau, r2, rmse = basismodell(lst_h, tag)

    # Modellgüte der Jahresspitze: Modell gegen Ist 2015-2025 (Ist auf Niveau 2025 skaliert)
    ist = lst_h.copy()
    pred = pd.Series(merkmale(ist.index, tag) @ beta + niveau, index=ist.index)
    j_ist = lokal(ist.index).year
    abw = []
    for y in RUECKBLICK:
        m = j_ist == y
        faktor = ist[j_ist == 2025].mean() / ist[m].mean()
        abw.append(
            {
                "jahr": y,
                "spitze_ist_skaliert": ist[m].max() * faktor,
                "spitze_modell": pred[m].max(),
            }
        )
    abw_df = pd.DataFrame(abw).set_index("jahr")
    abw_df["differenz"] = abw_df["spitze_ist_skaliert"] - abw_df["spitze_modell"]
    abw_df.round(2).to_csv(tab / "basismodell_spitzen.csv")

    wetter = t[lokal(t.index).year <= 2025]
    tag_w = tag[tag.index.year <= 2025]
    hdd_ref = float(tag_w["hdd"].groupby(tag_w.index.year).sum().mean())
    a = Annahmen()
    sz = szenario(wetter, tag, beta, niveau, pkw, a, hdd_ref)
    wj = wetterjahre(sz)
    wj.round(2).to_csv(tab / "szenario_wetterjahre.csv")
    grafik_spitzen(wj, graf)
    schlimmst = wj["spitze_ungesteuert_gw"].idxmax()
    grafik_woche(
        sz,
        wj.loc[schlimmst, "zeitpunkt_spitze"],
        graf,
        "kaeltewelle.png",
        f"Kältester Fall: Woche um {wj.loc[schlimmst, 'zeitpunkt_spitze']} "
        f"(Wetter {schlimmst}), Vollausbau",
    )
    typisch = (wj["spitze_ungesteuert_gw"] - wj["spitze_ungesteuert_gw"].median()).abs().idxmin()
    grafik_woche(
        sz,
        wj.loc[typisch, "zeitpunkt_spitze"],
        graf,
        "typische_spitze.png",
        f"Mittlerer Winter: Woche um {wj.loc[typisch, 'zeitpunkt_spitze']} "
        f"(Wetter {typisch}), Vollausbau",
    )

    # Empfindlichkeit der Ergebnisse auf die Annahmen
    varianten = {
        "Mitte": a,
        "Sanierung -25 % Raumwärme": replace(a, sanierung=0.25),
        "mehr Sole-WP (40 %)": replace(a, anteil_luft=0.60),
        "mehr Flächenheizung (50 %)": replace(a, anteil_heizkoerper=0.50),
        "COP ohne Praxisabschlag": replace(a, cop_korrektur=1.0),
        "COP-Abschlag 0,75": replace(a, cop_korrektur=0.75),
        "E-Auto 17 kWh/100 km": replace(a, kwh_je_100km=17.0),
        "E-Auto 24 kWh/100 km": replace(a, kwh_je_100km=24.0),
        "Fernwärme ohne Großwärmepumpen": replace(a, gwp_anteil_fw=0.0),
        "Fernwärme fossil ganz aus Großwärmepumpen": replace(a, gwp_anteil_fw=1.0),
    }
    sens = []
    for name, v in varianten.items():
        s = sz if v is a else szenario(wetter, tag, beta, niveau, pkw, v, hdd_ref)
        w = wetterjahre(s)
        sens.append(
            {
                "variante": name,
                "zusatz_twh_mittel": (w["gesamt_twh"] - w["basis_twh"]).mean(),
                "spitze_ungesteuert_median_gw": w["spitze_ungesteuert_gw"].median(),
                "spitze_ungesteuert_max_gw": w["spitze_ungesteuert_gw"].max(),
                "spitze_gesteuert_median_gw": w["spitze_gesteuert_gw"].median(),
                "spitze_gesteuert_max_gw": w["spitze_gesteuert_gw"].max(),
                "jaz_wp": s.kennzahlen["jaz_wp"],
            }
        )
    pd.DataFrame(sens).set_index("variante").round(2).to_csv(tab / "sensitivitaet.csv")

    # Teil C: Klimaanlagen
    h26 = hitze_2026(lst_h, pred, tag)
    h26.round(3).to_csv(tab / "hitze_sommer_2023_2026.csv")
    ka = klima.Klimaanlagen()
    t24 = t[lokal(t.index).year == 2024]
    steigung = klima.eichen(t24, ka)
    kl_sz, kl_info = klima.szenarien(t, ka)

    def basis_fn(idx: pd.DatetimeIndex, tg: pd.DataFrame) -> np.ndarray:
        return np.asarray(merkmale(idx, tg) @ beta + niveau)

    kl, kl_stunden = klima.rechnen(t, tagesmerkmale, basis_fn, ka, kl_sz, steigung)
    kl.round(3).to_csv(tab / "klima_wetterjahre.csv", index=False)
    kl_sum = kl.groupby("szenario", sort=False).agg(
        delta_k=("delta_k", "first"),
        anteil_haushalte=("anteil_haushalte", "first"),
        klima_twh_mittel=("klima_twh", "mean"),
        spitze_basis_median_gw=("spitze_basis_gw", "median"),
        spitze_median_gw=("spitze_gw", "median"),
        spitze_max_gw=("spitze_gw", "max"),
        klima_max_gw=("klima_max_gw", "max"),
        klima_bei_spitze_median_gw=("klima_bei_spitze_gw", "median"),
    )
    kl_sum.round(2).to_csv(tab / "klima_szenarien.csv")
    pfad = klima.ausbaupfad(ka, kl_sz[1].anteil, kl_sz[1].anteil_mobil, (320_000, 480_000, 640_000))
    pfad.round(2).to_csv(tab / "klima_ausbaupfad.csv", index=False)
    grafik_klima_spitzen(kl, graf)
    trend = kl[kl["szenario"] == kl_sz[2].name]
    heissest = trend.loc[trend["spitze_gw"].idxmax(), "zeitpunkt"]
    grafik_klima_woche(
        {kl_sz[0].name: kl_stunden[kl_sz[0].name], kl_sz[2].name: kl_stunden[kl_sz[2].name]},
        heissest,
        graf,
    )

    # Teil D: PV und Speicher gegen die abendliche Klimalast
    inst = daten.installiert(args.cache)
    pv = daten.pv_erzeugung(args.cache, 2026)
    monat = pv.index.tz_convert(TZ).tz_localize(None).to_period("M")
    pv_cf = pv / inst["pv_gw"].reindex(monat).to_numpy()
    hw = daten.HANDWERTE
    jetzt = inst.iloc[-1]
    heim_je_gw = hw["heimspeicher_gwh_2026"] / float(jetzt["pv_gw"])

    def stufe(name: str, pv_gw: float, gross_gw: float) -> klima.Ausbaustufe:
        heim_gwh = heim_je_gw * pv_gw
        # Großspeicher 2 h, Heimspeicher 0,5 C
        return klima.Ausbaustufe(name, pv_gw, gross_gw + heim_gwh / 2, 2 * gross_gw + heim_gwh)

    stufen = [
        klima.Ausbaustufe(
            f"Stand {inst.index[-1]}",
            float(jetzt["pv_gw"]),
            float(jetzt["speicher_gw"]),
            float(jetzt["speicher_gwh"]),
        ),
        klima.Ausbaustufe(
            "PV NEP 2037 B, Speicher wie heute",
            hw["nep_b"][2037]["pv_gw"],
            float(jetzt["speicher_gw"]),
            float(jetzt["speicher_gwh"]),
        ),
        stufe("NEP 2037 B", hw["nep_b"][2037]["pv_gw"], hw["nep_b"][2037]["grossspeicher_gw"]),
        stufe("NEP 2045 B", hw["nep_b"][2045]["pv_gw"], hw["nep_b"][2045]["grossspeicher_gw"]),
    ]
    ps = klima.pv_speicher(kl_stunden, pv_cf.dropna(), stufen)
    ps.round(3).to_csv(tab / "pv_speicher_wetterjahre.csv", index=False)
    ps_sum = (
        ps.groupby(["klimaszenario", "ausbau", "mit_klima"], sort=False)
        .agg(
            rl_ohne_speicher_median=("rl_spitze_ohne_speicher_gw", "median"),
            rl_abend_ohne_speicher_median=("rl_abend_max_ohne_speicher_gw", "median"),
            rl_mit_speicher_median=("rl_spitze_mit_speicher_gw", "median"),
            rl_mit_speicher_max=("rl_spitze_mit_speicher_gw", "max"),
            stunde_spitze=("stunde_spitze_mit_speicher", lambda x: int(x.mode().iloc[0])),
        )
        .reset_index()
    )
    ps_sum.round(2).to_csv(tab / "pv_speicher_szenarien.csv", index=False)
    pv_tag = grafik_pv_tag(kl_stunden[kl_sz[2].name], pv_cf.dropna(), stufen, graf)
    pd.DataFrame([s.__dict__ for s in stufen]).round(1).to_csv(
        tab / "pv_speicher_stufen.csv", index=False
    )

    lauf = {
        "temperatur_von": str(t.index.min()),
        "temperatur_bis": str(t.index.max()),
        "last_von": str(lst.index.min()),
        "last_bis": str(lst.index.max()),
        "basismodell_r2": round(r2, 4),
        "basismodell_rmse_gw": round(rmse, 3),
        "heizgradtage_referenz": round(hdd_ref, 1),
        **{k: round(v, 4) for k, v in sz.kennzahlen.items()},
        "wetterjahr_hoechste_spitze": int(schlimmst),
        "wetterjahr_typisch": int(typisch),
        "klima_steigung_je_k": round(steigung, 4),
        **{k: round(v, 3) for k, v in kl_info.items()},
        "pv_hitzetag": pv_tag,
    }
    (tab / "lauf.json").write_text(json.dumps(lauf, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps(lauf, indent=2, ensure_ascii=False))
    print(jt.round(2).to_string())
    print(empf.round(3).to_string())
    print(abw_df.round(1).to_string())
    print(wj.round(1).to_string())
    print(pd.DataFrame(sens).round(1).to_string())
    print(h26.round(2).to_string())
    print(kl_sum.round(2).to_string())
    print(pfad.round(2).to_string())
    print(pd.DataFrame([s.__dict__ for s in stufen]).round(1).to_string())
    print(ps_sum.round(1).to_string())


if __name__ == "__main__":
    main()
