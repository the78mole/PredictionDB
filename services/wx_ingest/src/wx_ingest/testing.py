"""Hilfen für Tests: simulierte GFS-Spiegel (S3 mit .idx + GRIB2) für httpx.MockTransport."""

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
    files: dict[str, bytes] = field(default_factory=dict)
    idx: dict[str, str] = field(default_factory=dict)
    requests: list[httpx.Request] = field(default_factory=list)

    def __post_init__(self) -> None:
        for step in self.available:
            base = f"/gfs.{RUN:%Y%m%d}/{RUN:%H}/atmos/gfs.t{RUN:%H}z.pgrb2.0p25.f{step:03d}"
            data, lines = b"", []
            for n, (var, level, fcst) in enumerate(LAYOUT, start=1):
                msg, _, _ = make_global_message(
                    values_fn=lambda lat, lon, n=n: n * 100 + lat + 0 * lon  # type: ignore[misc]
                )
                offset = len(data) + (1000 * n if self.stale_idx else 0)
                lines.append(f"{n}:{offset}:d={RUN:%Y%m%d%H}:{var}:{level}:{fcst.format(h=step)}:")
                data += msg
            self.files[base] = data
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
        rng = request.headers.get("range")
        if not rng or not self.honour_range:
            return httpx.Response(200, content=data)
        m = re.fullmatch(r"bytes=(\d+)-(\d*)", rng)
        if m is None:
            return httpx.Response(416)
        start, end = int(m[1]), int(m[2]) if m[2] else len(data) - 1
        return httpx.Response(206, content=data[start : end + 1])


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
