"""Batteriespeicher aus der öffentlichen Einheitenübersicht des Marktstammdatenregisters.

Die Übersicht (``GetErweiterteOeffentlicheEinheitStromerzeugung``) ist dieselbe JSON-Schnittstelle,
die die Tabelle auf marktstammdatenregister.de füllt, Lizenz dl-de/by-2-0. Der Gesamtdatenexport
wäre vollständiger, liegt aber auf ``download.marktstammdatenregister.de``, das aus der
Cloud-Umgebung nicht erreichbar war. Abgerufen werden

- alle Speichereinheiten über 30 kW mit jedem Betriebsstatus (``gross.json.gz``),
- für Einheiten bis 30 kW in Betrieb die Anzahl je Inbetriebnahmemonat (``klein_anzahl.json``)
  und je Monat eine zufällige Seite von 500 Einheiten für die mittlere Größe
  (``klein_stichprobe.json.gz``).

Aufruf::

    uv run --no-project --with requests python mastr_abruf.py ./speicher-daten/mastr
"""

import datetime as dt
import gzip
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
OUT = Path(sys.argv[1])
OUT.mkdir(parents=True, exist_ok=True)
SPEICHER = "Energieträger~eq~'2496'"
KEEP = [
    "MaStRNummer",
    "BetriebsStatusName",
    "InbetriebnahmeDatum",
    "GeplantesInbetriebsnahmeDatum",
    "EndgueltigeStilllegungDatum",
    "EinheitRegistrierungsdatum",
    "Nettonennleistung",
    "Bruttoleistung",
    "NutzbareSpeicherkapazitaet",
    "StromspeichertechnologieBezeichnung",
    "Batterietechnologie",
    "SpannungsebenenNamen",
    "Bundesland",
    "VollTeilEinspeisungBezeichnung",
    "AnlagenbetreiberPersonenArt",
    "EinheitName",
    "SystemStatusName",
    "DatumLetzteAktualisierung",
]


def q(filt: str, page: int = 1, size: int = 1) -> dict[str, Any]:
    """Eine Seite der Übersicht.

    Filter in der Kendo-Syntax der Oberfläche (``~eq~``, ``~gt~``, ``~lt~``); ``~le~`` und
    ``~ge~`` lehnt der Server ab.
    """
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


def schlank(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{k: r.get(k) for k in KEEP} for r in rows]


# 1) alle Einheiten über 30 kW, jeder Status
f = SPEICHER + "~and~Nettonennleistung der Einheit~gt~'30'"
if not (OUT / "gross.json.gz").exists():
    gesamt = q(f)["Total"]
    rows = []
    for p in range(1, gesamt // 5000 + 2):
        rows += schlank(q(f, p, 5000)["Data"])
    with gzip.open(OUT / "gross.json.gz", "wt") as fh:
        json.dump(rows, fh, ensure_ascii=False)
    print("über 30 kW", gesamt, len(rows))

# 2) bis 30 kW in Betrieb: Anzahl je Inbetriebnahmemonat und Stichprobe
klein = SPEICHER + "~and~Nettonennleistung der Einheit~lt~'30.001'~and~Betriebs-Status~eq~'35'"
heute = dt.datetime.now(dt.UTC).date()
counts, sample = [], []
random.seed(1)
for y in range(2010, heute.year + 1):
    for m in range(1, 13):
        if (y, m) > (heute.year, heute.month):
            break
        a = (dt.date(y, m, 1) - dt.timedelta(days=1)).strftime("%d.%m.%Y")
        b = f"01.{m % 12 + 1:02d}.{y + (m == 12)}"
        fm = klein + (
            f"~and~Inbetriebnahmedatum der Einheit~gt~'{a}'"
            f"~and~Inbetriebnahmedatum der Einheit~lt~'{b}'"
        )
        n = q(fm)["Total"]
        counts.append(dict(monat=f"{y}-{m:02d}", anzahl=n))
        if n:
            sample += schlank(q(fm, random.randint(1, max(1, n // 500)), 500)["Data"])
    print(y, sum(c["anzahl"] for c in counts if c["monat"].startswith(str(y))), flush=True)
vor_2010 = q(klein + "~and~Inbetriebnahmedatum der Einheit~lt~'01.01.2010'")["Total"]
alle = q(klein)["Total"]
(OUT / "klein_anzahl.json").write_text(
    json.dumps(dict(counts=counts, vor_2010=vor_2010, gesamt=alle))
)
with gzip.open(OUT / "klein_stichprobe.json.gz", "wt") as fh:
    json.dump(sample, fh, ensure_ascii=False)
print("bis 30 kW", alle, vor_2010, len(sample))
