"""ULID (Universally Unique Lexicographically Sortable Identifier) implementation.

Conforms to standard ULID specification:
- 48-bit Unix timestamp in milliseconds
- 80-bit cryptographically secure randomness
- 26-character Crockford's Base32 encoding
"""

import os
import time
from typing import Final

CROCKFORD_BASE32: Final[str] = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def generate_ulid(timestamp_ms: int | None = None) -> str:
    """Generate a standard 26-character Crockford Base32 ULID."""
    if timestamp_ms is None:
        timestamp_ms = int(time.time() * 1000)

    # 48-bit timestamp (6 bytes)
    time_bytes = timestamp_ms.to_bytes(6, byteorder="big")
    # 80-bit randomness (10 bytes)
    random_bytes = os.urandom(10)
    full_bytes = time_bytes + random_bytes

    # Convert 16 bytes (128 bits) into 26 base32 characters
    val = int.from_bytes(full_bytes, byteorder="big")
    chars = []
    for _ in range(26):
        chars.append(CROCKFORD_BASE32[val & 0x1F])
        val >>= 5
    return "".join(reversed(chars))


def is_valid_ulid(ulid_str: str) -> bool:
    """Validate whether a string is a 26-character Crockford Base32 ULID."""
    if len(ulid_str) != 26:
        return False
    return all(c in CROCKFORD_BASE32 for c in ulid_str.upper())
