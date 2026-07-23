"""
locustfile.py — Phase 5 load test for the URL Shortener service.

Usage
-----
Headless (CI-friendly):
    locust --headless --users 50 --spawn-rate 5 --run-time 60s \
           --host http://localhost:8000 --html load_test/results/report.html

Web UI (interactive):
    locust --host http://localhost:8000
    # then open http://localhost:8089

Task distribution (intentional)
--------------------------------
  70% — GET /{short_code}     (redirect, the hot path — should be cache-HIT fast)
  20% — GET /stats/{short_code} (read stats, always MySQL but cheap SELECT)
  10% — POST /shorten         (URL creation, rate-limited to 10 req/60s per IP)

  The 7:2:1 ratio reflects real-world traffic: mostly reads, rare writes.
  The rate limiter will block most POST /shorten traffic at high concurrency —
  this is expected behaviour, not a bug. 429s from the limiter are excluded
  from failure counts using `catch_response=True`.

What this test reveals
-----------------------
1. Cache HIT vs MISS latency gap (X-Cache header)
2. Rate limiter 429 behaviour under burst (per-IP sliding window)
3. MySQL connection pool saturation (if pool_size is too small)
4. Single uvicorn process CPU ceiling (GIL contention)
"""

import random
import string
from typing import Optional

from locust import HttpUser, between, events, task
from locust.runners import MasterRunner


# ── Shared URL pool (seeded in on_start) ───────────────────────────────────────
# Each virtual user creates URLs in on_start() and stores them here.
# All users can redirect to any seeded URL — ensures a warm cache quickly.
_seeded_codes: list[str] = []


# ── Stats collectors ───────────────────────────────────────────────────────────
_cache_hits:   int = 0
_cache_misses: int = 0
_rate_limited: int = 0


@events.test_stop.add_listener
def on_test_stop(environment, **kwargs):
    """Print cache and rate-limit summary at end of test."""
    total = _cache_hits + _cache_misses
    hit_rate = (_cache_hits / total * 100) if total > 0 else 0
    print(f"\n{'='*60}")
    print(f"  Cache HITs  : {_cache_hits:>6}  ({hit_rate:.1f}%)")
    print(f"  Cache MISSes: {_cache_misses:>6}  ({100-hit_rate:.1f}%)")
    print(f"  Rate limited: {_rate_limited:>6}  (429 from POST /shorten)")
    print(f"{'='*60}\n")


# ── Virtual user ───────────────────────────────────────────────────────────────

class URLShortenerUser(HttpUser):
    """
    Simulates a real-world mix of URL shortener traffic.

    Think-time: 0.1–0.5s between requests to model a realistic user,
    not a pure hammer. Reduce wait_time to (0, 0) for max throughput testing.
    """

    wait_time = between(0.1, 0.5)

    # Each user holds their own pool of short codes for redirect tasks
    _my_codes: list[str]

    def on_start(self) -> None:
        """
        Seed this user's URL pool before any tasks run.
        Creates 3 unique URLs so the redirect tasks always have valid codes.
        Seeded URLs are also added to the global pool for other users.
        """
        self._my_codes = []
        for _ in range(3):
            suffix = "".join(random.choices(string.ascii_lowercase + string.digits, k=12))
            payload = {"url": f"https://example-load-test.com/path/{suffix}"}
            with self.client.post(
                "/shorten",
                json=payload,
                catch_response=True,
                name="POST /shorten [seed]",
            ) as resp:
                if resp.status_code == 201:
                    code = resp.json().get("short_code", "")
                    if code:
                        self._my_codes.append(code)
                        _seeded_codes.append(code)
                    resp.success()
                elif resp.status_code == 429:
                    # Rate limiter during seeding — that's fine, use global pool
                    resp.success()
                else:
                    resp.failure(f"Unexpected seed response: {resp.status_code}")

    # ── Tasks ──────────────────────────────────────────────────────────────────

    @task(7)
    def redirect(self) -> None:
        """
        GET /{short_code} — the hot path.
        Reads X-Cache header to track HIT vs MISS ratio.
        allow_redirects=False so Locust measures only the redirect response,
        not the full downstream request to example.com.
        """
        global _cache_hits, _cache_misses

        # Pick a code — prefer this user's own pool, fall back to global pool
        codes = self._my_codes or _seeded_codes
        if not codes:
            return
        code = random.choice(codes)

        with self.client.get(
            f"/{code}",
            allow_redirects=False,
            catch_response=True,
            name="GET /{short_code}",
        ) as resp:
            if resp.status_code in (307, 301, 302):
                cache_header = resp.headers.get("X-Cache", "MISS")
                if cache_header == "HIT":
                    _cache_hits += 1
                else:
                    _cache_misses += 1
                resp.success()
            elif resp.status_code == 404:
                resp.success()   # code may have been expired; not a test failure
            else:
                resp.failure(f"Unexpected redirect status: {resp.status_code}")

    @task(2)
    def get_stats(self) -> None:
        """
        GET /stats/{short_code} — always hits MySQL (no cache).
        Useful for measuring raw DB read latency under concurrent load.
        """
        codes = self._my_codes or _seeded_codes
        if not codes:
            return
        code = random.choice(codes)
        self.client.get(f"/stats/{code}", name="GET /stats/{short_code}")

    @task(1)
    def shorten_url(self) -> None:
        """
        POST /shorten — URL creation. Rate-limited to 10 req/60s per IP.
        At high concurrency (all users share 127.0.0.1), 429s are expected.
        We catch them as 'success' to measure the limiter's own response time,
        not as failures that would distort the overall failure %.
        """
        global _rate_limited

        suffix = "".join(random.choices(string.ascii_lowercase + string.digits, k=8))
        payload = {"url": f"https://load-test-target.example.com/{suffix}"}

        with self.client.post(
            "/shorten",
            json=payload,
            catch_response=True,
            name="POST /shorten",
        ) as resp:
            if resp.status_code == 201:
                code = resp.json().get("short_code")
                if code:
                    self._my_codes.append(code)
                resp.success()
            elif resp.status_code == 429:
                _rate_limited += 1
                resp.success()   # 429 is correct behaviour, not a failure
            else:
                resp.failure(f"Unexpected POST status: {resp.status_code}")

    @task(1)
    def check_metrics(self) -> None:
        """
        GET /metrics/latency — verifies monitoring endpoint stays responsive.
        If this starts failing under load, the metrics pipeline has a leak.
        """
        self.client.get("/metrics/latency", name="GET /metrics/latency")
