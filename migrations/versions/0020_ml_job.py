"""Fortschritt laufender ``ml-train``-Jobs (snapshot, backtest, fit) für das Status-Dashboard

Revision ID: 0020_ml_job
Revises: 0019_prediction_error_join
Create Date: 2026-10-27

Ein Eintrag je Aufruf. ``ml-train`` führt Schritt und Zähler nach, ein Lebenszeichen hält
``updated_at`` auch in langen Schritten (Training einer Faltung) frisch. Bleibt ein Job ohne
Abschluss stehen (Container hart beendet), markiert ihn der nächste Start als abgebrochen.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0020_ml_job"
down_revision: str | None = "0019_prediction_error_join"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE meta.ml_job (
          job_id          bigserial PRIMARY KEY,
          command         text NOT NULL CHECK (command IN ('snapshot', 'backtest', 'fit')),
          config          text NOT NULL,
          variants        text[] NOT NULL DEFAULT '{}',
          args            jsonb NOT NULL DEFAULT '{}',
          step            text,
          steps_done      int NOT NULL DEFAULT 0,
          steps_total     int,
          status          text NOT NULL DEFAULT 'running'
                            CHECK (status IN ('running', 'done', 'failed', 'aborted')),
          result          text,
          error           text,
          host            text,
          pid             int,
          started_at      timestamptz NOT NULL DEFAULT now(),
          step_started_at timestamptz NOT NULL DEFAULT now(),
          updated_at      timestamptz NOT NULL DEFAULT now(),
          finished_at     timestamptz
        )""")
    op.execute("CREATE INDEX ml_job_started ON meta.ml_job (started_at DESC)")
    # Grafana liest über die Default-Privilegien aus 0004 (gilt auch für neue Tabellen)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS meta.ml_job")
