"""PV-Speicher, Szenario 2027: Haushalts-Börsenpreis (H0) +5 ct, Solarmittag weiter bei 0 ct.

Wiederholt die Rechnung aus ``docs/analysen/pv-speicher-arbitrage`` (10 kWp, 45° Süd, Speicher
10/10, 20/15, 30/15 kWh/kW) mit den Preisen und dem PV-Wetter von 2025, aber mit umgebauter
Preisreihe:

* Der mit dem Standardlastprofil H0 gewichtete Durchschnittspreis liegt 5 ct/kWh über 2025.
* In Solarstunden fällt der Preis trotzdem auf 0 ct (negative Preise bleiben negativ).
* Den Aufschlag tragen die übrigen Stunden.

Solarstunden kommen aus dem PV-Anteil an der deutschen Last im Jahr 2025: Ab 50 % PV-Anteil
zählt die Stunde voll als Solarstunde, darunter anteilig (linear bis 0 % PV-Anteil). Für jede
Stunde ist der Szenariopreis::

    p' = (1 - w) * (p + a) + w * min(p, 0),    w = clip(PV-Anteil / 0,5, 0, 1)

mit einem Aufschlag a, der so gewählt ist, dass der H0-gewichtete Mittelwert um 5 ct steigt.
Ergebnisse liegen daneben (README.md, tabellen/, grafiken/). Aufruf aus dem
Repository-Wurzelverzeichnis::

    uv run --with pandas --with pyarrow --with scipy --with matplotlib \\
        python docs/analysen/pv-speicher-szenario-2027/auswertung.py

``daten/h0_dyn_2025.csv`` ist das dynamisierte BDEW-H0-Profil für 2025 in Ortszeit (1000 kWh/a,
stündlich), erzeugt mit demandlib (siehe README).
"""

from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import numpy as np
import pandas as pd

HIER = Path(__file__).resolve().parent
TZ = "Europe/Berlin"
JAHR = 2025
AUFSCHLAG_H0 = 0.05  # EUR/kWh auf den H0-gewichteten Mittelwert
PV_ANTEIL_VOLL = 0.5  # ab diesem PV-Anteil an der Last gilt eine Stunde voll als Solarstunde


def _lade_basis() -> ModuleType:
    """Das Skript der Speicher-Analyse als Modul laden (beide heißen auswertung.py)."""
    pfad = HIER.parent / "pv-speicher-arbitrage" / "auswertung.py"
    spec = importlib.util.spec_from_file_location("pv_speicher_arbitrage", pfad)
    assert spec is not None and spec.loader is not None
    modul = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = modul  # dataclasses schlagen das Modul dort nach
    spec.loader.exec_module(modul)
    return modul


basis = _lade_basis()


def h0_profil(index: pd.DatetimeIndex) -> pd.Series:
    """H0-Gewichte (kWh je Stunde) auf einen UTC-Index gelegt, über die Ortszeit."""
    h0 = pd.read_csv(HIER / "daten" / "h0_dyn_2025.csv", parse_dates=["lokalzeit"])
    h0 = h0.set_index("lokalzeit").h0_kwh
    lokal = index.tz_convert(TZ).tz_localize(None).floor("h")
    werte = h0.groupby(h0.index).mean().reindex(lokal)
    # Zeitumstellung: fehlende Stunde aus der Nachbarstunde
    return pd.Series(werte.ffill().to_numpy(), index=index)


def solargewicht(solar: pd.Series, last: pd.Series) -> pd.Series:
    anteil = (solar / last).clip(lower=0)
    return (anteil / PV_ANTEIL_VOLL).clip(upper=1.0)


def szenario(
    preis: pd.Series, w: pd.Series, h0: pd.Series, im_jahr: np.ndarray, solar_null: bool
) -> tuple[pd.Series, float]:
    """Preisreihe mit H0-Mittel +5 ct im Jahr.

    ``solar_null``: Solarstunden auf min(p, 0), sonst bleibt ihr Preis von 2025.
    """
    p_solar = preis.clip(upper=0) if solar_null else preis
    ohne_a = (1 - w) * preis + w * p_solar
    # H0-Mittel(ohne_a + (1-w)*a) = H0-Mittel(preis) + 5 ct  ->  a (aus dem Jahr, gilt auch am Rand)
    hj = h0[im_jahr]
    ziel = np.average(preis[im_jahr], weights=hj) + AUFSCHLAG_H0
    a = (ziel - np.average(ohne_a[im_jahr], weights=hj)) / np.average(1 - w[im_jahr], weights=hj)
    return ohne_a + (1 - w) * a, float(a)


def rechnen(df: pd.DataFrame, label: str) -> list[dict[str, object]]:
    lokal_jahr = df.index.tz_convert(TZ).year
    im_jahr = lokal_jahr == JAHR
    direkt = basis.direkt(df)[im_jahr].sum()
    pvj = df.pv[im_jahr]
    prog = basis.prognose(df.pv)
    zeilen: list[dict[str, object]] = []
    for sp in basis.SPEICHER:
        schlecht = basis.Speicher(sp.name, sp.kwh, sp.kw, 0.92, 0.92, 0.05)

        def mehr(erg: pd.DataFrame, direkt: float = direkt) -> float:
            return float(erg[erg.index.tz_convert(TZ).year == JAHR].erloes.sum() - direkt)

        erg = basis.rollierend(df, sp)
        ej = erg[erg.index.tz_convert(TZ).year == JAHR]
        z: dict[str, object] = {
            "szenario": label,
            "speicher": sp.name,
            "pv_kwh": float(pvj.sum()),
            "marktwert_pv_ct": float(
                basis.direkt(df, abregeln=False)[im_jahr].sum() / pvj.sum() * 100
            ),
            "direkt_eur": float(direkt),
            "speicher_eur": float(ej.erloes.sum()),
            "mehr_eur": mehr(erg),
            "mehr_prognosefehler_eur": mehr(basis.rollierend(df, sp, pv_prognose=prog)),
            "mehr_wirkungsgrad_85_eur": mehr(basis.rollierend(df, schlecht)),
            "zyklen": float(ej.d.sum() / sp.eta_entl / sp.kwh),
        }
        zeilen.append(z)
        print(z)
    return zeilen


def grafiken(preise: dict[str, pd.Series], tab: pd.DataFrame, ziel: Path) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    ziel.mkdir(parents=True, exist_ok=True)
    stil = {
        "2025": ("#7f7f7f", "-"),
        "2027 Solar 0 ct": ("#e15759", "-"),
        "2027 Solar wie 2025": ("#f28e2b", "--"),
        "2025 +5 ct gleichmäßig": ("#4e79a7", ":"),
    }
    fig, axs = plt.subplots(1, 2, figsize=(11, 4.3), sharey=True)
    for ax, (titel, monate) in zip(
        axs,
        (("April bis September", range(4, 10)), ("Oktober bis März", (10, 11, 12, 1, 2, 3))),
        strict=True,
    ):
        for name, p in preise.items():
            q = p.copy()
            q.index = q.index.tz_convert(TZ)
            q = q[(q.index.year == JAHR) & q.index.month.isin(list(monate))]
            farbe, ls = stil[name]
            ax.plot(q.groupby(q.index.hour).mean() * 100, color=farbe, ls=ls, label=name)
        ax.set_title(f"Mittleres Tagesprofil {titel}")
        ax.set_xlabel("Stunde (Ortszeit)")
        ax.grid(alpha=0.3)
    axs[0].set_ylabel("Day-Ahead-Preis [ct/kWh]")
    axs[0].legend(fontsize=8)
    fig.tight_layout()
    fig.savefig(ziel / "preisprofil.png", dpi=130)
    plt.close(fig)

    fig, ax = plt.subplots(figsize=(9, 4.5))
    namen = list(preise)
    x = np.arange(len(basis.SPEICHER))
    b = 0.8 / len(namen)
    for k, name in enumerate(namen):
        t = tab[tab.szenario == name].set_index("speicher").loc[[s.name for s in basis.SPEICHER]]
        ax.bar(x + (k - (len(namen) - 1) / 2) * b, t.mehr_eur, b, color=stil[name][0], label=name)
    ax.set_xticks(x, [f"{s.name} kWh/kW" for s in basis.SPEICHER])
    ax.set_ylabel("Mehrerlös gegenüber Direkteinspeisung [EUR/Jahr]")
    ax.set_title("10 kWp, 45° Süd, Wetter und Preise 2025")
    ax.grid(axis="y", alpha=0.3)
    ax.legend(fontsize=8)
    fig.tight_layout()
    fig.savefig(ziel / "mehrerloes.png", dpi=130)
    plt.close(fig)


def main() -> None:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument("--energy", type=Path, default=Path(".ml-data/snap-20261022/energy.parquet"))
    ap.add_argument("--pvgis", type=Path, default=Path(".ml-data/pvgis/mitte.json"))
    args = ap.parse_args()

    preis, solar = basis.lade_energie(args.energy)
    d = pd.read_parquet(args.energy, columns=["ts", "value", "code"])
    last = d[d.code == "load_actual"].set_index("ts").value.sort_index()
    pv_pvgis = basis.lade_pvgis(args.pvgis)
    pv = pv_pvgis.reindex(preis.index).fillna(
        basis.nationales_profil(solar, pv_pvgis).reindex(preis.index)
    )

    # Silvester 2024 bis 2.1.2026, damit der 1.1. voll geplant ist
    von = pd.Timestamp(f"{JAHR - 1}-12-31 00:00", tz=TZ)
    bis = pd.Timestamp(f"{JAHR + 1}-01-02 00:00", tz=TZ)
    df = pd.DataFrame({"preis": preis, "pv": pv, "solar": solar, "last": last})
    df = df[(df.index >= von) & (df.index < bis)].dropna()
    w = solargewicht(df.solar, df["last"])
    h0 = h0_profil(df.index)
    im_jahr = np.asarray(df.index.tz_convert(TZ).year == JAHR)

    preise: dict[str, pd.Series] = {"2025": df.preis}
    kennzahlen = []
    for name, solar_null in (("2027 Solar 0 ct", True), ("2027 Solar wie 2025", False)):
        preise[name], a = szenario(df.preis, w, h0, im_jahr, solar_null)
        kennzahlen.append({"szenario": name, "aufschlag_ct": a * 100})
    preise["2025 +5 ct gleichmäßig"] = df.preis + AUFSCHLAG_H0

    for name, p in preise.items():
        pj, hj, wj = p[im_jahr], h0[im_jahr], w[im_jahr]
        k = next(
            (z for z in kennzahlen if z["szenario"] == name),
            {"szenario": name, "aufschlag_ct": np.nan},
        )
        k.update(
            {
                "mittel_ct": float(pj.mean() * 100),
                "h0_mittel_ct": float(np.average(pj, weights=hj) * 100),
                "solarstunden_mittel_ct": float(pj[wj >= 1].mean() * 100),
                "uebrige_mittel_ct": float(pj[wj < 0.02].mean() * 100),
                "negative_stunden": int((pj < 0).sum()),
            }
        )
        if k not in kennzahlen:
            kennzahlen.append(k)
    tabellen = HIER / "tabellen"
    tabellen.mkdir(exist_ok=True)
    pd.DataFrame(kennzahlen).to_csv(tabellen / "preise.csv", index=False, float_format="%.2f")
    print(pd.DataFrame(kennzahlen))
    print(
        "Solarstunden voll:",
        int((w[im_jahr] >= 1).sum()),
        "anteilig:",
        int(((w > 0) & (w < 1))[im_jahr].sum()),
    )

    zeilen = []
    for name, p in preise.items():
        zeilen += rechnen(pd.DataFrame({"preis": p, "pv": df.pv}), name)
    tab = pd.DataFrame(zeilen)
    tab.to_csv(tabellen / "ergebnis.csv", index=False, float_format="%.2f")
    grafiken(preise, tab, HIER / "grafiken")


if __name__ == "__main__":
    main()
