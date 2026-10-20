from datetime import date
from pathlib import Path

import httpx
import pytest
from obstore.store import MemoryStore

from mastr_ingest.download import (
    Bronze,
    Downloader,
    ExportManifest,
    ExportUnavailable,
    candidate_urls,
    links_in_page,
    mastr_release,
    parse_export_name,
)

TEMPLATE = "https://download.test/Gesamtdatenexport_{date}_{version}.zip"
PAGE = "https://www.test/MaStR/Datendownload"


@pytest.mark.parametrize(
    ("day", "version"),
    [
        (date(2026, 1, 1), "25.2"),
        (date(2026, 4, 1), "25.2"),
        (date(2026, 4, 2), "26.1"),
        (date(2026, 10, 1), "26.1"),
        (date(2026, 10, 2), "26.2"),
        (date(2026, 12, 31), "26.2"),
    ],
)
def test_release(day: date, version: str) -> None:
    assert mastr_release(day) == version


def test_candidates_and_names() -> None:
    assert candidate_urls(TEMPLATE, date(2026, 10, 20)) == [
        "https://download.test/Gesamtdatenexport_20261020_26.2.zip",
        "https://download.test/Gesamtdatenexport_20261020_27.1.zip",
        "https://download.test/Gesamtdatenexport_20261020_26.1.zip",
    ]
    assert parse_export_name("x/Gesamtdatenexport_20261001_26.1.zip") == (date(2026, 10, 1), "26.1")
    with pytest.raises(ValueError):
        parse_export_name("export.zip")
    html = '<a href="https://download.test/Gesamtdatenexport_20261020_26.2.zip">Download</a>'
    assert links_in_page(html + html) == [(date(2026, 10, 20), "26.2")]


def _client(handler: httpx.MockTransport) -> httpx.Client:
    return httpx.Client(transport=handler)


def test_discover_prefers_page_link() -> None:
    def handle(r: httpx.Request) -> httpx.Response:
        if str(r.url) == PAGE:
            return httpx.Response(200, text='href="/x/Gesamtdatenexport_20261020_27.1.zip"')
        return httpx.Response(404)

    d = Downloader(_client(httpx.MockTransport(handle)), PAGE, TEMPLATE)
    assert d.discover(date(2026, 10, 20)) == (
        "https://download.test/Gesamtdatenexport_20261020_27.1.zip",
        "27.1",
    )


def test_discover_falls_back_to_head() -> None:
    def handle(r: httpx.Request) -> httpx.Response:
        if str(r.url) == PAGE:
            return httpx.Response(403)
        if r.method == "HEAD" and r.url.path.endswith("_26.1.zip"):
            return httpx.Response(200)
        return httpx.Response(404)

    d = Downloader(_client(httpx.MockTransport(handle)), PAGE, TEMPLATE)
    url, version = d.discover(date(2026, 10, 20))
    assert version == "26.1" and url.endswith("20261020_26.1.zip")

    d_none = Downloader(_client(httpx.MockTransport(lambda r: httpx.Response(404))), PAGE, TEMPLATE)
    with pytest.raises(ExportUnavailable):
        d_none.discover(date(2026, 10, 20))


def test_download_resumes(tmp_path: Path) -> None:
    payload = bytes(range(256)) * 100

    def handle(r: httpx.Request) -> httpx.Response:
        rng = r.headers.get("Range")
        if rng:
            start = int(rng.removeprefix("bytes=").rstrip("-"))
            return httpx.Response(206, content=payload[start:])
        return httpx.Response(200, content=payload)

    target = tmp_path / "x.zip"
    target.with_suffix(".part").write_bytes(payload[:1000])
    d = Downloader(_client(httpx.MockTransport(handle)), PAGE, TEMPLATE)
    assert d.download("https://download.test/x.zip", target).read_bytes() == payload


def test_bronze_roundtrip(tmp_path: Path) -> None:
    bronze = Bronze(MemoryStore())
    day = date(2026, 10, 1)
    src = tmp_path / "export.zip"
    src.write_bytes(b"PK" + b"\0" * 5000)
    assert bronze.manifest(day) is None
    m = bronze.put(
        src, ExportManifest(day.isoformat(), "26.1", "https://x", bronze.zip_key(day, "26.1"))
    )
    assert m.object_key == "mastr/2026-10-01/Gesamtdatenexport_20261001_26.1.zip"
    assert m.size == 5002 and len(m.sha256) == 64
    assert bronze.manifest(day) == m
    out = bronze.fetch(m, tmp_path / "copy" / "export.zip")
    assert out.read_bytes() == src.read_bytes()
