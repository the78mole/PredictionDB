"""Lizenzklasse „nur Training“: meta.source.train

Revision ID: 0027_source_train
Revises: 0026_storage_usage
Create Date: 2026-11-04

``train`` sagt, ob ein veröffentlichtes Modell aus der Quelle lernen darf. Freie Quellen
(``free``) dürfen das immer. Quellen, deren Bedingungen nur die Vervielfältigung von einer
Zustimmung abhängig machen, „soweit sie nicht ohnehin gesetzlich gestattet ist“
(Netztransparenz, regelleistung.net), dürfen fürs Training genutzt werden (Text- und
Data-Mining, § 44b und § 87c UrhG), ihre Reihen werden aber nicht weitergegeben
(docs/ARCHITEKTUR.md 7.11.1). ENTSO-E außerhalb der Freigabeliste, die Thomson-Reuters-Reihen
der EIA und THE (Disclaimer ohne diesen Vorbehalt) bleiben ganz außen vor. ``energy-ingest``
schreibt den Wert beim Start ohnehin neu.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0027_source_train"
down_revision: str | None = "0026_storage_usage"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NO_TRAIN = ("entsoe", "eia_reuters", "the")


def upgrade() -> None:
    op.execute("ALTER TABLE meta.source ADD COLUMN train boolean NOT NULL DEFAULT true")
    codes = ", ".join(f"'{c}'" for c in NO_TRAIN)
    op.execute(f"UPDATE meta.source SET train = false WHERE code IN ({codes})")


def downgrade() -> None:
    op.execute("ALTER TABLE meta.source DROP COLUMN train")
