"""Zentrale Konfiguration über Umgebungsvariablen (Präfix ``PDB_``).

@relation(SWA-CORE-SETTINGS, scope=file)
"""

from functools import lru_cache

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Buckets(BaseSettings):
    """Namen der Garage-Buckets (Kapitel 5.2 der Architektur).

    @relation(SWR-CORE-001, scope=class)
    """

    model_config = SettingsConfigDict(env_prefix="PDB_BUCKET_", extra="ignore")

    bronze: str = "wx-bronze"
    silver: str = "wx-silver"
    ml_artifacts: str = "ml-artifacts"
    backups: str = "backups"
    #: Original-Exporte statischer Quellen (MaStR-Gesamtdatenexport)
    assets_bronze: str = "assets-bronze"

    def all(self) -> list[str]:
        return [self.bronze, self.silver, self.ml_artifacts, self.backups, self.assets_bronze]


class Settings(BaseSettings):
    """Laufzeitkonfiguration, gemeinsam für alle Dienste.

    Werte kommen aus Umgebungsvariablen bzw. ``.env``; Geheimnisse nie ins Repo.

    @relation(SWR-CORE-001, scope=class)
    """

    model_config = SettingsConfigDict(
        env_prefix="PDB_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    database_url: str = "postgresql+psycopg://predictiondb:predictiondb@localhost:5432/predictiondb"

    s3_endpoint_url: str = "http://localhost:3900"
    s3_region: str = "garage"
    s3_access_key_id: str = ""
    s3_secret_access_key: SecretStr = SecretStr("")

    buckets: Buckets = Field(default_factory=Buckets)

    log_level: str = "INFO"
    log_json: bool = True


@lru_cache
def get_settings() -> Settings:
    """@relation(SWR-CORE-001, scope=function)"""
    return Settings()
