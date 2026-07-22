"""
app/publisher.py — RabbitMQ click event publisher.

Publishes a structured click event to RabbitMQ on every successful redirect.
The event is consumed asynchronously by worker/consumer.py, which writes
rich per-click analytics to MongoDB.

Design decisions
----------------
Thread-local connections
    FastAPI with uvicorn (sync route handlers) uses a thread pool. pika's
    BlockingConnection is NOT thread-safe — sharing one connection across
    threads causes frame corruption and dropped messages. Using threading.local
    gives each thread its own connection and channel with zero locking overhead.
    In practice, uvicorn's thread pool size bounds the number of connections.

Persistent messages (delivery_mode=2)
    Messages are written to RabbitMQ's disk journal before the publish returns.
    If the broker restarts mid-flight, messages survive. Combined with a durable
    queue, this provides at-least-once delivery guarantees.

Durable queue
    The queue itself survives RabbitMQ restarts. If the worker is down and the
    broker restarts, queued events are not lost.

Fail-open
    Any AMQPError is logged and swallowed. The redirect always succeeds —
    losing an analytics event is far preferable to a failed redirect.

IP hashing (privacy)
    Raw IPs are never published. Each IP is SHA-256 hashed and truncated to
    16 hex characters. This is enough to group clicks from the same device
    within a session without storing any PII in the analytics store.

Click event schema
------------------
{
    "short_code":  str,   e.g. "000001"
    "long_url":    str,   e.g. "https://example.com"
    "timestamp":   str,   ISO 8601 UTC, e.g. "2024-01-15T10:30:00.123456+00:00"
    "user_agent":  str,   HTTP User-Agent (empty string if absent)
    "referrer":    str,   HTTP Referer (empty string if absent)
    "ip_hash":     str,   SHA-256[:16] of client IP (empty string if unknown)
}
"""

import hashlib
import json
import logging
import threading
from datetime import datetime, timezone

import pika
import pika.exceptions

from app.config import settings

logger = logging.getLogger(__name__)

# ── Thread-local state ─────────────────────────────────────────────────────────
# Each thread in uvicorn's thread pool gets its own RabbitMQ connection + channel.
# pika.BlockingConnection is NOT thread-safe; thread-local isolates them fully.
_local = threading.local()


def _get_channel() -> pika.adapters.blocking_connection.BlockingChannel:
    """
    Return this thread's RabbitMQ channel, creating a new connection if needed.

    Reconnects automatically when the broker closes the previous connection.
    Raises pika.exceptions.AMQPError on connection failure (caller handles it).
    """
    if not hasattr(_local, "connection") or _local.connection.is_closed:
        params = pika.URLParameters(settings.rabbitmq_url)
        params.socket_timeout = 2          # fail fast — don't block a request thread
        _local.connection = pika.BlockingConnection(params)
        _local.channel = _local.connection.channel()
        _local.channel.queue_declare(
            queue=settings.analytics_queue,
            durable=True,                  # queue survives RabbitMQ restart
        )
    return _local.channel


def _reset_connection() -> None:
    """Discard this thread's connection so the next publish attempt reconnects."""
    for attr in ("connection", "channel"):
        if hasattr(_local, attr):
            delattr(_local, attr)


# ── Public API ─────────────────────────────────────────────────────────────────

def publish_click_event(
    short_code: str,
    long_url: str,
    user_agent: str = "",
    referrer: str = "",
    ip: str = "",
) -> None:
    """
    Publish a click event to the RabbitMQ analytics queue.

    Called on every successful redirect (cache HIT and MISS alike). Returns
    immediately on RabbitMQ errors — the redirect response is never blocked.

    Args:
        short_code: Short URL code that was clicked, e.g. "000001".
        long_url:   Destination URL.
        user_agent: HTTP User-Agent header value (empty string if absent).
        referrer:   HTTP Referer header value (empty string if absent).
        ip:         Raw client IP (SHA-256 hashed before publishing).
    """
    ip_hash = hashlib.sha256(ip.encode()).hexdigest()[:16] if ip else ""

    event = {
        "short_code": short_code,
        "long_url":   long_url,
        "timestamp":  datetime.now(timezone.utc).isoformat(),
        "user_agent": user_agent,
        "referrer":   referrer,
        "ip_hash":    ip_hash,
    }

    try:
        channel = _get_channel()
        channel.basic_publish(
            exchange="",                          # default exchange — route by queue name
            routing_key=settings.analytics_queue,
            body=json.dumps(event),
            properties=pika.BasicProperties(
                content_type="application/json",
                delivery_mode=pika.DeliveryMode.Persistent,  # disk-backed, survives restart
            ),
        )
    except pika.exceptions.AMQPError as exc:
        logger.warning(
            "Click event publish failed for %r: %s — analytics skipped, redirect unaffected",
            short_code, exc,
        )
        _reset_connection()   # force reconnect on next publish attempt
