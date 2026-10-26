"""Daten für die Auswertung zur Steuerbarkeit nach § 14a EnWG.

Preisblätter 2026: von Hand aus den PDFs der Netzbetreiber übernommen (nur für diese
einmalige Analyse, Quelle je Netzbetreiber). Alle Preise netto. Die Pauschale in Modul 1
folgt bei allen Netzbetreibern der Formel der Festlegung BK8-22/010-A (80 € brutto plus
20 % Stabilitätsprämie auf 3.750 kWh zum Standard-Arbeitspreis); ``pruefe_preisblaetter``
rechnet sie nach.

Day-Ahead-Preise: SMARD (Bundesnetzagentur, CC BY 4.0), Gebotszone DE-LU, stündlich.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import numpy as np
import pandas as pd

SMARD = "https://www.smard.de/app/chart_data/4169/DE-LU"
ALLE = (1, 2, 3, 4)
WINTER = (1, 4)
SOMMER = (2, 3)


@dataclass(frozen=True)
class Preisblatt:
    vnb: str
    quelle: str
    stand: str  # endgültig, vorläufig
    gp: float  # €/a, Niederspannung ohne Leistungsmessung
    ap: float  # ct/kWh
    modul1: float  # €/a Gutschrift
    modul2_ap: float  # ct/kWh
    modul2_gp: float  # €/a für den separaten Zählpunkt
    ht: float  # ct/kWh, Modul 3
    st: float
    nt: float
    # Zeitfenster je Tarifstufe: (Stufe, von, bis, Quartale); nicht genannte Zeiten = ST
    fenster: tuple[tuple[str, str, str, tuple[int, ...]], ...] = field(default=())
    hinweis: str = ""


PREISBLAETTER: tuple[Preisblatt, ...] = (
    Preisblatt(
        "Bayernwerk Netz",
        "https://www.bayernwerk-netz.de/content/dam/revu-global/bayernwerk-netz/files/netz/netzzugang/netzentgeltestrom/bayernwerk-preisblatt-netzentgelte-2026.pdf",
        "endgültig",
        98.55,
        4.72,
        102.63,
        1.89,
        0.0,
        9.03,
        4.72,
        0.47,
        (("HT", "17:00", "22:00", SOMMER), ("NT", "10:00", "15:00", SOMMER)),
        "Modul 3 nur im 2. und 3. Quartal",
    ),
    Preisblatt(
        "NEW Netz",
        "https://www.new-netz.de/mediathek/Preisblatt_NN_Strom_2026.pdf",
        "endgültig",
        76.65,
        7.17,
        121.00,
        2.87,
        0.0,
        12.00,
        7.17,
        1.04,
        (("HT", "17:00", "20:30", ALLE), ("NT", "00:00", "06:00", ALLE)),
    ),
    Preisblatt(
        "Stromnetz Berlin",
        "https://www.stromnetz.berlin/files/globalassets/dokumente/entgelte/zugang/entgelte-01-01-2026/nne-b-2026_20260522.pdf",
        "endgültig",
        33.36,
        7.46,
        123.18,
        2.98,
        0.0,
        13.94,
        7.46,
        2.61,
        (("HT", "17:15", "20:15", ALLE), ("NT", "22:15", "06:30", ALLE)),
    ),
    Preisblatt(
        "Avacon Netz",
        "https://www.avacon-netz.de/content/dam/revu-global/avacon-netz/documents/netzentgelte-strom/2026/Preisbl%C3%A4tter_AVANG_Strom_01.01.2026.pdf",
        "vorläufig",
        80.30,
        6.04,
        112.53,
        2.41,
        0.0,
        8.41,
        6.04,
        0.60,
        (("HT", "16:30", "21:00", WINTER), ("NT", "00:15", "05:00", WINTER)),
        "Modul 3 nur im 1. und 4. Quartal",
    ),
    Preisblatt(
        "Netze ODR",
        "https://www.netze-odr.de/fileadmin/Netze-ODR/Dokumente/Unternehmen/Veroeffentlichungen/Netzentgelte/Netzentgelte_Strom_2026.pdf",
        "endgültig",
        65.70,
        5.87,
        111.25,
        2.35,
        0.0,
        10.18,
        5.87,
        2.35,
        (
            ("NT", "11:00", "17:00", SOMMER),
            ("HT", "22:00", "05:00", SOMMER),
        ),
        "Modul 3 nur im 2. und 3. Quartal, HT nachts",
    ),
    Preisblatt(
        "Netz Leipzig",
        "https://files.l.de/lde-typo3/Netz/Downloads/Netznutzung/Strom/Preisblatt_Netzentgelte_Strom_2026__voraussichtlich_.pdf",
        "vorläufig",
        75.00,
        4.29,
        99.40,
        1.72,
        0.0,
        4.29,
        4.85,
        1.66,
        (("HT", "17:00", "19:00", WINTER), ("NT", "02:30", "03:30", WINTER)),
        "vorläufig; HT liegt unter ST (so im Preisblatt)",
    ),
    Preisblatt(
        "enercity Netz",
        "https://www.enercity-netz.de/assets/cms/eng/partner/preisarchiv-strom/preisblatt-netznutzung-strom-2026-tag-vorlaeufig.pdf",
        "vorläufig",
        53.00,
        8.54,
        131.28,
        3.42,
        0.0,
        13.35,
        8.54,
        0.86,
        (("HT", "16:30", "20:15", ALLE), ("NT", "00:00", "06:00", ALLE)),
    ),
    Preisblatt(
        "EAM Netz",
        "https://www.eam-netz.de/fileadmin/user_upload/Ueber_Uns/Netzinformation/Netzentgelte/strom/251009_Preisblatt_EAM_Netz_Strom_2026_-_unter_Vorbehalt.pdf",
        "vorläufig",
        105.85,
        5.48,
        108.33,
        2.19,
        0.0,
        10.52,
        5.48,
        1.64,
        (
            ("NT", "23:00", "06:00", ALLE),
            ("NT", "11:00", "15:00", ALLE),
            ("HT", "17:00", "22:00", ALLE),
        ),
    ),
    Preisblatt(
        "RheinNetz",
        "https://www.rheinnetz.de/netzentgelte-strom-ab-dem-01.01.2026.pdfx",
        "endgültig",
        150.00,
        3.78,
        95.58,
        1.51,
        0.0,
        6.64,
        3.78,
        1.13,
        (("HT", "17:00", "19:30", ALLE), ("NT", "23:45", "05:45", ALLE)),
    ),
    Preisblatt(
        "WEMAG Netz",
        "https://www.wemag-netz.de/media/506/download",
        "endgültig",
        98.55,
        5.13,
        105.71,
        2.05,
        0.0,
        7.70,
        5.13,
        1.95,
        (
            ("HT", "07:00", "11:00", WINTER),
            ("HT", "17:00", "20:00", WINTER),
            ("NT", "21:00", "06:00", WINTER),
        ),
        "Modul 3 nur im 1. und 4. Quartal",
    ),
    Preisblatt(
        "Netze Duisburg",
        "https://www.netze-duisburg.de/fileadmin/user_upload/Netz_nutzen/Netzentgelte/Strom/251215_Netze_Duisburg_-_Preisblatt_Strom_2026_Final.pdf",
        "endgültig",
        101.00,
        6.94,
        119.28,
        2.78,
        0.0,
        9.77,
        6.94,
        2.09,
        (("HT", "14:15", "18:45", WINTER), ("NT", "00:00", "06:00", WINTER)),
        "Modul 3 nur im 1. und 4. Quartal",
    ),
    Preisblatt(
        "e-regio Netz",
        "https://cms.e-regio.de/assets/global/Dokumente/Netznutzungsentgelte/2025-12-19-Netzentgelte-Strom-e-regio-Netz-2026.pdf",
        "endgültig",
        80.30,
        9.02,
        134.85,
        3.61,
        0.0,
        15.46,
        9.02,
        3.61,
        (("HT", "15:00", "20:00", ALLE), ("NT", "00:00", "06:00", ALLE)),
    ),
)


def _viertel(zeit: str) -> int:
    h, m = zeit.split(":")
    return int(h) * 4 + int(m) // 15


def stufen(pb: Preisblatt, quartal: int) -> np.ndarray:
    """Tarifstufe je Viertelstunde (96 Werte, 'HT'/'ST'/'NT') im Quartal."""
    s = np.array(["ST"] * 96, dtype=object)
    for stufe, von, bis, quartale in pb.fenster:
        if quartal not in quartale:
            continue
        a, b = _viertel(von), _viertel(bis)
        idx = np.arange(a, b) if a < b else np.r_[np.arange(a, 96), np.arange(0, b)]
        s[idx] = stufe
    return s


def preis_stunde(pb: Preisblatt, quartal: int) -> np.ndarray:
    """Mittlerer Modul-3-Arbeitspreis je Stunde (ct/kWh, 24 Werte)."""
    p = {"HT": pb.ht, "ST": pb.st, "NT": pb.nt}
    v = np.array([p[x] for x in stufen(pb, quartal)], dtype=float)
    return v.reshape(24, 4).mean(axis=1)


def anteil_stunde(pb: Preisblatt, quartal: int, stufe: str) -> np.ndarray:
    return np.asarray((stufen(pb, quartal) == stufe).reshape(24, 4).mean(axis=1))


def pruefe_preisblaetter() -> pd.DataFrame:
    """Modul 1 nach BK8-22/010-A nachrechnen, Modul 2 = 40 % des Arbeitspreises."""
    zeilen = []
    for pb in PREISBLAETTER:
        m1 = 80 / 1.19 + 0.2 * pb.ap / 100 * 3750
        zeilen.append(
            {
                "vnb": pb.vnb,
                "modul1_preisblatt": pb.modul1,
                "modul1_formel": round(m1, 2),
                "modul2_preisblatt": pb.modul2_ap,
                "modul2_40prozent": round(0.4 * pb.ap, 2),
            }
        )
    return pd.DataFrame(zeilen).set_index("vnb")


def day_ahead(cache: Path, von: str = "2024-01-01", bis: str = "2026-01-01") -> pd.Series:
    """Day-Ahead DE-LU stündlich in €/MWh (SMARD), UTC-Index."""
    datei = cache / f"smard_4169_{von}_{bis}.csv"
    if datei.exists():
        s = pd.read_csv(datei, index_col=0, parse_dates=True).iloc[:, 0]
        return s
    with httpx.Client(timeout=60) as c:
        idx = c.get(f"{SMARD}/index_hour.json").json()["timestamps"]
        a = pd.Timestamp(von, tz="UTC").value // 10**6 - 7 * 86400 * 1000
        b = pd.Timestamp(bis, tz="UTC").value // 10**6
        werte: list[list[float]] = []
        for ts in idx:
            if a <= ts < b:
                d = c.get(f"{SMARD}/4169_DE-LU_hour_{ts}.json").json()
                werte.extend(x for x in d["series"] if x[1] is not None)
    s = pd.Series({pd.Timestamp(t, unit="ms", tz="UTC"): v for t, v in werte}, name="eur_mwh")
    s = s.sort_index()
    s = s[(s.index >= pd.Timestamp(von, tz="UTC")) & (s.index < pd.Timestamp(bis, tz="UTC"))]
    cache.mkdir(parents=True, exist_ok=True)
    s.to_csv(datei)
    return s


if __name__ == "__main__":
    print(pruefe_preisblaetter().to_string())
    print(json.dumps({pb.vnb: list(preis_stunde(pb, 1).round(2)) for pb in PREISBLAETTER[:2]}))
