"""Property-Tests mit Hypothesis: GHCR-Artefakte bei zufälligen und verfälschten Eingaben.

Manifeste und Layer kommen über GHCR von anderen Instanzen. Ungültiges darf nur zu
``ArtifactError`` führen, nie zu anderen Ausnahmen, zu Pfaden außerhalb des Ziels oder zu
stillschweigend falsch übernommenen Läufen. Der Fuzzer ``fuzz/predictiondb_fuzz.py`` prüft
dieselben Funktionen länger und abdeckungsgesteuert.
"""

from __future__ import annotations

import contextlib
import gzip
import io
import json
import tarfile
from datetime import UTC, datetime
from typing import Any

import numpy as np
import pytest
from hypothesis import HealthCheck, example, given, settings
from hypothesis import strategies as st
from obstore.store import MemoryStore

from wx_exchange import gefs, idx
from wx_exchange.artifact import (
    LAYER_TYPE,
    PREFIX,
    Layer,
    MonthArtifact,
    compare,
    content_hash,
    files_from_tar,
    month_bounds,
    parse_tag,
    tag,
    unpack,
)
from wx_exchange.safe import ArtifactError, check_digest, read_tar, safe_name

DIGEST = "sha256:" + "ab" * 32

_json = st.recursive(
    st.none() | st.booleans() | st.integers() | st.floats() | st.text(max_size=20),
    lambda kids: (
        st.lists(kids, max_size=4) | st.dictionaries(st.text(max_size=10), kids, max_size=4)
    ),
    max_leaves=20,
)
_name = st.text(alphabet=st.characters(blacklist_categories=["Cs"]), min_size=0, max_size=30)


def _tar(files: list[tuple[str, bytes]], *, compressed: bool = False) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for name, body in files:
            info = tarfile.TarInfo(name)
            info.size = len(body)
            tar.addfile(info, io.BytesIO(body))
    data = buf.getvalue()
    return gzip.compress(data, mtime=0) if compressed else data


def _valid_tar() -> bytes:
    return _tar([("zarr.json", b'{"attributes": {}}'), ("t2m/c/0/0", b"\x00" * 64)])


@given(st.binary(max_size=4096), st.booleans())
@example(b"", False)
@example(b"\x1f\x8b" + b"\x00" * 20, True)
@settings(max_examples=300)
def test_read_tar_beliebige_bytes(data: bytes, compressed: bool) -> None:
    """@relation(SWR-WXX-060, scope=function)"""
    try:
        files = read_tar(data, compressed=compressed)
    except ArtifactError:
        return
    for name in files:
        assert safe_name(name) == name


@given(st.lists(st.tuples(_name, st.binary(max_size=64)), max_size=6), st.booleans())
@example([("../etc/passwd", b"x")], False)
@example([("/abs", b"x")], True)
@example([("a//b", b"x")], False)
@example([("a", b"1"), ("a", b"2")], False)
@settings(max_examples=300, suppress_health_check=[HealthCheck.too_slow])
def test_read_tar_namen(files: list[tuple[str, bytes]], compressed: bool) -> None:
    """Nur sichere, eindeutige Namen kommen durch, und zwar unverändert.

    @relation(SWR-WXX-060, scope=function)
    """
    try:
        data = _tar(files, compressed=compressed)
    except (ValueError, UnicodeError):  # Namen, die tarfile selbst nicht schreibt
        return
    try:
        out = read_tar(data, compressed=compressed)
    except ArtifactError:
        return
    assert out == dict(files)
    assert len(out) == len(files)
    for name in out:
        parts = name.split("/")
        assert not name.startswith("/") and ".." not in parts and "" not in parts


@given(st.data())
@settings(max_examples=300)
def test_read_tar_verfaelscht(data: st.DataObject) -> None:
    """Abgeschnittene oder verfälschte gültige Tars.

    @relation(SWR-WXX-060, scope=function)
    """
    compressed = data.draw(st.booleans())
    raw = bytearray(_tar([("zarr.json", b"{}"), ("a/b", b"x" * 300)], compressed=compressed))
    for _ in range(data.draw(st.integers(0, 8))):
        raw[data.draw(st.integers(0, len(raw) - 1))] = data.draw(st.integers(0, 255))
    cut = data.draw(st.integers(0, len(raw)))
    with contextlib.suppress(ArtifactError):
        read_tar(bytes(raw[:cut]), compressed=compressed)


def test_read_tar_grenzen() -> None:
    """@relation(SWR-WXX-060, scope=function)"""
    data = _tar([(f"f{i}", b"x" * 10) for i in range(5)])
    assert len(read_tar(data)) == 5
    with pytest.raises(ArtifactError, match="Einträge"):
        read_tar(data, max_files=4)
    with pytest.raises(ArtifactError, match="größer"):
        read_tar(data, max_bytes=49)
    bombe = gzip.compress(b"\x00" * 10_000)
    with pytest.raises(ArtifactError, match="größer"):
        read_tar(bombe, compressed=True, max_bytes=1000)


def test_files_from_tar_lehnt_pfade_ab() -> None:
    """Bisher übernahm ``files_from_tar`` jeden Namen ungeprüft.

    @relation(SWR-WXX-060, scope=function)
    """
    with pytest.raises(ArtifactError):
        files_from_tar(_tar([("../x", b"1")]))
    assert files_from_tar(_valid_tar())["t2m/c/0/0"] == b"\x00" * 64


@given(st.binary(max_size=2048))
@settings(max_examples=200)
def test_silber_unpack(data: bytes) -> None:
    """Ein ungültiger Layer schreibt nichts, ein gültiger nur unter das Präfix.

    @relation(SWR-WXX-060, scope=function)
    """
    store = MemoryStore()
    try:
        unpack(data, store, "silver/run")
    except ArtifactError:
        return


@given(st.text(max_size=20))
@example("2026-10-06z")
@example("9999-12-00z")
@example("2026-1-00z")
@example("2026-10- 6z")
def test_parse_tag(text: str) -> None:
    """``parse_tag`` ist die Umkehrung von ``tag`` und liefert nur nutzbare Monate.

    @relation(SWR-WXX-060, scope=function)
    """
    parsed = parse_tag(text)
    if parsed is None:
        return
    assert tag(*parsed) == text
    a, b = month_bounds(parsed[0])
    assert a < b


@given(st.text(max_size=80))
@example("sha256:" + "0" * 64)
@example("sha256:../../tags/list")
def test_check_digest(text: str) -> None:
    """@relation(SWR-WXX-060, scope=function)"""
    try:
        assert check_digest(text) == text
    except ArtifactError:
        return
    assert len(text) == 71 and all(c in "0123456789abcdef" for c in text[7:])


def _descriptor() -> st.SearchStrategy[dict[str, Any]]:
    run = st.datetimes(
        min_value=datetime(2000, 1, 1),  # noqa: DTZ001 - Hypothesis will naive Grenzen
        max_value=datetime(2100, 1, 1),  # noqa: DTZ001
        timezones=st.just(UTC),
    )
    return st.fixed_dictionaries(
        {
            "mediaType": st.sampled_from([LAYER_TYPE, "x"]),
            "digest": st.one_of(st.just(DIGEST), _json),
            "size": st.one_of(st.integers(-5, 10**9), _json),
            "annotations": st.one_of(
                st.fixed_dictionaries(
                    {PREFIX + "run": st.one_of(run.map(datetime.isoformat), st.text(max_size=30))},
                    optional={
                        PREFIX + "steps": st.one_of(st.integers(-5, 500).map(str), st.text()),
                        PREFIX + "content": st.text(max_size=10),
                    },
                ),
                _json,
            ),
        }
    )


_manifest = st.one_of(
    _json,
    st.fixed_dictionaries(
        {"layers": st.one_of(st.lists(_descriptor(), max_size=4), _json)},
        optional={
            "annotations": st.one_of(
                st.fixed_dictionaries(
                    {PREFIX + "missing": st.text(max_size=30)},
                ),
                _json,
            )
        },
    ),
)


@given(_manifest)
@settings(max_examples=400, suppress_health_check=[HealthCheck.too_slow])
def test_manifest_silber(m: Any) -> None:
    """@relation(SWR-WXX-060, scope=function)"""
    try:
        art = MonthArtifact.from_manifest("2026-10", 0, m)
    except ArtifactError:
        return
    for run, layer in art.layers.items():
        assert run.tzinfo is not None
        check_digest(layer.digest)
        assert layer.size >= 0 and layer.steps >= 0


@given(_manifest)
@settings(max_examples=300, suppress_health_check=[HealthCheck.too_slow])
def test_manifest_gefs(m: Any) -> None:
    """@relation(SWR-WXX-060, scope=function)"""
    try:
        art = gefs.GefsMonth.from_manifest("2026-10", 0, m)
    except ArtifactError:
        return
    for run, layer in art.layers.items():
        assert run.tzinfo is not None
        check_digest(layer.digest)


@given(_json)
@example({"digest": DIGEST, "size": 1, "annotations": {PREFIX + "month": "2026-10"}})
@example(
    {
        "digest": DIGEST,
        "size": 1,
        "annotations": {
            PREFIX + "month": "2026-10",
            PREFIX + "hour": "0",
            PREFIX + "unknown_names": "[",
        },
    }
)
@settings(max_examples=300)
def test_idx_layer(d: Any) -> None:
    """@relation(SWR-WXX-060, scope=function)"""
    with contextlib.suppress(ArtifactError):
        idx.IdxLayer.from_descriptor(d)


def test_manifest_rundreise() -> None:
    """Was ``manifest`` schreibt, liest ``from_manifest`` unverändert zurück.

    @relation(SWR-WXX-060, scope=function)
    """
    run = datetime(2026, 10, 1, 6, tzinfo=UTC)
    art = MonthArtifact("2026-10", 6)
    art.layers[run] = Layer(run, DIGEST, 10, "c", "f", steps=3)
    art.missing.add(datetime(2026, 10, 2, 6, tzinfo=UTC))
    back = MonthArtifact.from_manifest("2026-10", 6, json.loads(json.dumps(art.manifest("x"))))
    assert back.layers == art.layers and back.missing == art.missing


@given(st.binary(max_size=200))
@example(b"[]")
@example(b'{"attributes": []}')
def test_zarr_json(raw: bytes) -> None:
    """Inhalts-Hash und Vergleich bei beliebigem ``zarr.json``.

    @relation(SWR-WXX-060, scope=function)
    """
    files = {"zarr.json": raw, "a/0": b"1"}
    try:
        content_hash(files)
        compare(files, {"zarr.json": b"{}"})
    except ArtifactError:
        pass


@given(st.binary(max_size=2048))
@settings(max_examples=200)
def test_gefs_unpack(data: bytes) -> None:
    """@relation(SWR-WXX-060, scope=function)"""
    try:
        meta, values = gefs.unpack(data)
    except ArtifactError:
        return
    assert isinstance(meta, dict) and values.dtype == np.float32


def test_gefs_unpack_fehlende_datei() -> None:
    """@relation(SWR-WXX-060, scope=function)"""
    with pytest.raises(ArtifactError, match="unvollständig"):
        gefs.unpack(_tar([("meta.json", b"{}")], compressed=True))
    buf = io.BytesIO()
    np.save(buf, np.array(["a"]), allow_pickle=False)
    with pytest.raises(ArtifactError, match="Zahlen"):
        gefs.unpack(_tar([("meta.json", b"{}"), ("values.npy", buf.getvalue())], compressed=True))
