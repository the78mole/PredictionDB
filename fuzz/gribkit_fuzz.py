"""Atheris-Fuzzer für die Parser in gribkit (SYSR-SEC-009).

Ein Ziel je Aufruf, die Eingabe ist ein beliebiger Byte-Strom:

    uv run --group fuzz python fuzz/gribkit_fuzz.py idx  -max_total_time=300 fuzz/korpus/idx
    uv run --group fuzz python fuzz/gribkit_fuzz.py head -max_total_time=300 fuzz/korpus/head

Erlaubt sind nur die vorgesehenen Fehler ``IdxParseError`` bzw. ``HeaderError``. Jede andere
Ausnahme bricht ab, libFuzzer schreibt die Eingabe als ``crash-*`` ins Arbeitsverzeichnis.
Die Property-Tests in ``packages/gribkit/tests/test_robustheit.py`` prüfen dieselben
Eigenschaften kürzer bei jedem Pull Request.
"""

from __future__ import annotations

import sys
from itertools import pairwise

import atheris

with atheris.instrument_imports():
    from gribkit import HeaderError, find_boundary, parse_head
    from gribkit.headers import entry, idx_line
    from gribkit.idx import IdxParseError, merge_ranges, parse_idx


def fuzz_idx(data: bytes) -> None:
    fdp = atheris.FuzzedDataProvider(data)
    groesse = fdp.ConsumeIntInRange(0, 2**40) if fdp.ConsumeBool() else None
    luecke = fdp.ConsumeIntInRange(0, 2**30)
    text = fdp.ConsumeUnicodeNoSurrogates(fdp.remaining_bytes())
    try:
        eintraege = parse_idx(text, groesse)
    except IdxParseError:
        return
    for e in eintraege:
        if e.length is not None and e.length <= 0:
            raise AssertionError(f"Länge {e.length} für {e}")
    bereiche = merge_ranges(eintraege, max_gap=luecke)
    for a, b in pairwise(bereiche):
        if a.end is None or a.end >= b.start:
            raise AssertionError(f"Bereiche überlappen: {a.header} {b.header}")


def fuzz_head(data: bytes) -> None:
    fdp = atheris.FuzzedDataProvider(data)
    complete = fdp.ConsumeBool()
    rest = fdp.ConsumeIntInRange(0, 2**40)
    roh = fdp.ConsumeBytes(fdp.remaining_bytes())
    try:
        head = parse_head(roh, complete=complete)
    except HeaderError:
        pass
    else:
        idx_line(entry(1, 0, head, "2026010100"))
    pos = find_boundary(roh, rest)
    if pos is not None and roh[pos - 4 : pos + 4] != b"7777GRIB":
        raise AssertionError(f"Grenze {pos} zeigt nicht auf 7777GRIB")


ZIELE = {"idx": fuzz_idx, "head": fuzz_head}


def main() -> None:
    if len(sys.argv) < 2 or sys.argv[1] not in ZIELE:
        sys.exit(f"Ziel fehlt: {' | '.join(ZIELE)}")
    ziel = ZIELE[sys.argv.pop(1)]
    atheris.Setup(sys.argv, ziel)
    atheris.Fuzz()


if __name__ == "__main__":
    main()
