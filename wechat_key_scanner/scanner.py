"""End-to-end passive scanner orchestration."""

from __future__ import annotations

import sys
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

from .candidate_scanner import CandidateBudgetExceeded, CandidateScanner
from .database_locator import discover_databases, read_stable_page1
from .key_verifier import verify_candidate
from .memory_reader import (
    MemoryRegion,
    MemoryScanTimeout,
    ReadBudgetExceeded,
    iter_memory_chunks,
    iter_readable_regions,
)
from .models import (
    DatabaseIdentity,
    KeyMode,
    ProcessIdentity,
    ScanFailure,
    ScanFailureCode,
    ScanLimits,
    VerifiedKey,
)
from .windows_process import (
    ProcessAccessDenied,
    close_process,
    discover_wechat_processes,
    open_process_readonly,
)


@dataclass(frozen=True)
class ScanDependencies:
    discover_databases: Callable[[str | Path | None], list[DatabaseIdentity]]
    read_page1: Callable[[DatabaseIdentity], bytes]
    discover_processes: Callable[[], list[ProcessIdentity]]
    open_process: Callable[[int], int]
    close_process: Callable[[int], None]
    iter_regions: Callable[[int, ScanLimits, Callable[[], bool] | None], Iterable[MemoryRegion]]
    iter_chunks: Callable[
        [int, Iterable[MemoryRegion], ScanLimits, Callable[[], bool] | None],
        Iterable[tuple[int, bytes]],
    ]
    verify: Callable[[bytes, bytes, bool], KeyMode | None]


def _default_dependencies() -> ScanDependencies:
    return ScanDependencies(
        discover_databases=discover_databases,
        read_page1=read_stable_page1,
        discover_processes=discover_wechat_processes,
        open_process=open_process_readonly,
        close_process=close_process,
        iter_regions=lambda handle, limits, cancel: iter_readable_regions(
            handle, limits, cancel=cancel
        ),
        iter_chunks=lambda handle, regions, limits, cancel: iter_memory_chunks(
            handle, regions, limits, cancel=cancel
        ),
        verify=lambda candidate, page, allow_password: verify_candidate(
            candidate, page, allow_password=allow_password
        ),
    )


def scan_wechat_key(
    *,
    data_root: str | Path | None = None,
    limits: ScanLimits | None = None,
    cancel: Callable[[], bool] | None = None,
    dependencies: ScanDependencies | None = None,
    platform: str | None = None,
) -> VerifiedKey | ScanFailure:
    host = sys.platform if platform is None else platform
    if host != "win32":
        return ScanFailure(
            ScanFailureCode.UNSUPPORTED_PLATFORM, "passive WeChat scanning requires Windows"
        )
    limits = limits or ScanLimits()
    deps = dependencies or _default_dependencies()

    databases = deps.discover_databases(data_root)
    if not databases:
        return ScanFailure(ScanFailureCode.DATABASE_NOT_FOUND, "message_0.db was not found")
    pages: list[tuple[DatabaseIdentity, bytes]] = []
    for database in databases:
        try:
            pages.append((database, deps.read_page1(database)))
        except OSError:
            continue
    if not pages:
        return ScanFailure(
            ScanFailureCode.DATABASE_NOT_FOUND,
            "no stable 4096-byte message database page was available",
        )

    processes = deps.discover_processes()
    if not processes:
        return ScanFailure(
            ScanFailureCode.PROCESS_NOT_FOUND, "no trusted running WeChat process was found"
        )

    password_attempts = 0
    try:
        for process in processes:
            try:
                handle = deps.open_process(process.pid)
            except ProcessAccessDenied:
                continue
            try:
                candidates = CandidateScanner(limits)
                regions = deps.iter_regions(handle, limits, cancel)
                for address, chunk in deps.iter_chunks(handle, regions, limits, cancel):
                    for candidate in candidates.feed(chunk, address):
                        for database, page in pages:
                            allow_password = password_attempts < limits.max_pbkdf2_candidates
                            mode = deps.verify(candidate, page, allow_password)
                            if allow_password:
                                password_attempts += 1
                            if mode is not None:
                                return VerifiedKey(candidate, mode, process, database)
            finally:
                deps.close_process(handle)
    except InterruptedError:
        return ScanFailure(ScanFailureCode.CANCELLED, "passive scan was cancelled")
    except MemoryScanTimeout:
        return ScanFailure(ScanFailureCode.TIMEOUT, "passive scan timed out")
    except (ReadBudgetExceeded, CandidateBudgetExceeded):
        return ScanFailure(
            ScanFailureCode.READ_BUDGET_EXCEEDED, "passive scan reached its safety budget"
        )
    return ScanFailure(
        ScanFailureCode.PASSIVE_UNSUPPORTED,
        "no database-verified passive key candidate was found",
    )
