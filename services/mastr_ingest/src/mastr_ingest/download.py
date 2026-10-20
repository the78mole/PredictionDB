"""Gesamtdatenexport finden, laden und unverändert in Garage ablegen (``assets-bronze``).

Schlüssel::

    mastr/2026-10-01/Gesamtdatenexport_20261001_26.1.zip
    mastr/2026-10-01/manifest.json        # URL, Release, SHA-256, Größe; erst nach dem ZIP

Ein Export gilt nur als gesichert, wenn sein Manifest existiert.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import httpx
import obstore
from obstore.store import ObjectStore
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from predictiondb_core.log import get_logger

log = get_logger(__name__)

USER_AGENT = "PredictionDB/0.1 (+https://github.com/the78mole/PredictionDB)"
_LINK = re.compile(r"Gesamtdatenexport_(?P<date>\d{8})_(?P<version>\d{2}\.\d)\.zip")


class ExportUnavailable(RuntimeError):
    """Für den Tag ist (noch) kein Export abrufbar."""


def mastr_release(day: date) -> str:
    """MaStR-Release, in dessen Format der Export eines Tages erscheint.

    Releases wechseln am 1. April und 1. Oktober; der Export dieses Tages entsteht nachts
    und hat noch das alte Format. 2026-04-01 → 25.2, 2026-04-02 → 26.1, 2026-10-02 → 26.2.
    """
    year, release = day.year, 1
    if (day.month, day.day) <= (4, 1):
        year, release = year - 1, 2
    elif (day.month, day.day) > (10, 1):
        release = 2
    return f"{year % 100:02d}.{release}"


def _neighbours(version: str) -> list[str]:
    y, r = (int(p) for p in version.split("."))
    after = f"{y:02d}.2" if r == 1 else f"{(y + 1) % 100:02d}.1"
    before = f"{(y - 1) % 100:02d}.2" if r == 1 else f"{y:02d}.1"
    return [version, after, before]


def candidate_urls(template: str, day: date) -> list[str]:
    return [
        template.format(date=f"{day:%Y%m%d}", version=v) for v in _neighbours(mastr_release(day))
    ]


def links_in_page(html: str) -> list[tuple[date, str]]:
    """Export-Links der Datendownload-Seite als ``(Exportdatum, Release)``."""
    return sorted({parse_export_name(m[0]) for m in _LINK.finditer(html)}, reverse=True)


def parse_export_name(name: str) -> tuple[date, str]:
    """``Gesamtdatenexport_20261001_26.1.zip`` → ``(2026-10-01, "26.1")``."""
    m = _LINK.search(name)
    if m is None:
        raise ValueError(f"{name!r} ist kein Gesamtdatenexport-Dateiname")
    return datetime.strptime(m["date"], "%Y%m%d").date(), m["version"]  # noqa: DTZ007


@dataclass
class ExportManifest:
    export_date: str
    version: str
    url: str
    object_key: str
    sha256: str = ""
    size: int = 0
    stored_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat(timespec="seconds"))


class Bronze:
    def __init__(self, store: ObjectStore, prefix: str = "mastr") -> None:
        self.store = store
        self.prefix = prefix

    def zip_key(self, day: date, version: str) -> str:
        return f"{self.prefix}/{day.isoformat()}/Gesamtdatenexport_{day:%Y%m%d}_{version}.zip"

    def manifest_key(self, day: date) -> str:
        return f"{self.prefix}/{day.isoformat()}/manifest.json"

    def manifest(self, day: date) -> ExportManifest | None:
        try:
            raw = bytes(obstore.get(self.store, self.manifest_key(day)).bytes())
        except FileNotFoundError:
            return None
        data: dict[str, Any] = json.loads(raw)
        return ExportManifest(**data)

    def put(self, path: Path, manifest: ExportManifest) -> ExportManifest:
        manifest.sha256 = sha256_file(path)
        manifest.size = path.stat().st_size
        obstore.put(self.store, manifest.object_key, path)
        obstore.put(
            self.store,
            self.manifest_key(date.fromisoformat(manifest.export_date)),
            json.dumps(asdict(manifest), indent=1).encode(),
        )
        log.info("mastr.bronze.stored", key=manifest.object_key, size=manifest.size)
        return manifest

    def fetch(self, manifest: ExportManifest, path: Path) -> Path:
        """Holt das ZIP aus Garage, falls es lokal fehlt oder nicht passt."""
        if path.exists() and path.stat().st_size == manifest.size:
            return path
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".part")
        with tmp.open("wb") as f:
            for chunk in obstore.get(self.store, manifest.object_key).stream():
                f.write(chunk)
        tmp.rename(path)
        return path


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


class Downloader:
    def __init__(self, client: httpx.Client, page_url: str, template: str) -> None:
        self.client = client
        self.page_url = page_url
        self.template = template

    def discover(self, day: date) -> tuple[str, str]:
        """URL und Release des Exports von ``day``: erst Link der Seite, dann Namensschema."""
        try:
            page = self.client.get(self.page_url, headers={"User-Agent": USER_AGENT})
            page.raise_for_status()
            for d, version in links_in_page(page.text):
                if d == day:
                    return self.template.format(date=f"{d:%Y%m%d}", version=version), version
        except httpx.HTTPError as e:
            log.warning("mastr.discover.page_failed", url=self.page_url, error=str(e))
        for url in candidate_urls(self.template, day):
            r = self.client.head(url, headers={"User-Agent": USER_AGENT})
            if r.status_code == 200:
                m = _LINK.search(url)
                assert m is not None
                return url, m["version"]
            log.debug("mastr.discover.miss", url=url, status=r.status_code)
        raise ExportUnavailable(f"kein Gesamtdatenexport für {day} gefunden")

    @retry(
        retry=retry_if_exception_type((httpx.TransportError, httpx.RemoteProtocolError)),
        stop=stop_after_attempt(4),
        wait=wait_exponential(min=5, max=120),
        reraise=True,
    )
    def download(self, url: str, path: Path) -> Path:
        """Lädt nach ``path`` (setzt einen abgebrochenen Download per Range fort)."""
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".part")
        offset = tmp.stat().st_size if tmp.exists() else 0
        headers = {"User-Agent": USER_AGENT}
        if offset:
            headers["Range"] = f"bytes={offset}-"
        with self.client.stream("GET", url, headers=headers) as r:
            if r.status_code == 416:  # schon vollständig
                tmp.rename(path)
                return path
            r.raise_for_status()
            mode = "ab" if offset and r.status_code == 206 else "wb"
            expected = r.headers.get("Content-Length")
            log.info(
                "mastr.download.start",
                url=url,
                offset=offset if mode == "ab" else 0,
                bytes=expected,
            )
            with tmp.open(mode) as f:
                for chunk in r.iter_bytes(1 << 20):
                    f.write(chunk)
        tmp.rename(path)
        log.info("mastr.download.done", url=url, size=path.stat().st_size)
        return path
