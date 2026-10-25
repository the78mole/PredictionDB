"""Brennstoff-Merkmale für den Backtest: EIA STEO, Gasspeicher (Eurostat), Gas Europa (Weltbank).

Alle Werte werden so verwendet, wie sie am jeweiligen Stichtag vorlagen:

* EIA Short-Term Energy Outlook: jede monatliche Ausgabe aus dem Archiv
  (``https://www.eia.gov/outlooks/steo/archives/<mmmYY>_base.xlsx``), Tabelle 2, Prognose
  Brent (``BREPUUS``) und Henry Hub (``NGHHUUS``). Die Ausgabe eines Monats erscheint in der
  ersten Monatshälfte und gilt ab dem Folgemonat als bekannt. Die Prognosen sind Werk der EIA
  und gemeinfrei (U.S. Government Work).
* Eurostat ``nrg_stk_gasm`` (CC BY 4.0): Gasspeicher-Endbestand EU27 in TJ, mit drei Monaten
  Verzug als bekannt angenommen.
* Weltbank Pink Sheet (CC BY 4.0): "Natural gas, Europe" in USD/MMBtu, mit einem Monat Verzug.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import httpx
import numpy as np
import pandas as pd

STEO = "https://www.eia.gov/outlooks/steo/archives/{name}_base.xlsx"
EUROSTAT = (
    "https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data/nrg_stk_gasm"
    "?geo=EU27_2020&stk_flow=STKCL_NAT&unit=TJ_GCV&format=JSON&lang=en"
)
PINK_SHEET = (
    "https://thedocs.worldbank.org/en/doc/74e8be41ceb20fa0da750cda2f6b9e4e-0050012026/"
    "related/CMO-Historical-Data-Monthly.xlsx"
)
MONATE = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]
CODES = {"BREPUUS": "brent", "NGHHUUS": "henry_hub"}
SPEICHER_VERZUG = 3
GAS_VERZUG = 1


def _laden(url: str, datei: Path) -> bytes:
    if not datei.exists():
        r = httpx.get(url, timeout=300, follow_redirects=True)
        r.raise_for_status()
        datei.write_bytes(r.content)
    return datei.read_bytes()


def steo_ausgabe(roh: bytes) -> pd.DataFrame:
    """Tabelle 2 einer STEO-Ausgabe: Monatswerte je Code (Ist und Prognose)."""
    df = pd.read_excel(io.BytesIO(roh), sheet_name="2tab", header=None)
    jahre = pd.to_numeric(df.iloc[2, 2:], errors="coerce").ffill()
    monate = df.iloc[3, 2:].astype(str).str[:3].str.lower()
    perioden = [
        pd.Period(f"{int(j)}-{MONATE.index(m) + 1:02d}", "M") if m in MONATE else None
        for j, m in zip(jahre, monate, strict=True)
    ]
    out = {}
    for code, name in CODES.items():
        zeile = df[df.iloc[:, 0].astype(str).str.strip() == code]
        if zeile.empty:
            continue
        werte = pd.to_numeric(zeile.iloc[0, 2:], errors="coerce").to_numpy()
        s = pd.Series(werte, index=perioden)
        out[name] = s[s.index.notna()]
    return pd.DataFrame(out)


def steo(cache: Path, von: int, bis: pd.Period) -> dict[pd.Period, pd.DataFrame]:
    """Alle Ausgaben, Schlüssel = Monat der Veröffentlichung."""
    cache.mkdir(parents=True, exist_ok=True)
    out = {}
    for p in pd.period_range(f"{von}-01", bis, freq="M"):
        name = f"{MONATE[p.month - 1]}{p.year % 100:02d}"
        try:
            roh = _laden(STEO.format(name=name), cache / f"{name}_base.xlsx")
        except httpx.HTTPStatusError:
            continue
        out[p] = steo_ausgabe(roh)
    return out


def speicher(cache: Path) -> pd.Series:
    datei = cache / "eurostat_speicher.json"
    if not datei.exists():
        r = httpx.get(EUROSTAT, timeout=120)
        r.raise_for_status()
        datei.write_text(r.text)
    d = json.loads(datei.read_text())
    zeiten = d["dimension"]["time"]["category"]["index"]
    werte = {int(k): v for k, v in d["value"].items()}
    s = pd.Series({pd.Period(t, "M"): werte.get(i, np.nan) for t, i in zeiten.items()})
    return s.astype(float).sort_index()


def gas_europa(cache: Path) -> pd.Series:
    roh = _laden(PINK_SHEET, cache / "pink_sheet.xlsx")
    df = pd.read_excel(io.BytesIO(roh), sheet_name="Monthly Prices", header=None)
    kopf = df.index[df.iloc[:, 1:].astype(str).eq("Natural gas, Europe").any(axis=1)][0]
    spalte = df.columns[df.loc[kopf].astype(str).eq("Natural gas, Europe")][0]
    zeilen = df[df.iloc[:, 0].astype(str).str.match(r"^\d{4}M\d{2}$")]
    idx = [pd.Period(f"{t[:4]}-{t[5:]}", "M") for t in zeilen.iloc[:, 0]]
    return pd.Series(pd.to_numeric(zeilen[spalte], errors="coerce").to_numpy(), index=idx)


def merkmale(
    stand: pd.Period,
    ziel: pd.Period,
    ausgaben: dict[pd.Period, pd.DataFrame],
    lager: pd.Series,
    gas: pd.Series,
) -> dict[str, float]:
    """Brennstoff-Merkmale, die am Tag nach ``stand`` bekannt waren."""
    out = {k: np.nan for k in ("d_brent", "d_henry_hub", "speicher_abw", "gas_trend")}
    a = ausgaben.get(stand)
    if a is not None:
        for name in CODES.values():
            if name in a and stand in a.index and ziel in a.index:
                v0, v1 = a.at[stand, name], a.at[ziel, name]
                if v0 > 0 and v1 > 0:
                    out[f"d_{name}"] = float(np.log(v1 / v0))
    m = stand - SPEICHER_VERZUG + 1
    if m in lager.index:
        vorjahre = [lager.get(m - 12 * k, np.nan) for k in range(1, 6)]
        norm = np.nanmean(vorjahre)
        if norm > 0:
            out["speicher_abw"] = float(lager[m] / norm - 1)
    g = stand - GAS_VERZUG + 1
    if g in gas.index and (g - 3) in gas.index:
        out["gas_trend"] = float(np.log(gas[g] / gas[g - 3]))
    return out
