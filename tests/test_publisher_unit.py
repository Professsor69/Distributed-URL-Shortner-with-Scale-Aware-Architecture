"""
Unit tests for app/publisher.py

All tests mock pika — no RabbitMQ instance required.
Run with:
    pytest tests/test_publisher_unit.py -v

Coverage
--------
publish_click_event():
    - Calls basic_publish exactly once per invocation
    - Routing key matches settings.analytics_queue
    - Message body is valid JSON
    - All required event fields present (short_code, long_url, timestamp,
      user_agent, referrer, ip_hash)
    - IP is hashed (SHA-256[:16]), not stored raw
    - Empty IP → empty ip_hash (no error)
    - delivery_mode is Persistent (2) — messages survive broker restart
    - content_type is application/json
    - Fail-open: AMQPError is caught, logged, and NOT re-raised
    - Connection is reset after AMQPError so next call reconnects
"""

import hashlib
import json
from unittest.mock import MagicMock, call, patch

import pika
import pytest

# Patch path: _get_channel is called inside publish_click_event
_PATCH_GET_CHANNEL = "app.publisher._get_channel"
_PATCH_RESET       = "app.publisher._reset_connection"

# ── Helpers ────────────────────────────────────────────────────────────────────

def _published_body(mock_channel: MagicMock) -> dict:
    """Extract and parse the JSON body from the most recent basic_publish call."""
    call_kwargs = mock_channel.basic_publish.call_args.kwargs
    return json.loads(call_kwargs["body"])


def _published_props(mock_channel: MagicMock) -> pika.BasicProperties:
    """Extract the BasicProperties from the most recent basic_publish call."""
    return mock_channel.basic_publish.call_args.kwargs["properties"]


# ── basic_publish is called ────────────────────────────────────────────────────

class TestPublishCalled:

    def test_basic_publish_called_once(self):
        from app.publisher import publish_click_event
        mock_channel = MagicMock()
        with patch(_PATCH_GET_CHANNEL, return_value=mock_channel):
            publish_click_event("000001", "https://example.com")
            mock_channel.basic_publish.assert_called_once()

    def test_routing_key_matches_analytics_queue(self):
        from app.publisher import publish_click_event
        from app.config import settings
        mock_channel = MagicMock()
        with patch(_PATCH_GET_CHANNEL, return_value=mock_channel):
            publish_click_event("000001", "https://example.com")
            routing_key = mock_channel.basic_publish.call_args.kwargs["routing_key"]
            assert routing_key == settings.analytics_queue

    def test_exchange_is_default_empty_string(self):
        from app.publisher import publish_click_event
        mock_channel = MagicMock()
        with patch(_PATCH_GET_CHANNEL, return_value=mock_channel):
            publish_click_event("000001", "https://example.com")
            exchange = mock_channel.basic_publish.call_args.kwargs["exchange"]
            assert exchange == ""


# ── Event schema — required fields ────────────────────────────────────────────

class TestEventSchema:

    _REQUIRED_FIELDS = {"short_code", "long_url", "timestamp", "user_agent", "referrer", "ip_hash"}

    def test_all_required_fields_present(self):
        from app.publisher import publish_click_event
        mock_channel = MagicMock()
        with patch(_PATCH_GET_CHANNEL, return_value=mock_channel):
            publish_click_event("000001", "https://example.com")
            body = _published_body(mock_channel)
            assert self._REQUIRED_FIELDS.issubset(body.keys())

    def test_body_is_valid_json(self):
        from app.publisher import publish_click_event
        mock_channel = MagicMock()
        with patch(_PATCH_GET_CHANNEL, return_value=mock_channel):
            publish_click_event("000001", "https://example.com")
            raw_body = mock_channel.basic_publish.call_args.kwargs["body"]
            # Must not raise
            parsed = json.loads(raw_body)
            assert isinstance(parsed, dict)

    def test_short_code_in_body(self):
        from app.publisher import publish_click_event
        mock_channel = MagicMock()
        with patch(_PATCH_GET_CHANNEL, return_value=mock_channel):
            publish_click_event("abc123", "https://example.com")
            body = _published_body(mock_channel)
            assert body["short_code"] == "abc123"

    def test_long_url_in_body(self):
        from app.publisher import publish_click_event
        mock_channel = MagicMock()
        with patch(_PATCH_GET_CHANNEL, return_value=mock_channel):
            publish_click_event("000001", "https://my-url.example.com/path?q=1")
            body = _published_body(mock_channel)
            assert body["long_url"] == "https://my-url.example.com/path?q=1"

    def test_user_agent_in_body(self):
        from app.publisher import publish_click_event
        mock_channel = MagicMock()
        with patch(_PATCH_GET_CHANNEL, return_value=mock_channel):
            publish_click_event("000001", "https://example.com", user_agent="Mozilla/5.0")
            body = _published_body(mock_channel)
            assert body["user_agent"] == "Mozilla/5.0"

    def test_referrer_in_body(self):
        from app.publisher import publish_click_event
        mock_channel = MagicMock()
        with patch(_PATCH_GET_CHANNEL, return_value=mock_channel):
            publish_click_event("000001", "https://example.com", referrer="https://twitter.com")
            body = _published_body(mock_channel)
            assert body["referrer"] == "https://twitter.com"

    def test_timestamp_is_iso8601_string(self):
        from app.publisher import publish_click_event
        from datetime import datetime
        mock_channel = MagicMock()
        with patch(_PATCH_GET_CHANNEL, return_value=mock_channel):
            publish_click_event("000001", "https://example.com")
            body = _published_body(mock_channel)
            # Must parse as a valid ISO datetime
            dt = datetime.fromisoformat(body["timestamp"].replace("Z", "+00:00"))
            assert isinstance(dt, datetime)


# ── IP hashing ─────────────────────────────────────────────────────────────────

class TestIpHashing:

    def test_ip_is_hashed_not_stored_raw(self):
        from app.publisher import publish_click_event
        ip = "203.0.113.5"
        mock_channel = MagicMock()
        with patch(_PATCH_GET_CHANNEL, return_value=mock_channel):
            publish_click_event("000001", "https://example.com", ip=ip)
            body = _published_body(mock_channel)
            assert body["ip_hash"] != ip, "Raw IP must not be stored"

    def test_ip_hash_is_sha256_prefix_16_chars(self):
        from app.publisher import publish_click_event
        ip = "203.0.113.5"
        expected = hashlib.sha256(ip.encode()).hexdigest()[:16]
        mock_channel = MagicMock()
        with patch(_PATCH_GET_CHANNEL, return_value=mock_channel):
            publish_click_event("000001", "https://example.com", ip=ip)
            body = _published_body(mock_channel)
            assert body["ip_hash"] == expected

    def test_empty_ip_results_in_empty_hash(self):
        from app.publisher import publish_click_event
        mock_channel = MagicMock()
        with patch(_PATCH_GET_CHANNEL, return_value=mock_channel):
            publish_click_event("000001", "https://example.com", ip="")
            body = _published_body(mock_channel)
            assert body["ip_hash"] == ""


# ── Message properties ─────────────────────────────────────────────────────────

class TestMessageProperties:

    def test_delivery_mode_is_persistent(self):
        from app.publisher import publish_click_event
        mock_channel = MagicMock()
        with patch(_PATCH_GET_CHANNEL, return_value=mock_channel):
            publish_click_event("000001", "https://example.com")
            props = _published_props(mock_channel)
            # pika stores delivery_mode as int; DeliveryMode.Persistent == 2
            assert props.delivery_mode == 2

    def test_content_type_is_json(self):
        from app.publisher import publish_click_event
        mock_channel = MagicMock()
        with patch(_PATCH_GET_CHANNEL, return_value=mock_channel):
            publish_click_event("000001", "https://example.com")
            props = _published_props(mock_channel)
            assert props.content_type == "application/json"


# ── Fail-open ──────────────────────────────────────────────────────────────────

class TestFailOpen:

    def test_amqp_error_does_not_raise(self):
        from app.publisher import publish_click_event
        mock_channel = MagicMock()
        mock_channel.basic_publish.side_effect = pika.exceptions.AMQPError("connection lost")
        with patch(_PATCH_GET_CHANNEL, return_value=mock_channel):
            with patch(_PATCH_RESET):
                publish_click_event("000001", "https://example.com")  # Must not raise

    def test_connection_reset_called_after_amqp_error(self):
        from app.publisher import publish_click_event
        mock_channel = MagicMock()
        mock_channel.basic_publish.side_effect = pika.exceptions.AMQPError("timeout")
        with patch(_PATCH_GET_CHANNEL, return_value=mock_channel):
            with patch(_PATCH_RESET) as mock_reset:
                publish_click_event("000001", "https://example.com")
                mock_reset.assert_called_once()

    def test_get_channel_failure_does_not_raise(self):
        from app.publisher import publish_click_event
        with patch(_PATCH_GET_CHANNEL, side_effect=pika.exceptions.AMQPConnectionError("refused")):
            with patch(_PATCH_RESET):
                publish_click_event("000001", "https://example.com")  # Must not raise
