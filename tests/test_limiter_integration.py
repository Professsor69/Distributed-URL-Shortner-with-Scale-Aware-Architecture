"""
Integration tests for the sliding window rate limiter.

These tests connect to a REAL Redis instance and test the limiter end-to-end,
including actual timing for the window reset scenario.

Auto-skipped if Redis is not reachable at localhost:6379.

Run with:
    pytest tests/test_limiter_integration.py -v
    pytest tests/test_limiter_integration.py -v -m integration

Key scenarios
-------------
- Hammer test: send limit+1 requests → first N pass, (N+1)th is blocked
- Remaining counter decrements correctly per request
- Blocked IP recovers after the window TTL expires
- X-RateLimit headers match the real Redis count
- Rate limit key is properly namespaced (does not collide with cache keys)
"""

import time

import pytest

from app.cache import get_client, ping
from app.limiter import check_rate_limit, clear_rate_limit


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

# Use a distinct IP per test class to avoid cross-test interference
_HAMMER_IP  = "integ_limiter_hammer"
_REMAINING_IP = "integ_limiter_remaining"
_RESET_IP   = "integ_limiter_reset"
_HEADER_IP  = "integ_limiter_headers"


# ── Hammer test — the critical end-to-end verification ────────────────────────

class TestHammerTest:
    """
    Rapid-fire N+1 requests at the rate limiter.

    This is the 'slam the endpoint' test described in Phase 3:
        - First N requests should pass (is_limited=False)
        - The (N+1)th should be blocked (is_limited=True)
    """

    _LIMIT  = 5
    _WINDOW = 30  # large window so no expiry during test

    def setup_method(self):
        clear_rate_limit(_HAMMER_IP)

    def teardown_method(self):
        clear_rate_limit(_HAMMER_IP)

    def test_first_n_requests_all_pass(self):
        for i in range(self._LIMIT):
            is_limited, _ = check_rate_limit(
                _HAMMER_IP, limit=self._LIMIT, window_seconds=self._WINDOW
            )
            assert is_limited is False, f"Request {i+1} should have passed but was blocked"

    def test_request_beyond_limit_is_blocked(self):
        # Exhaust the limit
        for _ in range(self._LIMIT):
            check_rate_limit(_HAMMER_IP, limit=self._LIMIT, window_seconds=self._WINDOW)

        # One more should be blocked
        is_limited, _ = check_rate_limit(
            _HAMMER_IP, limit=self._LIMIT, window_seconds=self._WINDOW
        )
        assert is_limited is True, "Request beyond limit should have been blocked"

    def test_pass_fail_sequence_is_correct(self):
        """Full sequence: [False] * LIMIT + [True]"""
        results = []
        for _ in range(self._LIMIT + 1):
            is_limited, _ = check_rate_limit(
                _HAMMER_IP, limit=self._LIMIT, window_seconds=self._WINDOW
            )
            results.append(is_limited)

        assert results[:self._LIMIT] == [False] * self._LIMIT, (
            f"First {self._LIMIT} requests should all pass"
        )
        assert results[self._LIMIT] is True, (
            f"Request {self._LIMIT + 1} should be blocked"
        )


# ── Remaining counter ──────────────────────────────────────────────────────────

class TestRemainingDecrement:
    _LIMIT  = 3
    _WINDOW = 30

    def setup_method(self):
        clear_rate_limit(_REMAINING_IP)

    def teardown_method(self):
        clear_rate_limit(_REMAINING_IP)

    def test_remaining_decrements_per_request(self):
        """
        With limit=3:
          request 1 → count=1, remaining=2
          request 2 → count=2, remaining=1
          request 3 → count=3, remaining=0
        """
        expected_remainders = [2, 1, 0]
        actual_remainders   = []

        for _ in range(self._LIMIT):
            _, headers = check_rate_limit(
                _REMAINING_IP, limit=self._LIMIT, window_seconds=self._WINDOW
            )
            actual_remainders.append(int(headers["X-RateLimit-Remaining"]))

        assert actual_remainders == expected_remainders, (
            f"Remaining should decrement as {expected_remainders}, "
            f"got {actual_remainders}"
        )

    def test_remaining_stays_at_zero_after_limit_exceeded(self):
        """Remaining must not go negative after the limit is exceeded."""
        for _ in range(self._LIMIT + 3):
            _, headers = check_rate_limit(
                _REMAINING_IP, limit=self._LIMIT, window_seconds=self._WINDOW
            )

        assert int(headers["X-RateLimit-Remaining"]) == 0


# ── Window reset ───────────────────────────────────────────────────────────────

class TestWindowReset:
    _LIMIT  = 2
    _WINDOW = 2  # 2-second window — fast enough for test

    def setup_method(self):
        clear_rate_limit(_RESET_IP)

    def teardown_method(self):
        clear_rate_limit(_RESET_IP)

    def test_blocked_ip_recovers_after_window_expires(self):
        """
        Fill the window → verify blocked → wait for window to expire →
        verify unblocked. Tests that EXPIRE + ZREMRANGEBYSCORE correctly
        clears old entries.
        """
        # Step 1: Fill the rate limit
        for _ in range(self._LIMIT):
            check_rate_limit(_RESET_IP, limit=self._LIMIT, window_seconds=self._WINDOW)

        # Step 2: Verify currently blocked
        is_limited, _ = check_rate_limit(
            _RESET_IP, limit=self._LIMIT, window_seconds=self._WINDOW
        )
        assert is_limited is True, "Should be blocked after filling the limit"

        # Step 3: Wait for the window to expire
        time.sleep(self._WINDOW + 1)

        # Step 4: Should now be unblocked
        is_limited, _ = check_rate_limit(
            _RESET_IP, limit=self._LIMIT, window_seconds=self._WINDOW
        )
        assert is_limited is False, (
            f"Should be unblocked after {self._WINDOW}s window expired"
        )


# ── Header values ──────────────────────────────────────────────────────────────

class TestHeaderValues:
    _LIMIT  = 10
    _WINDOW = 60

    def setup_method(self):
        clear_rate_limit(_HEADER_IP)

    def teardown_method(self):
        clear_rate_limit(_HEADER_IP)

    def test_limit_header_matches_configured_limit(self):
        _, headers = check_rate_limit(
            _HEADER_IP, limit=self._LIMIT, window_seconds=self._WINDOW
        )
        assert headers["X-RateLimit-Limit"] == str(self._LIMIT)

    def test_window_header_has_correct_suffix(self):
        _, headers = check_rate_limit(
            _HEADER_IP, limit=self._LIMIT, window_seconds=self._WINDOW
        )
        assert headers["X-RateLimit-Window"] == f"{self._WINDOW}s"

    def test_reset_header_is_in_the_future(self):
        _, headers = check_rate_limit(
            _HEADER_IP, limit=self._LIMIT, window_seconds=self._WINDOW
        )
        reset = int(headers["X-RateLimit-Reset"])
        assert reset > int(time.time()), "Reset timestamp should be in the future"
