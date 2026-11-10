"""Schwächen des D+15-Champions gegen den v1-Champion (D+1 bis D+7) und die Vorwoche (ab D+8).

Auftrag in ``job.yaml``. Eingang ist die mit ``daten.py`` auf der VM erzeugte Datei
``zeilen.parquet`` (Backtest-Prognosen beider Versionen aus der Registry). Aufruf aus dem
Repository-Wurzelverzeichnis::

    uv run --with matplotlib python docs/analysen/champion-schwaechen-d15/auswertung.py \\
        --cache .ml-data/champion-schwaechen-d15

Verglichen werden die Zeilen (Ziel, Ausgabezeitpunkt, Stunde), die beide Versionen haben, also
die Ausgaben um 08 UTC. Maß ist wie in der Freigaberegel (``ml_train.registry.compare``) der
Pinball-Verlust über P10, P50 und P90 bzw. gegen die Vorwoche nur über P50.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import matplotlib
import numpy as np
import pandas as pd

from ml_train.calendar import BERLIN, calendar_features
from ml_train.metrics import add_context, pinball
from ml_train.registry import week_reference

matplotlib.use("Agg")
import matplotlib.pyplot as plt

OUT = Path(__file__).parent
JOB = OUT / "job.yaml"
PRICE, LOAD = "da_price_de_lu", "load_actual"
STAGE1 = {LOAD: 1, "gen_wind_onshore": -1, "gen_wind_offshore": -1, "gen_solar": -1}
#: Gruppen, die die Freigaberegel am 10.11.2026 bemängelt hat (Ziel, Vorlauftage, Jahreszeit)
FAILED = [
    (PRICE, (6, 7), "Frühling", "kurz"),
    (PRICE, (7,), "Sommer", "kurz"),
    (LOAD, (7,), "Sommer", "kurz"),
    (PRICE, (10, 11, 12, 13), "Frühling", "lang"),
]
QUINTILES = ["Q1 (klein)", "Q2", "Q3", "Q4", "Q5 (groß)"]


def pinball3(y: np.ndarray, q10: np.ndarray, q50: np.ndarray, q90: np.ndarray) -> np.ndarray:
    out: np.ndarray = (pinball(y, q10, 0.1) + pinball(y, q50, 0.5) + pinball(y, q90, 0.9)) / 3
    return out


def short_rows(rows: pd.DataFrame) -> pd.DataFrame:
    """D+1 bis D+7: neu und v1 auf gemeinsamen Zeilen, je Zeile beide Prognosen."""
    key = ["target", "issue_time", "ts", "lead_day"]
    vals = ["q10", "q50", "q90", "actual"]
    neu = rows[(rows["model"] == "neu") & rows["lead_day"].between(1, 7)][key + vals]
    v1 = rows[rows["model"] == "v1"][key + vals]
    m = neu.merge(v1, on=key, suffixes=("_neu", "_ref"))
    m = m.dropna(subset=["q50_neu", "q50_ref", "actual_neu"])
    m = m.rename(columns={"actual_neu": "actual"}).drop(columns="actual_ref")
    y = m["actual"].to_numpy()
    for s in ("neu", "ref"):
        q = [m[f"{c}_{s}"].to_numpy() for c in ("q10", "q50", "q90")]
        m[f"pb_{s}"] = pinball3(y, *q)
        m[f"ae_{s}"] = np.abs(q[1] - y)
        m[f"cov_{s}"] = ((y >= q[0]) & (y <= q[2])).astype(float)
        m[f"wid_{s}"] = q[2] - q[0]
        m[f"bias_{s}"] = q[1] - y
    return add_context(m).assign(klasse="kurz", referenz="v1")


def long_rows(rows: pd.DataFrame) -> pd.DataFrame:
    """D+8 bis D+15: neu gegen die Vorwoche (nur P50, wie in der Freigaberegel)."""
    neu = rows[(rows["model"] == "neu") & (rows["lead_day"] >= 8)].copy()
    neu["target"] = neu["target"].astype(str)
    ref = week_reference(neu)[["target", "issue_time", "ts", "q50"]].rename(
        columns={"q50": "q50_ref"}
    )
    m = neu.merge(ref, on=["target", "issue_time", "ts"]).rename(columns={"q50": "q50_neu"})
    m = m.dropna(subset=["q50_neu", "q50_ref", "actual"])
    y = m["actual"].to_numpy()
    for s in ("neu", "ref"):
        m[f"ae_{s}"] = np.abs(m[f"q50_{s}"].to_numpy() - y)
        m[f"pb_{s}"] = m[f"ae_{s}"] / 2
        m[f"bias_{s}"] = m[f"q50_{s}"].to_numpy() - y
    m["cov_neu"] = ((y >= m["q10"]) & (y <= m["q90"])).astype(float)
    m["wid_neu"] = m["q90"] - m["q10"]
    return add_context(m).assign(klasse="lang", referenz="Vorwoche")


def residual_error(rows: pd.DataFrame) -> pd.DataFrame:
    """Fehler der Residuallast aus Stufe 1 (P50) je Version, Ausgabe und Stunde."""
    s1 = rows[rows["target"].isin(list(STAGE1))].copy()
    s1["sign"] = s1["target"].astype(str).map(STAGE1)
    s1["p"] = s1["q50"] * s1["sign"]
    s1["a"] = s1["actual"] * s1["sign"]
    g = s1.groupby(["model", "issue_time", "ts"], observed=True)[["p", "a"]].sum(min_count=4)
    g = g.dropna()
    g["rl_err"] = g["p"] - g["a"]
    return g["rl_err"].unstack("model").rename(columns={"neu": "rl_neu", "v1": "rl_ref"})


def group_table(m: pd.DataFrame) -> pd.DataFrame:
    g = m.groupby(["klasse", "referenz", "target", "lead_day", "season", "year"], observed=True)
    out = g.agg(
        n=("pb_neu", "size"),
        pinball_neu=("pb_neu", "mean"),
        pinball_ref=("pb_ref", "mean"),
        mae_neu=("ae_neu", "mean"),
        mae_ref=("ae_ref", "mean"),
        bias_neu=("bias_neu", "mean"),
        bias_ref=("bias_ref", "mean"),
    )
    out["mehr_pinball"] = out["pinball_neu"] / out["pinball_ref"] - 1
    out["mehr_mae"] = out["mae_neu"] / out["mae_ref"] - 1
    return out.reset_index()


def failed_mask(m: pd.DataFrame, target: str, leads: tuple[int, ...], season: str) -> pd.Series:
    return (m["target"] == target) & m["lead_day"].isin(leads) & (m["season"] == season)


def concentration(m: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    rows, episodes = [], []
    for target, leads, season, cls in FAILED:
        q = m[failed_mask(m, target, leads, season) & (m["klasse"] == cls)].copy()
        q["ausgabetag"] = pd.DatetimeIndex(q["issue_time"]).tz_convert(BERLIN).date
        q["mehr"] = q["pb_neu"] - q["pb_ref"]
        per_day = q.groupby("ausgabetag")["mehr"].sum().sort_values(ascending=False)
        k = int(np.ceil(len(per_day) * 0.1))
        total = per_day.sum()
        label = f"{target} D+{leads[0]}" + (f" bis D+{leads[-1]}" if len(leads) > 1 else "")
        rows.append(
            {
                "gruppe": f"{label} {season} ({cls})",
                "ausgabetage": len(per_day),
                "mehrfehler_summe": round(total, 1),
                "top10_tage": k,
                "anteil_top10": round(per_day.head(k).sum() / total, 3) if total > 0 else np.nan,
                "anteil_2024": round(
                    per_day[[d.year == 2024 for d in per_day.index]].sum() / total, 3
                )
                if total > 0
                else np.nan,
            }
        )
        top = per_day.head(10)
        for day, extra in top.items():
            d = q[q["ausgabetag"] == day]
            local = pd.DatetimeIndex(d["ts"]).tz_convert(BERLIN)
            episodes.append(
                {
                    "gruppe": rows[-1]["gruppe"],
                    "ausgabetag": day,
                    "zieltage": f"{local.min():%d.%m.%Y} bis {local.max():%d.%m.%Y}",
                    "mehrfehler": round(extra, 1),
                    "ist_mittel": round(d["actual"].mean(), 1),
                    "bias_neu": round(d["bias_neu"].mean(), 1),
                    "bias_ref": round(d["bias_ref"].mean(), 1),
                }
            )
    return pd.DataFrame(rows), pd.DataFrame(episodes)


def by_weather(m: pd.DataFrame, rl: pd.DataFrame) -> pd.DataFrame:
    x = m[m["klasse"] == "kurz"].set_index(["issue_time", "ts"]).join(rl, how="inner")
    x = x.reset_index()
    out = []
    for target, leads, season, _ in FAILED[:3]:
        q = x[failed_mask(x, target, leads, season)].copy()
        q["quintil"] = pd.qcut(q["rl_neu"].abs(), 5, labels=QUINTILES)
        g = q.groupby("quintil", observed=True)
        t = g.agg(
            n=("pb_neu", "size"),
            rl_fehler_neu_mw=("rl_neu", lambda s: s.abs().mean()),
            rl_fehler_ref_mw=("rl_ref", lambda s: s.abs().mean()),
            pinball_neu=("pb_neu", "mean"),
            pinball_ref=("pb_ref", "mean"),
            mae_neu=("ae_neu", "mean"),
            mae_ref=("ae_ref", "mean"),
        )
        t["mehr_pinball"] = t["pinball_neu"] / t["pinball_ref"] - 1
        label = f"{target} D+{leads[0]}" + (f"/D+{leads[-1]}" if len(leads) > 1 else "")
        out.append(t.reset_index().assign(gruppe=f"{label} {season}"))
    res = pd.concat(out, ignore_index=True)
    lead = x[x["target"] == PRICE].groupby("lead_day")
    per_lead = pd.DataFrame(
        {
            "quintil": "alle",
            "gruppe": [f"Preiszeilen D+{d}, alle Jahreszeiten" for d in lead.groups],
            "rl_fehler_neu_mw": lead["rl_neu"].apply(lambda s: s.abs().mean()).to_numpy(),
            "rl_fehler_ref_mw": lead["rl_ref"].apply(lambda s: s.abs().mean()).to_numpy(),
            "n": lead.size().to_numpy(),
        }
    )
    return pd.concat([res, per_lead], ignore_index=True)


def bands(m: pd.DataFrame) -> pd.DataFrame:
    s = m[(m["klasse"] == "kurz") & (m["target"] == PRICE)]
    g = s.groupby(["season", "year", "lead_day"], observed=True)
    out = g.agg(
        n=("pb_neu", "size"),
        abdeckung_neu=("cov_neu", "mean"),
        abdeckung_ref=("cov_ref", "mean"),
        breite_neu=("wid_neu", "mean"),
        breite_ref=("wid_ref", "mean"),
        bias_neu=("bias_neu", "mean"),
        bias_ref=("bias_ref", "mean"),
        pinball_neu=("pb_neu", "mean"),
        pinball_ref=("pb_ref", "mean"),
    )
    return out.reset_index()


def other_factors(m: pd.DataFrame, rows: pd.DataFrame) -> pd.DataFrame:
    q = m[failed_mask(m, PRICE, (6, 7), "Frühling") & (m["klasse"] == "kurz")].copy()
    q = q.reset_index(drop=True)
    cal = calendar_features(pd.DatetimeIndex(q["ts"])).reset_index(drop=True)
    q["feiertag"] = np.where(cal["holiday_share"].to_numpy() > 0, "Feiertag", "kein Feiertag")
    local_day = pd.DatetimeIndex(q["ts"]).tz_convert(BERLIN).normalize().tz_localize(None)
    q["zeitumstellung"] = "übrige Tage"
    for y in sorted(set(local_day.year)):
        end = pd.Timestamp(f"{y}-03-31")
        switch = end - pd.Timedelta(days=(end.day_of_week + 1) % 7)
        week = (local_day >= switch) & (local_day < switch + pd.Timedelta(days=7))
        q.loc[week, "zeitumstellung"] = "7 Tage ab Umstellung"
    # Preisniveausprung: Tagesmittel des Zieltags gegen das des Ausgabetags (Ist)
    price = rows[(rows["model"] == "neu") & (rows["target"] == PRICE)]
    daily = (
        price.drop_duplicates("ts")
        .assign(day=lambda d: pd.DatetimeIndex(d["ts"]).tz_convert(BERLIN).date)
        .groupby("day")["actual"]
        .mean()
    )
    issue_day = pd.DatetimeIndex(q["issue_time"]).tz_convert(BERLIN).date
    target_day = pd.DatetimeIndex(q["ts"]).tz_convert(BERLIN).date
    jump = daily.reindex(target_day).to_numpy() - daily.reindex(issue_day).to_numpy()
    q["sprung"] = pd.qcut(np.abs(jump), 3, labels=["Sprung klein", "mittel", "groß"])
    out = []
    for col in ("feiertag", "zeitumstellung", "sprung"):
        g = q.groupby([col, "year"], observed=True).agg(
            n=("pb_neu", "size"),
            pinball_neu=("pb_neu", "mean"),
            pinball_ref=("pb_ref", "mean"),
            bias_neu=("bias_neu", "mean"),
        )
        g["mehr_pinball"] = g["pinball_neu"] / g["pinball_ref"] - 1
        out.append(g.reset_index().rename(columns={col: "auspraegung"}).assign(faktor=col))
    return pd.concat(out, ignore_index=True)


def figures(groups: pd.DataFrame, weather: pd.DataFrame, band: pd.DataFrame) -> None:
    fig_dir = OUT / "grafiken"
    fig_dir.mkdir(exist_ok=True)
    spring = groups[(groups["target"] == PRICE) & (groups["season"] == "Frühling")]
    fig, ax = plt.subplots(figsize=(9, 4.2))
    for year, d in spring.groupby("year"):
        d = d.sort_values("lead_day")
        ax.plot(d["lead_day"], 100 * d["mehr_pinball"], marker="o", label=str(year))
    ax.axhline(5, color="grey", ls="--", lw=1)
    ax.axhline(0, color="black", lw=0.8)
    ax.axvline(7.5, color="grey", lw=0.8)
    ax.text(4, ax.get_ylim()[1] * 0.9, "gegen v1-Champion", ha="center")
    ax.text(11.5, ax.get_ylim()[1] * 0.9, "gegen Vorwoche", ha="center")
    ax.set_xlabel("Vorlauftag")
    ax.set_ylabel("Mehrfehler Pinball (%)")
    ax.set_title("Preis im Frühling: Mehrfehler des D+15-Champions je Jahr (gestrichelt: 5 %)")
    ax.legend(title="Jahr")
    fig.tight_layout()
    fig.savefig(fig_dir / "mehrfehler_je_vorlauf.png", dpi=150)
    plt.close(fig)

    w = weather[weather["quintil"] != "alle"]
    fig, ax = plt.subplots(figsize=(9, 4.2))
    names = list(dict.fromkeys(w["gruppe"]))
    width = 0.8 / len(names)
    for i, name in enumerate(names):
        d = w[w["gruppe"] == name]
        x = np.arange(len(d)) + i * width
        ax.bar(x, 100 * d["mehr_pinball"], width, label=name)
    ax.set_xticks(np.arange(5) + width * (len(names) - 1) / 2, QUINTILES)
    ax.axhline(0, color="black", lw=0.8)
    ax.set_xlabel("Quintil des Residuallastfehlers der Stufe 1 (neues Modell)")
    ax.set_ylabel("Mehrfehler Pinball gegen v1 (%)")
    ax.set_title("Mehrfehler nach Größe des Wetter- bzw. Residuallastfehlers")
    ax.legend()
    fig.tight_layout()
    fig.savefig(fig_dir / "mehrfehler_nach_wetterfehler.png", dpi=150)
    plt.close(fig)

    b = band[band["season"] == "Frühling"]
    fig, ax = plt.subplots(figsize=(9, 4.2))
    for year, d in b.groupby("year"):
        (line,) = ax.plot(d["lead_day"], 100 * d["abdeckung_neu"], marker="o", label=f"{year} neu")
        ax.plot(
            d["lead_day"],
            100 * d["abdeckung_ref"],
            ls="--",
            color=line.get_color(),
            label=f"{year} v1",
        )
    ax.axhline(80, color="grey", lw=0.8)
    ax.set_xlabel("Vorlauftag")
    ax.set_ylabel("Abdeckung P10-P90 (%)")
    ax.set_title("Preis im Frühling: Anteil der Ist-Werte im Band (Soll 80 %)")
    ax.legend(ncol=3, fontsize=8)
    fig.tight_layout()
    fig.savefig(fig_dir / "abdeckung_je_vorlauf.png", dpi=150)
    plt.close(fig)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache", required=True, type=Path, help="Ordner mit zeilen.parquet")
    a = ap.parse_args()
    started = datetime.now(UTC)
    rows = pd.read_parquet(a.cache / "zeilen.parquet")
    rows["model"] = rows["model"].astype(str)
    rows["target"] = rows["target"].astype(str)
    short, long_ = short_rows(rows), long_rows(rows)
    m = pd.concat([short, long_], ignore_index=True)
    rl = residual_error(rows)

    tab = OUT / "tabellen"
    tab.mkdir(exist_ok=True)
    groups = group_table(m[m["target"].isin([PRICE, LOAD])])
    groups.round(4).to_csv(tab / "gruppen.csv", index=False)
    conc, episodes = concentration(m)
    conc.to_csv(tab / "konzentration.csv", index=False)
    episodes.to_csv(tab / "episoden.csv", index=False)
    weather = by_weather(m, rl)
    weather.round(4).to_csv(tab / "wetterfehler.csv", index=False)
    band = bands(m)
    band.round(4).to_csv(tab / "bandbreite.csv", index=False)
    other_factors(m, rows).round(4).to_csv(tab / "andere.csv", index=False)
    figures(groups, weather, band)

    commit = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True)
    info: dict[str, Any] = {
        "job": "champion-schwaechen-d15",
        "git_commit": commit.stdout.strip(),
        "job_sha256": hashlib.sha256(JOB.read_bytes()).hexdigest(),
        "gestartet": started.isoformat(timespec="seconds"),
        "beendet": datetime.now(UTC).isoformat(timespec="seconds"),
        "eingang": {
            "datei": "zeilen.parquet (daten.py, Registry-Backtests)",
            "zeilen": len(rows),
            "versionen": sorted(rows["version"].unique()),
        },
        "zeitraum": {
            "ausgabe_von": str(m["issue_time"].min()),
            "ausgabe_bis": str(m["issue_time"].max()),
        },
        "umfang": {
            "gemeinsame_zeilen_kurz": len(short),
            "zeilen_lang_mit_vorwoche": len(long_),
            "ausgabestunde_utc_kurz": sorted(pd.DatetimeIndex(short["issue_time"]).hour.unique()),
        },
        "annahmen": [
            "Jahreszeit meteorologisch nach Ortszeit der Zielstunde (ml_train.metrics)",
            "Residuallast = Last - Wind an Land - Wind auf See - PV, je P50 der Stufe 1",
            "Zeitumstellung: 7 Tage ab dem letzten Sonntag im März",
        ],
    }
    (tab / "lauf.json").write_text(json.dumps(info, indent=1, ensure_ascii=False, default=int))
    print(json.dumps(info["umfang"], default=int))


if __name__ == "__main__":
    main()
