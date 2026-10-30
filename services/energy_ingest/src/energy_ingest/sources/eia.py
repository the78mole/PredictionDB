"""U.S. Energy Information Administration (EIA): Spotpreise Öl, Ölprodukte, Henry Hub.

Zwei Quellen mit derselben Technik, getrennt nach Nutzungsrecht:

- ``eia`` (frei): Reihen, die die EIA selbst erhebt (``copyright: None``), Public Domain
  nach https://www.eia.gov/about/copyrights_reuse.php. Das sind die ULSD-Notierungen
  (Diesel/Gasöl, Proxy für ARA-Gasöl).
- ``eia_reuters`` (unfrei): Reihen, die die EIA von Thomson Reuters bezieht (Brent, WTI,
  Benzin, Heizöl, Kerosin, Propan, Henry Hub). Sie sind urheberrechtlich geschützt und
  laufen nur mit ``PDB_ALLOW_EIA_REUTERS=true``.

Abruf über die API v2 mit kostenlosem Schlüssel (``PDB_EIA_API_KEY``, Registrierung unter
https://www.eia.gov/opendata/register.php). Ohne Schlüssel lädt die Quelle die
Bulk-Dateien ``PET.zip`` und ``NG.zip`` (zusammen rund 60 MB, täglich aktualisiert) und hält
sie einige Stunden im Speicher.

Tageswerte stehen am Beginn des Handelstags (00:00 Ortszeit Berlin, als UTC), in der
Notierungseinheit der EIA (USD/bbl, USD/gal, USD/MMBtu).

@relation(SWA-ENI-SRC-EIA, scope=file)
"""

from __future__ import annotations

import io
import json
import zipfile
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from functools import partial
from typing import Any, ClassVar

from energy_ingest.http import Http
from energy_ingest.model import DAY, SeriesData, series
from energy_ingest.sources import Cached, FormatError, SourceError, clip, day_start

API_URL = "https://api.eia.gov/v2"
BULK_URL = "https://api.eia.gov/bulk"
PAGE = 5000
BULK_TTL = timedelta(hours=6)


@dataclass(frozen=True)
class EiaSeries:
    route: str  # API-v2-Route, z. B. petroleum/pri/spt
    bulk: str  # Bulk-Datei ohne .zip
    series_id: str
    unit: str  # erwartete Einheit, klein geschrieben ohne Leerzeichen


# Reihen-Code -> EIA-Reihe
FREE_SERIES: dict[str, EiaSeries] = {
    "oil_ulsd_nyh": EiaSeries("petroleum/pri/spt", "PET", "EER_EPD2DXL0_PF4_Y35NY_DPG", "$/gal"),
    "oil_ulsd_usgc": EiaSeries("petroleum/pri/spt", "PET", "EER_EPD2DXL0_PF4_RGC_DPG", "$/gal"),
}
REUTERS_SERIES: dict[str, EiaSeries] = {
    "oil_brent": EiaSeries("petroleum/pri/spt", "PET", "RBRTE", "$/bbl"),
    "oil_wti": EiaSeries("petroleum/pri/spt", "PET", "RWTC", "$/bbl"),
    "oil_gasoline_nyh": EiaSeries("petroleum/pri/spt", "PET", "EER_EPMRU_PF4_Y35NY_DPG", "$/gal"),
    "oil_heating_oil_nyh": EiaSeries(
        "petroleum/pri/spt", "PET", "EER_EPD2F_PF4_Y35NY_DPG", "$/gal"
    ),
    "oil_jet_usgc": EiaSeries("petroleum/pri/spt", "PET", "EER_EPJK_PF4_RGC_DPG", "$/gal"),
    "lpg_propane_mb": EiaSeries("petroleum/pri/spt", "PET", "EER_EPLLPA_PF4_Y44MB_DPG", "$/gal"),
    "gas_henry_hub": EiaSeries("natural-gas/pri/fut", "NG", "RNGWHHD", "$/mmbtu"),
}


def _unit(text: str) -> str:
    return text.replace(" ", "").replace("\\/", "/").lower()


def _date(text: str) -> date:
    text = text.replace("-", "")
    return date(int(text[:4]), int(text[4:6]), int(text[6:8]))


def parse_bulk(raw: bytes, wanted: dict[str, EiaSeries]) -> dict[str, list[tuple[datetime, float]]]:
    """Gesuchte Tagesreihen aus einer Bulk-Datei (ZIP mit einer JSON-Zeile je Reihe).

    @relation(SWR-ENI-045, SWR-ENI-030, scope=function)
    """
    try:
        zf = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile as exc:
        raise FormatError(f"EIA-Bulk: kein ZIP ({exc})") from exc
    keys = {f'"series_id":"{s.bulk}.{s.series_id}.D"': code for code, s in wanted.items()}
    out: dict[str, list[tuple[datetime, float]]] = {}
    with zf, zf.open(zf.namelist()[0]) as fh:
        for line in io.TextIOWrapper(fh, encoding="utf-8"):
            code = next((c for k, c in keys.items() if k in line), None)
            if code is None:
                continue
            rec = json.loads(line)
            unit = _unit(str(rec.get("unitsshort", "")))
            if unit != wanted[code].unit:
                raise FormatError(f"EIA-Bulk {rec['series_id']}: Einheit {unit!r}")
            out[code] = sorted(
                (day_start(_date(str(d))), float(v)) for d, v in rec["data"] if v is not None
            )
    return out


def parse_api(body: Any, s: EiaSeries) -> tuple[int, list[tuple[datetime, float]]]:
    """Eine Seite der API v2; gibt die Gesamtzahl der Zeilen und die Punkte zurück.

    @relation(SWR-ENI-045, SWR-ENI-030, scope=function)
    """
    if not isinstance(body, dict) or "response" not in body:
        raise FormatError(f"EIA-API: unerwartete Antwort {str(body)[:200]!r}")
    resp = body["response"]
    pts = []
    for row in resp.get("data", []):
        if row.get("series") != s.series_id:
            raise FormatError(f"EIA-API: fremde Reihe {row.get('series')!r}")
        if _unit(str(row.get("units", ""))) != s.unit:
            raise FormatError(f"EIA-API {s.series_id}: Einheit {row.get('units')!r}")
        if row.get("value") not in (None, ""):
            pts.append((day_start(_date(str(row["period"]))), float(row["value"])))
    return int(resp.get("total", len(pts))), pts


class EiaBulk:
    """Bulk-Dateien, gemeinsam für ``eia`` und ``eia_reuters``."""

    def __init__(self, http: Http, base_url: str = BULK_URL) -> None:
        self.http = http
        self.base_url = base_url.rstrip("/")
        self._files: dict[str, Cached[bytes]] = {}

    def _load(self, name: str) -> bytes:
        r = self.http.get(f"{self.base_url}/{name}.zip")
        if r.status_code >= 400:
            raise SourceError(f"EIA-Bulk {name}: {r.status_code}")
        return r.content

    def series(self, wanted: dict[str, EiaSeries]) -> dict[str, list[tuple[datetime, float]]]:
        out: dict[str, list[tuple[datetime, float]]] = {}
        for name in sorted({s.bulk for s in wanted.values()}):
            if name not in self._files:
                self._files[name] = Cached(partial(self._load, name), BULK_TTL)
            part = {c: s for c, s in wanted.items() if s.bulk == name}
            out.update(parse_bulk(self._files[name].get(), part))
        return out


class Eia:
    """@relation(SWR-ENI-045, SWR-ENI-004, scope=class)"""

    code = "eia"
    license = "Public Domain, U.S. Energy Information Administration (EIA)"
    priority = 10
    free = True
    series_map: ClassVar[dict[str, EiaSeries]] = FREE_SERIES
    # Die EIA veröffentlicht mit zwei bis drei Werktagen Verzug
    sync_lookback: ClassVar[dict[str, timedelta]] = {"prices": timedelta(days=10)}

    def __init__(self, http: Http, api_key: str = "", bulk: EiaBulk | None = None) -> None:
        self.http = http
        self.api_key = api_key
        self.bulk = bulk or EiaBulk(http)

    def datasets(self) -> list[str]:
        return ["prices"]

    def max_chunk(self, dataset: str) -> timedelta:
        return timedelta(days=366)

    def _api(self, s: EiaSeries, start: datetime, end: datetime) -> list[tuple[datetime, float]]:
        pts: list[tuple[datetime, float]] = []
        offset = 0
        while True:
            params = {
                "api_key": self.api_key,
                "frequency": "daily",
                "data[0]": "value",
                "facets[series][]": s.series_id,
                "start": (start - DAY).date().isoformat(),
                "end": (end + DAY).date().isoformat(),
                "sort[0][column]": "period",
                "sort[0][direction]": "asc",
                "offset": offset,
                "length": PAGE,
            }
            r = self.http.get(f"{API_URL}/{s.route}/data/", params)
            if r.status_code >= 400:
                raise SourceError(f"EIA-API {s.series_id}: {r.status_code} {r.text[:200]}")
            total, page = parse_api(r.json(), s)
            pts.extend(page)
            offset += PAGE
            if not page or offset >= total:
                return pts

    def fetch(self, dataset: str, start: datetime, end: datetime) -> list[SeriesData]:
        if dataset != "prices":
            raise KeyError(dataset)
        if self.api_key:
            found = {code: self._api(s, start, end) for code, s in self.series_map.items()}
        else:
            found = self.bulk.series(self.series_map)
        missing = set(self.series_map) - set(found)
        if missing:
            raise FormatError(f"EIA: Reihen fehlen {sorted(missing)}")
        out = []
        for code, pts in found.items():
            pts = clip(pts, start, end)
            if pts:
                out.append(SeriesData(series(code), DAY, pts))
        return out


class EiaReuters(Eia):
    """@relation(SWR-ENI-045, SWR-ENI-003, scope=class)"""

    code = "eia_reuters"
    license = "Thomson Reuters über EIA, urheberrechtlich geschützt (nur mit Freigabe)"
    priority = 10
    free = False
    series_map: ClassVar[dict[str, EiaSeries]] = REUTERS_SERIES
