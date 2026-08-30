"""使用 Windows DPAPI 保护本地微信数据库凭据。

凭据文件只保存 DPAPI 密文。明文密钥仅在初始化只读 sidecar 的短时间内存在，
不会写入日志、命令行参数或标准输出。
"""

from __future__ import annotations

import base64
import ctypes
import json
import os
import re
from ctypes import wintypes
from pathlib import Path

_ENTROPY = b"WeMai local inbound credentials v1"
_HEX_KEY = re.compile(r"^[0-9a-fA-F]{64}$")


class CredentialError(RuntimeError):
    """凭据缺失、格式错误，或当前 Windows 用户无法解密。"""


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]


def _blob(data: bytes):
    buffer = ctypes.create_string_buffer(data)
    return _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte))), buffer


def _crypt_protect(data: bytes) -> bytes:
    if os.name != "nt":
        raise CredentialError("DPAPI 凭据只能在 Windows 上使用")
    source, source_buffer = _blob(data)
    entropy, entropy_buffer = _blob(_ENTROPY)
    output = _DataBlob()
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    result = crypt32.CryptProtectData(
        ctypes.byref(source),
        "WeMai local inbound",
        ctypes.byref(entropy),
        None,
        None,
        0x01,
        ctypes.byref(output),
    )
    _ = source_buffer, entropy_buffer
    if not result:
        raise CredentialError(f"DPAPI 加密失败：{ctypes.get_last_error()}")
    try:
        return ctypes.string_at(output.pbData, output.cbData)
    finally:
        kernel32.LocalFree(output.pbData)


def _crypt_unprotect(data: bytes) -> bytes:
    if os.name != "nt":
        raise CredentialError("DPAPI 凭据只能在 Windows 上使用")
    source, source_buffer = _blob(data)
    entropy, entropy_buffer = _blob(_ENTROPY)
    output = _DataBlob()
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    result = crypt32.CryptUnprotectData(
        ctypes.byref(source),
        None,
        ctypes.byref(entropy),
        None,
        None,
        0x01,
        ctypes.byref(output),
    )
    _ = source_buffer, entropy_buffer
    if not result:
        raise CredentialError(
            "DPAPI 解密失败：凭据属于其他 Windows 用户或电脑"
        )
    try:
        return ctypes.string_at(output.pbData, output.cbData)
    finally:
        kernel32.LocalFree(output.pbData)


def _protect_text(value: str) -> str:
    return base64.b64encode(_crypt_protect(value.encode("utf-8"))).decode("ascii")


def _unprotect_text(value: str) -> str:
    try:
        encrypted = base64.b64decode(value, validate=True)
    except (TypeError, ValueError) as exc:
        raise CredentialError("凭据密文不是有效的 Base64") from exc
    return _crypt_unprotect(encrypted).decode("utf-8")


def _atomic_write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def save_credentials(
    path,
    *,
    account_dir: str,
    wxid: str,
    db_key: str,
    image_xor_key: str = "",
    image_aes_key: str = "",
) -> None:
    account = Path(account_dir).expanduser().resolve()
    if not (account / "db_storage" / "session" / "session.db").is_file():
        raise CredentialError(f"微信账号目录无效（缺少 db_storage/session/session.db）：{account}")
    wxid = str(wxid or "").strip()
    db_key = str(db_key or "").strip()
    if not wxid:
        raise CredentialError("微信真 ID 不能为空")
    if not _HEX_KEY.fullmatch(db_key):
        raise CredentialError("数据库密钥必须是 64 位十六进制字符")
    payload = {
        "version": 1,
        "account_dir": str(account),
        "wxid": wxid,
        "db_key_dpapi": _protect_text(db_key.lower()),
        "image_xor_key_dpapi": _protect_text(str(image_xor_key or "").strip()),
        "image_aes_key_dpapi": _protect_text(str(image_aes_key or "").strip()),
    }
    _atomic_write_json(Path(path).expanduser().resolve(), payload)


def load_credentials(path) -> dict:
    credential_path = Path(path).expanduser().resolve()
    try:
        payload = json.loads(credential_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise CredentialError(
            f"本地微信数据库凭据尚未配置：{credential_path}"
        ) from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise CredentialError(f"无法读取本地微信数据库凭据：{credential_path}") from exc
    if payload.get("version") != 1:
        raise CredentialError("不支持的凭据文件版本")
    result = {
        "account_dir": str(payload.get("account_dir") or "").strip(),
        "wxid": str(payload.get("wxid") or "").strip(),
        "db_key": _unprotect_text(payload.get("db_key_dpapi", "")),
        "image_xor_key": _unprotect_text(payload.get("image_xor_key_dpapi", "")),
        "image_aes_key": _unprotect_text(payload.get("image_aes_key_dpapi", "")),
    }
    if not _HEX_KEY.fullmatch(result["db_key"]):
        raise CredentialError("DPAPI 解出的数据库密钥无效")
    if not Path(result["account_dir"]).is_dir() or not result["wxid"]:
        raise CredentialError("凭据中的微信账号目录或 wxid 不完整")
    return result
