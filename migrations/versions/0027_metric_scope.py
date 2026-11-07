"""Backtest-Kennzahlen je Sicht (alle Testzeilen oder Vergleichsfenster) und Jahreszeit

Revision ID: 0027_metric_scope
Revises: 0026_storage_usage
Create Date: 2026-11-05

``meta.model_metric`` bekommt die Spalte ``scope``: ``all`` für alle Backtest-Zeilen einer
Version (bisheriger Inhalt), sonst der Name des Vergleichsfensters aus der Konfiguration
(``compare.name``, docs/BACKTEST.md Kap. 7). ``meta.model_metric_scope`` merkt sich je Version
und Sicht, ob sie geprüft wurde und ob der Backtest das Fenster abdeckt, damit ``registry sync``
``backtest.parquet`` nicht bei jedem Abgleich erneut liest. Die Spalte ``season`` trennt die
Kennzahlen je Jahreszeit (``Winter``, ``Frühling``, ``Sommer``, ``Herbst``) von denen über alle
(``all``, bisheriger Inhalt).

Die Tabelle der geprüften Sichten startet leer: Der erste Abgleich danach liest jede
``backtest.parquet`` einmal und ergänzt die Zeilen je Jahreszeit und im Fenster; vorhandene
Zeilen bleiben (``ON CONFLICT DO NOTHING``).
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0027_metric_scope"
down_revision: str | None = "0026_storage_usage"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE meta.model_metric ADD COLUMN scope text NOT NULL DEFAULT 'all'")
    op.execute("ALTER TABLE meta.model_metric ADD COLUMN season text NOT NULL DEFAULT 'all'")
    op.execute("ALTER TABLE meta.model_metric DROP CONSTRAINT model_metric_pkey")
    op.execute(
        "ALTER TABLE meta.model_metric ADD PRIMARY KEY (version, scope, season, target, lead_day)"
    )
    op.execute("""
        CREATE TABLE meta.model_metric_scope (
          version    text NOT NULL REFERENCES meta.model_version ON DELETE CASCADE,
          scope      text NOT NULL,
          covered    boolean NOT NULL,
          checked_at timestamptz NOT NULL DEFAULT now(),
          PRIMARY KEY (version, scope)
        )""")
    # Grafana liest über die Default-Privilegien aus 0004 (gilt auch für neue Tabellen)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS meta.model_metric_scope")
    op.execute("DELETE FROM meta.model_metric WHERE scope <> 'all' OR season <> 'all'")
    op.execute("ALTER TABLE meta.model_metric DROP CONSTRAINT model_metric_pkey")
    op.execute("ALTER TABLE meta.model_metric ADD PRIMARY KEY (version, target, lead_day)")
    op.execute("ALTER TABLE meta.model_metric DROP COLUMN season")
    op.execute("ALTER TABLE meta.model_metric DROP COLUMN scope")
