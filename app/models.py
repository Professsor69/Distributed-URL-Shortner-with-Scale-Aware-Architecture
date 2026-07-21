"""
SQLAlchemy ORM model for the `urls` table.

Schema design notes
-------------------
- `id`          : BIGINT — source of truth for Base62 encoding. Auto-increment
                  guarantees uniqueness without any collision-resolution logic.
- `short_code`  : VARCHAR(10), UNIQUE INDEX — the encoded 6-7 char code.
                  NULL initially (set after we know the ID), then updated.
- `long_url`    : TEXT — original destination URL.
- `url_hash`    : CHAR(64), UNIQUE INDEX — SHA-256 of long_url.
                  TEXT columns cannot be indexed directly in MySQL, so we store
                  a fixed-length hash to enable fast duplicate detection for
                  idempotent POST /shorten calls. This is an O(1) index lookup
                  vs an O(n) full-table scan on long_url. (Interview talking point)
- `created_at`  : server-side default so the DB clock is authoritative.
- `expires_at`  : optional TTL; NULL means "never expires". Added now at zero cost
                  to enable the expiry story in interviews even if unused in Phase 1.
- `click_count` : incremented on every redirect. Kept in MySQL for now; in Phase 5
                  this will move to a Redis counter for atomic increments at scale.
"""

from datetime import datetime
from typing import Optional

from sqlalchemy import BigInteger, DateTime, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class URL(Base):
    __tablename__ = "urls"

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True, comment="Source for Base62 encoding"
    )
    short_code: Mapped[Optional[str]] = mapped_column(
        String(10), unique=True, index=True, nullable=True,
        comment="Base62-encoded ID; NULL until we have the ID from the INSERT"
    )
    long_url: Mapped[str] = mapped_column(
        Text, nullable=False,
        comment="Original destination URL"
    )
    url_hash: Mapped[str] = mapped_column(
        String(64), unique=True, nullable=False, index=True,
        comment="SHA-256 of long_url; indexed for O(1) duplicate detection"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    expires_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, nullable=True, default=None,
        comment="Optional expiry; NULL = never expires"
    )
    click_count: Mapped[int] = mapped_column(
        BigInteger, default=0, nullable=False
    )

    def __repr__(self) -> str:
        return f"<URL id={self.id} short_code={self.short_code!r}>"
