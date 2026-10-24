"""Trading Hub Europe (THE), Marktgebietsverantwortlicher Gas: Tagespreise am VTP.

DataExport-API ohne Zugangsdaten (``api.tradinghub.eu/api/dataexport/xmlexport/``),
Bericht ``PricesEnergyImbalance``: je Gastag der gewichtete Durchschnittspreis am VTP
(Börsenhandel THE, ``VTPDailyAveragePrice``) und die Ausgleichsenergiepreise, ab 01.10.2021
(Start des Marktgebiets THE). Die API verlangt einen aussagekräftigen User-Agent.

Laut Disclaimer von THE braucht die Vervielfältigung der Daten eine schriftliche Zustimmung.
Die Quelle läuft deshalb nur mit ``PDB_ALLOW_THE=true``.

Ein Gastag beginnt um 06:00 Ortszeit; dort steht der Wert (als UTC). Positiver
Ausgleichsenergiepreis ist der Preis, den der Bilanzkreis für Unterdeckung zahlt
(englisch "negative imbalance price").
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from xml.etree import ElementTree as ET

from energy_ingest.http import Http
from energy_ingest.model import BERLIN, DAY, SeriesData, series
from energy_ingest.sources import FormatError, SourceError

BASE_URL = "https://api.tradinghub.eu/api/dataexport/xmlexport"
FIRST_DAY = date(2021, 10, 1)

# XML-Element -> Reihen-Code (positiver Preis = Unterdeckung = "Negative…" bei THE)
FIELDS = {
    "VTPDailyAveragePrice": "gas_the_vtp",
    "NegativeEnergyImbalancePrice": "gas_the_imbalance_pos",
    "PositiveEnergyImbalancePrice": "gas_the_imbalance_neg",
}


def gas_day_start(d: date) -> datetime:
    return datetime.combine(d, time(6), tzinfo=BERLIN).astimezone(UTC)


def parse_prices(body: bytes) -> list[SeriesData]:
    try:
        root = ET.fromstring(body)
    except ET.ParseError as exc:
        raise FormatError(f"THE: kein XML ({exc})") from exc
    if root.tag != "PricesEnergyImbalance":
        raise FormatError(f"THE: unerwartetes Wurzelelement {root.tag!r}")
    points: dict[str, list[tuple[datetime, float]]] = {code: [] for code in FIELDS.values()}
    for item in root:
        day = item.findtext("Gasday")
        unit = item.findtext("Unit")
        if day is None or unit != "EUR/MWh":
            raise FormatError(f"THE: Gastag {day!r} oder Einheit {unit!r} unerwartet")
        missing = [f for f in FIELDS if item.find(f) is None]
        if missing:
            raise FormatError(f"THE: Felder fehlen {missing}")
        ts = gas_day_start(date.fromisoformat(day[:10]))
        for field, code in FIELDS.items():
            text = item.findtext(field)
            if text:
                points[code].append((ts, float(text)))
    return [SeriesData(series(c), DAY, sorted(pts)) for c, pts in points.items() if pts]


class The:
    code = "the"
    license = "Trading Hub Europe, Vervielfältigung nur mit schriftlicher Zustimmung"
    priority = 10
    free = False

    def __init__(self, http: Http, base_url: str = BASE_URL) -> None:
        self.http = http
        self.base_url = base_url.rstrip("/")

    def datasets(self) -> list[str]:
        return ["gas_prices"]

    def max_chunk(self, dataset: str) -> timedelta:
        return timedelta(days=366)

    def fetch(self, dataset: str, start: datetime, end: datetime) -> list[SeriesData]:
        if dataset != "gas_prices":
            raise KeyError(dataset)
        first = max(start.astimezone(BERLIN).date() - DAY, FIRST_DAY)
        last = end.astimezone(BERLIN).date()
        if first > last:
            return []
        params = {"startDate": first.isoformat(), "endDate": last.isoformat()}
        r = self.http.get(f"{self.base_url}/PricesEnergyImbalance", params)
        if r.status_code >= 400:
            raise SourceError(f"THE: {r.status_code} {r.text[:200]}")
        out = []
        for data in parse_prices(r.content):
            pts = [(ts, v) for ts, v in data.points if start <= ts < end]
            if pts:
                out.append(SeriesData(data.info, data.resolution, pts))
        return out
