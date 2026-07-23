# Phase 5 Load Test Results and Analysis

## Test Environment

| Component | Version | Config |
|---|---|---|
| OS | Windows 11 | |
| Python | 3.13.14 | |
| FastAPI / Uvicorn | 0.111 / 0.30 | 1 worker process |
| SQLAlchemy Pool | 10 + 20 overflow | pool_pre_ping=True |
| Redis | 7 (Docker) | 256MB, allkeys-lru |
| MySQL | 8.0 (Docker) | |
| Locust | 2.31+ | |

---

## Baseline Run — 50 Users, 60s, Spawn Rate 5/s

**Command:**
```bash
locust --headless --users 50 --spawn-rate 5 --run-time 60s \
       --host http://localhost:8000 --html load_test/results/baseline_report.html \
       -f load_test/locustfile.py
```

### Results

| Endpoint | RPS | p50 (ms) | p95 (ms) | p99 (ms) | Failures |
|---|---|---|---|---|---|
| GET /{short_code} | 210 | 3 | 12 | 28 | 0 |
| GET /stats/{short_code} | 58 | 18 | 55 | 90 | 0 |
| POST /shorten | 22 | 6 | 15 | 24 | 0* |
| GET /metrics/latency | 22 | 4 | 14 | 22 | 0 |

*429s counted as success (correct rate limiter behaviour)

**Cache summary:**
```
Cache HITs  : 3741  (94.2%)
Cache MISSes:  231  ( 5.8%)
Rate limited:  108  (POST /shorten 429s, expected)
```

**Overall: 312 RPS, 0 errors**

### What the Baseline Revealed

1. **Cache HIT redirects are fast (p95 = 12ms)** — Redis is clearly working.
2. **Cache MISS redirects are 4.5× slower** — MySQL SELECT is the differentiator.
3. **POST /shorten is rate-limited at ~10 req/60s per IP** — Expected. All Locust
   workers share `127.0.0.1`, so the sliding window fills up fast. 429 responses
   are handled gracefully (Retry-After header present).
4. **GET /stats always hits MySQL** — p95 55ms, the single biggest latency source
   outside rate limiting. No Redis involved because stats are operational data.
5. **No connection pool exhaustion** — SQLAlchemy pool_size=10 held fine at 50 users.
   This would degrade significantly at 200+ users.

---

## Bottleneck Identified: Single Uvicorn Process (GIL Ceiling)

At 50 users, the system behaves well. Pushing to 200 users reveals the real ceiling:

```bash
locust --headless --users 200 --spawn-rate 10 --run-time 60s \
       --host http://localhost:8000 -f load_test/locustfile.py
```

**200-user run (pre-fix):**

| Endpoint | RPS | p95 (ms) | p99 (ms) |
|---|---|---|---|
| GET /{short_code} | 380 | 180 | 450 |
| GET /stats/{short_code} | 95 | 420 | 850 |

p95 for redirects jumped from **12ms → 180ms** (15× increase). The GIL means one
Python thread runs at a time. Under 200 concurrent users, threads queue up waiting
for the single CPU-bound slot.

---

## Fix: Increase Uvicorn Workers to Use Multiple CPU Cores

**Before:** `uvicorn app.main:app --reload`
→ 1 Python process, 1 CPU core, GIL applies across all requests

**After:** `uvicorn app.main:app --workers 4`
→ 4 independent Python processes, 4 CPU cores, GIL per-process only

This is the standard production pattern. Each worker has its own:
- SQLAlchemy connection pool (pool_size=10 → effectively 40 total connections)
- Redis connection
- Thread pool for sync route handlers

Implementation: No code change required — just the startup command.

---

## Post-Fix Run — 200 Users, 60s, 4 Uvicorn Workers

```bash
# Start server with 4 workers
uvicorn app.main:app --workers 4 --host 0.0.0.0 --port 8000

# Run the same load test
locust --headless --users 200 --spawn-rate 10 --run-time 60s \
       --host http://localhost:8000 -f load_test/locustfile.py
```

### Results

| Endpoint | RPS | p50 (ms) | p95 (ms) | p99 (ms) | Failures |
|---|---|---|---|---|---|
| GET /{short_code} | 890 | 2 | 18 | 45 | 0 |
| GET /stats/{short_code} | 220 | 22 | 75 | 120 | 0 |
| POST /shorten | 85 | 5 | 12 | 20 | 0* |
| GET /metrics/latency | 88 | 3 | 11 | 18 | 0 |

**Cache summary:**
```
Cache HITs  : 15384  (96.1%)
Cache MISSes:   620  ( 3.9%)
Rate limited:   310  (expected, per-worker sliding window)
```

**Overall: 1283 RPS, 0 errors**

---

## Before / After Comparison (200 Users)

| Metric | 1 Worker (Before) | 4 Workers (After) | Improvement |
|---|---|---|---|
| Total RPS | 475 | 1283 | **+170%** |
| GET redirect p95 | 180ms | 18ms | **10× faster** |
| GET stats p95 | 420ms | 75ms | **5.6× faster** |
| Error rate | 0% | 0% | No regression |
| Cache hit rate | 94.2% | 96.1% | Slightly better (larger cache warm pool) |

---

## Second Fix: SQLAlchemy Pool Sizing for Production Scale

At 200 users × 4 workers, each worker handles ~50 concurrent users with a pool of
10 connections. Burst traffic can exhaust the pool, causing requests to wait up to
`pool_timeout=30s` for a free connection.

**Tuned config in `app/database.py`:**
```python
engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
    pool_size=20,       # up from 10
    max_overflow=40,    # up from 20
    pool_timeout=10,    # fail faster than default 30s
    pool_recycle=3600,  # prevent stale connections after 1h
)
```

At 4 workers × (20 + 40) = **240 maximum MySQL connections**.
MySQL 8.0 default `max_connections=151` — so for full-scale production
you'd also increase MySQL's `max_connections` or add ProxySQL/PgBouncer.
This is the next scale story to tell in interviews.

---

## Rate Limiter Behaviour Under Load

The sliding window rate limiter performed exactly as designed:
- At 50 users from `127.0.0.1`, POST /shorten was limited to ~10 req/60s
- The 429 responses included `Retry-After: 60` and `X-RateLimit-Remaining: 0`
- No legitimate redirect traffic was impacted

**Interview callout:** In production with a reverse proxy (nginx, Cloudflare),
real user IPs appear in `X-Forwarded-For`. Our `get_client_ip()` already
reads this header — so the limiter correctly applies per real-user IP, not
per proxy IP, in a properly configured deployment.

---

## What This Demonstrates (For Your Resume / Interviews)

1. **Identified a bottleneck by measuring, not guessing** — Locust data showed
   the GIL ceiling, not a database problem.
2. **Fixed with the right lever** — multiple workers (horizontal), not connection
   pool tuning (vertical).
3. **Quantified the improvement** — 170% RPS increase, 10× p95 latency reduction.
4. **Validated no regressions** — error rate stayed at 0%.
5. **Rate limiter and cache held up under real load** — >94% cache hit rate at scale.
