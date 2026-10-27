"""Download eines GFS-Prognoseschritts über einen Endpunkt.

Zwei Mechaniken (``docs/ARCHITEKTUR.md`` 4.1):

- ``idx_range`` (AWS ``noaa-gfs-bdp-pds``, Google ``global-forecast-system``):
  ``.idx`` laden, Nachrichten wählen, je Byte-Bereich eine Range-Anfrage,
  danach lokal auf den Ausschnitt zuschneiden. Diese Speicher beantworten
  Multi-Range-Anfragen mit der ganzen Datei, deshalb eine Anfrage je Bereich.
  Passt die ``.idx`` nicht zur GRIB-Datei (im Archiv wurden einzelne Dateien nachträglich neu
  geschrieben, die ``.idx`` blieb alt, z. B. 2022-11-30 00Z), wird die ganze Datei geladen
  und die Nachrichten werden über ihre Position den Zeilen der ``.idx`` zugeordnet.
- ``nomads_filter``: NOMADS Grib-Filter schneidet serverseitig zu. Der Filter
  kombiniert jede angeforderte Variable mit jedem angeforderten Level und kennt
  keinen Filter auf das Prognosefeld; er liefert daher eine Obermenge.
- ``header_scan`` (NCAR GDEX d084001, GFS vor v16): Archive ohne ``.idx``. Das Inventar entsteht
  aus den Nachrichtenköpfen (:mod:`gribkit.headers`), je Nachricht eine kleine Range-Anfrage.
  Damit das nicht Nachricht für Nachricht nacheinander läuft, wird die Datei in Abschnitte
  geteilt; an jedem Schnitt wird der nächste Nachrichtenanfang gesucht und die Abschnitte
  werden gleichzeitig abgelaufen. Danach weiter wie bei ``idx_range``. Passen die Abschnitte
  nicht aneinander (Fehltreffer beim Suchen), wird die ganze Datei geladen.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import httpx
from aiolimiter import AsyncLimiter
from tenacity import AsyncRetrying, retry_if_exception, stop_after_attempt, wait_exponential

from gribkit import (
    BBox,
    ByteRange,
    GribError,
    HeaderError,
    IdxEntry,
    MessageSpec,
    crop,
    describe,
    find_boundary,
    iter_messages,
    merge_ranges,
    missing_specs,
    parse_head,
    parse_idx,
    select,
)
from gribkit.headers import HEAD_BYTES, Head, entry
from predictiondb_core.log import get_logger
from wx_ingest.config import EndpointConfig

log = get_logger(__name__)

#: NOMADS liefert einfache Packung. CCSDS komprimiert dieselben quantisierten Werte
#: verlustfrei (etwa 40 % kleiner); komplexe Packung würde hier Bits verlieren.
NOMADS_PACKING = "grid_ccsds"

#: HTTP-Status, bei denen eine Wiederholung sinnvoll ist.
RETRY_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})

#: Fenster beim Suchen eines Nachrichtenanfangs (GFS-Nachrichten sind meist 0,2 bis 1 MB)
SCAN_WINDOW = 256 * 1024

#: Ganze GRIB-Dateien (je rund 540 MB) nur einzeln im Speicher, über alle Endpunkte und Läufe;
#: mehrere gleichzeitig ließen den Actions-Runner abstürzen
_FULL_DOWNLOAD = asyncio.Semaphore(1)


class StepUnavailable(Exception):
    """Der Schritt ist auf diesem Endpunkt (noch) nicht veröffentlicht."""


class FetchError(Exception):
    """Der Schritt ist vorhanden, der Download ist aber fehlgeschlagen."""


class StaleIndex(FetchError):
    """Die Byte-Bereiche aus der ``.idx`` treffen keine GRIB-Nachrichten."""


class _RetryableStatus(Exception):
    def __init__(self, response: httpx.Response) -> None:
        super().__init__(f"HTTP {response.status_code} für {response.request.url}")
        self.response = response


@dataclass
class StepData:
    grib: bytes
    url: str
    n_messages: int
    messages: list[str] = field(default_factory=list)
    absent: list[str] = field(default_factory=list)


def gfs_file(product: str, run: datetime, step: int) -> str:
    return f"gfs.t{run:%H}z.{product}.f{step:03d}"


def gfs_dir(run: datetime) -> str:
    return f"gfs.{run:%Y%m%d}/{run:%H}/atmos"


class Endpoint:
    """HTTP-Zugriff auf einen Endpunkt mit Nebenläufigkeits- und Ratenbegrenzung."""

    def __init__(
        self,
        config: EndpointConfig,
        client: httpx.AsyncClient,
        *,
        retry_attempts: int = 4,
        retry_wait_max: float = 30.0,
    ) -> None:
        self.config = config
        self.client = client
        self._sem = asyncio.Semaphore(config.concurrency)
        self._limiter = AsyncLimiter(config.rate_per_min, 60) if config.rate_per_min else None
        self._retry_attempts = retry_attempts
        self._retry_wait_max = retry_wait_max

    @property
    def name(self) -> str:
        return self.config.name

    async def get(self, url: str, **kwargs: Any) -> httpx.Response:
        retrying = AsyncRetrying(
            retry=retry_if_exception(
                lambda e: isinstance(e, (httpx.TransportError, _RetryableStatus))
            ),
            stop=stop_after_attempt(self._retry_attempts),
            wait=wait_exponential(multiplier=1, max=self._retry_wait_max),
            reraise=True,
        )
        try:
            async for attempt in retrying:
                with attempt:
                    async with self._sem:
                        if self._limiter:
                            await self._limiter.acquire()
                        response = await self.client.get(url, **kwargs)
                    if response.status_code in RETRY_STATUS:
                        raise _RetryableStatus(response)
                    return response
        except _RetryableStatus as exc:
            return exc.response
        raise AssertionError("unreachable")

    async def fetch_step(
        self, product: str, run: datetime, step: int, specs: list[MessageSpec], bbox: BBox
    ) -> StepData:
        if self.config.kind == "idx_range":
            return await self._fetch_idx_range(product, run, step, specs, bbox)
        if self.config.kind == "header_scan":
            return await self._fetch_header_scan(run, step, specs, bbox)
        return await self._fetch_nomads(product, run, step, specs, bbox)

    async def _fetch_idx_range(
        self, product: str, run: datetime, step: int, specs: list[MessageSpec], bbox: BBox
    ) -> StepData:
        url = f"{self.config.url.rstrip('/')}/{gfs_dir(run)}/{gfs_file(product, run, step)}"
        idx_resp = await self.get(url + ".idx")
        if idx_resp.status_code in (403, 404):
            raise StepUnavailable(url)
        if idx_resp.status_code != 200:
            raise FetchError(f"HTTP {idx_resp.status_code} für {url}.idx")

        entries = parse_idx(idx_resp.text)
        chosen = select(entries, specs)
        if not chosen:
            raise FetchError(f"keine der angeforderten Nachrichten in {url}.idx")
        ranges = merge_ranges(chosen, max_gap=self.config.max_gap)
        expected = sum(len(r.entries) for r in ranges)
        try:
            raw = await self._load_ranges(url, ranges)
            grib = await asyncio.to_thread(_crop_checked, raw, bbox, expected)
        except StaleIndex as exc:
            log.warning("idx_stale", url=url, error=str(exc))
            grib = await self._fetch_realigned(
                url, entries, [e for r in ranges for e in r.entries], bbox
            )
        return StepData(
            grib=grib,
            url=url,
            n_messages=expected,
            messages=[e.key for r in ranges for e in r.entries],
            absent=[s.label for s in missing_specs(entries, specs)],
        )

    async def _load_ranges(self, url: str, ranges: list[ByteRange]) -> bytes:
        async def load(r: ByteRange) -> bytes:
            resp = await self.get(url, headers={"Range": r.header})
            if resp.status_code == 404:
                # .idx schon da, GRIB-Datei noch nicht vollständig hochgeladen
                raise StepUnavailable(url)
            if resp.status_code == 416:
                raise StaleIndex(f"Range {r.header} liegt hinter dem Ende von {url}")
            if resp.status_code != 206:
                raise FetchError(f"Range {r.header} auf {url}: HTTP {resp.status_code} statt 206")
            try:
                return r.extract(resp.content)
            except ValueError as exc:
                raise FetchError(str(exc)) from exc

        return b"".join(await asyncio.gather(*(load(r) for r in ranges)))

    # --- header_scan ---------------------------------------------------------------------------

    async def _range(self, url: str, start: int, end: int) -> httpx.Response:
        resp = await self.get(url, headers={"Range": f"bytes={start}-{end}"})
        if resp.status_code in (403, 404):
            raise StepUnavailable(url)
        if resp.status_code != 206:
            raise FetchError(f"Range {start}-{end} auf {url}: HTTP {resp.status_code} statt 206")
        return resp

    async def _boundary(self, url: str, at: int, size: int) -> int | None:
        """Erster Nachrichtenanfang ab ``at`` (``None``, wenn bis Dateiende keiner folgt)."""
        off = at
        while off < size:
            end = min(size, off + SCAN_WINDOW) - 1
            chunk = (await self._range(url, off, end)).content
            found = find_boundary(chunk, size - off)
            if found is not None:
                return off + found
            off = end + 1 - 16  # Überlappung, damit ``7777GRIB`` am Rand nicht verloren geht
            if end + 1 >= size:
                break
        return None

    async def _walk(
        self, url: str, start: int, stop: int, first: bytes | None = None
    ) -> list[tuple[int, Head]]:
        """Köpfe der Nachrichten von ``start`` bis genau ``stop``."""
        out: list[tuple[int, Head]] = []
        off = start
        while off < stop:
            if off == 0 and first is not None:
                data = first
            else:
                data = (await self._range(url, off, off + HEAD_BYTES - 1)).content
            try:
                head = parse_head(data)
            except HeaderError as exc:
                raise StaleIndex(f"Kopf an Byte {off} von {url}: {exc}") from exc
            out.append((off, head))
            off += head.length
        if off != stop:
            raise StaleIndex(f"Köpfe ab Byte {start} enden bei {off} statt {stop} in {url}")
        return out

    async def _size(self, url: str, first: httpx.Response) -> int:
        """Dateigröße aus ``Content-Range``, sonst (NCAR-Ursprung) aus einer HEAD-Anfrage."""
        try:
            return int(first.headers["content-range"].rsplit("/", 1)[1])
        except (KeyError, ValueError, IndexError):
            pass
        async with self._sem:
            head = await self.client.head(url)
        try:
            return int(head.headers["content-length"])
        except (KeyError, ValueError) as exc:
            raise FetchError(f"{url}: Dateigröße weder in Content-Range noch per HEAD") from exc

    async def scan(self, url: str) -> tuple[str, list[IdxEntry]]:
        """Inventar einer Datei aus ihren Köpfen; liefert die URL nach Weiterleitungen mit."""
        first = await self._range(url, 0, HEAD_BYTES - 1)
        # OSDF leitet auf einen Cache weiter; der bleibt für die Datei derselbe
        url = str(first.url)
        size = await self._size(url, first)
        n = max(1, min(self.config.segments, size // (4 * SCAN_WINDOW)))
        cuts = [size * i // n for i in range(1, n)]
        found = await asyncio.gather(*(self._boundary(url, c, size) for c in cuts))
        bounds = sorted({0, *(b for b in found if b is not None)})
        parts = await asyncio.gather(
            *(
                self._walk(url, a, b, first.content)
                for a, b in zip(bounds, [*bounds[1:], size], strict=True)
            )
        )
        heads = [h for part in parts for h in part]
        return url, [entry(i, off, h) for i, (off, h) in enumerate(heads, start=1)]

    async def _fetch_header_scan(
        self, run: datetime, step: int, specs: list[MessageSpec], bbox: BBox
    ) -> StepData:
        assert self.config.path is not None
        url = f"{self.config.url.rstrip('/')}/{self.config.path.format(run=run, step=step)}"
        try:
            final, entries = await self.scan(url)
            chosen = select(entries, specs)
            if not chosen:
                raise FetchError(f"keine der angeforderten Nachrichten in {url}")
            ranges = merge_ranges(chosen, max_gap=self.config.max_gap)
            raw = await self._load_ranges(final, ranges)
            grib = await asyncio.to_thread(_crop_checked, raw, bbox, len(chosen))
        except StaleIndex as exc:
            log.warning("scan_failed", url=url, error=str(exc))
            entries, chosen, grib = await self._fetch_whole_scanned(url, specs, bbox)
        return StepData(
            grib=grib,
            url=url,
            n_messages=len(chosen),
            messages=[e.key for e in chosen],
            absent=[s.label for s in missing_specs(entries, specs)],
        )

    async def _fetch_whole_scanned(
        self, url: str, specs: list[MessageSpec], bbox: BBox
    ) -> tuple[list[IdxEntry], list[IdxEntry], bytes]:
        """Ganze Datei laden und die Köpfe im Speicher lesen (ohne Fehltreffer)."""
        async with _FULL_DOWNLOAD:
            resp = await self.get(url)
            if resp.status_code in (403, 404):
                raise StepUnavailable(url)
            if resp.status_code != 200:
                raise FetchError(f"ganze Datei {url}: HTTP {resp.status_code}")
            data = resp.content
            del resp
            entries = []
            try:
                for i, (start, _end) in enumerate(_message_bounds(data), start=1):
                    entries.append(entry(i, start, parse_head(data[start : start + HEAD_BYTES])))
            except HeaderError as exc:
                raise FetchError(f"{url}: {exc}") from exc
            chosen = select(entries, specs)
            if not chosen:
                raise FetchError(f"keine der angeforderten Nachrichten in {url}")
            raw = b"".join(data[e.offset : e.offset + (e.length or 0)] for e in chosen)
            del data
        try:
            grib = await asyncio.to_thread(crop, raw, bbox)
        except GribError as exc:
            raise FetchError(str(exc)) from exc
        log.info("scan_whole_file", url=url, messages=len(chosen))
        return entries, chosen, grib

    async def _fetch_realigned(
        self, url: str, entries: list[IdxEntry], chosen: list[IdxEntry], bbox: BBox
    ) -> bytes:
        async with _FULL_DOWNLOAD:
            resp = await self.get(url)
            if resp.status_code != 200:
                raise FetchError(f"ganze Datei {url}: HTTP {resp.status_code}")
            data = resp.content
            del resp
            grib = await asyncio.to_thread(_realign, data, entries, chosen, bbox)
            del data
        log.info("idx_realigned", url=url, messages=len(chosen))
        return grib

    async def _fetch_nomads(
        self, product: str, run: datetime, step: int, specs: list[MessageSpec], bbox: BBox
    ) -> StepData:
        params = nomads_params(product, run, step, specs, bbox)
        resp = await self.get(self.config.url, params=params)
        body = resp.content
        if resp.status_code == 200 and body.startswith(b"GRIB"):
            try:
                n = sum(1 for _ in iter_messages(body))
                # NOMADS tastet Süd→Nord ab; crop dreht auf Nord→Süd wie bei idx_range
                grib = await asyncio.to_thread(crop, body, bbox, NOMADS_PACKING)
            except GribError as exc:
                raise FetchError(f"NOMADS-Antwort unbrauchbar: {exc}") from exc
            return StepData(grib=grib, url=str(resp.request.url), n_messages=n)
        text = body[:2000].decode("utf-8", "replace").lower()
        if resp.status_code == 404 or "not present" in text or "no such file" in text:
            raise StepUnavailable(str(resp.request.url))
        raise FetchError(f"NOMADS: HTTP {resp.status_code}, keine GRIB-Daten")


def nomads_params(
    product: str, run: datetime, step: int, specs: list[MessageSpec], bbox: BBox
) -> dict[str, str]:
    params = {"dir": f"/{gfs_dir(run)}", "file": gfs_file(product, run, step)}
    for s in specs:
        params[f"var_{s.var}"] = "on"
        params["lev_" + s.level.replace(" ", "_")] = "on"
    params.update(
        subregion="",
        toplat=f"{bbox.north:g}",
        bottomlat=f"{bbox.south:g}",
        leftlon=f"{bbox.west:g}",
        rightlon=f"{bbox.east:g}",
    )
    return params


def _crop_checked(raw: bytes, bbox: BBox, expected: int) -> bytes:
    try:
        n = sum(1 for _ in iter_messages(raw))
    except GribError as exc:
        raise StaleIndex(str(exc)) from exc
    if n != expected:
        raise StaleIndex(f"{n} GRIB-Nachrichten erhalten, {expected} erwartet")
    try:
        return crop(raw, bbox)
    except GribError as exc:
        raise FetchError(str(exc)) from exc


def _message_bounds(data: bytes) -> list[tuple[int, int]]:
    """Anfang und Ende jeder GRIB2-Nachricht, ohne die Nachrichten zu kopieren."""
    bounds = []
    pos, n = 0, len(data)
    while pos < n:
        if data[pos : pos + 4] != b"GRIB" or n - pos < 16:
            raise FetchError(f"GRIB-Datei unbrauchbar: keine Nachricht an Byte {pos}")
        end = pos + int.from_bytes(data[pos + 8 : pos + 16], "big")
        if end > n or data[end - 4 : end] != b"7777":
            raise FetchError(f"GRIB-Datei unbrauchbar: Nachricht an Byte {pos} unvollständig")
        bounds.append((pos, end))
        pos = end
    return bounds


def _realign(data: bytes, entries: list[IdxEntry], chosen: list[IdxEntry], bbox: BBox) -> bytes:
    """Wählt die Nachrichten aus der ganzen Datei über ihre Position in der ``.idx``.

    Das gilt nur, wenn die Datei genau so viele Nachrichten hat wie die ``.idx`` Offsets;
    zusätzlich muss der Prognosezeitraum jeder gewählten Nachricht zur Zeile passen.
    """
    messages = _message_bounds(data)
    offsets = sorted({e.offset for e in entries})
    if len(messages) != len(offsets):
        raise FetchError(
            f".idx veraltet: {len(messages)} Nachrichten in der Datei, {len(offsets)} im Index"
        )
    position = {o: i for i, o in enumerate(offsets)}
    picked = []
    for e in chosen:
        start, end = messages[position[e.offset]]
        msg = data[start:end]
        m = re.match(r"(\d+(?:-\d+)?) hour", e.fcst)
        if m and describe(msg).step_range.split("-")[-1] != m[1].split("-")[-1]:
            raise FetchError(f".idx veraltet: {e.key} passt nicht zur Nachricht an Position")
        picked.append(msg)
    try:
        return crop(b"".join(picked), bbox)
    except GribError as exc:
        raise FetchError(str(exc)) from exc
