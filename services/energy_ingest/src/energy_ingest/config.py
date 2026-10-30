"""Einstellungen des Energie-Ingests (Präfix ``PDB_ENERGY_``).

Zugangsdaten: ``PDB_ENTSOE_TOKEN``, ``PDB_NETZTRANSPARENZ_CLIENT_ID``,
``PDB_NETZTRANSPARENZ_CLIENT_SECRET``, ``PDB_EIA_API_KEY`` (ohne Schlüssel lädt die EIA-Quelle
die Bulk-Dateien) und ``PDB_GIE_API_KEY`` (Gasspeicher AGSI+ und LNG ALSI, ohne Schlüssel aus).

Quellen, deren Nutzungsbedingungen die Weitergabe einschränken (ENTSO-E, Netztransparenz,
Thomson-Reuters-Reihen der EIA, THE; ``docs/ARCHITEKTUR.md`` 7.11.1), laufen nur mit
ausdrücklicher Freigabe ``PDB_ALLOW_ENTSO_E``, ``PDB_ALLOW_NETZTRANSPARENZ``,
``PDB_ALLOW_EIA_REUTERS`` bzw. ``PDB_ALLOW_THE`` (Vorgabe ``false``). Energy-Charts steht
laut API-Dokumentation unter CC BY 4.0 und ist frei. Ohne Freigabe liefern freie Quellen alles
Nötige; die Marktwerte rechnet dann ``pdb_marktwert`` selbst.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pydantic import AliasChoices, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from energy_ingest.sources.oil_bulletin import URL as OIL_BULLETIN_URL
from energy_ingest.sources.worldbank import URL as WORLDBANK_URL

#: Reihenfolge = Reihenfolge im Abgleich. SMARD und Energy-Charts zuerst, damit der
#: Day-Ahead-Preis des Folgetags kurz nach der Auktion da ist; die ENTSO-E-Abrufe dauern
#: auf der VM je Anfrage über eine Minute und laufen deshalb am Ende des Zyklus.
ALL_SOURCES = [
    "smard",
    "energy_charts",
    "netztransparenz",
    "pdb_marktwert",
    # Brennstoffpreise und Wechselkurs
    "eia",
    "eia_reuters",
    "eia_steo",
    "ecb",
    "worldbank",
    "oil_bulletin",
    "the",
    # Gasspeicher, LNG, Gasbilanz
    "gie_agsi",
    "gie_alsi",
    "eurostat",
    # ENTSO-E (Last-Prognosen der Nachbarländer, ggf. unfreie Reihen)
    "entsoe",
    "entsoe_last",
]

#: Unfreie Quellen und das Feld ihrer Freigabe in ``EnergySettings``
UNFREE_SOURCES = {
    "entsoe": "allow_entso_e",
    "netztransparenz": "allow_netztransparenz",
    "eia_reuters": "allow_eia_reuters",
    "the": "allow_the",
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
    allow_netztransparenz: bool = _allow("ALLOW_NETZTRANSPARENZ")
    allow_eia_reuters: bool = _allow("ALLOW_EIA_REUTERS")
    allow_the: bool = _allow("ALLOW_THE")
    eia_api_key: SecretStr = Field(
        default=SecretStr(""),
        validation_alias=AliasChoices("PDB_EIA_API_KEY", "PDB_ENERGY_EIA_API_KEY"),
    )
    gie_api_key: SecretStr = Field(
        default=SecretStr(""),
        validation_alias=AliasChoices("PDB_GIE_API_KEY", "PDB_ENERGY_GIE_API_KEY"),
    )
    # Download-Adressen mit Dokument-ID, die sich ändern kann (dann Grafana-Alarm)
    worldbank_url: str = WORLDBANK_URL
    oil_bulletin_url: str = OIL_BULLETIN_URL
    # Laufender Abgleich: Fenster [heute - lookback, heute + horizon)
    lookback_days: int = 3
    horizon_days: int = 2
    # Nachträgliche Korrekturen der Quellen: einmal je Intervall die letzten revision_days Tage
    # erneut abfragen (nur Datensätze, deren Abgleich nicht ohnehin so weit zurückreicht)
    revision_days: int = 30
    revision_interval_hours: int = 24
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
