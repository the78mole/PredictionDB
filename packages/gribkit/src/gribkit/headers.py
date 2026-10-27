"""Inventar einer GRIB2-Datei aus den Nachrichtenköpfen, wenn es keine ``.idx`` gibt.

Archive wie NCAR GDEX (d084001) halten nur die GRIB-Dateien vor. Statt der ganzen Datei werden
je Nachricht die ersten Bytes per Range gelesen: Sektion 0 nennt die Länge (damit den Anfang
der nächsten Nachricht), Sektion 4 Parameter, Fläche und Prognosezeit. Daraus entsteht eine
Zeile wie in einer ``.idx`` (wgrib2-Namen), sodass Auswahl und Byte-Bereiche aus
:mod:`gribkit.idx` unverändert gelten.

Die Namen decken die Parameter ab, die die Quellen-Konfiguration anfragt, und einige
Nachbarn; unbekannte Parameter heißen ``var<D>_<K>_<N>`` und werden nie ausgewählt.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

from gribkit.idx import IdxEntry

#: So viele Bytes je Nachricht lesen; GFS braucht für Sektion 0 bis 4 etwa 160
HEAD_BYTES = 512

#: (Disziplin, Kategorie, Parameter) → wgrib2-Name, NCEP-Tabellen wie in den GFS-``.idx``
PARAMETERS: dict[tuple[int, int, int], str] = {
    (0, 0, 0): "TMP",
    (0, 0, 4): "TMAX",
    (0, 0, 5): "TMIN",
    (0, 0, 6): "DPT",
    (0, 1, 0): "SPFH",
    (0, 1, 1): "RH",
    (0, 1, 3): "PWAT",
    (0, 1, 7): "PRATE",
    (0, 1, 8): "APCP",
    (0, 1, 10): "ACPCP",
    (0, 1, 11): "SNOD",
    (0, 1, 13): "WEASD",
    (0, 1, 192): "CRAIN",
    (0, 1, 193): "CFRZR",
    (0, 1, 194): "CICEP",
    (0, 1, 195): "CSNOW",
    (0, 2, 2): "UGRD",
    (0, 2, 3): "VGRD",
    (0, 2, 8): "VVEL",
    (0, 2, 22): "GUST",
    (0, 3, 0): "PRES",
    (0, 3, 1): "PRMSL",
    (0, 3, 5): "HGT",
    (0, 3, 192): "MSLET",
    (0, 4, 7): "DSWRF",
    (0, 4, 8): "USWRF",
    (0, 4, 192): "DSWRF",
    (0, 4, 193): "USWRF",
    (0, 5, 3): "DLWRF",
    (0, 5, 4): "ULWRF",
    (0, 5, 192): "DLWRF",
    (0, 5, 193): "ULWRF",
    (0, 6, 1): "TCDC",
    (0, 6, 201): "SUNSD",
    (0, 7, 6): "CAPE",
    (0, 7, 7): "CIN",
    (2, 0, 0): "LAND",
    (2, 0, 2): "TSOIL",
    (2, 0, 192): "SOILW",
}

#: Flächentypen ohne Höhenangabe
_SURFACES = {
    1: "surface",
    8: "top of atmosphere",
    10: "entire atmosphere",
    101: "mean sea level",
    200: "entire atmosphere (considered as a single layer)",
}

#: Statistische Verarbeitung (Code-Tabelle 4.10) → wgrib2-Kürzel
_STATS = {0: "ave", 1: "acc", 2: "max", 3: "min"}

#: Einheit der Zeit (Code-Tabelle 4.4) in Stunden
_UNIT_HOURS = {0: 1 / 60, 1: 1, 2: 24, 10: 3, 11: 6, 12: 12, 13: 1 / 3600}


class HeaderError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class Head:
    """Kopf einer GRIB2-Nachricht: Gesamtlänge und, wenn bestimmbar, die ``.idx``-Felder."""

    length: int
    var: str
    level: str
    fcst: str


def _signed(b: int) -> int:
    """Vorzeichen-Betrag-Kodierung von GRIB2 (oberstes Bit = Vorzeichen) für ein Byte."""
    return -(b & 0x7F) if b & 0x80 else b


def _scaled(factor: int, value: int) -> float | None:
    if value == 0xFFFFFFFF:
        return None
    return value / 10.0 ** _signed(factor)


def _level(sec4: bytes) -> str:
    t1, f1, v1 = sec4[22], sec4[23], struct.unpack(">I", sec4[24:28])[0]
    t2, f2, v2 = sec4[28], sec4[29], struct.unpack(">I", sec4[30:34])[0]
    if t1 in _SURFACES:
        return _SURFACES[t1]
    a = _scaled(f1, v1)
    if t1 == 103 and a is not None:
        return f"{a:g} m above ground"
    if t1 == 106 and a is not None:
        b = _scaled(f2, v2) if t2 == 106 else None
        return f"{a:g}-{b:g} m below ground" if b is not None else f"{a:g} m below ground"
    if t1 == 100 and a is not None:
        return f"{a / 100:g} mb"
    if t1 == 102 and a is not None:
        return f"{a:g} m above mean sea level"
    if t1 == 108 and a is not None:
        b = _scaled(f2, v2) if t2 == 108 else None
        if b is not None:
            return f"{a / 100:g}-{b / 100:g} mb above ground"
    return f"surface{t1}"


def _hours(unit: int, value: int) -> float:
    if unit not in _UNIT_HOURS:
        raise HeaderError(f"Zeiteinheit {unit} unbekannt")
    return value * _UNIT_HOURS[unit]


def _fcst(sec4: bytes, template: int) -> str:
    start = _hours(sec4[17], struct.unpack(">i", sec4[18:22])[0])
    if template in (0, 1):
        return "anl" if start == 0 else f"{start:g} hour fcst"
    if template in (8, 11):
        # Ensemble (4.11) hat vor der Zeitangabe drei Bytes mehr als 4.8
        base = 34 if template == 8 else 37
        n = sec4[base + 7]
        if n < 1:
            raise HeaderError("Statistik ohne Zeitbereich")
        r = base + 12
        kind = _STATS.get(sec4[r], f"stat{sec4[r]}")
        end = start + _hours(sec4[r + 2], struct.unpack(">I", sec4[r + 3 : r + 7])[0])
        if end and start % 24 == 0 and end % 24 == 0:
            # wgrib2 schreibt ganze Tage als Tage (``0-1 day acc fcst``)
            return f"{start / 24:g}-{end / 24:g} day {kind} fcst"
        return f"{start:g}-{end:g} hour {kind} fcst"
    return f"template4.{template}"


def parse_head(data: bytes) -> Head:
    """Liest Sektion 0 bis 4 vom Anfang einer GRIB2-Nachricht (mindestens bis Sektion 4)."""
    if len(data) < 16 or data[:4] != b"GRIB":
        raise HeaderError("keine GRIB-Kennung")
    if data[7] != 2:
        raise HeaderError(f"nur GRIB2 unterstützt, Edition {data[7]}")
    discipline = data[6]
    (length,) = struct.unpack(">Q", data[8:16])
    pos = 16
    while pos + 5 <= len(data):
        (sec_len,) = struct.unpack(">I", data[pos : pos + 4])
        number = data[pos + 4]
        if sec_len < 5:
            raise HeaderError(f"Sektion {number} mit Länge {sec_len}")
        if number == 4:
            sec4 = data[pos : pos + sec_len]
            if len(sec4) < sec_len or sec_len < 34:
                break
            (template,) = struct.unpack(">H", sec4[7:9])
            var = PARAMETERS.get((discipline, sec4[9], sec4[10]))
            name = var or f"var{discipline}_{sec4[9]}_{sec4[10]}"
            return Head(length, name, _level(sec4), _fcst(sec4, template))
        if number > 4:
            raise HeaderError("Sektion 4 fehlt")
        pos += sec_len
    raise HeaderError(f"Sektion 4 liegt nicht in den ersten {len(data)} Bytes")


def find_boundary(chunk: bytes, size_left: int) -> int | None:
    """Erster Nachrichtenanfang in ``chunk`` (Position nach einem ``7777``), sonst ``None``.

    ``size_left`` ist die Zahl der Bytes ab Anfang von ``chunk`` bis Dateiende. ``GRIB`` kann
    zufällig in gepackten Daten stehen; verlangt werden deshalb das Ende der vorigen Nachricht
    davor, Edition 2 und eine Länge, die in die Datei passt. Ob der Treffer echt war, zeigt
    erst der Lauf über die Köpfe, der genau am nächsten Anfang bzw. Dateiende landen muss.
    """
    pos = chunk.find(b"7777GRIB")
    while pos >= 0:
        start = pos + 4
        if len(chunk) - start >= 16 and chunk[start + 7] == 2:
            (length,) = struct.unpack(">Q", chunk[start + 8 : start + 16])
            if 16 < length <= size_left - start:
                return start
        pos = chunk.find(b"7777GRIB", pos + 1)
    return None


def entry(number: int, offset: int, head: Head, date: str = "") -> IdxEntry:
    """Zeile wie aus einer ``.idx`` für die Nachricht an ``offset``."""
    return IdxEntry(str(number), offset, head.length, date, head.var, head.level, head.fcst)
