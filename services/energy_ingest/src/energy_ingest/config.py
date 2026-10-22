"""Einstellungen des Energie-Ingests (Präfix ``PDB_ENERGY_``).

Zugangsdaten: ``PDB_ENTSOE_TOKEN``, ``PDB_NETZTRANSPARENZ_CLIENT_ID`` und
``PDB_NETZTRANSPARENZ_CLIENT_SECRET``.
"""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import AliasChoices, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

ALL_SOURCES = ["entsoe", "smard", "energy_charts", "netztransparenz"]


class EnergySettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="PDB_ENERGY_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    sources: list[str] = Field(default_factory=lambda: list(ALL_SOURCES))
    entsoe_token: SecretStr = Field(
        default=SecretStr(""),
        validation_alias=AliasChoices("PDB_ENTSOE_TOKEN", "PDB_ENERGY_ENTSOE_TOKEN"),
    )
    netztransparenz_client_id: str = Field(
        default="",
        validation_alias=AliasChoices(
            "PDB_NETZTRANSPARENZ_CLIENT_ID", "PDB_ENERGY_NETZTRANSPARENZ_CLIENT_ID"
        ),
    )
    netztransparenz_client_secret: SecretStr = Field(
        default=SecretStr(""),
        validation_alias=AliasChoices(
            "PDB_NETZTRANSPARENZ_CLIENT_SECRET", "PDB_ENERGY_NETZTRANSPARENZ_CLIENT_SECRET"
        ),
    )
    # Laufender Abgleich: Fenster [heute - lookback, heute + horizon)
    lookback_days: int = 3
    horizon_days: int = 2
    # Historie, die im Dauerbetrieb schrittweise nachgeladen wird
    backfill_from: datetime = datetime(2015, 1, 1, tzinfo=UTC)
    backfill_chunks_per_cycle: int = 6
    # Blöcke, die älter sind, gelten als endgültig und werden nicht erneut geladen
    final_after_days: int = 7
    # Warnschwelle für die relative Abweichung zwischen Quellen (Stundenmittel)
    check_rel_threshold: float = 0.05
    http_timeout_s: float = 60.0
