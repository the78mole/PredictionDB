"""Modell-Registry und Prognosefehler für das Status-Dashboard

Revision ID: 0012_ml_registry
Revises: 0011_freshness_year
Create Date: 2026-10-24

Die Registry selbst bleibt als Dateien in Garage (``ml_train.registry``). ``ml-train registry
sync`` spiegelt Versionen, Rollen, Verlauf und Backtest-Kennzahlen in die Datenbank, damit
Grafana sie lesen kann. ``fcst.prediction_error`` stellt jede Medianprognose dem Ist-Wert
(Stundenmittel aus ``energy.value_best``) gegenüber.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0012_ml_registry"
down_revision: str | None = "0011_freshness_year"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE meta.model_version (
          version          text PRIMARY KEY,
          config           text NOT NULL,
          variant          text NOT NULL,
          role             text NOT NULL
                           CHECK (role IN ('champion', 'challenger', 'baseline', 'previous',
                                           'retired')),
          created          timestamptz,
          train_from       date,
          train_to         date,
          train_rows       integer,
          train_seconds    real,
          backtest_seconds real,
          snapshot_id      text,
          git_commit       text,
          manifest         jsonb NOT NULL,
          synced_at        timestamptz NOT NULL DEFAULT now()
        )""")
    # Backtest-Kennzahlen je Version, Ziel und Vorlauftag; lead_day 0 = alle Vorlauftage
    op.execute("""
        CREATE TABLE meta.model_metric (
          version    text NOT NULL REFERENCES meta.model_version ON DELETE CASCADE,
          target     text NOT NULL,
          lead_day   smallint NOT NULL,
          n          integer NOT NULL,
          mae        double precision,
          rmse       double precision,
          bias       double precision,
          pinball    double precision,
          coverage80 double precision,
          PRIMARY KEY (version, target, lead_day)
        )""")
    op.execute("""
        CREATE TABLE meta.model_event (
          config   text NOT NULL,
          at       timestamptz NOT NULL,
          action   text NOT NULL,
          version  text NOT NULL,
          note     text NOT NULL DEFAULT '',
          PRIMARY KEY (config, at, action, version)
        )""")
    op.execute("""
        CREATE VIEW fcst.prediction_error AS
        SELECT p.model_version, p.target, p.issue_time, p.valid_time,
               (p.valid_time AT TIME ZONE 'Europe/Berlin')::date
                 - (p.issue_time AT TIME ZONE 'Europe/Berlin')::date AS lead_day,
               p.value AS q50, a.actual, p.value - a.actual AS err
          FROM fcst.prediction p
          CROSS JOIN LATERAL (
            SELECT avg(b.value) AS actual
              FROM energy.value_best b
             WHERE b.code = p.target
               AND b.ts >= p.valid_time AND b.ts < p.valid_time + interval '1 hour'
          ) a
         WHERE p.quantile = 0.5""")


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS fcst.prediction_error")
    op.execute("DROP TABLE IF EXISTS meta.model_event")
    op.execute("DROP TABLE IF EXISTS meta.model_metric")
    op.execute("DROP TABLE IF EXISTS meta.model_version")
