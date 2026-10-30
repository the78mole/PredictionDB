"""Installierte Leistung je Tag aus Inbetriebnahme-, Stilllegungs- und Unterbrechungsdaten.

Jede Einheit erzeugt Ereignisse ``(Tag, ±Leistung)``: plus bei Inbetriebnahme und Wiederaufnahme,
minus bei endgültiger oder vorübergehender Stilllegung (wirksam ab dem Stilllegungstag). Die
kumulierte Summe ergibt den Bestand je Tag. Ereignisse vor dem Betrachtungsbeginn zählen
zum Anfangsbestand, solche nach dem Stichtag des Exports werden ignoriert.

@relation(SWA-MAI-CAPACITY, scope=file)
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date

import numpy as np
from numpy.typing import NDArray


def daily_capacity(
    events: Iterable[tuple[date, float]], start: date, end: date
) -> NDArray[np.float64]:
    """Bestand für jeden Tag in ``[start, end]`` (Länge ``(end - start).days + 1``).

    @relation(SWR-MAI-033, scope=function)
    """
    if end < start:
        raise ValueError("end liegt vor start")
    n = (end - start).days + 1
    delta = np.zeros(n)
    for day, value in events:
        if day > end:
            continue
        delta[max(0, (day - start).days)] += value
    out: NDArray[np.float64] = np.cumsum(delta)
    # Rundungsreste aus Zu- und Abgängen nicht als negative Leistung stehen lassen
    out[np.abs(out) < 1e-6] = 0.0
    return out
