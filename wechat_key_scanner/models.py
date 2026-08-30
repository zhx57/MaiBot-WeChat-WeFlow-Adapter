"""Public value objects for the passive scanner."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path


class KeyMode(str, Enum):
    RAW = "raw"
    PASSWORD = "password"


class ScanFailureCode(str, Enum):
    UNSUPPORTED_PLATFORM = "unsupported_platform"
    PROCESS_NOT_FOUND = "process_not_found"
    DATABASE_NOT_FOUND = "database_not_found"
    UNTRUSTED_PROCESS = "untrusted_process"
    PERMISSION_DENIED = "permission_denied"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    READ_BUDGET_EXCEEDED = "read_budget_exceeded"
    PASSIVE_UNSUPPORTED = "passive_unsupported"


@dataclass(frozen=True, slots=True)
class ScanLimits:
    timeout_seconds: float = 30.0
    chunk_size: int = 1024 * 1024
    overlap: int = 194
    max_region_bytes: int = 256 * 1024 * 1024
    max_total_read_bytes: int = 2 * 1024 * 1024 * 1024
    max_raw_candidates: int = 50_000
    max_pbkdf2_candidates: int = 256

    def __post_init__(self) -> None:
        positive = (
            "timeout_seconds",
            "chunk_size",
            "max_region_bytes",
            "max_total_read_bytes",
            "max_raw_candidates",
            "max_pbkdf2_candidates",
        )
        for name in positive:
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive")
        if self.overlap < 0 or self.overlap >= self.chunk_size:
            raise ValueError("overlap must be non-negative and smaller than chunk_size")


@dataclass(frozen=True, slots=True)
class ProcessIdentity:
    pid: int
    executable: Path
    version: str | None = None


@dataclass(frozen=True, slots=True)
class DatabaseIdentity:
    path: Path
    account_id: str


@dataclass(frozen=True, slots=True)
class VerifiedKey:
    key: bytes
    mode: KeyMode
    process: ProcessIdentity
    database: DatabaseIdentity

    def __repr__(self) -> str:
        return (
            "VerifiedKey(key=<redacted>, "
            f"mode={self.mode!r}, process={self.process!r}, database={self.database!r})"
        )


@dataclass(frozen=True, slots=True)
class ScanFailure:
    code: ScanFailureCode
    message: str
