from pathlib import Path

import pytest
from gribkit import IdxParseError, MessageSpec, merge_ranges, missing_specs, parse_idx, select

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def f042():
    return parse_idx((FIXTURES / "gfs.t06z.pgrb2.0p25.f042.idx").read_text())


def test_parse_real_gfs_idx(f042):
    assert len(f042) == 743
    first = f042[0]
    assert (first.number, first.offset, first.date, first.var, first.level, first.fcst) == (
        "1",
        0,
        "2026102006",
        "PRMSL",
        "mean sea level",
        "42 hour fcst",
    )
    assert first.length == f042[1].offset
    # letzte Nachricht: Länge ohne Dateigröße unbekannt
    assert f042[-1].length is None and f042[-1].end is None


def test_total_size_sets_last_length(f042):
    text = (FIXTURES / "gfs.t06z.pgrb2.0p25.f042.idx").read_text()
    entries = parse_idx(text, total_size=f042[-1].offset + 1234)
    assert entries[-1].length == 1234
    with pytest.raises(IdxParseError):
        parse_idx(text, total_size=f042[-1].offset)


def test_select_and_missing(f042):
    specs = [
        MessageSpec("TMP", "2 m above ground"),
        MessageSpec("TCDC", "entire atmosphere"),
        MessageSpec("DSWRF", "surface", fcst="ave"),
        MessageSpec("NOPE", "surface"),
    ]
    chosen = select(f042, specs)
    assert [e.key for e in chosen] == [
        "TMP:2 m above ground:42 hour fcst",
        "TCDC:entire atmosphere:42 hour fcst",
        "TCDC:entire atmosphere:36-42 hour ave fcst",
        "DSWRF:surface:36-42 hour ave fcst",
    ]
    assert [s.var for s in missing_specs(f042, specs)] == ["NOPE"]


def test_f000_has_no_accumulations():
    entries = parse_idx((FIXTURES / "gfs.t06z.pgrb2.0p25.f000.idx").read_text())
    specs = [MessageSpec("DSWRF", "surface"), MessageSpec("TMP", "2 m above ground")]
    assert [s.var for s in missing_specs(entries, specs)] == ["DSWRF"]


def test_merge_adjacent_and_gap():
    text = "\n".join(
        [
            "1:0:d=2026102006:A:surface:anl:",
            "2:100:d=2026102006:B:surface:anl:",
            "3:250:d=2026102006:C:surface:anl:",
            "4:300:d=2026102006:D:surface:anl:",
        ]
    )
    e = parse_idx(text, total_size=400)
    ranges = merge_ranges([e[0], e[1], e[3]])
    assert [(r.start, r.end) for r in ranges] == [(0, 249), (300, 399)]
    assert ranges[0].header == "bytes=0-249"

    merged = merge_ranges([e[0], e[3]], max_gap=200)
    assert len(merged) == 1 and merged[0].header == "bytes=0-399"
    payload = bytes(range(256)) + bytes(144)
    # nur A (0..99) und D (300..399), C/B-Lücke verworfen
    assert merged[0].extract(payload) == payload[0:100] + payload[300:400]


def test_merge_open_end_and_submessages():
    text = "\n".join(
        [
            "1:0:d=2026102006:A:surface:anl:",
            "2.1:100:d=2026102006:U:10 m:anl:",
            "2.2:100:d=2026102006:V:10 m:anl:",
        ]
    )
    e = parse_idx(text)
    assert e[1].length is None and e[2].length is None
    ranges = merge_ranges(e)
    assert [(r.start, r.end) for r in ranges] == [(0, None)]
    assert ranges[0].header == "bytes=0-"
    assert ranges[0].extract(b"x" * 150) == b"x" * 150


def test_extract_checks_length():
    e = parse_idx("1:0:d=1:A:surface:anl:", total_size=10)
    with pytest.raises(ValueError):
        merge_ranges(e)[0].extract(b"short")


def test_parse_errors():
    with pytest.raises(IdxParseError):
        parse_idx("1:abc:d=1:A:surface:anl:")
    with pytest.raises(IdxParseError):
        parse_idx("1:0:d=1")
