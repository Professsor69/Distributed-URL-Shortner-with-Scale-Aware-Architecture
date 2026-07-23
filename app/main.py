"""
FastAPI application entry point.

Lifespan context manager (replaces deprecated on_event handlers):
  - On startup : creates all tables via SQLAlchemy metadata (idempotent).
  - On shutdown: nothing needed (connection pool cleans itself).

Running locally (dev):
  uvicorn app.main:app --reload

Running in production / Docker (Phase 6):
  uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 4

Analytics dashboard:
  http://localhost:8000/dashboard/
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.database import Base, engine
from app.routers import url as url_router
from app.routers import metrics as metrics_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: create tables if they don't exist
    # In a production system this would be replaced by Alembic migrations,
    # but for this project create_all is sufficient and avoids extra tooling.
    Base.metadata.create_all(bind=engine)
    yield
    # Shutdown: pool connections are disposed automatically by SQLAlchemy


app = FastAPI(
    title="URL Shortener",
    description=(
        "A scalable URL shortening service.\n\n"
        "**Architecture highlights:**\n"
        "- Base62 encoding on auto-increment IDs (zero collision risk)\n"
        "- Redis cache-aside pattern (Phase 2)\n"
        "- Async click analytics via RabbitMQ (Phase 4)\n"
        "- Load tested with Locust (Phase 5)"
    ),
    version="0.1.0",
    lifespan=lifespan,
)

app.include_router(url_router.router)
app.include_router(metrics_router.router)

# Phase 6: serve the analytics dashboard as static files
# Accessible at http://localhost:8000/dashboard/
app.mount("/dashboard", StaticFiles(directory="dashboard", html=True), name="dashboard")
