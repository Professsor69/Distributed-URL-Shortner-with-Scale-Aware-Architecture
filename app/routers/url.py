"""
URL shortener endpoints.

Route ordering matters in FastAPI — routes are matched in registration order.
We register /stats/{short_code} BEFORE /{short_code} so the literal prefix
"stats" is not accidentally consumed by the catch-all redirect route.

Endpoints
---------
  POST  /shorten              Create (or return existing) short URL
  GET   /stats/{short_code}   Retrieve click stats for a short code
  GET   /{short_code}         Redirect to the original URL  ← must be last
"""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.config import settings
from app.crud import create_short_url, get_url_by_short_code, increment_click_count
from app.database import get_db
from app.schemas import ShortenRequest, ShortenResponse, URLStatsResponse

router = APIRouter()


# ---------------------------------------------------------------------------
# POST /shorten
# ---------------------------------------------------------------------------

@router.post(
    "/shorten",
    response_model=ShortenResponse,
    status_code=201,
    summary="Shorten a URL",
    description=(
        "Accepts a long URL and returns a short code. "
        "Idempotent: submitting the same URL twice returns the same short code."
    ),
)
def shorten_url(
    payload: ShortenRequest,
    db: Session = Depends(get_db),
) -> ShortenResponse:
    long_url = str(payload.url)
    url_obj, created = create_short_url(db, long_url, payload.expires_at)

    return ShortenResponse(
        short_code=url_obj.short_code,
        short_url=f"{settings.base_url}/{url_obj.short_code}",
        long_url=url_obj.long_url,
        created=created,
    )


# ---------------------------------------------------------------------------
# GET /stats/{short_code}   ← registered BEFORE the catch-all /{short_code}
# ---------------------------------------------------------------------------

@router.get(
    "/stats/{short_code}",
    response_model=URLStatsResponse,
    summary="Get click stats for a short code",
)
def get_stats(
    short_code: str,
    db: Session = Depends(get_db),
) -> URLStatsResponse:
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


# ---------------------------------------------------------------------------
# GET /{short_code}   ← catch-all; MUST be the last route in this file
# ---------------------------------------------------------------------------

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
    url_obj = get_url_by_short_code(db, short_code)

    if not url_obj:
        raise HTTPException(
            status_code=404,
            detail=f"Short code '{short_code}' not found.",
        )

    # Expiry check: compare naive datetimes (MySQL stores without timezone)
    if url_obj.expires_at is not None:
        now_naive = datetime.now(timezone.utc).replace(tzinfo=None)
        if url_obj.expires_at < now_naive:
            raise HTTPException(
                status_code=410,
                detail="This short URL has expired.",
            )

    # Increment click count — atomic SQL UPDATE, not read-modify-write
    # Phase 4 will move this to an async RabbitMQ event to remove DB write
    # from the critical redirect path entirely.
    increment_click_count(db, url_obj.id)

    return RedirectResponse(url=url_obj.long_url, status_code=307)
