"""Download eines GFS-Prognoseschritts über einen Endpunkt.

Zwei Mechaniken (``docs/ARCHITEKTUR.md`` 4.1):

- ``idx_range`` (AWS ``noaa-gfs-bdp-pds``, Google ``global-forecast-system``):
  ``.idx`` laden, Nachrichten wählen, je Byte-Bereich eine Range-Anfrage,
  danach lokal auf den Ausschnitt zuschneiden. Diese Speicher beantworten
  Multi-Range-Anfragen mit der ganzen Datei, deshalb eine Anfrage je Bereich.
- ``nomads_filter``: NOMADS Grib-Filter schneidet serverseitig zu. Der Filter
  kombiniert jede angeforderte Variable mit jedem angeforderten Level und kennt
  keinen Filter auf das Prognosefeld; er liefert daher eine Obermenge.
"""

from __future__ import annotations

import asyncio
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
    MessageSpec,
    crop,
    iter_messages,
    merge_ranges,
    missing_specs,
    parse_idx,
    select,
)
from wx_ingest.config import EndpointConfig

#: HTTP-Status, bei denen eine Wiederholung sinnvoll ist.
RETRY_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})


class StepUnavailable(Exception):
    """Der Schritt ist auf diesem Endpunkt (noch) nicht veröffentlicht."""


class FetchError(Exception):
    """Der Schritt ist vorhanden, der Download ist aber fehlgeschlagen."""


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
            if resp.status_code != 206:
                raise FetchError(f"Range {r.header} auf {url}: HTTP {resp.status_code} statt 206")
            try:
                return r.extract(resp.content)
            except ValueError as exc:
                raise FetchError(str(exc)) from exc

        raw = b"".join(await asyncio.gather(*(load(r) for r in ranges)))
        expected = sum(len(r.entries) for r in ranges)
        grib = await asyncio.to_thread(_crop_checked, raw, bbox, expected)
        return StepData(
            grib=grib,
            url=url,
            n_messages=expected,
            messages=[e.key for r in ranges for e in r.entries],
            absent=[s.label for s in missing_specs(entries, specs)],
        )

    async def _fetch_nomads(
        self, product: str, run: datetime, step: int, specs: list[MessageSpec], bbox: BBox
    ) -> StepData:
        params = nomads_params(product, run, step, specs, bbox)
        resp = await self.get(self.config.url, params=params)
        body = resp.content
        if resp.status_code == 200 and body.startswith(b"GRIB"):
            try:
                n = sum(1 for _ in iter_messages(body))
            except GribError as exc:
                raise FetchError(f"NOMADS-Antwort unvollständig: {exc}") from exc
            return StepData(grib=body, url=str(resp.request.url), n_messages=n)
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
        if n != expected:
            raise FetchError(f"{n} GRIB-Nachrichten erhalten, {expected} erwartet")
        return crop(raw, bbox)
    except GribError as exc:
        raise FetchError(str(exc)) from exc
