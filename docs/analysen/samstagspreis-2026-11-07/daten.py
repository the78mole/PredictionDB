"""Abruf der freien Eingangsdaten für die Samstagspreis-Analyse (mit lokalem Cache).

* SMARD (Bundesnetzagentur | SMARD.de, CC BY 4.0): Stundenwerte Preis, Last, Residuallast
  und ÜNB-Prognosen für Last, Wind und PV.
* Energy-Charts.info (Fraunhofer ISE, CC BY 4.0): Day-Ahead-Preise der Nachbarzonen (Lizenz
  laut Feld ``license_info``: CC BY 4.0 von Bundesnetzagentur | SMARD.de), Handelssaldo
  Deutschlands (``/cbet``) und Kernkrafterzeugung Frankreichs (``/public_power``).
* Weltbank Pink Sheet (CC BY 4.0): Monatsmittel Erdgas Europa in USD/MMBtu.
* Eurostat ``ert_bil_eur_m`` (CC BY 4.0): Monatsmittel USD je EUR.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import httpx
import pandas as pd

SMARD = "https://www.smard.de/app/chart_data"
EC = "https://api.energy-charts.info"
PINK_SHEET = (
    "https://thedocs.worldbank.org/en/doc/74e8be41ceb20fa0da750cda2f6b9e4e-0050012026/"
    "related/CMO-Historical-Data-Monthly.xlsx"
)
EUROSTAT_USD = (
    "https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data/ert_bil_eur_m"
    "?currency=USD&statinfo=AVG&unit=NAC&format=JSON&lang=en&sinceTimePeriod=2023-01"
)
TZ = "Europe/Berlin"

_CLIENT = httpx.Client(
    timeout=httpx.Timeout(30.0, read=120.0),
    follow_redirects=True,
    headers={"User-Agent": "PredictionDB-Analyse"},
)


def _get(url: str, params: dict[str, str] | None = None) -> httpx.Response:
    for versuch in range(6):
        try:
            r = _CLIENT.get(url, params=params)
        except httpx.TransportError:
            time.sleep(5 * (versuch + 1))
            continue
        if r.status_code in (429, 502, 503):
            time.sleep(10 * (versuch + 1))
            continue
        r.raise_for_status()
        return r
    raise RuntimeError(f"{url}: kein Abruf nach 6 Versuchen")


def smard_reihe(fid: int, region: str, von: str, bis: str, cache: Path) -> pd.Series:
    """Stundenwerte einer SMARD-Reihe zwischen ``von`` und ``bis`` (Ortsdatum, inklusive)."""
    ordner = cache / "smard" / f"{fid}_{region}"
    ordner.mkdir(parents=True, exist_ok=True)
    start = pd.Timestamp(von, tz=TZ)
    ende = pd.Timestamp(bis, tz=TZ) + pd.Timedelta(days=1)
    stamps = _get(f"{SMARD}/{fid}/{region}/index_hour.json").json()["timestamps"]
    stamps = sorted(int(s) for s in stamps)
    teile = []
    for i, st in enumerate(stamps):
        b0 = pd.Timestamp(st, unit="ms", tz="UTC")
        b1 = pd.Timestamp(stamps[i + 1], unit="ms", tz="UTC") if i + 1 < len(stamps) else ende
        if b1 <= start or b0 >= ende:
            continue
        datei = ordner / f"{st}.json"
        if i >= len(stamps) - 2 or not datei.exists():  # jüngste Blöcke werden noch ergänzt
            url = f"{SMARD}/{fid}/{region}/{fid}_{region}_hour_{st}.json"
            datei.write_text(_get(url).text)
        werte = json.loads(datei.read_text())["series"]
        teile.append(pd.DataFrame(werte, columns=["ms", "wert"]))
    df = pd.concat(teile).dropna().drop_duplicates("ms")
    idx = pd.to_datetime(df.ms, unit="ms", utc=True).dt.tz_convert(TZ)
    s = pd.Series(df.wert.to_numpy(float), index=pd.DatetimeIndex(idx)).sort_index()
    return s[(s.index >= start) & (s.index < ende)]


def _ec_json(pfad: str, params: dict[str, str], datei: Path) -> dict[str, Any]:
    if not datei.exists():
        datei.parent.mkdir(parents=True, exist_ok=True)
        datei.write_text(_get(f"{EC}/{pfad}", params).text)
    out: dict[str, Any] = json.loads(datei.read_text())
    return out


def _stundenmittel(sek: list[int], werte: list[float | None]) -> pd.Series:
    idx = pd.to_datetime(pd.Series(sek), unit="s", utc=True).dt.tz_convert(TZ)
    s = pd.Series(pd.to_numeric(pd.Series(werte), errors="coerce").to_numpy(), index=idx)
    return s.resample("1h").mean()


def ec_preis(zone: str, von: str, bis: str, cache: Path) -> tuple[pd.Series, str]:
    d = _ec_json(
        "price",
        {"bzn": zone, "start": von, "end": bis},
        cache / "ec" / f"preis_{zone}_{von}_{bis}.json",
    )
    return _stundenmittel(d["unix_seconds"], d["price"]), str(d.get("license_info", ""))


def ec_handel_de(von: str, bis: str, cache: Path) -> pd.Series:
    """Handelssaldo DE in GW (+ = Import), Summe über alle Grenzpartner."""
    d = _ec_json(
        "cbet", {"country": "de", "start": von, "end": bis}, cache / "ec" / f"cbet_{von}_{bis}.json"
    )
    for land in d["countries"]:
        if str(land["name"]).lower() == "sum":
            return _stundenmittel(d["unix_seconds"], land["data"])
    raise RuntimeError("cbet: keine Summenreihe")


def ec_kernkraft_fr(von: str, bis: str, cache: Path) -> pd.Series:
    d = _ec_json(
        "public_power",
        {"country": "fr", "start": von, "end": bis},
        cache / "ec" / f"public_power_fr_{von}_{bis}.json",
    )
    for typ in d["production_types"]:
        if typ["name"] == "Nuclear":
            return _stundenmittel(d["unix_seconds"], typ["data"])
    raise RuntimeError("public_power fr: keine Reihe Nuclear")


def gas_europa(cache: Path) -> pd.Series:
    """Weltbank Pink Sheet, Erdgas Europa in USD/MMBtu je Monat."""
    datei = cache / "cmo.xlsx"
    if not datei.exists():
        datei.write_bytes(_get(PINK_SHEET).content)
    roh = pd.read_excel(datei, sheet_name="Monthly Prices", header=None)
    namen = roh.iloc[4].astype(str).str.strip()
    j = int(namen[namen == "Natural gas, Europe"].index[0])
    monate = roh.iloc[6:, 0].astype(str).str.replace("M", "-")
    s = pd.Series(
        pd.to_numeric(roh.iloc[6:, j], errors="coerce").to_numpy(),
        index=pd.PeriodIndex(monate, freq="M"),
    )
    return s.dropna()


def usd_je_eur(cache: Path) -> pd.Series:
    """Eurostat, Monatsmittel USD je EUR."""
    datei = cache / "eurostat_usd.json"
    if not datei.exists():
        datei.write_bytes(_get(EUROSTAT_USD).content)
    d = json.loads(datei.read_text())
    zeit = d["dimension"]["time"]["category"]["index"]
    werte = {p: d["value"].get(str(i)) for p, i in zeit.items()}
    s = pd.Series(werte, dtype=float).dropna()
    s.index = pd.PeriodIndex(s.index, freq="M")
    return s
