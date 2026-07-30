"""按需管理包含 OCR 的 WeChatAuto.SDK 高级扩展宿主。"""

from __future__ import annotations

import contextlib
import logging
import os
import socket
import subprocess
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)


class OptionalHostError(RuntimeError):
    pass


class OptionalFeatureHost:
    """仅在启用联系人或群管理时启动原 SDK 扩展。"""

    def __init__(
        self,
        *,
        contacts: bool,
        groups: bool,
        port: int,
        executable: os.PathLike[str] | str | None = None,
    ):
        root = Path(__file__).resolve().parent
        self.executable = Path(executable or (
            root / "extensions" / "WeChatAutoServer" / "Server.exe"
        ))
        self.contacts = bool(contacts)
        self.groups = bool(groups)
        self.port = int(port)
        self.process: subprocess.Popen | None = None
        self.last_error: BaseException | None = None
        self._readers: list[threading.Thread] = []

    @property
    def enabled(self) -> bool:
        return self.contacts or self.groups

    @property
    def is_running(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def _pipe_reader(self, stream, level: int) -> None:
        if stream is None:
            return
        try:
            for line in stream:
                message = line.rstrip()
                if message:
                    logger.log(level, "高级扩展: %s", message)
        except (OSError, ValueError):
            pass

    def _wait_for_port(self, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self.is_running:
                code = self.process.poll() if self.process else None
                raise OptionalHostError(f"高级扩展启动后立即退出，退出码 {code}")
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=0.25):
                    return
            except OSError:
                time.sleep(0.1)
        raise OptionalHostError(f"高级扩展在 {timeout:g} 秒内未监听端口 {self.port}")

    def start(self, timeout: float = 20.0) -> None:
        if not self.enabled or self.is_running:
            return
        if not self.executable.is_file():
            raise OptionalHostError(
                "已在 WebUI 开启 OCR 高级扩展，但安装包缺少 "
                f"{self.executable}"
            )
        environment = os.environ.copy()
        environment.update({
            "ASPNETCORE_URLS": f"http://127.0.0.1:{self.port}",
            "WEMAI_CONTACTS_ENABLED": str(self.contacts).lower(),
            "WEMAI_GROUPS_ENABLED": str(self.groups).lower(),
        })
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            self.process = subprocess.Popen(
                [str(self.executable)],
                cwd=self.executable.parent,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=creationflags,
            )
            self._readers = [
                threading.Thread(
                    target=self._pipe_reader,
                    args=(self.process.stdout, logging.INFO),
                    name="optional-host-out",
                    daemon=True,
                ),
                threading.Thread(
                    target=self._pipe_reader,
                    args=(self.process.stderr, logging.WARNING),
                    name="optional-host-err",
                    daemon=True,
                ),
            ]
            for thread in self._readers:
                thread.start()
            self._wait_for_port(timeout)
            logger.info(
                "OCR 高级扩展已启动 pid=%s port=%s 联系人=%s 群管理=%s",
                self.process.pid,
                self.port,
                self.contacts,
                self.groups,
            )
        except BaseException as exc:
            self.last_error = exc
            self.close(timeout=2)
            raise

    def close(self, timeout: float = 10.0) -> None:
        process, self.process = self.process, None
        if process is None:
            return
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=max(timeout, 0.5))
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                with contextlib.suppress(OSError):
                    stream.close()
        for thread in self._readers:
            thread.join(timeout=1)
        self._readers.clear()
        logger.info("OCR 高级扩展已停止")
