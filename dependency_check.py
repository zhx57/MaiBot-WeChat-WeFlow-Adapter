"""由首次启动器调用，输出 WeMai Python 依赖检查结果。"""

from __future__ import annotations

import importlib.util
import json
from importlib import metadata

REQUIREMENTS = (
    ("wx4py", "wx4py", "0.2.1"),
    ("Pillow", "PIL", None),
    ("pyperclip", "pyperclip", None),
    ("websockets", "websockets", None),
    ("maim-message", "maim_message", "0.6.8"),
    ("aiofiles", "aiofiles", None),
    ("python-dotenv", "dotenv", None),
)


def check_dependencies() -> list[dict[str, str]]:
    missing: list[dict[str, str]] = []
    for distribution, module, required_version in REQUIREMENTS:
        if importlib.util.find_spec(module) is None:
            missing.append({
                "package": distribution,
                "reason": "未安装",
                "required": required_version or "兼容版本",
            })
            continue
        if required_version is not None:
            try:
                installed = metadata.version(distribution)
            except metadata.PackageNotFoundError:
                installed = "未知"
            if installed != required_version:
                missing.append({
                    "package": distribution,
                    "reason": f"当前版本 {installed}",
                    "required": required_version,
                })
    return missing


if __name__ == "__main__":
    print(json.dumps(check_dependencies(), ensure_ascii=False))

