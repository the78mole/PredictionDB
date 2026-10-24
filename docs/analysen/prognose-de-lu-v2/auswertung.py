"""Ablation der freien Zusatzreihen im Prognosemodell de_lu_v2 (Tabellen und Grafiken).

Aufruf nach den vier ``ml-train backtest``-Läufen aus job.yaml::

    uv run --with matplotlib python docs/analysen/prognose-de-lu-v2/auswertung.py \\
        .ml-data/bt2-haupt .ml-data/bt2-09utc .ml-data/bt2-wetter .ml-data/bt2-niveau

Je Lauf werden alle Varianten auf denselben Zeilen verglichen (Ziel, Ausgabe, Stunde, für die
jede Variante eine Prognose hat). Schreibt nach ``tabellen/`` und ``grafiken/`` neben diesem
Skript.
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
TAB, FIG = HERE / "tabellen", HERE / "grafiken"
RUNS = ["haupt", "09utc", "wetter", "niveau"]

LABEL = {
    "kalender": "Maßstab (wie v1)",
    "nachbarn": "+ Nachbarzonen-Preise",
    "lastprognose": "+ Lastprognose ÜNB",
    "brennstoffe": "+ Brennstoffe",
    "marktwerte": "+ Marktwerte",
    "frei": "alle freien Reihen",
    "preisniveau": "+ 28-Tage-Mittel Preis DE-LU",
    "frei_niveau": "alle freien Reihen + 28-Tage-Mittel",
    "wetter": "GFS-Wetter",
    "wetter_frei": "GFS-Wetter + alle freien Reihen",
    "naive_day": "Vortag",
    "naive_week": "Vorwoche",
    "bench_load_da_forecast": "ÜNB Day-Ahead (Last)",
}
TARGET = {
    "load_actual": ("Netzlast", "MW"),
    "da_price_de_lu": ("Day-Ahead-Preis DE-LU", "EUR/MWh"),
    "gen_wind_onshore": ("Wind an Land", "MW"),
    "gen_wind_offshore": ("Wind auf See", "MW"),
    "gen_solar": ("PV", "MW"),
}
COLORS = {
    "kalender": "#57606a",
    "nachbarn": "#1f6feb",
    "lastprognose": "#1a7f37",
    "brennstoffe": "#bf8700",
    "marktwerte": "#8250df",
    "frei": "#d1242f",
    "preisniveau": "#bc4c00",
    "frei_niveau": "#6e7781",
    "wetter": "#0969da",
    "wetter_frei": "#cf222e",
}


def variants(pred: pd.DataFrame) -> list[str]:
    return [m for m in LABEL if m in set(pred["method"]) and m in COLORS]


def relative(m: pd.DataFrame, base: str, keys: list[str]) -> pd.DataFrame:
    """MAE und Pinball relativ zum Maßstab ``base`` (negativ = besser)."""
    b = m[m["method"] == base].set_index(keys)[["mae", "pinball"]]
    out = m.set_index(["method", *keys])
    for c in ("mae", "pinball"):
        out[f"{c}_rel"] = out[c] / b[c].reindex(out.index.droplevel("method")).to_numpy() - 1
    return out.reset_index()


def tables(name: str, pred: pd.DataFrame, base: str) -> pd.DataFrame:
    vs = variants(pred)
    common = add_context(common_rows(pred, vs))
    keys_all = {"gesamt": ["target"], "vorlauf": ["target", "lead_day"]}
    for suffix, keys in keys_all.items():
        rel = relative(score(common, ["method", *keys]), base, keys)
        rel.to_csv(TAB / f"{name}_{suffix}.csv", index=False, float_format="%.4f")
    for suffix, extra in {"jahreszeit": "season", "jahr": "year"}.items():
        keys = ["target", extra]
        rel = relative(score(common, ["method", *keys]), base, keys)
        rel.to_csv(TAB / f"{name}_{suffix}.csv", index=False, float_format="%.4f")
    daily_rank_corr(common[common["target"] == "da_price_de_lu"], ["method", "lead_day"]).to_csv(
        TAB / f"{name}_preis_rangkorrelation.csv", index=False, float_format="%.4f"
    )
    # Folgetag mit Vergleichsverfahren auf denselben Stunden
    d1 = pred[pred["lead_day"] == 1]
    c1 = common_rows(d1, sorted(d1["method"].unique()))
    score(add_context(c1), ["method", "target"]).to_csv(
        TAB / f"{name}_folgetag_gemeinsam.csv", index=False, float_format="%.3f"
    )
    return relative(score(common, ["method", "target", "lead_day"]), base, ["target", "lead_day"])


def plot_gain(name: str, rel: pd.DataFrame, base: str, targets: list[str]) -> None:
    fig, axes = plt.subplots(1, len(targets), figsize=(5.4 * len(targets), 3.8), squeeze=False)
    for ax, t in zip(axes[0], targets, strict=True):
        d = rel[(rel["target"] == t) & (rel["method"] != base)]
        for meth, g in d.groupby("method"):
            ax.plot(
                g["lead_day"],
                100 * g["mae_rel"],
                "-o",
                ms=4,
                color=COLORS.get(str(meth), "#999"),
                label=LABEL.get(str(meth), str(meth)),
            )
        ax.axhline(0, color="black", lw=0.8)
        ax.set_title(TARGET[t][0])
        ax.set_xlabel("Liefertag nach Ausgabe (D+n)")
        ax.set_ylabel(f"MAE gegenüber {LABEL[base]} [%]")
        ax.grid(alpha=0.3)
    axes[0][0].legend(fontsize=8)
    fig.tight_layout()
    fig.savefig(FIG / f"{name}_mae_gewinn.png", dpi=120)
    plt.close(fig)


def main(dirs: list[str]) -> None:
    TAB.mkdir(exist_ok=True)
    FIG.mkdir(exist_ok=True)
    rows = {}
    for name, d in zip(RUNS, dirs, strict=False):
        pred = pd.read_parquet(Path(d) / "predictions.parquet")
        shutil.copy(Path(d) / "lauf.json", TAB / f"lauf_{name}.json")
        base = "wetter" if name == "wetter" else "kalender"
        rel = tables(name, pred, base)
        plot_gain(name, rel, base, ["load_actual", "da_price_de_lu"])
        if name in ("haupt", "wetter"):
            plot_gain(
                f"{name}_erzeugung",
                rel,
                base,
                ["gen_wind_onshore", "gen_wind_offshore", "gen_solar"],
            )
        rows[name] = len(pred)
    print(json.dumps(rows))


if __name__ == "__main__":
    main(sys.argv[1:])
