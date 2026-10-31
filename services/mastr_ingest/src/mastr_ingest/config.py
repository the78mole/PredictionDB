"""Konfiguration der Quelle und Clusterung (``config/sources/mastr.yaml``).

Pfade kommen aus ``PDB_MASTR_*``; S3-Zugang, Datenbank und Logging aus ``predictiondb_core``
(Umgebungsvariablen ``PDB_*``).

@relation(SWA-MAI-CONFIG, scope=file)
"""

from __future__ import annotations

import tempfile
from datetime import date
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from mastr_ingest.units import TECHNOLOGIES


class MastrSettings(BaseSettings):
    """@relation(SWR-MAI-002, scope=class)"""

    model_config = SettingsConfigDict(
        env_prefix="PDB_MASTR_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    config: Path = Path("config/sources/mastr.yaml")
    #: Arbeitsverzeichnis für das ZIP (mehrere GB); nach erfolgreichem Import wird es geleert.
    work_dir: Path = Path(tempfile.gettempdir()) / "mastr"


class GridConfig(BaseModel):
    name: str
    #: Auflösung in Grad; Gitterpunkte liegen auf Vielfachen davon (wie GFS 0,25°, ICON-EU 0,0625°).
    res: float = Field(gt=0, le=5)

    def snap(self, value: float) -> float:
        return round(round(value / self.res) * self.res, 6)


class TechnologyConfig(BaseModel):
    clusters: int = Field(ge=1, le=500)


class Point(BaseModel):
    lat: float
    lon: float


class MastrConfig(BaseModel):
    code: str = "mastr"
    license: str = "DL-DE BY 2.0"
    page_url: str
    download_url: str
    keep_snapshots: int = Field(default=4, ge=1)
    history_start: date = date(2015, 1, 1)
    cluster_grid: GridConfig
    weight_grids: list[GridConfig]
    recluster_after_days: int = Field(default=365, ge=1)
    technologies: dict[str, TechnologyConfig]
    sea_areas: dict[str, Point] = Field(default_factory=dict)

    @field_validator("technologies")
    @classmethod
    def _known(cls, v: dict[str, TechnologyConfig]) -> dict[str, TechnologyConfig]:
        unknown = set(v) - set(TECHNOLOGIES)
        if unknown:
            raise ValueError(f"unbekannte Technologien {sorted(unknown)}, erlaubt: {TECHNOLOGIES}")
        return v

    @classmethod
    def from_yaml(cls, path: str | Path) -> MastrConfig:
        """@relation(SWR-MAI-002, scope=function)"""
        return cls.model_validate(yaml.safe_load(Path(path).read_text()))
