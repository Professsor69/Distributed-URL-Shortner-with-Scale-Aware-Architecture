"""
Redis cache layer — cache-aside pattern for URL lookups.

Design overview
---------------
Pattern  : Cache-aside (lazy loading + optional eager population on POST /shorten)
Key      : "url:cache:{short_code}"     ← namespaced prefix avoids collisions
Value    : JSON {"id": int, "url": str} ← stores url_id alongside long_url
TTL      : Configurable via REDIS_TTL_SECONDS (default: 3600s = 1 hour)
Eviction : allkeys-lru (configured in docker-compose) — hot URLs survive memory pressure

Why store url_id in the cache?
    On a cache HIT we want zero MySQL reads. But we still need to increment
    click_count. By caching {"id": url_id, "url": long_url}, one Redis GET
    gives us everything needed to redirect AND do the atomic click_count UPDATE
    without any MySQL SELECT at all. This is the key performance gain of Phase 2.

Why SETEX and not SET + EXPIRE?
    SETEX is a single atomic Redis command. SET followed by EXPIRE has a small
    window between the two commands where the key exists without a TTL. If Redis
    crashes in that window, the key becomes permanent. SETEX eliminates that race.

Why fail-open?
    Redis is a performance layer, not a correctness dependency. If Redis goes down,
    every operation catches the exception, logs a warning, and returns a safe default
    (None / no-op). Requests fall through to MySQL seamlessly with zero errors.

Latency metrics
    We record lookup latency into two bounded Redis lists (LPUSH + LTRIM, max 1000
    samples each). This persists across server restarts and feeds the /metrics/latency
    endpoint without any in-process state.
"""

import json
import logging
from typing import Optional

import redis

from app.config import settings

logger = logging.getLogger(__name__)

# ── Key namespace ──────────────────────────────────────────────────────────────
_URL_PREFIX = "url:cache:"           # e.g. "url:cache:000001"
_HITS_KEY   = "metrics:latency:hits"
_MISSES_KEY = "metrics:latency:misses"
_MAX_METRIC_SAMPLES = 1000           # LTRIM keeps last N entries → O(1) memory

# Module-level singleton — created once on first call to get_client()
_client: Optional[redis.Redis] = None


# ── Client factory ─────────────────────────────────────────────────────────────

def get_client() -> redis.Redis:
    """
    Return the shared Redis client, lazily initialised on first call.

    Timeouts (both 1s) ensure a Redis outage causes at most 1 second of added
    latency before the fail-open path kicks in, rather than hanging forever.
    """
    global _client
    if _client is None:
        _client = redis.Redis(
            host=settings.redis_host,
            port=settings.redis_port,
            db=0,
            decode_responses=True,      # work with str, not raw bytes
            socket_connect_timeout=1,   # fail fast on connection refused
            socket_timeout=1,           # don't block a request > 1s
        )
    return _client


def ping() -> bool:
    """Return True if Redis is reachable. Used by /metrics/health."""
    try:
        return bool(get_client().ping())
    except redis.RedisError as exc:
        logger.warning("Redis ping failed: %s", exc)
        return False


# ── URL cache helpers ──────────────────────────────────────────────────────────

def get_cached_url(short_code: str) -> Optional[dict]:
    """
    Look up a short_code in the cache.

    Returns {"id": int, "url": str} on a HIT, None on a MISS or any Redis error.
    Fail-open: exceptions are logged and caller falls through to MySQL.
    """
    try:
        raw = get_client().get(f"{_URL_PREFIX}{short_code}")
        if raw is None:
            return None
        return json.loads(raw)
    except (redis.RedisError, json.JSONDecodeError) as exc:
        logger.warning("Redis GET error for %r: %s", short_code, exc)
        return None  # Fail open → caller queries MySQL instead


def set_cached_url(
    short_code: str,
    url_id: int,
    long_url: str,
    ttl_seconds: int,
) -> None:
    """
    Cache a short_code → {id, url} mapping with an explicit TTL.

    Uses SETEX (atomic SET + EXPIRE) — not two separate commands — to avoid
    the race window where the key could exist without a TTL.
    """
    payload = json.dumps({"id": url_id, "url": long_url})
    try:
        get_client().setex(f"{_URL_PREFIX}{short_code}", ttl_seconds, payload)
    except redis.RedisError as exc:
        logger.warning("Redis SETEX error for %r: %s", short_code, exc)


def delete_cached_url(short_code: str) -> None:
    """
    Evict a short_code from the cache.
    Called when a URL has expired (410 Gone) to prevent stale cache hits.
    """
    try:
        get_client().delete(f"{_URL_PREFIX}{short_code}")
    except redis.RedisError as exc:
        logger.warning("Redis DELETE error for %r: %s", short_code, exc)


# ── Latency metrics ────────────────────────────────────────────────────────────

def record_latency(*, hit: bool, latency_ms: float) -> None:
    """
    Append a latency sample to a bounded Redis list.

    hit=True  → appended to metrics:latency:hits
    hit=False → appended to metrics:latency:misses

    LTRIM keeps only the last MAX_METRIC_SAMPLES entries so memory stays O(1)
    regardless of traffic volume. Keyword-only args (*) prevent accidentally
    swapping `hit` and `latency_ms`. Best-effort: Redis errors are suppressed.
    """
    key = _HITS_KEY if hit else _MISSES_KEY
    try:
        r = get_client()
        r.lpush(key, f"{latency_ms:.4f}")
        r.ltrim(key, 0, _MAX_METRIC_SAMPLES - 1)
    except redis.RedisError:
        pass  # Metrics are non-critical — never let this affect a request


def get_latency_samples() -> dict[str, list[float]]:
    """Return all stored latency samples as {"hits": [...], "misses": [...]}."""
    try:
        r = get_client()
        hits   = [float(x) for x in r.lrange(_HITS_KEY,   0, -1)]
        misses = [float(x) for x in r.lrange(_MISSES_KEY, 0, -1)]
        return {"hits": hits, "misses": misses}
    except redis.RedisError as exc:
        logger.warning("Redis lrange failed: %s", exc)
        return {"hits": [], "misses": []}
