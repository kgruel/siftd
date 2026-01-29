"""ULID generation (inline, no dependency).

Shared utility used by storage modules to avoid duplication.
"""

import os
import time

_ENCODING = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_ENCODING_LEN = len(_ENCODING)


def ulid() -> str:
    """Generate a ULID (Universally Unique Lexicographically Sortable Identifier).

    Format: 10 chars timestamp (48 bits, ms precision) + 16 chars randomness (80 bits)
    Total: 26 chars, sortable by creation time, no collisions in practice.
    """
    # Timestamp: milliseconds since Unix epoch
    timestamp_ms = int(time.time() * 1000)

    # Encode timestamp (10 chars)
    ts_chars = []
    for _ in range(10):
        ts_chars.append(_ENCODING[timestamp_ms % _ENCODING_LEN])
        timestamp_ms //= _ENCODING_LEN
    ts_part = "".join(reversed(ts_chars))

    # Random part (16 chars, 80 bits)
    rand_bytes = os.urandom(10)
    rand_int = int.from_bytes(rand_bytes, "big")
    rand_chars = []
    for _ in range(16):
        rand_chars.append(_ENCODING[rand_int % _ENCODING_LEN])
        rand_int //= _ENCODING_LEN
    rand_part = "".join(reversed(rand_chars))

    return ts_part + rand_part
