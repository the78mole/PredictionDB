"""Silber-Variablen und ihre Zuordnung zu GRIB2-Nachrichten.

Nachrichten werden über die numerischen GRIB2-Schlüssel erkannt (Disziplin, Kategorie,
Parameter, Flächentyp, Höhe, ``stepType``), nicht über ``shortName``: der hängt von den
ecCodes-Tabellen und vom Zentrum ab (GFS-Strahlung ist z. B. der NCEP-Parameter 0/4/192).
Was nicht in der Tabelle steht, wird ignoriert; das deckt die Obermenge ab, die der
NOMADS-Filter liefert.

Gespeichert wird als Int16 mit ``scale_factor``/``add_offset`` (CF-Konvention, wie
``docs/ARCHITEKTUR.md`` 5.1). Mittel und Summen der Quelle beziehen sich bei GFS auf Eimer, die
alle 6 h neu beginnen (``36-42 hour ave``); in Silber stehen stattdessen Werte für das Intervall
seit dem vorigen Schritt (siehe :mod:`wx_process.decode`).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

StepType = Literal["instant", "avg", "accum"]

#: (discipline, parameterCategory, parameterNumber, typeOfFirstFixedSurface, level, stepType)
GribKey = tuple[int, int, int, int, int, str]


@dataclass(frozen=True, slots=True)
class Variable:
    name: str
    units: str
    long_name: str
    #: Auflösung der Ablage (Int16) und Mittelpunkt des Wertebereichs
    scale: float
    offset: float = 0.0
    #: Untergrenze nach dem Rückrechnen von Eimer-Mitteln (Rundung kann leicht negativ werden)
    min_value: float | None = None
    #: ``accum``: Summe im Intervall geteilt durch dessen Länge (Rate je Stunde)
    per_hour: bool = False

    @property
    def lo(self) -> float:
        return self.offset - 32767 * self.scale

    @property
    def hi(self) -> float:
        return self.offset + 32767 * self.scale


V = Variable
VARIABLES: dict[GribKey, Variable] = {
    (0, 0, 0, 103, 2, "instant"): V("t2m", "K", "Temperatur 2 m", 0.01, 273.15),
    (0, 0, 6, 103, 2, "instant"): V("d2m", "K", "Taupunkt 2 m", 0.01, 273.15),
    (0, 1, 1, 103, 2, "instant"): V("r2m", "%", "Relative Feuchte 2 m", 0.01, 50.0, 0.0),
    (0, 2, 2, 103, 10, "instant"): V("u10", "m s-1", "Wind 10 m, West-Ost", 0.01),
    (0, 2, 3, 103, 10, "instant"): V("v10", "m s-1", "Wind 10 m, Süd-Nord", 0.01),
    (0, 2, 2, 103, 100, "instant"): V("u100", "m s-1", "Wind 100 m, West-Ost", 0.01),
    (0, 2, 3, 103, 100, "instant"): V("v100", "m s-1", "Wind 100 m, Süd-Nord", 0.01),
    (0, 2, 22, 1, 0, "instant"): V("gust", "m s-1", "Böen am Boden", 0.01, 0.0, 0.0),
    # GFS meldet DSWRF als NCEP-Parameter 0/4/192, WMO-Standard ist 0/4/7
    (0, 4, 192, 1, 0, "avg"): V("dswrf", "W m-2", "Globalstrahlung, Intervallmittel", 0.05, 0, 0),
    (0, 4, 7, 1, 0, "avg"): V("dswrf", "W m-2", "Globalstrahlung, Intervallmittel", 0.05, 0, 0),
    (0, 6, 1, 10, 0, "instant"): V("tcc", "%", "Gesamtbedeckung", 0.01, 50.0, 0.0),
    (0, 6, 1, 10, 0, "avg"): V("tcc_mean", "%", "Gesamtbedeckung, Intervallmittel", 0.01, 50, 0),
    (0, 1, 8, 1, 0, "accum"): V(
        "tp", "kg m-2 h-1", "Niederschlag je Stunde im Intervall", 0.001, 30.0, 0.0, per_hour=True
    ),
    (0, 1, 195, 1, 0, "instant"): V("csnow", "1", "Schneefall (kategorisch)", 0.001, 0, 0),
    (0, 1, 195, 1, 0, "avg"): V("csnow_mean", "1", "Schneefall, Anteil im Intervall", 0.001, 0, 0),
    (0, 3, 1, 101, 0, "instant"): V("prmsl", "Pa", "Luftdruck auf Meereshöhe", 1.0, 100000.0),
}

BY_NAME: dict[str, Variable] = {v.name: v for v in VARIABLES.values()}

#: Version der Silber-Kodierung; eine Änderung erzwingt den Neuaufbau aus Bronze
SILVER_VERSION = "1"
