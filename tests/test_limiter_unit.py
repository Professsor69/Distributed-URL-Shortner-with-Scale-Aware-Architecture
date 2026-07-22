"""
Unit tests for app/limiter.py

All tests mock the Redis client — no running Redis or Docker instance required.
Run with:
    pytest tests/test_limiter_unit.py -v

Coverage
--------
check_rate_limit():
    - Returns (False, headers) when request count is within the limit
    - Returns (True,  headers) when request count exceeds the limit
    - X-RateLimit-Remaining computed correctly as max(0, limit - count)
    - X-RateLimit-Remaining is 0 (not negative) when far over the limit
    - Retry-After header IS present on limited responses
    - Retry-After header NOT present on non-limited responses
    - All required X-RateLimit-* fields present in headers
    - Fail-open: Redis error → (False, {})  — never blocks all traffic
    - Uses settings defaults when limit/window_seconds are not passed

get_client_ip():
    - Uses X-Forwarded-For when present
    - Takes the FIRST IP from a comma-separated X-Forwarded-For list
    - Falls back to request.client.host when X-Forwarded-For is absent
    - Returns "unknown" when request.client is None
"""

from unittest.mock import MagicMock, patch

import pytest
import redis as redis_lib

# Patch path: get_client is imported into limiter.py from app.cache
_PATCH = "app.limiter.get_client"


def _make_redis_mock(script_return_value: int) -> MagicMock:
    """
    Build a mock Redis client whose registered script returns script_return_value.

    Mimics: get_client().register_script(lua)(...) == script_return_value
    """
    mock_client = MagicMock()
    mock_script  = MagicMock(return_value=script_return_value)
    mock_client.register_script.return_value = mock_script
    return mock_client


# ── check_rate_limit — pass / fail logic ──────────────────────────────────────

class TestCheckRateLimitLogic:

    def test_not_limited_when_count_within_limit(self):
        from app.limiter import check_rate_limit
        with patch(_PATCH, return_value=_make_redis_mock(5)):
            is_limited, _ = check_rate_limit("127.0.0.1", limit=10, window_seconds=60)
            assert is_limited is False

    def test_limited_when_count_equals_limit_plus_one(self):
        from app.limiter import check_rate_limit
        with patch(_PATCH, return_value=_make_redis_mock(11)):
            is_limited, _ = check_rate_limit("127.0.0.1", limit=10, window_seconds=60)
            assert is_limited is True

    def test_limited_when_count_far_exceeds_limit(self):
        from app.limiter import check_rate_limit
        with patch(_PATCH, return_value=_make_redis_mock(100)):
            is_limited, _ = check_rate_limit("127.0.0.1", limit=10, window_seconds=60)
            assert is_limited is True

    def test_not_limited_exactly_at_limit(self):
        """count == limit is still allowed (limit is inclusive upper bound)."""
        from app.limiter import check_rate_limit
        with patch(_PATCH, return_value=_make_redis_mock(10)):
            is_limited, _ = check_rate_limit("127.0.0.1", limit=10, window_seconds=60)
            assert is_limited is False


# ── check_rate_limit — X-RateLimit-Remaining header ──────────────────────────

class TestRateLimitRemaining:

    def test_remaining_is_limit_minus_count(self):
        from app.limiter import check_rate_limit
        with patch(_PATCH, return_value=_make_redis_mock(3)):
            _, headers = check_rate_limit("127.0.0.1", limit=10, window_seconds=60)
            assert headers["X-RateLimit-Remaining"] == "7"  # 10 - 3

    def test_remaining_is_zero_not_negative_when_over_limit(self):
        """Remaining must never be negative — clamp at 0."""
        from app.limiter import check_rate_limit
        with patch(_PATCH, return_value=_make_redis_mock(25)):
            _, headers = check_rate_limit("127.0.0.1", limit=10, window_seconds=60)
            assert headers["X-RateLimit-Remaining"] == "0"

    def test_remaining_is_zero_exactly_at_limit(self):
        from app.limiter import check_rate_limit
        with patch(_PATCH, return_value=_make_redis_mock(10)):
            _, headers = check_rate_limit("127.0.0.1", limit=10, window_seconds=60)
            assert headers["X-RateLimit-Remaining"] == "0"


# ── check_rate_limit — required header fields ─────────────────────────────────

class TestRateLimitHeaders:

    _REQUIRED_HEADERS = {
        "X-RateLimit-Limit",
        "X-RateLimit-Remaining",
        "X-RateLimit-Window",
        "X-RateLimit-Reset",
    }

    def test_all_required_headers_present_on_pass(self):
        from app.limiter import check_rate_limit
        with patch(_PATCH, return_value=_make_redis_mock(5)):
            _, headers = check_rate_limit("127.0.0.1", limit=10, window_seconds=60)
            assert self._REQUIRED_HEADERS.issubset(headers.keys())

    def test_all_required_headers_present_on_fail(self):
        from app.limiter import check_rate_limit
        with patch(_PATCH, return_value=_make_redis_mock(11)):
            _, headers = check_rate_limit("127.0.0.1", limit=10, window_seconds=60)
            assert self._REQUIRED_HEADERS.issubset(headers.keys())

    def test_limit_header_matches_argument(self):
        from app.limiter import check_rate_limit
        with patch(_PATCH, return_value=_make_redis_mock(1)):
            _, headers = check_rate_limit("127.0.0.1", limit=42, window_seconds=60)
            assert headers["X-RateLimit-Limit"] == "42"

    def test_window_header_includes_unit_suffix(self):
        from app.limiter import check_rate_limit
        with patch(_PATCH, return_value=_make_redis_mock(1)):
            _, headers = check_rate_limit("127.0.0.1", limit=10, window_seconds=120)
            assert headers["X-RateLimit-Window"] == "120s"

    def test_retry_after_present_only_when_limited(self):
        from app.limiter import check_rate_limit
        with patch(_PATCH, return_value=_make_redis_mock(11)):
            _, headers = check_rate_limit("127.0.0.1", limit=10, window_seconds=60)
            assert "Retry-After" in headers

    def test_retry_after_absent_when_not_limited(self):
        from app.limiter import check_rate_limit
        with patch(_PATCH, return_value=_make_redis_mock(5)):
            _, headers = check_rate_limit("127.0.0.1", limit=10, window_seconds=60)
            assert "Retry-After" not in headers

    def test_retry_after_equals_window_seconds(self):
        from app.limiter import check_rate_limit
        with patch(_PATCH, return_value=_make_redis_mock(11)):
            _, headers = check_rate_limit("127.0.0.1", limit=10, window_seconds=60)
            assert headers["Retry-After"] == "60"


# ── check_rate_limit — fail-open on Redis errors ──────────────────────────────

class TestFailOpen:

    def test_fail_open_on_redis_error_returns_false(self):
        """Redis down → request passes through (is_limited=False)."""
        from app.limiter import check_rate_limit
        mock_client = MagicMock()
        mock_client.register_script.return_value = MagicMock(
            side_effect=redis_lib.RedisError("connection refused")
        )
        with patch(_PATCH, return_value=mock_client):
            is_limited, _ = check_rate_limit("127.0.0.1", limit=10, window_seconds=60)
            assert is_limited is False

    def test_fail_open_on_redis_error_returns_empty_headers(self):
        """Redis down → headers dict is empty (no X-RateLimit-* to send)."""
        from app.limiter import check_rate_limit
        mock_client = MagicMock()
        mock_client.register_script.return_value = MagicMock(
            side_effect=redis_lib.RedisError("timeout")
        )
        with patch(_PATCH, return_value=mock_client):
            _, headers = check_rate_limit("127.0.0.1", limit=10, window_seconds=60)
            assert headers == {}


# ── check_rate_limit — defaults from settings ─────────────────────────────────

class TestDefaultSettings:

    def test_uses_settings_defaults_when_no_args(self):
        """
        Without explicit limit/window args, the function reads from settings.
        We verify the Limit header reflects settings.rate_limit_requests.
        """
        from app.limiter import check_rate_limit
        from app.config import settings
        with patch(_PATCH, return_value=_make_redis_mock(1)):
            _, headers = check_rate_limit("127.0.0.1")
            assert headers["X-RateLimit-Limit"] == str(settings.rate_limit_requests)


# ── get_client_ip ──────────────────────────────────────────────────────────────

class TestGetClientIp:

    def _mock_request(self, forwarded_for=None, host="192.168.1.1"):
        req = MagicMock()
        req.headers.get.return_value = forwarded_for
        req.client.host = host
        return req

    def test_uses_x_forwarded_for_when_present(self):
        from app.limiter import get_client_ip
        req = self._mock_request(forwarded_for="203.0.113.5")
        assert get_client_ip(req) == "203.0.113.5"

    def test_takes_first_ip_from_comma_separated_list(self):
        from app.limiter import get_client_ip
        req = self._mock_request(forwarded_for="203.0.113.5, 10.0.0.1, 172.16.0.1")
        assert get_client_ip(req) == "203.0.113.5"

    def test_strips_whitespace_from_forwarded_for(self):
        from app.limiter import get_client_ip
        req = self._mock_request(forwarded_for="  203.0.113.5  , 10.0.0.1")
        assert get_client_ip(req) == "203.0.113.5"

    def test_falls_back_to_client_host_when_no_forwarded_for(self):
        from app.limiter import get_client_ip
        req = self._mock_request(forwarded_for=None, host="192.168.1.100")
        assert get_client_ip(req) == "192.168.1.100"

    def test_returns_unknown_when_client_is_none(self):
        from app.limiter import get_client_ip
        req = MagicMock()
        req.headers.get.return_value = None
        req.client = None
        assert get_client_ip(req) == "unknown"
