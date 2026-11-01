"""Datenbankzugriff (PostgreSQL + TimescaleDB) über SQLAlchemy 2 und psycopg 3.

@relation(SWA-CORE-DB, scope=file)
"""

from sqlalchemy import Engine, create_engine

from predictiondb_core.settings import Settings, get_settings


def create_db_engine(settings: Settings | None = None, **kwargs: object) -> Engine:
    """@relation(SWR-CORE-002, scope=function)"""
    s = settings or get_settings()
    return create_engine(s.database_url, pool_pre_ping=True, **kwargs)
