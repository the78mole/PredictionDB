"""Netztransparenz.de, Daten-API der vier ÜNB (OAuth2 Client Credentials).

Zugang: Konto im API-Portal (api-portal.netztransparenz.de), dort einen Client anlegen;
Client-ID und -Secret in ``PDB_NETZTRANSPARENZ_CLIENT_ID`` und
``PDB_NETZTRANSPARENZ_CLIENT_SECRET``. Ohne beide bleibt die Quelle abgeschaltet. Eine Lizenz
für die Daten ist nicht veröffentlicht; die Quelle läuft deshalb nur mit
``PDB_ALLOW_NETZTRANSPARENZ=true``, sonst rechnet ``pdb_marktwert`` die Marktwerte selbst.

Genutzte Endpunkte (Antwort CSV, Semikolon, Dezimalkomma, Werte in ct/kWh)::

    /marktpraemie/{MM}/{JJJJ}/{MM}/{JJJJ}  -> Monat;MW-EPEX;MW Wind Onshore;…;MW Solar;…
                                              (Monat als ``1/2025``, Daten ab 1/2012)
    /Jahresmarktpraemie                    -> Zeilen JW, JW Wind an Land, JW Wind auf See,
                                              JW Solar; je Jahr eine Spalte (ab 2013)

Monatswerte stehen am Monatsbeginn, Jahreswerte am Jahresbeginn (jeweils 00:00 Ortszeit,
als UTC). Umgerechnet in EUR/MWh.
"""

from __future__ import annotations

import csv
import io
import time
from datetime import UTC, datetime, timedelta
from typing import ClassVar

import httpx

from energy_ingest.http import Http
from energy_ingest.model import BERLIN, MONTH, YEAR, SeriesData, next_period, series
from energy_ingest.sources import SourceError, clip

BASE_URL = "https://ds.netztransparenz.de/api/v1/data"
TOKEN_URL = "https://identity.netztransparenz.de/users/connect/token"

# Ältester Monat, den /marktpraemie liefert
FIRST_MONTH = (2012, 1)

# Spalten von /marktpraemie -> Reihen-Code
MONTH_COLUMNS: dict[str, str] = {
    "MW-EPEX in ct/kWh": "mw_base",
    "MW Wind Onshore in ct/kWh": "mw_wind_onshore",
    "MW Wind Offshore in ct/kWh": "mw_wind_offshore",
    "MW Solar in ct/kWh": "mw_solar",
}

# Zeilen von /Jahresmarktpraemie -> Reihen-Code
YEAR_ROWS: dict[str, str] = {
    "JW": "jw_base",
    "JW Wind an Land": "jw_wind_onshore",
    "JW Wind auf See": "jw_wind_offshore",
    "JW Solar": "jw_solar",
}

CT_KWH_TO_EUR_MWH = 10.0


def _number(text: str) -> float | None:
    text = text.strip()
    return float(text.replace(".", "").replace(",", ".")) * CT_KWH_TO_EUR_MWH if text else None


def _month_start(year: int, month: int) -> datetime:
    return datetime(year, month, 1, tzinfo=BERLIN).astimezone(UTC)


def _rows(body: str) -> list[list[str]]:
    return [r for r in csv.reader(io.StringIO(body.lstrip("﻿")), delimiter=";") if r]


def parse_months(body: str) -> list[SeriesData]:
    rows = _rows(body)
    if not rows or rows[0][0].strip() != "Monat":
        raise SourceError(f"Netztransparenz: unerwartete Monatsmarktwerte {body[:80]!r}")
    header = [h.strip() for h in rows[0]]
    cols = {code: header.index(name) for name, code in MONTH_COLUMNS.items() if name in header}
    points: dict[str, list[tuple[datetime, float]]] = {code: [] for code in cols}
    for row in rows[1:]:
        month, year = (int(x) for x in row[0].strip().split("/"))
        ts = _month_start(year, month)
        for code, i in cols.items():
            value = _number(row[i]) if i < len(row) else None
            if value is not None:
                points[code].append((ts, value))
    return [SeriesData(series(code), MONTH, pts) for code, pts in points.items() if pts]


def parse_years(body: str) -> list[SeriesData]:
    rows = _rows(body)
    if not rows or not rows[0][0].startswith("Alle Werte"):
        raise SourceError(f"Netztransparenz: unerwartete Jahresmarktwerte {body[:80]!r}")
    stamps = [_month_start(int(y), 1) for y in rows[0][1:]]
    out = []
    for row in rows[1:]:
        code = YEAR_ROWS.get(row[0].strip())
        if code is None:
            continue
        pts = [
            (ts, v)
            for ts, cell in zip(stamps, row[1:], strict=False)
            if (v := _number(cell)) is not None
        ]
        if pts:
            out.append(SeriesData(series(code), YEAR, pts))
    return out


class Netztransparenz:
    code = "netztransparenz"
    license = "Netztransparenz.de (50Hertz, Amprion, TenneT, TransnetBW), Nutzungsbedingungen"
    priority = 15
    free = False

    # Werte erscheinen erst nach Periodenende; der laufende Abgleich schaut entsprechend
    # weit zurück, damit nachgereichte Monate und Jahre ankommen
    sync_lookback: ClassVar[dict[str, timedelta]] = {
        "market_value_month": timedelta(days=95),
        "market_value_year": timedelta(days=400),
    }

    def __init__(
        self,
        http: Http,
        client_id: str,
        client_secret: str,
        base_url: str = BASE_URL,
        token_url: str = TOKEN_URL,
    ) -> None:
        if not (client_id and client_secret):
            raise ValueError(
                "Netztransparenz-Zugang fehlt (PDB_NETZTRANSPARENZ_CLIENT_ID/_CLIENT_SECRET)"
            )
        self.http = http
        self.client_id = client_id
        self.client_secret = client_secret
        self.base_url = base_url.rstrip("/")
        self.token_url = token_url
        self._token: str | None = None
        self._token_until = 0.0

    def datasets(self) -> list[str]:
        return ["market_value_month", "market_value_year"]

    def max_chunk(self, dataset: str) -> timedelta:
        return timedelta(days=366)

    def _access_token(self) -> str:
        if self._token is None or time.monotonic() >= self._token_until:
            r = self.http.client.post(
                self.token_url,
                data={
                    "grant_type": "client_credentials",
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                },
            )
            if r.status_code != httpx.codes.OK:
                raise SourceError(f"Netztransparenz: Token-Abruf {r.status_code} {r.text[:200]}")
            data = r.json()
            self._token = str(data["access_token"])
            self._token_until = time.monotonic() + float(data.get("expires_in", 3600)) - 60
        return self._token

    def _get(self, path: str) -> str:
        for attempt in range(2):
            headers = {"Authorization": f"Bearer {self._access_token()}"}
            r = self.http.get(f"{self.base_url}/{path}", headers=headers)
            if r.status_code == httpx.codes.UNAUTHORIZED and attempt == 0:
                self._token = None
                continue
            if r.status_code >= 400:
                raise SourceError(f"Netztransparenz: {path} {r.status_code} {r.text[:200]}")
            return r.text
        raise SourceError(f"Netztransparenz: {path} nicht autorisiert")

    def fetch(self, dataset: str, start: datetime, end: datetime) -> list[SeriesData]:
        if dataset == "market_value_month":
            first = start.astimezone(BERLIN)
            last = (end - timedelta(microseconds=1)).astimezone(BERLIN)
            a = max((first.year, first.month), FIRST_MONTH)
            b = (last.year, last.month)
            if a > b:
                return []
            batch = parse_months(self._get(f"marktpraemie/{a[1]:02d}/{a[0]}/{b[1]:02d}/{b[0]}"))
        elif dataset == "market_value_year":
            # Der Endpunkt liefert ohne Parameter alle Jahre auf einmal
            if next_period(start, 12) <= _month_start(FIRST_MONTH[0] + 1, 1):
                return []
            batch = parse_years(self._get("Jahresmarktpraemie"))
        else:
            raise KeyError(dataset)
        out = []
        for data in batch:
            pts = clip(data.points, start, end)
            if pts:
                out.append(SeriesData(data.info, data.resolution, pts))
        return out
