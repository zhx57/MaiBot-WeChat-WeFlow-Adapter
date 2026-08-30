"""Cryptographically verify WeChat database-key candidates."""

from __future__ import annotations

import hashlib
import hmac

from .models import KeyMode


PAGE_SIZE = 4096
SALT_SIZE = 16
HMAC_SIZE = 64


def _page_hmac_matches(page_key: bytes, page1: bytes) -> bool:
    hmac_salt = bytes(value ^ 0x3A for value in page1[:SALT_SIZE])
    hmac_key = hashlib.pbkdf2_hmac("sha512", page_key, hmac_salt, 2, 32)
    authenticated = page1[SALT_SIZE:-HMAC_SIZE] + (1).to_bytes(4, "little")
    actual = hmac.new(hmac_key, authenticated, hashlib.sha512).digest()
    return hmac.compare_digest(actual, page1[-HMAC_SIZE:])


def verify_candidate(
    candidate: bytes, page1: bytes, allow_password: bool = True
) -> KeyMode | None:
    """Return the verified WCDB key mode, or ``None`` for no exact match."""
    if not isinstance(candidate, bytes) or len(candidate) != 32:
        return None
    if not isinstance(page1, bytes) or len(page1) != PAGE_SIZE:
        return None

    if _page_hmac_matches(candidate, page1):
        return KeyMode.RAW

    if not allow_password:
        return None
    salt = page1[:SALT_SIZE]
    derived = hashlib.pbkdf2_hmac("sha512", candidate, salt, 256_000, 32)
    if _page_hmac_matches(derived, page1):
        return KeyMode.PASSWORD
    return None
