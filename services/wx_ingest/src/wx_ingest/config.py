"""Quellen-Konfiguration (``config/sources/*.yaml``).

S3-Zugang und Logging kommen aus ``predictiondb_core`` (Umgebungsvariablen ``PDB_*``).
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

from gribkit import BBox, MessageSpec


class EndpointConfig(BaseModel):
    """Ein Download-Endpunkt; die Reihenfolge in der YAML-Datei ist die Priorität."""

    name: str
    kind: Literal["idx_range", "nomads_filter", "header_scan"]
    url: str
    #: ``header_scan``: Pfad der GRIB-Datei unter ``url``, Platzhalter ``{run}`` und ``{step}``
    #: (z. B. ``{run:%Y}/{run:%Y%m%d}/gfs.0p25.{run:%Y%m%d%H}.f{step:03d}.grib2``)
    path: str | None = None
    #: ``header_scan``: Abschnitte je Datei, deren Köpfe gleichzeitig gelesen werden
    segments: int = Field(default=16, ge=1)
    #: Endpunkt hält das Archiv vor und darf für Backfill genutzt werden.
    archive: bool = False
    #: Gleichzeitige Anfragen an diesen Endpunkt.
    concurrency: int = Field(default=8, ge=1)
    #: Obergrenze Anfragen pro Minute (``None`` = unbegrenzt).
    rate_per_min: int | None = Field(default=None, ge=1)
    #: Lücken bis zu dieser Größe zwischen gewählten Nachrichten mitladen.
    max_gap: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _path(self) -> EndpointConfig:
        if self.kind == "header_scan" and not self.path:
            raise ValueError(f"Endpunkt {self.name}: header_scan braucht path")
        return self


class MessageConfig(BaseModel):
    var: str
    level: str
    fcst: str | None = None

    def spec(self) -> MessageSpec:
        return MessageSpec(self.var, self.level, self.fcst)


class BBoxConfig(BaseModel):
    north: float
    south: float
    west: float
    east: float

    def bbox(self) -> BBox:
        return BBox(self.north, self.south, self.west, self.east)


class GfsConfig(BaseModel):
    code: str
    model: str = "gfs"
    resolution: str = "0p25"
    product: str = "pgrb2.0p25"
    runs: list[int]
    #: Liste von ``[start, ende, schrittweite]`` in Stunden, Ende inklusive.
    steps: list[tuple[int, int, int]]
    bbox: BBoxConfig
    endpoints: list[EndpointConfig]
    messages: list[MessageConfig]
    #: Wie weit zurück der Live-Modus nach unvollständigen Läufen sucht.
    lookback_hours: int = 30
    #: Schritte gleichzeitig je Lauf.
    step_concurrency: int = Field(default=4, ge=1)
    #: ``header_scan``: Kopf-Inventare je Schritt (JSON, gern ``.json.gz``) als Vorlagen,
    #: relativ zur Konfigurationsdatei; fehlende Schritte baut der Endpunkt im ersten Lauf
    templates: Path | None = None

    @field_validator("runs")
    @classmethod
    def _runs(cls, v: list[int]) -> list[int]:
        if not v or any(h not in (0, 6, 12, 18) for h in v):
            raise ValueError("GFS-Läufe nur 0, 6, 12, 18 UTC")
        return sorted(set(v))

    @model_validator(mode="after")
    def _check(self) -> GfsConfig:
        self.bbox.bbox()
        if not self.endpoints:
            raise ValueError("mindestens ein Endpunkt nötig")
        if not self.messages:
            raise ValueError("mindestens eine Nachricht nötig")
        for start, end, inc in self.steps:
            if inc <= 0 or end < start:
                raise ValueError(f"ungültiger Schrittbereich {[start, end, inc]}")
        return self

    def step_list(self) -> list[int]:
        return sorted({h for s, e, i in self.steps for h in range(s, e + 1, i)})

    def specs(self) -> list[MessageSpec]:
        return [m.spec() for m in self.messages]

    def endpoints_for(self, archive_only: bool) -> list[EndpointConfig]:
        return [e for e in self.endpoints if e.archive or not archive_only]

    def runs_between(
        self, start: datetime, end: datetime, hours: Iterable[int] | None = None
    ) -> list[datetime]:
        """Alle konfigurierten Läufe im Intervall ``[start, end]`` (UTC).

        ``hours`` schränkt auf eine Teilmenge der konfigurierten Laufzeiten ein (z. B. 0, 12).
        """
        wanted = set(self.runs) if hours is None else set(self.runs) & set(hours)
        t = start.astimezone(UTC).replace(minute=0, second=0, microsecond=0)
        t -= timedelta(hours=t.hour % 6)
        out = []
        while t <= end:
            if t >= start and t.hour in wanted:
                out.append(t)
            t += timedelta(hours=6)
        return out

    def recent_runs(self, now: datetime) -> list[datetime]:
        return self.runs_between(now - timedelta(hours=self.lookback_hours), now)

    @classmethod
    def from_yaml(cls, path: str | Path) -> GfsConfig:
        config = cls.model_validate(yaml.safe_load(Path(path).read_text()))
        if config.templates and not config.templates.is_absolute():
            config.templates = Path(path).parent / config.templates
        return config
