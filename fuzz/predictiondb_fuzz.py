"""Atheris-Fuzzer für Eingaben von außen (SYSR-SEC-009): GHCR-Artefakte und ENTSO-E.

Ein Ziel je Aufruf, die Eingabe ist ein beliebiger Byte-Strom:

    uv run --group fuzz python fuzz/predictiondb_fuzz.py tar -max_total_time=300 fuzz/korpus/tar

Ziele: ``tar`` (Layer-Tars, GEFS-Layer), ``manifest`` (Tags, Manifeste, idx-Layer) und
``entsoe`` (ENTSO-E-XML); der Startkorpus liegt jeweils unter ``fuzz/korpus/<ziel>``.

Erlaubt sind nur die vorgesehenen Fehler ``ArtifactError`` bzw. ``SourceError``. Jede andere
Ausnahme bricht ab, libFuzzer schreibt die Eingabe als ``crash-*`` ins Arbeitsverzeichnis.
Die Property-Tests ``test_robustheit.py`` in wx_exchange und energy_ingest prüfen dieselben
Eigenschaften kürzer bei jedem Pull Request.
"""

from __future__ import annotations

import contextlib
import json
import math
import sys
from datetime import UTC, datetime, timedelta

import atheris

with atheris.instrument_imports():
    from energy_ingest.sources import SourceError
    from energy_ingest.sources.entsoe import merge, parse_document
    from wx_exchange import gefs, idx
    from wx_exchange.artifact import MonthArtifact, content_hash, month_bounds, parse_tag, tag
    from wx_exchange.safe import ArtifactError, check_digest, read_tar, safe_name


def fuzz_tar(data: bytes) -> None:
    fdp = atheris.FuzzedDataProvider(data)
    compressed = fdp.ConsumeBool()
    roh = fdp.ConsumeBytes(fdp.remaining_bytes())
    try:
        files = read_tar(roh, compressed=compressed, max_bytes=64 * 1024**2)
    except ArtifactError:
        pass
    else:
        for name in files:
            if safe_name(name) != name:
                raise AssertionError(f"unsicherer Name {name!r}")
        if "zarr.json" in files:
            with contextlib.suppress(ArtifactError):
                content_hash(files)
    with contextlib.suppress(ArtifactError):
        gefs.unpack(roh)


_KEYS = [
    "layers", "annotations", "mediaType", "digest", "size",
    "org.predictiondb.run", "org.predictiondb.missing", "org.predictiondb.steps",
    "org.predictiondb.month", "org.predictiondb.hour", "org.predictiondb.unknown_names",
    "org.predictiondb.runs", "org.predictiondb.content",
]  # fmt: skip
_TEXTS = [
    "application/vnd.predictiondb.zarr.v3.tar", "application/vnd.predictiondb.gefs.v1.tar+gzip",
    "sha256:" + "ab" * 32, "2026-10-01T06:00:00+00:00", "2026-10-01T06:00:00", "2026100106",
    "2026-10", "0", "-1", "[]", '["a"]',
]  # fmt: skip


def _value(fdp: atheris.FuzzedDataProvider, depth: int = 0) -> object:
    """JSON-Wert aus den Fuzz-Bytes, mit den Schlüsseln und Texten der Manifeste."""
    kind = fdp.ConsumeIntInRange(0, 7 if depth < 4 else 4)
    if kind == 0:
        return None
    if kind == 1:
        return fdp.ConsumeInt(8)
    if kind == 2:
        return fdp.PickValueInList(_TEXTS)
    if kind == 3:
        return fdp.ConsumeUnicodeNoSurrogates(fdp.ConsumeIntInRange(0, 30))
    if kind == 4:
        return fdp.ConsumeFloat()
    if kind == 5:
        return [_value(fdp, depth + 1) for _ in range(fdp.ConsumeIntInRange(0, 3))]
    out = {}
    for _ in range(fdp.ConsumeIntInRange(0, 5)):
        key = fdp.PickValueInList(_KEYS) if fdp.ConsumeBool() else fdp.ConsumeUnicode(5)
        out[key] = _value(fdp, depth + 1)
    return out


def fuzz_manifest(data: bytes) -> None:
    fdp = atheris.FuzzedDataProvider(data)
    text = fdp.ConsumeUnicodeNoSurrogates(fdp.ConsumeIntInRange(0, 20))
    parsed = parse_tag(text)
    if parsed is not None:
        if tag(*parsed) != text:
            raise AssertionError(f"parse_tag({text!r}) = {parsed}")
        month_bounds(parsed[0])
    m = _value(fdp, 0)
    # einmal durch JSON, wie aus der Registry
    m = json.loads(json.dumps(m))
    for cls in (MonthArtifact, gefs.GefsMonth):
        try:
            art = cls.from_manifest("2026-10", 0, m)
        except ArtifactError:
            continue
        for run, layer in art.layers.items():
            if run.tzinfo is None:
                raise AssertionError(f"Lauf ohne Zeitzone: {run}")
            check_digest(layer.digest)
    layers = m.get("layers") if isinstance(m, dict) else None
    for d in layers if isinstance(layers, list) else [m]:
        with contextlib.suppress(ArtifactError):
            idx.IdxLayer.from_descriptor(d)


def fuzz_entsoe(data: bytes) -> None:
    try:
        series = parse_document(data)
        out = merge(series, datetime(1, 1, 2, tzinfo=UTC), datetime(9999, 12, 30, tzinfo=UTC))
    except SourceError:
        return
    if out is not None:
        res, pts = out
        if res <= timedelta(0) or not all(math.isfinite(v) for _, v in pts):
            raise AssertionError(f"ungültiges Ergebnis: {res} {pts[:3]}")


ZIELE = {"tar": fuzz_tar, "manifest": fuzz_manifest, "entsoe": fuzz_entsoe}


def main() -> None:
    if len(sys.argv) < 2 or sys.argv[1] not in ZIELE:
        sys.exit(f"Ziel fehlt: {' | '.join(ZIELE)}")
    ziel = ZIELE[sys.argv.pop(1)]
    atheris.Setup(sys.argv, ziel)
    atheris.Fuzz()


if __name__ == "__main__":
    main()
