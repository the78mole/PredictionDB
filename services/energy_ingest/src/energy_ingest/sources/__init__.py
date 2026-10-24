"""Quellen für Markt- und Netzdaten.

Jede Quelle bietet mehrere Datensätze (z. B. ``price``, ``generation``) an. Ein Datensatz
wird für ein Zeitfenster ``[start, end)`` abgerufen und liefert eine oder mehrere Reihen.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import Protocol

from energy_ingest.model import BERLIN, SeriesData


class SourceError(RuntimeError):
    """Quelle hat geantwortet, aber nicht verwertbar (z. B. Fehlermeldung statt Daten)."""


class FormatError(SourceError):
    """Aufbau der Antwort weicht vom erwarteten ab (Spalten, Blätter, Einheiten).

    Der Abruf schreibt dann nichts und meldet ``status = 'format'``; Grafana alarmiert.
    """


class SourceMeta(Protocol):
    code: str  # meta.source.code
    license: str
    priority: int  # kleiner = bevorzugt in energy.value_best
    #: Nutzungsbedingungen erlauben freie Weitergabe (meta.source.free); unfreie Quellen
    #: laufen nur mit Freigabe (``EnergySettings.allowed``)
    free: bool


class Source(SourceMeta, Protocol):
    # Optional: ``sync_lookback: dict[str, timedelta]`` verlängert den Rückblick des
    # laufenden Abgleichs je Datensatz (siehe ``energy_ingest.ingest.sync_lookback``)
    # Optional: ``empty_retry: timedelta`` verkürzt die Wartezeit bis zum erneuten Versuch
    # leer geladener Blöcke (siehe ``energy_ingest.ingest.empty_retry``)
    def datasets(self) -> list[str]: ...

    def max_chunk(self, dataset: str) -> timedelta: ...

    def fetch(self, dataset: str, start: datetime, end: datetime) -> list[SeriesData]: ...


def clip(
    points: list[tuple[datetime, float]], start: datetime, end: datetime
) -> list[tuple[datetime, float]]:
    return [(ts, v) for ts, v in points if start <= ts < end]


def day_start(d: date) -> datetime:
    """Beginn des Kalendertags ``d`` in Ortszeit, als UTC (Zeitstempel von Tageswerten)."""
    return datetime(d.year, d.month, d.day, tzinfo=BERLIN).astimezone(UTC)


class Cached[T]:
    """Ergebnis eines teuren Abrufs (ganze Datei) für ``ttl`` im Speicher halten, damit der
    15-min-Abgleich und Backfill-Blöcke dieselbe Datei nicht wiederholt laden."""

    def __init__(self, load: Callable[[], T], ttl: timedelta) -> None:
        self.load = load
        self.ttl = ttl.total_seconds()
        self._value: T | None = None
        self._until = 0.0

    def get(self) -> T:
        if self._value is None or time.monotonic() >= self._until:
            self._value = self.load()
            self._until = time.monotonic() + self.ttl
        return self._value
