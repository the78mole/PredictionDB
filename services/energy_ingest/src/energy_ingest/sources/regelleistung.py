"""regelleistung.net (Plattform der deutschen ÜNB): Ergebnisse der Regelleistungsauktionen.

Datencenter-Download ohne Zugangsdaten, je Liefertag und Produkt eine xlsx-Datei::

    /apps/cpp-publisher/api/v1/download/tenders/resultsoverview
        ?date=JJJJ-MM-TT&exportFormat=xlsx&market=CAPACITY&productTypes=FCR|aFRR|mFRR

Geladen wird der Leistungsmarkt für Deutschland: FCR (Primärregelleistung) mit
Leistungspreis und deutschem Bedarf, aFRR und mFRR je Richtung mit mittlerem und
Grenz-Leistungspreis, angebotener und (aFRR) zugeschlagener Leistung. Die Produkte sind
Zeitscheiben in Ortszeit (``POS_00_04``, bei FCR 07/2019 bis 06/2020 ``NEGPOS_00_24``); jeder Wert
wird auf die Stunden seiner Zeitscheibe verteilt. Leistungspreise stehen einheitlich in
EUR je MW und Stunde: ältere Dateien nennen sie je MW und Zeitscheibe, sie werden durch die
Zahl der Stunden der Zeitscheibe geteilt. aFRR und mFRR reichen bis 07/2018 zurück, FCR bis
07/2019 (davor Wochenprodukte).

Die Auktionen laufen am Vortag vor der Day-Ahead-Auktion (FCR und aFRR seit 07/2020 bzw.
11/2020 täglich, davor wöchentlich); die Ergebnisse gelten deshalb ab 12:00 Ortszeit am
Vortag als bekannt (``Kind.RESERVE_CAPACITY``).

Lizenz: Laut Impressum braucht die Vervielfältigung der Inhalte die schriftliche Zustimmung
der ÜNB, „soweit die Vervielfältigung nicht ohnehin gesetzlich gestattet ist“. Das Training
ist Text- und Data-Mining (§ 44b UrhG); die Quelle hat deshalb die Klasse „nur Training“
(``free = False``, ``train = True``, docs/ARCHITEKTUR.md 7.11.1): ein Modell darf daraus lernen,
die Reihen selbst werden nicht weitergegeben. Sie läuft nur mit ``PDB_ALLOW_REGELLEISTUNG=true``.

@relation(SWA-ENI-SRC-RL, scope=file)
"""

from __future__ import annotations

import io
import re
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

from openpyxl import load_workbook

from energy_ingest.http import Http
from energy_ingest.model import BERLIN, HOUR, SeriesData, series
from energy_ingest.sources import FormatError, SourceError

BASE_URL = "https://www.regelleistung.net/apps/cpp-publisher/api/v1/download/tenders"
#: Erster Liefertag je Datensatz: aFRR und mFRR mit 4-h-Zeitscheiben seit 12.07.2018, FCR als
#: Tagesprodukt seit 01.07.2019 (davor Wochenprodukte, nicht geladen)
FIRST_DAY = {
    "fcr_capacity": date(2019, 7, 1),
    "afrr_capacity": date(2018, 7, 12),
    "mfrr_capacity": date(2018, 7, 12),
}

#: Produkt-Spalte je Datei (FCR heißt sie anders)
PRODUCT_COLUMNS = ("PRODUCT", "PRODUCTNAME")
PRODUCT_RE = re.compile(r"^(NEG|POS|NEGPOS)_(\d{2})_(\d{2})$")
#: Deutschland heißt in den Spalten je nach Jahrgang ``GERMANY`` oder ``DE``
COUNTRY_PREFIXES = ("GERMANY_", "DE_")


@dataclass(frozen=True)
class Column:
    """Spalte (ohne Länderpräfix und Einheit) -> Reihe; ``price`` = Leistungspreis."""

    name: str
    code: str  # mit ``{dir}`` für die Richtung (pos/neg)
    price: bool = False


#: Datensatz -> (productTypes, Spalten)
DATASETS: dict[str, tuple[str, list[Column]]] = {
    "fcr_capacity": (
        "FCR",
        [
            Column("SETTLEMENTCAPACITY_PRICE", "rl_fcr_price", price=True),
            Column("DEMAND", "rl_fcr_demand"),
        ],
    ),
    "afrr_capacity": (
        "aFRR",
        [
            Column("AVERAGE_CAPACITY_PRICE", "rl_afrr_{dir}_price_avg", price=True),
            Column("MARGINAL_CAPACITY_PRICE", "rl_afrr_{dir}_price_marginal", price=True),
            Column("ALLOCATED_VOLUME", "rl_afrr_{dir}_volume"),
            Column("SUM_OF_OFFERED_CAPACITY", "rl_afrr_{dir}_offered"),
        ],
    ),
    "mfrr_capacity": (
        "mFRR",
        [
            Column("AVERAGE_CAPACITY_PRICE", "rl_mfrr_{dir}_price_avg", price=True),
            Column("MARGINAL_CAPACITY_PRICE", "rl_mfrr_{dir}_price_marginal", price=True),
            Column("SUM_OF_OFFERED_CAPACITY", "rl_mfrr_{dir}_offered"),
        ],
    ),
}


def _header(cell: Any) -> tuple[str, str] | None:
    """``GERMANY_AVERAGE_CAPACITY_PRICE_[(EUR/MW)/h]`` -> (``AVERAGE_CAPACITY_PRICE``, Einheit)."""
    if not isinstance(cell, str):
        return None
    for prefix in COUNTRY_PREFIXES:
        if cell.startswith(prefix):
            name, _, unit = cell[len(prefix) :].partition("_[")
            return name, unit.rstrip("]")
    return None


def _value(cell: Any) -> float | None:
    if isinstance(cell, int | float) and not isinstance(cell, bool):
        return float(cell)
    if isinstance(cell, str):
        try:
            return float(cell.replace(",", "."))
        except ValueError:
            return None  # "-" = kein Wert
    return None


def _day(cell: Any) -> date:
    if isinstance(cell, datetime):
        return cell.date()
    if isinstance(cell, date):
        return cell
    if isinstance(cell, str):
        return date.fromisoformat(cell[:10])
    raise FormatError(f"regelleistung.net: Datum {cell!r} unlesbar")


def hours(day: date, first: int, last: int) -> Iterator[datetime]:
    """Stundenbeginne (UTC) der Zeitscheibe ``first``-``last`` Uhr Ortszeit am ``day``."""
    a = datetime.combine(day, time(first), tzinfo=BERLIN).astimezone(UTC)
    end_day = day + timedelta(days=1) if last == 24 else day
    b = datetime.combine(end_day, time(last % 24), tzinfo=BERLIN).astimezone(UTC)
    ts = a
    while ts < b:
        yield ts
        ts += HOUR


def parse_capacity(raw: bytes, dataset: str) -> list[SeriesData]:
    """Eine Ergebnisdatei des Leistungsmarkts -> Stundenwerte je Reihe.

    @relation(SWR-ENI-054, SWR-ENI-030, scope=function)
    """
    _, columns = DATASETS[dataset]
    try:
        wb = load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
    except Exception as exc:
        raise FormatError(f"regelleistung.net: keine lesbare xlsx-Datei ({exc})") from exc
    ws = wb.worksheets[0]
    ws.reset_dimensions()  # viele Dateien geben fälschlich A1:A1 als Ausdehnung an
    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        return []
    head = list(rows[0])
    try:
        i_day, i_to = head.index("DATE_FROM"), head.index("DATE_TO")
        i_prod = next(head.index(c) for c in PRODUCT_COLUMNS if c in head)
    except (ValueError, StopIteration) as exc:
        raise FormatError(f"regelleistung.net: Kopfzeile unerwartet {head[:6]}") from exc
    found: dict[str, tuple[int, str]] = {}
    for i, cell in enumerate(head):
        h = _header(cell)
        if h and h[0] not in found:
            found[h[0]] = (i, h[1])
    wanted = [c for c in columns if c.name in found]
    if not any(c.price for c in wanted):
        raise FormatError(f"regelleistung.net: keine Preisspalte für DE in {dataset}")
    points: dict[str, dict[datetime, float]] = {}
    for raw_row in rows[1:]:
        # ältere Dateien lassen leere Zellen am Zeilenende weg
        row = tuple(raw_row) + (None,) * (len(head) - len(raw_row))
        if row[i_prod] is None:
            continue
        m = PRODUCT_RE.match(str(row[i_prod]).strip())
        if not m:
            raise FormatError(f"regelleistung.net: Produkt {row[i_prod]!r} unbekannt")
        direction, first, last = m.group(1), int(m.group(2)), int(m.group(3))
        if not 0 <= first < last <= 24:
            raise FormatError(f"regelleistung.net: Zeitscheibe {row[i_prod]!r} ungültig")
        day = _day(row[i_day])
        if _day(row[i_to]) != day:
            raise FormatError(f"regelleistung.net: Produkt über mehrere Tage {row[i_prod]!r}")
        slots = list(hours(day, first, last))
        for col in wanted:
            i, unit = found[col.name]
            v = _value(row[i])
            if v is None:
                continue
            if col.price:
                if unit == "EUR/MW":  # je Zeitscheibe -> je Stunde (Zeitumstellung: 3 bzw. 5 h)
                    v /= len(slots)
                elif unit != "(EUR/MW)/h":
                    raise FormatError(f"regelleistung.net: Einheit {unit!r} für {col.name}")
            elif unit != "MW":
                raise FormatError(f"regelleistung.net: Einheit {unit!r} für {col.name}")
            code = col.code.format(dir=direction.lower())
            for ts in slots:
                points.setdefault(code, {})[ts] = v
    return [SeriesData(series(c), HOUR, sorted(p.items())) for c, p in points.items()]


class Regelleistung:
    """@relation(SWR-ENI-054, scope=class)"""

    code = "regelleistung"
    license = "regelleistung.net (deutsche ÜNB), Weitergabe nur mit Zustimmung; nur Training"
    priority = 10
    free = False
    #: Weitergabe nur mit Zustimmung, Training erlaubt (§ 44b UrhG, ARCHITEKTUR.md 7.11.1)
    train = True

    def __init__(self, http: Http, base_url: str = BASE_URL) -> None:
        self.http = http
        self.base_url = base_url.rstrip("/")

    def datasets(self) -> list[str]:
        return list(DATASETS)

    def max_chunk(self, dataset: str) -> timedelta:
        return timedelta(days=31)  # eine Datei je Liefertag

    def fetch(self, dataset: str, start: datetime, end: datetime) -> list[SeriesData]:
        product, _ = DATASETS[dataset]
        first = max(start.astimezone(BERLIN).date(), FIRST_DAY[dataset])
        last = (end - timedelta(microseconds=1)).astimezone(BERLIN).date()
        merged: dict[str, SeriesData] = {}
        day = first
        while day <= last:
            params = {
                "date": day.isoformat(),
                "exportFormat": "xlsx",
                "market": "CAPACITY",
                "productTypes": product,
            }
            r = self.http.get(f"{self.base_url}/resultsoverview", params)
            if r.status_code >= 400:
                raise SourceError(f"regelleistung.net {dataset} {day}: {r.status_code}")
            for data in parse_capacity(r.content, dataset):
                pts = [(ts, v) for ts, v in data.points if start <= ts < end]
                if pts:
                    merged.setdefault(data.code, SeriesData(data.info, HOUR)).points.extend(pts)
            day += timedelta(days=1)
        return list(merged.values())
