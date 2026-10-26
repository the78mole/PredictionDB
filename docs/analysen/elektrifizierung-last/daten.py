"""Eingangsdaten für die Analyse "Elektrifizierung und Last".

Frei abrufbar (werden geladen und zwischengespeichert):

- Stündliche Netzlast Deutschland 2015-2026: Energy-Charts ``/public_power`` (CC BY 4.0), über
  ``preisniveau-schaetzung/daten.py``.
- Stündliche Lufttemperatur von 22 DWD-Stationen ab 1991 (DWD Open Data, CC BY 4.0), gewichtet
  mit der Einwohnerzahl der Region zu einer Deutschland-Temperatur.
- Pkw-Bestand und -Neuzulassungen nach Antrieb: Eurostat ``road_eqs_carpda`` /
  ``road_eqr_carpda`` (CC BY 4.0).
- Endenergie der Haushalte nach Anwendung und Energieträger: Eurostat ``nrg_d_hhq``
  (CC BY 4.0), daraus die Umweltwärme der Wärmepumpen.

Werte, die nur als PDF oder Webseite vorliegen, stehen mit Quelle in ``HANDWERTE``. Sie
werden nur in dieser einmaligen Analyse verwendet und nicht in die Datenbank übernommen.
"""

from __future__ import annotations

import importlib.util
import io
import re
import zipfile
from pathlib import Path
from typing import Any

import httpx
import pandas as pd

HIER = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location(
    "preisniveau_daten", HIER.parent / "preisniveau-schaetzung" / "daten.py"
)
assert _spec and _spec.loader
ec_daten = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ec_daten)

DWD = (
    "https://opendata.dwd.de/climate_environment/CDC/observations_germany/climate/hourly/"
    "air_temperature"
)
EUROSTAT = "https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data"
ERSTES_WETTERJAHR = 1991

# DWD-Station -> Einwohner der zugeordneten Region in Mio. (Bundesländer, große Länder auf
# mehrere Stationen aufgeteilt; Destatis-Stand 2024, gerundet).
STATIONEN = {
    "01975": ("Hamburg-Fuhlsbüttel", 4.9),  # HH + SH
    "04271": ("Rostock-Warnemünde", 1.6),
    "02014": ("Hannover", 8.1),
    "00691": ("Bremen", 0.7),
    "00433": ("Berlin-Tempelhof", 3.8),
    "00427": ("Berlin Brandenburg", 2.6),
    "03126": ("Magdeburg", 2.2),
    "01048": ("Dresden-Klotzsche", 2.0),
    "02928": ("Leipzig-Holzhausen", 2.0),
    "01270": ("Erfurt-Weimar", 2.1),
    "01303": ("Essen-Bredeney", 6.0),
    "02667": ("Köln/Bonn", 6.0),
    "01766": ("Münster/Osnabrück", 6.1),
    "01420": ("Frankfurt/Main", 6.3),
    "05906": ("Mannheim", 4.1),  # RP
    "04336": ("Saarbrücken-Ensheim", 1.0),
    "04928": ("Stuttgart (Schnarrenberg)", 7.0),
    "01443": ("Freiburg", 4.2),
    "01262": ("München-Flughafen", 5.0),
    "03668": ("Nürnberg", 3.5),
    "00232": ("Augsburg", 2.5),
    "04104": ("Regensburg", 2.4),
}

# Einmalig von Hand übernommene Werte (Quelle je Eintrag).
HANDWERTE: dict[str, Any] = {
    # Bundesverband Wärmepumpe, Absatz Heizungswärmepumpen in Stück
    # https://www.waermepumpe.de/presse/zahlen-daten/absatzzahlen/
    "bwp_absatz": {
        2007: 45_300,
        2008: 62_500,
        2009: 54_800,
        2010: 51_000,
        2011: 57_000,
        2012: 59_500,
        2013: 60_000,
        2014: 58_000,
        2015: 57_000,
        2016: 66_500,
        2017: 78_000,
        2018: 84_000,
        2019: 86_300,
        2020: 120_000,
        2021: 154_000,
        2022: 236_000,
        2023: 356_000,
        2024: 193_000,
        2025: 299_000,
    },
    # Fachverband Gebäude-Klima: verkaufte Außengeräte Split/Multi-Split/VRF (nur Eckwerte
    # aus dem Text, Grafik nicht maschinenlesbar)
    # https://www.haustechnikdialog.de/News/30725/Marktentwicklung-Raumklimageraete-und-Luft-Luft-Waermepumpen
    "fgk_klima": {2020: 330_000, 2024: 250_000},
    # AG Energiebilanzen, Anwendungsbilanzen 2023/2024, Endenergie in PJ
    # https://ag-energiebilanzen.de/wp-content/uploads/EBD24e_AnwBil.pdf (Tab. 1.1, 2.1, 3.1)
    # Warmwasser Haushalte nach Energieträger nur für 2023 (Tab. 5):
    # https://ag-energiebilanzen.de/wp-content/uploads/2024/11/AGEB_AnwBilanzen_2023_Endbericht_2025-04.pdf
    "ageb_pj": {
        "hh_raumwaerme": {
            "oel": 353.5,
            "gas": 631.1,
            "kohle": 7.3,
            "fernwaerme": 167.6,
            "strom": 28.1,
            "erneuerbar": 323.1,
        },
        "hh_warmwasser_2023": {
            "oel": 61.1,
            "gas": 184.8,
            "kohle": 0.0,
            "fernwaerme": 18.0,
            "strom": 53.8,
        },
        "ghd_raumwaerme": {
            "oel": 59.2,
            "gas": 300.5,
            "kohle": 0.0,
            "fernwaerme": 76.3,
            "strom": 31.1,
            "erneuerbar": 129.8,
        },
        "ghd_warmwasser": {
            "oel": 0.6,
            "gas": 25.2,
            "fernwaerme": 4.9,
            "strom": 5.5,
            "erneuerbar": 5.7,
        },
        "ind_raumwaerme": {
            "oel": 11.7,
            "gas": 75.4,
            "kohle": 4.3,
            "fernwaerme": 13.1,
            "strom": 1.7,
            "erneuerbar": 20.9,
        },
    },
    # BDEW Statusreport Wärme 07/2026, S. 56: Netto-Fernwärmeerzeugung 2025 und Anteile in %
    # https://www.bdew.de/media/documents/2026_07_10_Statusreport_Waerme_final_1.pdf
    "fernwaerme_twh": 133.0,
    "fernwaerme_fossil_anteil": (49.3 + 7.9 + 3.6 + 1.5) / 100,
    # KBA über dpa: Pkw-Fahrleistung 2024 gut 594 Mrd. km, 12.309 km je Pkw
    # https://www.autohaus.de/nachrichten/autohandel/trendwende-menschen-in-deutschland-fahren-wieder-mehr-auto-3688922
    "pkw_fahrleistung_km": 594e9,
    # Destatis: rund 49,5 Mio. Pkw am 1.1.2026
    # https://www.destatis.de/DE/Presse/Pressemitteilungen/2026/07/PD26_N050_46_85.html
    "pkw_bestand": 49.5e6,
    # ADAC über ecomento: Ladeverluste 10-25 %
    # https://ecomento.de/2020/07/23/adac-ermittelt-elektroauto-ladeverluste/
    "ladeverlust": (0.10, 0.25),
    # Verivox/Innofact-Umfrage Juli 2026: 23,4 % der Haushalte mit Klimaanlage (2025: 17,9 %),
    # davon 69,2 % mobile Monoblockgeräte; 24,9 % planen eine Anschaffung
    # https://www.heidelberg24.de/politik-und-wirtschaft/klimaanlagen-boom-immer-mehr-haushalte-kaufen-geraete-zr-94393480.html
    "klima_anteil_haushalte": {2025: 0.179, 2026: 0.234},
    "klima_anteil_mobil_2026": 0.692,
    # FGK über dpa-AFX (15.07.2026): Raumklimageräte (meist Split) 2020 ~350.000, 2023 ~260.000,
    # 2025 ~320.000 Stück; VDKF: Wartezeit auf Installateure "gut drei Monate"
    # https://www.boerse-online.de/dpa-afx/roundup-begehrte-kuehlgeraete-vielerorts-vergriffen-und-nun-538041.html
    "fgk_raumklima_absatz": {2020: 350_000, 2023: 260_000, 2025: 320_000},
    # Destatis Mikrozensus 2024: 40,8 Mio. Privathaushalte
    # https://www.drweb.de/41-millionen-haushalte-wie-deutschland-wirklich-wohnt/
    "haushalte": 40.8e6,
    # BNetzA, Genehmigung Szenariorahmen NEP Strom 2037/2045 (2025), Szenario B, S. 4
    # https://www.netzentwicklungsplan.de/sites/default/files/2025-04/Genehmigung%20Szenariorahmen%202025_0.pdf
    "nep_b": {
        2037: {"pv_gw": 345.4, "grossspeicher_gw": 67.6},
        2045: {"pv_gw": 400.0, "grossspeicher_gw": 67.6},
    },
    # battery-charts.de über solarserver.de: Juli 2026 29,8 GWh, davon ~22 GWh Heimspeicher
    # https://www.solarserver.de/2026/07/06/batteriekapazitaet-in-deutschland-waechst-auf-298-gwh-rekordtempo-beim-speicherausbau/
    "heimspeicher_gwh_2026": 22.0,
}


def _http(url: str, **params: str) -> httpx.Response:
    r = httpx.get(url, params=params, timeout=300, follow_redirects=True)
    r.raise_for_status()
    return r


def _dwd_station(sid: str, cache: Path) -> pd.Series:
    datei = cache / f"dwd_{sid}.csv.gz"
    if datei.exists():
        s = pd.read_csv(datei, index_col=0).iloc[:, 0]
        s.index = pd.to_datetime(s.index, utc=True)
        return s
    liste = _http(f"{DWD}/historical/").text
    teile = []
    namen = sorted(set(re.findall(rf"stundenwerte_TU_{sid}_\d{{8}}_\d{{8}}_hist\.zip", liste)))
    urls = [f"{DWD}/historical/{n}" for n in namen] + [
        f"{DWD}/recent/stundenwerte_TU_{sid}_akt.zip"
    ]
    for url in urls:
        z = zipfile.ZipFile(io.BytesIO(_http(url).content))
        name = next(n for n in z.namelist() if n.startswith("produkt"))
        df = pd.read_csv(z.open(name), sep=";", skipinitialspace=True)
        t = pd.to_datetime(df["MESS_DATUM"].astype(str), format="%Y%m%d%H", utc=True)
        teile.append(pd.Series(df["TT_TU"].to_numpy(), index=t))
    s = pd.concat(teile)
    s = s[~s.index.duplicated(keep="last")].sort_index()
    s = s.where(s > -900)
    s = s[s.index >= pd.Timestamp(f"{ERSTES_WETTERJAHR}-01-01", tz="UTC")].rename(sid)
    s.to_csv(datei)
    return s


def temperatur(cache: Path) -> pd.Series:
    """Einwohnergewichtete Stundentemperatur Deutschland (UTC-Index, °C)."""
    cache.mkdir(parents=True, exist_ok=True)
    df = pd.DataFrame({sid: _dwd_station(sid, cache) for sid in STATIONEN})
    idx = pd.date_range(df.index.min(), df.index.max(), freq="1h", tz="UTC")
    df = df.reindex(idx).interpolate(limit=6)
    w = pd.Series({sid: g for sid, (_, g) in STATIONEN.items()})
    gew = df.notna().mul(w, axis=1)
    t = (df.fillna(0) * w).sum(axis=1) / gew.sum(axis=1)
    return t[gew.sum(axis=1) > 0.7 * w.sum()].rename("temperatur")


def last(cache: Path, bis: int) -> pd.Series:
    df = ec_daten.lade(cache / "energy_charts", bis)
    s = df["last"].dropna()
    s.index = s.index.tz_convert("UTC")
    return (s / 1000).rename("last_gw")


def _eurostat(datensatz: str, **filter_: str | list[str]) -> pd.DataFrame:
    params: list[tuple[str, str | int | float | bool | None]] = [("format", "JSON"), ("lang", "EN")]
    for k, v in filter_.items():
        for x in [v] if isinstance(v, str) else v:
            params.append((k, x))
    d = httpx.get(f"{EUROSTAT}/{datensatz}", params=params, timeout=120).json()
    dims, size = d["id"], d["size"]
    cats = {k: list(d["dimension"][k]["category"]["index"]) for k in dims}
    zeilen = []
    for idx, v in d["value"].items():
        i, pos = int(idx), []
        for s in reversed(size):
            pos.append(i % s)
            i //= s
        zeilen.append({**{k: cats[k][p] for k, p in zip(dims, pos[::-1], strict=True)}, "wert": v})
    return pd.DataFrame(zeilen)


def pkw(cache: Path) -> pd.DataFrame:
    """Pkw-Bestand (31.12.) und Neuzulassungen nach Antrieb je Jahr (Stück)."""
    datei = cache / "eurostat_pkw.csv"
    if datei.exists():
        return pd.read_csv(datei, index_col=0)
    antrieb = ["TOTAL", "ELC", "ELC_PET_PI", "ELC_DIE_PI"]
    b = _eurostat("road_eqs_carpda", geo="DE", mot_nrg=antrieb, leg_form="TOTAL")
    n = _eurostat("road_eqr_carpda", geo="DE", mot_nrg=antrieb)
    out = pd.concat(
        [
            b.pivot_table(index="time", columns="mot_nrg", values="wert").add_prefix("bestand_"),
            n.pivot_table(index="time", columns="mot_nrg", values="wert").add_prefix("neu_"),
        ],
        axis=1,
    )
    out.index = out.index.astype(int)
    for p in ("bestand_", "neu_"):
        out[f"{p}phev"] = out.pop(f"{p}ELC_PET_PI") + out.pop(f"{p}ELC_DIE_PI")
    out = out.rename(columns=lambda c: c.replace("ELC", "bev").replace("TOTAL", "gesamt"))
    cache.mkdir(parents=True, exist_ok=True)
    out.to_csv(datei)
    return out


def haushalte_waerme(cache: Path) -> pd.DataFrame:
    """Haushalte: Raumwärme + Warmwasser nach Energieträger je Jahr in TWh (Eurostat)."""
    datei = cache / "eurostat_hhq.csv"
    if datei.exists():
        return pd.read_csv(datei, index_col=0)
    d = _eurostat(
        "nrg_d_hhq",
        geo="DE",
        unit="TJ",
        nrg_bal=["FC_OTH_HH_E_SH", "FC_OTH_HH_E_WH", "FC_OTH_HH_E_SC"],
        siec=["TOTAL", "G3000", "O4000", "SFF_P1000_S2000", "RA600", "E7000", "H8000"],
    )
    d["wert"] = d["wert"] / 3600  # TJ -> TWh
    out = d.pivot_table(index="time", columns=["nrg_bal", "siec"], values="wert")
    out.columns = [f"{a.split('_')[-1].lower()}_{b}" for a, b in out.columns]
    out.index = out.index.astype(int)
    out.to_csv(datei)
    return out


def installiert(cache: Path) -> pd.DataFrame:
    """Installierte Leistung je Monat: PV (GW DC), Batteriespeicher (GW, GWh); Energy-Charts."""
    datei = cache / "installed_power.csv"
    if datei.exists():
        df = pd.read_csv(datei, index_col=0)
        df.index = pd.PeriodIndex(df.index, freq="M")
        return df
    d = _http(
        f"{ec_daten.API}/installed_power",
        country="de",
        time_step="monthly",
        installation_decommission="false",
    ).json()
    idx = pd.PeriodIndex([pd.Period(f"{t[3:]}-{t[:2]}", "M") for t in d["time"]])
    namen = {
        "Solar DC": "pv_gw",
        "Battery storage (power)": "speicher_gw",
        "Battery storage (capacity)": "speicher_gwh",
    }
    out = {
        spalte: pd.Series(
            next(p["data"] for p in d["production_types"] if p["name"] == name),
            index=idx,
            dtype=float,
        )
        for name, spalte in namen.items()
    }
    df = pd.DataFrame(out).ffill().fillna(0.0)
    df.to_csv(datei)
    return df


def pv_erzeugung(cache: Path, bis: int) -> pd.Series:
    """Stündliche PV-Erzeugung in GW (Energy-Charts, UTC-Index)."""
    df = ec_daten.lade(cache / "energy_charts", bis)
    s = df["solar"].dropna()
    s.index = s.index.tz_convert("UTC")
    return (s / 1000).rename("pv_gw")
