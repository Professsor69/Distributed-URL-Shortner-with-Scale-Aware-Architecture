# URL Shortener — Distributed, Scale-Aware Architecture

A URL shortening service built to demonstrate real system design and scalability
thinking — not just CRUD. Each architectural decision maps directly to a concept
tested in SDE interviews at Google, Microsoft, and Amazon.

## Architecture Overview

```
Client
  └── FastAPI Service (POST /shorten, GET /{code}, GET /metrics/*)
        ├── Redis Cache      [Phase 2 ✅] — cache-aside, allkeys-lru, 256MB
        ├── MySQL            [Phase 1 ✅] — source of truth (short_code → long_url)
        └── RabbitMQ         [Phase 4]   — async click event publishing
              └── Worker → MongoDB — click analytics (geo, device, timestamp)

Load Testing: Locust  [Phase 5]
Infrastructure:       Docker Compose (MySQL + Redis now, full stack in Phase 6)
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
| 2 | Redis cache-aside, latency metrics | 🔲 |
| 3 | Custom token-bucket rate limiter | 🔲 |
| 4 | RabbitMQ + async analytics worker | 🔲 |
| 5 | Locust load testing, bottleneck analysis | 🔲 |
| 6 | Full Docker Compose, analytics dashboard | 🔲 |
