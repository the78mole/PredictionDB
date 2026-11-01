"""Property-Tests mit Hypothesis: gribkit bei zufälligen und verfälschten Eingaben.

Die Eingaben (``.idx``-Inventare, GRIB-Köpfe) kommen von NOAA, GDEX oder über GHCR von anderen
Instanzen. Ungültiges darf nur zu ``IdxParseError`` bzw. ``HeaderError`` führen, nie zu anderen
Ausnahmen oder stillschweigend falschen Bereichen. Der Fuzzer unter ``fuzz/`` prüft dieselben
Funktionen länger und mit abdeckungsgesteuerten Eingaben.
"""

from __future__ import annotations

from functools import cache

from hypothesis import HealthCheck, example, given, settings
from hypothesis import strategies as st

from gribkit import HeaderError, find_boundary, parse_head
from gribkit.headers import HEAD_BYTES, entry, idx_line
from gribkit.idx import IdxEntry, IdxParseError, merge_ranges, parse_idx
from gribkit.testing import make_global_message

# Felder ohne Doppelpunkt und Zeilenumbruch, wie sie in .idx-Zeilen stehen
_feld = st.text(
    alphabet=st.characters(blacklist_characters=":\r\n\x0b\x0c\x1c\x1d\x1e\x85\u2028\u2029"),
    max_size=20,
).map(str.strip)


@cache
def _nachricht() -> bytes:
    msg, _, _ = make_global_message(inc=10.0)
    return bytes(msg)


@st.composite
def _eintraege(draw: st.DrawFn) -> list[IdxEntry]:
    """Gültige Einträge mit aufsteigenden Offsets, wie in einer echten ``.idx``."""
    abstaende = draw(st.lists(st.integers(1, 10**9), min_size=1, max_size=40))
    offset = draw(st.integers(0, 10**6))
    out = []
    for i, abstand in enumerate(abstaende, start=1):
        out.append(
            IdxEntry(
                str(i),
                offset,
                abstand,
                draw(st.from_regex(r"\A[0-9]{10}\Z")),
                draw(_feld.filter(bool)),
                draw(_feld),
                draw(_feld).rstrip(":"),
            )
        )
        offset += abstand
    return out


@given(st.text(max_size=2000), st.one_of(st.none(), st.integers(-(10**12), 10**12)))
@example("1:0:d=2026010100:TMP:2 m above ground:anl:\n2:-5:d=x:A:B:C:", None)
@example("1:10:d=:A:B:C:\n2:10:d=:A:B:C:", 10)
def test_parse_idx_beliebiger_text(text: str, groesse: int | None) -> None:
    """Beliebiger Text ergibt Einträge mit positiver Länge oder ``IdxParseError``.

    @relation(SWR-GRIB-008, scope=function)
    """
    try:
        eintraege = parse_idx(text, groesse)
    except IdxParseError:
        return
    for e in eintraege:
        assert e.length is None or e.length > 0


@given(_eintraege())
def test_parse_idx_rundlauf(eintraege: list[IdxEntry]) -> None:
    """Geschriebene ``.idx``-Zeilen lesen sich mit denselben Werten zurück.

    @relation(SWR-GRIB-008, scope=function)
    """
    groesse = (eintraege[-1].end or 0) + 1
    text = "\n".join(idx_line(e) for e in eintraege)
    assert parse_idx(text, groesse) == eintraege


@given(_eintraege(), st.data(), st.integers(0, 10**9))
def test_merge_ranges_deckt_genau_die_auswahl(
    eintraege: list[IdxEntry], data: st.DataObject, luecke: int
) -> None:
    """Bereiche überlappen nicht, sind sortiert und liefern beim Ausschneiden genau die
    gewählten Nachrichten.

    @relation(SWR-GRIB-008, scope=function)
    """
    auswahl = data.draw(st.lists(st.sampled_from(eintraege), unique=True))
    bereiche = merge_ranges(auswahl, max_gap=luecke)
    for a, b in zip(bereiche, bereiche[1:], strict=False):
        assert a.end is not None and a.end < b.start
    gedeckt = [e for r in bereiche for e in r.entries]
    assert sorted(e.offset for e in gedeckt) == sorted({e.offset for e in auswahl})
    for r in bereiche:
        for e in r.entries:
            assert r.start <= e.offset and e.end is not None and r.end is not None
            assert e.end <= r.end
        # Ausschneiden aus Bytes, die ihre Position kodieren, ergibt genau die Nachrichten
        if r.length is not None and r.length <= 4096:
            daten = bytes((r.start + i) % 251 for i in range(r.length))
            erwartet = b"".join(
                bytes((e.offset + i) % 251 for i in range(e.length or 0)) for e in r.entries
            )
            assert r.extract(daten) == erwartet


@given(st.binary(max_size=HEAD_BYTES))
@settings(suppress_health_check=[HealthCheck.too_slow])
def test_parse_head_beliebige_bytes(daten: bytes) -> None:
    """Beliebige Bytes ergeben einen Kopf oder ``HeaderError``.

    @relation(SWR-GRIB-008, scope=function)
    """
    for complete in (False, True):
        try:
            parse_head(daten, complete=complete)
        except HeaderError:
            pass


@given(st.data())
@settings(max_examples=300, deadline=None)  # erste Nachricht baut eccodes, das dauert
def test_parse_head_verfaelschte_nachricht(data: st.DataObject) -> None:
    """Eine echte Nachricht mit verfälschten, abgeschnittenen oder eingefügten Bytes ergibt
    einen Kopf oder ``HeaderError``.

    @relation(SWR-GRIB-008, scope=function)
    """
    msg = bytearray(_nachricht()[:HEAD_BYTES])
    for _ in range(data.draw(st.integers(1, 8))):
        pos = data.draw(st.integers(0, len(msg) - 1))
        msg[pos] = data.draw(st.integers(0, 255))
    msg = msg[: data.draw(st.integers(0, len(msg)))]
    for complete in (False, True):
        try:
            head = parse_head(bytes(msg), complete=complete)
        except HeaderError:
            continue
        idx_line(entry(1, 0, head, "2026010100"))


def _mit_sektion4(sec4_rumpf: bytes) -> bytes:
    """Echte Nachricht bis Sektion 3, danach eine Sektion 4 mit beliebigem Inhalt."""
    msg = _nachricht()
    pos = 16
    while msg[pos + 4] != 4:
        pos += int.from_bytes(msg[pos : pos + 4], "big")
    return msg[:pos] + (len(sec4_rumpf) + 5).to_bytes(4, "big") + b"\x04" + sec4_rumpf


@given(st.binary(min_size=29, max_size=120))
@settings(max_examples=500, deadline=None)
@example(bytes(2) + b"\x00\x08" + bytes(8) + b"\x01" + bytes(16))  # Vorlage 4.8, zu kurz
@example(bytes(2) + b"\x00\x0b" + bytes(8) + b"\x01" + bytes(16))  # Vorlage 4.11, zu kurz
def test_parse_head_beliebige_sektion4(rumpf: bytes) -> None:
    """Beliebiger Inhalt der Produktdefinition (Sektion 4, alle Vorlagen) ergibt einen Kopf
    oder ``HeaderError``.

    @relation(SWR-GRIB-008, scope=function)
    """
    daten = _mit_sektion4(rumpf)
    for complete in (False, True):
        try:
            parse_head(daten, complete=complete)
        except HeaderError:
            pass


@given(st.binary(max_size=512), st.integers(0, 10**12))
@example(b"7777GRIB" + bytes(8), 10**6)
def test_find_boundary_beliebige_bytes(chunk: bytes, rest: int) -> None:
    """Ein gefundener Anfang zeigt auf ``GRIB`` nach ``7777`` und passt in die Datei.

    @relation(SWR-GRIB-008, scope=function)
    """
    pos = find_boundary(chunk, rest)
    if pos is not None:
        assert chunk[pos - 4 : pos + 4] == b"7777GRIB"
        assert pos + 16 <= len(chunk) and pos < rest
