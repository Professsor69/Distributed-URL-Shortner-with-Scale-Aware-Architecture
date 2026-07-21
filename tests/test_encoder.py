"""
Unit tests for app/encoder.py

These tests cover the Base62 encoder in complete isolation — no DB, no FastAPI,
no HTTP calls. This is the primary selling point: the encoder is pure Python and
fully testable as a standalone module.

Run with:
    pytest tests/test_encoder.py -v
"""

import pytest

from app.encoder import ALPHABET, BASE, MIN_LENGTH, decode, encode


class TestEncode:
    """Tests for the encode() function."""

    def test_encodes_zero_to_all_zeros(self):
        """encode(0) should return a string of MIN_LENGTH '0' characters."""
        result = encode(0)
        assert result == "0" * MIN_LENGTH

    def test_encodes_one(self):
        assert encode(1) == "000001"

    def test_encodes_last_single_digit(self):
        """62nd character of alphabet is index 61 — the last single-digit Base62 value."""
        # ALPHABET[61] should be 'z' (0-9=10, A-Z=26, a-z=26 → index 61 = 'z')
        assert encode(61) == f"00000{ALPHABET[61]}"

    def test_encodes_base_rollover(self):
        """encode(62) is '10' in Base62 — same as how 10 in decimal means 'one 10 + zero ones'."""
        assert encode(62) == "000010"

    def test_encodes_63(self):
        assert encode(63) == "000011"

    def test_minimum_output_length(self):
        """All outputs must be at least MIN_LENGTH characters long."""
        for num in [0, 1, 61, 62, 100, 1000, 62**3, 62**5]:
            result = encode(num)
            assert len(result) >= MIN_LENGTH, (
                f"encode({num}) returned {result!r} which is shorter than {MIN_LENGTH}"
            )

    def test_large_number_exceeds_min_length(self):
        """Numbers ≥ 62^6 should produce strings longer than MIN_LENGTH."""
        big = 62**6  # First number that needs 7 Base62 digits
        result = encode(big)
        assert len(result) == 7

    def test_uniqueness_for_sequential_ids(self):
        """No two different IDs should produce the same short code."""
        codes = [encode(i) for i in range(1000)]
        assert len(set(codes)) == 1000, "Duplicate short codes detected"

    def test_raises_on_negative(self):
        with pytest.raises(ValueError, match="negative"):
            encode(-1)

    def test_raises_on_large_negative(self):
        with pytest.raises(ValueError):
            encode(-999999)


class TestDecode:
    """Tests for the decode() function."""

    def test_decodes_zero(self):
        assert decode("000000") == 0

    def test_decodes_one(self):
        assert decode("000001") == 1

    def test_decodes_base_rollover(self):
        assert decode("000010") == 62

    def test_raises_on_invalid_char(self):
        with pytest.raises(ValueError, match="Invalid character"):
            decode("abc!!!")

    def test_raises_on_space(self):
        with pytest.raises(ValueError, match="Invalid character"):
            decode("abc def")


class TestRoundTrip:
    """encode → decode should be a perfect identity for all valid inputs."""

    @pytest.mark.parametrize("num", [
        0, 1, 61, 62, 63, 100, 999,
        62**2, 62**3, 62**4, 62**5,
        62**6,           # first 7-digit Base62 number
        999_999_999,     # ~1 billion
        56_800_235_583,  # 62^6 - 1 (last 6-digit Base62 number)
    ])
    def test_round_trip(self, num: int):
        assert decode(encode(num)) == num, (
            f"Round-trip failed for {num}: encode gave {encode(num)!r}, "
            f"decode gave {decode(encode(num))}"
        )

    def test_round_trip_sequential_batch(self):
        """Spot-check 10,000 sequential IDs."""
        for i in range(10_000):
            assert decode(encode(i)) == i
