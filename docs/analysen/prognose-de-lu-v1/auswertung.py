"""Tabellen und Grafiken zum Backtest des ersten Prognosemodells (de_lu_v1).

Aufruf nach ``ml-train backtest`` (siehe job.yaml)::

    uv run --with matplotlib python docs/analysen/prognose-de-lu-v1/auswertung.py \\
        .ml-data/bt-main .ml-data/bt-wx

Liest ``predictions.parquet`` und ``lauf.json`` beider Läufe und schreibt nach ``tabellen/``
und ``grafiken/`` neben diesem Skript.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd

from ml_train.metrics import add_context, common_rows, daily_rank_corr, score

HERE = Path(__file__).resolve().parent
#: Energiedaten fehlen in der DB vom 18.09. bis 16.10.2026 (alle Quellen); spätere Ausgaben haben
#: keine Lags, daher endet die Wetter-Vorschau mit der Ausgabe am 17.09.2026
WETTER_BIS = pd.Timestamp("2026-09-18", tz="UTC")
TAB, FIG = HERE / "tabellen", HERE / "grafiken"

LABEL = {
    "kalender": "Modell ohne Wetter",
    "wetter": "Modell mit GFS-Wetter",
    "naive_day": "Vortag (letzter bekannter Tag)",
    "naive_week": "Vorwoche",
    "bench_load_da_forecast": "ENTSO-E Day-Ahead",
    "bench_gen_da_forecast_wind_onshore": "ÜNB Day-Ahead",
    "bench_gen_da_forecast_wind_offshore": "ÜNB Day-Ahead",
    "bench_gen_da_forecast_solar": "ÜNB Day-Ahead",
}
TARGET = {
    "load_actual": ("Netzlast", "MW"),
    "da_price_de_lu": ("Day-Ahead-Preis DE-LU", "EUR/MWh"),
    "gen_wind_onshore": ("Wind an Land", "MW"),
    "gen_wind_offshore": ("Wind auf See", "MW"),
    "gen_solar": ("PV", "MW"),
}
COLORS = {
    "kalender": "#1f6feb",
    "wetter": "#d1242f",
    "naive_day": "#8c959f",
    "naive_week": "#57606a",
    "bench": "#1a7f37",
}


def color(m: str) -> str:
    return COLORS["bench"] if m.startswith("bench") else COLORS.get(m, "#999999")


def tables(name: str, pred: pd.DataFrame) -> None:
    ctx = add_context(pred)
    score(ctx, ["method", "target"]).to_csv(
        TAB / f"{name}_gesamt.csv", index=False, float_format="%.3f"
    )
    score(ctx, ["method", "target", "lead_day"]).to_csv(
        TAB / f"{name}_vorlauf.csv", index=False, float_format="%.3f"
    )
    score(ctx, ["method", "target", "season"]).to_csv(
        TAB / f"{name}_jahreszeit.csv", index=False, float_format="%.3f"
    )
    score(ctx, ["method", "target", "year"]).to_csv(
        TAB / f"{name}_jahr.csv", index=False, float_format="%.3f"
    )
    daily_rank_corr(ctx[ctx["target"] == "da_price_de_lu"], ["method", "lead_day"]).to_csv(
        TAB / f"{name}_preis_rangkorrelation.csv", index=False, float_format="%.3f"
    )
    # Folgetag: alle Verfahren auf denselben Stunden
    d1 = ctx[ctx["lead_day"] == 1]
    common = common_rows(d1, sorted(d1["method"].unique()))
    score(add_context(common), ["method", "target"]).to_csv(
        TAB / f"{name}_folgetag_gemeinsam.csv", index=False, float_format="%.3f"
    )


def plot_lead(name: str, pred: pd.DataFrame, targets: list[str]) -> None:
    m = score(add_context(pred), ["method", "target", "lead_day"])
    fig, axes = plt.subplots(1, len(targets), figsize=(5.2 * len(targets), 3.8))
    for ax, t in zip(axes, targets, strict=True):
        d = m[m["target"] == t]
        for meth, g in d.groupby("method"):
            ls = "o" if g["lead_day"].nunique() == 1 else "-o"
            ax.plot(
                g["lead_day"], g["mae"], ls, color=color(meth), label=LABEL.get(meth, meth), ms=4
            )
        title, unit = TARGET[t]
        ax.set_title(title)
        ax.set_xlabel("Liefertag nach Ausgabe (D+n)")
        ax.set_ylabel(f"MAE [{unit}]")
        ax.grid(alpha=0.3)
        ax.set_ylim(bottom=0)
    axes[0].legend(fontsize=8)
    fig.tight_layout()
    fig.savefig(FIG / f"{name}_mae_vorlauf.png", dpi=120)
    plt.close(fig)


def plot_example(pred: pd.DataFrame, method: str, start: str, days: int, out: str) -> None:
    p = pred[(pred["method"] == method) & (pred["lead_day"] == 1)].copy()
    t0 = pd.Timestamp(start, tz="Europe/Berlin")
    p = p[(p["ts"] >= t0) & (p["ts"] < t0 + pd.Timedelta(days=days))]
    bench = pred[
        (pred["method"] == "bench_load_da_forecast")
        & (pred["ts"] >= t0)
        & (pred["ts"] < t0 + pd.Timedelta(days=days))
    ]
    fig, axes = plt.subplots(2, 1, figsize=(11, 6), sharex=True)
    for ax, t in zip(axes, ["load_actual", "da_price_de_lu"], strict=True):
        d = p[p["target"] == t].sort_values("ts")
        x = d["ts"].dt.tz_convert("Europe/Berlin")
        ax.fill_between(
            x, d["q10"], d["q90"], color=color(method), alpha=0.2, step="post", label="P10-P90"
        )
        ax.step(x, d["q50"], where="post", color=color(method), label=f"{LABEL[method]} P50")
        ax.step(x, d["actual"], where="post", color="black", lw=1.2, label="Ist")
        if t == "load_actual":
            b = bench.sort_values("ts")
            ax.step(
                b["ts"].dt.tz_convert("Europe/Berlin"),
                b["q50"],
                where="post",
                color=COLORS["bench"],
                ls="--",
                lw=1,
                label="ENTSO-E Day-Ahead",
            )
        title, unit = TARGET[t]
        ax.set_ylabel(f"{title} [{unit}]")
        ax.grid(alpha=0.3)
        ax.legend(fontsize=8, loc="upper left")
    axes[0].set_title(f"Folgetag-Prognosen (D+1), {t0:%d.%m.%Y} bis {days} Tage")
    fig.tight_layout()
    fig.savefig(FIG / out, dpi=120)
    plt.close(fig)


def main(dirs: list[str]) -> None:
    TAB.mkdir(exist_ok=True)
    FIG.mkdir(exist_ok=True)
    runs = {}
    for name, d in zip(["haupt", "wetter"], dirs, strict=False):
        pred = pd.read_parquet(Path(d) / "predictions.parquet")
        if name == "wetter":
            pred = pred[pred["issue_time"] < WETTER_BIS]
        shutil.copy(Path(d) / "lauf.json", TAB / f"lauf_{name}.json")
        tables(name, pred)
        plot_lead(name, pred, ["load_actual", "da_price_de_lu"])
        plot_lead(f"{name}_erzeugung", pred, ["gen_wind_onshore", "gen_wind_offshore", "gen_solar"])
        runs[name] = json.loads((Path(d) / "lauf.json").read_text())
        if name == "haupt":
            plot_example(pred, "kalender", "2026-01-12", 7, "haupt_beispiel_winter.png")
            plot_example(pred, "kalender", "2026-06-15", 7, "haupt_beispiel_sommer.png")
        else:
            plot_example(pred, "wetter", "2026-09-11", 7, "wetter_beispiel_september.png")
    print(json.dumps({k: v.get("rows") for k, v in runs.items()}))


if __name__ == "__main__":
    main(sys.argv[1:])
