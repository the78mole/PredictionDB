"""Alembic-Umgebung. Das Schema wird mit handgeschriebenem SQL gepflegt (Timescale-Befehle)."""

from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool

from predictiondb_core.db import create_db_engine
from predictiondb_core.settings import Settings

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)


def run_migrations_offline() -> None:
    context.configure(url=Settings().database_url, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_db_engine(Settings(), poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(connection=connection)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
