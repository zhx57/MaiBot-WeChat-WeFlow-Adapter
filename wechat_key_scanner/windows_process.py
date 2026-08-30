"""Discover and open trusted Windows WeChat processes read-only."""

from __future__ import annotations

import ctypes
import os
import stat
import subprocess
import sys
from collections.abc import Callable, Iterable
from pathlib import Path

from .models import ProcessIdentity


PROCESS_VM_READ = 0x0010
PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
TH32CS_SNAPPROCESS = 0x00000002
_WECHAT_NAMES = {"wechat.exe", "weixin.exe"}


class ProcessAccessDenied(PermissionError):
    pass


class _ProcessEntry32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", ctypes.c_uint32),
        ("cntUsage", ctypes.c_uint32),
        ("th32ProcessID", ctypes.c_uint32),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", ctypes.c_uint32),
        ("cntThreads", ctypes.c_uint32),
        ("th32ParentProcessID", ctypes.c_uint32),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", ctypes.c_uint32),
        ("szExeFile", ctypes.c_wchar * 260),
    ]


def _kernel32():
    if sys.platform != "win32":
        raise OSError("Windows process discovery is unavailable")
    return ctypes.WinDLL("kernel32", use_last_error=True)


def _enumerate_processes() -> list[tuple[int, Path]]:
    kernel = _kernel32()
    kernel.CreateToolhelp32Snapshot.argtypes = [ctypes.c_uint32, ctypes.c_uint32]
    kernel.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
    kernel.Process32FirstW.argtypes = [ctypes.c_void_p, ctypes.POINTER(_ProcessEntry32W)]
    kernel.Process32FirstW.restype = ctypes.c_int
    kernel.Process32NextW.argtypes = [ctypes.c_void_p, ctypes.POINTER(_ProcessEntry32W)]
    kernel.Process32NextW.restype = ctypes.c_int
    kernel.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    kernel.OpenProcess.restype = ctypes.c_void_p
    kernel.QueryFullProcessImageNameW.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_wchar_p,
        ctypes.POINTER(ctypes.c_uint32),
    ]
    kernel.QueryFullProcessImageNameW.restype = ctypes.c_int
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel.CloseHandle.restype = ctypes.c_int

    invalid = ctypes.c_void_p(-1).value
    snapshot = kernel.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snapshot or snapshot == invalid:
        return []
    rows: list[tuple[int, Path]] = []
    try:
        entry = _ProcessEntry32W()
        entry.dwSize = ctypes.sizeof(entry)
        more = bool(kernel.Process32FirstW(snapshot, ctypes.byref(entry)))
        while more:
            if entry.szExeFile.lower() in _WECHAT_NAMES:
                handle = kernel.OpenProcess(
                    PROCESS_QUERY_LIMITED_INFORMATION, False, entry.th32ProcessID
                )
                if handle:
                    try:
                        buffer = ctypes.create_unicode_buffer(32768)
                        size = ctypes.c_uint32(len(buffer))
                        if kernel.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
                            rows.append((int(entry.th32ProcessID), Path(buffer.value)))
                    finally:
                        kernel.CloseHandle(handle)
            more = bool(kernel.Process32NextW(snapshot, ctypes.byref(entry)))
    finally:
        kernel.CloseHandle(snapshot)
    return rows


def _trusted_powershell() -> Path:
    kernel = _kernel32()
    kernel.GetSystemDirectoryW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32]
    kernel.GetSystemDirectoryW.restype = ctypes.c_uint32
    buffer = ctypes.create_unicode_buffer(32768)
    length = kernel.GetSystemDirectoryW(buffer, len(buffer))
    if not length or length >= len(buffer):
        raise OSError("cannot resolve the Windows system directory")
    path = Path(buffer.value) / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    if not path.is_file() or path.is_symlink():
        raise OSError("trusted Windows PowerShell is unavailable")
    return path


def verify_tencent_signature(path: Path) -> bool:
    try:
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode) or path.is_symlink():
            return False
        literal = str(path.resolve(strict=True)).replace("'", "''")
        script = (
            f"$s=Get-AuthenticodeSignature -LiteralPath '{literal}';"
            "if($s.Status -eq 'Valid' -and $s.SignerCertificate.Subject -match '(?i)(Tencent|腾讯)'){exit 0}else{exit 3}"
        )
        result = subprocess.run(
            [str(_trusted_powershell()), "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=15,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            env={key: value for key, value in os.environ.items() if key.upper() in {"SYSTEMROOT", "WINDIR", "PATH", "PATHEXT"}},
        )
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def discover_wechat_processes(
    *,
    enumerate_processes: Callable[[], Iterable[tuple[int, Path]]] = _enumerate_processes,
    signature_check: Callable[[Path], bool] = verify_tencent_signature,
) -> list[ProcessIdentity]:
    trusted: list[ProcessIdentity] = []
    for pid, executable in enumerate_processes():
        path = Path(executable)
        if path.name.lower() not in _WECHAT_NAMES:
            continue
        if signature_check(path):
            trusted.append(ProcessIdentity(int(pid), path))
    return sorted(trusted, key=lambda item: item.pid)


def open_process_readonly(
    pid: int,
    *,
    open_process=None,
    last_error: Callable[[], int] = ctypes.get_last_error,
) -> int:
    if open_process is None:
        kernel = _kernel32()
        kernel.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        kernel.OpenProcess.restype = ctypes.c_void_p
        open_process = kernel.OpenProcess
    handle = open_process(PROCESS_VM_READ | PROCESS_QUERY_INFORMATION, False, int(pid))
    if not handle:
        error = int(last_error())
        if error == 5:
            raise ProcessAccessDenied("same-user read access to WeChat was denied")
        raise OSError(error, "OpenProcess failed")
    return int(handle)


def close_process(handle: int) -> None:
    kernel = _kernel32()
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel.CloseHandle.restype = ctypes.c_int
    kernel.CloseHandle(handle)
