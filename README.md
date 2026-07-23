# Distributed URL Shortener — Scale-Aware Architecture

A URL shortening service designed from the ground up to demonstrate real system-design thinking — not just CRUD. Every architectural decision traces directly to a concept tested in SDE interviews at Google, Meta, Amazon, and Microsoft. Built across 6 progressive phases, each one adding a production-grade layer with measured, quantified trade-offs.

---

## Architecture

```
                          ┌─────────────────────────────────────────┐
                          │        FastAPI Service (4 workers)       │
   Client ───────────────▶│  POST /shorten  ·  GET /{code}          │
                          │  GET /stats/{code}  ·  GET /metrics/*    │
                          │  GET /dashboard/                         │
                          └──┬──────────┬──────────────┬────────────┘
                             │          │              │
                     ┌───────▼──┐  ┌────▼─────┐  ┌───▼────────────┐
                     │  Redis   │  │  MySQL   │  │   RabbitMQ     │
                     │  Cache   │  │  Source  │  │   Click Queue  │
                     │  Phase 2 │  │  of Truth│  │   Phase 4      │
                     │  256MB   │  │  Phase 1 │  └───────┬────────┘
                     │  LRU     │  │  InnoDB  │          │ async
                     └──────────┘  └──────────┘  ┌───────▼────────┐
                                                  │  Worker        │
                     ┌──────────┐                 │  consumer.py   │
                     │  Redis   │                 └───────┬────────┘
                     │  Limiter │                         │
                     │  Phase 3 │                 ┌───────▼────────┐
                     │  Sliding │                 │   MongoDB      │
                     │  Window  │                 │   Click Events │
                     └──────────┘                 │   Phase 4      │
                                                  └────────────────┘

Load Tested: Locust — 312 RPS (50u) → 1,283 RPS (200u, 4 workers)  [Phase 5]
Containerized: docker compose up -d brings up all 6 services         [Phase 6]
```

---

## Setup — One Command

```bash
git clone https://github.com/Professsor69/Distributed-URL-Shortner-with-Scale-Aware-Architecture
cd Distributed-URL-Shortner-with-Scale-Aware-Architecture
cp .env.example .env        # defaults work out-of-the-box with docker-compose
docker compose up -d        # builds images, starts all 6 services
```

Wait ~30 seconds for MySQL and RabbitMQ to pass their healthchecks, then:

| URL | What |
|---|---|
| `http://localhost:8000/docs` | Swagger UI — interactive API explorer |
| `http://localhost:8000/dashboard/` | Analytics dashboard |
| `http://localhost:15672` | RabbitMQ management (guest / guest) |

**Local development (no Docker):**
```bash
python -m venv venv && venv\Scripts\activate
pip install -r requirements.txt
docker compose up -d mysql redis rabbitmq mongodb   # infra only
uvicorn app.main:app --reload                        # API dev server
python -m worker.consumer                           # worker (second terminal)
```

---

## Features

| Feature | Implementation | Phase |
|---|---|---|
| URL shortening | Base62 encoder on auto-increment MySQL ID | 1 |
| Idempotent shortening | SHA-256 URL hash → UNIQUE INDEX | 1 |
| Click tracking | Atomic `UPDATE click_count = click_count + 1` | 1 |
| URL expiry | `expires_at` column + 410 Gone response | 1 |
| Redirect caching | Redis cache-aside, `SETEX`, `allkeys-lru` | 2 |
| Latency metrics | `GET /metrics/latency` — p50/p95/p99 HIT vs MISS | 2 |
| Rate limiting | Sliding window, per-IP, atomic Lua script | 3 |
| Async click analytics | RabbitMQ publish → MongoDB consumer worker | 4 |
| Privacy | SHA-256 IP hashing (16-char hex, never raw IPs) | 4 |
| Load tested | Locust — 1,283 RPS, p95 18ms at 200 concurrent users | 5 |
| Containerized | docker compose up -d — 6 services, healthcheck-ordered | 6 |
| Analytics dashboard | Dark-mode web UI — click count, cache metrics, latency table | 6 |

---

## Phase 5: Load Test Results

> The most important performance story in this project.

### Test setup
- Tool: Locust `--headless --users N --spawn-rate 5 --run-time 60s`
- Task weights: 70% GET redirect · 20% GET stats · 10% POST shorten
- All Locust workers share `127.0.0.1` (rate limiting 429s counted as success)

### Results

| Scenario | Users | Total RPS | GET redirect p50 | GET redirect p95 | Error % |
|---|---|---|---|---|---|
| Baseline | 50 | **312** | 3ms | 12ms | 0% |
| High load — before fix | 200 | 475 | 15ms | **180ms** ⚠️ | 0% |
| High load — after fix | 200 | **1,283** | 2ms | **18ms** ✅ | 0% |

**Cache hit rate:** 94.2% → 96.1% · **Improvement: +170% RPS, 10× lower p95**

### Bottleneck: Python GIL

The bottleneck at 200 users was **not MySQL, not Redis, not the connection pool** — it was Python's GIL. A single uvicorn process means one thread executes Python bytecode at a time. With 200 concurrent requests, threads queue for a single CPU slot.

**Fix: `uvicorn app.main:app --workers 4`**

Zero code change. Four independent processes → four GIL domains → four CPU cores. Already baked into the Docker image `CMD` in `Dockerfile`.

Full analysis: [`load_test/results/load_test_report.md`](load_test/results/load_test_report.md)

---

## API Reference

### `POST /shorten`
```json
// Request
{ "url": "https://github.com/Professsor69" }

// Response 201
{
  "short_code": "000001",
  "short_url":  "http://localhost:8000/000001",
  "long_url":   "https://github.com/Professsor69",
  "created":    true
}
```
Same URL → `"created": false` with the existing code (idempotent).
Rate limited: 10 requests per 60s per IP (sliding window).

### `GET /{short_code}`
307 redirect to original URL. Headers: `X-Cache: HIT|MISS`.  
Returns 404 if not found, 410 if expired.

### `GET /stats/{short_code}`
```json
{
  "short_code":  "000001",
  "long_url":    "https://github.com/Professsor69",
  "click_count": 42,
  "created_at":  "2025-07-22T01:40:00",
  "expires_at":  null
}
```

### `GET /metrics/latency`
```json
{
  "cache_hit_rate": "94.2%",
  "cache_hits":   { "p50_ms": 0.3, "p95_ms": 1.1, "p99_ms": 2.8, "count": 941 },
  "cache_misses": { "p50_ms": 18.2, "p95_ms": 55.1, "p99_ms": 91.0, "count": 58 }
}
```

---

## Technical Decisions

| Decision | Why |
|---|---|
| Encode the **auto-increment ID**, not a hash | IDs are monotonically increasing → zero collision risk, no resolution strategy needed |
| `url_hash` CHAR(64) UNIQUE INDEX | TEXT columns can't be indexed in MySQL; SHA-256 gives O(1) duplicate detection |
| `SETEX` not `SET` + `EXPIRE` | SETEX is atomic — eliminates the race where a key exists without a TTL |
| Lua script for rate limiting | All 4 Redis ops (ZREM, ZADD, ZCARD, EXPIRE) run atomically — no concurrent race between check and increment |
| Sliding window over token bucket | Eliminates the fixed-window boundary exploit (2N requests in 2 seconds) |
| RabbitMQ over direct MongoDB writes | `<1ms` publish vs 5-20ms DB write on the hottest path; queue buffers during DB downtime |
| `basic_qos(prefetch_count=1)` | One message in flight per consumer — enables fair multi-worker load distribution |
| `pool_timeout=10` (was 30) | Fail fast on pool exhaustion — don't stall requests for 30 seconds before surfacing an error |
| Multi-stage Docker build | Builder stage installs pip packages; runtime stage copies only `/root/.local` — no build tools or cache in the final image |

---

## Known Limitations — Deliberate Scope Cuts

These are intentional decisions, not oversights. Each one has a clearly identified production path.

| Limitation | Current behaviour | Production path |
|---|---|---|
| **At-least-once delivery** | NACK + requeue on MongoDB error may produce duplicate click documents if a write succeeds but the ACK is lost | Idempotency keys in MongoDB (`_id` based on message hash) or distributed transaction coordination. Acceptable tradeoff: click analytics is not billing-critical, so rare overcounting beats the complexity of exactly-once. |
| **No dead-letter queue (DLQ)** | Malformed messages are dropped via `basic_nack(requeue=False)` — no inspection path | Add `x-dead-letter-exchange` to `queue_declare`. Single argument change, excluded here to keep infrastructure simple. |
| **MySQL max_connections gap** | 4 workers × 60 max connections = 240, exceeding MySQL 8.0's default `max_connections=151` | Add PgBouncer or ProxySQL as a connection pooler in front of MySQL, or raise `max_connections` in MySQL config. Excluded here to keep portfolio infrastructure manageable. |

---

## Running Tests

```bash
# All 101 unit tests (zero infrastructure required)
pytest tests/ -v

# Individual test files
pytest tests/test_encoder.py -v        # Base62 encoder
pytest tests/test_cache_unit.py -v     # Redis cache layer
pytest tests/test_limiter_unit.py -v   # Sliding window rate limiter
pytest tests/test_publisher_unit.py -v # RabbitMQ publisher
pytest tests/test_worker_unit.py -v    # MongoDB consumer worker
```

## Running Load Tests

```bash
# Interactive web UI at http://localhost:8089
locust --host http://localhost:8000 -f load_test/locustfile.py

# Headless (saves HTML report)
locust --headless --users 50 --spawn-rate 5 --run-time 60s \
       --host http://localhost:8000 \
       --html load_test/results/baseline_report.html \
       -f load_test/locustfile.py

# PowerShell script (runs baseline + 200-user test automatically)
.\load_test\run_load_test.ps1
```

---

## Project Roadmap

| Phase | Feature | Status |
|---|---|---|
| 1 | Core API + Base62 encoder + MySQL | ✅ |
| 2 | Redis cache-aside + latency metrics | ✅ |
| 3 | Sliding window rate limiter (Lua + Redis sorted sets) | ✅ |
| 4 | Async click analytics (RabbitMQ → MongoDB worker) | ✅ |
| 5 | Locust load testing + GIL bottleneck fix | ✅ |
| 6 | Docker Compose (6 services) + analytics dashboard | ✅ |
