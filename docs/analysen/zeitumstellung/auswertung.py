"""Zeitumstellung: Wie stark verschieben Lag-Merkmale mit festen 24 h die Tagesform?

Auftrag in ``job.yaml``, Ergebnisse daneben (README.md, tabellen/, grafiken/). Aufruf aus dem
Repository-Wurzelverzeichnis::

    uv run --with matplotlib python docs/analysen/zeitumstellung/auswertung.py

Für Last (DE) und Day-Ahead-Preis (DE-LU) aus SMARD wird der Wert einer Zielstunde mit dem
Wert k Tage früher verglichen, einmal als feste Zeitspanne (k·24 h in UTC, wie ``ml_train``
bis zu diesem PR) und einmal als dieselbe Uhrzeit k Ortstage früher. Beide sind naive
Prognosen; ihr Unterschied zeigt, wie viel Fehler allein die Verschiebung um eine Stunde in den
Tagen nach einer Zeitumstellung einträgt. Lizenz der Daten: CC BY 4.0, Bundesnetzagentur |
SMARD.de.
"""

from __future__ import annotations

import argparse
import json
from datetime import date, timedelta
from pathlib import Path

import httpx
import pandas as pd

BASE = "https://www.smard.de/app/chart_data"
BERLIN = "Europe/Berlin"
HERE = Path(__file__).parent
SERIES = {
    "load": (410, "DE", "Last DE [GW]", 1e-3),
    "residual_load": (4359, "DE", "Residuallast DE [GW]", 1e-3),
    "pv": (4068, "DE", "PV DE [GW]", 1e-3),
    "price": (4169, "DE-LU", "Preis [€/MWh]", 1.0),
}
LAGS = {"tag_k1": 1, "tag_k2": 2, "woche": 7}
START = pd.Timestamp("2018-10-01", tz="UTC")


def fetch(fid: int, region: str, cache: Path) -> pd.Series:
    """Stundenwerte einer SMARD-Reihe (Index UTC, Intervallbeginn)."""
    f = cache / f"{fid}_{region}_hour.json"
    if not f.exists():
        with httpx.Client(timeout=60) as c:
            idx = c.get(f"{BASE}/{fid}/{region}/index_hour.json").json()["timestamps"]
            pts: list[list[float | None]] = []
            for ts in idx:
                if ts < START.value // 10**6 - 8 * 86400000:
                    continue
                url = f"{BASE}/{fid}/{region}/{fid}_{region}_hour_{ts}.json"
                pts += c.get(url).json()["series"]
        f.write_text(json.dumps(pts))
    pts = json.loads(f.read_text())
    s = pd.Series(
        [p[1] for p in pts],
        index=pd.to_datetime([p[0] for p in pts], unit="ms", utc=True),
        dtype=float,
    )
    s = s[~s.index.duplicated()].sort_index()
    return s[s.index >= START].dropna()


def changes(years: range) -> list[tuple[date, str]]:
    """Tage der Zeitumstellung (letzter Sonntag im März bzw. Oktober)."""
    out = []
    for y in years:
        for m, kind in ((3, "Frühjahr"), (10, "Herbst")):
            d = date(y, m, 31)
            out.append((d - timedelta(days=(d.weekday() + 1) % 7), kind))
    return out


def lagged(s: pd.Series, k: int) -> tuple[pd.Series, pd.Series]:
    """Wert k·24 h früher (UTC) und Wert zur selben Ortsuhrzeit k Ortstage früher."""
    utc = s.reindex(s.index - pd.Timedelta(days=k))
    utc.index = s.index
    wall = s.index.tz_convert(BERLIN).tz_localize(None)
    by_wall = pd.Series(s.to_numpy(), index=wall)
    by_wall = by_wall[~by_wall.index.duplicated(keep="first")]
    loc = by_wall.reindex(wall - pd.Timedelta(days=k))
    loc.index = s.index
    # Fehlende Ortsstunde (02 Uhr am 23-h-Tag): auf die feste Zeitspanne ausweichen
    return utc, loc.fillna(utc)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache", type=Path, default=Path(".cache/zeitumstellung"))
    a = ap.parse_args()
    a.cache.mkdir(parents=True, exist_ok=True)
    (HERE / "tabellen").mkdir(exist_ok=True)
    (HERE / "grafiken").mkdir(exist_ok=True)
    last = pd.Timestamp.now(tz="UTC").year
    rows, hourly = [], []
    for name, (fid, region, _label, scale) in SERIES.items():
        s = fetch(fid, region, a.cache) * scale
        local_day = pd.Series(s.index.tz_convert(BERLIN).date, index=s.index)
        for lag, k in LAGS.items():
            utc, loc = lagged(s, k)
            e_utc, e_loc = (utc - s).abs(), (loc - s).abs()
            moved = (utc - loc).abs() > 1e-9
            for c, kind in changes(range(2019, last + 1)):
                # betroffene Zieltage: Umstellungstag bis k Tage danach
                days = [c + timedelta(days=i) for i in range(k + 1)]
                m = local_day.isin(days) & e_utc.notna() & e_loc.notna()
                if not m.any():
                    continue
                rows.append(
                    {
                        "reihe": name,
                        "lag": lag,
                        "umstellung": c.isoformat(),
                        "art": kind,
                        "stunden": int(m.sum()),
                        "verschoben": int((m & moved).sum()),
                        "mae_utc": e_utc[m].mean(),
                        "mae_ortszeit": e_loc[m].mean(),
                    }
                )
                hd = s.index[m].tz_convert(BERLIN)
                hourly.append(
                    pd.DataFrame(
                        {
                            "reihe": name,
                            "lag": lag,
                            "art": kind,
                            "stunde": hd.hour,
                            "diff": (e_utc[m] - e_loc[m]).to_numpy(),
                        }
                    )
                )
            # Jahresmittel: Anteil des Gesamtfehlers, der auf die Verschiebung entfällt
            ok = e_utc.notna() & e_loc.notna() & (s.index >= pd.Timestamp("2019-01-01", tz="UTC"))
            rows.append(
                {
                    "reihe": name,
                    "lag": lag,
                    "umstellung": "alle Stunden",
                    "art": "gesamt",
                    "stunden": int(ok.sum()),
                    "verschoben": int((ok & moved).sum()),
                    "mae_utc": e_utc[ok].mean(),
                    "mae_ortszeit": e_loc[ok].mean(),
                }
            )
    df = pd.DataFrame(rows)
    df["gewinn_%"] = 100 * (1 - df["mae_ortszeit"] / df["mae_utc"])
    df.round(3).to_csv(HERE / "tabellen/je_umstellung.csv", index=False)
    summ = (
        df[df["art"] != "gesamt"]
        .groupby(["reihe", "lag", "art"])
        .agg(
            n=("umstellung", "count"),
            mae_utc=("mae_utc", "mean"),
            mae_ortszeit=("mae_ortszeit", "mean"),
        )
        .reset_index()
    )
    summ["gewinn_%"] = 100 * (1 - summ["mae_ortszeit"] / summ["mae_utc"])
    summ = pd.concat([summ, df[df["art"] == "gesamt"][summ.columns.drop("n").tolist()]])
    summ.round(3).to_csv(HERE / "tabellen/zusammenfassung.csv", index=False)
    print(summ.round(2).to_string(index=False))

    h = pd.concat(hourly).groupby(["reihe", "lag", "art", "stunde"])["diff"].mean().reset_index()
    h.round(3).to_csv(HERE / "tabellen/je_ortsstunde.csv", index=False)
    plot(h, s_load_example(a.cache))


def s_load_example(cache: Path) -> pd.DataFrame:
    """Last am Montag 26.10.2026 gegen die beiden Vorwochenlags als Beispiel."""
    s = fetch(410, "DE", cache) * 1e-3
    utc, loc = lagged(s, 7)
    day = pd.Timestamp("2026-10-26", tz=BERLIN)
    m = (s.index >= day) & (s.index < day + pd.Timedelta(days=1))
    return pd.DataFrame({"ist": s[m], "utc": utc[m], "ortszeit": loc[m]})


def plot(h: pd.DataFrame, ex: pd.DataFrame) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, axes = plt.subplots(2, 2, figsize=(11, 7), sharex=True)
    for ax, (name, (_f, _r, label, _s)) in zip(axes.flat, SERIES.items(), strict=True):
        for (lag, art), g in h[h["reihe"] == name].groupby(["lag", "art"]):
            if lag == "tag_k1":
                continue
            ax.plot(g["stunde"], g["diff"], marker="o", ms=3, label=f"{lag}, {art}")
        ax.axhline(0, color="grey", lw=0.8)
        ax.set_title(f"{label}: Mehrfehler 24-h-Lag gegen Ortszeit-Lag")
        ax.set_xlabel("Ortsstunde der Zielzeit")
        ax.set_xticks(range(0, 24, 3))
    axes.flat[0].legend(fontsize=8)
    fig.tight_layout()
    fig.savefig(HERE / "grafiken/mehrfehler_je_stunde.png", dpi=130)

    if len(ex):
        fig, ax = plt.subplots(figsize=(8, 3.5))
        x = ex.index.tz_convert(BERLIN).hour
        ax.plot(x, ex["ist"], color="black", label="Ist 26.10.2026")
        ax.plot(x, ex["utc"], ls="--", label="Lag 7·24 h (UTC) = 19.10. eine Stunde später")
        ax.plot(x, ex["ortszeit"], ls=":", label="Lag gleiche Ortsuhrzeit 19.10.")
        ax.set_xlabel("Ortsstunde")
        ax.set_ylabel("Last [GW]")
        ax.legend(fontsize=8)
        fig.tight_layout()
        fig.savefig(HERE / "grafiken/beispiel_26_10_2026.png", dpi=130)


if __name__ == "__main__":
    main()
