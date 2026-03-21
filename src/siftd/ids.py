"""ULID generation (inline, no dependency).

Shared utility used by storage modules to avoid duplication.
"""

import os
import time

_ENCODING = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_ENCODING_LEN = len(_ENCODING)

# Buffer random bytes in batches to reduce os.urandom syscall overhead.
# 10 bytes per ULID × 1000 = 10KB per batch.
_RAND_BATCH_SIZE = 1000
_rand_buffer = b""
_rand_offset = 0


def _refill_rand_buffer() -> None:
    global _rand_buffer, _rand_offset
    _rand_buffer = os.urandom(10 * _RAND_BATCH_SIZE)
    _rand_offset = 0


def ulid() -> str:
    """Generate a ULID (Universally Unique Lexicographically Sortable Identifier).

    Format: 10 chars timestamp (48 bits, ms precision) + 16 chars randomness (80 bits)
    Total: 26 chars, sortable by creation time, no collisions in practice.
    """
    global _rand_offset
    enc = _ENCODING
    enc_len = _ENCODING_LEN

    # Timestamp: milliseconds since Unix epoch
    timestamp_ms = int(time.time() * 1000)

    # Encode timestamp (10 chars)
    ts_chars = []
    for _ in range(10):
        ts_chars.append(enc[timestamp_ms % enc_len])
        timestamp_ms //= enc_len
    ts_part = "".join(reversed(ts_chars))

    # Random part from buffered random bytes
    if _rand_offset >= len(_rand_buffer):
        _refill_rand_buffer()

    rand_int = int.from_bytes(_rand_buffer[_rand_offset:_rand_offset + 10], "big")
    _rand_offset += 10

    # Encode random (16 chars)
    rand_chars = []
    for _ in range(16):
        rand_chars.append(enc[rand_int % enc_len])
        rand_int //= enc_len
    rand_part = "".join(reversed(rand_chars))

    return ts_part + rand_part
