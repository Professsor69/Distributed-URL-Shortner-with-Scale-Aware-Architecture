# URL Shortener — Distributed, Scale-Aware Architecture

A URL shortening service built to demonstrate real system design and scalability
thinking — not just CRUD. Each architectural decision maps directly to a concept
tested in SDE interviews at Google, Microsoft, and Amazon.

## Architecture Overview

```
Client
  └── FastAPI Service (POST /shorten, GET /{code}, GET /metrics/*)
        ├── Redis Cache      [Phase 2 ✅] — cache-aside, allkeys-lru, 256MB
        ├── Redis Limiter    [Phase 3 ✅] — sliding window, per-IP, Lua script
        ├── MySQL            [Phase 1 ✅] — source of truth (short_code → long_url)
        └── RabbitMQ         [Phase 4]   — async click event publishing
              └── Worker → MongoDB — click analytics (geo, device, timestamp)

Load Testing: Locust  [Phase 5]
Infrastructure:       Docker Compose (MySQL + Redis now, full stack in Phase 6)
```

## Phase 4: Async Click Analytics (current)

### Architecture

```
GET /{short_code}
  │
  ├── Redis cache-aside (Phase 2)
  ├── MySQL click_count++ (Phase 1)
  └── publish_click_event() → RabbitMQ queue: "click_events"
                                      │
                                      │  (async, decoupled)
                                      ▼
                              worker/consumer.py
                                      │
                                      ▼
                              MongoDB: click_events collection
```

### Why RabbitMQ over direct MongoDB writes?

Synchronously writing to MongoDB on every redirect would add 5-20ms of database latency to the hottest path in the system. Publishing to RabbitMQ takes <1ms and allows the redirect to return immediately. If MongoDB is slow or temporarily down, messages queue up safely in RabbitMQ instead of blocking the redirect or dropping data.

### Privacy: IP Hashing

Raw IPs are never stored in the database. Each client's IP is SHA-256 hashed and truncated to 16 hex characters before publishing. This allows grouping clicks by "same device" within a session without storing PII (Personally Identifiable Information).

### Running the Worker and Querying MongoDB

Start the infrastructure:
```bash
docker compose up -d
```

Start the worker process (in a separate terminal):
```bash
python -m worker.consumer
```

Trigger some redirects via the API or browser, then query MongoDB:
```bash
docker exec -it urlshortener_mongodb mongosh urlshortener_analytics
> db.click_events.find().pretty()
```

## Phase 3: Rate Limiting

### Algorithm: Sliding Window (Redis Sorted Set)

```
POST /shorten  (per-IP)
  │
  ├─ Redis Lua script (atomic):
  │      ZREMRANGEBYSCORE  ← evict requests older than window
  │      ZADD              ← record this request (nanosecond member ID)
  │      ZCARD             ← count requests in window
  │      EXPIRE            ← auto-cleanup when IP goes silent
  │
  ├─ count ≤ limit → 201 Created  +  X-RateLimit-Remaining: N
  └─ count  > limit → 429 Too Many Requests  +  Retry-After: 60
```

### Why sliding window over token bucket?

| | Token Bucket | Sliding Window |
|---|---|---|
| Boundary exploit | Not applicable | Eliminated |
| Burst at instant | Allows full N burst | Spread across window |
| Accuracy | Approximate | Exact |
| State | `{tokens, last_refill}` | Sorted set of timestamps |
| Redis ops | HGET + HSET | ZADD + ZREMRANGEBYSCORE + ZCARD |

The fixed-window flaw: send N at 23:59:59, N more at 00:00:01 → 2N in 2 seconds.
Sliding window eliminates this: the count is always over exactly the last 60s.

### Why Lua script for atomicity?

Without atomicity, two concurrent requests can both see count=9 (limit=10)
and both get approved — violating the limit. The Lua script runs all 4 Redis
commands as one indivisible unit on the server. No interleaving possible.

### Response headers on every POST /shorten

```
X-RateLimit-Limit:     10      ← max requests per window
X-RateLimit-Remaining: 7       ← requests left in current window
X-RateLimit-Window:    60s     ← window duration
X-RateLimit-Reset:     1234567 ← Unix timestamp when window resets
Retry-After:           60      ← only on 429 responses
```

### Verifying rate limiting (curl)

```bash
# 1. Send 11 requests rapidly (limit=10 by default)
for i in $(seq 1 12); do
  curl -s -o /dev/null -w "%{http_code} " -X POST http://localhost:8000/shorten \\
    -H "Content-Type: application/json" -d '{"url":"https://example.com"}'
done
# Expected: 201 201 201 201 201 201 201 201 201 201 429 429

# 2. Inspect rate limit headers on a passing request
curl -v -X POST http://localhost:8000/shorten \\
  -H "Content-Type: application/json" -d '{"url":"https://example.com"}' 2>&1 | grep X-Rate
# X-RateLimit-Limit: 10
# X-RateLimit-Remaining: 6
# X-RateLimit-Window: 60s
# X-RateLimit-Reset: 1234567890
```

## Phase 2: Redis Cache-Aside (current)

### How it works

```
GET /{short_code}
  │
  ├─ Redis GET url:cache:{short_code}
  │      ├── HIT  → 307 redirect immediately  (no MySQL read)
  │      │          increment click_count by cached url_id
  │      │          X-Cache: HIT
  │      │
  │      └── MISS → MySQL SELECT short_code (indexed)
  │                 Redis SETEX with TTL       (lazy loading)
  │                 307 redirect
  │                 X-Cache: MISS
  │
  └─ Latency recorded to Redis list → /metrics/latency
```

### Cache design decisions

| Decision | Why |
|---|---|
| Store `{id, url}` JSON, not just `url` | On HIT, we need `url_id` for the atomic `click_count` UPDATE — no MySQL SELECT at all |
| `SETEX` not `SET` + `EXPIRE` | SETEX is atomic — eliminates the race window where key exists without TTL |
| `allkeys-lru` eviction policy | When Redis hits 256MB, least recently used URL is evicted — hot URLs survive |
| Fail-open on all Redis errors | Redis outage → MySQL-only mode, zero failed requests |
| Eager cache on POST /shorten | First GET after creation is a HIT, not a MISS |
| Bounded latency lists (1000 samples) | LPUSH+LTRIM — O(1) memory regardless of traffic |

### Cache invalidation policy

URLs in this system are **immutable after creation** — there is no update or delete endpoint, so stale cache entries are not possible by construction. The only invalidation path is time-based: if a URL has an `expires_at` set and the redirect endpoint detects it has passed, `delete_cached_url()` is called to evict the entry before returning 410 Gone. TTL-based eviction via `SETEX` handles the normal expiry case automatically. If URL mutability is added in a future phase, an explicit `delete_cached_url()` call would be required in the update/delete handler before or after the MySQL write.

### New endpoints

| Endpoint | Description |
|---|---|
| `GET /metrics/latency` | p50/p95/p99 for cache HITs vs MISSes + hit rate |
| `GET /metrics/health` | Redis ping check |

### Verifying cache behaviour (Postman / curl)

```bash
# First request — cache MISS (populates Redis)
curl -v http://localhost:8000/000001
# Response header: X-Cache: MISS

# Second request — cache HIT (no MySQL read)
curl -v http://localhost:8000/000001
# Response header: X-Cache: HIT

# View latency comparison
curl http://localhost:8000/metrics/latency
# { "cache_hit_rate": "50.0%", "cache_hits": {"p50_ms": 0.3, ...}, ... }
```

## Phase 1: Core Service (current)

What's implemented:
- `POST /shorten` — validates URL, stores in MySQL, returns short code
- `GET /{short_code}` — looks up DB, increments click count, returns 307 redirect
- `GET /stats/{short_code}` — returns click count and metadata
- **Base62 encoder** — maps auto-increment IDs to 6-char URL-safe codes
- **Idempotent shortening** — same URL always returns the same short code
- Optional URL expiry (`expires_at`) with 410 Gone response

## Technical Decisions Worth Explaining in Interviews

| Decision | Why |
|---|---|
| Encode the **auto-increment ID**, not a hash | IDs are monotonically increasing → zero collision risk, no resolution strategy needed |
| `url_hash` CHAR(64) UNIQUE INDEX | TEXT columns can't be indexed in MySQL; SHA-256 gives O(1) duplicate detection |
| Atomic `UPDATE click_count = click_count + 1` | Avoids read-modify-write race condition under concurrent requests |
| `expires_at` column added in Phase 1 | Zero-cost schema addition that enables the TTL expiry story in interviews |
| Route ordering: `/stats/{code}` before `/{code}` | FastAPI matches routes top-down; static prefix must precede catch-all |

## Setup

### Prerequisites
- Python 3.11+
- Docker & Docker Compose (for MySQL)

### 1. Clone and create virtual environment
```bash
git clone <repo>
cd url-shortener
python -m venv venv
venv\Scripts\activate          # Windows
# source venv/bin/activate     # macOS/Linux
```

### 2. Install dependencies
```bash
pip install -r requirements.txt
```

### 3. Start MySQL
```bash
docker compose up -d
```
Wait ~15 seconds for MySQL to initialise. Check health:
```bash
docker compose ps
```

### 4. Configure environment
```bash
copy .env.example .env   # Windows
# cp .env.example .env   # macOS/Linux
```
The default `.env` values match the Docker Compose service — no edits needed for local dev.

### 5. Run the API
```bash
uvicorn app.main:app --reload
```
Tables are created automatically on first startup via `Base.metadata.create_all`.

### 6. Open Swagger UI
[http://localhost:8000/docs](http://localhost:8000/docs)

## Running Tests
```bash
pytest tests/ -v
```
The encoder unit tests run with zero infrastructure (no DB, no Docker required).

## API Reference

### `POST /shorten`
```json
// Request
{ "url": "https://example.com/very/long/path" }

// Response 201
{
  "short_code": "000001",
  "short_url": "http://localhost:8000/000001",
  "long_url": "https://example.com/very/long/path",
  "created": true
}
```
Submitting the same URL again returns `"created": false` with the existing code.

### `GET /{short_code}`
Returns HTTP 307 redirect to the original URL.
Returns 404 if code not found, 410 if expired.

### `GET /stats/{short_code}`
```json
{
  "short_code": "000001",
  "long_url": "https://example.com/very/long/path",
  "click_count": 42,
  "created_at": "2025-07-22T01:40:00",
  "expires_at": null
}
```

## Roadmap

| Phase | Feature | Status |
|---|---|---|
| 1 | Core API + Base62 + MySQL | ✅ Done |
| 2 | Redis cache-aside, latency metrics | ✅ Done |
| 3 | Custom sliding-window rate limiter | ✅ Done |
| 4 | RabbitMQ + async analytics worker | ✅ Done |
| 5 | Locust load testing, bottleneck analysis | 🔲 |
| 6 | Full Docker Compose, analytics dashboard | 🔲 |

