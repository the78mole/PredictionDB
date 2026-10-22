"""PV-Ausrichtung mit Speicher: 19,9 kWp und 50 kWh / 25 kW, Süd gegen Ost-West gegen Fassade.

Drei Belegungen derselben 19,9 kWp, alle Dachflächen 45° geneigt:

* ``sued``: 100 % Süd (Dach 45°)
* ``ost_west``: 50 % Ost, 50 % West (Dach 45°)
* ``ost_west_fassade``: 40 % Ost, 40 % West (Dach 45°), 20 % Südfassade (90°)

Für jede Belegung: Jahresertrag, Erlös bei Direkteinspeisung zum Day-Ahead-Preis DE-LU und
Mehrerlös mit Speicher 50 kWh / 25 kW. Fahrplan, Speicher- und PV-Modell kommen unverändert aus
``docs/analysen/pv-speicher-arbitrage/auswertung.py`` (13-Uhr-Planung bis Mitternacht des
Folgetags). Ergebnisse liegen daneben (README.md, tabellen/, grafiken/). Aufruf aus dem
Repository-Wurzelverzeichnis::

    uv run --with pandas --with pyarrow --with scipy --with matplotlib \\
        python docs/analysen/pv-ausrichtung-speicher/auswertung.py

Eingaben: ``energy.parquet`` aus ``ml-train snapshot`` und PVGIS-``seriescalc``-JSON für 1 kWp
je Fläche (Standort 50,9° N / 10,0° O, 14 % Verluste), siehe README.
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
KWP = 19.9


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
SPEICHER = basis.Speicher("50/25", 50, 25)

# Flächen: Datei-Kürzel -> (Neigung, Azimut) nur zur Doku; Anteile an KWP je Belegung
FLAECHEN = {"sued45": "Süd 45°", "ost45": "Ost 45°", "west45": "West 45°", "fassade90": "Süd 90°"}
BELEGUNGEN: dict[str, dict[str, float]] = {
    "sued": {"sued45": 1.0},
    "ost_west": {"ost45": 0.5, "west45": 0.5},
    "ost_west_fassade": {"ost45": 0.4, "west45": 0.4, "fassade90": 0.2},
}
NAMEN = {
    "sued": "Süd 45°",
    "ost_west": "Ost-West 50/50",
    "ost_west_fassade": "Ost-West 40/40 + Fassade 20",
}
FARBEN = {"sued": "#f28e2b", "ost_west": "#4e79a7", "ost_west_fassade": "#59a14f"}


def pv_reihe(pvgis: dict[str, pd.Series], anteile: dict[str, float]) -> pd.Series:
    """PVGIS-Reihe der Belegung in kW (Summe der Flächen, je 1 kWp skaliert)."""
    return sum(pvgis[f] * anteil * KWP for f, anteil in anteile.items())


def jahr_teil(df: pd.DataFrame, j: int) -> tuple[pd.DataFrame, np.ndarray]:
    """Jahr j samt Silvester des Vorjahres (damit der 1.1. voll geplant ist)."""
    von = pd.Timestamp(f"{j - 1}-12-31 00:00", tz=TZ)
    bis = pd.Timestamp(f"{j + 1}-01-02 00:00", tz=TZ)
    teil = df[(df.index >= von) & (df.index < bis)].dropna()
    return teil, np.asarray(teil.index.tz_convert(TZ).year == j)


def auswerten(
    df: pd.DataFrame, jahre: list[int], belegung: str
) -> tuple[pd.DataFrame, dict[int, pd.DataFrame]]:
    zeilen = []
    reihen: dict[int, pd.DataFrame] = {}
    for j in jahre:
        teil, im_jahr = jahr_teil(df, j)
        direkt = basis.direkt(teil)[im_jahr].sum()
        ohne = basis.direkt(teil, abregeln=False)[im_jahr]
        pvj = teil.pv[im_jahr]

        def mehr(erg: pd.DataFrame, j: int = j, direkt: float = direkt) -> float:
            return float(erg[erg.index.tz_convert(TZ).year == j].erloes.sum() - direkt)

        erg = basis.rollierend(teil, SPEICHER)
        ej = erg[erg.index.tz_convert(TZ).year == j]
        reihen[j] = ej
        schlecht = basis.Speicher("50/25", 50, 25, 0.92, 0.92, 0.05)
        z = {
            "belegung": belegung,
            "jahr": j,
            "pv_kwh": float(pvj.sum()),
            "spitze_kw": float(pvj.max()),
            "marktwert_pv_ct": float(ohne.sum() / pvj.sum() * 100),
            "direkt_eur": float(direkt),
            "speicher_eur": float(ej.erloes.sum()),
            "mehr_eur": mehr(erg),
            "mehr_prognosefehler_eur": mehr(
                basis.rollierend(teil, SPEICHER, pv_prognose=basis.prognose(teil.pv))
            ),
            "mehr_wirkungsgrad_85_eur": mehr(basis.rollierend(teil, schlecht)),
            "zyklen": float(ej.d.sum() / SPEICHER.eta_entl / SPEICHER.kwh),
            "abgeregelt_direkt_kwh": float(pvj[teil.preis[im_jahr] < 0].sum()),
        }
        zeilen.append(z)
        print(belegung, j, {k: round(v, 1) for k, v in z.items() if isinstance(v, float)})
    return pd.DataFrame(zeilen), reihen


def grafiken(
    tab: pd.DataFrame,
    pv_pvgis: dict[str, pd.Series],
    pv: dict[str, pd.Series],
    reihen: dict[str, dict[int, pd.DataFrame]],
    preis: pd.Series,
    ziel: Path,
) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    ziel.mkdir(parents=True, exist_ok=True)
    jahre = sorted(tab.jahr.unique())
    x = np.arange(len(jahre))
    b = 0.27

    # Erlös je Jahr: Direkt (hell) und mit Speicher (voll)
    fig, ax = plt.subplots(figsize=(9, 4.8))
    for k, bel in enumerate(BELEGUNGEN):
        t = tab[tab.belegung == bel].set_index("jahr").loc[jahre]
        ax.bar(x + (k - 1) * b, t.speicher_eur, b, color=FARBEN[bel], alpha=0.35)
        ax.bar(x + (k - 1) * b, t.direkt_eur, b, color=FARBEN[bel], label=NAMEN[bel])
    ax.set_xticks(x, [str(j) for j in jahre])
    ax.set_ylabel("Erlös [EUR/Jahr]")
    ax.set_title("19,9 kWp: Erlös direkt (voll) und mit Speicher 50/25 (hell darüber)")
    ax.grid(axis="y", alpha=0.3)
    ax.legend()
    fig.tight_layout()
    fig.savefig(ziel / "erloes_jahr.png", dpi=130)
    plt.close(fig)

    # Mehrerlös je Jahr
    fig, ax = plt.subplots(figsize=(9, 4.5))
    for k, bel in enumerate(BELEGUNGEN):
        t = tab[tab.belegung == bel].set_index("jahr").loc[jahre]
        ax.bar(x + (k - 1) * b, t.mehr_eur, b, color=FARBEN[bel], label=NAMEN[bel])
    ax.set_xticks(x, [str(j) for j in jahre])
    ax.set_ylabel("Mehrerlös durch Speicher [EUR/Jahr]")
    ax.set_title("Mehrerlös des Speichers 50 kWh / 25 kW gegenüber Direkteinspeisung")
    ax.grid(axis="y", alpha=0.3)
    ax.legend()
    fig.tight_layout()
    fig.savefig(ziel / "mehrerloes_jahr.png", dpi=130)
    plt.close(fig)

    # Ertrag je Monat, Mittel der PVGIS-Jahre 2005-2023
    fig, ax = plt.subplots(figsize=(9, 4.5))
    for k, bel in enumerate(BELEGUNGEN):
        s = pv_reihe(pv_pvgis, BELEGUNGEN[bel])
        s.index = s.index.tz_convert(TZ)
        mon = s.groupby([s.index.year, s.index.month]).sum().groupby(level=1).mean()
        ax.bar(mon.index + (k - 1) * b, mon, b, color=FARBEN[bel], label=NAMEN[bel])
    ax.set_xticks(range(1, 13))
    ax.set_xlabel("Monat")
    ax.set_ylabel("Ertrag [kWh]")
    ax.set_title("Ertrag je Monat, Mittel 2005-2023 (PVGIS)")
    ax.grid(axis="y", alpha=0.3)
    ax.legend()
    fig.tight_layout()
    fig.savefig(ziel / "ertrag_monat.png", dpi=130)
    plt.close(fig)

    # Mittleres Tagesprofil Juni und Dezember, dazu der Preis 2025
    fig, axs = plt.subplots(1, 2, figsize=(11, 4.3), sharey=False)
    p = preis.copy()
    p.index = p.index.tz_convert(TZ)
    for ax, monat, titel in ((axs[0], 6, "Juni"), (axs[1], 12, "Dezember")):
        for bel in BELEGUNGEN:
            s = pv_reihe(pv_pvgis, BELEGUNGEN[bel])
            s.index = s.index.tz_convert(TZ)
            s = s[s.index.month == monat]
            ax.plot(s.groupby(s.index.hour).mean(), color=FARBEN[bel], label=NAMEN[bel])
        ax.set_title(f"{titel}: mittlere Leistung (2005-2023)")
        ax.set_xlabel("Stunde (Ortszeit)")
        ax.set_ylabel("kW")
        ax.grid(alpha=0.3)
        ax2 = ax.twinx()
        pm = p[(p.index.year == 2025) & (p.index.month == monat)]
        ax2.plot(
            pm.groupby(pm.index.hour).mean() * 100, color="#e15759", ls="--", label="Preis 2025"
        )
        ax2.set_ylabel("Preis 2025 [ct/kWh]", color="#e15759")
    axs[0].legend(fontsize=8, loc="upper left")
    fig.tight_layout()
    fig.savefig(ziel / "tagesprofil.png", dpi=130)
    plt.close(fig)

    # Beispieltage im Juni des jüngsten Jahres, alle Belegungen
    j = max(jahre)
    von = pd.Timestamp(f"{j}-06-09", tz=TZ)
    fig, axs = plt.subplots(len(BELEGUNGEN), 1, figsize=(10, 8), sharex=True)
    for ax, bel in zip(axs, BELEGUNGEN, strict=True):
        e = reihen[bel][j]
        w = e[(e.index >= von) & (e.index < von + pd.Timedelta(days=4))]
        t = w.index.tz_convert(TZ)
        ax.fill_between(
            t, pv[bel].loc[w.index], step="post", alpha=0.3, color="#edc948", label="PV"
        )
        ax.step(t, w.einsp, where="post", color="#4e79a7", label="Einspeisung")
        ax.step(t, w.s, where="post", color="#59a14f", label="Ladestand [kWh]")
        ax.set_title(NAMEN[bel], fontsize=10)
        ax.set_ylabel("kWh/h bzw. kWh")
        ax.grid(alpha=0.3)
    axs[0].legend(fontsize=8, loc="upper left", ncol=3)
    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig(ziel / "beispieltage.png", dpi=130)
    plt.close(fig)


def main() -> None:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument("--energy", type=Path, default=Path(".ml-data/snap-20261022/energy.parquet"))
    ap.add_argument("--pvgis", type=Path, default=Path(".ml-data/pvgis"))
    ap.add_argument("--jahre", default="2019-2025")
    args = ap.parse_args()
    a, b = (int(x) for x in args.jahre.split("-"))
    jahre = list(range(a, b + 1))

    preis, solar = basis.lade_energie(args.energy)
    pv_pvgis = {f: basis.lade_pvgis(args.pvgis / f"mitte_{f}_1kwp.json") for f in FLAECHEN}
    tabellen = HIER / "tabellen"
    tabellen.mkdir(exist_ok=True)

    pv: dict[str, pd.Series] = {}
    tabs = []
    reihen: dict[str, dict[int, pd.DataFrame]] = {}
    for bel, anteile in BELEGUNGEN.items():
        roh = pv_reihe(pv_pvgis, anteile)
        # PVGIS-Wetterjahr = Preisjahr, wo vorhanden; sonst nationales Profil in dieser Form
        pv[bel] = roh.reindex(preis.index).fillna(
            basis.nationales_profil(solar, roh).reindex(preis.index)
        )
        t, reihen[bel] = auswerten(pd.DataFrame({"preis": preis, "pv": pv[bel]}), jahre, bel)
        tabs.append(t)
    tab = pd.concat(tabs, ignore_index=True)
    tab.to_csv(tabellen / "jahre.csv", index=False, float_format="%.2f")

    # Langjähriger Ertrag je Fläche und Belegung (PVGIS 2005-2023)
    ertrag = []
    for f, s in pv_pvgis.items():
        ertrag.append(
            {"flaeche": FLAECHEN[f], "kwh_je_kwp": float(s.groupby(s.index.year).sum().mean())}
        )
    for bel, anteile in BELEGUNGEN.items():
        s = pv_reihe(pv_pvgis, anteile)
        jahr = s.groupby(s.index.year).sum()
        ertrag.append(
            {
                "flaeche": NAMEN[bel],
                "kwh_je_kwp": float(jahr.mean() / KWP),
                "kwh_anlage": float(jahr.mean()),
                "kwh_min": float(jahr.min()),
                "kwh_max": float(jahr.max()),
                "spitze_kw": float(s.max()),
            }
        )
    pd.DataFrame(ertrag).to_csv(
        tabellen / "ertrag_langjaehrig.csv", index=False, float_format="%.1f"
    )

    grafiken(tab, pv_pvgis, pv, reihen, preis, HIER / "grafiken")


if __name__ == "__main__":
    main()
