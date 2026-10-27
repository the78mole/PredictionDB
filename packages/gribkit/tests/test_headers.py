from typing import Any

import pytest

from gribkit import HeaderError, find_boundary, parse_head
from gribkit.headers import HEAD_BYTES, entry
from gribkit.testing import make_global_message


def _message(**keys: Any) -> bytes:
    import eccodes as ec

    msg, _, _ = make_global_message(inc=10.0)
    h = ec.codes_new_from_message(msg)
    try:
        for k, v in keys.items():
            ec.codes_set(h, k, v)
        return bytes(ec.codes_get_message(h))
    finally:
        ec.codes_release(h)


def test_instant_above_ground() -> None:
    msg = _message(
        discipline=0,
        parameterCategory=2,
        parameterNumber=2,
        typeOfFirstFixedSurface=103,
        scaledValueOfFirstFixedSurface=100,
        scaleFactorOfFirstFixedSurface=0,
        forecastTime=42,
    )
    head = parse_head(msg[:HEAD_BYTES])
    assert (head.length, head.var, head.level, head.fcst) == (
        len(msg),
        "UGRD",
        "100 m above ground",
        "42 hour fcst",
    )
    assert entry(7, 1234, head).key == "UGRD:100 m above ground:42 hour fcst"


def test_analysis_and_surfaces() -> None:
    msg = _message(
        discipline=0,
        parameterCategory=3,
        parameterNumber=1,
        typeOfFirstFixedSurface=101,
        forecastTime=0,
    )
    head = parse_head(msg)
    assert (head.var, head.level, head.fcst) == ("PRMSL", "mean sea level", "anl")


def test_soil_layer() -> None:
    msg = _message(
        discipline=2,
        parameterCategory=0,
        parameterNumber=192,
        typeOfFirstFixedSurface=106,
        scaleFactorOfFirstFixedSurface=1,
        scaledValueOfFirstFixedSurface=1,
        typeOfSecondFixedSurface=106,
        scaleFactorOfSecondFixedSurface=1,
        scaledValueOfSecondFixedSurface=4,
    )
    head = parse_head(msg)
    assert (head.var, head.level) == ("SOILW", "0.1-0.4 m below ground")


@pytest.mark.parametrize(
    ("start", "end", "type_", "fcst"),
    [
        (36, 42, 0, "36-42 hour ave fcst"),
        (0, 24, 1, "0-1 day acc fcst"),
        (240, 252, 1, "240-252 hour acc fcst"),
    ],
)
def test_statistics(start: int, end: int, type_: int, fcst: str) -> None:
    msg = _message(
        productDefinitionTemplateNumber=8,
        discipline=0,
        parameterCategory=1,
        parameterNumber=8,
        typeOfFirstFixedSurface=1,
        typeOfStatisticalProcessing=type_,
        stepUnits=1,
        startStep=start,
        endStep=end,
    )
    head = parse_head(msg)
    assert (head.var, head.level, head.fcst) == ("APCP", "surface", fcst)


def test_unknown_parameter_keeps_numbers() -> None:
    msg = _message(discipline=0, parameterCategory=19, parameterNumber=0)
    assert parse_head(msg).var == "var0_19_0"


def test_head_errors() -> None:
    msg = _message()
    with pytest.raises(HeaderError, match="GRIB-Kennung"):
        parse_head(b"<html>" + msg)
    with pytest.raises(HeaderError, match="Sektion 4"):
        parse_head(msg[:40])


def test_find_boundary() -> None:
    msg = _message()
    data = msg + msg + msg
    # Mitten in der ersten Nachricht: nächster Anfang ist die zweite
    assert find_boundary(data[100:], len(data) - 100) == len(msg) - 100
    # Ein "GRIB" ohne vorangehendes 7777 zählt nicht
    assert find_boundary(b"xxxxGRIB" + b"\0" * 20, 1000) is None
    # Länge, die nicht mehr in die Datei passt, ist ein Fehltreffer
    assert find_boundary(data[len(msg) - 4 :], len(msg)) is None
