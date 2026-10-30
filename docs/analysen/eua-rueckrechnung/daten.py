"""Stundendaten von SMARD und Brennstoff-Proxys für die EUA-Rückrechnung laden.

SMARD (Bundesnetzagentur | SMARD.de, CC BY 4.0), Stundenauflösung:

* Day-Ahead-Preis: Filter 251 (DE-AT-LU) bis 30.09.2018, danach 4169 (DE-LU)
* Netzlast (410), Residuallast (4359) und Erzeugung je Energieträger

Weltbank Pink Sheet (CC BY 4.0), Monatswerte: Erdgas Europa (ab 04/2015 TTF), Kohle
Australien und Südafrika. Die Weltbank nennt als Quelle u. a. Bloomberg; veröffentlicht sind
die Monatsmittel unter CC BY 4.0.

Eurostat ``ert_bil_eur_m`` (CC BY 4.0): Monatsmittel USD je EUR, um die USD-Preise umzurechnen.

ACER LNG-Preisbewertung (nur Zusatzvariante, Lizenz ungeklärt): werktäglich ab 13.01.2023,
EU-Preis DES Spot. Die Spalte "LNG Benchmark" enthält ICE-TTF und wird nicht gelesen.
"""

from __future__ import annotations

import io
import json
import time
from pathlib import Path

import httpx
import pandas as pd

SMARD = "https://www.smard.de/app/chart_data"
ZONENWECHSEL = pd.Timestamp("2018-10-01", tz="Europe/Berlin")
REIHEN = {
    "last": 410,
    "residuallast": 4359,
    "braunkohle": 1223,
    "kernenergie": 1224,
    "wind_offshore": 1225,
    "wind_onshore": 4067,
    "solar": 4068,
    "steinkohle": 4069,
    "erdgas": 4071,
}
PINK_SHEET = (
    "https://thedocs.worldbank.org/en/doc/74e8be41ceb20fa0da750cda2f6b9e4e-0050012026/"
    "related/CMO-Historical-Data-Monthly.xlsx"
)
EUROSTAT_USD = (
    "https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data/ert_bil_eur_m"
    "?currency=USD&statinfo=AVG&unit=NAC&format=JSON&lang=en&sinceTimePeriod=2014-01"
)
ACER = "https://aegis.acer.europa.eu/terminal/price_assessments/historical_data"


_CLIENT = httpx.Client(
    timeout=httpx.Timeout(30.0, read=120.0),
    follow_redirects=True,
    headers={"User-Agent": "Mozilla/5.0 (PredictionDB-Analyse)"},
    limits=httpx.Limits(max_connections=8),
)


def _get(url: str) -> httpx.Response:
    for versuch in range(6):
        try:
            r = _CLIENT.get(url)
        except httpx.TransportError:
            time.sleep(5 * (versuch + 1))
            continue
        if r.status_code in (429, 502, 503):
            time.sleep(10 * (versuch + 1))
            continue
        r.raise_for_status()
        return r
    raise RuntimeError(f"{url}: kein Abruf nach 6 Versuchen")


def smard_reihe(filter_id: int, region: str, cache: Path) -> pd.Series:
    """Alle Stundenwerte einer SMARD-Reihe (Wochenblöcke, abgeschlossene Blöcke gecacht)."""
    ordner = cache / "smard" / f"{filter_id}_{region}"
    ordner.mkdir(parents=True, exist_ok=True)
    stamps = _get(f"{SMARD}/{filter_id}/{region}/index_hour.json").json()["timestamps"]
    teile = []
    for i, st in enumerate(stamps):
        datei = ordner / f"{st}.json"
        letzter = i >= len(stamps) - 2  # die jüngsten Blöcke werden noch ergänzt
        if letzter or not datei.exists():
            url = f"{SMARD}/{filter_id}/{region}/{filter_id}_{region}_hour_{st}.json"
            datei.write_text(_get(url).text)
        werte = json.loads(datei.read_text())["series"]
        teile.append(pd.DataFrame(werte, columns=["ms", "wert"]))
    df = pd.concat(teile).dropna().drop_duplicates("ms")
    idx = pd.to_datetime(df.ms, unit="ms", utc=True)
    return pd.Series(df.wert.to_numpy(float), index=idx).sort_index()


def stunden(cache: Path) -> pd.DataFrame:
    datei = cache / "stunden.parquet"
    if datei.exists() and time.time() - datei.stat().st_mtime < 6 * 3600:
        return pd.read_parquet(datei)
    alt = smard_reihe(251, "DE-AT-LU", cache)
    neu = smard_reihe(4169, "DE-LU", cache)
    preis = pd.concat([alt[alt.index < ZONENWECHSEL], neu[neu.index >= ZONENWECHSEL]])
    spalten = {"preis": preis}
    for name, fid in REIHEN.items():
        spalten[name] = smard_reihe(fid, "DE", cache)  # MWh je Stunde = mittlere MW
    df = pd.DataFrame(spalten).sort_index()
    df = df[df.index >= pd.Timestamp("2015-01-05", tz="UTC")]
    df.to_parquet(datei)
    return df


def pink_sheet(cache: Path) -> pd.DataFrame:
    datei = cache / "cmo.xlsx"
    if not datei.exists():
        datei.write_bytes(_get(PINK_SHEET).content)
    roh = pd.read_excel(datei, sheet_name="Monthly Prices", header=None)
    namen = roh.iloc[4].astype(str).str.strip()
    spalten = {
        "Natural gas, Europe": "gas_eu_usd_mmbtu",
        "Coal, Australian": "kohle_au_usd_t",
        "Coal, South African **": "kohle_za_usd_t",
    }
    out = {}
    for name, ziel in spalten.items():
        j = int(namen[namen == name].index[0])
        out[ziel] = pd.to_numeric(roh.iloc[6:, j], errors="coerce").to_numpy()
    monate = roh.iloc[6:, 0].astype(str).str.replace("M", "-")
    df = pd.DataFrame(out, index=pd.PeriodIndex(monate, freq="M"))
    return df.dropna(how="all")


def usd_je_eur(cache: Path) -> pd.Series:
    datei = cache / "eurostat_usd.json"
    if not datei.exists() or time.time() - datei.stat().st_mtime > 86400:
        datei.write_bytes(_get(EUROSTAT_USD).content)
    d = json.loads(datei.read_text())
    zeit = d["dimension"]["time"]["category"]["index"]
    werte = {p: d["value"].get(str(i)) for p, i in zeit.items()}
    s = pd.Series(werte, dtype=float).dropna()
    s.index = pd.PeriodIndex(s.index, freq="M")
    return s.rename("usd_je_eur")


def acer_lng(cache: Path) -> pd.Series:
    datei = cache / "acer_lng.csv"
    if not datei.exists() or time.time() - datei.stat().st_mtime > 86400:
        datei.write_bytes(_get(ACER).content)
    d = pd.read_csv(io.BytesIO(datei.read_bytes()), encoding="latin-1")
    s = pd.Series(
        pd.to_numeric(d["EU PRICE (EUR/MWh)"], errors="coerce").to_numpy(),
        index=pd.to_datetime(d["DATE"]),
    )
    # Die ersten Tage (Jan. 2023) haben nur NWE/SE; EU fehlt dort -> NWE als Ersatz.
    nwe = pd.Series(
        pd.to_numeric(d["NORTH-WEST EUROPE PRICE (EUR/MWh)"], errors="coerce").to_numpy(),
        index=s.index,
    )
    return s.fillna(nwe).dropna().sort_index().rename("lng_eur_mwh")
