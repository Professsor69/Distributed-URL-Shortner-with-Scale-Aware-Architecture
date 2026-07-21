"""
Pydantic schemas for request validation and response serialisation.

Pydantic v2 is used throughout (FastAPI 0.111+ requires it).
AnyHttpUrl enforces that URLs have a valid scheme (http/https) and host.
"""

from datetime import datetime
from typing import Optional

from pydantic import AnyHttpUrl, BaseModel, ConfigDict, field_validator


class ShortenRequest(BaseModel):
    """Request body for POST /shorten."""

    url: AnyHttpUrl
    expires_at: Optional[datetime] = None

    @field_validator("url", mode="before")
    @classmethod
    def strip_whitespace(cls, v: str) -> str:
        """Strip accidental leading/trailing whitespace from pasted URLs."""
        if isinstance(v, str):
            return v.strip()
        return v


class ShortenResponse(BaseModel):
    """Response body for POST /shorten."""

    model_config = ConfigDict(from_attributes=True)

    short_code: str
    short_url: str
    long_url: str
    created: bool  # True = new record; False = returned existing (idempotent)


class URLStatsResponse(BaseModel):
    """Response body for GET /stats/{short_code}."""

    model_config = ConfigDict(from_attributes=True)

    short_code: str
    long_url: str
    click_count: int
    created_at: datetime
    expires_at: Optional[datetime]
