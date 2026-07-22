"""
app/limiter.py — Sliding Window Rate Limiter

Algorithm choice: Sliding Window over Token Bucket
---------------------------------------------------
Token bucket tracks {tokens_remaining, last_refill_time} and requires float
arithmetic to compute the refill rate. It allows bursts of up to bucket_size
requests at any single instant.

Fixed window (naive alternative) has a well-known boundary exploit: a client
can send N requests at 23:59:59 and N more at 00:00:01, effectively getting
2N requests in one real second. The sliding window eliminates this entirely.

Sliding window counts actual requests in a rolling time period — the count
is always over exactly the last window_seconds, no matter when in time you
ask. This is the most accurate and fair algorithm for API rate limiting.

Implementation: Redis Sorted Set
---------------------------------
Key:    "ratelimit:sw:{ip}"
Member: nanosecond timestamp as string (guarantees uniqueness per request)
Score:  float seconds (for ZREMRANGEBYSCORE range queries)

On every request, in a single atomic Lua script:
    1. ZREMRANGEBYSCORE  — evict entries older than the rolling window
    2. ZADD              — record this request with its unique member
    3. ZCARD             — count entries remaining (= requests in window)
    4. EXPIRE            — auto-cleanup when IP goes silent

Why a Lua script?
    ZADD + ZREMRANGEBYSCORE + ZCARD as separate commands can interleave with
    other clients in a concurrent environment. Two requests could both see
    count=9 when the limit is 10 and both get approved — violating the limit.
    A Lua script executes atomically on the Redis server; no command from any
    other client can interleave between the four steps.

Why nanosecond member IDs?
    Redis sorted set members must be unique. If two requests arrive in the
    same microsecond (possible under heavy load), a float timestamp member
    would deduplicate them — under-counting by one. Using time.time_ns()
    (nanosecond resolution) makes collisions essentially impossible.

Fail-open policy:
    If Redis is unavailable, check_rate_limit returns (False, {}) — the
    request passes through. Rate limiting is a fairness layer, not a
    correctness dependency; a cache outage should never block all traffic.
"""

import logging
import time
from typing import Optional

import redis

from app.cache import get_client
from app.config import settings

logger = logging.getLogger(__name__)

# ── Key namespace ──────────────────────────────────────────────────────────────
_KEY_PREFIX = "ratelimit:sw:"  # e.g. "ratelimit:sw:127.0.0.1"

# ── Atomic Lua script ──────────────────────────────────────────────────────────
#
# KEYS[1]  = sorted set key        e.g. "ratelimit:sw:127.0.0.1"
# ARGV[1]  = now as float seconds  (sorted set score for range queries)
# ARGV[2]  = unique member string  (nanosecond timestamp — prevents deduplication)
# ARGV[3]  = window size (seconds)
#
# Returns: integer request count *including* the current request.
_SLIDING_WINDOW_LUA = """
local key          = KEYS[1]
local now          = tonumber(ARGV[1])
local member       = ARGV[2]
local window       = tonumber(ARGV[3])
local window_start = now - window

-- Step 1: Evict entries that have fallen outside the rolling window
redis.call('ZREMRANGEBYSCORE', key, '-inf', window_start)

-- Step 2: Record this request (nanosecond member ensures uniqueness)
redis.call('ZADD', key, now, member)

-- Step 3: Count all entries currently inside the window
local count = redis.call('ZCARD', key)

-- Step 4: Auto-cleanup — key self-destructs if IP goes silent for one window
redis.call('EXPIRE', key, math.ceil(window))

return count
"""


# ── Public API ─────────────────────────────────────────────────────────────────

def get_client_ip(request) -> str:
    """
    Extract the real client IP from a FastAPI Request object.

    Respects the X-Forwarded-For header added by reverse proxies (nginx,
    AWS ALB, Cloudflare). The header is a comma-separated list of IPs; the
    first entry is always the original client.

    Falls back to request.client.host for direct connections (local dev,
    no proxy).
    """
    forwarded_for = request.headers.get("X-Forwarded-For")
    if forwarded_for:
        # "203.0.113.5, 10.0.0.1, 172.16.0.1" → "203.0.113.5"
        return forwarded_for.split(",")[0].strip()
    if request.client:
        return request.client.host
    return "unknown"


def check_rate_limit(
    ip: str,
    limit: Optional[int] = None,
    window_seconds: Optional[int] = None,
) -> tuple[bool, dict[str, str]]:
    """
    Check whether an IP has exceeded the sliding window rate limit.

    Executes the Lua script atomically on Redis: records this request, evicts
    expired entries, returns the current window count.

    Args:
        ip:             Client IP address — used as the Redis key suffix.
        limit:          Max requests per window. Defaults to settings value.
        window_seconds: Window duration in seconds. Defaults to settings value.

    Returns:
        (is_limited: bool, headers: dict[str, str])

        is_limited=True  → caller must return HTTP 429.
        headers          → X-RateLimit-* dict to attach to the response.
                           Empty dict on Redis errors (fail-open path).

    Headers follow the IETF draft-ietf-httpapi-ratelimit-headers convention
    used by GitHub, Stripe, and most production APIs.
    """
    effective_limit  = limit          if limit          is not None else settings.rate_limit_requests
    effective_window = window_seconds if window_seconds is not None else settings.rate_limit_window_seconds

    try:
        now_ns = time.time_ns()                   # nanosecond precision → unique member
        now_f  = now_ns / 1_000_000_000          # float seconds         → ZSET score
        member = str(now_ns)
        key    = f"{_KEY_PREFIX}{ip}"

        script = get_client().register_script(_SLIDING_WINDOW_LUA)
        count  = int(script(keys=[key], args=[now_f, member, effective_window]))

        is_limited = count > effective_limit
        remaining  = max(0, effective_limit - count)
        reset_at   = int(now_f) + effective_window

        headers: dict[str, str] = {
            "X-RateLimit-Limit":     str(effective_limit),
            "X-RateLimit-Remaining": str(remaining),
            "X-RateLimit-Window":    f"{effective_window}s",
            "X-RateLimit-Reset":     str(reset_at),
        }
        if is_limited:
            # Retry-After: tells the client exactly how long to wait
            headers["Retry-After"] = str(effective_window)

        return is_limited, headers

    except redis.RedisError as exc:
        logger.warning(
            "Rate limiter Redis error for %s: %s — failing open (request passes through)",
            ip,
            exc,
        )
        return False, {}  # Fail open — a Redis outage never blocks all traffic


def clear_rate_limit(ip: str) -> None:
    """
    Delete the sliding window sorted set for an IP address.

    Used ONLY in tests to reset rate limit state between test cases.
    Not exposed via any API endpoint.
    """
    try:
        get_client().delete(f"{_KEY_PREFIX}{ip}")
    except redis.RedisError as exc:
        logger.warning("clear_rate_limit Redis error for %s: %s", ip, exc)
