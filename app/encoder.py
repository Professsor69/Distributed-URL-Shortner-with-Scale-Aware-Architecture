"""
Base62 Encoder / Decoder for URL short codes.

Design rationale
----------------
We encode the auto-increment database ID, NOT a hash or random string.

Why this matters (interview talking point):
  - Auto-increment IDs are monotonically increasing → guaranteed uniqueness.
  - No collision-resolution strategy needed.
  - Encoding is deterministic: same ID always yields the same short code.
  - Hash-based approaches (e.g. MD5 of the URL) require handling collisions and
    are therefore more complex without being meaningfully better for this use-case.

Alphabet
--------
  0-9, A-Z, a-z  →  62 characters, fully URL-safe, no special chars.

Capacity
--------
  6-char codes cover 62^6 ≈ 56.8 billion unique URLs — more than enough.
"""

ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
BASE = len(ALPHABET)  # 62
MIN_LENGTH = 6        # minimum output width; shorter results are left-padded with '0'

# Pre-computed reverse lookup for O(1) decode
_CHAR_TO_INDEX: dict[str, int] = {char: idx for idx, char in enumerate(ALPHABET)}


def encode(num: int) -> str:
    """
    Encode a non-negative integer to a Base62 string, zero-padded to MIN_LENGTH.

    Args:
        num: A non-negative integer (typically a database primary key).

    Returns:
        A Base62 string of length >= MIN_LENGTH.

    Raises:
        ValueError: If num is negative.

    Examples:
        >>> encode(0)
        '000000'
        >>> encode(1)
        '000001'
        >>> encode(61)
        '00000z'
        >>> encode(62)
        '000010'
    """
    if num < 0:
        raise ValueError(f"Cannot encode a negative integer: {num}")

    if num == 0:
        return ALPHABET[0] * MIN_LENGTH

    chars: list[str] = []
    while num:
        chars.append(ALPHABET[num % BASE])
        num //= BASE

    # chars is built LSB-first; reverse for correct order, then left-pad
    encoded = "".join(reversed(chars))
    return encoded.rjust(MIN_LENGTH, ALPHABET[0])


def decode(short_code: str) -> int:
    """
    Decode a Base62 string back to its original integer.

    Args:
        short_code: A string containing only characters from ALPHABET.

    Returns:
        The original integer.

    Raises:
        ValueError: If the string contains characters not in ALPHABET.

    Examples:
        >>> decode('000001')
        1
        >>> decode('000010')
        62
    """
    result = 0
    for char in short_code:
        if char not in _CHAR_TO_INDEX:
            raise ValueError(
                f"Invalid character '{char}' in short code '{short_code}'. "
                f"Allowed characters: {ALPHABET!r}"
            )
        result = result * BASE + _CHAR_TO_INDEX[char]
    return result
