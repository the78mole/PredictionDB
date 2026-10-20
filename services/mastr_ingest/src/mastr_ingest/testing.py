"""Hilfen für Tests: synthetischer Gesamtdatenexport im Format des MaStR (UTF-16-XML im ZIP)."""

from __future__ import annotations

import zipfile
from pathlib import Path
from typing import Any

from lxml import etree

#: Auszug aus ``Katalogwerte.xml`` (Ids wie im echten Export)
CATALOG = {
    "31": "In Planung",
    "35": "In Betrieb",
    "37": "Vorübergehend stillgelegt",
    "38": "Endgültig stillgelegt",
    "852": "Freiflächensolaranlage",
    "853": "Gebäudesolaranlage",
    "2484": "Sonstige Solaranlage",
    "2961": "Steckerfertige Solaranlage (sog. Balkonkraftwerk)",
    "888": "Windkraft an Land",
    "889": "Windkraft auf See",
    "639": "Ostsee",
    "640": "Nordsee",
    "699": "Süd",
    "809": "21 - 40 Grad",
    "1403": "Bayern",
    "1408": "Niedersachsen",
    "1416": "Ausschließliche Wirtschaftszone",
}

ELEMENT = {
    "EinheitenSolar": "EinheitSolar",
    "EinheitenWind": "EinheitWind",
    "EinheitenBiomasse": "EinheitBiomasse",
    "EinheitenWasser": "EinheitWasser",
}

ENERGIETRAEGER = {
    "EinheitenSolar": "2495",
    "EinheitenWind": "2497",
    "EinheitenBiomasse": "2493",
    "EinheitenWasser": "2498",
}


def xml_bytes(root: str, item: str, records: list[dict[str, Any]]) -> bytes:
    el = etree.Element(root)
    for rec in records:
        child = etree.SubElement(el, item)
        for k, v in rec.items():
            if v is not None:
                etree.SubElement(child, k).text = str(v)
    out: bytes = etree.tostring(el, encoding="UTF-16", xml_declaration=True)
    return out


def unit(
    nr: str, kw: float, commissioned: str | None = "2018-05-01", **fields: Any
) -> dict[str, Any]:
    rec: dict[str, Any] = {
        "EinheitMastrNummer": nr,
        "EinheitBetriebsstatus": "35",
        "Bruttoleistung": f"{kw:.3f}",
        "Nettonennleistung": f"{kw:.3f}",
        "Inbetriebnahmedatum": commissioned,
        "DatumLetzteAktualisierung": "2026-01-15T10:00:00.1234567",
    }
    rec.update(fields)
    return rec


def make_export(
    directory: Path,
    tables: dict[str, list[dict[str, Any]]],
    *,
    export_date: str = "20261001",
    version: str = "26.1",
    split: int = 0,
) -> Path:
    """Schreibt ``Gesamtdatenexport_<datum>_<release>.zip``.

    ``tables`` bildet Tabellennamen (``EinheitenSolar`` …) auf Datensätze ab. Mit ``split > 0``
    wird jede Tabelle wie im echten Export auf nummerierte Dateien zu je ``split`` Sätzen verteilt.
    """
    path = directory / f"Gesamtdatenexport_{export_date}_{version}.zip"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(
            "Katalogwerte.xml",
            xml_bytes(
                "Katalogwerte",
                "Katalogwert",
                [{"Id": k, "Wert": v, "KatalogKategorieId": 1} for k, v in CATALOG.items()],
            ),
        )
        zf.writestr("Marktakteure.xml", xml_bytes("Marktakteure", "Marktakteur", []))
        for table, records in tables.items():
            recs = [{**r, "Energietraeger": ENERGIETRAEGER[table]} for r in records]
            if split:
                for n, start in enumerate(range(0, len(recs), split), 1):
                    zf.writestr(
                        f"{table}_{n}.xml",
                        xml_bytes(table, ELEMENT[table], recs[start : start + split]),
                    )
            else:
                zf.writestr(f"{table}.xml", xml_bytes(table, ELEMENT[table], recs))
    return path
