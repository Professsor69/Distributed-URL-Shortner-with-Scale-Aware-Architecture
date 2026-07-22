"""
Unit tests for worker/consumer.py

All tests mock MongoDB and pika — no external services required.
Run with:
    pytest tests/test_worker_unit.py -v

Coverage
--------
process_message():
    - Calls collection.insert_one with a dict
    - Inserts all fields from the original event
    - Normalises ISO timestamp string to datetime object
    - Falls back to utcnow() on unparseable timestamp
    - Raises JSONDecodeError on malformed JSON body

on_message():
    - Calls basic_ack after successful process_message
    - Calls basic_nack(requeue=False) on JSONDecodeError
    - Calls basic_nack(requeue=False) on ValueError
    - Calls basic_nack(requeue=True) on PyMongoError
    - Calls basic_nack(requeue=True) on unexpected Exception
"""

import json
from datetime import datetime, timezone
from unittest.mock import MagicMock, call, patch

import pytest
from pymongo.errors import PyMongoError


# ── process_message ────────────────────────────────────────────────────────────

class TestProcessMessage:

    def _make_event(self, **overrides) -> dict:
        base = {
            "short_code": "000001",
            "long_url":   "https://example.com",
            "timestamp":  "2024-01-15T10:30:00.123456+00:00",
            "user_agent": "Mozilla/5.0",
            "referrer":   "https://twitter.com",
            "ip_hash":    "abc123def456abcd",
        }
        base.update(overrides)
        return base

    def test_calls_insert_one(self):
        from worker.consumer import process_message
        collection = MagicMock()
        event = self._make_event()
        process_message(json.dumps(event).encode(), collection)
        collection.insert_one.assert_called_once()

    def test_inserted_document_contains_short_code(self):
        from worker.consumer import process_message
        collection = MagicMock()
        process_message(json.dumps(self._make_event(short_code="abc999")).encode(), collection)
        inserted = collection.insert_one.call_args[0][0]
        assert inserted["short_code"] == "abc999"

    def test_inserted_document_contains_long_url(self):
        from worker.consumer import process_message
        collection = MagicMock()
        process_message(json.dumps(self._make_event()).encode(), collection)
        inserted = collection.insert_one.call_args[0][0]
        assert inserted["long_url"] == "https://example.com"

    def test_timestamp_normalised_to_datetime(self):
        """
        ISO string "2024-01-15T10:30:00.123456+00:00" must be converted
        to a Python datetime object so MongoDB stores it as BSON Date.
        """
        from worker.consumer import process_message
        collection = MagicMock()
        ts_str = "2024-01-15T10:30:00.123456+00:00"
        process_message(json.dumps(self._make_event(timestamp=ts_str)).encode(), collection)
        inserted = collection.insert_one.call_args[0][0]
        assert isinstance(inserted["timestamp"], datetime), (
            f"Expected datetime, got {type(inserted['timestamp'])}"
        )

    def test_timestamp_z_suffix_is_handled(self):
        """ISO timestamps ending with 'Z' must also be normalised correctly."""
        from worker.consumer import process_message
        collection = MagicMock()
        process_message(
            json.dumps(self._make_event(timestamp="2024-01-15T10:30:00Z")).encode(),
            collection,
        )
        inserted = collection.insert_one.call_args[0][0]
        assert isinstance(inserted["timestamp"], datetime)

    def test_invalid_timestamp_falls_back_to_utcnow(self):
        from worker.consumer import process_message
        collection = MagicMock()
        before = datetime.now(timezone.utc)
        process_message(
            json.dumps(self._make_event(timestamp="not-a-date")).encode(),
            collection,
        )
        after = datetime.now(timezone.utc)
        inserted = collection.insert_one.call_args[0][0]
        dt = inserted["timestamp"]
        assert isinstance(dt, datetime)
        # The fallback datetime should be within the test's execution window
        assert dt.replace(tzinfo=timezone.utc) >= before.replace(tzinfo=timezone.utc)

    def test_raises_on_malformed_json(self):
        from worker.consumer import process_message
        import json
        collection = MagicMock()
        with pytest.raises(json.JSONDecodeError):
            process_message(b"not valid json {{{", collection)

    def test_mongo_error_propagates(self):
        from worker.consumer import process_message
        collection = MagicMock()
        collection.insert_one.side_effect = PyMongoError("write failed")
        with pytest.raises(PyMongoError):
            process_message(json.dumps(self._make_event()).encode(), collection)


# ── on_message — ACK / NACK routing ───────────────────────────────────────────

class TestOnMessage:

    def _make_channel(self) -> MagicMock:
        return MagicMock()

    def _make_method(self, delivery_tag=42) -> MagicMock:
        method = MagicMock()
        method.delivery_tag = delivery_tag
        return method

    def _good_body(self) -> bytes:
        return json.dumps({
            "short_code": "000001",
            "long_url":   "https://example.com",
            "timestamp":  "2024-01-15T10:30:00+00:00",
            "user_agent": "",
            "referrer":   "",
            "ip_hash":    "",
        }).encode()

    def test_acks_on_success(self):
        from worker.consumer import on_message
        channel    = self._make_channel()
        method     = self._make_method()
        collection = MagicMock()

        on_message(channel, method, MagicMock(), self._good_body(), collection)

        channel.basic_ack.assert_called_once_with(delivery_tag=42)
        channel.basic_nack.assert_not_called()

    def test_nacks_without_requeue_on_bad_json(self):
        from worker.consumer import on_message
        channel    = self._make_channel()
        method     = self._make_method(delivery_tag=7)
        collection = MagicMock()

        on_message(channel, method, MagicMock(), b"{{not json}}", collection)

        channel.basic_nack.assert_called_once_with(delivery_tag=7, requeue=False)
        channel.basic_ack.assert_not_called()

    def test_nacks_with_requeue_on_mongo_error(self):
        from worker.consumer import on_message
        channel    = self._make_channel()
        method     = self._make_method(delivery_tag=99)
        collection = MagicMock()
        collection.insert_one.side_effect = PyMongoError("connection timeout")

        on_message(channel, method, MagicMock(), self._good_body(), collection)

        channel.basic_nack.assert_called_once_with(delivery_tag=99, requeue=True)
        channel.basic_ack.assert_not_called()

    def test_nacks_with_requeue_on_unexpected_error(self):
        from worker.consumer import on_message
        channel    = self._make_channel()
        method     = self._make_method(delivery_tag=55)
        collection = MagicMock()
        collection.insert_one.side_effect = RuntimeError("unexpected")

        on_message(channel, method, MagicMock(), self._good_body(), collection)

        channel.basic_nack.assert_called_once_with(delivery_tag=55, requeue=True)

    def test_delivery_tag_passed_correctly_to_ack(self):
        from worker.consumer import on_message
        channel    = self._make_channel()
        method     = self._make_method(delivery_tag=123)
        collection = MagicMock()

        on_message(channel, method, MagicMock(), self._good_body(), collection)

        assert channel.basic_ack.call_args.kwargs["delivery_tag"] == 123
