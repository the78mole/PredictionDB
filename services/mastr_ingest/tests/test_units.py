import zipfile
from datetime import date
from pathlib import Path

from mastr_ingest.testing import CATALOG, make_export, unit
from mastr_ingest.units import classify, iter_units, read_catalog, to_unit, unit_members


def test_members_sorted_and_filtered(tmp_path: Path) -> None:
    path = make_export(
        tmp_path,
        {
            "EinheitenWind": [unit(f"SEE{i}", 3000) for i in range(5)],
            "EinheitenSolar": [unit(f"SEE9{i}", 9.9) for i in range(25)],
        },
        split=10,
    )
    with zipfile.ZipFile(path) as zf:
        names = [n for n, _ in unit_members(zf)]
    assert names == [
        "EinheitenSolar_1.xml",
        "EinheitenSolar_2.xml",
        "EinheitenSolar_3.xml",
        "EinheitenWind_1.xml",
    ]


def test_catalog_utf16(tmp_path: Path) -> None:
    with zipfile.ZipFile(make_export(tmp_path, {})) as zf:
        cat = read_catalog(zf)
    assert cat["1416"] == "Ausschließliche Wirtschaftszone"
    assert cat == CATALOG


def test_classify_solar_by_catalog_text() -> None:
    def tech(**f: str) -> str | None:
        return classify("EinheitenSolar", unit("SEE1", 50, **f), CATALOG)

    assert tech(ArtDerSolaranlage="852") == "pv_ground"
    assert tech(ArtDerSolaranlage="2484") == "pv_ground"
    assert tech(ArtDerSolaranlage="853") == "pv_roof"
    assert tech(ArtDerSolaranlage="2961") == "pv_roof"
    # ohne Angabe nach Größe
    assert tech() == "pv_roof"
    assert classify("EinheitenSolar", unit("SEE2", 5000), CATALOG) == "pv_ground"


def test_classify_wind_offshore() -> None:
    assert classify("EinheitenWind", unit("A", 3000, WindAnLandOderAufSee="888"), CATALOG) == (
        "wind_on"
    )
    assert classify("EinheitenWind", unit("B", 3000, WindAnLandOderAufSee="889"), CATALOG) == (
        "wind_off"
    )
    assert classify("EinheitenWind", unit("C", 3000, Bundesland="1416"), CATALOG) == "wind_off"
    assert classify("EinheitenVerbrennung", unit("D", 3000), CATALOG) is None


def test_to_unit_fields() -> None:
    rec = unit(
        "SEE1",
        5230,
        "2017-12-15",
        WindAnLandOderAufSee="889",
        Seelage="639",
        Breitengrad="54.850012",
        Laengengrad="14.113451",
        Nabenhoehe="97.500",
        Rotordurchmesser="135.000",
        Bundesland="1416",
    )
    u = to_unit("EinheitenWind", rec, CATALOG)
    assert u is not None
    assert (u.technology, u.status, u.capacity_kw, u.commissioned) == (
        "wind_off",
        "active",
        5230.0,
        date(2017, 12, 15),
    )
    assert (u.lat, u.lon, u.sea_area, u.hub_height_m) == (54.850012, 14.113451, "Ostsee", 97.5)
    assert u.bundesland == "Ausschließliche Wirtschaftszone"


def test_status_dates() -> None:
    gone = to_unit("EinheitenBiomasse", unit("A", 500, EinheitBetriebsstatus="38"), CATALOG)
    assert gone is not None and gone.status == "decommissioned"
    assert gone.decommissioned == date(2026, 1, 15)  # ohne Datum: letzte Aktualisierung

    paused = to_unit(
        "EinheitenBiomasse",
        unit(
            "B", 500, EinheitBetriebsstatus="37", DatumBeginnVoruebergehendeStilllegung="2024-03-01"
        ),
        CATALOG,
    )
    assert paused is not None and (paused.paused_from, paused.paused_until) == (
        date(2024, 3, 1),
        None,
    )

    resumed = to_unit(
        "EinheitenBiomasse",
        unit(
            "C",
            500,
            DatumBeginnVoruebergehendeStilllegung="2020-01-01",
            DatumWiederaufnahmeBetrieb="2021-01-01",
        ),
        CATALOG,
    )
    assert resumed is not None and (resumed.paused_from, resumed.paused_until) == (
        date(2020, 1, 1),
        date(2021, 1, 1),
    )

    # Beginn ohne Wiederaufnahme bei Einheit in Betrieb: nicht rekonstruierbar, verworfen
    odd = to_unit(
        "EinheitenWasser",
        unit("D", 50, DatumBeginnVoruebergehendeStilllegung="2020-01-01"),
        CATALOG,
    )
    assert odd is not None and odd.paused_from is None


def test_invalid_units_skipped() -> None:
    assert to_unit("EinheitenWasser", unit("A", 0), CATALOG) is None
    assert to_unit("EinheitenWasser", {"Bruttoleistung": "5"}, CATALOG) is None
    planned = to_unit("EinheitenWind", unit("B", 3000, None, EinheitBetriebsstatus="31"), CATALOG)
    assert planned is not None and planned.status == "planned" and planned.commissioned is None


def test_iter_units_over_split_files(tmp_path: Path) -> None:
    path = make_export(
        tmp_path,
        {
            "EinheitenSolar": [
                unit(f"SEE{i:03d}", 9.9, Postleitzahl="80331", Gemeindeschluessel="09162000")
                for i in range(120)
            ],
            "EinheitenWasser": [unit("SEEW", 300)],
        },
        split=50,
    )
    with zipfile.ZipFile(path) as zf:
        units = list(iter_units(zf))
    assert len(units) == 121
    assert {u.technology for u in units} == {"pv_roof", "hydro"}
    assert units[0].ags == "09162000" and units[0].plz == "80331" and units[0].lat is None
