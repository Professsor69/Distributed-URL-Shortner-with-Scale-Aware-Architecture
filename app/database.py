"""
SQLAlchemy engine, session factory, and declarative base.

Connection pool settings:
  - pool_pre_ping=True  : validates the connection before use (prevents
                          "MySQL server has gone away" errors on idle connections)
  - pool_size=10        : base number of persistent connections
  - max_overflow=20     : extra connections allowed when pool is exhausted

These values are reasonable for Phase 1 single-instance dev.
They become tunable knobs once Locust load testing reveals the real bottleneck.
"""

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, DeclarativeBase
from app.config import settings


DATABASE_URL = (
    f"mysql+pymysql://{settings.db_user}:{settings.db_password}"
    f"@{settings.db_host}:{settings.db_port}/{settings.db_name}"
)

engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
    pool_size=10,
    max_overflow=20,
    echo=False,  # Set to True to log all SQL statements (useful for debugging)
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    """Shared declarative base for all ORM models."""
    pass


def get_db():
    """
    FastAPI dependency that provides a database session per request.
    Ensures the session is always closed, even on exceptions.
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
