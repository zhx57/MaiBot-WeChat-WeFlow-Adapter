"""Extract known WeChat key literals from adjacent memory chunks."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterator

from .models import ScanLimits


_WECHAT_KEY_LITERAL = re.compile(rb"x'([0-9a-fA-F]{64,192})'")


class CandidateBudgetExceeded(RuntimeError):
    """Raised before more unique candidates than configured are emitted."""


class CandidateScanner:
    def __init__(self, limits: ScanLimits) -> None:
        if limits.overlap < 194:
            raise ValueError("overlap must be at least 194 bytes")
        self._limits = limits
        self._tail = b""
        self._next_address: int | None = None
        self._seen: set[bytes] = set()
        self._count = 0

    @property
    def candidate_count(self) -> int:
        return self._count

    def feed(self, chunk: bytes, base_address: int) -> Iterator[bytes]:
        if not isinstance(chunk, bytes):
            raise TypeError("chunk must be bytes")
        if base_address < 0:
            raise ValueError("base_address must be non-negative")

        prefix = self._tail if self._next_address == base_address else b""
        combined = prefix + chunk
        self._tail = combined[-self._limits.overlap :]
        self._next_address = base_address + len(chunk)

        for match in _WECHAT_KEY_LITERAL.finditer(combined):
            candidate = bytes.fromhex(match.group(1)[:64].decode("ascii"))
            digest = hashlib.sha256(candidate).digest()
            if digest in self._seen:
                continue
            if self._count >= self._limits.max_raw_candidates:
                raise CandidateBudgetExceeded("candidate budget exceeded")
            self._seen.add(digest)
            self._count += 1
            yield candidate
