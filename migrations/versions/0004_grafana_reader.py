"""Leserolle für Grafana (nur SELECT auf die Fachschemas)

Revision ID: 0004_grafana_reader
Revises: 0003_wx_features
Create Date: 2026-10-21

Die Rolle entsteht ohne Login. Ist ``PDB_GRAFANA_DB_PASSWORD`` gesetzt, bekommt sie Login und
dieses Passwort. Ein späterer Passwortwechsel geht per ``ALTER ROLE grafana_reader PASSWORD ...``.
"""

import os
from collections.abc import Sequence

from alembic import op

revision: str = "0004_grafana_reader"
down_revision: str | None = "0003_wx_features"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ROLE = "grafana_reader"
SCHEMAS = ("meta", "energy", "assets", "feat", "fcst")


def upgrade() -> None:
    op.execute(f"""
        DO $$ BEGIN
          IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '{ROLE}') THEN
            CREATE ROLE {ROLE} NOLOGIN;
          END IF;
        END $$""")
    for schema in SCHEMAS:
        op.execute(f"GRANT USAGE ON SCHEMA {schema} TO {ROLE}")
        op.execute(f"GRANT SELECT ON ALL TABLES IN SCHEMA {schema} TO {ROLE}")
        # gilt für Tabellen, die der Migrationsbenutzer später anlegt
        op.execute(f"ALTER DEFAULT PRIVILEGES IN SCHEMA {schema} GRANT SELECT ON TABLES TO {ROLE}")
    # Größen und Kompressionsstand in den Status-Panels
    op.execute(f"GRANT pg_read_all_stats TO {ROLE}")

    password = os.environ.get("PDB_GRAFANA_DB_PASSWORD", "")
    if password:
        literal = password.replace("'", "''")
        op.execute(f"ALTER ROLE {ROLE} LOGIN PASSWORD '{literal}'")


def downgrade() -> None:
    for schema in SCHEMAS:
        op.execute(
            f"ALTER DEFAULT PRIVILEGES IN SCHEMA {schema} REVOKE SELECT ON TABLES FROM {ROLE}"
        )
        op.execute(f"REVOKE ALL ON ALL TABLES IN SCHEMA {schema} FROM {ROLE}")
        op.execute(f"REVOKE USAGE ON SCHEMA {schema} FROM {ROLE}")
    op.execute(f"REVOKE pg_read_all_stats FROM {ROLE}")
    op.execute(f"DROP ROLE IF EXISTS {ROLE}")
