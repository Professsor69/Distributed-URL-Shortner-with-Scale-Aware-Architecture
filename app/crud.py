"""
CRUD (Create, Read, Update) operations against the `urls` table.

All functions receive a SQLAlchemy Session as a dependency — they contain
no FastAPI-specific code, making them independently testable.

Idempotency design
------------------
POST /shorten with the same URL must return the same short code.
We detect duplicates by looking up `url_hash` (SHA-256 of long_url), which is
indexed as a UNIQUE column. This is an O(1) index scan regardless of table size,
avoiding a full TEXT column scan on long_url.

Two-step INSERT for Base62 encoding
------------------------------------
We can't compute the short code before the INSERT because the short code IS
derived from the auto-increment ID. The flow is:
  1. INSERT row (short_code = NULL, url_hash set)
  2. db.flush() → DB assigns the ID, ORM object is populated
  3. short_code = encode(id)
  4. UPDATE the same row
  5. db.commit()

This is a single transaction (flush ≠ commit) so there is no observable
intermediate state with short_code = NULL.
"""

import hashlib
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.encoder import encode
from app.models import URL


def _hash_url(long_url: str) -> str:
    """Return the SHA-256 hex digest of a URL string."""
    return hashlib.sha256(long_url.encode("utf-8")).hexdigest()


def get_url_by_hash(db: Session, long_url: str) -> Optional[URL]:
    """Look up an existing URL record by the SHA-256 hash of the original URL."""
    url_hash = _hash_url(long_url)
    return db.execute(
        select(URL).where(URL.url_hash == url_hash)
    ).scalar_one_or_none()


def get_url_by_short_code(db: Session, short_code: str) -> Optional[URL]:
    """Look up a URL record by its short code. Uses the indexed short_code column."""
    return db.execute(
        select(URL).where(URL.short_code == short_code)
    ).scalar_one_or_none()


def create_short_url(
    db: Session,
    long_url: str,
    expires_at: Optional[datetime] = None,
) -> tuple[URL, bool]:
    """
    Create a new short URL, or return the existing one (idempotent).

    Returns:
        (URL object, created: bool)
        created=True  → new record was inserted
        created=False → existing record returned (same long_url seen before)
    """
    # --- Idempotency check ---
    existing = get_url_by_hash(db, long_url)
    if existing:
        return existing, False

    url_hash = _hash_url(long_url)

    # --- Step 1: INSERT to obtain the auto-increment ID ---
    url_obj = URL(long_url=long_url, url_hash=url_hash, expires_at=expires_at)
    db.add(url_obj)
    db.flush()  # Sends INSERT to DB; populates url_obj.id (no commit yet)

    # --- Step 2: Derive short_code from the now-known ID ---
    url_obj.short_code = encode(url_obj.id)

    # --- Step 3: Commit both the INSERT and the short_code UPDATE atomically ---
    db.commit()
    db.refresh(url_obj)
    return url_obj, True


def increment_click_count(db: Session, url_id: int) -> None:
    """
    Atomically increment click_count for a given URL record.

    Uses a SQL-level UPDATE (not a read-modify-write) to avoid race conditions
    under concurrent requests.

    Note: In Phase 4, this will be replaced by an async RabbitMQ event so the
    write no longer blocks the redirect response path.
    """
    db.execute(
        update(URL)
        .where(URL.id == url_id)
        .values(click_count=URL.click_count + 1)
    )
    db.commit()
