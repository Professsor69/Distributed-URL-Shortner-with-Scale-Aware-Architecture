"""
worker/consumer.py — Click analytics consumer.

Standalone process that consumes click events from RabbitMQ and persists
them as rich analytics documents in MongoDB.

Usage (from project root):
    python -m worker.consumer

Architecture
------------
    FastAPI redirect handler
      └── publish_click_event() ──→ RabbitMQ: "click_events" queue
                                              │
                                    ┌─────────┘  (async, decoupled)
                                    ▼
                            worker/consumer.py (this file)
                              └── on_message() ──→ MongoDB: click_events collection

Why this separation exists
--------------------------
The redirect endpoint is on the hot path — every click hits it. A synchronous
MongoDB write on every click would add 5–20ms of DB latency. By publishing to
RabbitMQ (<1ms) and handling the write in a separate process, the redirect stays
sub-millisecond while analytics are processed at the worker's own pace.

If MongoDB is slow or temporarily unavailable, messages queue up in RabbitMQ and
are processed when it recovers. A direct write would either block or lose data.

Reliability model
-----------------
  basic_qos(prefetch_count=1): process one message at a time, ensures
      fair dispatch if multiple worker processes run in parallel.
  Manual ACK: message stays in queue until worker confirms success.

  AT-LEAST-ONCE DELIVERY (intentional design decision)
  This worker provides at-least-once delivery, not exactly-once.
  If a MongoDB insert succeeds but the network blips before the ACK
  reaches RabbitMQ, the message is redelivered and inserted again.
  Duplicate click events are therefore possible in MongoDB.
  This is an accepted tradeoff: click analytics is not billing-critical
  data, so slight overcounting on rare network failures is preferable
  to the significant complexity of exactly-once deduplication (which
  would require distributed transaction coordination or idempotency keys).

  NACK + requeue=False on bad JSON: message is *dropped* (discarded).
  There is no dead-letter queue (DLQ) configured in this phase — this
  is a deliberate scope cut. In a production system, you would configure
  a DLQ on the queue declaration so malformed messages are routed there
  for inspection rather than silently discarded. Adding a DLQ is a
  single `x-dead-letter-exchange` argument on `queue_declare` — kept
  out of scope here to avoid infrastructure complexity.

  NACK + requeue=True on DB error: retry when MongoDB recovers; message not lost.

  SCALING PATH (not implemented, but trivial to add)
  Run multiple instances of this worker process against the same queue.
  RabbitMQ distributes messages across all consumers using basic_qos
  round-robin dispatch. No code changes required — just start more
  `python -m worker.consumer` processes or add more replicas in Docker.

  SIGTERM / SIGINT
      → stop_consuming() → close connection → exit 0
      → in-flight message is ACK'd before shutdown

MongoDB indexes created on startup
-----------------------------------
  short_code ASC               → "all clicks for URL X"
  timestamp  ASC               → time-range queries
  (short_code, timestamp) ASC  → "clicks for URL X between T1 and T2"
"""

import json
import logging
import signal
import sys
from datetime import datetime, timezone

import pika
import pika.exceptions
from pymongo import ASCENDING, MongoClient
from pymongo.collection import Collection
from pymongo.errors import PyMongoError

from app.config import settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [worker] %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger(__name__)


# ── MongoDB setup ──────────────────────────────────────────────────────────────

def get_mongo_collection() -> Collection:
    """
    Connect to MongoDB and return the click_events collection.

    Creates indexes on startup — idempotent, safe to call repeatedly.
    MongoDB ignores `create_index` if the index already exists.
    Called once at worker startup, not on every message.
    """
    client = MongoClient(settings.mongodb_uri)
    db     = client.get_default_database()
    col    = db["click_events"]

    # Index for "all clicks for a given short code"
    col.create_index([("short_code", ASCENDING)], background=True)

    # Index for time-range queries across all URLs
    col.create_index([("timestamp", ASCENDING)], background=True)

    # Compound index for the most common analytics query:
    # "how many times was URL X clicked between time T1 and T2?"
    col.create_index(
        [("short_code", ASCENDING), ("timestamp", ASCENDING)],
        background=True,
        name="short_code_timestamp_compound",
    )

    logger.info("MongoDB ready: %s.click_events (indexes ensured)", db.name)
    return col


# ── Message processing ─────────────────────────────────────────────────────────

def process_message(body: bytes, collection: Collection) -> None:
    """
    Parse a click event from RabbitMQ and write it to MongoDB.

    Converts the ISO 8601 timestamp string to a Python datetime so MongoDB
    stores it as a proper BSON Date — enabling native date arithmetic and
    the MongoDB aggregation pipeline for time-series analytics.

    Args:
        body:       Raw JSON bytes from RabbitMQ.
        collection: MongoDB collection to insert into.

    Raises:
        json.JSONDecodeError:  On malformed JSON (caller NACKs without requeue).
        PyMongoError:          On MongoDB failure (caller NACKs with requeue).
    """
    event: dict = json.loads(body)   # raises JSONDecodeError on bad JSON

    # Normalise timestamp: ISO string → Python datetime → BSON Date in MongoDB
    ts_str = event.get("timestamp", "")
    try:
        # Python 3.11+ handles "+00:00" natively; replace "Z" for 3.10 compat
        event["timestamp"] = datetime.fromisoformat(
            ts_str.replace("Z", "+00:00")
        )
    except (ValueError, AttributeError):
        logger.warning("Unparseable timestamp %r — using utcnow()", ts_str)
        event["timestamp"] = datetime.now(timezone.utc)

    collection.insert_one(event)    # raises PyMongoError on failure
    logger.info("Stored click: short_code=%r  url=%r", event.get("short_code"), event.get("long_url"))


def on_message(
    channel:    pika.adapters.blocking_connection.BlockingChannel,
    method:     pika.spec.Basic.Deliver,
    properties: pika.spec.BasicProperties,
    body:       bytes,
    collection: Collection,
) -> None:
    """
    RabbitMQ delivery callback — process message then ACK or NACK.

    Delivery guarantee: AT-LEAST-ONCE.
    If MongoDB insert succeeds but the ACK is lost in transit (network blip),
    RabbitMQ redelivers the message and it is inserted again, producing a
    duplicate click document in MongoDB. This is an intentional tradeoff:
    click analytics is not billing-critical, so rare overcounting is
    preferable to the complexity of exactly-once deduplication.

    ACK  (basic_ack)
        → successful MongoDB insert; message removed from queue permanently.

    NACK requeue=False (basic_nack)
        → bad JSON or schema error; message is DROPPED (discarded).
        → No dead-letter queue (DLQ) is configured in this phase — deliberate
          scope cut. In production, add x-dead-letter-exchange to queue_declare
          so malformed messages are routed to a DLQ for inspection rather than
          silently lost.
        → Requeueing would loop forever on corrupt data.

    NACK requeue=True (basic_nack)
        → MongoDB write error; message returned to queue for retry.
        → Safe to retry once MongoDB recovers.
    """
    try:
        process_message(body, collection)
        channel.basic_ack(delivery_tag=method.delivery_tag)

    except (json.JSONDecodeError, KeyError, ValueError) as exc:
        logger.error("Discarding malformed message: %s | body=%r", exc, body[:200])
        channel.basic_nack(delivery_tag=method.delivery_tag, requeue=False)

    except PyMongoError as exc:
        logger.error("MongoDB error — requeueing: %s", exc)
        channel.basic_nack(delivery_tag=method.delivery_tag, requeue=True)

    except Exception as exc:
        logger.error("Unexpected error — requeueing: %s", exc)
        channel.basic_nack(delivery_tag=method.delivery_tag, requeue=True)


# ── Entry point ────────────────────────────────────────────────────────────────

def main() -> None:
    logger.info("Starting click analytics worker...")

    # Connect to MongoDB once at startup; create indexes
    collection = get_mongo_collection()

    # Connect to RabbitMQ
    params     = pika.URLParameters(settings.rabbitmq_url)
    connection = pika.BlockingConnection(params)
    channel    = connection.channel()

    channel.queue_declare(queue=settings.analytics_queue, durable=True)
    channel.basic_qos(prefetch_count=1)   # one unacked message at a time

    # Graceful shutdown — finish current message before exiting
    def _shutdown(signum, frame):
        logger.info("Signal %s received — shutting down gracefully...", signum)
        channel.stop_consuming()
        connection.close()
        sys.exit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT,  _shutdown)

    channel.basic_consume(
        queue=settings.analytics_queue,
        on_message_callback=lambda ch, method, props, body: on_message(
            ch, method, props, body, collection
        ),
    )

    logger.info(
        "Worker ready — queue=%r  mongodb=%s",
        settings.analytics_queue,
        settings.mongodb_uri,
    )
    channel.start_consuming()


if __name__ == "__main__":
    main()
