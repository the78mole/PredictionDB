"""Einstellungen des Prognose-Dienstes (Präfix ``PDB_PREDICT_``).

Schlüssel braucht der Dienst keine. ``PDB_EIA_API_KEY`` ist optional: Ohne ihn lädt die
EIA-Quelle die Bulk-Dateien (rund 60 MB, alle sechs Stunden).
"""

from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class PredictSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="PDB_PREDICT_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    #: Modellpaket: lokaler Ordner oder OCI-Referenz, z. B.
    #: ``ghcr.io/the78mole/predictiondb-models/de_lu:champion``
    model: str = "/models"
    #: Arbeitsordner: ``cache/`` (Eingänge, Pakete aus GHCR) und ``out/`` (Prognosen)
    data_dir: Path = Path("/data")
    #: Abstand der Prognoseläufe in Minuten und Minute in der Stunde, zu der sie starten
    every_min: int = Field(default=60, ge=5)
    offset_min: int = Field(default=20, ge=0, le=59)
    #: HTTP-Schnittstelle (0 = aus)
    http_host: str = "0.0.0.0"
    http_port: int = Field(default=8080, ge=0, le=65535)
    #: Ältere Prognosen im Archiv ``out/archive`` löschen
    keep_days: int = Field(default=14, ge=1)
    #: Wandernde Tags (``champion``) so oft neu auflösen
    model_refresh_h: float = Field(default=24.0, gt=0)
    #: Quellen-YAML von wx-ingest (Endpunkte, Gitterausschnitt) für Wetter-Varianten
    gfs_config: Path = Path("config/sources/gfs_0p25.yaml")
    #: Eine Prognose gilt nach so vielen Stunden als veraltet (``/health`` meldet 503)
    stale_after_h: float = Field(default=3.0, gt=0)
    http_timeout_s: float = 60.0

    @property
    def cache_dir(self) -> Path:
        return self.data_dir / "cache"

    @property
    def out_dir(self) -> Path:
        return self.data_dir / "out"
