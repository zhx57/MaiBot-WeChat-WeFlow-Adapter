"""WeMai 监督器：保持 WebUI 存活并安全管理核心子进程。"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from config_webui import ConfigWebUIServer, read_env_file
from local_inbound_credentials import CredentialError, load_credentials
from wechat_key_bootstrap import WechatKeyBootstrap

ROOT = Path(__file__).resolve().parent
ENV_PATH = ROOT / ".env"
RELOAD_REQUESTED = 75


def format_uptime(seconds: int | float) -> str:
    """按月、天、小时、分钟、秒显示时长，并省略前导零单位。"""

    remaining = max(0, int(seconds))
    units = (
        (30 * 24 * 60 * 60, "月"),
        (24 * 60 * 60, "天"),
        (60 * 60, "小时"),
        (60, "分钟"),
        (1, "秒"),
    )
    values: list[tuple[int, str]] = []
    for size, label in units:
        value, remaining = divmod(remaining, size)
        values.append((value, label))
    first = next((index for index, (value, _) in enumerate(values) if value), len(values) - 1)
    return "".join(f"{value}{label}" for value, label in values[first:])


class Supervisor:
    def __init__(self, *, config_only: bool = False):
        values = read_env_file(ENV_PATH)
        self.config_only = config_only
        self.desired_running = not config_only
        self.stopped_by_user = False
        self.restart_requested = threading.Event()
        self.start_requested = threading.Event()
        self.child_stop_requested = threading.Event()
        self.stop_requested = threading.Event()
        self.interrupt_requested = threading.Event()
        self.child: subprocess.Popen | None = None
        self.child_started_at = 0.0
        self.last_exit_code: int | None = None
        self.restart_count = 0
        self.control_error = ""
        credentials_path = ROOT / "data" / "local_inbound_credentials.json"
        key_bootstrap = WechatKeyBootstrap(
            credentials_path=credentials_path,
            node_path=ROOT / "local_inbound" / "runtime" / "node.exe",
            verifier_path=ROOT / "local_inbound" / "verify-database-key.js",
        )
        self.webui = ConfigWebUIServer(
            ENV_PATH,
            values["WEBUI_HOST"],
            int(values["WEBUI_PORT"]),
            values["WEBUI_PASSWORD"],
            status_provider=self.status,
            start_callback=self.start_requested.set,
            stop_callback=self.child_stop_requested.set,
            restart_callback=self.restart_requested.set,
            shutdown_callback=self.shutdown_from_webui,
            credentials_path=credentials_path,
            local_inbound_node_path=ROOT / "local_inbound" / "runtime" / "node.exe",
            conversation_helper_path=ROOT / "local_inbound" / "list-conversations.js",
            wechat_key_bootstrap=key_bootstrap,
        )

    def status(self) -> dict:
        if self.control_error:
            return {"state": "error", "label": "操作失败", "detail": self.control_error}
        if not self.desired_running:
            if self.stopped_by_user:
                return {
                    "state": "stopped",
                    "label": "已停止",
                    "detail": "核心服务已停止，可从 WebUI 重新启动",
                }
            return {
                "state": "stopped",
                "label": "仅配置模式",
                "detail": "保存配置后启动 WeMai",
            }
        child = self.child
        if child is not None and child.poll() is None:
            elapsed = max(0, int(time.monotonic() - self.child_started_at))
            return {
                "state": "running",
                "label": "运行中",
                "detail": f"核心进程 PID {child.pid}，已运行 {format_uptime(elapsed)}",
            }
        if self.last_exit_code not in (None, 0):
            return {
                "state": "error",
                "label": "核心进程异常",
                "detail": f"退出码 {self.last_exit_code}，监督器将自动恢复",
            }
        return {"state": "stopped", "label": "已停止", "detail": "等待启动"}

    def start_child(self) -> None:
        if not self.desired_running:
            return
        if self.child is not None and self.child.poll() is None:
            return
        command = [sys.executable, str(ROOT / "main.py"), "--all"]
        environment = os.environ.copy()
        environment["WEMAI_SUPERVISED"] = "1"
        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        print("[WeMai] 正在启动核心消息服务……", flush=True)
        self.child = subprocess.Popen(
            command,
            cwd=ROOT,
            env=environment,
            creationflags=creationflags,
        )
        self.child_started_at = time.monotonic()
        self.control_error = ""

    def stop_child(self, timeout: float = 30.0) -> None:
        child, self.child = self.child, None
        if child is None or child.poll() is not None:
            return
        print("[WeMai] 正在停止核心消息服务……", flush=True)
        try:
            if os.name == "nt":
                child.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                child.send_signal(signal.SIGTERM)
            child.wait(timeout=timeout)
        except (OSError, subprocess.TimeoutExpired):
            child.terminate()
            try:
                child.wait(timeout=3)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=3)
        self.last_exit_code = child.returncode

    def _install_signal_handlers(self) -> None:
        def handle(signum, _frame):
            if signum == signal.SIGINT:
                self.interrupt_requested.set()
                return
            self.stop_requested.set()

        signal.signal(signal.SIGINT, handle)
        signal.signal(signal.SIGTERM, handle)

    def start_core_from_saved_config(self) -> bool:
        try:
            load_credentials(ROOT / "data" / "local_inbound_credentials.json")
        except (CredentialError, OSError):
            self.control_error = (
                "本地数据库凭据尚未配置，请在 WebUI 的微信数据库解密区填写"
            )
            print(f"[WeMai] 启动失败：{self.control_error}", flush=True)
            return False
        self.desired_running = True
        self.config_only = False
        self.stopped_by_user = False
        self.control_error = ""
        self.start_child()
        return True

    def stop_core_from_webui(self) -> None:
        self.desired_running = False
        self.config_only = True
        self.stopped_by_user = True
        self.control_error = ""
        self.stop_child()
        print("[WeMai] 核心消息服务已停止，WebUI 保持运行。", flush=True)

    def shutdown_from_webui(self) -> None:
        """请求监督器在主循环中安全停止全部服务并退出。"""

        self.control_error = ""
        print("[WeMai] 收到 WebUI 完全退出请求，正在安全停止全部服务……", flush=True)
        self.stop_requested.set()

    @staticmethod
    def _confirm_console_stop() -> bool:
        """使用中文确认 Ctrl+C，避免误触导致无人值守服务退出。"""
        while True:
            try:
                answer = input(
                    "\n[WeMai] 是否关闭 WeMai？输入 Y 确认，N 继续运行 [Y/N]："
                ).strip().casefold()
            except (EOFError, KeyboardInterrupt):
                print("\n[WeMai] 无法读取确认输入，继续运行。", flush=True)
                return False
            if answer in {"y", "yes", "是", "确认"}:
                print("[WeMai] 已确认关闭，正在安全停止……", flush=True)
                return True
            if answer in {"n", "no", "否", "取消", ""}:
                print("[WeMai] 已取消关闭，继续运行。", flush=True)
                return False
            print("[WeMai] 请输入 Y（关闭）或 N（继续运行）。", flush=True)

    def run(self) -> int:
        self._install_signal_handlers()
        self.webui.start()
        print(f"[WeMai] WebUI 访问地址：{self.webui.url}", flush=True)
        if self.desired_running:
            self.start_child()
        try:
            while not self.stop_requested.wait(0.5):
                if self.interrupt_requested.is_set():
                    self.interrupt_requested.clear()
                    if self._confirm_console_stop():
                        self.stop_requested.set()
                        break
                if self.restart_requested.is_set():
                    print("[WeMai] 收到 WebUI 重启请求，正在重新加载全部配置……", flush=True)
                    return RELOAD_REQUESTED
                if self.child_stop_requested.is_set():
                    self.child_stop_requested.clear()
                    self.stop_core_from_webui()
                    continue
                if self.start_requested.is_set():
                    self.start_requested.clear()
                    self.start_core_from_saved_config()
                    continue
                if not self.desired_running:
                    continue
                if self.child is not None and self.child.poll() is not None:
                    self.last_exit_code = self.child.returncode
                    self.child = None
                    if self.stop_requested.is_set():
                        break
                    stable_for = time.monotonic() - self.child_started_at
                    self.restart_count = 0 if stable_for >= 300 else self.restart_count + 1
                    delay = min(5 * (2 ** min(self.restart_count - 1, 4)), 60)
                    print(
                        f"[WeMai] 核心进程退出（代码 {self.last_exit_code}），"
                        f"{delay} 秒后自动重启。请查看 wemai.log。",
                        flush=True,
                    )
                    if self.stop_requested.wait(delay):
                        break
                    self.start_child()
        finally:
            self.stop_child()
            self.webui.close()
        return 0


def main(argv=None) -> int:
    arguments = set(sys.argv[1:] if argv is None else argv)
    config_only = "--config-only" in arguments
    while True:
        result = Supervisor(config_only=config_only).run()
        if result != RELOAD_REQUESTED:
            return result
        # WebUI 首次配置完成后进入正常运行模式；后续重载保持同一控制台进程。
        config_only = False
        print("[WeMai] 配置已重新加载，正在恢复服务……", flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
