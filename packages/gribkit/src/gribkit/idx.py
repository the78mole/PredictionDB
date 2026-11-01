"""Parser für NCEP-Inventardateien (``.idx``) und Bildung von Byte-Bereichen.

Eine ``.idx``-Zeile hat die Form::

    581:415092545:d=2026102006:TMP:2 m above ground:42 hour fcst:

Die Länge einer Nachricht ergibt sich aus dem Offset der folgenden Zeile.
Für die letzte Nachricht ist die Länge nur bekannt, wenn die Dateigröße
übergeben wird; sonst bleibt sie offen (``Range: bytes=<offset>-``).

@relation(SWA-GRIB-IDX, scope=file)
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from itertools import pairwise


@dataclass(frozen=True, slots=True)
class IdxEntry:
    """Eine Nachricht aus einer ``.idx``-Datei."""

    number: str
    offset: int
    length: int | None
    date: str
    var: str
    level: str
    fcst: str

    @property
    def end(self) -> int | None:
        """Letztes Byte (inklusive) oder ``None``, wenn unbekannt."""
        return None if self.length is None else self.offset + self.length - 1

    @property
    def key(self) -> str:
        """``VAR:LEVEL:FCST``, wie wgrib2 es ausgibt."""
        return f"{self.var}:{self.level}:{self.fcst}"


@dataclass(frozen=True, slots=True)
class MessageSpec:
    """Auswahl einer Nachricht über Kurzname und Level.

    ``fcst`` ist ein optionaler regulärer Ausdruck auf das Prognosefeld der
    ``.idx``-Zeile, z. B. ``"ave"`` für Mittelwerte. Ohne ``fcst`` werden
    alle Nachrichten mit passendem ``VAR:LEVEL`` gewählt (bei GFS z. B. für
    ``TCDC`` sowohl der Momentanwert als auch das Intervallmittel).

    @relation(SWR-GRIB-002, scope=class)
    """

    var: str
    level: str
    fcst: str | None = None

    def matches(self, entry: IdxEntry) -> bool:
        if entry.var != self.var or entry.level != self.level:
            return False
        return self.fcst is None or re.search(self.fcst, entry.fcst) is not None

    @property
    def label(self) -> str:
        return f"{self.var}:{self.level}" + (f":{self.fcst}" if self.fcst else "")


@dataclass(frozen=True, slots=True)
class ByteRange:
    """Zusammenhängender Byte-Bereich (``end`` inklusive, ``None`` = bis Dateiende)."""

    start: int
    end: int | None
    entries: tuple[IdxEntry, ...]

    @property
    def header(self) -> str:
        return f"bytes={self.start}-" + ("" if self.end is None else str(self.end))

    @property
    def length(self) -> int | None:
        return None if self.end is None else self.end - self.start + 1

    def extract(self, data: bytes) -> bytes:
        """Schneidet aus den geladenen Bytes nur die gewählten Nachrichten heraus
        (bei verschmolzenen Bereichen liegen dazwischen fremde Nachrichten).

        @relation(SWR-GRIB-003, scope=function)
        """
        if self.length is not None and len(data) != self.length:
            raise ValueError(f"{self.header}: {len(data)} Bytes erhalten, {self.length} erwartet")
        parts = []
        for e in self.entries:
            lo = e.offset - self.start
            parts.append(data[lo:] if e.length is None else data[lo : lo + e.length])
        return b"".join(parts)


class IdxParseError(ValueError):
    """@relation(SWR-GRIB-001, scope=class)"""

    pass


def parse_idx(text: str, total_size: int | None = None) -> list[IdxEntry]:
    """Liest eine ``.idx``-Datei. Unter-Nachrichten (``12.1``, ``12.2``) teilen
    sich einen Offset und damit denselben Byte-Bereich.

    @relation(SWR-GRIB-001, scope=function)
    """
    rows: list[tuple[str, int, str, str, str, str]] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        parts = line.split(":")
        if len(parts) < 6:
            raise IdxParseError(f"Zeile {lineno}: zu wenige Felder: {line!r}")
        try:
            offset = int(parts[1])
        except ValueError as exc:
            raise IdxParseError(f"Zeile {lineno}: Offset ist keine Zahl: {line!r}") from exc
        number, _, date, var, level = parts[:5]
        fcst = ":".join(parts[5:]).rstrip(":")
        rows.append((number, offset, date.removeprefix("d="), var, level, fcst))

    offsets = sorted({r[1] for r in rows})
    next_offset = dict(pairwise(offsets))
    if total_size is not None and offsets:
        if total_size <= offsets[-1]:
            raise IdxParseError("Dateigröße liegt vor dem letzten Offset")
        next_offset[offsets[-1]] = total_size

    entries = []
    for number, offset, date, var, level, fcst in rows:
        nxt = next_offset.get(offset)
        entries.append(
            IdxEntry(number, offset, None if nxt is None else nxt - offset, date, var, level, fcst)
        )
    return entries


def select(entries: Iterable[IdxEntry], specs: Sequence[MessageSpec]) -> list[IdxEntry]:
    """Alle Einträge, die mindestens eine Spezifikation erfüllen (Reihenfolge der Datei).

    @relation(SWR-GRIB-002, scope=function)
    """
    return [e for e in entries if any(s.matches(e) for s in specs)]


def missing_specs(entries: Iterable[IdxEntry], specs: Sequence[MessageSpec]) -> list[MessageSpec]:
    """Spezifikationen ohne Treffer, z. B. akkumulierte Größen im Analyse-Schritt f000.

    @relation(SWR-GRIB-002, scope=function)
    """
    entries = list(entries)
    return [s for s in specs if not any(s.matches(e) for e in entries)]


def merge_ranges(entries: Iterable[IdxEntry], max_gap: int = 0) -> list[ByteRange]:
    """Fasst gewählte Nachrichten zu möglichst wenigen Byte-Bereichen zusammen.

    Nachrichten mit gleichem Offset (Unter-Nachrichten) werden nur einmal
    geladen. Liegen zwei Bereiche höchstens ``max_gap`` Bytes auseinander,
    werden sie verschmolzen (die Lücke wird mitgeladen und verworfen).

    @relation(SWR-GRIB-003, scope=function)
    """
    unique: dict[int, IdxEntry] = {}
    for e in sorted(entries, key=lambda e: e.offset):
        unique.setdefault(e.offset, e)

    ranges: list[ByteRange] = []
    for e in unique.values():
        if ranges:
            last = ranges[-1]
            if last.end is not None and e.offset - (last.end + 1) <= max_gap:
                ranges[-1] = ByteRange(last.start, e.end, (*last.entries, e))
                continue
        ranges.append(ByteRange(e.offset, e.end, (e,)))
    return ranges
