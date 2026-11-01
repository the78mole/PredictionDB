"""Property-Tests mit Hypothesis: ENTSO-E-Antworten bei zufälligen und verfälschten Eingaben.

Ungültige Antworten dürfen nur zu ``SourceError`` (bzw. ``FormatError``) führen, nie zu
anderen Ausnahmen, Endlosschleifen oder Werten, die nicht in der Antwort standen. Der Fuzzer
``fuzz/predictiondb_fuzz.py`` prüft dieselben Funktionen länger und abdeckungsgesteuert.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from pathlib import Path
from xml.sax.saxutils import escape

import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st

from energy_ingest.sources import FormatError, SourceError
from energy_ingest.sources.entsoe import MAX_POINTS, merge, parse_document, parse_duration

FIXTURES = Path(__file__).parent / "fixtures"
NS = "urn:iec62325.351:tc57wg16:451-3:publicationdocument:7:3"
T0 = datetime(2026, 10, 18, 22, 0, tzinfo=UTC)


def _check(xml: bytes) -> None:
    """Parsen und Zusammenfassen; nur ``SourceError`` ist erlaubt."""
    try:
        series = parse_document(xml)
        out = merge(series, datetime(1, 1, 2, tzinfo=UTC), datetime(9999, 12, 30, tzinfo=UTC))
    except SourceError:
        return
    if out is not None:
        res, pts = out
        assert res > timedelta(0)
        assert all(math.isfinite(v) for _, v in pts)
        assert len(pts) <= MAX_POINTS * len(series) * 10


@given(st.text(max_size=40))
@example("PT0M")
@example("P0DT0H0M")
@example("P99999999999D")
@example("PT15M\n")
def test_parse_duration(text: str) -> None:
    """@relation(SWR-ENI-033, scope=function)"""
    try:
        res = parse_duration(text)
    except SourceError:
        return
    assert res > timedelta(0)


@given(st.binary(max_size=2000))
@example(b"")
@example(b"<a>")
@example(b'<?xml version="1.0" encoding="x-unbekannt"?><a/>')
@settings(max_examples=300)
def test_parse_document_beliebige_bytes(xml: bytes) -> None:
    """@relation(SWR-ENI-033, scope=function)"""
    _check(xml)


_feld = st.one_of(
    st.text(alphabet=st.characters(blacklist_categories=["Cs", "Cc"]), max_size=25),
    st.sampled_from(["2026-10-18T22:00Z", "2026-10-18T23:00Z", "0001-01-01T00:00Z"]),
    st.sampled_from(["9999-12-31T23:59Z", "PT15M", "PT60M", "PT0M", "PT1M", "P1D"]),
)
_zahl = st.one_of(
    _feld,
    st.integers(-5, 10).map(str),
    st.floats(allow_nan=True, allow_infinity=True).map(repr),
    st.sampled_from(["nan", "inf", "-inf", "1e400", "1_0", "9" * 5000]),
)


@st.composite
def _dokument(draw: st.DrawFn) -> bytes:
    """Ein A44/A75-Dokument mit beliebigen Feldinhalten."""
    perioden = []
    for _ in range(draw(st.integers(0, 3))):
        punkte = "".join(
            f"<Point><position>{escape(draw(_zahl))}</position>"
            f"<quantity>{escape(draw(_zahl))}</quantity></Point>"
            for _ in range(draw(st.integers(0, 4)))
        )
        perioden.append(
            f"<Period><timeInterval><start>{escape(draw(_feld))}</start>"
            f"<end>{escape(draw(_feld))}</end></timeInterval>"
            f"<resolution>{escape(draw(_feld))}</resolution>{punkte}</Period>"
        )
    root = draw(st.sampled_from(["GL_MarketDocument", "Acknowledgement_MarketDocument"]))
    reason = (
        f"<Reason><code>{escape(draw(_feld))}</code><text>{escape(draw(_feld))}</text></Reason>"
    )
    body = f"<TimeSeries>{''.join(perioden)}</TimeSeries>{reason}"
    return f'<{root} xmlns="{NS}">{body}</{root}>'.encode()


@given(_dokument())
@settings(max_examples=500)
def test_parse_document_felder(xml: bytes) -> None:
    """@relation(SWR-ENI-033, scope=function)"""
    _check(xml)


@given(st.data())
@settings(max_examples=200)
def test_parse_document_verfaelscht(data: st.DataObject) -> None:
    """Abgeschnittene und verfälschte echte Antworten.

    @relation(SWR-ENI-033, scope=function)
    """
    name = data.draw(st.sampled_from(["entsoe_a44.xml", "entsoe_a75.xml", "entsoe_ack.xml"]))
    raw = bytearray((FIXTURES / name).read_bytes())
    for _ in range(data.draw(st.integers(0, 6))):
        raw[data.draw(st.integers(0, len(raw) - 1))] = data.draw(st.integers(0, 255))
    _check(bytes(raw[: data.draw(st.integers(0, len(raw)))]))


def _period(start: str, end: str, res: str, points: str = "") -> bytes:
    return (
        f'<Publication_MarketDocument xmlns="{NS}"><TimeSeries><Period><timeInterval>'
        f"<start>{start}</start><end>{end}</end></timeInterval><resolution>{res}</resolution>"
        f"{points}</Period></TimeSeries></Publication_MarketDocument>"
    ).encode()


def test_funde() -> None:
    """Fälle, die vorher andere Ausnahmen oder Endlosschleifen auslösten.

    @relation(SWR-ENI-033, scope=function)
    """
    t0, t1 = "2026-10-18T22:00Z", "2026-10-18T23:00Z"
    # Auflösung null: vorher ZeroDivisionError
    with pytest.raises(FormatError, match="null"):
        list(parse_document(_period(t0, t1, "PT0M"))[0].periods())
    # Jahrtausende in Minuten: vorher Milliarden Schleifendurchläufe
    with pytest.raises(FormatError, match="Punkten"):
        list(parse_document(_period("0001-01-01T00:00Z", t1, "PT1M"))[0].periods())
    # kein XML, kein Zeitpunkt, keine Zahl, kein endlicher Wert: vorher ParseError/ValueError
    with pytest.raises(FormatError):
        parse_document(b"<a>")
    with pytest.raises(FormatError):
        list(parse_document(_period("gestern", t1, "PT60M"))[0].periods())
    point = "<Point><position>1</position><quantity>{}</quantity></Point>"
    for bad in ("abc", "nan", "1e400"):
        with pytest.raises(FormatError):
            list(parse_document(_period(t0, t1, "PT60M", point.format(bad)))[0].periods())
    with pytest.raises(FormatError, match="vor Beginn"):
        list(parse_document(_period(t1, t0, "PT60M"))[0].periods())
    ok = parse_document(_period(t0, t1, "PT15M", point.format("5")))
    assert merge(ok, T0, T0 + timedelta(hours=1)) == (
        timedelta(minutes=15),
        [(T0 + i * timedelta(minutes=15), 5.0) for i in range(4)],
    )
