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
  Ein vollständiges Inventar kostet je Datei einige hundert Anfragen. Es dient deshalb als
  Vorlage für denselben Schritt späterer Läufe: Die Reihenfolge der Nachrichten ist innerhalb
  einer Modellversion fest, nur die Längen schwanken. Aus der Vorlage wird geschätzt, wo die
  gewählten Nachrichten liegen; dort wird der nächste Anfang gesucht und nur über die wenigen
  Köpfe bis zu den gewählten Nachrichten gelaufen (:meth:`Endpoint.scan_targeted`). Passt die
  Vorlage nicht (andere Modellversion), entsteht ein neues vollständiges Inventar.
  Liegen vollständige ``.idx`` vor (OCI-Artefakt ``gfs-0p25-gdex-idx``,
  :meth:`Endpoint.import_idx`), entfällt das Lesen der Köpfe; geladen wird wie bei ``idx_range``.

@relation(SWA-WXI-ENDPOINT, scope=file)
"""

from __future__ import annotations

import asyncio
import gzip
import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
from aiolimiter import AsyncLimiter
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
from tenacity import AsyncRetrying, retry_if_exception, stop_after_attempt, wait_exponential

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


class BrokenFile(FetchError):
    """Die Datei liegt im Archiv, ist dort aber leer oder abgeschnitten; Wiederholen hilft nicht."""


class StaleIndex(FetchError):
    """Die Byte-Bereiche aus der ``.idx`` treffen keine GRIB-Nachrichten."""


class _TemplateMiss(Exception):
    """Die Vorlage passt nicht zur Datei; ein vollständiges Inventar ist nötig."""


#: Sprünge je Gruppe gewählter Nachrichten, bevor die Vorlage als unpassend gilt
_MAX_JUMPS = 8
#: So viele Köpfe vor einer Gruppe dürfen noch abgelaufen statt angesprungen werden
_MAX_LEAD = 3
#: Höchstens so viele Köpfe ablaufen, wenn die Sprünge das Ziel nur einschließen
_MAX_WALK = 32
#: Höchstens so viele mehrdeutige Köpfe nach einem Sprung überspringen
_MAX_SKIP = 8


@dataclass(frozen=True)
class _Template:
    """Vollständiges Inventar eines Schritts als Vorlage für spätere Läufe."""

    size: int
    entries: list[IdxEntry]

    def keys(self) -> tuple[str, ...]:
        """Nachrichtenfolge; Vorlagen mit gleicher Folge sind gleichwertig."""
        return tuple(e.key for e in self.entries)

    def index(self) -> dict[str, int]:
        """Position je Schlüssel; mehrdeutige Schlüssel taugen nicht zum Wiederfinden."""
        seen: dict[str, int] = {}
        doubles: set[str] = set()
        for i, e in enumerate(self.entries):
            if e.key in seen:
                doubles.add(e.key)
            seen[e.key] = i
        return {k: i for k, i in seen.items() if k not in doubles}


def _groups(indices: list[int], gap: int = 2) -> list[tuple[int, int]]:
    """Fasst Positionen mit höchstens ``gap`` Nachrichten Abstand zu Gruppen zusammen."""
    out: list[tuple[int, int]] = []
    for i in indices:
        if out and i - out[-1][1] <= gap:
            out[-1] = (out[-1][0], i)
        else:
            out.append((i, i))
    return out


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


def read_templates(path: Path) -> dict[str, Any]:
    """Vorlagen aus :meth:`Endpoint.export_templates` (``{"steps": ...}``, ``.gz`` entpackt)."""
    raw = path.read_bytes()
    data = json.loads(gzip.decompress(raw) if path.suffix == ".gz" else raw)
    steps: dict[str, Any] = data["steps"]
    return steps


def write_templates(path: Path, run: str, steps: dict[str, Any]) -> None:
    """Schreibt Vorlagen kompakt; ``.gz`` ohne Zeitstempel, damit gleicher Inhalt gleich bleibt."""
    raw = json.dumps({"run": run, "steps": steps}, separators=(",", ":")).encode()
    path.write_bytes(gzip.compress(raw, mtime=0) if path.suffix == ".gz" else raw)


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
        #: header_scan: vollständige Inventare je Schritt als Vorlagen für spätere Läufe,
        #: zuletzt passende zuerst (die Nachrichtenfolge wechselt mit Modell-Updates)
        self._templates: dict[int, list[_Template]] = {}
        self._scan_locks: dict[int, asyncio.Lock] = {}
        #: header_scan: vollständige ``.idx`` je Datei, Schlüssel ist der Pfad im Archiv
        self._idx: dict[str, bytes] = {}

    @property
    def name(self) -> str:
        return self.config.name

    async def get(self, url: str, **kwargs: Any) -> httpx.Response:
        """@relation(SWR-WXI-011, SWR-WXI-012, scope=function)"""
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
        """@relation(SWR-WXI-014, scope=function)"""
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
        """@relation(SWR-WXI-014, scope=function)"""

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
        if resp.status_code == 416:
            # Range jenseits des Dateiendes: im GDEX-Archiv liegen einzelne Dateien mit 0 Byte
            raise BrokenFile(f"Range {start}-{end} auf {url}: HTTP 416, Datei leer oder kürzer")
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
        self,
        url: str,
        start: int,
        stop: int,
        first: bytes | None = None,
        *,
        complete: bool = False,
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
                head = parse_head(data, complete=complete)
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

    async def scan(self, url: str, *, complete: bool = False) -> tuple[str, list[IdxEntry]]:
        """Inventar einer Datei aus ihren Köpfen; liefert die URL nach Weiterleitungen mit.

        ``complete``: Namen und Bezugsdatum wie ``wgrib2 -s`` (vollständige ``.idx``), sonst
        die Schlüssel der Vorlagen.
        """
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
                self._walk(url, a, b, first.content, complete=complete)
                for a, b in zip(bounds, [*bounds[1:], size], strict=True)
            )
        )
        heads = [h for part in parts for h in part]
        return url, [entry(i, off, h) for i, (off, h) in enumerate(heads, start=1)]

    async def scan_targeted(
        self, url: str, template: _Template, specs: list[MessageSpec]
    ) -> tuple[str, list[IdxEntry]]:
        """Nur die gewählten Nachrichten finden, Lage geschätzt aus ``template``.

        Liefert die Einträge der gewählten Nachrichten (mit Nachbarn aus den abgelaufenen
        Köpfen). Passt die Vorlage nicht, folgt :class:`_TemplateMiss`.

        @relation(SWR-WXI-016, scope=function)
        """
        first = await self._range(url, 0, HEAD_BYTES - 1)
        url = str(first.url)
        size = await self._size(url, first)
        tmpl = template.entries
        wanted = [i for i, e in enumerate(tmpl) if any(s.matches(e) for s in specs)]
        if not wanted:
            raise _TemplateMiss("Vorlage enthält keine gewählte Nachricht")
        index = template.index()
        keys = {e.key for e in tmpl}
        scale = size / template.size

        def aim(anchor_off: int, anchor_i: int, target: int) -> int:
            # Mitte der Nachricht vor dem Ziel: die Suche findet dann den Anfang des Ziels
            before = tmpl[target - 1]
            mid = before.offset + (before.length or 0) // 2
            return anchor_off + int((mid - tmpl[anchor_i].offset) * scale)

        async def locate(est: int) -> tuple[int, int]:
            """Nächster wiedererkennbarer Nachrichtenanfang ab ``est`` und seine Position.

            Mehrdeutige Schlüssel (z. B. Schichten, deren Fläche keinen Namen hat) werden
            übersprungen, indem über die folgenden Köpfe gelaufen wird.
            """
            found = await self._boundary(url, max(0, min(est, size - 1)), size)
            if found is None:
                raise _TemplateMiss(f"kein Nachrichtenanfang ab Byte {est}")
            for _ in range(_MAX_SKIP + 1):
                data = (await self._range(url, found, found + HEAD_BYTES - 1)).content
                try:
                    head = parse_head(data)
                except HeaderError as exc:
                    raise _TemplateMiss(f"Kopf an Byte {found}: {exc}") from exc
                key = entry(0, found, head).key
                if key in index:
                    return found, index[key]
                if key not in keys:
                    raise _TemplateMiss(f"{key} an Byte {found} fehlt in der Vorlage")
                found += head.length
                if found >= size:
                    break
            raise _TemplateMiss(f"ab Byte {est} kein eindeutiger Schlüssel")

        async def start(a: int) -> tuple[int, int]:
            """Ein Nachrichtenanfang höchstens ``_MAX_LEAD`` Nachrichten vor ``a``."""
            lo = (0, 0)  # bester bekannter Anfang vor dem Ziel; der Dateianfang gilt immer
            back = 1
            est = aim(0, 0, a)
            for _ in range(_MAX_JUMPS):
                if a - lo[1] <= _MAX_LEAD:
                    break
                pos, i = await locate(est)
                if i <= a:
                    if a - i <= _MAX_LEAD:
                        return pos, i
                    lo = max(lo, (pos, i), key=lambda p: p[1])
                    est = aim(pos, i, a)
                elif a - lo[1] <= _MAX_WALK:
                    break  # Ziel liegt zwischen zwei gefundenen Anfängen: ablaufen
                else:
                    # zu weit: immer weiter vor dem Ziel ansetzen, bis ein Anfang davor liegt
                    est = aim(pos, i, max(1, a - back))
                    back *= 2
            if a - lo[1] > _MAX_WALK:
                raise _TemplateMiss(f"Nachricht {tmpl[a].key} nach {_MAX_JUMPS} Sprüngen")
            return lo

        async def group(a: int, b: int) -> list[IdxEntry]:
            pos, i = await start(a)
            out: list[IdxEntry] = []
            off = pos
            for n in range(i, b + 1):
                data = (await self._range(url, off, off + HEAD_BYTES - 1)).content
                try:
                    e = entry(n + 1, off, parse_head(data))
                except HeaderError as exc:
                    raise _TemplateMiss(f"Kopf an Byte {off}: {exc}") from exc
                if e.key != tmpl[n].key:
                    raise _TemplateMiss(f"an Position {n + 1} {e.key} statt {tmpl[n].key}")
                out.append(e)
                off += e.length or 0
            if off > size:
                raise _TemplateMiss(f"Nachricht {tmpl[b].key} reicht über das Dateiende")
            return out

        parts = await asyncio.gather(*(group(a, b) for a, b in _groups(wanted)))
        # Vorlauf einer Gruppe kann die vorige überlappen: jede Nachricht nur einmal
        unique = {e.offset: e for part in parts for e in part}
        return url, [unique[o] for o in sorted(unique)]

    def _scan_path(self, run: datetime, step: int) -> str:
        assert self.config.path is not None
        return self.config.path.format(run=run, step=step)

    def _scan_url(self, run: datetime, step: int) -> str:
        return f"{self.config.url.rstrip('/')}/{self._scan_path(run, step)}"

    def import_idx(self, files: dict[str, bytes]) -> int:
        """Übernimmt vollständige ``.idx`` (Pfad der GRIB-Datei im Archiv plus ``.idx``).

        Für diese Dateien entfällt das Lesen der Köpfe. Liefert die Zahl der Dateien.
        """
        found = {n.removesuffix(".idx"): t for n, t in files.items() if n.endswith(".idx")}
        self._idx.update(found)
        return len(found)

    async def complete_inventory(self, run: datetime, step: int) -> list[IdxEntry]:
        """Alle Nachrichten einer Datei mit Namen wie ``wgrib2 -s`` (für eine ``.idx``).

        Passen die Abschnitte nicht aneinander, wird die ganze Datei geladen. Ist sie leer oder
        abgeschnitten, folgt :class:`BrokenFile`.
        """
        url = self._scan_url(run, step)
        try:
            return (await self.scan(url, complete=True))[1]
        except StaleIndex as exc:
            log.warning("scan_failed", url=url, error=str(exc))
        async with _FULL_DOWNLOAD:
            resp = await self.get(url)
            if resp.status_code in (403, 404):
                raise StepUnavailable(url)
            if resp.status_code != 200:
                raise FetchError(f"ganze Datei {url}: HTTP {resp.status_code}")
            data = resp.content
            del resp
            try:
                return [
                    entry(i, start, parse_head(data[start : start + HEAD_BYTES], complete=True))
                    for i, (start, _end) in enumerate(_message_bounds(data), start=1)
                ]
            except HeaderError as exc:
                raise FetchError(f"{url}: {exc}") from exc

    async def build_templates(
        self,
        run: datetime,
        steps: list[int],
        specs: list[MessageSpec],
        concurrency: int = 8,
    ) -> list[int]:
        """Vorlagen für alle Schritte eines Laufs; liefert die Schritte mit passender Vorlage.

        Passt eine vorhandene Vorlage, kostet das nur wenige Anfragen; sonst entsteht aus
        dem vollständigen Inventar eine weitere. Höchstens ``concurrency`` Schritte
        gleichzeitig, damit der Fortschritt im Log sichtbar ist.
        """
        sem = asyncio.Semaphore(concurrency)
        started = time.monotonic()
        finished = 0

        async def one(step: int) -> int | None:
            nonlocal finished
            async with sem:
                try:
                    _, entries = await self._inventory(self._scan_url(run, step), step, specs)
                except (StepUnavailable, FetchError, httpx.HTTPError) as exc:
                    # auch Zeitüberschreitungen nach allen Wiederholungen: dieser Schritt
                    # fehlt dann in den Vorlagen, die Monatsjobs bauen ihn selbst
                    entries, error = [], f"{type(exc).__name__}: {exc}"
                else:
                    error = ""
            finished += 1
            progress = {
                "run": f"{run:%Y%m%d%H}",
                "step": step,
                "done": f"{finished}/{len(steps)}",
                "seconds": round(time.monotonic() - started),
            }
            if error:
                log.warning("template_failed", error=error, **progress)
                return None
            log.info("template_ready", messages=len(entries), **progress)
            return step

        log.info("templates_start", run=f"{run:%Y%m%d%H}", steps=len(steps))
        built = [s for s in await asyncio.gather(*(one(s) for s in steps)) if s is not None]
        failed = [s for s in steps if s not in built]
        if failed:
            # zweiter Durchgang für Schritte, die an einer trägen OSDF-Phase scheiterten;
            # was dann noch fehlt, bauen die Monatsjobs selbst
            log.info("templates_retry", run=f"{run:%Y%m%d%H}", steps=failed)
            finished, steps = 0, failed
            built += [s for s in await asyncio.gather(*(one(s) for s in failed)) if s is not None]
        return sorted(built)

    def export_templates(self) -> dict[str, Any]:
        """Vorlagen als JSON-taugliches Objekt, damit andere Jobs sie nicht neu bauen müssen.

        Je Schritt eine Liste von Varianten, in der Reihenfolge der Nachrichtenfolge, damit
        gleicher Bestand gleich exportiert wird.
        """
        return {
            str(step): [
                {
                    "size": t.size,
                    "entries": [[e.offset, e.length, e.var, e.level, e.fcst] for e in t.entries],
                }
                for t in sorted(variants, key=_Template.keys)
            ]
            for step, variants in sorted(self._templates.items())
        }

    def import_templates(self, data: dict[str, Any]) -> int:
        """Übernimmt Vorlagen aus :meth:`export_templates` (auch eine je Schritt ohne Liste).

        Liefert die Zahl der Schritte.
        """
        for step, variants in data.items():
            for t in reversed(variants if isinstance(variants, list) else [variants]):
                entries = [
                    IdxEntry(str(i), off, length, "", var, level, fcst)
                    for i, (off, length, var, level, fcst) in enumerate(t["entries"], start=1)
                ]
                self._add_template(int(step), _Template(int(t["size"]), entries))
        return len(data)

    def _add_template(self, step: int, template: _Template) -> None:
        """Vorlage nach vorn; eine mit gleicher Nachrichtenfolge ersetzt sie."""
        keys = template.keys()
        rest = [t for t in self._templates.get(step, []) if t.keys() != keys]
        self._templates[step] = [template, *rest]

    async def _fetch_header_scan(
        self, run: datetime, step: int, specs: list[MessageSpec], bbox: BBox
    ) -> StepData:
        """@relation(SWR-WXI-016, scope=function)"""
        url = self._scan_url(run, step)
        path = self._scan_path(run, step)
        try:
            known = self._idx.get(path)
            if known is not None:
                final, entries = url, parse_idx(known.decode())
            else:
                final, entries = await self._inventory(url, step, specs)
            chosen = select(entries, specs)
            if not chosen:
                raise FetchError(f"keine der angeforderten Nachrichten in {url}")
            ranges = merge_ranges(chosen, max_gap=self.config.max_gap)
            raw = await self._load_ranges(final, ranges)
            grib = await asyncio.to_thread(_crop_checked, raw, bbox, len(chosen))
        except StaleIndex as exc:
            log.warning("scan_failed", url=url, error=str(exc))
            if self._idx.pop(path, None) is None:
                self._templates.pop(step, None)
            entries, chosen, grib = await self._fetch_whole_scanned(url, specs, bbox)
        return StepData(
            grib=grib,
            url=url,
            n_messages=len(chosen),
            messages=[e.key for e in chosen],
            absent=[s.label for s in missing_specs(entries, specs)],
        )

    async def _inventory(
        self, url: str, step: int, specs: list[MessageSpec]
    ) -> tuple[str, list[IdxEntry]]:
        """Einträge für die Auswahl: gezielt über eine Vorlage, sonst vollständig (neue Vorlage)."""
        variants = self._templates.get(step)
        if not variants:
            # Gleichzeitige Läufe warten auf die Vorlage, statt jeder alle Köpfe zu lesen
            async with self._scan_locks.setdefault(step, asyncio.Lock()):
                variants = self._templates.get(step)
                if not variants:
                    return await self._full_inventory(url, step)
        errors = []
        for template in list(variants):
            try:
                found = await self.scan_targeted(url, template, specs)
            except _TemplateMiss as exc:
                errors.append(str(exc))
                continue
            if template is not variants[0]:
                self._add_template(step, template)
            return found
        log.info("scan_template_miss", url=url, variants=len(errors), error=errors[0])
        return await self._full_inventory(url, step)

    async def _full_inventory(self, url: str, step: int) -> tuple[str, list[IdxEntry]]:
        final, entries = await self.scan(url)
        if entries:
            end = entries[-1].offset + (entries[-1].length or 0)
            self._add_template(step, _Template(end, entries))
        return final, entries

    async def _fetch_whole_scanned(
        self, url: str, specs: list[MessageSpec], bbox: BBox
    ) -> tuple[list[IdxEntry], list[IdxEntry], bytes]:
        """Ganze Datei laden und die Köpfe im Speicher lesen (ohne Fehltreffer).

        @relation(SWR-WXI-016, scope=function)
        """
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
        """@relation(SWR-WXI-015, scope=function)"""
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
        """@relation(SWR-WXI-013, scope=function)"""
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
    """@relation(SWR-WXI-013, scope=function)"""
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
    """@relation(SWR-WXI-014, scope=function)"""
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
            raise BrokenFile(f"GRIB-Datei unbrauchbar: keine Nachricht an Byte {pos}")
        end = pos + int.from_bytes(data[pos + 8 : pos + 16], "big")
        if end > n or data[end - 4 : end] != b"7777":
            raise BrokenFile(f"GRIB-Datei unbrauchbar: Nachricht an Byte {pos} unvollständig")
        bounds.append((pos, end))
        pos = end
    return bounds


def _realign(data: bytes, entries: list[IdxEntry], chosen: list[IdxEntry], bbox: BBox) -> bytes:
    """Wählt die Nachrichten aus der ganzen Datei über ihre Position in der ``.idx``.

    Das gilt nur, wenn die Datei genau so viele Nachrichten hat wie die ``.idx`` Offsets;
    zusätzlich muss der Prognosezeitraum jeder gewählten Nachricht zur Zeile passen.

    @relation(SWR-WXI-015, scope=function)
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
