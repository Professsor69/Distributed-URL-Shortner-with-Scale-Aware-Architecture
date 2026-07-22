"""
URL shortener endpoints.

Route ordering matters in FastAPI — routes are matched in registration order.
We register /stats/{short_code} BEFORE /{short_code} so the literal prefix
"stats" is not accidentally consumed by the catch-all redirect route.

Phase 2 additions
-----------------
GET /{short_code} now implements the full cache-aside pattern:
    1. Redis GET        — sub-millisecond on HIT (no MySQL read at all)
    2. MySQL SELECT     — only on cache MISS (indexed short_code lookup)
    3. Redis SETEX      — populate cache for next request (lazy loading)
    4. X-Cache header   — HIT or MISS, visible in Postman / curl / browser devtools

POST /shorten eagerly populates Redis after creation so the first GET is a HIT.
On every redirect, latency is recorded to Redis lists for /metrics/latency.

Phase 3 additions
-----------------
POST /shorten now enforces a per-IP sliding window rate limit via
rate_limit_dependency. Rejected requests receive HTTP 429 with:
    - X-RateLimit-Limit / Remaining / Window / Reset headers
    - Retry-After: {window_seconds}
All /shorten responses (pass AND fail) include X-RateLimit-* headers.
"""

import time
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app import cache as url_cache
from app.config import settings
from app.crud import create_short_url, get_url_by_short_code, increment_click_count
from app.database import get_db
from app.dependencies import rate_limit_dependency
from app.schemas import ShortenRequest, ShortenResponse, URLStatsResponse

router = APIRouter()


# ── POST /shorten ──────────────────────────────────────────────────────────────

@router.post(
    "/shorten",
    response_model=ShortenResponse,
    status_code=201,
    summary="Shorten a URL",
    description=(
        "Accepts a long URL and returns a short code. "
        "Idempotent: submitting the same URL twice returns the same short code. "
        "Phase 2: eagerly cached in Redis so the first GET is a HIT. "
        "Phase 3: rate-limited per IP (sliding window). Returns 429 when exceeded."
    ),
)
def shorten_url(
    payload: ShortenRequest,
    db: Session = Depends(get_db),
    _rl: None = Depends(rate_limit_dependency),  # Phase 3: rate limit enforcement
) -> ShortenResponse:
    long_url = str(payload.url)
    url_obj, created = create_short_url(db, long_url, payload.expires_at)

    # Phase 2: Eager cache population
    # The first GET after creation hits Redis (HIT), not MySQL (MISS).
    url_cache.set_cached_url(
        short_code=url_obj.short_code,
        url_id=url_obj.id,
        long_url=url_obj.long_url,
        ttl_seconds=settings.redis_ttl_seconds,
    )

    return ShortenResponse(
        short_code=url_obj.short_code,
        short_url=f"{settings.base_url}/{url_obj.short_code}",
        long_url=url_obj.long_url,
        created=created,
    )


# ── GET /stats/{short_code} ────────────────────────────────────────────────────
# Registered BEFORE /{short_code} to prevent the catch-all consuming "stats"

@router.get(
    "/stats/{short_code}",
    response_model=URLStatsResponse,
    summary="Get click stats for a short code",
)
def get_stats(
    short_code: str,
    db: Session = Depends(get_db),
) -> URLStatsResponse:
    # Stats always come from MySQL (source of truth), not the cache
    url_obj = get_url_by_short_code(db, short_code)
    if not url_obj:
        raise HTTPException(
            status_code=404,
            detail=f"Short code '{short_code}' not found.",
        )

    return URLStatsResponse(
        short_code=url_obj.short_code,
        long_url=url_obj.long_url,
        click_count=url_obj.click_count,
        created_at=url_obj.created_at,
        expires_at=url_obj.expires_at,
    )


# ── GET /{short_code} — cache-aside redirect ───────────────────────────────────
# MUST be the last route — it is a catch-all single-segment path

@router.get(
    "/{short_code}",
    status_code=307,
    summary="Redirect to original URL",
    responses={
        307: {"description": "Redirect to the original URL"},
        404: {"description": "Short code not found"},
        410: {"description": "Short URL has expired"},
    },
)
def redirect_url(
    short_code: str,
    db: Session = Depends(get_db),
) -> RedirectResponse:
    # ── Step 1: Redis lookup (cache-aside) ─────────────────────────────────────
    t_redis = time.perf_counter()
    cached = url_cache.get_cached_url(short_code)
    redis_ms = (time.perf_counter() - t_redis) * 1000

    if cached:
        # ── CACHE HIT ──────────────────────────────────────────────────────────
        # The cached value contains {"id": int, "url": str} — no MySQL SELECT.
        # We still increment click_count via an atomic SQL UPDATE using the cached ID.
        url_cache.record_latency(hit=True, latency_ms=redis_ms)
        increment_click_count(db, cached["id"])

        response = RedirectResponse(url=cached["url"], status_code=307)
        response.headers["X-Cache"] = "HIT"
        return response

    # ── Step 2: Cache MISS — query MySQL ──────────────────────────────────────
    t_db = time.perf_counter()
    url_obj = get_url_by_short_code(db, short_code)
    db_ms = (time.perf_counter() - t_db) * 1000
    total_ms = redis_ms + db_ms  # full miss cost = Redis check + MySQL read

    if not url_obj:
        raise HTTPException(
            status_code=404,
            detail=f"Short code '{short_code}' not found.",
        )

    # Expiry check: compare naive datetimes (MySQL stores without timezone info)
    if url_obj.expires_at is not None:
        now_naive = datetime.now(timezone.utc).replace(tzinfo=None)
        if url_obj.expires_at < now_naive:
            url_cache.delete_cached_url(short_code)  # evict stale entry if present
            raise HTTPException(
                status_code=410,
                detail="This short URL has expired.",
            )

    # ── Step 3: Populate cache for the next request (lazy loading) ─────────────
    url_cache.set_cached_url(
        short_code=short_code,
        url_id=url_obj.id,
        long_url=url_obj.long_url,
        ttl_seconds=settings.redis_ttl_seconds,
    )

    url_cache.record_latency(hit=False, latency_ms=total_ms)
    increment_click_count(db, url_obj.id)

    response = RedirectResponse(url=url_obj.long_url, status_code=307)
    response.headers["X-Cache"] = "MISS"
    return response
