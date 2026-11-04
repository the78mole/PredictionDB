"""Stammdaten der französischen Kernkraftwerke, einmalig von Hand übernommen.

Koordinaten (zwei Nachkommastellen), Gewässer und Kühlart entsprechen den Artikeln
„Centrale nucléaire de …“ der französischen Wikipedia (CC BY-SA 4.0, Link je Standort). Die
Werte sind aus dem Kenntnisstand eingetragen und nicht Zeile für Zeile gegen die Artikel
geprüft, weil Wikipedia aus der Analyseumgebung nicht erreichbar war; die Auswertung braucht
nur den 0,5°-Gitterpunkt und die Gruppe. Bugey (Blöcke 2 und 3 mit Durchlaufkühlung, 4 und 5
mit Kühltürmen) zählt als ``fluss_offen``. Fessenheim (Rhein) ist seit 2020 stillgelegt.

Die Kästen der Einzugsgebiete (Süd, Nord, West, Ost in Grad) sind grobe Rechtecke über dem
Oberlauf und dienen nur als Mittelungsgebiet für Niederschlag und Schnee aus dem GFS.
"""

from __future__ import annotations

from typing import NamedTuple

WIKI = "https://fr.wikipedia.org/wiki/Centrale_nucl%C3%A9aire_"


class Standort(NamedTuple):
    code: str
    name: str
    lat: float
    lon: float
    gewaesser: str  # Fluss, Meer oder Ästuar
    gruppe: str  # fluss_offen | fluss_turm | kueste
    entsoe: str  # Präfix der Blocknamen in ENTSO-E 16.1.A (Großbuchstaben)
    einzugsgebiet: tuple[float, float, float, float] | None  # S, N, W, O
    quelle: str


STANDORTE: tuple[Standort, ...] = (
    Standort(
        "BUG",
        "Bugey",
        45.80,
        5.27,
        "Rhône",
        "fluss_offen",
        "BUGEY",
        (45.5, 47.0, 5.5, 8.5),
        WIKI + "du_Bugey",
    ),
    Standort(
        "SAL",
        "Saint-Alban",
        45.40,
        4.76,
        "Rhône",
        "fluss_offen",
        "ST ALBAN",
        (45.0, 48.0, 4.0, 8.0),
        WIKI + "de_Saint-Alban",
    ),
    Standort(
        "TRI",
        "Tricastin",
        44.33,
        4.73,
        "Rhône",
        "fluss_offen",
        "TRICASTIN",
        (44.5, 48.0, 4.0, 8.0),
        WIKI + "du_Tricastin",
    ),
    Standort(
        "BLA",
        "Blayais",
        45.26,
        -0.69,
        "Gironde",
        "fluss_offen",
        "BLAYAIS",
        (42.5, 45.5, -0.5, 3.0),
        WIKI + "du_Blayais",
    ),
    Standort(
        "CRU",
        "Cruas",
        44.63,
        4.76,
        "Rhône",
        "fluss_turm",
        "CRUAS",
        (44.5, 48.0, 4.0, 8.0),
        WIKI + "de_Cruas",
    ),
    Standort(
        "GOL",
        "Golfech",
        44.11,
        0.85,
        "Garonne",
        "fluss_turm",
        "GOLFECH",
        (42.5, 44.5, -0.5, 2.5),
        WIKI + "de_Golfech",
    ),
    Standort(
        "BEL",
        "Belleville",
        47.51,
        2.88,
        "Loire",
        "fluss_turm",
        "BELLEVILLE",
        (44.5, 47.5, 2.0, 4.5),
        WIKI + "de_Belleville",
    ),
    Standort(
        "DAM",
        "Dampierre",
        47.73,
        2.52,
        "Loire",
        "fluss_turm",
        "DAMPIERRE",
        (44.5, 47.7, 2.0, 4.5),
        WIKI + "de_Dampierre",
    ),
    Standort(
        "SLB",
        "Saint-Laurent",
        47.72,
        1.58,
        "Loire",
        "fluss_turm",
        "ST LAURENT",
        (44.5, 47.8, 1.5, 4.5),
        WIKI + "de_Saint-Laurent",
    ),
    Standort(
        "CHI",
        "Chinon",
        47.23,
        0.17,
        "Loire",
        "fluss_turm",
        "CHINON",
        (44.5, 47.8, 0.0, 4.5),
        WIKI + "de_Chinon",
    ),
    Standort(
        "CIV",
        "Civaux",
        46.46,
        0.65,
        "Vienne",
        "fluss_turm",
        "CIVAUX",
        (45.5, 46.5, 0.5, 2.5),
        WIKI + "de_Civaux",
    ),
    Standort(
        "CAT",
        "Cattenom",
        49.42,
        6.22,
        "Moselle",
        "fluss_turm",
        "CATTENOM",
        (47.8, 49.5, 5.5, 7.2),
        WIKI + "de_Cattenom",
    ),
    Standort(
        "CHO",
        "Chooz",
        50.09,
        4.79,
        "Meuse",
        "fluss_turm",
        "CHOOZ",
        (47.8, 50.0, 4.5, 5.8),
        WIKI + "de_Chooz",
    ),
    Standort(
        "NOG",
        "Nogent",
        48.52,
        3.52,
        "Seine",
        "fluss_turm",
        "NOGENT",
        (47.4, 48.6, 3.4, 5.2),
        WIKI + "de_Nogent",
    ),
    Standort(
        "GRA",
        "Gravelines",
        51.02,
        2.14,
        "Nordsee",
        "kueste",
        "GRAVELINES",
        None,
        WIKI + "de_Gravelines",
    ),
    Standort("PEN", "Penly", 49.98, 1.21, "Ärmelkanal", "kueste", "PENLY", None, WIKI + "de_Penly"),
    Standort(
        "PAL", "Paluel", 49.86, 0.64, "Ärmelkanal", "kueste", "PALUEL", None, WIKI + "de_Paluel"
    ),
    Standort(
        "FLA",
        "Flamanville",
        49.54,
        -1.88,
        "Ärmelkanal",
        "kueste",
        "FLAMANVILLE",
        None,
        WIKI + "de_Flamanville",
    ),
)

#: Pegel, die die automatische Wahl in abruf.py ersetzen (Hub'eau-Code, Grund)
PEGEL_FEST: dict[str, tuple[str, str]] = {
    # Le Rhône à Bourg-Saint-Andéol misst den kurzgeschlossenen Alt-Rhône (Median ≈ 85 m³/s),
    # der Kanal von Donzère-Mondragon mit dem Kühlwasser von Tricastin läuft daran vorbei.
    "TRI": ("V4530010", "Le Rhône à Viviers, oberhalb der Kanalabzweigung"),
}
