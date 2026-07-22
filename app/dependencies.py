"""
FastAPI dependency functions.

Centralising shared dependencies here keeps route handlers clean and makes
testing simple — FastAPI's app.dependency_overrides lets you swap any
dependency in tests without patching internals.

Phase 3: rate_limit_dependency
    Applied to POST /shorten only. Redirect endpoints (GET /{short_code})
    are intentionally unrestricted — rate-limiting read-only redirects would
    penalise end users who are just clicking links.
"""

from fastapi import HTTPException, Request, Response

from app.config import settings
from app.limiter import check_rate_limit, get_client_ip


def rate_limit_dependency(request: Request, response: Response) -> None:
    """
    FastAPI dependency that enforces per-IP sliding window rate limiting.

    Usage in a route handler:
        @router.post("/shorten", dependencies=[Depends(rate_limit_dependency)])

    Behaviour:
        - Calls check_rate_limit(ip) — runs the atomic Lua script on Redis.
        - Adds X-RateLimit-* headers to EVERY response (pass AND fail) so
          clients can track their remaining budget before hitting 429.
        - On limit exceeded: raises HTTPException(429) with Retry-After header.
        - On Redis error: fail-open — headers are empty, request passes through.

    Why attach headers on both pass and fail?
        A client should be able to see "X-RateLimit-Remaining: 2" on a 200
        response and back off proactively, rather than discovering the limit
        only when they hit 429.
    """
    ip = get_client_ip(request)
    is_limited, rl_headers = check_rate_limit(ip)

    # Add rate limit headers to the response regardless of pass/fail
    for header_name, header_value in rl_headers.items():
        response.headers[header_name] = header_value

    if is_limited:
        raise HTTPException(
            status_code=429,
            detail=(
                f"Rate limit exceeded. "
                f"Allowed {settings.rate_limit_requests} requests per "
                f"{settings.rate_limit_window_seconds}s window. "
                f"Retry after {settings.rate_limit_window_seconds} seconds."
            ),
        )
