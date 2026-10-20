"""Datenbankzugriff (PostgreSQL + TimescaleDB) über SQLAlchemy 2 und psycopg 3."""

from sqlalchemy import Engine, create_engine

from predictiondb_core.settings import Settings, get_settings


def create_db_engine(settings: Settings | None = None, **kwargs: object) -> Engine:
    s = settings or get_settings()
    return create_engine(s.database_url, pool_pre_ping=True, **kwargs)
