"""Umrisskarte für das Karten-Dashboard aus Natural Earth (gemeinfrei) erzeugen.

Staatsgrenzen (admin 0) im Ausschnitt um Deutschland und die Bundesländer (admin 1), vereinfacht
und auf drei Nachkommastellen gerundet. Eingaben sind die GeoJSON-Dateien aus
https://github.com/nvkelso/natural-earth-vector/tree/master/geojson:

    uv run --with shapely python scripts/karte-umrisse.py \\
        ne_10m_admin_0_countries.geojson ne_10m_admin_1_states_provinces.geojson \\
        config/grafana/maps/umrisse-de.geojson
"""

import json
import sys
from typing import Any

from shapely.geometry import box, mapping, shape  # type: ignore[import-untyped]

# deutlich größer als der sichtbare Ausschnitt, damit keine Schnittkanten zu sehen sind
BBOX = box(-15.0, 35.0, 35.0, 66.0)
TOLERANZ = 0.01  # Grad, rund 1 km (Deutschland und Bundesländer)
TOLERANZ_AUSLAND = 0.03


def _runden(obj: object) -> object:
    if isinstance(obj, float):
        return round(obj, 3)
    if isinstance(obj, (list, tuple)):
        return [_runden(x) for x in obj]
    return obj


def _feature(geom: Any, props: dict[str, Any]) -> dict[str, Any] | None:
    tol = TOLERANZ if props["de"] else TOLERANZ_AUSLAND
    g = geom.intersection(BBOX).simplify(tol, preserve_topology=True)
    if g.is_empty:
        return None
    m = mapping(g)
    return {
        "type": "Feature",
        "properties": props,
        "geometry": {"type": m["type"], "coordinates": _runden(m["coordinates"])},
    }


def _lesen(pfad: str) -> list[dict[str, Any]]:
    with open(pfad) as fh:
        features: list[dict[str, Any]] = json.load(fh)["features"]
    return features


def main(staaten: str, laender: str, ziel: str) -> None:
    out: list[dict[str, Any]] = []
    for f in _lesen(staaten):
        geom = shape(f["geometry"])
        if not geom.intersects(BBOX):
            continue
        p = f["properties"]
        de = p["ADM0_A3"] == "DEU"
        feat = _feature(
            geom, {"name": p["NAME_DE"] or p["NAME"], "ebene": "staat", "de": 1 if de else 0}
        )
        if feat:
            out.append(feat)
    for f in _lesen(laender):
        p = f["properties"]
        if p["adm0_a3"] != "DEU":
            continue
        feat = _feature(
            shape(f["geometry"]), {"name": p["name_de"] or p["name"], "ebene": "land", "de": 1}
        )
        if feat:
            out.append(feat)
    # Bundesländer zuletzt, damit ihre Linien über der Staatsfläche liegen
    doc = {
        "type": "FeatureCollection",
        "name": "Umrisse Deutschland und Nachbarstaaten (Natural Earth, gemeinfrei)",
        "features": out,
    }
    with open(ziel, "w") as fh:
        json.dump(doc, fh, ensure_ascii=False, separators=(",", ":"))
        fh.write("\n")


if __name__ == "__main__":
    main(*sys.argv[1:4])
