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
    e = _ENCODING

    # Timestamp: milliseconds since Unix epoch, encode as 10 base-32 chars
    # Unrolled loop for speed (avoids list.append + join overhead)
    t = int(time.time() * 1000)
    t0 = e[t & 31]
    t >>= 5
    t1 = e[t & 31]
    t >>= 5
    t2 = e[t & 31]
    t >>= 5
    t3 = e[t & 31]
    t >>= 5
    t4 = e[t & 31]
    t >>= 5
    t5 = e[t & 31]
    t >>= 5
    t6 = e[t & 31]
    t >>= 5
    t7 = e[t & 31]
    t >>= 5
    t8 = e[t & 31]
    t >>= 5
    t9 = e[t & 31]

    # Random part from buffered random bytes
    if _rand_offset >= len(_rand_buffer):
        _refill_rand_buffer()

    r = int.from_bytes(_rand_buffer[_rand_offset:_rand_offset + 10], "big")
    _rand_offset += 10

    # Unrolled: encode 16 base-32 chars for random part
    r0 = e[r & 31]
    r >>= 5
    r1 = e[r & 31]
    r >>= 5
    r2 = e[r & 31]
    r >>= 5
    r3 = e[r & 31]
    r >>= 5
    r4 = e[r & 31]
    r >>= 5
    r5 = e[r & 31]
    r >>= 5
    r6 = e[r & 31]
    r >>= 5
    r7 = e[r & 31]
    r >>= 5
    r8 = e[r & 31]
    r >>= 5
    r9 = e[r & 31]
    r >>= 5
    r10 = e[r & 31]
    r >>= 5
    r11 = e[r & 31]
    r >>= 5
    r12 = e[r & 31]
    r >>= 5
    r13 = e[r & 31]
    r >>= 5
    r14 = e[r & 31]
    r >>= 5
    r15 = e[r & 31]

    return (t9 + t8 + t7 + t6 + t5 + t4 + t3 + t2 + t1 + t0
            + r15 + r14 + r13 + r12 + r11 + r10 + r9 + r8 + r7 + r6
            + r5 + r4 + r3 + r2 + r1 + r0)
