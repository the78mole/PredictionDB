"""Regionen und Gitter-Gewichte für das GEFS-Silber (``config/gefs/regions_v<n>.json.gz``).

Die Gewichte müssen im Runner vorliegen, weil dort schon je Region verdichtet wird; sie sind
deshalb eine versionierte Datei im Repository. Eine neue Version baut die GEFS-Historie neu
(``fingerprint`` enthält den Hash der Datei).

Version 2 (Version 1 reichte nur von 41 bis 63° N und 5° W bis 25° O, mit wenigen Nachbarzonen):

- **DE** aus dem MaStR (DL-DE BY 2.0): Leistung je 0,5°-Zelle für Wind an Land, Wind auf See
  und PV (Frei- plus Dachflächen), dazu die leistungsgewichtete Nabenhöhe. Die Zellwerte kommen
  als CSV aus der Datenbank der VM (Abfrage ``config/gefs/mastr_cells.sql``).
- **Alle Gebotszonen des Verbundnetzes** (``ZONES``, auch DE) flächengewichtet
  (``<zone>:area``), aus den Grenzen von Natural Earth (gemeinfrei). Zonen innerhalb eines
  Landes sind aus Verwaltungsregionen zusammengesetzt. Freie Anlagenregister der Nachbarn
  ergänzen das in einer späteren Version (``<zone>:wind``, ``<zone>:pv``).
"""

from __future__ import annotations

import csv
import gzip
import json
import math
from collections import defaultdict
from pathlib import Path
from typing import Any

import httpx
import numpy as np
from numpy.typing import NDArray

from wx_exchange.gefs import BBox

F64 = NDArray[np.float64]

NATURAL_EARTH = (
    "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/v5.1.2/geojson/{name}.geojson"
)
ADMIN0 = "ne_10m_admin_0_countries"
ADMIN1 = "ne_10m_admin_1_states_provinces"


def _land(code: str) -> list[dict[str, Any]]:
    return [{"adm0": code}]


def _parts(adm0: str, names: list[str], field: str = "name") -> list[dict[str, Any]]:
    return [{"adm1": adm0, "field": field, "names": names}]


#: Gebotszone → Teile: ganze Länder (``ADM0_A3``) oder Verwaltungsregionen eines Landes
#: (admin-1 ``name``, bei Italien ``region``, bei Großbritannien ``geonunit``). Umfang: das
#: synchrone Verbundnetz Kontinentaleuropa plus Nordeuropa, GB und Irland (ENTSO-E), dazu
#: Ukraine, Moldau und die Türkei; ohne Maghreb, Island und Zypern (Nutzer 2026-10-29).
#: Norwegische und schwedische Zonen folgen den Grenzen der Provinzen nur näherungsweise.
ZONES: dict[str, list[dict[str, Any]]] = {
    "DE": _land("DEU"),
    "AL": _land("ALB"),
    "AT": _land("AUT"),
    "BA": _land("BIH"),
    "BE": _land("BEL"),
    "BG": _land("BGR"),
    "CH": _land("CHE"),
    "CZ": _land("CZE"),
    "DK1": _parts("DNK", ["Nordjylland", "Midtjylland", "Syddanmark"]),
    "DK2": _parts("DNK", ["Hovedstaden", "Sjaælland"]),
    "EE": _land("EST"),
    "ES": _land("ESP"),
    "FI": _land("FIN"),
    "FR": _land("FRA"),
    "GB": _parts("GBR", ["England", "Scotland", "Wales"], "geonunit"),
    "GR": _land("GRC"),
    "HR": _land("HRV"),
    "HU": _land("HUN"),
    # Single Electricity Market: Irland und Nordirland
    "IE": _land("IRL") + _parts("GBR", ["Northern Ireland"], "geonunit"),
    "IT-Nord": _parts(
        "ITA",
        [
            "Piemonte",
            "Valle d'Aosta",
            "Lombardia",
            "Trentino-Alto Adige",
            "Veneto",
            "Friuli-Venezia Giulia",
            "Liguria",
            "Emilia-Romagna",
        ],
        "region",
    ),
    "IT-CNor": _parts("ITA", ["Toscana", "Marche", "Umbria"], "region"),
    "IT-CSud": _parts("ITA", ["Lazio", "Abruzzo", "Campania"], "region"),
    "IT-Sud": _parts("ITA", ["Molise", "Apulia", "Basilicata"], "region"),
    "IT-Calabria": _parts("ITA", ["Calabria"], "region"),
    "IT-Sicilia": _parts("ITA", ["Sicily"], "region"),
    "IT-Sardegna": _parts("ITA", ["Sardegna"], "region"),
    "LT": _land("LTU"),
    "LV": _land("LVA"),
    "MD": _land("MDA"),
    "ME": _land("MNE"),
    "MK": _land("MKD"),
    "NL": _land("NLD"),
    "NO1": _parts(
        "NOR", ["Oslo", "Akershus", "Østfold", "Hedmark", "Oppland", "Buskerud", "Vestfold"]
    ),
    "NO2": _parts("NOR", ["Vest-Agder", "Aust-Agder", "Rogaland", "Telemark"]),
    "NO3": _parts("NOR", ["Møre og Romsdal", "Sør-Trøndelag", "Nord-Trøndelag"]),
    "NO4": _parts("NOR", ["Nordland", "Troms", "Finnmark"]),
    "NO5": _parts("NOR", ["Hordaland", "Sogn og Fjordane"]),
    "PL": _land("POL"),
    "PT": _land("PRT"),
    "RO": _land("ROU"),
    "RS": _land("SRB"),
    "SE1": _parts("SWE", ["Norrbotten"]),
    "SE2": _parts("SWE", ["Västerbotten", "Jämtland", "Västernorrland", "Gävleborg"]),
    "SE3": _parts(
        "SWE",
        [
            "Stockholm",
            "Uppsala",
            "Södermanland",
            "Östergötland",
            "Jönköping",
            "Gotland",
            "Västra Götaland",
            "Värmland",
            "Orebro",
            "Västmanland",
            "Dalarna",
        ],
    ),
    "SE4": _parts("SWE", ["Skåne", "Blekinge", "Kronoberg", "Halland", "Kalmar"]),
    "SI": _land("SVN"),
    "SK": _land("SVK"),
    "TR": _land("TUR"),
    "UA": _land("UKR"),
    "XK": _land("KOS"),
}

#: MaStR-Technologie → Region in DE
MASTR: dict[str, str] = {
    "wind_on": "wind_on",
    "wind_off": "wind_off",
    "pv_ground": "pv",
    "pv_roof": "pv",
}

SOURCES = [
    "MaStR Gesamtdatenexport (Bundesnetzagentur, DL-DE BY 2.0)",
    "Natural Earth 1:10m admin-0/admin-1 v5.1.2 (gemeinfrei)",
]


def load_natural_earth(directory: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    """Admin-0 und Admin-1 aus ``directory``; fehlende Dateien werden geladen."""
    directory.mkdir(parents=True, exist_ok=True)
    out = []
    for name in (ADMIN0, ADMIN1):
        path = directory / f"{name}.geojson"
        if not path.exists():
            r = httpx.get(NATURAL_EARTH.format(name=name), timeout=120, follow_redirects=True)
            r.raise_for_status()
            path.write_bytes(r.content)
        out.append(json.loads(path.read_text()))
    return out[0], out[1]


def zone_rings(zone: str, admin0: dict[str, Any], admin1: dict[str, Any]) -> list[F64]:
    """Außenringe der Polygone einer Zone als Arrays (lon, lat)."""
    feats: list[dict[str, Any]] = []
    for part in ZONES[zone]:
        if "adm0" in part:
            found0 = [
                f for f in admin0["features"] if f["properties"].get("ADM0_A3") == part["adm0"]
            ]
            if not found0:
                raise ValueError(f"{zone}: keine Grenzen für {part['adm0']}")
            feats += found0
            continue
        key = part["field"]
        found1 = [
            f
            for f in admin1["features"]
            if f["properties"].get("adm0_a3") == part["adm1"]
            and f["properties"].get(key) in part["names"]
        ]
        if missing := set(part["names"]) - {f["properties"].get(key) for f in found1}:
            raise ValueError(f"{zone}: Regionen nicht gefunden: {sorted(missing)}")
        feats += found1
    rings: list[F64] = []
    for f in feats:
        g = f["geometry"]
        polys = [g["coordinates"]] if g["type"] == "Polygon" else g["coordinates"]
        for poly in polys:
            # nur Außenringe; Seen und Enklaven in Ländern sind für die Gewichte unerheblich
            rings.append(np.asarray(poly[0], dtype=np.float64))
    return rings


def inside(lon: F64, lat: F64, rings: list[F64]) -> NDArray[np.bool_]:
    """Punkt-in-Polygon (gerade/ungerade Kreuzungen) über alle Ringe."""
    hit = np.zeros(lon.shape, dtype=bool)
    for ring in rings:
        x0, y0 = ring[:, 0], ring[:, 1]
        # nur Punkte im umschließenden Rechteck des Rings prüfen
        box = (lon >= x0.min()) & (lon <= x0.max()) & (lat >= y0.min()) & (lat <= y0.max())
        if not box.any():
            continue
        px, py = lon[box][:, None], lat[box][:, None]
        x1, y1 = np.roll(x0, -1)[None, :], np.roll(y0, -1)[None, :]
        xa, ya = x0[None, :], y0[None, :]
        cond = (ya > py) != (y1 > py)
        with np.errstate(divide="ignore", invalid="ignore"):
            xc = xa + (py - ya) * (x1 - xa) / (y1 - ya)
        crossings = np.count_nonzero(cond & (px < xc), axis=1)
        sub = hit[box]
        sub ^= crossings % 2 == 1
        hit[box] = sub
    return hit


def area_cells(rings: list[F64], bbox: BBox, res: float, sub: int = 6) -> list[list[float]]:
    """Zellen ``[lat, lon, anteil]``: Flächenanteil der Zone je Zelle mal cos(Breite)."""
    lats = np.arange(bbox.north, bbox.south - res / 2, -res)
    lons = np.arange(bbox.west, bbox.east + res / 2, res)
    allx = np.concatenate([r[:, 0] for r in rings])
    ally = np.concatenate([r[:, 1] for r in rings])
    lats = lats[(lats >= ally.min() - res) & (lats <= ally.max() + res)]
    lons = lons[(lons >= allx.min() - res) & (lons <= allx.max() + res)]
    off = (np.arange(sub) + 0.5) / sub * res - res / 2
    cells = []
    for la in lats:
        for lo in lons:
            gy, gx = np.meshgrid(la + off, lo + off, indexing="ij")
            frac = float(inside(gx.ravel(), gy.ravel(), rings).mean())
            if frac > 0:
                cells.append(
                    [round(float(la), 3), round(float(lo), 3), frac * math.cos(math.radians(la))]
                )
    total = sum(c[2] for c in cells)
    if total <= 0:
        raise ValueError("Zone liegt außerhalb des Ausschnitts")
    return [[c[0], c[1], round(c[2] / total, 6)] for c in cells]


def mastr_regions(path: Path) -> tuple[list[dict[str, Any]], str]:
    """DE-Regionen aus der CSV der VM-Abfrage; zweites Ergebnis: Stand des Exports."""
    cells: dict[str, dict[tuple[float, float], float]] = defaultdict(lambda: defaultdict(float))
    hub: dict[str, list[float]] = defaultdict(lambda: [0.0, 0.0])
    export = ""
    with path.open() as f:
        for row in csv.DictReader(f):
            region = MASTR.get(row["technology"])
            if region is None:
                continue
            mw = float(row["mw"])
            cells[region][(float(row["lat"]), float(row["lon"]))] += mw
            if row.get("hub_m"):
                hub[region][0] += mw * float(row["hub_m"])
                hub[region][1] += mw
            export = row.get("export_date", export)
    out = []
    for region in ("wind_on", "wind_off", "pv"):
        total = sum(cells[region].values())
        entry: dict[str, Any] = {
            "code": f"DE:{region}",
            "technology": region,
            "capacity_mw": round(total, 1),
            "cells": [
                [lat, lon, round(mw / total, 6)]
                for (lat, lon), mw in sorted(cells[region].items())
                if mw > 0
            ],
        }
        if hub[region][1] > 0:
            entry["hub_height_m"] = round(hub[region][0] / hub[region][1], 1)
        out.append(entry)
    return out, export


def build(
    mastr_csv: Path, natural_earth: Path, bbox: BBox, *, res: float = 0.5, version: str = "v1"
) -> dict[str, Any]:
    admin0, admin1 = load_natural_earth(natural_earth)
    regions, export = mastr_regions(mastr_csv)
    for zone in ZONES:
        cells = area_cells(zone_rings(zone, admin0, admin1), bbox, res)
        regions.append({"code": f"{zone}:area", "technology": "area", "cells": cells})
    return {
        "version": version,
        "grid": {"res": res, **bbox.model_dump()},
        "mastr_export": export,
        "sources": SOURCES,
        "regions": regions,
    }


def write(data: dict[str, Any], out: Path) -> None:
    raw = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(gzip.compress(raw, compresslevel=9, mtime=0))
