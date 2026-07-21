"""
Unit tests for app/cache.py

All tests use unittest.mock to patch the Redis client — no running Redis instance
needed. This keeps the test suite runnable in any environment, including CI.

Run with:
    pytest tests/test_cache_unit.py -v

What's tested
-------------
- get_cached_url : returns None on miss, dict on hit, None on Redis error (fail-open),
                   None on malformed JSON, and uses the correct namespaced key
- set_cached_url : calls SETEX with correct namespaced key, TTL, and JSON payload;
                   silently ignores Redis errors
- delete_cached_url : calls DELETE with namespaced key; silently ignores errors
- record_latency    : routes to correct key (hits/misses), trims list after push,
                      silently ignores Redis errors
"""

import json
from unittest.mock import MagicMock, call, patch

import pytest
import redis as redis_lib

# The path we patch — get_client is called inside every cache function
_PATCH = "app.cache.get_client"


# ── get_cached_url ─────────────────────────────────────────────────────────────

class TestGetCachedUrl:
    def test_returns_none_on_cache_miss(self):
        from app.cache import get_cached_url
        with patch(_PATCH) as mock:
            mock.return_value.get.return_value = None
            assert get_cached_url("000001") is None

    def test_returns_dict_on_cache_hit(self):
        from app.cache import get_cached_url
        payload = json.dumps({"id": 1, "url": "https://example.com"})
        with patch(_PATCH) as mock:
            mock.return_value.get.return_value = payload
            result = get_cached_url("000001")
            assert result == {"id": 1, "url": "https://example.com"}

    def test_returns_none_on_redis_error(self):
        from app.cache import get_cached_url
        with patch(_PATCH) as mock:
            mock.return_value.get.side_effect = redis_lib.RedisError("connection refused")
            assert get_cached_url("000001") is None  # Fail open

    def test_returns_none_on_invalid_json(self):
        from app.cache import get_cached_url
        with patch(_PATCH) as mock:
            mock.return_value.get.return_value = "not-valid-json{{{"
            assert get_cached_url("000001") is None  # Fail open

    def test_uses_namespaced_key(self):
        from app.cache import get_cached_url
        with patch(_PATCH) as mock:
            mock.return_value.get.return_value = None
            get_cached_url("abc123")
            mock.return_value.get.assert_called_once_with("url:cache:abc123")

    def test_different_short_codes_use_different_keys(self):
        from app.cache import get_cached_url
        with patch(_PATCH) as mock:
            mock.return_value.get.return_value = None
            get_cached_url("000001")
            get_cached_url("000002")
            calls = mock.return_value.get.call_args_list
            assert calls[0] == call("url:cache:000001")
            assert calls[1] == call("url:cache:000002")


# ── set_cached_url ─────────────────────────────────────────────────────────────

class TestSetCachedUrl:
    def test_calls_setex_with_correct_arguments(self):
        from app.cache import set_cached_url
        with patch(_PATCH) as mock:
            set_cached_url("000001", url_id=1, long_url="https://example.com", ttl_seconds=3600)
            mock.return_value.setex.assert_called_once_with(
                "url:cache:000001",
                3600,
                json.dumps({"id": 1, "url": "https://example.com"}),
            )

    def test_uses_namespaced_key(self):
        from app.cache import set_cached_url
        with patch(_PATCH) as mock:
            set_cached_url("mycode", url_id=99, long_url="https://test.io", ttl_seconds=60)
            key_used = mock.return_value.setex.call_args[0][0]
            assert key_used == "url:cache:mycode"

    def test_ttl_is_passed_correctly(self):
        from app.cache import set_cached_url
        with patch(_PATCH) as mock:
            set_cached_url("000001", url_id=1, long_url="https://x.com", ttl_seconds=7200)
            ttl_used = mock.return_value.setex.call_args[0][1]
            assert ttl_used == 7200

    def test_silently_handles_redis_error(self):
        from app.cache import set_cached_url
        with patch(_PATCH) as mock:
            mock.return_value.setex.side_effect = redis_lib.RedisError("timeout")
            # Must not raise
            set_cached_url("000001", url_id=1, long_url="https://x.com", ttl_seconds=60)


# ── delete_cached_url ──────────────────────────────────────────────────────────

class TestDeleteCachedUrl:
    def test_calls_delete_with_namespaced_key(self):
        from app.cache import delete_cached_url
        with patch(_PATCH) as mock:
            delete_cached_url("000001")
            mock.return_value.delete.assert_called_once_with("url:cache:000001")

    def test_silently_handles_redis_error(self):
        from app.cache import delete_cached_url
        with patch(_PATCH) as mock:
            mock.return_value.delete.side_effect = redis_lib.RedisError("connection reset")
            delete_cached_url("000001")  # Must not raise


# ── record_latency ─────────────────────────────────────────────────────────────

class TestRecordLatency:
    def test_hit_goes_to_hits_key(self):
        from app.cache import record_latency
        with patch(_PATCH) as mock:
            record_latency(hit=True, latency_ms=1.5)
            mock.return_value.lpush.assert_called_once_with("metrics:latency:hits", "1.5000")

    def test_miss_goes_to_misses_key(self):
        from app.cache import record_latency
        with patch(_PATCH) as mock:
            record_latency(hit=False, latency_ms=12.345)
            mock.return_value.lpush.assert_called_once_with("metrics:latency:misses", "12.3450")

    def test_ltrim_called_after_lpush(self):
        from app.cache import record_latency
        with patch(_PATCH) as mock:
            record_latency(hit=True, latency_ms=1.0)
            mock.return_value.ltrim.assert_called_once_with("metrics:latency:hits", 0, 999)

    def test_miss_ltrim_uses_misses_key(self):
        from app.cache import record_latency
        with patch(_PATCH) as mock:
            record_latency(hit=False, latency_ms=5.0)
            mock.return_value.ltrim.assert_called_once_with("metrics:latency:misses", 0, 999)

    def test_latency_formatted_to_4_decimal_places(self):
        from app.cache import record_latency
        with patch(_PATCH) as mock:
            record_latency(hit=True, latency_ms=0.123456789)
            pushed_value = mock.return_value.lpush.call_args[0][1]
            assert pushed_value == "0.1235"  # rounded to 4 decimal places

    def test_silently_handles_redis_error(self):
        from app.cache import record_latency
        with patch(_PATCH) as mock:
            mock.return_value.lpush.side_effect = redis_lib.RedisError("connection reset")
            record_latency(hit=True, latency_ms=1.0)  # Must not raise
