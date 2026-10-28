"""Einmaliger Abruf ID-AEP und reBAP von Netztransparenz (nur für die Analyse, nicht in die DB).

Läuft im energy-ingest-Container (Zugangsdaten aus .env), schreibt CSV nach stdout:
endpoint;Datum;Zeitzone;von;bis;...Originalspalten...
"""
import os
import sys
import time
from datetime import date

import httpx

cid = os.environ["PDB_NETZTRANSPARENZ_CLIENT_ID"]
sec = os.environ["PDB_NETZTRANSPARENZ_CLIENT_SECRET"]
c = httpx.Client(timeout=120)
tok = c.post(
    "https://identity.netztransparenz.de/users/connect/token",
    data={"grant_type": "client_credentials", "client_id": cid, "client_secret": sec},
).json()["access_token"]
H = {"Authorization": f"Bearer {tok}"}
BASE = "https://ds.netztransparenz.de/api/v1/data"
EP = {"idaep": "IdAep", "rebap": "NrvSaldo/reBAP/Qualitaetsgesichert"}
start_year = int(sys.argv[1]) if len(sys.argv) > 1 else 2020
today = date.today()
for name, path in EP.items():
    header_done = False
    y, m = start_year, 1
    while (y, m) <= (today.year, today.month):
        ny, nm = (y + 1, 1) if m == 12 else (y, m + 1)
        url = f"{BASE}/{path}/{y:04d}-{m:02d}-01T00:00:00/{ny:04d}-{nm:02d}-01T00:00:00"
        for attempt in range(3):
            r = c.get(url, headers=H)
            if r.status_code < 500:
                break
            time.sleep(5)
        if r.status_code != 200:
            print(f"{name} {y}-{m:02d}: HTTP {r.status_code} {r.text[:120]}", file=sys.stderr)
        else:
            lines = [ln for ln in r.text.lstrip("﻿").splitlines() if ln.strip()]
            if lines:
                if not header_done:
                    print(f"endpoint;{lines[0]}")
                    header_done = True
                for ln in lines[1:]:
                    print(f"{name};{ln}")
            print(f"{name} {y}-{m:02d}: {max(len(lines) - 1, 0)} Zeilen", file=sys.stderr)
        y, m = ny, nm
        time.sleep(0.5)
