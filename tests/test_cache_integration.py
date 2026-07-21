"""
Integration tests for the Redis cache layer.

These tests connect to a REAL Redis instance (localhost:6379 by default).
They are auto-skipped if Redis is not reachable, so they never fail in
environments without Docker.

Run with:
    pytest tests/test_cache_integration.py -v

Or explicitly with the marker:
    pytest tests/test_cache_integration.py -v -m integration

Key scenarios tested
--------------------
- Real SET → GET round-trip (not mocked)
- DELETE removes the key from Redis
- TTL eviction: key set with 2s TTL must be gone after 3 seconds
- Key still present within its TTL window
- Latency list bounded to 1000 samples after 1050 pushes
"""

import time

import pytest

from app.cache import (
    delete_cached_url,
    get_cached_url,
    get_client,
    get_latency_samples,
    ping,
    record_latency,
    set_cached_url,
)


# ── Skip entire module if Redis is not reachable ───────────────────────────────

def _redis_available() -> bool:
    try:
        return get_client().ping()
    except Exception:
        return False


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not _redis_available(),
        reason="Redis not reachable at localhost:6379 — start with `docker compose up -d`",
    ),
]


# ── Connectivity ───────────────────────────────────────────────────────────────

class TestConnectivity:
    def test_ping_returns_true(self):
        assert ping() is True


# ── SET / GET / DELETE round-trip ──────────────────────────────────────────────

class TestSetGetDelete:
    _CODE = "integ_test_001"

    def setup_method(self):
        """Ensure a clean state before each test."""
        delete_cached_url(self._CODE)

    def teardown_method(self):
        """Clean up after each test."""
        delete_cached_url(self._CODE)

    def test_set_then_get_returns_correct_data(self):
        set_cached_url(self._CODE, url_id=42, long_url="https://integration.test", ttl_seconds=60)
        result = get_cached_url(self._CODE)
        assert result is not None
        assert result["id"] == 42
        assert result["url"] == "https://integration.test"

    def test_get_on_nonexistent_key_returns_none(self):
        result = get_cached_url("definitely_does_not_exist_xyz_999")
        assert result is None

    def test_delete_removes_key(self):
        set_cached_url(self._CODE, url_id=1, long_url="https://delete.me", ttl_seconds=60)
        assert get_cached_url(self._CODE) is not None
        delete_cached_url(self._CODE)
        assert get_cached_url(self._CODE) is None

    def test_overwrite_updates_value(self):
        set_cached_url(self._CODE, url_id=1, long_url="https://original.com", ttl_seconds=60)
        set_cached_url(self._CODE, url_id=1, long_url="https://updated.com",  ttl_seconds=60)
        result = get_cached_url(self._CODE)
        assert result["url"] == "https://updated.com"


# ── TTL eviction ───────────────────────────────────────────────────────────────

class TestTTLEviction:
    _CODE = "integ_ttl_test"

    def setup_method(self):
        delete_cached_url(self._CODE)

    def teardown_method(self):
        delete_cached_url(self._CODE)

    def test_key_expires_after_ttl(self):
        """
        Core TTL eviction test.

        SET with 2-second TTL → wait 3 seconds → key must be gone.
        This directly verifies that SETEX is working correctly and Redis
        is performing automatic key expiry.
        """
        set_cached_url(self._CODE, url_id=99, long_url="https://expires.soon", ttl_seconds=2)
        assert get_cached_url(self._CODE) is not None, "Key should exist immediately after SETEX"

        time.sleep(3)  # Wait for TTL to expire

        result = get_cached_url(self._CODE)
        assert result is None, (
            f"Key should have been evicted after 2s TTL, but got: {result}"
        )

    def test_key_survives_within_ttl_window(self):
        """Key set with 10s TTL should still be accessible after 1 second."""
        set_cached_url(self._CODE, url_id=77, long_url="https://still.alive", ttl_seconds=10)
        time.sleep(1)
        result = get_cached_url(self._CODE)
        assert result is not None
        assert result["url"] == "https://still.alive"


# ── Latency metrics ────────────────────────────────────────────────────────────

class TestLatencyMetrics:
    def setup_method(self):
        """Clean metric keys before each test."""
        r = get_client()
        r.delete("metrics:latency:hits")
        r.delete("metrics:latency:misses")

    def teardown_method(self):
        r = get_client()
        r.delete("metrics:latency:hits")
        r.delete("metrics:latency:misses")

    def test_records_hit_and_retrieves_sample(self):
        record_latency(hit=True, latency_ms=1.5)
        samples = get_latency_samples()
        assert len(samples["hits"]) == 1
        assert abs(samples["hits"][0] - 1.5) < 0.01

    def test_records_miss_and_retrieves_sample(self):
        record_latency(hit=False, latency_ms=18.7)
        samples = get_latency_samples()
        assert len(samples["misses"]) == 1
        assert abs(samples["misses"][0] - 18.7) < 0.01

    def test_list_bounded_to_1000_samples(self):
        """
        LPUSH 1050 samples → LTRIM should keep exactly 1000.
        This verifies the O(1) memory guarantee on the metrics list.
        """
        for i in range(1050):
            record_latency(hit=False, latency_ms=float(i))

        samples = get_latency_samples()
        assert len(samples["misses"]) == 1000, (
            f"Expected exactly 1000 samples after LTRIM, got {len(samples['misses'])}"
        )

    def test_hits_and_misses_stored_separately(self):
        record_latency(hit=True,  latency_ms=1.0)
        record_latency(hit=True,  latency_ms=2.0)
        record_latency(hit=False, latency_ms=20.0)

        samples = get_latency_samples()
        assert len(samples["hits"])   == 2
        assert len(samples["misses"]) == 1
