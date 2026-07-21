"""
Metrics endpoints — cache performance and infrastructure health.

GET /metrics/latency
    Returns p50, p95, p99, avg, min, max for:
      - cache HITs  (Redis GET hit → redirect, no MySQL read)
      - cache MISSes (Redis miss → MySQL SELECT → cache populate → redirect)
    Plus overall cache hit rate and sample count.

GET /metrics/health
    Redis ping check — reports "up" or "down".

This data drives the Phase 5 Locust report.
The latency gap between HITs and MISSes is the core resume metric:
  "Achieved X% cache hit rate, reducing p99 redirect latency from Yms to Zms"
"""

import statistics

from fastapi import APIRouter

from app.cache import get_latency_samples, ping

router = APIRouter(prefix="/metrics", tags=["Metrics"])


def _compute_stats(samples: list[float]) -> dict:
    """Compute descriptive statistics over a list of latency values (ms)."""
    if not samples:
        return {
            "count": 0,
            "p50_ms": None,
            "p95_ms": None,
            "p99_ms": None,
            "avg_ms": None,
            "min_ms": None,
            "max_ms": None,
        }

    s = sorted(samples)
    n = len(s)

    def pct(p: float) -> float:
        idx = min(int(n * p / 100), n - 1)
        return round(s[idx], 3)

    return {
        "count": n,
        "p50_ms": pct(50),
        "p95_ms": pct(95),
        "p99_ms": pct(99),
        "avg_ms": round(statistics.mean(samples), 3),
        "min_ms": round(min(samples), 3),
        "max_ms": round(max(samples), 3),
    }


@router.get(
    "/latency",
    summary="Cache latency comparison (HITs vs MISSes)",
    description=(
        "Returns percentile latencies for cache HITs and MISSes, plus the "
        "overall cache hit rate. Based on the last 1,000 redirect requests."
    ),
)
def get_latency_metrics() -> dict:
    samples = get_latency_samples()
    hits   = samples["hits"]
    misses = samples["misses"]
    total  = len(hits) + len(misses)

    hit_rate = f"{len(hits) / total * 100:.1f}%" if total > 0 else "N/A"

    return {
        "cache_hit_rate": hit_rate,
        "total_requests_sampled": total,
        "cache_hits": _compute_stats(hits),
        "cache_misses": _compute_stats(misses),
        "methodology": (
            "HIT latency  = time for Redis GET only (no MySQL read). "
            "MISS latency = Redis GET + MySQL SELECT + Redis SETEX. "
            "Samples stored via LPUSH+LTRIM (last 1,000 per category)."
        ),
    }


@router.get(
    "/health",
    summary="Infrastructure health check",
    description="Checks Redis connectivity via PING command.",
)
def redis_health() -> dict:
    alive = ping()
    return {
        "redis": "up" if alive else "down",
        "status": (
            "healthy"
            if alive
            else "degraded — falling back to MySQL-only mode"
        ),
    }
