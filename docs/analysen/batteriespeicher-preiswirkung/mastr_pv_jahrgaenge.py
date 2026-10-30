"""PV-Leistung in Betrieb je Inbetriebnahmejahr und Größenklasse aus der MaStR-Einheitenübersicht.

Für die Energy-Sharing-Betrachtung: welche Leistung fällt wann aus der EEG-Förderung (Ü20) und
wie viel liegt in Jahrgängen mit niedriger Vergütung. Je Jahr und Klasse zählt das Skript die
Einheiten (``Total``) und zieht eine zufällige Seite mit 300 Einheiten für die mittlere
Nettonennleistung. Aufruf::

    uv run --no-project --with requests python mastr_pv_jahrgaenge.py ./speicher-daten/mastr
"""

import json
import random
import sys
import time
from pathlib import Path
from typing import Any

import requests  # type: ignore[import-untyped]

URL = (
    "https://www.marktstammdatenregister.de/MaStR/Einheit/EinheitJson/"
    "GetErweiterteOeffentlicheEinheitStromerzeugung"
)


def q(filt: str, page: int = 1, size: int = 1) -> dict[str, Any]:
    """Eine Seite der Übersicht, Filter wie in ``mastr_abruf.py``."""
    for i in range(5):
        try:
            r = requests.get(
                URL,
                params=dict(sort="", page=page, pageSize=size, group="", filter=filt),
                timeout=300,
            )
            r.raise_for_status()
            d = r.json()
            if d.get("Errors") or "Total" not in d:
                raise RuntimeError(str(d)[:200])
            return dict(d)
        except Exception as e:
            print("Wiederholung", e, file=sys.stderr)
            time.sleep(2**i)
    raise SystemExit("abgebrochen")


OUT = Path(sys.argv[1])
SOLAR = "Energieträger~eq~'2495'~and~Betriebs-Status~eq~'35'"
KLASSEN = [(0, 10), (10, 30), (30, 100), (100, 1000), (1000, 1e6)]
random.seed(2)
zeilen = []
for jahr in range(2000, 2027):
    for lo, hi in KLASSEN:
        f = (
            SOLAR
            + f"~and~Inbetriebnahmedatum der Einheit~gt~'31.12.{jahr - 1}'"
            + f"~and~Inbetriebnahmedatum der Einheit~lt~'01.01.{jahr + 1}'"
            + f"~and~Nettonennleistung der Einheit~gt~'{lo}'"
            + f"~and~Nettonennleistung der Einheit~lt~'{hi + 0.001}'"
        )
        n = q(f)["Total"]
        kw = 0.0
        if n:
            seite = q(f, random.randint(1, max(1, n // 300)), 300)["Data"]
            werte = [r["Nettonennleistung"] or 0 for r in seite]
            kw = sum(werte) / len(werte) if werte else 0.0
        zeilen.append(
            dict(jahr=jahr, klasse=f"{lo}-{hi:g}", anzahl=n, kw_mittel=kw, mw=n * kw / 1000)
        )
    print(jahr, round(sum(z["mw"] for z in zeilen if z["jahr"] == jahr)), flush=True)
(OUT / "pv_jahrgaenge.json").write_text(json.dumps(zeilen))
