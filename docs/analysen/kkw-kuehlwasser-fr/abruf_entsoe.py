"""ENTSO-E-Rohdaten für kkw-kuehlwasser-fr laden (nur lokal, nicht ins Repo).

Nur Standardbibliothek, damit es ohne Umgebung auf der VM läuft, wo der Token liegt:

    ssh daniel@172.22.7.162 'set -a; . /opt/predictiondb/.env; python3 - /tmp/kkw_entsoe' \
        < docs/analysen/kkw-kuehlwasser-fr/abruf_entsoe.py

- 16.1.A Ist-Erzeugung je Block (A73, Kernkraft B14) für FR, Tag für Tag
  -> ZIEL/a73/<JJJJ-MM-TT>.csv (Block-mRID, Name, Stunde UTC, MW als Stundenmittel; Kurventyp
  A03 innerhalb jeder Period vorwärts gefüllt, Stunden ohne Period fehlen)
- 15.1.A/B Nichtverfügbarkeit der Erzeugungseinheiten (A80) für FR, monatsweise mit Offset
  -> ZIEL/a80/<JJJJ-MM>_<offset>.zip (Originalantworten)

ENTSO-E-Reihen stehen nicht auf der Liste der frei weiterverwendbaren Daten; die Rohdaten bleiben
lokal, ins Repo kommen nur aggregierte Kennzahlen (CLAUDE.md, Daten und Lizenzen).
"""

from __future__ import annotations

import datetime as dt
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

API = "https://web-api.tp.entsoe.eu/api"
FR = "10YFR-RTE------C"
START = dt.date(2021, 3, 1)
ENDE = dt.date(2026, 11, 1)


def hole(params: dict[str, str]) -> bytes | None:
    params = {**params, "securityToken": os.environ["PDB_ENTSOE_TOKEN"]}
    url = API + "?" + urllib.parse.urlencode(params)
    for versuch in range(6):
        try:
            with urllib.request.urlopen(url, timeout=120) as r:
                data: bytes = r.read()
                return data
        except urllib.error.HTTPError as exc:
            if exc.code == 400:  # "No matching data found" kommt als 400 mit Acknowledgement
                return None
            if exc.code == 429:
                time.sleep(60)
                continue
            time.sleep(2**versuch)
        except Exception:
            time.sleep(2**versuch)
    raise RuntimeError(f"ENTSO-E nicht erreichbar: {params.get('documentType')}")


def lokal(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def kind(el: ET.Element, *pfad: str) -> ET.Element | None:
    cur: ET.Element | None = el
    for n in pfad:
        if cur is None:
            return None
        cur = next((c for c in cur if lokal(c.tag) == n), None)
    return cur


def text(el: ET.Element, *pfad: str) -> str:
    c = kind(el, *pfad)
    return c.text.strip() if c is not None and c.text else ""


def dauer_min(s: str) -> int:
    return {"PT15M": 15, "PT30M": 30, "PT60M": 60}[s]


def a73(tag: dt.date, ziel: Path) -> None:
    datei = ziel / f"{tag}.csv"
    if datei.exists():
        return
    p = {
        "documentType": "A73",
        "processType": "A16",
        "in_Domain": FR,
        "psrType": "B14",
        "periodStart": tag.strftime("%Y%m%d0000"),
        "periodEnd": (tag + dt.timedelta(1)).strftime("%Y%m%d0000"),
    }
    roh = hole(p)
    zeilen = ["mrid,name,stunde_utc,mw"]
    if roh:
        wurzel = ET.fromstring(roh)
        for ts in wurzel:
            if lokal(ts.tag) != "TimeSeries":
                continue
            psr = kind(ts, "MktPSRType", "PowerSystemResources")
            mrid = text(psr, "mRID") if psr is not None else ""
            name = (text(psr, "name") if psr is not None else "").replace(",", " ")
            for per in ts:
                if lokal(per.tag) != "Period":
                    continue
                t0 = dt.datetime.strptime(text(per, "timeInterval", "start"), "%Y-%m-%dT%H:%MZ")
                t1 = dt.datetime.strptime(text(per, "timeInterval", "end"), "%Y-%m-%dT%H:%MZ")
                schritt = dauer_min(text(per, "resolution"))
                anzahl = int((t1 - t0).total_seconds() // 60 // schritt)
                punkte = {
                    int(text(pt, "position")): float(text(pt, "quantity"))
                    for pt in per
                    if lokal(pt.tag) == "Point"
                }
                # Kurventyp A03: Punkte nur an Änderungen, dazwischen gilt der vorige Wert
                werte: list[float] = []
                letzter = float("nan")
                for pos in range(1, anzahl + 1):
                    letzter = punkte.get(pos, letzter)
                    werte.append(letzter)
                summe: dict[dt.datetime, list[float]] = {}
                for i, w in enumerate(werte):
                    h = (t0 + dt.timedelta(minutes=schritt * i)).replace(minute=0)
                    summe.setdefault(h, []).append(w)
                for h, w in sorted(summe.items()):
                    zeilen.append(f"{mrid},{name},{h:%Y-%m-%dT%H:00Z},{sum(w) / len(w):.1f}")
    datei.write_text("\n".join(zeilen) + "\n")


def a80(ziel: Path) -> None:
    monat = dt.date(START.year, START.month, 1)
    while monat < ENDE:
        naechster = (monat + dt.timedelta(32)).replace(day=1)
        for offset in range(0, 5000, 200):
            datei = ziel / f"{monat:%Y-%m}_{offset:04d}.zip"
            if datei.exists():
                continue
            p = {
                "documentType": "A80",
                "biddingZone_Domain": FR,
                "periodStart": monat.strftime("%Y%m%d0000"),
                "periodEnd": naechster.strftime("%Y%m%d0000"),
                "offset": str(offset),
            }
            roh = hole(p)
            if not roh or not roh.startswith(b"PK"):
                break
            datei.write_bytes(roh)
        monat = naechster


def main() -> None:
    ziel = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/kkw_entsoe")
    (ziel / "a73").mkdir(parents=True, exist_ok=True)
    (ziel / "a80").mkdir(parents=True, exist_ok=True)
    tag = START
    while tag < ENDE:
        a73(tag, ziel / "a73")
        tag += dt.timedelta(1)
    print("A73 fertig", flush=True)
    a80(ziel / "a80")
    print("A80 fertig", flush=True)


if __name__ == "__main__":
    main()
