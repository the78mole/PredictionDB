"""Quellen für Markt- und Netzdaten.

Jede Quelle bietet mehrere Datensätze (z. B. ``price``, ``generation``) an. Ein Datensatz
wird für ein Zeitfenster ``[start, end)`` abgerufen und liefert eine oder mehrere Reihen.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Protocol

from energy_ingest.model import SeriesData


class SourceError(RuntimeError):
    """Quelle hat geantwortet, aber nicht verwertbar (z. B. Fehlermeldung statt Daten)."""


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
    def datasets(self) -> list[str]: ...

    def max_chunk(self, dataset: str) -> timedelta: ...

    def fetch(self, dataset: str, start: datetime, end: datetime) -> list[SeriesData]: ...


def clip(
    points: list[tuple[datetime, float]], start: datetime, end: datetime
) -> list[tuple[datetime, float]]:
    return [(ts, v) for ts, v in points if start <= ts < end]
