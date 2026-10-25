"""Terminpreis-Treffsicherheit: Wie gut sagt der Jahres-Future den späteren Spotpreis voraus?

Vergleicht den Phelix-DE Base Year Future, den die Übertragungsnetzbetreiber für die Prognose
der EEG-Umlage bzw. des EEG-Finanzierungsbedarfs ansetzen (``tabellen/terminpreise.csv``,
einmalig von Hand aus den Quellen übernommen, Fundstelle je Wert), mit dem später
realisierten Jahresmittel des Day-Ahead-Preises. Zum Vergleich dienen naive Schätzer, die am
selben Stichtag (15.09. des Vorjahres) bekannt waren.

Aufruf aus dem Repository-Wurzelverzeichnis::

    uv run --with pandas --with matplotlib --with httpx \\
        python docs/analysen/terminpreis-treffsicherheit/auswertung.py

Spotpreise: Energy-Charts-API ``/price`` (Fraunhofer ISE), Gebotszone DE-AT-LU bis
30.09.2018, danach DE-LU, Lizenz CC BY 4.0 (Bundesnetzagentur | SMARD.de). Seit
01.10.2025 liefert die Auktion Viertelstunden; vor dem Jahresmittel wird daher auf Stunden
gemittelt, damit jede Stunde gleich zählt.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import httpx
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd

HIER = Path(__file__).resolve().parent
API = "https://api.energy-charts.info/price"
ZONENWECHSEL = pd.Timestamp("2018-10-01", tz="Europe/Berlin")
ERSTES_JAHR = 2015  # Energy-Charts führt DE-AT-LU ab 2015
STICHTAG = "09-15"  # Ende des Mittelungszeitraums der ÜNB (16.06.-15.09.)


# ---------------------------------------------------------------------------------------------
# Daten
# ---------------------------------------------------------------------------------------------


def hole_jahr(zone: str, jahr: int) -> pd.Series:
    """Rohpreise einer Zone für ein Kalenderjahr; leere Serie, wenn die API nichts hat."""
    for versuch in range(6):
        r = httpx.get(
            API, params={"bzn": zone, "start": f"{jahr}-01-01", "end": f"{jahr}-12-31"}, timeout=180
        )
        if r.status_code == 429:
            time.sleep(10 * (versuch + 1))
            continue
        if r.status_code == 404 or not r.text.startswith("{"):
            return pd.Series(dtype=float)
        r.raise_for_status()
        d = r.json()
        ts = pd.to_datetime(d["unix_seconds"], unit="s", utc=True).tz_convert("Europe/Berlin")
        return pd.Series(d["price"], index=ts, dtype=float).dropna()
    raise RuntimeError(f"Energy-Charts antwortet dauerhaft mit 429 ({zone} {jahr})")


def spotpreise(cache: Path, bis: int) -> pd.Series:
    """Stundenmittel des Day-Ahead-Preises, DE-AT-LU vor und DE-LU ab dem Zonenwechsel."""
    if cache.exists():
        s = pd.read_csv(cache, index_col=0)["preis"]
        s.index = pd.to_datetime(s.index, utc=True).tz_convert("Europe/Berlin")
        return s
    teile = []
    for jahr in range(ERSTES_JAHR, bis + 1):
        for zone in ("DE-AT-LU", "DE-LU"):
            s = hole_jahr(zone, jahr)
            if s.empty:
                continue
            s = s[s.index < ZONENWECHSEL] if zone == "DE-AT-LU" else s[s.index >= ZONENWECHSEL]
            teile.append(s)
    roh = pd.concat(teile).sort_index()
    roh = roh[~roh.index.duplicated()]
    stunden = roh.resample("1h").mean().dropna()
    cache.parent.mkdir(parents=True, exist_ok=True)
    stunden.rename("preis").to_frame().to_csv(cache)
    return stunden


# ---------------------------------------------------------------------------------------------
# Auswertung
# ---------------------------------------------------------------------------------------------


def naive_schaetzer(stunden: pd.Series, jahr: int) -> dict[str, float]:
    """Am Stichtag 15.09. des Vorjahres bekannte Spot-Mittel."""
    ende = pd.Timestamp(f"{jahr - 1}-{STICHTAG}", tz="Europe/Berlin") + pd.Timedelta(days=1)
    fenster = {
        "spot_16jun_15sep": ende - pd.DateOffset(months=3),
        "spot_12_monate": ende - pd.DateOffset(years=1),
    }
    out = {}
    for name, start in fenster.items():
        s = stunden[(stunden.index >= start) & (stunden.index < ende)]
        # mindestens 95 % der Stunden, sonst fehlt der Schätzer
        soll = (ende - start) / pd.Timedelta(hours=1)
        out[name] = round(s.mean(), 2) if len(s) >= 0.95 * soll else float("nan")
    return out


def paare(termin: pd.DataFrame, stunden: pd.Series) -> pd.DataFrame:
    jahre = stunden.groupby(stunden.index.year)
    ist = jahre.mean().round(2)
    stunden_je_jahr = jahre.size()
    zeilen = []
    for _, r in termin.iterrows():
        j = int(r["jahr"])
        vollstaendig = j in stunden_je_jahr and stunden_je_jahr[j] >= 8700
        zeilen.append(
            {
                "jahr": j,
                "terminpreis": r["terminpreis_eur_mwh"],
                "spot_ist": ist.get(j, float("nan")) if vollstaendig else float("nan"),
                "spot_bisher": ist.get(j, float("nan")),
                "stunden": int(stunden_je_jahr.get(j, 0)),
                **naive_schaetzer(stunden, j),
            }
        )
    df = pd.DataFrame(zeilen)
    for col in ("terminpreis", "spot_16jun_15sep", "spot_12_monate"):
        df[f"fehler_{col}"] = (df[col] - df["spot_ist"]).round(2)
        df[f"fehler_{col}_pct"] = (100 * df[f"fehler_{col}"] / df["spot_ist"]).round(1)
    return df


def kennzahlen(df: pd.DataFrame, ohne: tuple[int, ...] = ()) -> pd.DataFrame:
    d = df.dropna(subset=["spot_ist", "spot_12_monate"])
    d = d[~d["jahr"].isin(ohne)]
    zeilen = []
    for col, name in [
        ("terminpreis", "Terminpreis (ÜNB, Phelix Cal)"),
        ("spot_16jun_15sep", "Spot 16.06.-15.09. Vorjahr"),
        ("spot_12_monate", "Spot 12 Monate bis 15.09. Vorjahr"),
    ]:
        f = d[f"fehler_{col}"]
        p = d[f"fehler_{col}_pct"]
        richtung = (
            (d[col] - d["spot_12_monate"]) * (d["spot_ist"] - d["spot_12_monate"]) > 0
        ).mean()
        zeilen.append(
            {
                "schaetzer": name,
                "jahre": f"{d['jahr'].min()}-{d['jahr'].max()} ({len(d)})",
                "bias": round(f.mean(), 2),
                "mae": round(f.abs().mean(), 2),
                "median_ape_pct": round(p.abs().median(), 1),
                "mape_pct": round(p.abs().mean(), 1),
                "richtung_vs_12m": None if col == "spot_12_monate" else round(100 * richtung),
            }
        )
    return pd.DataFrame(zeilen)


def grafik(df: pd.DataFrame, ziel: Path) -> None:
    d = df[df["jahr"] >= 2016]
    fig, ax = plt.subplots(figsize=(9, 4.8))
    ax.plot(d["jahr"], d["spot_ist"], "o-", color="black", label="Spot realisiert (Jahresmittel)")
    ax.plot(d["jahr"], d["terminpreis"], "s--", color="tab:blue", label="Terminpreis Cal Y+1 (ÜNB)")
    ax.plot(
        d["jahr"],
        d["spot_12_monate"],
        "^:",
        color="tab:orange",
        label="Spot 12 Monate bis 15.09. Vorjahr",
    )
    bisher = d[d["spot_ist"].isna() & (d["stunden"] > 0)]
    ax.plot(
        bisher["jahr"],
        bisher["spot_bisher"],
        "o",
        mfc="none",
        color="black",
        label="Spot bisher (Jahr läuft)",
    )
    ax.set_yscale("log")
    ax.set_yticks([25, 50, 100, 200, 400])
    ax.yaxis.set_major_formatter(matplotlib.ticker.ScalarFormatter())
    ax.yaxis.set_minor_formatter(matplotlib.ticker.NullFormatter())
    ax.set_ylabel("EUR/MWh (log)")
    ax.set_xticks(d["jahr"])
    ax.grid(alpha=0.3)
    ax.legend(fontsize=8)
    ax.set_title("Phelix-DE Base: Terminpreis vom Sommer des Vorjahres gegen das Ist")
    fig.tight_layout()
    fig.savefig(ziel, dpi=150)
    plt.close(fig)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--cache", type=Path, default=HIER / "daten" / "spot_stunden.csv.gz")
    args = ap.parse_args()

    termin = pd.read_csv(HIER / "tabellen" / "terminpreise.csv")
    stunden = spotpreise(args.cache, int(termin["jahr"].max()))
    df = paare(termin, stunden)

    (HIER / "tabellen").mkdir(exist_ok=True)
    (HIER / "grafiken").mkdir(exist_ok=True)
    df.to_csv(HIER / "tabellen" / "paare.csv", index=False)
    k = kennzahlen(df)
    k_ohne = kennzahlen(df, ohne=(2021, 2022, 2023, 2024))
    k.to_csv(HIER / "tabellen" / "kennzahlen.csv", index=False)
    k_ohne.to_csv(HIER / "tabellen" / "kennzahlen_ohne_krise.csv", index=False)
    grafik(df, HIER / "grafiken" / "termin_gegen_ist.png")
    lauf = {
        "spot_von": str(stunden.index.min()),
        "spot_bis": str(stunden.index.max()),
        "stunden": len(stunden),
    }
    (HIER / "tabellen" / "lauf.json").write_text(json.dumps(lauf, indent=2) + "\n")
    with pd.option_context("display.width", 200, "display.max_columns", 30):
        print(df)
        print(k)
        print(k_ohne)


if __name__ == "__main__":
    main()
