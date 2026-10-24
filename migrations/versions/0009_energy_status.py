"""Abrufstatus je Quelle und Datensatz, Aktualität je Reihe, Energy-Charts frei

Revision ID: 0009_energy_status
Revises: 0008_source_free
Create Date: 2026-10-24
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0009_energy_status"
down_revision: str | None = "0008_source_free"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Energy-Charts: laut API-Dokumentation v2.0 CC BY 4.0, Preise DE-LU CC BY 4.0 (SMARD)
    op.execute("""
        UPDATE meta.source
           SET free = true, enabled = true,
               license = 'CC BY 4.0, Energy-Charts.info (Fraunhofer ISE); Preise DE-LU CC BY 4.0, '
                         'Bundesnetzagentur | SMARD.de'
         WHERE code = 'energy_charts'""")

    # Letzter Ausgang je Quelle und Datensatz; status: ok, empty, failed, format
    # (format = Aufbau der Antwort weicht ab, es wurde nichts geschrieben)
    op.execute("""
        CREATE TABLE meta.energy_status (
          source_id   smallint NOT NULL REFERENCES meta.source,
          dataset     text NOT NULL,
          status      text NOT NULL,
          n_values    int NOT NULL DEFAULT 0,
          error       text,
          checked_at  timestamptz NOT NULL DEFAULT now(),
          last_ok_at  timestamptz,
          PRIMARY KEY (source_id, dataset)
        )""")

    # Jüngster Wert je Reihe und das erwartete Höchstalter nach Auflösung: Viertelstunden-
    # und Stundenwerte 2 Tage, Tageswerte 7 Tage (Wochenende, Feiertage), Wochenwerte
    # 21 Tage, Monatswerte 75 Tage, Jahreswerte 500 Tage
    op.execute("""
        CREATE VIEW energy.series_freshness AS
        SELECT src.code AS source, s.code, s.resolution, src.enabled, last.ts AS last_ts,
               CASE WHEN s.resolution <= interval '1 hour' THEN interval '2 days'
                    WHEN s.resolution <= interval '1 day' THEN interval '7 days'
                    WHEN s.resolution <= interval '7 days' THEN interval '21 days'
                    WHEN s.resolution <= interval '31 days' THEN interval '75 days'
                    ELSE interval '500 days' END AS max_age
          FROM energy.series s
          JOIN meta.source src ON src.source_id = s.source_id
          LEFT JOIN LATERAL (
            SELECT max(v.ts) AS ts FROM energy.value v WHERE v.series_id = s.series_id
          ) last ON true""")


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS energy.series_freshness")
    op.execute("DROP TABLE IF EXISTS meta.energy_status")
    op.execute("UPDATE meta.source SET free = false, enabled = false WHERE code = 'energy_charts'")
