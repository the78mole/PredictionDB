"""Vorhersagegüte von Flusstemperatur und Abfluss mit echten Vorläufen D+1 bis D+7.

Eingabe aus abruf.py, Ausgabe nach tabellen/ und grafiken/. Zum Ausgabezeitpunkt (Lauf 00Z des
Tages T) ist die Messung bis T-1 bekannt. Alle Modelle sind lineare Regressionen, geprüft je
Kalenderjahr des Zieltags und gelernt auf den übrigen Jahren (ein Jahr auslassen), ausgewertet im
Sommer.

Aufruf: uv run --no-project --with pandas --with numpy --with matplotlib --with pyyaml python \
    docs/analysen/kkw-vorhersage/auswertung.py --cache /tmp/kkwv
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import subprocess
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

HIER = Path(__file__).resolve().parent
sys.path.insert(0, str(HIER.parent / "kkw-kuehlwasser-fr"))
from daten import PEGEL_FEST  # type: ignore[import-not-found]  # noqa: E402

SOMMER = [5, 6, 7, 8, 9]
VORLAEUFE = range(1, 8)
NAME = {
    "BUG": "Bugey",
    "SAL": "Saint-Alban",
    "TRI": "Tricastin",
    "CRU": "Cruas",
    "GOL": "Golfech",
    "BEL": "Belleville",
    "DAM": "Dampierre",
    "SLB": "Saint-Laurent",
    "CHI": "Chinon",
    "CIV": "Civaux",
    "CAT": "Cattenom",
    "CHO": "Chooz",
    "NOG": "Nogent",
}
#: Wassertemperatur: je Station nur ein Standort, Cruas teilt sich die Station mit Tricastin
TEMP_ZIELE = ["TRI", "GOL", "SLB"]
#: Cruas teilt sich den Pegel mit Tricastin, Dampierre liegt am selben Fluss wie Saint-Laurent
ABFLUSS_ZIELE = ["BUG", "SAL", "TRI", "GOL", "BEL", "DAM", "SLB", "CHI", "CIV", "CAT", "CHO", "NOG"]


def lade_gfs(cache: Path) -> pd.DataFrame:
    df = pd.concat(pd.read_csv(f) for f in sorted((cache / "gfs").glob("*.csv")))
    df["tag"] = pd.to_datetime(df["tag"])
    return df


def lade_hubeau(cache: Path) -> pd.DataFrame:
    ziel = cache / "hubeau"
    wahl = json.loads((ziel / "stationen.json").read_text())
    for code, (pegel, _) in PEGEL_FEST.items():
        wahl[code]["abfluss"] = pegel
    reihen: dict[str, pd.Series] = {}
    for code, w in wahl.items():
        if w.get("temperatur"):
            t = pd.read_csv(ziel / f"temp_{w['temperatur']}.csv")
            t = t[(t["grad_c"] > -1) & (t["grad_c"] < 40)]
            t["tag"] = pd.to_datetime(t["datum"])
            reihen[f"{code}|wasser"] = t.groupby("tag")["grad_c"].mean()
        if w.get("abfluss"):
            q = pd.read_csv(ziel / f"q_{w['abfluss']}.csv")
            q["tag"] = pd.to_datetime(q["datum"])
            q = q.drop_duplicates("tag").set_index("tag")["l_s"] / 1000.0
            reihen[f"{code}|abfluss"] = q[q > 0]
    return pd.DataFrame(reihen).asfreq("D")


def matrix(g: pd.DataFrame, wert: str) -> pd.DataFrame:
    """Zeilen: Ausgabetag, Spalten: Vorlauf."""
    return g.pivot_table(index="tag", columns="vorlauf", values=wert).asfreq("D")


def fenster_mittel(ist: pd.Series, vor: pd.DataFrame, d: int, n: int) -> pd.Series:
    """Mittel über die ``n`` Tage bis T+d: Wetter ab T aus dem Lauf T, davor das eingetretene."""
    werte = []
    for k in range(n):
        versatz = d - k
        if versatz >= 0:
            werte.append(vor[versatz])
        else:
            werte.append(ist.shift(-versatz))
    return pd.concat(werte, axis=1).mean(axis=1, skipna=True)


def fenster_summe_perfekt(ist: pd.Series, a: int, b: int) -> pd.Series:
    """Summe des eingetretenen Werts über T+a bis T+b."""
    return pd.concat([ist.shift(-k) for k in range(a, b + 1)], axis=1).sum(axis=1, skipna=False)


def ols(x: pd.DataFrame, y: pd.Series, lern: pd.Series, test: pd.Series) -> pd.Series:
    a = np.column_stack([np.ones(int(lern.sum())), x[lern].to_numpy()])
    beta, *_ = np.linalg.lstsq(a, y[lern].to_numpy(), rcond=None)
    vor = np.column_stack([np.ones(int(test.sum())), x[test].to_numpy()]) @ beta
    return pd.Series(vor, index=x.index[test])


def je_jahr(
    x: pd.DataFrame, y: pd.Series, gut: pd.Series, ziel_tag: pd.Series, test: pd.Series
) -> pd.Series:
    """Prognose für die Zeilen ``test``: je Jahr des Zieltags gelernt auf allen anderen Jahren."""
    jahr = ziel_tag.dt.year
    teile = []
    for j in sorted(jahr[test].unique()):
        lern = gut & (jahr != j)
        t = test & (jahr == j)
        if lern.sum() >= 200 and t.any():
            teile.append(ols(x, y, lern, t))
    return pd.concat(teile) if teile else pd.Series(dtype=float)


def schwellen_guete(ist: pd.Series, vor: pd.Series, s: float, unter: bool) -> dict[str, float]:
    a = ist < s if unter else ist >= s
    b = vor < s if unter else vor >= s
    treffer = int((a & b).sum())
    verpasst = int((a & ~b).sum())
    falsch = int((~a & b).sum())
    return {
        "ereignisse": int(a.sum()),
        "trefferquote": round(treffer / (treffer + verpasst), 3) if treffer + verpasst else np.nan,
        "fehlalarmquote": round(falsch / (treffer + falsch), 3) if treffer + falsch else np.nan,
    }


def wasser_merkmale(
    luft: pd.DataFrame, strahl: pd.DataFrame, abfluss: pd.Series, d: int, vorhersage: bool
) -> pd.DataFrame:
    """Merkmale für den Zieltag T+d; ``vorhersage=False`` nimmt das eingetretene Wetter."""
    luft_ist, strahl_ist = luft[0], strahl[0]
    v_luft = luft if vorhersage else pd.DataFrame({k: luft_ist.shift(-k) for k in range(8)})
    v_str = strahl if vorhersage else pd.DataFrame({k: strahl_ist.shift(-k) for k in range(8)})
    x = {f"luft{n}": fenster_mittel(luft_ist, v_luft, d, n) for n in (3, 14, 30)}
    x["strahlung7"] = fenster_mittel(strahl_ist, v_str, d, 7)
    x["log_abfluss"] = abfluss.shift(1).ffill(limit=5)
    return pd.DataFrame(x)


def wassertemperatur(
    gfs: pd.DataFrame, mess: pd.DataFrame
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    zeilen, schw, fehler = [], [], []
    for code in TEMP_ZIELE:
        if f"{code}|wasser" not in mess:
            continue
        p = gfs[(gfs["standort"] == code) & (gfs["ort"] == "punkt")]
        luft = matrix(p, "t_mittel")
        strahl = matrix(p, "dswrf")
        tag = luft.index.union(mess.index)
        luft, strahl = luft.reindex(tag), strahl.reindex(tag)
        wasser = mess[f"{code}|wasser"].reindex(tag)
        abfluss = (
            np.log(mess[f"{code}|abfluss"].reindex(tag))
            if f"{code}|abfluss" in mess
            else pd.Series(np.nan, index=tag)
        )
        luft_ist = luft[0]

        for d in VORLAEUFE:
            y = wasser.shift(-d)
            ziel_tag = pd.Series(y.index + pd.Timedelta(days=d), index=y.index)
            x_v = wasser_merkmale(luft, strahl, abfluss, d, True)
            x_p = wasser_merkmale(luft, strahl, abfluss, d, False)
            x_p = x_p.drop(columns=[c for c in x_p if x_p[c].isna().all()])
            x_v = x_v[x_p.columns]
            gut = (
                x_p.notna().all(axis=1)
                & x_v.notna().all(axis=1)
                & y.notna()
                & wasser.shift(1).notna()
            )
            som = pd.Series(ziel_tag.dt.month.isin(SOMMER).to_numpy(), index=y.index)
            vor_v = je_jahr(x_v, y, gut, ziel_tag, gut & som)
            if len(vor_v) < 50:
                continue
            test_s = pd.Series(y.index.isin(vor_v.index), index=y.index)
            ist = y[test_s]
            modelle: dict[str, pd.Series] = {"persistenz": wasser.shift(1)[test_s]}
            modelle["luft_vorhersage"] = vor_v
            modelle["luft_perfekt"] = je_jahr(x_p, y, gut, ziel_tag, test_s)
            modelle["direkt"] = je_jahr(
                x_v.assign(letzte=wasser.shift(1)), y, gut, ziel_tag, test_s
            )
            for name, vor in modelle.items():
                zeilen.append(
                    {
                        "standort": code,
                        "name": NAME[code],
                        "vorlauf": d,
                        "modell": name,
                        "n_test": len(ist),
                        "jahre": int(ziel_tag[test_s].dt.year.nunique()),
                        "mae_k": round(float((ist - vor).abs().mean()), 3),
                        "bias_k": round(float((vor - ist).mean()), 3),
                    }
                )
                for s in (24.0, 26.0):
                    schw.append(
                        {
                            "standort": code,
                            "name": NAME[code],
                            "vorlauf": d,
                            "modell": name,
                            "schwelle_c": s,
                            **schwellen_guete(ist, vor, s, unter=False),
                        }
                    )
        for d in VORLAEUFE:
            f = (luft[d] - luft_ist.shift(-d)).dropna()
            f = f[(f.index + pd.Timedelta(days=d)).month.isin(SOMMER)]
            fehler.append(
                {
                    "standort": code,
                    "name": NAME[code],
                    "vorlauf": d,
                    "groesse": "luft_c",
                    "mae": round(float(f.abs().mean()), 3),
                    "bias": round(float(f.mean()), 3),
                }
            )
    return pd.DataFrame(zeilen), pd.DataFrame(schw), pd.DataFrame(fehler)


def abfluss(
    gfs: pd.DataFrame, mess: pd.DataFrame
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    zeilen, schw, fehler = [], [], []
    for code in ABFLUSS_ZIELE:
        if f"{code}|abfluss" not in mess:
            continue
        k = gfs[(gfs["standort"] == code) & (gfs["ort"] == "einzugsgebiet")]
        if k.empty:
            continue
        regen = matrix(k, "niederschlag_mm")
        boden = matrix(k, "soilw")[0]
        schnee = matrix(k, "weasd")[0]
        tag = regen.index.union(mess.index)
        regen = regen.reindex(tag)
        boden, schnee = boden.reindex(tag), schnee.reindex(tag)
        q = mess[f"{code}|abfluss"].reindex(tag)
        lq = np.log(q)
        regen_ist = regen[0]
        doy = pd.DatetimeIndex(tag).dayofyear.to_numpy()
        basis = pd.DataFrame(
            {
                "lq1": lq.shift(1),
                "lq8": lq.shift(8),
                "regen7": regen_ist.rolling(7, min_periods=4).sum(),
                "regen30": regen_ist.rolling(30, min_periods=15).sum(),
                "regen90": regen_ist.rolling(90, min_periods=45).sum(),
                "boden": boden,
                "schnee": schnee,
                "sin": np.sin(2 * np.pi * doy / 365.25),
                "cos": np.cos(2 * np.pi * doy / 365.25),
            },
            index=tag,
        )
        basis = basis.drop(columns=[c for c in basis if basis[c].isna().all()])
        for d in VORLAEUFE:
            y = lq.shift(-d)
            ziel_tag = pd.Series(y.index + pd.Timedelta(days=d), index=y.index)
            kommend_v = regen[list(range(1, d + 1))].sum(axis=1, skipna=False)
            kommend_p = fenster_summe_perfekt(regen_ist, 1, d)
            x_v = basis.assign(regen_kommend=kommend_v)
            x_p = basis.assign(regen_kommend=kommend_p)
            gut = x_v.notna().all(axis=1) & x_p.notna().all(axis=1) & y.notna()
            som = pd.Series(ziel_tag.dt.month.isin(SOMMER).to_numpy(), index=y.index)
            vor_v = je_jahr(x_v, y, gut, ziel_tag, gut & som)
            if len(vor_v) < 50:
                continue
            test_s = pd.Series(y.index.isin(vor_v.index), index=y.index)
            ist = y[test_s]
            # Median über alle Tage: Bezug der Schwelle, nicht Teil des Modells
            median = float(np.exp(lq.median()))
            modelle = {
                "persistenz": lq.shift(1)[test_s],
                "direkt_vorhersage": vor_v,
                "direkt_perfekt": je_jahr(x_p, y, gut, ziel_tag, test_s),
            }
            for name, vor in modelle.items():
                zeilen.append(
                    {
                        "standort": code,
                        "name": NAME[code],
                        "vorlauf": d,
                        "modell": name,
                        "n_test": len(ist),
                        "jahre": int(ziel_tag[test_s].dt.year.nunique()),
                        "mae_log": round(float((ist - vor).abs().mean()), 4),
                        "bias_log": round(float((vor - ist).mean()), 4),
                        "median_m3s": round(median, 1),
                    }
                )
                schw.append(
                    {
                        "standort": code,
                        "name": NAME[code],
                        "vorlauf": d,
                        "modell": name,
                        "schwelle_anteil_median": 0.5,
                        **schwellen_guete(ist, vor, float(np.log(0.5 * median)), unter=True),
                    }
                )
            f = (kommend_v - kommend_p).dropna()
            f = f[(f.index + pd.Timedelta(days=d)).month.isin(SOMMER)]
            fehler.append(
                {
                    "standort": code,
                    "name": NAME[code],
                    "vorlauf": d,
                    "groesse": "regen_summe_mm",
                    "mae": round(float(f.abs().mean()), 3),
                    "bias": round(float(f.mean()), 3),
                }
            )
    return pd.DataFrame(zeilen), pd.DataFrame(schw), pd.DataFrame(fehler)


FARBE = {
    "persistenz": "#888888",
    "luft_vorhersage": "#d95f02",
    "luft_perfekt": "#fdae6b",
    "direkt": "#1b9e77",
    "direkt_vorhersage": "#1b9e77",
    "direkt_perfekt": "#a1d99b",
}


def grafik(df: pd.DataFrame, wert: str, y: str, titel: str, datei: Path) -> None:
    codes = list(df["standort"].unique())
    spalten = min(4, len(codes)) or 1
    zeilen = int(np.ceil(len(codes) / spalten))
    fig, axes = plt.subplots(zeilen, spalten, figsize=(3.4 * spalten, 2.8 * zeilen), squeeze=False)
    for ax, code in zip(axes.flat, codes, strict=False):
        g = df[df["standort"] == code]
        for m, h in g.groupby("modell"):
            ax.plot(h["vorlauf"], h[wert], marker="o", ms=3, label=m, color=FARBE[str(m)])
        ax.set_title(str(g["name"].iloc[0]), fontsize=10)
        ax.set_xlabel("Vorlauf (Tage)")
        ax.set_ylabel(y)
        ax.grid(alpha=0.3)
        ax.set_ylim(bottom=0)
    for ax in list(axes.flat)[len(codes) :]:
        ax.axis("off")
    axes.flat[0].legend(fontsize=7)
    fig.suptitle(titel, fontsize=11)
    fig.tight_layout()
    fig.savefig(datei, dpi=130)
    plt.close(fig)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--cache", type=Path, required=True)
    a = p.parse_args()
    start = dt.datetime.now(dt.UTC)
    gfs = lade_gfs(a.cache)
    mess = lade_hubeau(a.cache)
    tab, gra = HIER / "tabellen", HIER / "grafiken"
    tab.mkdir(exist_ok=True)
    gra.mkdir(exist_ok=True)
    w, ws, wf = wassertemperatur(gfs, mess)
    q, qs, qf = abfluss(gfs, mess)
    w.to_csv(tab / "wassertemperatur.csv", index=False)
    ws.to_csv(tab / "wasser_schwellen.csv", index=False)
    q.to_csv(tab / "abfluss.csv", index=False)
    qs.to_csv(tab / "abfluss_schwellen.csv", index=False)
    pd.concat([wf, qf]).to_csv(tab / "wetterfehler.csv", index=False)
    grafik(
        w,
        "mae_k",
        "MAE (K)",
        "Flusstemperatur, Sommer im Prüfzeitraum",
        gra / "wassertemperatur.png",
    )
    grafik(q, "mae_log", "MAE log(Abfluss)", "Abfluss, Sommer im Prüfzeitraum", gra / "abfluss.png")
    lauf = {
        "job": "kkw-vorhersage",
        "job_sha256": hashlib.sha256((HIER / "job.yaml").read_bytes()).hexdigest(),
        "git_commit": subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, cwd=HIER, check=False
        ).stdout.strip(),
        "gestartet": start.isoformat(timespec="seconds"),
        "beendet": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        "zeitraum": {"von": str(gfs["tag"].min().date()), "bis": str(gfs["tag"].max().date())},
        "sommer_monate": SOMMER,
        "umfang": {
            "gfs_laeufe": int(gfs["tag"].nunique()),
            "tage_gesamt": int((gfs["tag"].max() - gfs["tag"].min()).days) + 1,
            "stationen_temperatur": sorted(w["standort"].unique().tolist()) if not w.empty else [],
            "pegel": sorted(q["standort"].unique().tolist()) if not q.empty else [],
        },
    }
    (tab / "lauf.json").write_text(json.dumps(lauf, indent=1, ensure_ascii=False) + "\n")
    if not w.empty:
        print(
            w[w["vorlauf"].isin([1, 3, 7])]
            .pivot_table(index=["name", "vorlauf"], columns="modell", values="mae_k")
            .round(2)
        )
    if not q.empty:
        print(
            q[q["vorlauf"].isin([1, 3, 7])]
            .pivot_table(index=["name", "vorlauf"], columns="modell", values="mae_log")
            .round(3)
        )


if __name__ == "__main__":
    main()
