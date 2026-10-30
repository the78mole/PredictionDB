"""ENTSO-E Transparency Platform, REST-API mit kostenlosem Token.

Den Token gibt es nach Registrierung auf transparency.entsoe.eu und einer E-Mail an
transparency@entsoe.eu mit dem Betreff „Restful API access“. Ohne ``PDB_ENTSOE_TOKEN``
bleibt die Quelle abgeschaltet.

Frei weiterverwendbar (CC BY 4.0) ist nur die „List of Data available for free re-use“. Von den
genutzten Reihen steht nur die Day-Ahead-Lastprognose (6.1.b) darauf; sie läuft für Deutschland
und die Nachbarzonen als eigene, freie Quelle ``entsoe_last`` ohne Schalter, sobald ein Token
gesetzt ist. Ist-Last, Preise, Erzeugung
und Wind-/PV-Prognosen (Quelle ``entsoe``) laufen nur mit ``PDB_ALLOW_ENTSO_E=true``.

Antworten sind XML-Dokumente (``Publication_MarketDocument`` bzw. ``GL_MarketDocument``)
mit ``TimeSeries/Period/Point``. Bei Kurventyp A03 fehlen Punkte, deren Wert sich nicht
ändert; sie werden mit dem vorigen Wert aufgefüllt. Liegen keine Daten vor, kommt ein
``Acknowledgement_MarketDocument`` mit Grund 999.

@relation(SWA-ENI-SRC-ENTSOE, scope=file)
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

from energy_ingest.http import Http
from energy_ingest.model import LOAD_FORECAST_ZONES, SERIES, SeriesData, series
from energy_ingest.sources import SourceError, clip

BASE_URL = "https://web-api.tp.entsoe.eu/api"

EIC_DE_LU = "10Y1001A1001A82H"  # Gebotszone DE-LU (seit 2018-10-01)
EIC_DE = "10Y1001A1001A83F"  # Deutschland (Land), vergleichbar mit SMARD und Energy-Charts

#: Gebotszonen der Nachbarn (Kürzel aus ``LOAD_FORECAST_ZONES``) -> EIC
EIC_ZONES: dict[str, str] = {
    "at": "10YAT-APG------L",
    "be": "10YBE----------2",
    "ch": "10YCH-SWISSGRIDZ",
    "cz": "10YCZ-CEPS-----N",
    "dk1": "10YDK-1--------W",
    "dk2": "10YDK-2--------M",
    "fr": "10YFR-RTE------C",
    "nl": "10YNL----------L",
    "no2": "10YNO-2--------T",
    "pl": "10YPL-AREA-----S",
    "se4": "10Y1001A1001A47J",
}
assert set(EIC_ZONES) == set(LOAD_FORECAST_ZONES)

PSR_TYPES: dict[str, str] = {
    "B01": "biomass",
    "B02": "lignite",
    "B03": "coal_gas",
    "B04": "gas",
    "B05": "hard_coal",
    "B06": "oil",
    "B09": "geothermal",
    "B10": "hydro_pumped",
    "B11": "hydro_ror",
    "B12": "hydro_reservoir",
    "B14": "nuclear",
    "B15": "other_renewable",
    "B16": "solar",
    "B17": "waste",
    "B18": "wind_offshore",
    "B19": "wind_onshore",
    "B20": "other",
    "B25": "storage",
}

# Datensatz -> feste Abfrageparameter
QUERIES: dict[str, dict[str, str]] = {
    "price": {
        "documentType": "A44",
        "in_Domain": EIC_DE_LU,
        "out_Domain": EIC_DE_LU,
        "contract_MarketAgreement.type": "A01",
    },
    "load": {"documentType": "A65", "processType": "A16", "outBiddingZone_Domain": EIC_DE},
    "load_forecast": {
        "documentType": "A65",
        "processType": "A01",
        "outBiddingZone_Domain": EIC_DE,
    },
    # Day-Ahead-Lastprognosen der Nachbarzonen, Datensätze ``load_forecast_<kürzel>``
    **{
        f"load_forecast_{z}": {
            "documentType": "A65",
            "processType": "A01",
            "outBiddingZone_Domain": EIC_ZONES[z],
        }
        for z in LOAD_FORECAST_ZONES
    },
    "generation": {"documentType": "A75", "processType": "A16", "in_Domain": EIC_DE},
    "wind_solar_forecast": {"documentType": "A69", "processType": "A01", "in_Domain": EIC_DE},
}

_DURATION = re.compile(r"^P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?)?$")


def parse_duration(text: str) -> timedelta:
    m = _DURATION.match(text.strip())
    if not m or not any(m.groups()):
        raise SourceError(f"ENTSO-E: Auflösung {text!r} nicht unterstützt")
    d, h, mi = (int(g) if g else 0 for g in m.groups())
    return timedelta(days=d, hours=h, minutes=mi)


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _child(el: ET.Element, *path: str) -> ET.Element | None:
    cur: ET.Element | None = el
    for name in path:
        if cur is None:
            return None
        cur = next((c for c in cur if _local(c.tag) == name), None)
    return cur


def _text(el: ET.Element, *path: str) -> str | None:
    c = _child(el, *path)
    return c.text.strip() if c is not None and c.text else None


def _parse_ts(text: str) -> datetime:
    return datetime.strptime(text, "%Y-%m-%dT%H:%MZ").replace(tzinfo=UTC)


def _fmt(ts: datetime) -> str:
    return ts.astimezone(UTC).strftime("%Y%m%d%H%M")


class TimeSeries:
    """Eine ``TimeSeries`` mit allen Perioden, aufgefüllt nach Kurventyp A03."""

    def __init__(self, el: ET.Element) -> None:
        self.el = el
        self.psr_type = _text(el, "MktPSRType", "psrType")
        self.in_domain = _text(el, "inBiddingZone_Domain.mRID")
        self.out_domain = _text(el, "outBiddingZone_Domain.mRID")

    def periods(self) -> Iterator[tuple[timedelta, list[tuple[datetime, float]]]]:
        for period in (c for c in self.el if _local(c.tag) == "Period"):
            start_s = _text(period, "timeInterval", "start")
            end_s = _text(period, "timeInterval", "end")
            res_s = _text(period, "resolution")
            if not (start_s and end_s and res_s):
                raise SourceError("ENTSO-E: Period ohne timeInterval/resolution")
            start, end, res = _parse_ts(start_s), _parse_ts(end_s), parse_duration(res_s)
            given: dict[int, float] = {}
            for point in (c for c in period if _local(c.tag) == "Point"):
                pos = _text(point, "position")
                val = _text(point, "quantity") or _text(point, "price.amount")
                if pos is not None and val is not None:
                    given[int(pos)] = float(val)
            n = int((end - start) / res)
            pts: list[tuple[datetime, float]] = []
            last: float | None = None
            for i in range(1, n + 1):
                last = given.get(i, last)
                if last is not None:
                    pts.append((start + (i - 1) * res, last))
            yield res, pts


def parse_document(xml: bytes) -> list[TimeSeries]:
    """@relation(SWR-ENI-042, SWR-ENI-030, scope=function)"""
    root = ET.fromstring(xml)
    if _local(root.tag) == "Acknowledgement_MarketDocument":
        code = _text(root, "Reason", "code")
        text = _text(root, "Reason", "text") or ""
        if code == "999" or "No matching data" in text:
            return []
        raise SourceError(f"ENTSO-E: {code} {text}".strip())
    return [TimeSeries(c) for c in root if _local(c.tag) == "TimeSeries"]


def merge(
    series_list: list[TimeSeries], start: datetime, end: datetime
) -> tuple[timedelta, list[tuple[datetime, float]]] | None:
    """Fasst Perioden zusammen; bei mehreren Auflösungen gewinnt die feinste.

    @relation(SWR-ENI-042, scope=function)
    """
    by_res: dict[timedelta, dict[datetime, float]] = {}
    for ts in series_list:
        for res, pts in ts.periods():
            bucket = by_res.setdefault(res, {})
            for t, v in pts:
                bucket.setdefault(t, v)
    if not by_res:
        return None
    res = min(by_res)
    pts = clip(sorted(by_res[res].items()), start, end)
    return (res, pts) if pts else None


#: Datensätze auf der Freigabeliste von ENTSO-E (Stand 18.10.2023)
FREE_DATASETS = ["load_forecast", *(f"load_forecast_{z}" for z in LOAD_FORECAST_ZONES)]


class Entsoe:
    """@relation(SWR-ENI-042, scope=class)"""

    code = "entsoe"
    license = "ENTSO-E Transparency Platform, Nutzungsbedingungen von ENTSO-E"
    priority = 20
    free = False

    def __init__(self, http: Http, token: str, base_url: str = BASE_URL) -> None:
        if not token:
            raise ValueError("ENTSO-E-Token fehlt (PDB_ENTSOE_TOKEN)")
        self.http = http
        self.token = token
        self.base_url = base_url

    def datasets(self) -> list[str]:
        return [d for d in QUERIES if d not in FREE_DATASETS]

    def max_chunk(self, dataset: str) -> timedelta:
        return timedelta(days=31)

    def _query(self, dataset: str, start: datetime, end: datetime) -> list[TimeSeries]:
        params = {
            "securityToken": self.token,
            **QUERIES[dataset],
            "periodStart": _fmt(start),
            "periodEnd": _fmt(end),
        }
        r = self.http.get(self.base_url, params)
        if r.status_code >= 400 and b"Acknowledgement_MarketDocument" not in r.content:
            r.raise_for_status()
        return parse_document(r.content)

    def fetch(self, dataset: str, start: datetime, end: datetime) -> list[SeriesData]:
        docs = self._query(dataset, start, end)
        groups: dict[str, list[TimeSeries]] = {}
        if dataset == "price":
            groups["da_price_de_lu"] = docs
        elif dataset == "load":
            groups["load_actual"] = docs
        elif dataset == "load_forecast":
            groups["load_da_forecast"] = docs
        elif dataset.startswith("load_forecast_"):
            groups["load_da_forecast_" + dataset.removeprefix("load_forecast_")] = docs
        else:
            prefix = "gen_" if dataset == "generation" else "gen_da_forecast_"
            for ts in docs:
                # Verbrauch (z. B. Pumpspeicher) steht mit outBiddingZone_Domain im Dokument
                if ts.out_domain and not ts.in_domain:
                    continue
                code = prefix + PSR_TYPES.get(ts.psr_type or "", "?")
                if code in SERIES:
                    groups.setdefault(code, []).append(ts)
        out = []
        for code, members in groups.items():
            merged = merge(members, start, end)
            if merged:
                out.append(SeriesData(series(code), merged[0], merged[1]))
        return out


class EntsoeLast(Entsoe):
    """Freier Teil von ENTSO-E: Day-Ahead-Lastprognosen (6.1.b) für DE und die Nachbarzonen,
    CC BY 4.0.

    @relation(SWR-ENI-042, scope=class)
    """

    code = "entsoe_last"
    license = "CC BY 4.0, ENTSO-E Transparency Platform (List of Data available for free re-use)"
    free = True

    def datasets(self) -> list[str]:
        return list(FREE_DATASETS)
