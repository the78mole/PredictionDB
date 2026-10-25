"""Stundendaten für den Backtest des Preisniveaus aus der Energy-Charts-API laden.

Day-Ahead-Preis (``/price``, DE-AT-LU bis 30.09.2018, danach DE-LU) sowie Last, Wind an Land,
Wind auf See und Solar (``/public_power``, Deutschland). Alle Reihen CC BY 4.0 laut
Energy-Charts (Fraunhofer ISE; Preise Bundesnetzagentur | SMARD.de). Viertelstunden werden zu
Stundenmitteln zusammengefasst. Ergebnis: eine CSV mit einer Zeile je Stunde.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import httpx
import pandas as pd

API = "https://api.energy-charts.info"
ZONENWECHSEL = pd.Timestamp("2018-10-01", tz="Europe/Berlin")
ERSTES_JAHR = 2015
ERZEUGUNG = {
    "Load": "last",
    "Wind onshore": "wind_onshore",
    "Wind offshore": "wind_offshore",
    "Solar": "solar",
}


def _get(pfad: str, params: dict[str, str]) -> dict[str, Any] | None:
    for versuch in range(8):
        r = httpx.get(f"{API}/{pfad}", params=params, timeout=300)
        if r.status_code == 429:
            time.sleep(15 * (versuch + 1))
            continue
        if r.status_code == 404 or not r.text.startswith("{"):
            return None
        r.raise_for_status()
        out: dict[str, Any] = r.json()
        return out
    raise RuntimeError(f"Energy-Charts antwortet dauerhaft mit 429 ({pfad} {params})")


def _index(sekunden: list[int]) -> pd.DatetimeIndex:
    return pd.to_datetime(sekunden, unit="s", utc=True).tz_convert("Europe/Berlin")


def preis(jahr: int) -> pd.Series:
    teile = []
    for zone in ("DE-AT-LU", "DE-LU"):
        d = _get("price", {"bzn": zone, "start": f"{jahr}-01-01", "end": f"{jahr}-12-31"})
        if not d:
            continue
        s = pd.Series(d["price"], index=_index(d["unix_seconds"]), dtype=float)
        teile.append(
            s[s.index < ZONENWECHSEL] if zone == "DE-AT-LU" else s[s.index >= ZONENWECHSEL]
        )
    s = pd.concat(teile).sort_index()
    return s[~s.index.duplicated()].rename("preis")


def erzeugung(jahr: int) -> pd.DataFrame:
    spalten: dict[str, list[pd.Series]] = {}
    for q in range(4):
        start = pd.Timestamp(year=jahr, month=3 * q + 1, day=1)
        ende = start + pd.DateOffset(months=3) - pd.Timedelta(days=1)
        d = _get(
            "public_power",
            {
                "country": "de",
                "start": start.strftime("%Y-%m-%d"),
                "end": ende.strftime("%Y-%m-%d"),
            },
        )
        if not d:
            continue
        idx = _index(d["unix_seconds"])
        for p in d["production_types"]:
            if p["name"] in ERZEUGUNG:
                spalten.setdefault(ERZEUGUNG[p["name"]], []).append(
                    pd.Series(p["data"], index=idx, dtype=float)
                )
    return pd.DataFrame({k: pd.concat(v) for k, v in spalten.items()})


def lade(ziel: Path, bis: int) -> pd.DataFrame:
    """Lädt alle Jahre (je Jahr eine Cache-Datei) und gibt Stundenmittel zurück."""
    ziel.mkdir(parents=True, exist_ok=True)
    teile = []
    for jahr in range(ERSTES_JAHR, bis + 1):
        datei = ziel / f"stunden_{jahr}.csv.gz"
        aktuell = jahr == bis
        if not datei.exists() or aktuell:
            df = erzeugung(jahr).join(preis(jahr), how="outer").sort_index()
            df = df[~df.index.duplicated()].resample("1h").mean()
            df.to_csv(datei)
        df = pd.read_csv(datei, index_col=0)
        df.index = pd.to_datetime(df.index, utc=True).tz_convert("Europe/Berlin")
        teile.append(df)
    return pd.concat(teile).sort_index()
