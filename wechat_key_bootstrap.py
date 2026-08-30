"""Use WeMai's read-only scanner to acquire and store a local WeChat DB key."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from local_inbound_credentials import CredentialError, load_credentials, save_credentials
from wechat_key_scanner import scan_wechat_key
from wechat_key_scanner.database_locator import discover_databases as find_databases
from wechat_key_scanner.models import ScanFailure, ScanFailureCode, VerifiedKey
from wechat_key_scanner.windows_process import discover_wechat_processes


class WechatKeyError(RuntimeError):
    """Public acquisition error; messages must not include process-memory data."""


class WechatKeyProtocolError(WechatKeyError):
    pass


class WechatKeyVerificationError(WechatKeyError):
    pass


@dataclass(frozen=True)
class WechatAccount:
    id: str
    wxid: str
    account_dir: Path
    process_id: int = field(repr=False)


@dataclass(frozen=True)
class WechatKeyStatus:
    state: Literal["not_running", "not_logged_in", "logged_in", "error"]
    accounts: tuple[WechatAccount, ...] = ()
    error_code: str = ""

    def to_public_dict(self) -> dict:
        return {
            "state": self.state,
            "accounts": [
                {"id": item.id, "wxid": item.wxid, "accountDir": str(item.account_dir)}
                for item in self.accounts
            ],
            "errorCode": self.error_code,
        }


@dataclass(frozen=True)
class AcquiredCredential:
    account_dir: Path
    wxid: str
    db_key: str = field(repr=False)
    verified_database: str = "message_0.db"


def _account_dir(database_path: Path) -> Path:
    path = Path(database_path)
    if path.parent.name.lower() == "message" and path.parent.parent.name.lower() == "db_storage":
        return path.parents[2]
    for parent in path.parents:
        if parent.name.lower().startswith("wxid_"):
            return parent
    return path.parent


def _account_selection_id(process_id: int, account_dir: Path) -> str:
    value = f"{process_id}\0{os.path.normcase(str(account_dir.resolve()))}".encode("utf-8")
    return hashlib.sha256(value).hexdigest()


def save_verified_credential(path, credential: AcquiredCredential, verifier) -> None:
    target = Path(path).expanduser().resolve()
    previous = target.read_bytes() if target.exists() else None
    image_xor_key = ""
    image_aes_key = ""
    if previous is not None:
        existing = load_credentials(target)
        image_xor_key = existing["image_xor_key"]
        image_aes_key = existing["image_aes_key"]
    try:
        save_credentials(
            target,
            account_dir=credential.account_dir,
            wxid=credential.wxid,
            db_key=credential.db_key,
            image_xor_key=image_xor_key,
            image_aes_key=image_aes_key,
        )
        loaded = load_credentials(target)
        round_trip = AcquiredCredential(
            Path(loaded["account_dir"]), loaded["wxid"], loaded["db_key"],
            credential.verified_database,
        )
        if verifier(round_trip) is False:
            raise ValueError("database rejected key")
    except Exception as exc:
        if previous is None:
            target.unlink(missing_ok=True)
        else:
            rollback = target.with_suffix(target.suffix + ".rollback")
            rollback.write_bytes(previous)
            os.replace(rollback, target)
        raise WechatKeyVerificationError("数据库密钥二次验证失败，未保存") from exc


class WechatKeyBootstrap:
    def __init__(
        self,
        *,
        credentials_path=None,
        verifier=None,
        scan=None,
        discover_processes=None,
        discover_databases=None,
        node_path=None,
        verifier_path=None,
    ):
        self.credentials_path = Path(credentials_path) if credentials_path else None
        self.verifier = verifier or (lambda _credential: True)
        if verifier is None and node_path and verifier_path:
            self.verifier = lambda credential: verify_database_credential(
                Path(node_path), Path(verifier_path), credential
            )
        self._scan = scan or scan_wechat_key
        self._discover_processes = discover_processes or discover_wechat_processes
        self._discover_databases = discover_databases or find_databases
        self._last_accounts: dict[str, WechatAccount] = {}
        self._save = self._save_credential

    def status(self) -> WechatKeyStatus:
        try:
            processes = self._discover_processes()
            if not processes:
                self._last_accounts = {}
                return WechatKeyStatus("not_running")
            databases = self._discover_databases(None)
            if not databases:
                self._last_accounts = {}
                return WechatKeyStatus("not_logged_in")
            process = processes[0]
            accounts = []
            seen = set()
            for database in databases:
                account_dir = _account_dir(database.path).resolve()
                identity = os.path.normcase(str(account_dir))
                if identity in seen:
                    continue
                seen.add(identity)
                accounts.append(
                    WechatAccount(
                        _account_selection_id(process.pid, account_dir),
                        database.account_id,
                        account_dir,
                        process.pid,
                    )
                )
            result = WechatKeyStatus("logged_in", tuple(accounts))
            self._last_accounts = {item.id: item for item in result.accounts}
            return result
        except OSError as exc:
            self._last_accounts = {}
            return WechatKeyStatus("error", error_code=type(exc).__name__)

    def acquire_and_save(self, selected_id: str) -> dict:
        selected = self._last_accounts.get(str(selected_id))
        if selected is None:
            raise WechatKeyProtocolError("请选择当前已登录的微信账号")
        result = self._scan(data_root=selected.account_dir)
        if isinstance(result, ScanFailure):
            messages = {
                ScanFailureCode.PROCESS_NOT_FOUND: "未检测到当前用户运行的微信",
                ScanFailureCode.DATABASE_NOT_FOUND: "未找到当前账号的 message_0.db",
                ScanFailureCode.PERMISSION_DENIED: "当前用户无法只读访问微信进程",
                ScanFailureCode.TIMEOUT: "扫描微信进程超时",
                ScanFailureCode.READ_BUDGET_EXCEEDED: "扫描达到安全读取上限",
                ScanFailureCode.PASSIVE_UNSUPPORTED: "未找到可验证密钥；当前微信版本可能不支持被动扫描",
            }
            raise WechatKeyError(messages.get(result.code, result.message))
        if not isinstance(result, VerifiedKey):
            raise WechatKeyProtocolError("扫描器返回了无效结果")
        result_account_dir = _account_dir(result.database.path).resolve()
        if (
            result.database.account_id != selected.wxid
            or os.path.normcase(str(result_account_dir))
            != os.path.normcase(str(selected.account_dir.resolve()))
        ):
            raise WechatKeyProtocolError("扫描结果与所选账号不匹配")
        credential = AcquiredCredential(
            selected.account_dir,
            selected.wxid,
            result.key.hex(),
            result.database.path.name,
        )
        self._save(credential)
        return {
            "ok": True,
            "wxid": credential.wxid,
            "accountDir": str(credential.account_dir),
            "dbKeyHex": credential.db_key,
            "keyMode": result.mode.value,
            "restartRequired": True,
        }

    def _save_credential(self, credential: AcquiredCredential) -> None:
        if self.credentials_path is None:
            raise CredentialError("凭据保存路径未配置")
        save_verified_credential(self.credentials_path, credential, self.verifier)


def verify_database_credential(
    node_path: Path,
    verifier_path: Path,
    credential: AcquiredCredential,
    timeout: float = 10,
) -> bool:
    payload = json.dumps(
        {"accountDir": str(credential.account_dir), "dbKeyHex": credential.db_key}
    ).encode("utf-8")
    completed = subprocess.run(
        [str(node_path), str(verifier_path)],
        input=payload,
        capture_output=True,
        timeout=timeout,
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if len(completed.stdout) > 4096:
        return False
    try:
        result = json.loads(completed.stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False
    return completed.returncode == 0 and result == {"ok": True}
