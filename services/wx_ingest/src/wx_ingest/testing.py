"""Hilfen für Tests: simulierte GFS-Spiegel (S3 mit .idx + GRIB2) für httpx.MockTransport.

Ohne ``.idx`` (``index=False``) liegt die Datei wie im NCAR-Archiv GDEX d084001; die
Nachrichten tragen die zu ``LAYOUT`` passenden GRIB2-Schlüssel, damit das Kopf-Inventar
dieselben Zeilen liefert.

@relation(SWA-WXI-TESTING, scope=file)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx
from gribkit.testing import make_global_message

from wx_ingest.config import GfsConfig

RUN = datetime(2026, 10, 20, 6, tzinfo=UTC)

#: (VAR, LEVEL, FCST) je Nachricht der synthetischen GFS-Datei
LAYOUT = [
    ("PRMSL", "mean sea level", "{h} hour fcst"),
    ("CLMR", "1 hybrid level", "{h} hour fcst"),
    ("TMP", "2 m above ground", "{h} hour fcst"),
    ("DPT", "2 m above ground", "{h} hour fcst"),
    ("TCDC", "entire atmosphere", "{h} hour fcst"),
    ("TCDC", "entire atmosphere", "0-{h} hour ave fcst"),
    ("UGRD", "100 m above ground", "{h} hour fcst"),
]


#: (Disziplin, Kategorie, Parameter, Flächentyp) je ``LAYOUT``-Zeile
_KEYS = [
    (0, 3, 1, 101),
    (0, 1, 22, 105),
    (0, 0, 0, 103),
    (0, 0, 6, 103),
    (0, 6, 1, 10),
    (0, 6, 1, 10),
    (0, 2, 2, 103),
]
_HEIGHTS = {2: 2, 3: 2, 6: 100}

GDEX_PATH = "{run:%Y}/{run:%Y%m%d}/gfs.0p25.{run:%Y%m%d%H}.f{step:03d}.grib2"


def keyed_message(n: int, step: int) -> bytes:
    """Nachricht ``n`` (ab 1) der synthetischen Datei mit Werten ``n * 100 + lat``."""
    import eccodes as ec

    msg, _, _ = make_global_message(values_fn=lambda lat, lon: n * 100 + lat + 0 * lon)
    h = ec.codes_new_from_message(msg)
    try:
        if "ave" in LAYOUT[n - 1][2]:
            ec.codes_set(h, "productDefinitionTemplateNumber", 8)
        names = ("discipline", "parameterCategory", "parameterNumber", "typeOfFirstFixedSurface")
        for k, v in zip(names, _KEYS[n - 1], strict=True):
            ec.codes_set(h, k, v)
        if (n - 1) in _HEIGHTS:
            ec.codes_set(h, "scaleFactorOfFirstFixedSurface", 0)
            ec.codes_set(h, "scaledValueOfFirstFixedSurface", _HEIGHTS[n - 1])
        if "ave" in LAYOUT[n - 1][2]:
            ec.codes_set(h, "typeOfStatisticalProcessing", 0)
            ec.codes_set(h, "stepUnits", 1)
            ec.codes_set(h, "startStep", 0)
            ec.codes_set(h, "endStep", step)
        else:
            ec.codes_set(h, "forecastTime", step)
        return bytes(ec.codes_get_message(h))
    finally:
        ec.codes_release(h)


def config_dict(**over: Any) -> dict[str, Any]:
    cfg = {
        "code": "gfs_test",
        "runs": [0, 6, 12, 18],
        "steps": [[0, 3, 1], [6, 12, 3]],
        "bbox": {"north": 58, "south": 44, "west": 0, "east": 20},
        "step_concurrency": 2,
        "endpoints": [
            {"name": "aws", "kind": "idx_range", "url": "https://aws.test", "archive": True},
            {"name": "gcs", "kind": "idx_range", "url": "https://gcs.test", "archive": True},
        ],
        "messages": [
            {"var": "TMP", "level": "2 m above ground"},
            {"var": "TCDC", "level": "entire atmosphere"},
            {"var": "UGRD", "level": "100 m above ground"},
        ],
    }
    cfg.update(over)
    return cfg


@dataclass
class FakeGfsHost:
    """Simuliert einen S3-Spiegel mit .idx + GRIB-Dateien."""

    host: str
    available: set[int]
    honour_range: bool = True
    fail_status: int | None = None
    #: .idx mit verschobenen Offsets, wie nach einem Neuschreiben der GRIB-Datei im Archiv
    stale_idx: bool = False
    #: ohne ``.idx`` und mit Pfaden wie in GDEX d084001 (``GDEX_PATH``)
    index: bool = True
    #: ``Content-Range`` ohne Gesamtgröße (``bytes 0-511/*``) wie am NCAR-Ursprung
    unknown_size: bool = False
    files: dict[str, bytes] = field(default_factory=dict)
    idx: dict[str, str] = field(default_factory=dict)
    requests: list[httpx.Request] = field(default_factory=list)

    def __post_init__(self) -> None:
        for step in self.available:
            if self.index:
                base = f"/gfs.{RUN:%Y%m%d}/{RUN:%H}/atmos/gfs.t{RUN:%H}z.pgrb2.0p25.f{step:03d}"
            else:
                base = "/" + GDEX_PATH.format(run=RUN, step=step)
            data, lines = b"", []
            for n, (var, level, fcst) in enumerate(LAYOUT, start=1):
                msg = keyed_message(n, step)
                offset = len(data) + (1000 * n if self.stale_idx else 0)
                lines.append(f"{n}:{offset}:d={RUN:%Y%m%d%H}:{var}:{level}:{fcst.format(h=step)}:")
                data += msg
            self.files[base] = data
            if self.index:
                self.idx[base + ".idx"] = "\n".join(lines) + "\n"

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.fail_status:
            return httpx.Response(self.fail_status)
        path = request.url.path
        if path in self.idx:
            return httpx.Response(200, text=self.idx[path])
        if path not in self.files:
            return httpx.Response(404)
        data = self.files[path]
        if request.method == "HEAD":
            return httpx.Response(200, headers={"content-length": str(len(data))})
        rng = request.headers.get("range")
        if not rng or not self.honour_range:
            return httpx.Response(200, content=data)
        m = re.fullmatch(r"bytes=(\d+)-(\d*)", rng)
        if m is None:
            return httpx.Response(416)
        start, end = int(m[1]), int(m[2]) if m[2] else len(data) - 1
        if start >= len(data):
            return httpx.Response(416)
        end = min(end, len(data) - 1)
        return httpx.Response(
            206,
            content=data[start : end + 1],
            headers={
                "content-range": f"bytes {start}-{end}/{'*' if self.unknown_size else len(data)}"
            },
        )


class Router:
    def __init__(self, *hosts: FakeGfsHost) -> None:
        self.hosts = {h.host: h for h in hosts}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        host = self.hosts.get(request.url.host)
        return host.handle(request) if host else httpx.Response(404)


def make_client(*hosts: FakeGfsHost) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(Router(*hosts)))


def make_config(**over: Any) -> GfsConfig:
    return GfsConfig.model_validate(config_dict(**over))
