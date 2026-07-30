"""WeMai 首次启动与依赖安装器，只依赖 Python 标准库。"""

from __future__ import annotations

import contextlib
import ctypes
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENV = ROOT / ".venv"
REQUIREMENTS = ROOT / "requirements.txt"
STAMP = VENV / ".wemai_requirements.sha256"


def set_console_title() -> None:
    if os.name != "nt":
        return
    with contextlib.suppress(AttributeError, OSError):
        ctypes.windll.kernel32.SetConsoleTitleW("WeMai")


def venv_python() -> Path:
    return VENV / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def requirements_hash() -> str:
    return hashlib.sha256(REQUIREMENTS.read_bytes()).hexdigest()


def run_checked(command: list[str], description: str) -> None:
    print(f"[WeMai 安装] {description}", flush=True)
    result = subprocess.run(command, cwd=ROOT)
    if result.returncode:
        raise RuntimeError(f"{description}失败，退出码 {result.returncode}")


def inspect_dependencies(python: Path) -> list[dict[str, str]]:
    result = subprocess.run(
        [str(python), "-X", "utf8", str(ROOT / "dependency_check.py")],
        cwd=ROOT,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
    )
    if result.returncode:
        raise RuntimeError(f"依赖检查失败：{result.stderr.strip() or result.stdout.strip()}")
    return json.loads(result.stdout.strip() or "[]")


def ensure_environment() -> Path:
    if sys.version_info < (3, 9):  # noqa: UP036 - 启动器需要给旧 Python 明确中文提示
        raise RuntimeError(
            f"需要 Python 3.9 或更高版本，当前为 {sys.version.split()[0]}"
        )
    python = venv_python()
    if not python.exists():
        print(f"[WeMai 安装] 未找到项目虚拟环境：{VENV}", flush=True)
        run_checked([sys.executable, "-m", "venv", str(VENV)], "创建项目虚拟环境")

    missing = inspect_dependencies(python)
    current_hash = requirements_hash()
    stamp_changed = not STAMP.exists() or STAMP.read_text(encoding="ascii").strip() != current_hash
    if missing:
        print("[WeMai 安装] 检测到以下缺少或版本不符的依赖：", flush=True)
        for item in missing:
            print(
                f"  - {item['package']}：{item['reason']}，需要 {item['required']}",
                flush=True,
            )
    elif stamp_changed:
        print("[WeMai 安装] requirements.txt 已更新，需要同步依赖。", flush=True)
    else:
        print("[WeMai 安装] Python 依赖完整。", flush=True)

    if missing or stamp_changed:
        run_checked(
            [
                str(python), "-m", "pip", "install",
                "--disable-pip-version-check", "-r", str(REQUIREMENTS),
            ],
            "自动安装 Python 依赖",
        )
        remaining = inspect_dependencies(python)
        if remaining:
            details = "；".join(
                f"{item['package']}（{item['reason']}）" for item in remaining
            )
            raise RuntimeError(f"依赖安装完成后仍有异常：{details}")
        STAMP.write_text(current_hash + "\n", encoding="ascii")
        print("[WeMai 安装] Python 依赖安装完成。", flush=True)
    return python


def prepare_env_file() -> bool:
    env_path = ROOT / ".env"
    if env_path.exists():
        return False
    shutil.copyfile(ROOT / ".env.example", env_path)
    print("[WeMai] 已创建 .env，WebUI 地址将在命令行显示。", flush=True)
    return True


def has_weflow_token() -> bool:
    from config_webui import read_env_file

    return bool(read_env_file(ROOT / ".env").get("WEFLOW_API_TOKEN", "").strip())


def main(argv=None) -> int:
    set_console_title()
    try:
        python = ensure_environment()
        prepare_env_file()
        config_only = not has_weflow_token()
        supervisor_args = [str(python), str(ROOT / "wemai_supervisor.py")]
        if config_only:
            supervisor_args.append("--config-only")
        previous_sigint = None
        if os.name == "nt":
            # Ctrl+C 由监督器统一做中文确认，启动器不重复处理中断。
            previous_sigint = signal.signal(signal.SIGINT, signal.SIG_IGN)
        try:
            return subprocess.call(supervisor_args, cwd=ROOT)
        finally:
            if previous_sigint is not None:
                signal.signal(signal.SIGINT, previous_sigint)
    except Exception as exc:
        print("", flush=True)
        print(f"[WeMai 安装失败] {exc}", flush=True)
        print("请检查上方输出；安装日志已经保留在当前命令行窗口。", flush=True)
        if os.environ.get("WEMAI_LAUNCHED_WINDOW") == "1":
            with contextlib.suppress(EOFError, KeyboardInterrupt):
                input("按回车键关闭窗口……")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
