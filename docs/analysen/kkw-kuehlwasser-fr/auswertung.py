"""Auswertung kkw-kuehlwasser-fr: Kühlwasserbedingte Drosselung der französischen Kernkraftwerke.

Liest die Rohdaten aus abruf.py (GFS-Silber, Hub'eau, Energy-Charts) und abruf_entsoe.py
(ENTSO-E A73/A80, nur lokal) und schreibt tabellen/*.csv, grafiken/*.png und tabellen/lauf.json.

Aufruf: uv run --no-project --with pandas --with numpy --with scipy --with matplotlib \
    --with pyyaml python docs/analysen/kkw-kuehlwasser-fr/auswertung.py \
    --cache /tmp/kkw --entsoe /tmp/kkw/entsoe
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import yaml
from scipy import stats

HIER = Path(__file__).resolve().parent
sys.path.insert(0, str(HIER))
from daten import PEGEL_FEST, STANDORTE  # noqa: E402

TAB = HIER / "tabellen"
GRAF = HIER / "grafiken"
SOMMER = (5, 6, 7, 8, 9)
GRUPPE = {s.code: s.gruppe for s in STANDORTE}
NAME = {s.code: s.name for s in STANDORTE}

#: Schlüsselwörter für Umweltgründe in den A80-Meldungen (EDF schreibt Englisch oder Französisch)
UMWELT = re.compile(
    r"environ|contraintes? thermique|thermal (?:limit|constraint)|"
    r"temp[ée]rature (?:de|du|of) (?:la |l'|the )?"
    r"(?:rh[ôo]ne|garonne|loire|river|fleuve|eau|water)|d[ée]bit|low flow|canicule|heat ?wave|"
    # Ausnahmegenehmigung bei Hitze: EDF darf auf Anforderung von RTE über die Grenze fahren
    r"exceptional situation|RTE may request",
    re.I,
)


# --------------------------------------------------------------------------- Einlesen


def energy_charts(cache: Path) -> pd.DataFrame:
    teile = []
    for datei in sorted((cache / "ec").glob("pp_fr_*.json")):
        d = json.loads(datei.read_text())
        idx = pd.to_datetime(d["unix_seconds"], unit="s", utc=True)
        df = pd.DataFrame({p["name"]: p["data"] for p in d["production_types"]}, index=idx)
        teile.append(df[["Nuclear", "Load", "Wind onshore", "Solar", "Hydro Run-of-River"]])
    pp = pd.concat(teile).astype(float)
    pp.columns = ["kern_mw", "last_mw", "wind_mw", "pv_mw", "laufwasser_mw"]
    teile = []
    for datei in sorted((cache / "ec").glob("cbet_fr_*.json")):
        d = json.loads(datei.read_text())
        idx = pd.to_datetime(d["unix_seconds"], unit="s", utc=True)
        teile.append(pd.DataFrame({c["name"]: c["data"] for c in d["countries"]}, index=idx))
    cb = pd.concat(teile).astype(float) * 1000  # GW -> MW, + = Import nach FR
    cb = cb.rename(columns={"sum": "import_mw", "Germany": "import_de_mw"})[
        ["import_mw", "import_de_mw"]
    ]
    preise = {}
    for zone, muster in (("preis_fr", "price_fr_*.json"), ("preis_de", "price_de_lu_*.json")):
        teile_p = []
        for datei in sorted((cache / "ec").glob(muster)):
            d = json.loads(datei.read_text())
            idx = pd.to_datetime(d["unix_seconds"], unit="s", utc=True)
            teile_p.append(pd.Series(d["price"], index=idx, dtype=float))
        preise[zone] = pd.concat(teile_p)
    tag = pd.concat(
        [pp.resample("D").mean(), cb.resample("D").mean()]
        + [s.resample("D").mean().rename(k) for k, s in preise.items()],
        axis=1,
    )
    tag.index = tag.index.tz_localize(None).normalize()
    return tag


def gfs(cache: Path) -> pd.DataFrame:
    df = pd.concat(pd.read_csv(f) for f in sorted((cache / "gfs").glob("*.csv")))
    df["tag"] = pd.to_datetime(df["tag"])
    return df


def hubeau(cache: Path) -> tuple[pd.DataFrame, dict[str, dict[str, str | None]]]:
    ziel = cache / "hubeau"
    wahl: dict[str, dict[str, str | None]] = json.loads((ziel / "stationen.json").read_text())
    for code, (pegel, _) in PEGEL_FEST.items():
        wahl[code]["abfluss"] = pegel
    reihen: dict[str, pd.Series] = {}
    for code, w in wahl.items():
        if w["temperatur"]:
            t = pd.read_csv(ziel / f"temp_{w['temperatur']}.csv")
            t = t[(t["grad_c"] > -1) & (t["grad_c"] < 40)]
            t["tag"] = pd.to_datetime(t["datum"])
            g = t.groupby("tag")["grad_c"]
            reihen[f"{code}|wasser_t"] = g.mean()
            reihen[f"{code}|wasser_tmax"] = g.max()
        if w["abfluss"]:
            q = pd.read_csv(ziel / f"q_{w['abfluss']}.csv")
            q["tag"] = pd.to_datetime(q["datum"])
            q = q.drop_duplicates("tag").set_index("tag")["l_s"] / 1000.0
            reihen[f"{code}|abfluss"] = q[q > 0]
    return pd.DataFrame(reihen), wahl


def _lokal(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _kind(el: ET.Element | None, *pfad: str) -> ET.Element | None:
    cur = el
    for n in pfad:
        if cur is None:
            return None
        cur = next((c for c in cur if _lokal(c.tag) == n), None)
    return cur


def _text(el: ET.Element | None, *pfad: str) -> str:
    c = _kind(el, *pfad)
    return c.text.strip() if c is not None and c.text else ""


def standort_von(name: str) -> str | None:
    n = name.upper().replace("-", " ").replace("SAINT", "ST").replace(".", " ")
    n = re.sub(r"\s+", " ", n)
    for s in STANDORTE:
        if n.startswith(s.entsoe) or s.entsoe in n:
            return s.code
    return None


def a73(entsoe: Path) -> pd.DataFrame:
    teile = []
    for datei in sorted((entsoe / "a73").glob("*.csv")):
        df = pd.read_csv(datei)
        if len(df):
            teile.append(df)
    df = pd.concat(teile)
    df["stunde"] = pd.to_datetime(df["stunde_utc"]).dt.tz_localize(None)
    df["standort"] = df["name"].map(standort_von)
    return df


def _dauer(s: str) -> pd.Timedelta:
    return pd.Timedelta(s.replace("PT", "").lower().replace("m", "min"))


def a80(entsoe: Path) -> pd.DataFrame:
    """Nichtverfügbarkeiten als Teilintervalle je Block, letzte Revision je Meldung, ohne Storno.

    Die Elemente heißen flach mit Punkten (``production_RegisteredResource.pSRType.psrType``).
    Im Kurventyp A03 gilt eine verfügbare Leistung ab ihrer Position bis zur nächsten.
    """
    rr = "production_RegisteredResource"
    zeilen = []
    for datei in sorted((entsoe / "a80").glob("*.zip")):
        with zipfile.ZipFile(datei) as z:
            for n in z.namelist():
                w = ET.fromstring(z.read(n))
                grund_code = " ".join(_text(r, "code") for r in w if _lokal(r.tag) == "Reason")
                grund = " ".join(_text(r, "text") for r in w if _lokal(r.tag) == "Reason")
                for ts in w:
                    if _lokal(ts.tag) != "TimeSeries":
                        continue
                    if _text(ts, f"{rr}.pSRType.psrType") != "B14":
                        continue
                    block = _text(ts, f"{rr}.pSRType.powerSystemResources.name")
                    nenn = float(_text(ts, f"{rr}.pSRType.powerSystemResources.nominalP") or "nan")
                    grund_ts = " ".join(_text(r, "text") for r in ts if _lokal(r.tag) == "Reason")
                    for per in ts:
                        if _lokal(per.tag) != "Available_Period":
                            continue
                        t0 = pd.Timestamp(_text(per, "timeInterval", "start")).tz_localize(None)
                        t1 = pd.Timestamp(_text(per, "timeInterval", "end")).tz_localize(None)
                        res = _dauer(_text(per, "resolution"))
                        punkte = sorted(
                            (int(_text(p, "position")), float(_text(p, "quantity")))
                            for p in per
                            if _lokal(p.tag) == "Point"
                        )
                        for i, (pos, menge) in enumerate(punkte):
                            a = t0 + (pos - 1) * res
                            b = t0 + (punkte[i + 1][0] - 1) * res if i + 1 < len(punkte) else t1
                            zeilen.append(
                                {
                                    "mrid": _text(w, "mRID"),
                                    "revision": int(_text(w, "revisionNumber") or 0),
                                    "status": _text(w, "docStatus", "value"),
                                    "art": _text(ts, "businessType"),
                                    "block": block,
                                    "nenn_mw": nenn,
                                    "verfuegbar_mw": menge,
                                    "start": a,
                                    "ende": min(b, t1),
                                    "grund_code": grund_code,
                                    "grund": (grund + " " + grund_ts)[:300],
                                }
                            )
    df = pd.DataFrame(zeilen)
    letzte = df.groupby("mrid")["revision"].transform("max")
    df = df[df["revision"] == letzte]
    df = df[~df["status"].isin(["A09", "A13"])]
    df["standort"] = df["block"].map(standort_von)
    df["umwelt"] = df["grund"].str.contains(UMWELT.pattern, flags=re.I)
    df["fehl_mw"] = (df["nenn_mw"] - df["verfuegbar_mw"]).clip(lower=0)
    return df


def taeglich(ev: pd.DataFrame) -> pd.DataFrame:
    """MW-Tagesmittel der Nichtverfügbarkeit je Standort, getrennt Umwelt / sonst.

    Meldungen überlappen sich (Aktualisierungen unter neuer mRID, Teilmeldungen). Je Block und
    Viertelstunde zählt daher die größte gemeldete Fehlleistung, nicht die Summe; Umwelt ist die
    größte Fehlleistung unter den Umweltmeldungen, sonst der Rest bis zur größten insgesamt.
    """
    raster = pd.date_range("2021-03-01", "2026-11-01", freq="15min", inclusive="left")
    t0 = raster[0]
    n = len(raster)
    erg: dict[str, np.ndarray] = {}
    for (code, _block), g in ev.dropna(subset=["standort"]).groupby(["standort", "block"]):
        alle = np.zeros(n)
        umwelt = np.zeros(n)
        for r in g.itertuples():
            i = max(0, int((r.start - t0) / pd.Timedelta("15min")))
            j = min(n, int(np.ceil((r.ende - t0) / pd.Timedelta("15min"))))
            if j <= i:
                continue
            w = min(r.fehl_mw, r.nenn_mw) if np.isfinite(r.nenn_mw) else r.fehl_mw
            np.maximum(alle[i:j], w, out=alle[i:j])
            if r.umwelt:
                np.maximum(umwelt[i:j], w, out=umwelt[i:j])
        for name, werte in ((f"{code}|umwelt_mw", umwelt), (f"{code}|sonst_mw", alle - umwelt)):
            erg[name] = erg.get(name, np.zeros(n)) + werte
    df = pd.DataFrame(erg, index=raster)
    return df.resample("D").mean()


# --------------------------------------------------------------------------- Merkmale


def merkmale(wx: pd.DataFrame) -> pd.DataFrame:
    teile = {}
    for (code, ort), g in wx.groupby(["standort", "ort"]):
        g = g.set_index("tag").sort_index().asfreq("D")
        if ort == "punkt":
            for n in (1, 3, 7, 14, 30):
                teile[f"{code}|luft_t{n}"] = (
                    g["t_mittel"].rolling(n, min_periods=max(1, n // 2)).mean()
                )
            teile[f"{code}|luft_tmax3"] = g["t_max"].rolling(3, min_periods=2).max()
            teile[f"{code}|strahlung7"] = g["dswrf"].rolling(7, min_periods=4).mean()
            teile[f"{code}|feuchte7"] = g["rh"].rolling(7, min_periods=4).mean()
            teile[f"{code}|wind7"] = g["wind10"].rolling(7, min_periods=4).mean()
        else:
            for n in (7, 30, 90):
                teile[f"{code}|regen{n}"] = (
                    g["niederschlag_mm"].rolling(n, min_periods=n // 2).sum()
                )
            teile[f"{code}|schnee"] = g["weasd"]
            teile[f"{code}|boden"] = g["soilw"]
            teile[f"{code}|gebiet_t14"] = g["t_mittel"].rolling(14, min_periods=7).mean()
    return pd.DataFrame(teile)


# --------------------------------------------------------------------------- Auswertung


def korrelationen(df: pd.DataFrame, codes: list[str], ziele: list[str]) -> pd.DataFrame:
    sommer = df[df.index.month.isin(SOMMER)]
    keine = {"umwelt_mw", "sonst_mw", "umwelt_anteil", "ausnutzung", "abfluss"}
    faktoren = sorted({c.split("|", 1)[1] for c in df.columns if "|" in c} - set(ziele) - keine)
    zeilen = []
    for code in codes:
        for ziel in ziele:
            y = sommer.get(f"{code}|{ziel}")
            if y is None or y.notna().sum() < 100 or y.std() == 0:
                continue
            for f in faktoren:
                x = sommer.get(f"{code}|{f}")
                if x is None:
                    continue
                m = x.notna() & y.notna()
                if m.sum() < 100:
                    continue
                if x[m].nunique() < 3:
                    continue
                rho, p = stats.spearmanr(x[m], y[m])
                zeilen.append(
                    {
                        "gruppe": GRUPPE[code],
                        "standort": code,
                        "name": NAME[code],
                        "ziel": ziel,
                        "faktor": f,
                        "rho": round(float(rho), 3),
                        "p": float(p),
                        "n": int(m.sum()),
                    }
                )
    return pd.DataFrame(zeilen)


def schwellen(df: pd.DataFrame, codes: list[str], ziel: str) -> pd.DataFrame:
    zeilen = []
    for merkmal, klassen in (
        ("luft_t7", [-50, 16, 18, 20, 22, 24, 26, 50]),
        ("wasser_t", [-5, 18, 20, 22, 24, 26, 28, 50]),
        ("abfluss_rel", [0, 0.3, 0.4, 0.5, 0.7, 1.0, 100]),
    ):
        for code in codes:
            x, y = df.get(f"{code}|{merkmal}"), df.get(f"{code}|{ziel}")
            if x is None or y is None:
                continue
            m = x.notna() & y.notna() & df.index.month.isin(SOMMER)
            k = pd.cut(x[m], klassen)
            for klasse, idx in y[m].groupby(k, observed=True):
                zeilen.append(
                    {
                        "gruppe": GRUPPE[code],
                        "standort": code,
                        "merkmal": merkmal,
                        "klasse": str(klasse),
                        "tage": len(idx),
                        "anteil_tage_umwelt": round(float((idx > 50).mean()), 3),
                        "mittel_umwelt_mw": round(float(idx.mean()), 1),
                    }
                )
    return pd.DataFrame(zeilen)


def wassertemperatur_modell(df: pd.DataFrame, codes: list[str]) -> pd.DataFrame:
    zeilen = []
    for code in codes:
        y = df.get(f"{code}|wasser_t")
        if y is None:
            continue
        for variante, spalten in (
            ("luft_t7", ["luft_t7"]),
            ("luft_t3_t14_t30", ["luft_t3", "luft_t14", "luft_t30"]),
            ("luft + abfluss", ["luft_t3", "luft_t14", "luft_t30", "log_abfluss"]),
            (
                "luft + abfluss + strahlung",
                ["luft_t3", "luft_t14", "luft_t30", "log_abfluss", "strahlung7"],
            ),
        ):
            x = pd.DataFrame({s: df.get(f"{code}|{s}") for s in spalten})
            if x.isna().all().any():
                continue
            m = x.notna().all(axis=1) & y.notna()
            # zeitlich geteilt: die ersten 60 % der gültigen Tage lernen, den Rest prüfen
            grenze = df.index[m][int(m.sum() * 0.6)] if m.sum() else df.index[0]
            train = m & (df.index < grenze)
            test = m & (df.index >= grenze)
            if train.sum() < 200 or test.sum() < 100:
                continue
            a = np.column_stack([np.ones(train.sum()), x[train].to_numpy()])
            beta, *_ = np.linalg.lstsq(a, y[train].to_numpy(), rcond=None)
            vor = np.column_stack([np.ones(test.sum()), x[test].to_numpy()]) @ beta
            ist = y[test].to_numpy()
            r2 = 1 - ((ist - vor) ** 2).sum() / ((ist - ist.mean()) ** 2).sum()
            som = df.index[test].month.isin(SOMMER)
            mae_s = float(np.abs(ist - vor)[som].mean()) if som.any() else np.nan
            zeilen.append(
                {
                    "standort": code,
                    "name": NAME[code],
                    "variante": variante,
                    "n_train": int(train.sum()),
                    "n_test": int(test.sum()),
                    "r2_test": round(float(r2), 3),
                    "mae_test_k": round(float(np.abs(ist - vor).mean()), 2),
                    "mae_test_sommer_k": round(mae_s, 2),
                }
            )
    return pd.DataFrame(zeilen)


def ols(y: pd.Series, x: pd.DataFrame) -> tuple[pd.Series, pd.Series, float, int]:
    m = y.notna() & x.notna().all(axis=1)
    a = np.column_stack([np.ones(m.sum()), x[m].to_numpy(float)])
    beta, *_ = np.linalg.lstsq(a, y[m].to_numpy(float), rcond=None)
    rest = y[m].to_numpy(float) - a @ beta
    n, k = a.shape
    s2 = (rest**2).sum() / (n - k)
    cov = s2 * np.linalg.pinv(a.T @ a)
    se = np.sqrt(np.diag(cov))
    r2 = 1 - (rest**2).sum() / ((y[m] - y[m].mean()) ** 2).sum()
    namen = ["const", *x.columns]
    return pd.Series(beta, namen), pd.Series(se, namen), float(r2), int(n)


def austausch(df: pd.DataFrame) -> pd.DataFrame:
    sommer = df[df.index.month.isin(SOMMER)].copy()
    kal = pd.get_dummies(sommer.index.month, prefix="m", drop_first=True, dtype=float)
    kal = kal.join(
        pd.get_dummies(sommer.index.day_of_week, prefix="wt", drop_first=True, dtype=float)
    )
    kal = kal.join(pd.get_dummies(sommer.index.year, prefix="j", drop_first=True, dtype=float))
    kal.index = sommer.index
    zeilen = []
    for ziel, einheit in (
        ("import_mw", "MW je MW"),
        ("import_de_mw", "MW je MW"),
        ("spread", "EUR/MWh je GW"),
        ("kern_mw", "MW je MW"),
    ):
        y = sommer[ziel]
        for variante, x in (
            ("nur umwelt", pd.DataFrame({"umwelt_gw": sommer["umwelt_mw"] / 1000})),
            (
                "mit kalender, last, ee, sonst. ausfall",
                pd.concat(
                    [
                        pd.DataFrame(
                            {
                                "umwelt_gw": sommer["umwelt_mw"] / 1000,
                                "sonst_gw": sommer["sonst_mw"] / 1000,
                                "last_gw": sommer["last_mw"] / 1000,
                                "wind_gw": sommer["wind_mw"] / 1000,
                                "pv_gw": sommer["pv_mw"] / 1000,
                            }
                        ),
                        kal,
                    ],
                    axis=1,
                ),
            ),
        ):
            beta, se, r2, n = ols(y, x)
            teiler = 1.0 if ziel == "spread" else 1000.0  # MW je GW -> MW je MW
            zeilen.append(
                {
                    "ziel": ziel,
                    "variante": variante,
                    "einheit": einheit,
                    "koeff_umwelt": round(float(beta["umwelt_gw"]) / teiler, 3),
                    "se": round(float(se["umwelt_gw"]) / teiler, 3),
                    "r2": round(r2, 3),
                    "n_tage": n,
                }
            )
    return pd.DataFrame(zeilen)


# --------------------------------------------------------------------------- Grafiken


def grafik_ueberblick(df: pd.DataFrame, jahr: int, datei: Path) -> None:
    t = df[(df.index >= f"{jahr}-05-01") & (df.index < f"{jahr}-10-01")]
    fig, ax = plt.subplots(3, 1, figsize=(10, 8), sharex=True)
    ax[0].fill_between(
        t.index,
        t["umwelt_mw"] / 1000,
        color="#c0392b",
        alpha=0.8,
        label="nicht verfügbar, Umweltgrund",
    )
    ax[0].plot(t.index, t["sonst_mw"] / 1000, color="#7f8c8d", lw=1, label="nicht verfügbar, sonst")
    ax[0].set_ylabel("GW")
    ax[0].legend(loc="upper left", fontsize=8)
    ax[0].set_title(f"Französische Kernkraft im Sommer {jahr}")
    for code, farbe in (("BUG", "#2c3e50"), ("GOL", "#e67e22"), ("SLB", "#16a085")):
        if f"{code}|wasser_t" in t:
            ax[1].plot(t.index, t[f"{code}|wasser_t"], color=farbe, label=f"Wasser {NAME[code]}")
        ax[1].plot(
            t.index, t[f"{code}|luft_t7"], color=farbe, ls=":", lw=1, label=f"Luft 7 d {NAME[code]}"
        )
    ax[1].set_ylabel("°C")
    ax[1].legend(loc="upper left", fontsize=7, ncol=2)
    ax[2].plot(t.index, t["import_mw"] / 1000, color="#2980b9", label="Netto-Import FR")
    ax[2].plot(t.index, t["import_de_mw"] / 1000, color="#8e44ad", lw=1, label="davon aus DE")
    ax[2].axhline(0, color="k", lw=0.5)
    ax[2].set_ylabel("GW (+ = Import)")
    ax[2].legend(loc="upper left", fontsize=8)
    fig.tight_layout()
    fig.savefig(datei, dpi=130)
    plt.close(fig)


def grafik_korrelation(k: pd.DataFrame, datei: Path) -> None:
    t = k[k["ziel"] == "umwelt_anteil"].pivot_table(
        index="faktor", columns="standort", values="rho"
    )
    reihen = [s.code for s in STANDORTE if s.code in t.columns]
    t = t[reihen]
    t = t.loc[t.abs().max(axis=1).sort_values(ascending=False).index]
    fig, ax = plt.subplots(figsize=(10, 0.35 * len(t) + 1.5))
    im = ax.imshow(t.to_numpy(), cmap="RdBu_r", vmin=-0.7, vmax=0.7, aspect="auto")
    ax.set_xticks(range(len(t.columns)), [NAME[c] for c in t.columns], rotation=45, ha="right")
    ax.set_yticks(range(len(t)), t.index)
    for i in range(t.shape[0]):
        for j in range(t.shape[1]):
            v = t.iat[i, j]
            if np.isfinite(v):
                ax.text(j, i, f"{v:.2f}", ha="center", va="center", fontsize=7)
    fig.colorbar(im, ax=ax, label="Spearman rho (Mai bis Sep)")
    ax.set_title("Umweltbedingte Nichtverfügbarkeit (Anteil der Leistung) gegen Einflussfaktoren")
    fig.tight_layout()
    fig.savefig(datei, dpi=130)
    plt.close(fig)


def grafik_schwellen(s: pd.DataFrame, datei: Path) -> None:
    fig, ax = plt.subplots(1, 3, figsize=(14, 4.2), sharey=True)
    for a, merkmal, titel, einheit in (
        (ax[0], "luft_t7", "Lufttemperatur am Standort, 7-Tage-Mittel (GFS)", "°C"),
        (ax[1], "abfluss_rel", "Abfluss relativ zum Median (Hub'eau)", "Anteil"),
        (ax[2], "wasser_t", "Wassertemperatur, Tagesmittel (Hub'eau)", "°C"),
    ):
        t = s[s["merkmal"] == merkmal]
        klassen = list(dict.fromkeys(t["klasse"]))
        klassen.sort(key=lambda k: float(k.strip("(]").split(",")[0]))
        pos = {k: i for i, k in enumerate(klassen)}
        for code, g in t.groupby("standort", sort=False):
            g = g[g["tage"] >= 10]
            if g.empty or g["anteil_tage_umwelt"].max() < 0.05:
                continue
            a.plot(g["klasse"].map(pos), g["anteil_tage_umwelt"], marker="o", label=NAME[code])
        a.set_xticks(range(len(klassen)), klassen, rotation=30, fontsize=7)
        a.set_title(titel, fontsize=9)
        a.set_xlabel(einheit)
        a.legend(fontsize=7)
    ax[0].set_ylabel("Anteil der Tage mit Umwelt-Drosselung > 50 MW (Mai bis Sep)")
    fig.tight_layout()
    fig.savefig(datei, dpi=130)
    plt.close(fig)


def grafik_austausch(au: pd.DataFrame, datei: Path) -> None:
    t = au[au["variante"].str.startswith("mit")].set_index("ziel")
    fig, ax = plt.subplots(1, 2, figsize=(10, 3.8), gridspec_kw={"width_ratios": [3, 1]})
    namen = {
        "kern_mw": "Kernkraft FR",
        "import_mw": "Netto-Import FR",
        "import_de_mw": "davon aus DE",
    }
    werte = [t.loc[z, "koeff_umwelt"] for z in namen]
    fehler = [2 * t.loc[z, "se"] for z in namen]
    ax[0].barh(list(namen.values()), werte, xerr=fehler, color=["#7f8c8d", "#2980b9", "#8e44ad"])
    ax[0].axvline(0, color="k", lw=0.5)
    ax[0].set_xlabel("MW je MW umweltbedingter Nichtverfügbarkeit (± 2 SE)")
    ax[1].bar(
        ["Preis FR - DE-LU"],
        [t.loc["spread", "koeff_umwelt"]],
        yerr=[2 * t.loc["spread", "se"]],
        color="#c0392b",
    )
    ax[1].set_ylabel("EUR/MWh je GW")
    fig.suptitle(
        "Wirkung je Tag, Mai bis Sep 2021 bis 2026, "
        "bereinigt um Kalender, Last, Wind, PV, übrige Ausfälle",
        fontsize=10,
    )
    fig.tight_layout()
    fig.savefig(datei, dpi=130)
    plt.close(fig)


# --------------------------------------------------------------------------- Hauptteil


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--cache", type=Path, required=True)
    p.add_argument("--entsoe", type=Path, required=True)
    args = p.parse_args()
    gestartet = dt.datetime.now(dt.UTC)
    TAB.mkdir(exist_ok=True)
    GRAF.mkdir(exist_ok=True)

    ec = energy_charts(args.cache)
    wx = gfs(args.cache)
    hub, wahl = hubeau(args.cache)
    mm = merkmale(wx)
    ev = a80(args.entsoe)
    ausfall = taeglich(ev)
    erz = a73(args.entsoe)

    # Leistung je Standort: 99,5-%-Quantil der Stundenwerte je Block und Jahr, summiert
    erz["jahr"] = erz["stunde"].dt.year
    kap_block = erz.groupby(["standort", "mrid", "jahr"])["mw"].quantile(0.995)
    kap = kap_block.groupby(["standort", "jahr"]).sum()
    erz_tag = erz.groupby(["standort", erz["stunde"].dt.normalize()])["mw"].sum() / 24
    erz_tag = erz_tag.unstack(0)

    df = pd.concat([ec, mm, hub, ausfall], axis=1)
    df = df[(df.index >= "2021-03-01") & (df.index < "2026-11-01")].copy()
    for s in STANDORTE:
        k = df.index.year.map(lambda j, c=s.code: kap.get((c, j), np.nan)).to_numpy(float)
        u = df.get(f"{s.code}|umwelt_mw", pd.Series(0.0, index=df.index)).fillna(0)
        df[f"{s.code}|umwelt_mw"] = u
        df[f"{s.code}|umwelt_anteil"] = u / k
        if s.code in erz_tag:
            df[f"{s.code}|ausnutzung"] = erz_tag[s.code].reindex(df.index) / k
        if f"{s.code}|abfluss" in df:
            df[f"{s.code}|log_abfluss"] = np.log(df[f"{s.code}|abfluss"])
            # Abfluss relativ zum Median aller Tage des Pegels (Niedrigwasser < 0,5)
            df[f"{s.code}|abfluss_rel"] = df[f"{s.code}|abfluss"] / df[f"{s.code}|abfluss"].median()
    df = df.copy()
    df["umwelt_mw"] = df[[c for c in df.columns if c.endswith("|umwelt_mw")]].sum(axis=1)
    df["sonst_mw"] = df[[c for c in df.columns if c.endswith("|sonst_mw")]].sum(axis=1)
    df["spread"] = df["preis_fr"] - df["preis_de"]

    codes = [s.code for s in STANDORTE]
    binnen = [s.code for s in STANDORTE if s.gruppe != "kueste"]

    # Tabellen
    standorte = pd.DataFrame(
        [
            {
                "standort": s.code,
                "name": s.name,
                "gruppe": s.gruppe,
                "gewaesser": s.gewaesser,
                "lat": s.lat,
                "lon": s.lon,
                "leistung_mw_2025": round(float(kap.get((s.code, 2025), np.nan)), 0),
                "umwelt_gwh_sommer_mittel": round(
                    float(
                        df[f"{s.code}|umwelt_mw"][df.index.month.isin(SOMMER)].sum()
                        * 24
                        / 1000
                        / df.index.year.nunique()
                    ),
                    1,
                ),
                "quelle": s.quelle,
            }
            for s in STANDORTE
        ]
    )
    standorte.to_csv(TAB / "standorte.csv", index=False)
    pd.DataFrame(
        [
            {"standort": c, "temperatur_station": w["temperatur"], "abfluss_pegel": w["abfluss"]}
            for c, w in wahl.items()
        ]
    ).to_csv(TAB / "stationen.csv", index=False)

    k = korrelationen(
        df, binnen + [c for c in codes if c not in binnen], ["umwelt_anteil", "ausnutzung"]
    )
    k.to_csv(TAB / "korrelation.csv", index=False)
    sw = schwellen(df, binnen, "umwelt_mw")
    sw.to_csv(TAB / "schwellen.csv", index=False)
    wm = wassertemperatur_modell(df, binnen)
    wm.to_csv(TAB / "wassertemperatur_modell.csv", index=False)

    um = ev[ev["umwelt"]].copy()
    um["jahr"] = um["start"].dt.year
    um_tab = (
        um.groupby(["jahr", "standort"])
        .agg(meldungen=("mrid", "nunique"), max_fehl_mw=("fehl_mw", "max"))
        .reset_index()
    )
    gwh = df.groupby(df.index.year)[[f"{c}|umwelt_mw" for c in codes]].sum() * 24 / 1000
    gwh.columns = [c.split("|")[0] for c in gwh.columns]
    gwh = gwh.stack().rename("gwh").reset_index()
    gwh.columns = ["jahr", "standort", "gwh"]
    um_tab = um_tab.merge(gwh, on=["jahr", "standort"], how="outer").fillna(0)
    um_tab = um_tab[(um_tab["meldungen"] > 0) | (um_tab["gwh"] > 0)]
    um_tab["gwh"] = um_tab["gwh"].round(1)
    um_tab.to_csv(TAB / "umweltereignisse.csv", index=False)

    au = austausch(df)
    au.to_csv(TAB / "austausch.csv", index=False)

    # Monatswerte je Gruppe (nur Summen, keine Block- oder Tageswerte aus ENTSO-E)
    monat = pd.DataFrame(index=df.index)
    for gruppe in ("fluss_offen", "fluss_turm", "kueste"):
        spalten = [f"{s.code}|umwelt_mw" for s in STANDORTE if s.gruppe == gruppe]
        monat[f"umwelt_gwh_{gruppe}"] = df[spalten].sum(axis=1) * 24 / 1000
    monat["sonst_gwh"] = df["sonst_mw"] * 24 / 1000
    monat["kern_gwh"] = df["kern_mw"] * 24 / 1000
    monat["import_gwh"] = df["import_mw"] * 24 / 1000
    monat["import_de_gwh"] = df["import_de_mw"] * 24 / 1000
    monate = monat.resample("MS").sum().round(1)
    monate["preis_fr"] = df["preis_fr"].resample("MS").mean().round(2)
    monate["preis_de"] = df["preis_de"].resample("MS").mean().round(2)
    monate.index = monate.index.strftime("%Y-%m")
    monate.to_csv(TAB / "monate.csv", index_label="monat")

    # Grafiken
    grafik_ueberblick(df, 2022, GRAF / "ueberblick_2022.png")
    grafik_ueberblick(df, 2026, GRAF / "ueberblick_2026.png")
    grafik_korrelation(k, GRAF / "korrelation.png")
    grafik_schwellen(sw, GRAF / "schwellen.png")
    grafik_austausch(au, GRAF / "austausch.png")

    # Grundtexte der Umweltmeldungen für das README (nur Häufigkeit)
    texte = um["grund"].str.slice(0, 80).value_counts().head(15)
    print(texte.to_string())

    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"], capture_output=True, text=True, cwd=HIER
    ).stdout.strip()
    job = yaml.safe_load((HIER / "job.yaml").read_text())
    lauf = {
        "job": job["id"],
        "job_sha256": sha(HIER / "job.yaml"),
        "git_commit": commit,
        "gestartet": gestartet.isoformat(timespec="seconds"),
        "beendet": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        "zeitraum": {"von": str(df.index.min().date()), "bis": str(df.index.max().date())},
        "sommer_monate": list(SOMMER),
        "umfang": {
            "tage": len(df),
            "gfs_laeufe": int(wx["tag"].nunique()),
            "standorte": len(STANDORTE),
            "a73_stunden_zeilen": len(erz),
            "a73_bloecke": int(erz["mrid"].nunique()),
            "a73_bloecke_ohne_standort": sorted(
                erz.loc[erz["standort"].isna(), "name"].unique().tolist()
            ),
            "a80_meldungen_kernkraft": int(ev["mrid"].nunique()),
            "a80_meldungen_umwelt": int(um["mrid"].nunique()),
            "hubeau_temperatur_stationen": sorted(
                {w["temperatur"] for w in wahl.values() if w["temperatur"]}
            ),
            "hubeau_abfluss_pegel": sorted({w["abfluss"] for w in wahl.values() if w["abfluss"]}),
        },
        "fehlend": {
            "gfs_tage_ohne_lauf": int(
                len(pd.date_range("2021-03-01", "2026-10-31")) - wx["tag"].nunique()
            ),
            "a73_tage_ohne_daten": int(
                len(pd.date_range("2021-03-01", "2026-10-31"))
                - erz["stunde"].dt.normalize().nunique()
            ),
        },
        "interpoliert": "keine; gleitende Mittel mit min_periods (halbes Fenster)",
        "annahmen": [
            "Umweltgrund per Schlüsselwortsuche im Reason-Text der A80-Meldung",
            "Standortleistung = Summe der 99,5-%-Quantile der Blöcke je Jahr (A73)",
            "GFS-Tageswerte aus f003 bis f024 des 00Z-Laufs (Vorhersage, keine Analyse)",
        ],
    }
    (TAB / "lauf.json").write_text(json.dumps(lauf, indent=1, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main()
