"""Einstellungen des Energie-Ingests (Präfix ``PDB_ENERGY_``).

Zugangsdaten: ``PDB_ENTSOE_TOKEN``, ``PDB_NETZTRANSPARENZ_CLIENT_ID`` und
``PDB_NETZTRANSPARENZ_CLIENT_SECRET``.

Quellen, deren Nutzungsbedingungen die Weitergabe einschränken (ENTSO-E, Energy-Charts,
Netztransparenz; ``docs/ARCHITEKTUR.md`` 7.11.1), laufen nur mit ausdrücklicher Freigabe
``PDB_ALLOW_ENTSO_E``, ``PDB_ALLOW_ENERGY_CHARTS`` bzw. ``PDB_ALLOW_NETZTRANSPARENZ`` (Vorgabe
``false``). Ohne Freigabe liefern freie Quellen alles Nötige; die Marktwerte rechnet dann
``pdb_marktwert`` selbst.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pydantic import AliasChoices, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

ALL_SOURCES = ["entsoe", "smard", "energy_charts", "netztransparenz", "pdb_marktwert"]

#: Unfreie Quellen und das Feld ihrer Freigabe in ``EnergySettings``
UNFREE_SOURCES = {
    "entsoe": "allow_entso_e",
    "energy_charts": "allow_energy_charts",
    "netztransparenz": "allow_netztransparenz",
}


def _allow(name: str) -> Any:
    return Field(default=False, validation_alias=AliasChoices(f"PDB_{name}", name))


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
    allow_entso_e: bool = _allow("ALLOW_ENTSO_E")
    allow_energy_charts: bool = _allow("ALLOW_ENERGY_CHARTS")
    allow_netztransparenz: bool = _allow("ALLOW_NETZTRANSPARENZ")
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

    def allowed(self, code: str) -> bool:
        """Freie Quellen immer, unfreie nur mit ``PDB_ALLOW_*=true``."""
        field = UNFREE_SOURCES.get(code)
        return field is None or bool(getattr(self, field))
