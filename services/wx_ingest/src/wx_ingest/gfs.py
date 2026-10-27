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
    IdxEntry,
    MessageSpec,
    crop,
    describe,
    iter_messages,
    merge_ranges,
    missing_specs,
    parse_idx,
    select,
)
from predictiondb_core.log import get_logger
from wx_ingest.config import EndpointConfig

log = get_logger(__name__)

#: NOMADS liefert einfache Packung. CCSDS komprimiert dieselben quantisierten Werte
#: verlustfrei (etwa 40 % kleiner); komplexe Packung würde hier Bits verlieren.
NOMADS_PACKING = "grid_ccsds"

#: HTTP-Status, bei denen eine Wiederholung sinnvoll ist.
RETRY_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})

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

        expected = sum(len(r.entries) for r in ranges)
        try:
            raw = b"".join(await asyncio.gather(*(load(r) for r in ranges)))
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
