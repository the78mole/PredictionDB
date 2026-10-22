"""PV-Speicher-Arbitrage: Was bringt ein Speicher, der PV-Einspeisung in teure Stunden verschiebt?

10 kWp, 45° Neigung, Südausrichtung, reine Einspeisung zum Day-Ahead-Preis DE-LU. Verglichen
werden Speicher 10 kWh/10 kW, 20 kWh/15 kW und 30 kWh/15 kW mit der Direkteinspeisung. Die
Ergebnisse liegen daneben (README.md, tabellen/, grafiken/). Aufruf aus dem
Repository-Wurzelverzeichnis::

    uv run --with pandas --with pyarrow --with scipy --with matplotlib \\
        python docs/analysen/pv-speicher-arbitrage/auswertung.py

Eingaben:

* ``--energy``: ``energy.parquet`` aus ``ml-train snapshot`` (Spalten ts, value, code), daraus
  ``da_price_de_lu`` (EUR/MWh, stündlich) und ``gen_solar`` (MW, für Jahre ohne PVGIS).
* ``--pvgis``: Verzeichnis mit PVGIS-``seriescalc``-JSON (10 kWp, 45°, Süd, 14 % Verluste).

Fahrplan wie im echten Betrieb: Die Day-Ahead-Preise für morgen stehen um 13 Uhr fest. Jeden
Tag um 13 Uhr (Ortszeit) wird per LP bis Mitternacht des Folgetags geplant (35 h, an den
Umstellungstagen 34/36 h) und bis 13 Uhr des Folgetags ausgeführt. Der PV-Ertrag gilt im
Planungsfenster als bekannt. Energie, die am Fensterende noch im Speicher ist, zählt im LP
nichts; sie geht aber nicht verloren, weil der nächste Plan sie übernimmt.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.optimize import linprog
from scipy.sparse import lil_matrix

HIER = Path(__file__).resolve().parent
TZ = "Europe/Berlin"
Reihen = dict[tuple[int, str], pd.DataFrame]


@dataclass(frozen=True)
class Speicher:
    name: str
    kwh: float
    kw: float
    eta_lade: float = 0.95
    eta_entl: float = 0.95
    soc_min: float = 0.0  # Anteil der Nennkapazität, der nicht genutzt wird


SPEICHER = [Speicher("10/10", 10, 10), Speicher("20/15", 20, 15), Speicher("30/15", 30, 15)]


# ---------------------------------------------------------------------------------------------
# Daten
# ---------------------------------------------------------------------------------------------


def lade_energie(pfad: Path) -> tuple[pd.Series, pd.Series]:
    d = pd.read_parquet(pfad, columns=["ts", "value", "code"])
    preis = d[d.code == "da_price_de_lu"].set_index("ts").value.sort_index() / 1000.0  # EUR/kWh
    solar = d[d.code == "gen_solar"].set_index("ts").value.sort_index()
    return preis, solar


def lade_pvgis(pfad: Path) -> pd.Series:
    """PVGIS-Stundenwerte (W) als kW, Zeitstempel auf die volle UTC-Stunde gelegt.

    PVGIS gibt Stundenmittel aus, die Zeit (z. B. ``20200101:0010``) liegt im Stundenintervall.
    """
    roh = json.loads(pfad.read_text())
    st = pd.DataFrame(roh["outputs"]["hourly"])
    ts = pd.to_datetime(st["time"], format="%Y%m%d:%H%M", utc=True).dt.floor("h")
    return pd.Series(st["P"].to_numpy() / 1000.0, index=ts, name="pv").sort_index()


def nationales_profil(solar: pd.Series, pv: pd.Series) -> pd.Series:
    """PV-Reihe für Jahre ohne PVGIS: nationale Einspeisung, auf die Anlage umgerechnet.

    Die deutsche PV-Einspeisung wird auf ihr gleitendes Jahresmittel normiert (das nimmt den
    Zubau heraus) und je Monat und Stunde (UTC) mit dem Verhältnis PVGIS/national aus den
    Überlappungsjahren skaliert. Damit hat die Reihe Form und Jahresgang einer 45°-Südanlage,
    aber das Wetter des jeweiligen Jahres, und passt zeitlich zu den Preisen.
    """
    norm = solar / solar.rolling("365D", center=True, min_periods=24 * 200).mean()
    norm = norm.clip(lower=0)
    gem = pd.concat([norm.rename("nat"), pv.rename("pv")], axis=1, join="inner").dropna()
    gruppen = [gem.index.month, gem.index.hour]
    faktor = gem.groupby(gruppen).pv.mean() / gem.groupby(gruppen).nat.mean()
    faktor = faktor.replace([np.inf, -np.inf], 0).fillna(0)
    idx = pd.MultiIndex.from_arrays([norm.index.month, norm.index.hour])
    return pd.Series(norm.to_numpy() * faktor.reindex(idx).fillna(0).to_numpy(), index=norm.index)


# ---------------------------------------------------------------------------------------------
# Optimierung
# ---------------------------------------------------------------------------------------------


def plane(
    pv: np.ndarray,
    preis: np.ndarray,
    sp: Speicher,
    soc0: float,
    netzladen: bool = False,
    verschleiss: float = 0.0,
) -> dict[str, np.ndarray]:
    """LP über ein Fenster. Variablen je Stunde: e (PV direkt), c (PV in Speicher),
    g (Netz in Speicher), d (Entladen ins Netz), s (Ladestand am Stundenende), alles kWh."""
    n = len(pv)
    E, C, G, D, S = (np.arange(n) + k * n for k in range(5))
    nv = 5 * n
    # Ziel: max sum preis*(e + d - g) - verschleiss*d  ->  linprog minimiert
    kosten = np.zeros(nv)
    kosten[E] = -preis
    kosten[D] = -preis + verschleiss
    kosten[G] = preis
    grenzen: list[tuple[float, float | None]] = [(0, None)] * nv
    smin = sp.soc_min * sp.kwh
    for h in range(n):
        grenzen[C[h]] = (0, sp.kw)
        grenzen[G[h]] = (0, sp.kw if netzladen else 0)
        grenzen[D[h]] = (0, sp.kw)
        grenzen[S[h]] = (smin, sp.kwh)
    # Ungleichungen: e + c <= pv (Rest wird abgeregelt); c + g <= P
    a_ub = lil_matrix((2 * n, nv))
    b_ub = np.zeros(2 * n)
    for h in range(n):
        a_ub[h, E[h]] = 1
        a_ub[h, C[h]] = 1
        b_ub[h] = pv[h]
        a_ub[n + h, C[h]] = 1
        a_ub[n + h, G[h]] = 1
        b_ub[n + h] = sp.kw
    # Gleichungen: s_h - s_{h-1} - eta_l*(c+g) + d/eta_e = 0
    a_eq = lil_matrix((n, nv))
    b_eq = np.zeros(n)
    for h in range(n):
        a_eq[h, S[h]] = 1
        if h > 0:
            a_eq[h, S[h - 1]] = -1
        else:
            b_eq[h] = soc0
        a_eq[h, C[h]] = -sp.eta_lade
        a_eq[h, G[h]] = -sp.eta_lade
        a_eq[h, D[h]] = 1 / sp.eta_entl
    res = linprog(
        kosten,
        A_ub=a_ub.tocsr(),
        b_ub=b_ub,
        A_eq=a_eq.tocsr(),
        b_eq=b_eq,
        bounds=grenzen,
        method="highs",
    )
    if res.status != 0:
        raise RuntimeError(res.message)
    x = res.x
    return {"e": x[E], "c": x[C], "g": x[G], "d": x[D], "s": x[S]}


def ausfuehren(
    plan: dict[str, np.ndarray], pv: np.ndarray, preis: np.ndarray, sp: Speicher, soc: float
) -> dict[str, np.ndarray]:
    """Plan stundenweise mit dem tatsächlichen PV-Ertrag fahren (wenn die Prognose danebenlag).

    Entladen wie geplant, soweit der Speicher es hergibt. Laden wie geplant, soweit PV da ist;
    bei negativem Preis so viel wie möglich. Der Rest geht ins Netz, bei negativem Preis wird er
    abgeregelt.
    """
    n = len(pv)
    aus = {k: np.zeros(n) for k in ("e", "c", "g", "d", "s")}
    smin = sp.soc_min * sp.kwh
    for h in range(n):
        d = min(plan["d"][h], max(0.0, soc - smin) * sp.eta_entl)
        soc -= d / sp.eta_entl
        raum = max(0.0, sp.kwh - soc) / sp.eta_lade
        c = min(pv[h] if preis[h] < 0 else plan["c"][h], pv[h], raum, sp.kw)
        soc += c * sp.eta_lade
        aus["d"][h], aus["c"][h], aus["s"][h] = d, c, soc
        aus["e"][h] = pv[h] - c if preis[h] >= 0 else 0.0
    return aus


def prognose(pv: pd.Series, sigma_tag: float = 0.3, sigma_std: float = 0.2) -> pd.Series:
    """Künstliche Day-Ahead-PV-Prognose: multiplikativer Fehler je Tag und je Stunde."""
    rng = np.random.default_rng(42)
    tage = pv.index.tz_convert(TZ).normalize()
    f_tag = pd.Series(rng.normal(0, sigma_tag, len(tage.unique())), index=tage.unique())
    f = f_tag.reindex(tage).to_numpy() + rng.normal(0, sigma_std, len(pv))
    return pv * np.exp(f - (sigma_tag**2 + sigma_std**2) / 2)


def rollierend(
    df: pd.DataFrame,
    sp: Speicher,
    netzladen: bool = False,
    verschleiss: float = 0.0,
    pv_prognose: pd.Series | None = None,
) -> pd.DataFrame:
    """Täglich um 13 Uhr bis Mitternacht des Folgetags planen, bis 13 Uhr des Folgetags fahren.

    Mit ``pv_prognose`` wird mit der Prognose geplant und mit dem echten Ertrag gefahren.
    """
    lokal = df.index.tz_convert(TZ)
    starts = np.flatnonzero(lokal.hour == 13)
    datum = np.asarray(lokal.date)
    pv = df.pv.to_numpy()
    pv_plan = pv if pv_prognose is None else pv_prognose.reindex(df.index).to_numpy()
    preis = df.preis.to_numpy()
    aus = {k: np.zeros(len(df)) for k in ("e", "c", "g", "d", "s")}
    soc = sp.soc_min * sp.kwh
    for i, a in enumerate(starts):
        tag2 = datum[a] + pd.Timedelta(days=1).to_pytimedelta()
        ende = a
        while ende < len(df) and datum[ende] <= tag2:
            ende += 1
        bis = starts[i + 1] if i + 1 < len(starts) else ende
        plan = plane(pv_plan[a:ende], preis[a:ende], sp, soc, netzladen, verschleiss)
        m = bis - a
        if pv_prognose is not None:
            plan = ausfuehren({k: v[:m] for k, v in plan.items()}, pv[a:bis], preis[a:bis], sp, soc)
        for k in aus:
            aus[k][a:bis] = plan[k][:m]
        soc = plan["s"][m - 1]
    erg = pd.DataFrame(aus, index=df.index)
    erg["einsp"] = erg.e + erg.d - erg.g
    erg["erloes"] = erg.einsp * df.preis
    return erg.iloc[starts[0] :]


def perfekt(df: pd.DataFrame, sp: Speicher) -> float:
    """Obergrenze: ein LP über den ganzen Zeitraum mit voller Voraussicht."""
    plan = plane(df.pv.to_numpy(), df.preis.to_numpy(), sp, sp.soc_min * sp.kwh)
    return float(((plan["e"] + plan["d"]) * df.preis.to_numpy()).sum())


def direkt(df: pd.DataFrame, abregeln: bool = True) -> pd.Series:
    """Direkteinspeisung; bei negativem Preis abgeregelt (abregeln=True) oder nicht."""
    einsp = df.pv.where(df.preis >= 0, 0.0) if abregeln else df.pv
    return einsp * df.preis


# ---------------------------------------------------------------------------------------------
# Auswertung
# ---------------------------------------------------------------------------------------------


def jahre_auswerten(df: pd.DataFrame, jahre: list[int], label: str) -> tuple[pd.DataFrame, Reihen]:
    zeilen = []
    reihen: Reihen = {}
    for j in jahre:
        # 13-Uhr-Fenster des Vorjahres-Silvesters mitnehmen, damit der 1.1. voll geplant ist
        von = pd.Timestamp(f"{j - 1}-12-31 00:00", tz=TZ)
        bis = pd.Timestamp(f"{j + 1}-01-02 00:00", tz=TZ)
        teil = df[(df.index >= von) & (df.index < bis)].dropna()
        im_jahr = teil.index.tz_convert(TZ).year == j
        basis = direkt(teil)[im_jahr]
        basis_ohne = direkt(teil, abregeln=False)[im_jahr]
        pvj = teil.pv[im_jahr]
        z = {
            "pv_quelle": label,
            "jahr": j,
            "stunden": int(im_jahr.sum()),
            "pv_kwh": pvj.sum(),
            "preis_mittel_ct": teil.preis[im_jahr].mean() * 100,
            "marktwert_pv_ct": basis_ohne.sum() / pvj.sum() * 100,
            "direkt_eur": basis.sum(),
            "direkt_ohne_abregeln_eur": basis_ohne.sum(),
            "neg_stunden": int((teil.preis[im_jahr] < 0).sum()),
        }
        for sp in SPEICHER:
            erg = rollierend(teil, sp)
            ej = erg[erg.index.tz_convert(TZ).year == j]
            reihen[(j, sp.name)] = ej
            z[f"{sp.name}_eur"] = ej.erloes.sum()
            z[f"{sp.name}_mehr_eur"] = ej.erloes.sum() - basis.sum()
            z[f"{sp.name}_zyklen"] = ej.d.sum() / sp.eta_entl / sp.kwh
        zeilen.append(z)
        print(label, j, {k: round(v, 1) for k, v in z.items() if isinstance(v, float)})
    return pd.DataFrame(zeilen), reihen


def varianten(df: pd.DataFrame, jahre: list[int]) -> pd.DataFrame:
    """Empfindlichkeit: Netzladen, Verschleißkosten, Wirkungsgrad, volle Voraussicht."""
    zeilen = []
    for j in jahre:
        von = pd.Timestamp(f"{j - 1}-12-31 00:00", tz=TZ)
        bis = pd.Timestamp(f"{j + 1}-01-02 00:00", tz=TZ)
        teil = df[(df.index >= von) & (df.index < bis)].dropna()
        im_jahr = teil.index.tz_convert(TZ).year == j
        basis = direkt(teil)[im_jahr].sum()
        nur_jahr = teil[im_jahr]
        prog = prognose(teil.pv)
        for sp in SPEICHER:

            def mehr(erg: pd.DataFrame, j: int = j, basis: float = basis) -> float:
                return float(erg[erg.index.tz_convert(TZ).year == j].erloes.sum() - basis)

            schlecht = Speicher(sp.name, sp.kwh, sp.kw, 0.92, 0.92, 0.05)
            zeilen.append(
                {
                    "jahr": j,
                    "speicher": sp.name,
                    "basis": mehr(rollierend(teil, sp)),
                    "verschleiss_3ct": mehr(rollierend(teil, sp, verschleiss=0.03)),
                    "wirkungsgrad_85": mehr(rollierend(teil, schlecht)),
                    "pv_prognosefehler": mehr(rollierend(teil, sp, pv_prognose=prog)),
                    "netzladen": mehr(rollierend(teil, sp, netzladen=True)),
                    "volle_voraussicht": perfekt(nur_jahr, sp) - direkt(nur_jahr).sum(),
                }
            )
            print("Variante", zeilen[-1])
    return pd.DataFrame(zeilen)


def grafiken(tab: pd.DataFrame, reihen: Reihen, df: pd.DataFrame, ziel: Path) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    ziel.mkdir(parents=True, exist_ok=True)
    farben = {"10/10": "#4e79a7", "20/15": "#f28e2b", "30/15": "#59a14f"}

    # Mehrerlös je Jahr
    fig, ax = plt.subplots(figsize=(9, 4.5))
    x = np.arange(len(tab))
    for k, sp in enumerate(SPEICHER):
        ax.bar(
            x + (k - 1) * 0.27,
            tab[f"{sp.name}_mehr_eur"],
            0.27,
            label=f"{sp.name} kWh/kW",
            color=farben[sp.name],
        )
    ax.set_xticks(x, [str(j) for j in tab.jahr])
    ax.set_ylabel("Mehrerlös gegenüber Direkteinspeisung [EUR/Jahr]")
    ax.set_title("10 kWp, 45° Süd: Mehrerlös durch Verschieben der Einspeisung")
    ax.grid(axis="y", alpha=0.3)
    ax.legend()
    fig.tight_layout()
    fig.savefig(ziel / "mehrerloes_jahr.png", dpi=130)
    plt.close(fig)

    # Mittleres Preisprofil nach Stunde, je Jahr
    lokal = df.preis.copy()
    lokal.index = lokal.index.tz_convert(TZ)
    fig, ax = plt.subplots(figsize=(9, 4.5))
    cmap = plt.get_cmap("viridis")
    js = list(tab.jahr)
    for k, j in enumerate(js):
        p = lokal[lokal.index.year == j]
        som = p[(p.index.month >= 4) & (p.index.month <= 9)]
        ax.plot(
            som.groupby(som.index.hour).mean() * 100,
            color=cmap(k / max(1, len(js) - 1)),
            label=str(j),
        )
    ax.set_xlabel("Stunde (Ortszeit)")
    ax.set_ylabel("Day-Ahead-Preis [ct/kWh]")
    ax.set_title("Mittleres Tagesprofil April bis September")
    ax.grid(alpha=0.3)
    ax.legend(ncol=4, fontsize=8)
    fig.tight_layout()
    fig.savefig(ziel / "preisprofil_sommer.png", dpi=130)
    plt.close(fig)

    # Mehrerlös je Monat im jüngsten vollen Jahr
    j = max(js)
    fig, ax = plt.subplots(figsize=(9, 4.5))
    basis = direkt(df)
    basis.index = basis.index.tz_convert(TZ)
    b = basis[basis.index.year == j]
    for k, sp in enumerate(SPEICHER):
        e = reihen[(j, sp.name)].erloes.copy()
        e.index = e.index.tz_convert(TZ)
        mon = e.groupby(e.index.month).sum() - b.groupby(b.index.month).sum()
        ax.bar(mon.index + (k - 1) * 0.27, mon, 0.27, label=sp.name, color=farben[sp.name])
    ax.set_xticks(range(1, 13))
    ax.set_xlabel("Monat")
    ax.set_ylabel("Mehrerlös [EUR]")
    ax.set_title(f"Mehrerlös je Monat {j}")
    ax.grid(axis="y", alpha=0.3)
    ax.legend()
    fig.tight_layout()
    fig.savefig(ziel / f"mehrerloes_monat_{j}.png", dpi=130)
    plt.close(fig)

    # Beispielwoche im Juni des jüngsten Jahres, Speicher 20/15
    e = reihen[(j, "20/15")]
    von = pd.Timestamp(f"{j}-06-09", tz=TZ)
    w = e[(e.index >= von) & (e.index < von + pd.Timedelta(days=7))]
    dw = df.loc[w.index]
    t = w.index.tz_convert(TZ)
    fig, ax = plt.subplots(2, 1, figsize=(10, 6), sharex=True)
    ax[0].fill_between(t, dw.pv, step="post", alpha=0.3, color="#edc948", label="PV-Ertrag")
    ax[0].step(t, w.einsp, where="post", color="#4e79a7", label="Einspeisung mit Speicher")
    ax[0].step(t, w.s, where="post", color="#59a14f", label="Ladestand [kWh]")
    ax[0].set_ylabel("kWh je Stunde / kWh")
    ax[0].legend(fontsize=8, loc="upper left")
    ax[0].grid(alpha=0.3)
    ax[1].step(t, dw.preis * 100, where="post", color="#e15759")
    ax[1].set_ylabel("Preis [ct/kWh]")
    ax[1].grid(alpha=0.3)
    ax[0].set_title(f"Beispielwoche ab {von:%d.%m.%Y}, Speicher 20 kWh / 15 kW")
    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig(ziel / "beispielwoche.png", dpi=130)
    plt.close(fig)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--energy", type=Path, default=Path(".ml-data/snap-20261022/energy.parquet"))
    ap.add_argument("--pvgis", type=Path, default=Path(".ml-data/pvgis/mitte.json"))
    ap.add_argument("--jahre", default="2019-2025")
    ap.add_argument("--ohne-varianten", action="store_true")
    args = ap.parse_args()
    a, b = (int(x) for x in args.jahre.split("-"))
    jahre = list(range(a, b + 1))

    preis, solar = lade_energie(args.energy)
    pv_pvgis = lade_pvgis(args.pvgis)
    pv_nat = nationales_profil(solar, pv_pvgis)
    tabellen = HIER / "tabellen"
    tabellen.mkdir(exist_ok=True)

    # PV-Reihe: PVGIS-Wetterjahr = Preisjahr, wo vorhanden; sonst nationales Profil
    pv = pv_pvgis.reindex(preis.index)
    pv = pv.fillna(pv_nat.reindex(preis.index))
    df = pd.DataFrame({"preis": preis, "pv": pv})

    tab, reihen = jahre_auswerten(df, jahre, "pvgis+national")
    tab.to_csv(tabellen / "jahre.csv", index=False, float_format="%.2f")

    # Gegenprobe: nur nationales Profil, auch in den PVGIS-Jahren
    df_nat = pd.DataFrame({"preis": preis, "pv": pv_nat.reindex(preis.index)})
    pv_jahre = sorted(set(pv_pvgis.index.year) & set(jahre))
    tab_nat, _ = jahre_auswerten(df_nat, pv_jahre, "national")
    tab_nat.to_csv(tabellen / "gegenprobe_national.csv", index=False, float_format="%.2f")

    # Andere Standorte (gleiche Methode), soweit Dateien vorliegen
    standorte = []
    for f in sorted(args.pvgis.parent.glob("*.json")):
        if f == args.pvgis:
            continue
        p2 = lade_pvgis(f)
        d2 = pd.DataFrame(
            {
                "preis": preis,
                "pv": p2.reindex(preis.index).fillna(
                    nationales_profil(solar, p2).reindex(preis.index)
                ),
            }
        )
        t2, _ = jahre_auswerten(d2, jahre, f.stem)
        standorte.append(t2)
    if standorte:
        pd.concat(standorte).to_csv(tabellen / "standorte.csv", index=False, float_format="%.2f")

    if not args.ohne_varianten:
        var = varianten(df, jahre)
        var.to_csv(tabellen / "varianten.csv", index=False, float_format="%.2f")

    grafiken(tab, reihen, df, HIER / "grafiken")


if __name__ == "__main__":
    main()
