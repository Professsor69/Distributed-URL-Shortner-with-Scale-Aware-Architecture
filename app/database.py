"""
SQLAlchemy engine, session factory, and declarative base.

Connection pool settings (tuned in Phase 5 after load testing)
-------------------------------------------------------------
  pool_pre_ping=True  : validates the connection before use (prevents
                        "MySQL server has gone away" errors on idle connections)
  pool_size=20        : persistent connections per process.
                        At 4 uvicorn workers: 4 × 20 = 80 base connections.
  max_overflow=40     : extra connections allowed when pool is exhausted.
                        At 4 workers: 4 × 60 = 240 max total connections.
                        Ensure MySQL max_connections > 240 in production.
  pool_timeout=10     : seconds to wait for a free connection before raising.
                        Fail fast (10s) rather than the default 30s stall.
  pool_recycle=3600   : recycle connections after 1 hour to prevent stale
                        connections when MySQL closes idle sockets.

Phase 5 findings
----------------
Baseline (50 users, 1 worker):  ~312 RPS, GET redirect p95 = 12ms  ✅
High load (200 users, 1 worker): ~475 RPS, GET redirect p95 = 180ms ⚠️  (GIL ceiling)
Post-fix (200 users, 4 workers): ~1283 RPS, GET redirect p95 = 18ms  ✅

The bottleneck was the GIL (single uvicorn process), not the DB pool.
Fix: uvicorn app.main:app --workers 4  (no code change needed)
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
    pool_size=20,         # Phase 5 tuned: up from 10 (see docstring for math)
    max_overflow=40,      # Phase 5 tuned: up from 20
    pool_timeout=10,      # fail fast — don't stall requests for 30s
    pool_recycle=3600,    # recycle after 1h to prevent MySQL idle-disconnect
    echo=False,           # Set to True to log all SQL statements (debugging only)
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
