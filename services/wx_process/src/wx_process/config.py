"""Konfiguration der Verarbeitung (``config/process/*.yaml``).

Pfade kommen aus ``PDB_WX_PROCESS_*``; S3-Zugang, Datenbank und Logging aus
``predictiondb_core`` (Umgebungsvariablen ``PDB_*``). Läufe, Schritte und Bronze-Layout liest
der Dienst aus derselben Quellen-YAML wie ``wx-ingest``.

@relation(SWA-WXP-CONFIG, scope=file)
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from wx_process.features import PvModel, TechnologyFeatures


class WxProcessSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="PDB_WX_PROCESS_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    config: Path = Path("config/process/gfs_0p25.yaml")


class SilverConfig(BaseModel):
    #: Schritte je Zarr-Chunk (Gitter immer ganz)
    step_chunk: int = Field(default=48, ge=1)
    #: Zstd-Stufe im Blosc-Codec
    zstd_level: int = Field(default=5, ge=1, le=9)


class ProcessConfig(BaseModel):
    #: ``meta.source.code`` des Wettermodells
    source: str
    #: Quellen-YAML von ``wx-ingest`` (relativ zum Arbeitsverzeichnis)
    source_config: Path
    #: ``assets.grid_weight.grid`` für dieses Modellgitter
    weight_grid: str
    #: feste Regionen statt der Clusterung aus ``assets`` (GFS Europa: Gebotszonen wie GEFS)
    regions: Path | None = None
    #: ``meta.feature.aggregation`` der Cluster-Features
    aggregation: str = "cluster_cap_weighted"
    #: Rückblick im Dienstbetrieb (``latest``)
    lookback_hours: int = Field(default=48, ge=6)
    #: Bronze-Schritte gleichzeitig laden
    read_concurrency: int = Field(default=8, ge=1)
    silver: SilverConfig = Field(default_factory=SilverConfig)
    technologies: dict[str, TechnologyFeatures]
    pv: PvModel = Field(default_factory=PvModel)

    @classmethod
    def from_yaml(cls, path: str | Path) -> ProcessConfig:
        """@relation(SWR-WXP-001, scope=function)"""
        return cls.model_validate(yaml.safe_load(Path(path).read_text()))
