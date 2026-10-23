"""Marktwert-Schätzung: Wie nah kommen eigene Marktwerte an die amtlichen von Netztransparenz?

Vergleicht die Monats- und Jahresmarktwerte, die ``energy_ingest.sources.marktwert`` aus
freien Daten rechnet (Quelle ``pdb_marktwert``), mit den Werten der ÜNB auf Netztransparenz.de.
Die Ergebnisse liegen daneben (README.md, tabellen/, grafiken/). Aufruf aus dem
Repository-Wurzelverzeichnis::

    uv run --with pandas --with matplotlib \\
        python docs/analysen/marktwert-schaetzung/auswertung.py --daten <ordner>

Eingaben im Ordner ``--daten`` (Export aus der Produktionsdatenbank, nur lesend):

* ``stunden.csv.gz``: Stundenmittel je Quelle (smard, entsoe, energy_charts) und Code
  (``da_price_de_lu``, ``gen_solar``, ``gen_wind_onshore``, ``gen_wind_offshore``);
  Spalten hour, source, code, value, n
* ``amtlich.csv.gz``: ``mw_*`` und ``jw_*`` von Netztransparenz; Spalten ts, code, value
* ``viertelstunden.csv.gz``: Rohwerte ab 30.09.2025 (smard, entsoe); Spalten ts, source,
  code, value. Seit 01.10.2025 handelt die Day-Ahead-Auktion Viertelstunden.

Varianten:

* ``smard``: genau die Rechnung von ``pdb_marktwert`` (Stundenmittel aus SMARD)
* ``entsoe``: dieselbe Rechnung mit ENTSO-E-Daten, zur Einordnung der Datenquelle
* ``smard_15min``: ab Oktober 2025 mit Viertelstundenpreisen und -erzeugung gewichtet
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd

from energy_ingest.model import BERLIN
from energy_ingest.sources.marktwert import MIN_COVERAGE, PRICE, WEIGHTS, market_values

HIER = Path(__file__).resolve().parent
CODES = [PRICE, *WEIGHTS]
TYPEN = ["base", "solar", "wind_onshore", "wind_offshore"]
VIERTELSTUNDEN_AB = datetime(2025, 10, 1, tzinfo=BERLIN)


# ---------------------------------------------------------------------------------------------
# Daten
# ---------------------------------------------------------------------------------------------


def lade(daten: Path) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    stunden = pd.read_csv(daten / "stunden.csv.gz", parse_dates=["hour"])
    amtlich = pd.read_csv(daten / "amtlich.csv.gz", parse_dates=["ts"])
    viertel = pd.read_csv(daten / "viertelstunden.csv.gz", parse_dates=["ts"])
    for df, col in [(stunden, "hour"), (amtlich, "ts"), (viertel, "ts")]:
        df[col] = pd.to_datetime(df[col], utc=True)
    return stunden, amtlich, viertel


def perioden(von: datetime, bis: datetime, monate: int) -> list[datetime]:
    """Periodenanfänge (Ortszeit, als UTC) von ``von`` bis vor ``bis``."""
    out: list[datetime] = []
    lokal = von.astimezone(BERLIN)
    j, m = lokal.year, (lokal.month if monate == 1 else 1)
    while True:
        p = datetime(j, m, 1, tzinfo=BERLIN).astimezone(UTC)
        if p >= bis:
            return out
        if p >= von:
            out.append(p)
        m += monate
        j, m = j + (m - 1) // 12, (m - 1) % 12 + 1


def schaetze(stunden: pd.DataFrame, quelle: str, monate: int) -> pd.DataFrame:
    """Marktwerte wie ``pdb_marktwert``, aus den Stundenwerten einer Quelle."""
    d = stunden[(stunden.source == quelle) & stunden.code.isin(CODES)]
    if d.empty:
        return pd.DataFrame(columns=["ts", "code", "value"])
    rows = [
        (h.to_pydatetime(), c, float(v)) for h, c, v in zip(d.hour, d.code, d.value, strict=True)
    ]
    ps = perioden(d.hour.min().to_pydatetime(), d.hour.max().to_pydatetime(), monate)
    out = [(ts, s.code, v) for s in market_values(rows, ps, monate) for ts, v in s.points]
    return pd.DataFrame(out, columns=["ts", "code", "value"])


def schaetze_viertel(viertel: pd.DataFrame) -> pd.DataFrame:
    """Monatswerte ab Oktober 2025 mit Viertelstundenpreisen gewichtet (SMARD)."""
    d = viertel[(viertel.source == "smard") & (viertel.ts >= VIERTELSTUNDEN_AB)]
    w = d.pivot_table(index="ts", columns="code", values="value", aggfunc="mean")
    if PRICE not in w:
        return pd.DataFrame(columns=["ts", "code", "value"])
    lokal = w.index.tz_convert(BERLIN)
    w["monat"] = [datetime(t.year, t.month, 1, tzinfo=BERLIN).astimezone(UTC) for t in lokal]
    out = []
    for monat, g in w.groupby("monat"):
        ende = (pd.Timestamp(monat).tz_convert(BERLIN) + pd.offsets.MonthBegin(1)).tz_convert(UTC)
        soll = (ende - pd.Timestamp(monat)) / pd.Timedelta(minutes=15)
        p = g[PRICE].dropna()
        if len(p) < MIN_COVERAGE * soll:
            continue
        out.append((monat, "mw_base", float(p.mean())))
        for gen, typ in WEIGHTS.items():
            if gen not in g:
                continue
            gg = g[[PRICE, gen]].dropna()
            if len(gg) < MIN_COVERAGE * soll or gg[gen].sum() <= 0:
                continue
            out.append((monat, f"mw_{typ}", float((gg[PRICE] * gg[gen]).sum() / gg[gen].sum())))
    return pd.DataFrame(out, columns=["ts", "code", "value"])


# ---------------------------------------------------------------------------------------------
# Vergleich
# ---------------------------------------------------------------------------------------------


def paare(schaetzung: pd.DataFrame, amtlich: pd.DataFrame, variante: str) -> pd.DataFrame:
    s = schaetzung.assign(ts=pd.to_datetime(schaetzung.ts, utc=True))
    p = s.merge(amtlich, on=["ts", "code"], suffixes=("_schaetzung", "_amtlich"))
    p["fehler"] = p.value_schaetzung - p.value_amtlich
    p["variante"] = variante
    p["typ"] = p.code.str.split("_", n=1).str[1]
    p["periode"] = p.code.str[:2].map({"mw": "Monat", "jw": "Jahr"})
    p["jahr"] = p.ts.dt.tz_convert(BERLIN).dt.year
    return p


def kennzahlen(p: pd.DataFrame, by: list[str]) -> pd.DataFrame:
    g = p.groupby(by)
    k = pd.DataFrame(
        {
            "n": g.size(),
            "von": g.ts.min().dt.tz_convert(BERLIN).dt.strftime("%Y-%m"),
            "bis": g.ts.max().dt.tz_convert(BERLIN).dt.strftime("%Y-%m"),
            "bias_eur_mwh": g.fehler.mean(),
            "mae_eur_mwh": g.fehler.apply(lambda e: e.abs().mean()),
            "max_abs_eur_mwh": g.fehler.apply(lambda e: e.abs().max()),
            "rmse_eur_mwh": g.fehler.apply(lambda e: (e**2).mean() ** 0.5),
            "bias_prozent": g.apply(lambda d: 100 * d.fehler.sum() / d.value_amtlich.abs().sum()),
            "mae_prozent": g.apply(
                lambda d: 100 * d.fehler.abs().sum() / d.value_amtlich.abs().sum()
            ),
        }
    )
    return k.round(3).reset_index()


# ---------------------------------------------------------------------------------------------
# Grafiken
# ---------------------------------------------------------------------------------------------


def grafik_verlauf(p: pd.DataFrame, pfad: Path) -> None:
    d = p[(p.variante == "smard") & (p.periode == "Monat")]
    fig, axes = plt.subplots(len(TYPEN), 1, figsize=(10, 2.6 * len(TYPEN)), sharex=True)
    for ax, typ in zip(axes, TYPEN, strict=True):
        t = d[d.typ == typ].sort_values("ts")
        ax.plot(t.ts, t.value_amtlich, label="amtlich (Netztransparenz)", lw=1.6)
        ax.plot(t.ts, t.value_schaetzung, label="geschätzt (SMARD)", lw=1.0, ls="--")
        ax.set_ylabel("EUR/MWh")
        ax.set_title(f"Monatsmarktwert {typ}", loc="left", fontsize=10)
        ax.grid(alpha=0.3)
    axes[0].legend(loc="upper left", fontsize=8)
    fig.tight_layout()
    fig.savefig(pfad, dpi=130)
    plt.close(fig)


def grafik_fehler(p: pd.DataFrame, pfad: Path) -> None:
    d = p[(p.variante == "smard") & (p.periode == "Monat")]
    fig, ax = plt.subplots(figsize=(10, 4))
    for typ in TYPEN:
        t = d[d.typ == typ].sort_values("ts")
        ax.plot(t.ts, t.fehler, marker=".", lw=0.8, label=typ)
    ax.axhline(0, color="black", lw=0.6)
    ax.set_ylabel("Schätzung - amtlich [EUR/MWh]")
    titel = "Abweichung der Monatsmarktwerte (SMARD, Stundenbasis)"
    ax.set_title(titel, loc="left", fontsize=10)
    ax.grid(alpha=0.3)
    ax.legend(fontsize=8, ncol=4)
    fig.tight_layout()
    fig.savefig(pfad, dpi=130)
    plt.close(fig)


# ---------------------------------------------------------------------------------------------


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--daten", type=Path, required=True)
    args = ap.parse_args()
    gestartet = datetime.now(UTC)
    tab, graf = HIER / "tabellen", HIER / "grafiken"
    tab.mkdir(exist_ok=True)
    graf.mkdir(exist_ok=True)

    stunden, amtlich, viertel = lade(args.daten)
    alle = []
    for quelle in ["smard", "entsoe"]:
        for monate in (1, 12):
            alle.append(paare(schaetze(stunden, quelle, monate), amtlich, quelle))
    alle.append(paare(schaetze_viertel(viertel), amtlich, "smard_15min"))
    p = pd.concat([a for a in alle if not a.empty], ignore_index=True)

    p.sort_values(["variante", "code", "ts"]).assign(
        monat=lambda d: d.ts.dt.tz_convert(BERLIN).dt.strftime("%Y-%m")
    )[["variante", "code", "monat", "value_amtlich", "value_schaetzung", "fehler"]].round(3).to_csv(
        tab / "paare.csv", index=False
    )
    kennzahlen(p, ["variante", "periode", "typ"]).to_csv(tab / "kennzahlen.csv", index=False)
    kennzahlen(p[p.variante == "smard"], ["periode", "typ", "jahr"]).to_csv(
        tab / "kennzahlen_jahre.csv", index=False
    )
    # Fair verglichen: Stunden- und Viertelstundenbasis auf denselben Monaten
    neu = p[p.variante == "smard_15min"][["ts", "code"]]
    if not neu.empty:
        gleich = p[p.variante.isin(["smard", "smard_15min"])].merge(neu, on=["ts", "code"])
        kennzahlen(gleich, ["variante", "typ"]).to_csv(
            tab / "kennzahlen_viertelstunden.csv", index=False
        )

    grafik_verlauf(p, graf / "verlauf_monate.png")
    grafik_fehler(p, graf / "fehler_monate.png")

    abdeckung = (
        stunden.groupby(["source", "code"])
        .hour.agg(["min", "max", "count"])
        .reset_index()
        .astype(str)
        .to_dict(orient="records")
    )
    eingaben = {
        f.name: hashlib.sha256(f.read_bytes()).hexdigest()
        for f in sorted(args.daten.glob("*.csv.gz"))
    }
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=False
    ).stdout.strip()
    lauf = {
        "analyse": "marktwert-schaetzung",
        "git_commit": commit,
        "gestartet": gestartet.isoformat(),
        "beendet": datetime.now(UTC).isoformat(),
        "eingaben_sha256": eingaben,
        "abdeckung_stunden": abdeckung,
        "paare_je_variante": p.groupby("variante").size().to_dict(),
        "min_abdeckung": MIN_COVERAGE,
    }
    (tab / "lauf.json").write_text(json.dumps(lauf, indent=1, ensure_ascii=False) + "\n")
    print(kennzahlen(p, ["variante", "periode", "typ"]).to_string())


if __name__ == "__main__":
    main()
