"""Teil C: Klimaanlagen der Haushalte in einem wärmeren Klima.

Ausbau der Klimaanlagen nach dem Sättigungsmodell von McNeil und Letschert (Anteil der
Haushalte mit Klimaanlage wächst mit den jährlichen Kühlgradtagen), auf die Ausstattung 2026
in Deutschland geeicht. Das Klima wird verschoben: Jede Stunde der Wetterjahre 1991-2026 wird
um ein festes Delta wärmer, sodass das Sommermittel dem Zielklima entspricht. Die Last je
Haushalt mit Klimaanlage hängt von der gedämpften Außentemperatur und der Tageszeit ab und ist
auf den Kühlstrom der Haushalte aus Eurostat (2024) geeicht.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

TZ = "Europe/Berlin"
SOMMER = (5, 6, 7, 8, 9)
JJA = (6, 7, 8)
KUEHLGRENZE = 18.0  # °C Tagesmittel, wie bei McNeil und Letschert
EINSCHALT = 22.0  # °C gedämpfte Außentemperatur, ab der Geräte anlaufen

# Anwesenheit / Nutzung je lokale Stunde (Schlafzimmer nachts, Wohnräume abends)
NUTZUNG = np.array(
    [0.55, 0.50, 0.45, 0.40, 0.35, 0.30, 0.30, 0.35, 0.40, 0.45, 0.50, 0.55,
     0.60, 0.65, 0.70, 0.80, 0.90, 1.00, 1.00, 1.00, 0.95, 0.85, 0.75, 0.65]
)  # fmt: skip


def saettigung(cdd: float | np.ndarray) -> Any:
    """McNeil und Letschert (2007): maximaler Ausstattungsgrad bei cdd Kühlgradtagen (18 °C)."""
    return 1 - 0.949 * np.exp(-0.00187 * np.asarray(cdd))


@dataclass(frozen=True)
class Klimaanlagen:
    """Bestand und Technik (Mittelwerte, Quellen in daten.HANDWERTE)."""

    haushalte: float = 40.8e6
    anteil_2026: float = 0.234
    anteil_mobil_2026: float = 0.692
    anteil_2024: float = 0.14  # Annahme: zwischen 0 und dem Umfragewert 2025 (17,9 %)
    kuehlstrom_hh_2024_twh: float = 1.33  # Eurostat nrg_d_hhq, Raumkühlung Strom
    kw_mobil: float = 0.90  # elektrisch bei Volllast, ~2 kW Kälte, EER ~2,2
    kw_split: float = 0.65  # ~2,5 kW Kälte, EER ~3,8


def gedaempft(t: pd.Series) -> np.ndarray:
    """Gebäudeträgheit: halb Stundenwert, halb gleitendes 24-h-Mittel."""
    return np.asarray(0.5 * t.to_numpy() + 0.5 * t.rolling(24, min_periods=1).mean().to_numpy())


def nutzung(t: pd.Series, steigung: float) -> np.ndarray:
    stunde = t.index.tz_convert(TZ).hour.to_numpy()
    roh = np.clip(steigung * (gedaempft(t) - EINSCHALT), 0, 1)
    return np.asarray(roh * NUTZUNG[stunde])


def eichen(t_2024: pd.Series, k: Klimaanlagen) -> float:
    """Steigung so, dass der Kühlstrom der Haushalte 2024 dem Eurostat-Wert entspricht."""
    n = k.haushalte * k.anteil_2024
    kw = k.anteil_mobil_2026 * k.kw_mobil + (1 - k.anteil_mobil_2026) * k.kw_split
    ziel_kwh = k.kuehlstrom_hh_2024_twh * 1e9 / n
    lo, hi = 0.001, 5.0
    for _ in range(60):
        mitte = (lo + hi) / 2
        if kw * nutzung(t_2024, mitte).sum() < ziel_kwh:
            lo = mitte
        else:
            hi = mitte
    return (lo + hi) / 2


@dataclass(frozen=True)
class Klimaszenario:
    name: str
    delta_k: float  # Verschiebung gegenüber den beobachteten Wetterjahren
    anteil: float  # Haushalte mit Klimaanlage
    anteil_mobil: float


def jja_mittel(t: pd.Series) -> pd.Series:
    lok = t.tz_convert(TZ)
    tag = lok.groupby(lok.index.date).mean()
    tag.index = pd.to_datetime(tag.index)
    s = tag[tag.index.month.isin(JJA)]
    return s.groupby(s.index.year).mean()


def kuehlgradtage(t: pd.Series) -> pd.Series:
    lok = t.tz_convert(TZ)
    tag = lok.groupby(lok.index.date).mean()
    tag.index = pd.to_datetime(tag.index)
    cdd = (tag - KUEHLGRENZE).clip(lower=0)
    return cdd.groupby(cdd.index.year).sum()


def szenarien(t: pd.Series, k: Klimaanlagen) -> tuple[list[Klimaszenario], dict[str, float]]:
    """Klimazustände aus dem beobachteten Trend; Ausstattung nach McNeil/Letschert."""
    jja = jja_mittel(t)
    jja = jja[jja.index <= 2026]
    p = np.polyfit(jja.index.to_numpy(), jja.to_numpy(), 1)
    ref_1991_2020 = float(jja.loc[1991:2020].mean())
    mittel_wetter = float(jja.mean())
    trend_2026 = float(np.polyval(p, 2026))
    trend_2050 = float(np.polyval(p, 2050))
    # gedämpft: ab 2026 halbe Trendsteigung
    gedaempft_2050 = trend_2026 + 0.5 * (trend_2050 - trend_2026)

    def cdd_mittel(delta: float) -> float:
        return float(kuehlgradtage(t + delta).loc[1991:2026].mean())

    d_heute = trend_2026 - mittel_wetter
    d_mitte = gedaempft_2050 - mittel_wetter
    d_trend = trend_2050 - mittel_wetter
    cdd = {
        n: cdd_mittel(d) for n, d in (("heute", d_heute), ("mitte", d_mitte), ("trend", d_trend))
    }
    s_heute = saettigung(cdd["heute"])

    def anteil(name: str) -> float:
        return float(k.anteil_2026 * saettigung(cdd[name]) / s_heute)

    sz = [
        Klimaszenario("Bestand 2026, Klima 2026", d_heute, k.anteil_2026, k.anteil_mobil_2026),
        Klimaszenario("2050 realistisch (halber Trend)", d_mitte, anteil("mitte"), 0.45),
        Klimaszenario("2050 Trend fortgesetzt", d_trend, anteil("trend"), 0.45),
        Klimaszenario("Obergrenze: halbe Haushalte, 2050 Trend", d_trend, 0.50, 0.40),
    ]
    info = {
        "jja_trend_k_je_jahrzehnt": float(p[0] * 10),
        "jja_1991_2020": ref_1991_2020,
        "jja_mittel_wetterjahre": mittel_wetter,
        "jja_trend_2026": trend_2026,
        "jja_2050_halber_trend": gedaempft_2050,
        "jja_2050_trend": trend_2050,
        "cdd_klima_2026": cdd["heute"],
        "cdd_2050_halber_trend": cdd["mitte"],
        "cdd_2050_trend": cdd["trend"],
        "saettigung_mcneil_2026": float(s_heute),
    }
    return sz, info


def rechnen(
    t: pd.Series,
    tag_fn: Callable[[pd.Series], pd.DataFrame],
    basis_fn: Callable[[pd.DatetimeIndex, pd.DataFrame], np.ndarray],
    k: Klimaanlagen,
    sz: list[Klimaszenario],
    steigung: float,
) -> tuple[pd.DataFrame, dict[str, pd.DataFrame]]:
    """Sommerspitzen je Szenario und Wetterjahr; Basis = heutiges Lastmodell im neuen Klima."""
    n_basis = k.haushalte * k.anteil_2024
    kw_basis = k.anteil_mobil_2026 * k.kw_mobil + (1 - k.anteil_mobil_2026) * k.kw_split
    zeilen = []
    stunden: dict[str, pd.DataFrame] = {}
    lok_jahr = t.index.tz_convert(TZ)
    sommer = lok_jahr.month.isin(SOMMER) & (lok_jahr.year <= 2026)
    for s in sz:
        tv = t + s.delta_k
        tag = tag_fn(tv)
        basis = basis_fn(tv.index, tag)
        u = nutzung(tv, steigung)
        kw = s.anteil_mobil * k.kw_mobil + (1 - s.anteil_mobil) * k.kw_split
        n = k.haushalte * s.anteil
        ac_gesamt = n * kw * u / 1e6  # GW
        ac_zusatz = ac_gesamt - n_basis * kw_basis * u / 1e6
        df = pd.DataFrame(
            {
                "basis": basis,
                "klima_zusatz": ac_zusatz,
                "klima_gesamt": ac_gesamt,
                "temperatur": tv.to_numpy(),
            },
            index=t.index,
        )[sommer]
        stunden[s.name] = df
        jahr = df.index.tz_convert(TZ).year
        for y in sorted(set(jahr)):
            d = df[jahr == y]
            summe = d["basis"] + d["klima_zusatz"]
            i = summe.idxmax()
            zeilen.append(
                {
                    "szenario": s.name,
                    "wetterjahr": y,
                    "delta_k": s.delta_k,
                    "anteil_haushalte": s.anteil,
                    "klima_twh": d["klima_gesamt"].sum() / 1000,
                    "spitze_basis_gw": d["basis"].max(),
                    "spitze_gw": summe.max(),
                    "zeitpunkt": i.tz_convert(TZ).strftime("%Y-%m-%d %H:00"),
                    "klima_bei_spitze_gw": d.loc[i, "klima_gesamt"],
                    "klima_max_gw": d["klima_gesamt"].max(),
                    "tmax": d["temperatur"].max(),
                }
            )
    return pd.DataFrame(zeilen), stunden


def ausbaupfad(
    k: Klimaanlagen,
    ziel_anteil: float,
    ziel_mobil: float,
    raten: tuple[int, ...],
    lebensdauer: float = 15.0,
) -> pd.DataFrame:
    """Jahre bis zum Ziel-Bestand an Split-Haushalten bei begrenzter Installationsleistung.

    Jede Installation ersetzt zuerst Altgeräte (Bestand / Lebensdauer), nur der Rest
    erweitert den Bestand.
    """
    split_heute = k.haushalte * k.anteil_2026 * (1 - k.anteil_mobil_2026)
    split_ziel = k.haushalte * ziel_anteil * (1 - ziel_mobil)
    zeilen = []
    for r in raten:
        bestand, jahre = split_heute, 0
        while bestand < split_ziel and jahre < 100:
            bestand += r - bestand / lebensdauer
            jahre += 1
        gleichgewicht = r * lebensdauer
        zeilen.append(
            {
                "installationen_je_jahr": r,
                "split_haushalte_heute_mio": split_heute / 1e6,
                "split_haushalte_ziel_mio": split_ziel / 1e6,
                "jahre_bis_ziel": jahre if bestand >= split_ziel else None,
                "bestand_im_gleichgewicht_mio": gleichgewicht / 1e6,
            }
        )
    return pd.DataFrame(zeilen)


@dataclass(frozen=True)
class Ausbaustufe:
    name: str
    pv_gw: float
    speicher_gw: float
    speicher_gwh: float


def _kappen(r: np.ndarray, energie: float, leistung: float) -> np.ndarray:
    """Spitzenkappung: entlädt höchstens leistung, insgesamt energie, auf die höchsten Stunden."""
    if energie <= 0 or leistung <= 0:
        return np.zeros_like(r)
    lo, hi = float(r.min()) - leistung, float(r.max())
    for _ in range(50):
        lam = (lo + hi) / 2
        e = np.minimum(np.maximum(r - lam, 0), leistung).sum()
        if e > energie:
            lo = lam
        else:
            hi = lam
    return np.asarray(np.minimum(np.maximum(r - hi, 0), leistung))


def _laden(r: np.ndarray, energie: float, leistung: float) -> np.ndarray:
    """Talfüllung: lädt höchstens leistung, insgesamt energie, in die niedrigsten Stunden."""
    return -_kappen(-r, energie, leistung)


def pv_speicher(
    stunden: dict[str, pd.DataFrame],
    pv_cf: pd.Series,
    stufen: list[Ausbaustufe],
    wirkungsgrad: float = 0.9,
) -> pd.DataFrame:
    """Sommerliche Residuallast (Last minus PV) mit und ohne zusätzliche Klimaanlagen.

    Speicher je Tag: laden in den Stunden mit der niedrigsten Residuallast, entladen gegen die
    höchste (Tagesfahrplan mit perfekter Voraussicht, Wirkungsgrad wirkungsgrad).
    """
    zeilen = []
    for name, df in stunden.items():
        d = df.join(pv_cf.rename("cf"), how="inner")
        lok = d.index.tz_convert(TZ)
        for stufe in stufen:
            pv = d["cf"].to_numpy() * stufe.pv_gw
            for mit_klima in (False, True):
                r = d["basis"].to_numpy() - pv
                if mit_klima:
                    r = r + d["klima_zusatz"].to_numpy()
                nach = r.copy()
                tage = pd.DatetimeIndex(lok.date)
                for tag in np.unique(tage):
                    m = np.asarray(tage == tag)
                    rt = r[m]
                    ent = _kappen(rt, stufe.speicher_gwh * wirkungsgrad, stufe.speicher_gw)
                    lad = _laden(rt, ent.sum() / wirkungsgrad, stufe.speicher_gw)
                    nach[m] = rt - ent + lad
                for jahr in sorted(set(lok.year)):
                    mj = np.asarray(lok.year == jahr)
                    abend = mj & np.asarray((lok.hour >= 18) & (lok.hour <= 23))
                    zeilen.append(
                        {
                            "klimaszenario": name,
                            "ausbau": stufe.name,
                            "mit_klima": mit_klima,
                            "wetterjahr": jahr,
                            "rl_spitze_ohne_speicher_gw": r[mj].max(),
                            "rl_spitze_mit_speicher_gw": nach[mj].max(),
                            "rl_abend_max_ohne_speicher_gw": r[abend].max(),
                            "stunde_spitze_mit_speicher": int(lok.hour[mj][np.argmax(nach[mj])]),
                        }
                    )
    return pd.DataFrame(zeilen)
