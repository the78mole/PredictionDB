"""``fcst.prediction_error`` ohne Abfrage je Prognosezeile

Revision ID: 0019_prediction_error_join
Revises: 0018_silver_run
Create Date: 2026-10-27

Bisher holte die View den Ist-Wert je Prognosezeile per ``LATERAL`` aus ``energy.value_best``.
Weil die Zeitgrenzen dort aus ``p.valid_time`` kommen, konnte TimescaleDB keinen Chunk
ausschließen: Planung über alle Chunks von ``energy.value`` (VM: ~900, 10 s) und je Zeile ein
Durchlauf. Mit vier aktiven Modellversionen lief das Panel „Prognosefehler im Betrieb" 110 s und
brach in Grafana ab.

Jetzt werden die Ist-Werte einmal zu Stundenmitteln verdichtet und per Hash-Join angehängt:

- nur ab der ersten gespeicherten Prognose (``fcst.prediction_since()``, Ausschluss beim Start)
  und höchstens 400 Tage zurück (``now()``, Ausschluss schon beim Planen); ältere Prognosen
  behalten die Zeile, aber ohne Ist-Wert,
- nur für die Zielgrößen der jüngsten Ausgabe (``fcst.prediction_targets()``).
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0019_prediction_error_join"
down_revision: str | None = "0018_silver_run"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD_VIEW = """
    CREATE OR REPLACE VIEW fcst.prediction_error AS
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
     WHERE p.quantile = 0.5"""


def upgrade() -> None:
    op.execute("""
        CREATE FUNCTION fcst.prediction_since() RETURNS timestamptz
          LANGUAGE sql STABLE AS $$ SELECT min(valid_time) FROM fcst.prediction $$""")
    op.execute("""
        CREATE FUNCTION fcst.prediction_targets() RETURNS text[]
          LANGUAGE sql STABLE AS $$
            SELECT coalesce(array_agg(DISTINCT target), '{}')
              FROM fcst.prediction
             WHERE valid_time >= (SELECT max(valid_time) FROM fcst.prediction)
                                 - interval '1 day' $$""")
    op.execute("""
        CREATE OR REPLACE VIEW fcst.prediction_error AS
        WITH actual AS (
          SELECT b.code, time_bucket('1 hour', b.ts) AS valid_time, avg(b.value) AS actual
            FROM energy.value_best b
           WHERE b.ts >= now() - interval '400 days'
             AND b.ts >= fcst.prediction_since()
             AND b.code = ANY(fcst.prediction_targets())
           GROUP BY 1, 2)
        SELECT p.model_version, p.target, p.issue_time, p.valid_time,
               (p.valid_time AT TIME ZONE 'Europe/Berlin')::date
                 - (p.issue_time AT TIME ZONE 'Europe/Berlin')::date AS lead_day,
               p.value AS q50, a.actual, p.value - a.actual AS err
          FROM fcst.prediction p
          LEFT JOIN actual a ON a.code = p.target AND a.valid_time = p.valid_time
         WHERE p.quantile = 0.5""")


def downgrade() -> None:
    op.execute(OLD_VIEW)
    op.execute("DROP FUNCTION IF EXISTS fcst.prediction_targets()")
    op.execute("DROP FUNCTION IF EXISTS fcst.prediction_since()")
