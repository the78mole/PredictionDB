"""Streaming-Parser für den MaStR-Gesamtdatenexport.

Der Export ist ein ZIP mit UTF-16-XML-Dateien je Tabelle, große Tabellen sind nummeriert
(``EinheitenSolar_1.xml`` …). Jede Einheit ist ein Element ``Einheit<Typ>`` mit einfachen
Kindelementen; Aufzählungswerte sind Ids aus ``Katalogwerte.xml``. Gelesen wird mit
``lxml.etree.iterparse``, verarbeitete Elemente werden sofort verworfen, damit auch die
Solar-Tabellen (Millionen Einheiten) mit wenig Speicher durchlaufen.

Die Zuordnung zur Technologie geht über den Katalogtext, nicht über die Id, damit sie
neue Releases mit verschobenen Ids übersteht.
"""

from __future__ import annotations

import re
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date
from typing import IO

from lxml import etree

from predictiondb_core.log import get_logger

log = get_logger(__name__)

TECHNOLOGIES = ("wind_on", "wind_off", "pv_ground", "pv_roof", "biomass", "hydro")

#: Einheitentabellen und ihr Elementname
TABLES = {
    "EinheitenSolar": "EinheitSolar",
    "EinheitenWind": "EinheitWind",
    "EinheitenBiomasse": "EinheitBiomasse",
    "EinheitenWasser": "EinheitWasser",
}
_MEMBER = re.compile(r"^(?P<table>Einheiten[A-Za-z]+?)(?:_(?P<n>\d+))?\.xml$")
_ANY_MEMBER = re.compile(r"^(?P<table>[A-Za-z]+?)(?:_(?P<n>\d+))?\.xml$")

#: Betriebsstatus (Katalogtext) → Kurzform in ``assets.unit.status``
STATUS = {
    "in betrieb": "active",
    "vorübergehend stillgelegt": "paused",
    "endgültig stillgelegt": "decommissioned",
    "in planung": "planned",
}


@dataclass(slots=True)
class Unit:
    mastr_nr: str
    technology: str
    status: str | None
    capacity_kw: float
    net_capacity_kw: float | None
    commissioned: date | None
    decommissioned: date | None
    paused_from: date | None
    paused_until: date | None
    lat: float | None
    lon: float | None
    ags: str | None
    plz: str | None
    bundesland: str | None
    sea_area: str | None
    hub_height_m: float | None
    rotor_diameter_m: float | None
    azimuth: str | None
    tilt: str | None
    lokation_nr: str | None = None


Catalog = dict[str, str]

#: Regelzonen der Übertragungsnetzbetreiber: Teil des Katalogtexts → Code in ``assets.unit``
REGELZONEN = {
    "50hertz": "50hertz",
    "amprion": "amprion",
    "tennet": "tennet",
    "transnet": "transnetbw",
}


@dataclass(slots=True)
class GridConnection:
    """Netzanschlusspunkt einer Lokation (``Netzanschlusspunkte_*.xml``)."""

    lokation_nr: str
    regelzone: str | None
    netzbetreiber_nr: str | None


def unit_members(zf: zipfile.ZipFile) -> list[tuple[str, str]]:
    """Einheitendateien im Export als ``(Dateiname, Elementname)``, in stabiler Reihenfolge."""
    out = []
    for name in zf.namelist():
        m = _MEMBER.match(name.rsplit("/", 1)[-1])
        if m and m["table"] in TABLES:
            out.append((m["table"], int(m["n"] or 0), name))
    return [(name, TABLES[table]) for table, _, name in sorted(out)]


def table_members(zf: zipfile.ZipFile, table: str) -> list[str]:
    """Dateien einer (ggf. nummerierten) Tabelle des Exports in Reihenfolge."""
    out = []
    for name in zf.namelist():
        m = _ANY_MEMBER.match(name.rsplit("/", 1)[-1])
        if m and m["table"] == table:
            out.append((int(m["n"] or 0), name))
    return [name for _, name in sorted(out)]


def _iter_records(source: IO[bytes], tag: str) -> Iterator[dict[str, str]]:
    # recover: einzelne fehlerhafte Zeichen im Export sollen nicht den ganzen Import kippen
    for _, el in etree.iterparse(source, events=("end",), tag=tag, recover=True, huge_tree=True):
        rec = {child.tag: child.text for child in el if child.text is not None}
        el.clear()
        while el.getprevious() is not None:
            parent = el.getparent()
            if parent is None:
                break
            del parent[0]
        yield rec


def read_catalog(zf: zipfile.ZipFile) -> Catalog:
    name = next(n for n in zf.namelist() if n.rsplit("/", 1)[-1] == "Katalogwerte.xml")
    with zf.open(name) as f:
        return {r["Id"]: r.get("Wert", "") for r in _iter_records(f, "Katalogwert") if "Id" in r}


def _float(v: str | None) -> float | None:
    if v is None or not v.strip():
        return None
    try:
        return float(v.replace(",", "."))
    except ValueError:
        return None


def _date(v: str | None) -> date | None:
    if not v:
        return None
    try:
        return date.fromisoformat(v[:10])
    except ValueError:
        return None


def _text(catalog: Catalog, v: str | None) -> str | None:
    if v is None:
        return None
    return catalog.get(v.strip(), v.strip()).strip() or None


def classify(table: str, rec: dict[str, str], catalog: Catalog) -> str | None:
    """Technologie-Code aus ``TECHNOLOGIES`` oder ``None`` (nicht betrachtet)."""
    if table == "EinheitenWind":
        lage = (_text(catalog, rec.get("WindAnLandOderAufSee")) or "").lower()
        land = (_text(catalog, rec.get("Bundesland")) or "").lower()
        if "see" in lage or "wirtschaftszone" in land:
            return "wind_off"
        return "wind_on"
    if table == "EinheitenSolar":
        art = (
            _text(catalog, rec.get("ArtDerSolaranlage")) or _text(catalog, rec.get("Lage")) or ""
        ).lower()
        if "freifläche" in art or "sonstige" in art or "gewässer" in art:
            return "pv_ground"
        if "gebäude" in art or "steckerfertig" in art or "balkon" in art or "dach" in art:
            return "pv_roof"
        # ohne Angabe: Größe als Hinweis (Dachanlagen sind fast immer < 1 MW)
        return "pv_ground" if (_float(rec.get("Bruttoleistung")) or 0) >= 1000 else "pv_roof"
    if table == "EinheitenBiomasse":
        return "biomass"
    if table == "EinheitenWasser":
        return "hydro"
    return None


def to_unit(table: str, rec: dict[str, str], catalog: Catalog) -> Unit | None:
    nr = rec.get("EinheitMastrNummer")
    capacity = _float(rec.get("Bruttoleistung"))
    technology = classify(table, rec, catalog)
    if not nr or technology is None or capacity is None or capacity <= 0:
        return None
    status_text = (_text(catalog, rec.get("EinheitBetriebsstatus")) or "").lower()
    status = STATUS.get(status_text)
    updated = _date(rec.get("DatumLetzteAktualisierung"))
    decommissioned = _date(rec.get("DatumEndgueltigeStilllegung"))
    if status == "decommissioned" and decommissioned is None:
        decommissioned = updated
    paused_from = _date(rec.get("DatumBeginnVoruebergehendeStilllegung"))
    paused_until = _date(rec.get("DatumWiederaufnahmeBetrieb"))
    if status == "paused":
        paused_from = paused_from or updated
        if paused_until is not None and paused_from is not None and paused_until <= paused_from:
            paused_until = None
    elif paused_until is None:
        # ohne Wiederaufnahmedatum ist eine alte Unterbrechung nicht rekonstruierbar
        paused_from = None
    sea = _text(catalog, rec.get("Seelage")) if technology == "wind_off" else None
    return Unit(
        mastr_nr=nr.strip(),
        technology=technology,
        status=status,
        capacity_kw=capacity,
        net_capacity_kw=_float(rec.get("Nettonennleistung")),
        commissioned=_date(rec.get("Inbetriebnahmedatum")),
        decommissioned=decommissioned,
        paused_from=paused_from,
        paused_until=paused_until,
        lat=_float(rec.get("Breitengrad")),
        lon=_float(rec.get("Laengengrad")),
        ags=(rec.get("Gemeindeschluessel") or "").strip() or None,
        plz=(rec.get("Postleitzahl") or "").strip() or None,
        bundesland=_text(catalog, rec.get("Bundesland")),
        sea_area=sea,
        hub_height_m=_float(rec.get("Nabenhoehe")),
        rotor_diameter_m=_float(rec.get("Rotordurchmesser")),
        azimuth=_text(catalog, rec.get("Hauptausrichtung")),
        tilt=_text(catalog, rec.get("HauptausrichtungNeigungswinkel")),
        lokation_nr=(rec.get("LokationMaStRNummer") or "").strip() or None,
    )


def iter_units(zf: zipfile.ZipFile, catalog: Catalog | None = None) -> Iterator[Unit]:
    """Alle betrachteten Einheiten des Exports (``TECHNOLOGIES``), Datei für Datei."""
    catalog = read_catalog(zf) if catalog is None else catalog
    for name, tag in unit_members(zf):
        table = _MEMBER.match(name.rsplit("/", 1)[-1])["table"]  # type: ignore[index]
        n = skipped = 0
        with zf.open(name) as f:
            for rec in _iter_records(f, tag):
                unit = to_unit(table, rec, catalog)
                if unit is None:
                    skipped += 1
                    continue
                n += 1
                yield unit
        log.info("mastr.parse.file", file=name, units=n, skipped=skipped)


def regelzone(text: str | None) -> str | None:
    """Code der Regelzone aus dem Katalogtext (``TenneT`` → ``tennet``)."""
    low = (text or "").lower().replace(" ", "")
    return next((code for key, code in REGELZONEN.items() if key in low), None)


def iter_grid_connections(
    zf: zipfile.ZipFile, catalog: Catalog | None = None
) -> Iterator[GridConnection]:
    """Netzanschlusspunkte mit Regelzone und Netzbetreiber, je Lokation der Einheiten.

    Strom-Netzanschlusspunkte tragen die Regelzone des ÜNB (``RegelzoneNetzanschlusspunkt``)
    und die MaStR-Nummer des Netzbetreibers; Einheiten verweisen über
    ``LokationMaStRNummer`` darauf. Punkte ohne beides (Gas) werden übersprungen.
    """
    catalog = read_catalog(zf) if catalog is None else catalog
    for name in table_members(zf, "Netzanschlusspunkte"):
        n = 0
        with zf.open(name) as f:
            for rec in _iter_records(f, "Netzanschlusspunkt"):
                lok = (rec.get("LokationMaStRNummer") or "").strip()
                zone = regelzone(_text(catalog, rec.get("RegelzoneNetzanschlusspunkt")))
                nb = (rec.get("NetzbetreiberMaStRNummer") or "").strip() or None
                if not lok or (zone is None and nb is None):
                    continue
                n += 1
                yield GridConnection(lok, zone, nb)
        log.info("mastr.parse.file", file=name, grid_connections=n)
