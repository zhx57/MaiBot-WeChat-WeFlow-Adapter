"""Bounded, read-only Windows process-memory access."""

from __future__ import annotations

import ctypes
import sys
import time
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass

from .models import ScanLimits


MEM_COMMIT = 0x1000
PAGE_NOACCESS = 0x01
PAGE_READONLY = 0x02
PAGE_READWRITE = 0x04
PAGE_WRITECOPY = 0x08
PAGE_EXECUTE = 0x10
PAGE_EXECUTE_READ = 0x20
PAGE_EXECUTE_READWRITE = 0x40
PAGE_EXECUTE_WRITECOPY = 0x80
PAGE_GUARD = 0x100
_READABLE = {
    PAGE_READONLY,
    PAGE_READWRITE,
    PAGE_WRITECOPY,
    PAGE_EXECUTE,
    PAGE_EXECUTE_READ,
    PAGE_EXECUTE_READWRITE,
    PAGE_EXECUTE_WRITECOPY,
}
_MAX_USER_ADDRESS = 0x7FFF_FFFF_FFFF


class ReadBudgetExceeded(RuntimeError):
    pass


class MemoryScanTimeout(TimeoutError):
    pass


@dataclass(frozen=True, slots=True)
class MemoryRegion:
    base_address: int
    region_size: int
    state: int
    protect: int


class _MemoryBasicInformation64(ctypes.Structure):
    _fields_ = [
        ("BaseAddress", ctypes.c_uint64),
        ("AllocationBase", ctypes.c_uint64),
        ("AllocationProtect", ctypes.c_uint32),
        ("__alignment1", ctypes.c_uint32),
        ("RegionSize", ctypes.c_uint64),
        ("State", ctypes.c_uint32),
        ("Protect", ctypes.c_uint32),
        ("Type", ctypes.c_uint32),
        ("__alignment2", ctypes.c_uint32),
    ]


def _kernel32():
    if sys.platform != "win32":
        raise OSError("Windows process memory is unavailable")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.VirtualQueryEx.argtypes = [
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.POINTER(_MemoryBasicInformation64),
        ctypes.c_size_t,
    ]
    kernel.VirtualQueryEx.restype = ctypes.c_size_t
    kernel.ReadProcessMemory.argtypes = [
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_size_t,
        ctypes.POINTER(ctypes.c_size_t),
    ]
    kernel.ReadProcessMemory.restype = ctypes.c_int
    return kernel


def region_is_readable(region: MemoryRegion, limits: ScanLimits) -> bool:
    base_protection = region.protect & 0xFF
    return (
        region.state == MEM_COMMIT
        and 0 < region.region_size <= limits.max_region_bytes
        and not region.protect & PAGE_GUARD
        and base_protection != PAGE_NOACCESS
        and base_protection in _READABLE
    )


def iter_readable_regions(
    handle: int,
    limits: ScanLimits,
    *,
    cancel: Callable[[], bool] | None = None,
) -> Iterator[MemoryRegion]:
    kernel = _kernel32()
    address = 0
    deadline = time.monotonic() + limits.timeout_seconds
    while address < _MAX_USER_ADDRESS:
        if cancel and cancel():
            raise InterruptedError("memory scan cancelled")
        if time.monotonic() > deadline:
            raise MemoryScanTimeout("memory scan timed out")
        mbi = _MemoryBasicInformation64()
        result = kernel.VirtualQueryEx(
            handle, ctypes.c_void_p(address), ctypes.byref(mbi), ctypes.sizeof(mbi)
        )
        if not result:
            break
        region = MemoryRegion(
            base_address=int(mbi.BaseAddress),
            region_size=int(mbi.RegionSize),
            state=int(mbi.State),
            protect=int(mbi.Protect),
        )
        if region_is_readable(region, limits):
            yield region
        next_address = region.base_address + region.region_size
        if next_address <= address:
            break
        address = next_address


def read_process_memory(handle: int, address: int, size: int) -> bytes | None:
    if size <= 0:
        return b""
    kernel = _kernel32()
    buffer = ctypes.create_string_buffer(size)
    count = ctypes.c_size_t()
    success = kernel.ReadProcessMemory(
        handle,
        ctypes.c_void_p(address),
        buffer,
        size,
        ctypes.byref(count),
    )
    if not success and count.value == 0:
        return None
    return bytes(buffer.raw[: count.value])


def iter_memory_chunks(
    handle: int,
    regions: Iterable[MemoryRegion],
    limits: ScanLimits,
    *,
    read: Callable[[int, int, int], bytes | None] = read_process_memory,
    cancel: Callable[[], bool] | None = None,
) -> Iterator[tuple[int, bytes]]:
    deadline = time.monotonic() + limits.timeout_seconds
    total_requested = 0
    for region in regions:
        if not region_is_readable(region, limits):
            continue
        offset = 0
        while offset < region.region_size:
            if cancel and cancel():
                raise InterruptedError("memory scan cancelled")
            if time.monotonic() > deadline:
                raise MemoryScanTimeout("memory scan timed out")
            requested = min(limits.chunk_size, region.region_size - offset)
            if total_requested + requested > limits.max_total_read_bytes:
                raise ReadBudgetExceeded("total process-memory read budget exceeded")
            address = region.base_address + offset
            data = read(handle, address, requested)
            total_requested += requested
            offset += requested
            if data:
                yield address, data
