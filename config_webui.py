"""WeMai 本机配置 WebUI，仅使用 Python 标准库。"""

from __future__ import annotations

import contextlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import subprocess
import threading
import time
from collections.abc import Callable
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from local_inbound_credentials import CredentialError, load_credentials, save_credentials
from wechat_key_bootstrap import WechatKeyError

DEFAULTS = {
    "LOCAL_INBOUND_DISPATCH_WORKERS": "8",
    "LOCAL_INBOUND_POLL_INTERVAL_MS": "250",
    "LOCAL_INBOUND_STARTUP_TIMEOUT_SECONDS": "30",
    "LOCAL_INBOUND_MEDIA_WAIT_SECONDS": "120",
    "LOCAL_INBOUND_ALLOWLIST": "",
    "LOCAL_INBOUND_BLOCKLIST": "",
    "MAIBOT_API_URL": "ws://127.0.0.1:8000/ws",
    "MAIBOT_DATA_DIR": "",
    "INBOUND_ROUTER_CONCURRENCY": "8",
    "PLATFORM_ID": "wx",
    "WEMAI_BOT_NICKNAME": "",
    "IMAGE_SAVE_DIR": "WeMai媒体",
    "FILE_MAX_BYTES": "209715200",
    "LOG_LEVEL": "INFO",
    "WEBUI_HOST": "127.0.0.1",
    "WEBUI_PORT": "8888",
    "WEBUI_PASSWORD": "",
}

EDITABLE_KEYS = tuple(DEFAULTS)
SECRET_KEYS = {"WEBUI_PASSWORD"}
BOOL_KEYS = set()
LIST_KEYS = {"LOCAL_INBOUND_ALLOWLIST", "LOCAL_INBOUND_BLOCKLIST"}
BOUNDED_INT_KEYS = {
    "LOCAL_INBOUND_DISPATCH_WORKERS": (1, 32, "本地入站解析并发"),
    "LOCAL_INBOUND_POLL_INTERVAL_MS": (100, 60000, "本地入站轮询间隔"),
    "LOCAL_INBOUND_STARTUP_TIMEOUT_SECONDS": (1, 300, "本地入站启动超时"),
    "LOCAL_INBOUND_MEDIA_WAIT_SECONDS": (1, 600, "入站媒体等待时间"),
    "INBOUND_ROUTER_CONCURRENCY": (1, 32, "MaiBot 入站投递并发"),
}
_ENV_LINE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$")
_MANDATORY_SYSTEM_ENTRIES = {
    "gh_*",
    "brandsessionholder",
    "filehelper",
    "floatbottle",
    "fmessage",
    "medianote",
    "newsapp",
    "notification_messages",
    "qmessage",
    "qqmail",
    "weixin",
    "weixinreminder",
    "wxpay",
}


def _is_mandatory_system_entry(value: object) -> bool:
    normalized = str(value or "").strip().casefold()
    return (
        normalized.startswith("gh_")
        or "@kefu.openim" in normalized
        or normalized in _MANDATORY_SYSTEM_ENTRIES
    )


def _parse_bool_value(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().casefold()
        if normalized in {"true", "1", "yes", "on"}:
            return True
        if normalized in {"false", "0", "no", "off", ""}:
            return False
    raise ValueError("布尔配置必须是 true 或 false")


def _decode_env_value(raw: str) -> str:
    value = raw.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        if value[0] == '"':
            try:
                return json.loads(value)
            except json.JSONDecodeError:
                pass
        return value[1:-1]
    return value


def read_env_file(path: os.PathLike[str] | str) -> dict[str, str]:
    """读取简单 dotenv；不存在时返回默认值。"""
    values = dict(DEFAULTS)
    env_path = Path(path)
    if not env_path.exists():
        return values
    for line in env_path.read_text(encoding="utf-8-sig").splitlines():
        match = _ENV_LINE.match(line.strip())
        if match and (
            match.group(1) in DEFAULTS or match.group(1) == "WX_BOT_NICKNAME"
        ):
            values[match.group(1)] = _decode_env_value(match.group(2))
    if not values["WEMAI_BOT_NICKNAME"]:
        values["WEMAI_BOT_NICKNAME"] = values.get("WX_BOT_NICKNAME", "")
    values["PLATFORM_ID"] = "wx"
    return values


def _encode_env_value(value: str) -> str:
    if not value:
        return ""
    if re.fullmatch(r"[^\s#'\"]+", value):
        return value
    return json.dumps(value, ensure_ascii=False)


def update_env_file(path: os.PathLike[str] | str, updates: dict[str, str]) -> None:
    """原子更新 dotenv，同时保留用户的注释和未知配置。"""
    env_path = Path(path)
    lines = env_path.read_text(encoding="utf-8-sig").splitlines() if env_path.exists() else []
    pending = dict(updates)
    output: list[str] = []
    for line in lines:
        match = _ENV_LINE.match(line.strip())
        if match and match.group(1) in pending:
            key = match.group(1)
            output.append(f"{key}={_encode_env_value(pending.pop(key))}")
        else:
            output.append(line)
    if pending:
        if output and output[-1].strip():
            output.append("")
        output.append("# 由 WeMai WebUI 保存")
        for key in EDITABLE_KEYS:
            if key in pending:
                output.append(f"{key}={_encode_env_value(pending[key])}")
    env_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = env_path.with_suffix(env_path.suffix + ".tmp")
    temporary.write_text("\n".join(output).rstrip() + "\n", encoding="utf-8")
    os.replace(temporary, env_path)


def validate_updates(payload: object, current: dict[str, str]) -> dict[str, str]:
    if not isinstance(payload, dict):
        raise ValueError("配置数据必须是对象")
    updates: dict[str, str] = {}
    for key in EDITABLE_KEYS:
        if key not in payload:
            continue
        value = payload[key]
        if key in BOOL_KEYS:
            updates[key] = "true" if _parse_bool_value(value) else "false"
        elif key in LIST_KEYS:
            if not isinstance(value, str):
                raise ValueError(f"{key} 必须是文本")
            entries = []
            seen = set()
            for item in re.split(r"[\r\n,]+", value):
                item = item.strip()
                folded = item.casefold()
                if item and folded not in seen:
                    seen.add(folded)
                    entries.append(item)
            if len(entries) > 1000:
                raise ValueError("单个入站名单最多 1000 项")
            updates[key] = "\n".join(entries)
        elif isinstance(value, (str, int)):
            updates[key] = str(value).strip()
        else:
            raise ValueError(f"{key} 的值无效")

    merged = {**current, **updates}
    if merged["LOCAL_INBOUND_ALLOWLIST"] and merged["LOCAL_INBOUND_BLOCKLIST"]:
        raise ValueError("入站白名单和黑名单不能同时启用")
    if merged["PLATFORM_ID"].strip().lower() != "wx":
        raise ValueError("MaiBot 平台标识固定为 wx")
    host = merged["WEBUI_HOST"].strip()
    try:
        address = ipaddress.ip_address(host)
    except ValueError as exc:
        if host.lower() != "localhost":
            raise ValueError("WebUI 监听地址必须是 IP 地址或 localhost") from exc
        address = ipaddress.ip_address("127.0.0.1")
    try:
        port = int(merged["WEBUI_PORT"])
    except ValueError as exc:
        raise ValueError("WebUI 端口必须是整数") from exc
    if not 1 <= port <= 65535:
        raise ValueError("WebUI 端口必须在 1 到 65535 之间")
    try:
        file_max_bytes = int(merged["FILE_MAX_BYTES"])
    except ValueError as exc:
        raise ValueError("普通文件上限必须是有效的 MB 数值") from exc
    if not 1 <= file_max_bytes <= 200 * 1024 * 1024:
        raise ValueError("普通文件上限必须在 1 字节到 200 MB 之间")
    for key, (minimum, maximum, label) in BOUNDED_INT_KEYS.items():
        try:
            value = int(merged[key])
        except ValueError as exc:
            raise ValueError(f"{label}必须是整数") from exc
        if not minimum <= value <= maximum:
            raise ValueError(f"{label}必须在 {minimum} 到 {maximum} 之间")
    if not address.is_loopback and not merged["WEBUI_PASSWORD"]:
        raise ValueError("监听 0.0.0.0 或局域网地址时必须设置访问密码")
    if merged["LOG_LEVEL"].upper() not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
        raise ValueError("日志级别无效")
    if not merged["MAIBOT_API_URL"].startswith(("ws://", "wss://")):
        raise ValueError("MaiBot 地址必须以 ws:// 或 wss:// 开头")
    return updates


def _load_local_contact_options(
    credentials_path: os.PathLike[str] | str | None,
    node_path: os.PathLike[str] | str | None,
    helper_path: os.PathLike[str] | str | None,
) -> list[dict[str, object]]:
    """通过一次性只读 Node helper 解密读取微信完整通讯录。"""

    if not credentials_path or not node_path or not helper_path:
        return []
    try:
        credentials = load_credentials(credentials_path)
        node = Path(node_path)
        helper = Path(helper_path)
        if not node.is_file() or not helper.is_file():
            return []
        payload = json.dumps(
            {
                "accountDir": credentials["account_dir"],
                "wxid": credentials["wxid"],
                "dbKeyHex": credentials["db_key"],
            },
            ensure_ascii=False,
        )
        completed = subprocess.run(
            [str(node), str(helper)],
            input=payload,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=8,
            cwd=str(helper.parent),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            check=False,
        )
        if completed.returncode != 0:
            return []
        result = json.loads(completed.stdout.strip() or "{}")
        rows = result.get("conversations")
        if not isinstance(rows, list):
            return []
        return [row for row in rows if isinstance(row, dict)]
    except (CredentialError, OSError, ValueError, json.JSONDecodeError, subprocess.SubprocessError):
        return []


def load_conversation_options(
    database_path: os.PathLike[str] | str,
    *,
    credentials_path: os.PathLike[str] | str | None = None,
    node_path: os.PathLike[str] | str | None = None,
    helper_path: os.PathLike[str] | str | None = None,
) -> list[dict[str, object]]:
    """只读取微信本地通讯录好友和仍在群内的群聊。"""

    del database_path  # 历史 WeMai/MaiBot 映射不属于通讯录候选来源。
    options: list[dict[str, object]] = []
    seen: set[str] = set()

    def append_option(raw: object) -> None:
        if not isinstance(raw, dict):
            return
        identifier = str(raw.get("id") or raw.get("identifier") or "").strip()
        if (
            not identifier
            or identifier.casefold() in seen
            or _is_mandatory_system_entry(identifier)
        ):
            return
        name = str(raw.get("name") or identifier).strip() or identifier
        normalized_id = identifier.casefold()
        normalized_type = str(raw.get("type") or "").strip().casefold()
        if normalized_type not in {"private", "group"}:
            return
        seen.add(normalized_id)
        try:
            updated = float(raw.get("updated") or 0)
        except (TypeError, ValueError):
            updated = 0.0
        options.append({"id": identifier, "name": name, "type": normalized_type, "updated": updated})

    # 候选只来自微信 contact.db；helper 已按好友和在群状态过滤。
    for row in _load_local_contact_options(credentials_path, node_path, helper_path):
        append_option(row)
    return options
HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="icon" type="image/png" href="/static/favicon.png">
<title>WeMai · 控制中心</title>
<style>
:root{--sidebar-w:200px}
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
html{scroll-behavior:smooth}
body{margin:0;color:#1a3040;font:14px/1.6 -apple-system,"Segoe UI","Microsoft YaHei","PingFang SC",sans-serif;background:#f0f6fc;min-height:100vh;background-image:radial-gradient(ellipse 80% 50% at 50% -20%,rgba(56,183,216,.08) 0%,transparent 70%),radial-gradient(ellipse 60% 40% at 20% 90%,rgba(22,139,195,.06) 0%,transparent 60%);background-attachment:fixed}
.shell{display:flex;flex-direction:column;min-height:100vh}
.topbar{position:sticky;top:0;z-index:50;height:56px;display:flex;align-items:center;padding:0 20px;gap:12px;background:rgba(255,255,255,.78);backdrop-filter:blur(16px) saturate(1.5);border-bottom:1px solid rgba(22,139,195,.12);box-shadow:0 1px 0 rgba(255,255,255,.8) inset,0 1px 3px rgba(0,0,0,.04)}
.brandmark{width:30px;height:30px;border-radius:7px;overflow:hidden;flex-shrink:0;background:linear-gradient(135deg,#08658f,#168bc3);display:grid;place-items:center;box-shadow:0 2px 6px rgba(22,139,195,.2)}
.brandmark img{width:22px;height:22px;border-radius:4px;object-fit:cover}
.brand{display:flex;flex-direction:column;font-size:15px;font-weight:700;color:#123c56;line-height:1.3}
.brand small{font-size:10px;font-weight:500;color:#6b8e9f}
.topbar-right{margin-left:auto;display:flex;align-items:center;gap:14px}
.status-badge{display:flex;align-items:center;gap:7px;padding:4px 12px 4px 9px;border-radius:20px;font-size:12px;font-weight:600;background:rgba(22,139,195,.06);border:1px solid rgba(22,139,195,.12);color:#2f5b70}
.dot{width:8px;height:8px;border-radius:50%;background:#b8ccda;flex-shrink:0;transition:background .4s,box-shadow .4s}
.dot.running{background:#24b47e;animation:pulse-r 2s infinite}
.dot.stopped{background:#f5a623}
.dot.error{background:#e8525f;animation:pulse-e 1.5s infinite}
@keyframes pulse-r{0%{box-shadow:0 0 0 0 rgba(36,180,126,.5)}70%{box-shadow:0 0 0 8px rgba(36,180,126,0)}100%{box-shadow:0 0 0 0 rgba(36,180,126,0)}}
@keyframes pulse-e{0%,100%{box-shadow:0 0 0 0 rgba(232,82,95,.4)}50%{box-shadow:0 0 0 6px rgba(232,82,95,0)}}
.layout-wrapper{display:flex;flex:1;min-height:calc(100vh - 56px)}
.sidebar{width:var(--sidebar-w);flex-shrink:0;background:rgba(255,255,255,.55);backdrop-filter:blur(12px);border-right:1px solid rgba(22,139,195,.1);padding:16px 10px;display:flex;flex-direction:column;gap:4px}
.nav-item{display:flex;align-items:center;gap:10px;padding:10px 12px;border-radius:8px;cursor:pointer;font-size:13px;font-weight:500;color:#4d6e82;transition:all .15s;user-select:none}
.nav-item:hover{background:rgba(22,139,195,.08);color:#1d4b63}
.nav-item.active{background:rgba(22,139,195,.12);color:#08658f;font-weight:650}
.nav-item svg{width:18px;height:18px;flex-shrink:0;opacity:.75}
.nav-item.active svg{opacity:1}
.main-content{flex:1;min-width:0;padding:24px 28px 40px;animation:fade-up .35s ease-out}
@keyframes fade-up{from{opacity:0;transform:translateY(10px)}to{opacity:1;transform:translateY(0)}}
.page-section{display:none}
.page-section.active{display:block}
.pagehead{margin-bottom:20px}
.pagehead h1{font-size:20px;font-weight:700;color:#0e2e40;letter-spacing:-.3px}
.pagehead p{margin:4px 0 0;color:#608094;font-size:13px}
.status-card{background:rgba(255,255,255,.75);backdrop-filter:blur(12px);border:1px solid rgba(255,255,255,.85);border-radius:10px;padding:16px 20px;margin-bottom:18px;display:flex;align-items:center;gap:12px;box-shadow:0 2px 8px rgba(0,0,0,.03),0 8px 24px rgba(22,139,195,.06)}
.status-card .dot{width:11px;height:11px}
.status-card .status-info{flex:1}
.status-card .status-label{font-weight:650;font-size:14px;color:#123c56}
.status-card .status-detail{font-size:12px;color:#608094;margin-top:1px}
.cards{display:flex;flex-direction:column;gap:14px}
.card{background:rgba(255,255,255,.72);backdrop-filter:blur(12px);border:1px solid rgba(255,255,255,.82);border-radius:10px;overflow:hidden;box-shadow:0 1px 3px rgba(0,0,0,.02),0 6px 20px rgba(22,139,195,.05);transition:box-shadow .25s}
.card:hover{box-shadow:0 1px 3px rgba(0,0,0,.02),0 8px 28px rgba(22,139,195,.08)}
.card-header{display:flex;align-items:center;gap:8px;font-size:13px;font-weight:650;padding:13px 18px;color:#1d4b63;background:linear-gradient(90deg,rgba(219,242,255,.6),rgba(255,255,255,.3));border-bottom:1px solid rgba(154,201,226,.25);user-select:none}
.card-header svg{width:15px;height:15px;flex-shrink:0;opacity:.7}
.fields{padding:18px;display:grid;grid-template-columns:1fr 1fr;gap:16px 20px}
.field{min-width:0;display:flex;flex-direction:column}
.field.wide{grid-column:1/-1}
label{display:block;font-weight:600;font-size:13px;margin-bottom:5px;color:#1d4b63}
.hint{display:block;color:#7899ac;font-size:11.5px;margin-top:4px;line-height:1.4;min-height:16px}
.hint.configured{color:#187a58;font-weight:600}
input,select,textarea{width:100%;height:38px;border:1.5px solid rgba(154,201,226,.4);border-radius:7px;background:rgba(255,255,255,.85);padding:0 11px;color:#1a3040;font-size:13px;font-family:inherit;outline:none;transition:border-color .2s,box-shadow .2s,background .2s}
textarea{height:88px;padding:9px 11px;resize:vertical;line-height:1.5}
input::placeholder{color:#a6bfce}
input:hover,select:hover,textarea:hover{border-color:rgba(22,139,195,.45)}
input:focus,select:focus,textarea:focus{border-color:#168bc3;background:#fff;box-shadow:0 0 0 3px rgba(22,139,195,.1)}
input[readonly]{background:rgba(200,220,235,.15);color:#608094;cursor:default}
select{cursor:pointer;appearance:none;background-image:url("data:image/svg+xml,%3Csvg xmlns=%27http://www.w3.org/2000/svg%27 width=%2712%27 height=%2712%27 viewBox=%270 0 24 24%27 fill=%27none%27 stroke=%27%23608094%27 stroke-width=%272%27 stroke-linecap=%27round%27 stroke-linejoin=%27round%27%3E%3Cpath d=%27m6 9 6 6 6-6%27/%3E%3C/svg%3E");background-repeat:no-repeat;background-position:right 10px center;padding-right:30px}
.segmented{display:grid;grid-template-columns:repeat(3,1fr);gap:4px;padding:4px;border-radius:8px;background:rgba(22,139,195,.07);border:1px solid rgba(22,139,195,.12)}
.segmented label{margin:0;cursor:pointer;text-align:center;padding:6px 8px;border-radius:6px;color:#608094;font-size:12px;transition:all .15s}
.segmented input{position:absolute;opacity:0;pointer-events:none;width:1px;height:1px}
.segmented label:has(input:checked){background:#fff;color:#08658f;box-shadow:0 1px 4px rgba(22,139,195,.15);font-weight:650}
.conversation-picker{display:flex;flex-direction:column;gap:7px}
.conversation-list{height:164px;overflow:auto;padding:6px;background:rgba(255,255,255,.72);border:1px solid rgba(154,201,226,.35);border-radius:7px}
.conversation-option{display:flex;align-items:center;gap:8px;width:100%;min-height:34px;padding:6px 8px;border:1px solid transparent;border-radius:5px;background:transparent;color:#365d70;text-align:left;cursor:pointer;font:inherit;transition:background .14s,border-color .14s,color .14s}
.conversation-option:hover{background:rgba(22,139,195,.08);border-color:rgba(22,139,195,.2)}
.conversation-option.selected{background:#168bc3;border-color:#0d719f;color:#fff;box-shadow:0 1px 3px rgba(22,139,195,.2)}
.conversation-option .checkmark{display:inline-flex;align-items:center;justify-content:center;width:17px;height:17px;flex:none;border:1px solid #9ac9df;border-radius:4px;background:#fff;color:transparent;font-size:12px;font-weight:700}
.conversation-option.selected .checkmark{border-color:#fff;background:#fff;color:#168bc3}
.conversation-option .conversation-label{min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.conversation-option .conversation-id{margin-left:auto;max-width:48%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;opacity:.72;font-size:11px}
.conversation-option.selected .conversation-id{opacity:.9}
.filter-disabled{opacity:.5;pointer-events:none}
.warning{display:none;margin:0 18px 16px;padding:10px 14px;border-radius:7px;border-left:3px solid #e8a531;background:rgba(255,248,230,.85);color:#7a5418;font-size:12.5px;line-height:1.5}
.actions{display:flex;align-items:center;gap:10px;padding:16px 18px;border-top:1px solid rgba(154,201,226,.2)}
.actions .spacer{flex:1}
button{height:36px;border:1.5px solid rgba(154,201,226,.3);border-radius:7px;background:rgba(255,255,255,.85);padding:0 16px;cursor:pointer;font-weight:600;font-size:13px;font-family:inherit;color:#1d4b63;transition:all .18s;display:inline-flex;align-items:center;gap:6px;white-space:nowrap}
button:hover{background:#fff;border-color:rgba(22,139,195,.35);box-shadow:0 2px 8px rgba(22,139,195,.08)}
button:active{transform:scale(.97)}
button.primary{background:linear-gradient(135deg,#08658f,#168bc3);border-color:#168bc3;color:#fff;box-shadow:0 2px 8px rgba(22,139,195,.2)}
button.primary:hover{background:linear-gradient(135deg,#075379,#1076a8);box-shadow:0 4px 14px rgba(22,139,195,.3)}
button.danger{color:#b84050;border-color:rgba(184,64,80,.2);background:rgba(255,245,246,.7)}
button.danger:hover{background:#fff;border-color:rgba(184,64,80,.35);box-shadow:0 2px 8px rgba(184,64,80,.08)}
button:disabled{opacity:.5;cursor:wait;transform:none}
button svg{width:15px;height:15px}
.log-toolbar{display:flex;align-items:center;gap:12px;margin-bottom:14px;flex-wrap:wrap}
.log-toolbar label{font-size:12px;margin-bottom:0;white-space:nowrap}
.log-toolbar select{width:auto;min-width:80px;height:32px;font-size:12px}
.log-toolbar .log-toggle{display:flex;align-items:center;gap:6px;cursor:pointer;font-size:12px;color:#4d6e82;user-select:none}
.log-toolbar .log-toggle input[type=checkbox]{width:16px;height:16px;cursor:pointer;accent-color:#168bc3}
.log-toolbar .log-info{font-size:11px;color:#7899ac;margin-left:auto}
.log-container{background:rgba(255,255,255,.7);backdrop-filter:blur(8px);border:1px solid rgba(154,201,226,.25);border-radius:8px;overflow:hidden}
.log-container .log-header{display:flex;align-items:center;padding:10px 14px;background:rgba(219,242,255,.35);border-bottom:1px solid rgba(154,201,226,.2);font-size:12px;font-weight:600;color:#4d6e82;gap:10px}
.log-output{overflow-y:auto;max-height:calc(100vh - 300px);min-height:350px;font:12px/1.6 "Cascadia Code","Fira Code","Consolas","Courier New",monospace;padding:12px 14px;color:#1a3040}
.log-output .line{padding:0;white-space:pre-wrap;word-break:break-all}
.log-output .line.debug{color:#6b8e9f}
.log-output .line.info{color:#1a3040}
.log-output .line.warning{color:#b8860b}
.log-output .line.error{color:#c0394a;font-weight:600}
.log-output .line.critical{color:#c0394a;font-weight:700;background:rgba(192,57,74,.06)}
.log-empty{display:flex;align-items:center;justify-content:center;height:200px;color:#7899ac;font-size:13px}
.toast-container{position:fixed;right:20px;bottom:20px;z-index:999;pointer-events:none}
.toast{pointer-events:auto;max-width:380px;padding:11px 16px;border-radius:9px;font-size:13px;line-height:1.5;color:#fff;box-shadow:0 6px 24px rgba(0,0,0,.12);animation:toast-in .35s ease-out;margin-top:8px}
.toast.info{background:#1d4b63}
.toast.error{background:#c0394a}
.toast.success{background:#187a58}
@keyframes toast-in{from{opacity:0;transform:translateY(14px) scale(.95)}to{opacity:1;transform:translateY(0) scale(1)}}
@media(max-width:780px){
  .sidebar{width:56px;padding:12px 8px}
  .nav-item{padding:10px;justify-content:center}
  .nav-item span{display:none}
  .main-content{padding:16px 14px 30px}
  .brand small{display:none}
  .status-badge{padding:3px 9px 3px 7px;font-size:11px}
  .pagehead h1{font-size:18px}
  .fields{grid-template-columns:1fr;padding:14px;gap:12px}
  .card-header{padding:11px 14px}
  .actions{flex-wrap:wrap;padding:12px 14px}
  .actions .spacer{display:none}
  .actions button{flex:1 1 calc(50% - 6px);min-width:110px;justify-content:center}
  .toast-container{left:12px;right:12px;bottom:12px}
  .toast{max-width:none}
  input,select,button{font-size:15px}
}
</style>
</head>
<body>
<div class="shell">
  <header class="topbar">
    <div class="brandmark"><img src="/static/favicon.png" alt="WeMai"></div>
    <div class="brand">WeMai <small>控制中心</small></div>
    <div class="topbar-right">
      <div class="status-badge">
        <span id="statusDot" class="dot"></span>
        <span id="statusText">读取状态</span>
      </div>
    </div>
  </header>
  <div class="layout-wrapper">
    <nav class="sidebar" id="sidebar">
      <div class="nav-item active" data-page="config" onclick="switchPage(&#39;config&#39;)">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06A1.65 1.65 0 0 0 4.68 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06A1.65 1.65 0 0 0 9 4.68a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>
        <span>控制面板</span>
      </div>
      <div class="nav-item" data-page="logs" onclick="switchPage(&#39;logs&#39;)">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/><polyline points="10 9 9 9 8 9"/></svg>
        <span>运行日志</span>
      </div>
    </nav>
    <main class="main-content">
      <div id="page-config" class="page-section active">
        <div class="pagehead">
          <h1>运行配置</h1>
          <p id="runtimeLine">正在读取 WeMai 状态</p>
        </div>
        <div class="status-card" id="statusCard">
          <span id="statusCardDot" class="dot"></span>
          <div class="status-info">
            <div class="status-label" id="statusCardLabel">连接中</div>
            <div class="status-detail" id="statusCardDetail">正在获取服务状态</div>
          </div>
        </div>
        <form id="form">
          <div class="cards">
            <div class="card">
              <div class="card-header"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="11" width="18" height="10" rx="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/></svg>微信数据库解密</div>
              <div class="fields">
                <div class="field wide"><label>微信账号目录</label><input id="credentialAccountDir" autocomplete="off"><span class="hint">填写包含 db_storage 的账号目录</span></div>
                <div class="field wide"><label>当前账号ID（wxid）</label><input id="credentialWxid" autocomplete="off"></div>
                <div class="field wide"><label>64 位十六进制数据库密钥</label><input id="credentialDatabaseKey" type="text" autocomplete="off" spellcheck="false"><span class="hint">密钥会显示在本机 WebUI，并通过 Windows DPAPI 加密保存</span></div>
                <div class="field"><label>图片 XOR 密钥</label><input id="credentialImageXorKey" type="password" autocomplete="new-password"><span class="hint">用于入站图片解密，留空保持现值</span></div>
                <div class="field"><label>图片 AES 密钥</label><input id="credentialImageAesKey" type="password" autocomplete="new-password"><span class="hint">用于入站图片解密，留空保持现值</span></div>
                <div class="field wide"><span id="credentialStatus" class="hint">正在检查数据库凭据</span></div>
                <div class="field wide"><label>首次自动获取</label><select id="wechatAccountSelect" disabled><option value="">正在检测微信登录状态</option></select><span id="wechatKeyStatus" class="hint">请先启动并登录电脑版微信</span></div>
              </div>
              <div class="actions"><button id="acquireWechatKey" type="button">自动获取密钥</button><div class="spacer"></div><button id="saveCredentials" type="button" class="primary"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z"/><polyline points="17 21 17 13 7 13 7 21"/></svg>保存数据库凭据</button></div>
            </div>
            <div class="card">
              <div class="card-header"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12h14"/><path d="M12 5l7 7-7 7"/></svg>服务连接</div>
              <div class="fields">
                <div class="field"><label>本地入站解析并发</label><input name="LOCAL_INBOUND_DISPATCH_WORKERS" inputmode="numeric"><span class="hint">同一会话串行，不同会话默认最多 8 路</span></div>
                <div class="field"><label>MaiBot 入站投递并发</label><input name="INBOUND_ROUTER_CONCURRENCY" inputmode="numeric"><span class="hint">与解析并发保持一致，建议使用 8</span></div>
                <div class="field"><label>数据库保险轮询（毫秒）</label><input name="LOCAL_INBOUND_POLL_INTERVAL_MS" inputmode="numeric"><span class="hint">文件变更会立即触发，250 毫秒仅用于防漏</span></div>
                <div class="field"><label>入站启动超时（秒）</label><input name="LOCAL_INBOUND_STARTUP_TIMEOUT_SECONDS" inputmode="numeric"><span class="hint">数据库短暂占用时会自动重试</span></div>
                <div class="field"><label>媒体等待上限（秒）</label><input name="LOCAL_INBOUND_MEDIA_WAIT_SECONDS" inputmode="numeric"><span class="hint">文件最长等待 120 秒；图片和语音最多等待 10 秒</span></div>
                <div class="field wide"><label>MaiBot WebSocket</label><input name="MAIBOT_API_URL" autocomplete="url"></div>
                <div class="field wide"><label>MaiBot data 目录</label><input name="MAIBOT_DATA_DIR"></div>
                <div class="field wide"><label>MaiBot 平台标识</label><input name="PLATFORM_ID" value="wx" readonly><span class="hint">WeMai 与 MaiBot 两端必须一致，固定为 wx</span></div>
                <div class="field wide"><label>入站名单模式</label><div class="segmented" id="inboundFilterMode"><label><input type="radio" name="inboundFilterMode" value="all" checked>全部会话</label><label><input type="radio" name="inboundFilterMode" value="allow">仅白名单</label><label><input type="radio" name="inboundFilterMode" value="block">排除黑名单</label></div><span class="hint">白名单与黑名单二选一；微信系统用户固定不入站且不显示</span></div>
                <div class="field wide conversation-picker" id="conversationPicker"><label>选择会话</label><input id="conversationSearch" placeholder="搜索通讯录好友、已添加群聊或微信真 ID"><div id="inboundConversationList" class="conversation-list" role="listbox" aria-label="入站名单会话" aria-multiselectable="true"></div><span class="hint">鼠标单击即可勾选，再次单击取消；列表仅来自微信本地通讯录，与 MaiBot 聊天流和 WeMai 历史映射无关</span></div>
                <div class="field wide"><label>手动补充会话真 ID</label><textarea id="inboundManualEntries" placeholder="列表中尚未出现的会话可在此填写，每行一个"></textarea><span class="hint">优先填写稳定真 ID；已在上方选择的会话无需重复填写</span></div>
                <input type="hidden" name="LOCAL_INBOUND_ALLOWLIST"><input type="hidden" name="LOCAL_INBOUND_BLOCKLIST">
              </div>
            </div>
            <div class="card">
              <div class="card-header"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>微信与文件</div>
              <div class="fields">
                <div class="field"><label>机器人微信昵称</label><input name="WEMAI_BOT_NICKNAME"></div>
                <div class="field"><label>日志级别</label><select name="LOG_LEVEL"><option>DEBUG</option><option>INFO</option><option>WARNING</option><option>ERROR</option><option>CRITICAL</option></select></div>
                <div class="field"><label>媒体保存目录</label><input name="IMAGE_SAVE_DIR"></div>
                <div class="field"><label>普通文件上限（MB）</label><input name="FILE_MAX_BYTES" inputmode="decimal" type="number" min="0.001" max="200" step="0.001"><span class="hint">默认 200 MB，内部自动换算为字节；文件采用磁盘流式保存</span></div>
              </div>
            </div>
            <div class="card">
              <div class="card-header"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="2" y="3" width="20" height="14" rx="2" ry="2"/><line x1="8" y1="21" x2="16" y2="21"/><line x1="12" y1="17" x2="12" y2="21"/></svg>WebUI 访问</div>
              <div class="fields">
                <div class="field"><label>监听地址</label><input name="WEBUI_HOST"><span class="hint">本机使用 127.0.0.1，局域网使用 0.0.0.0</span></div>
                <div class="field"><label>端口</label><input name="WEBUI_PORT" inputmode="numeric"></div>
                <div class="field wide"><label>访问密码</label><input name="WEBUI_PASSWORD" type="password" autocomplete="new-password"><span id="passwordHint" class="hint"></span></div>
              </div>
              <div class="warning" id="networkWarning" style="display:none">对外监听必须设置访问密码。配置页包含服务地址和控制操作，请勿直接暴露到公网。</div>
            </div>
          </div>
          <div class="actions">
            <div class="spacer"></div>
            <button id="start" type="button"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polygon points="5 3 19 12 5 21 5 3"/></svg>启动</button>
            <button id="stop" class="danger" type="button"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="6" y="6" width="12" height="12" rx="2"/></svg>停止</button>
            <button id="restart" class="danger" type="button"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21.5 2v6h-6M2.5 22v-6h6M2 11.5a10 10 0 0 1 18.8-4.3M22 12.5a10 10 0 0 1-18.8 4.2"/></svg>重启</button>
            <button id="shutdown" class="danger" type="button" title="停止全部服务并关闭 WebUI"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M18.36 6.64a9 9 0 1 1-12.73 0"/><line x1="12" y1="2" x2="12" y2="12"/></svg>完全退出</button>
            <button id="save" class="primary" type="submit"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z"/><polyline points="17 21 17 13 7 13 7 21"/><polyline points="7 3 7 8 15 8"/></svg>保存配置</button>
          </div>
        </form>
      </div>
      <div id="page-logs" class="page-section">
        <div class="pagehead">
          <h1>运行日志</h1>
          <p id="logStatusLine">实时查看 WeMai 日志</p>
        </div>
        <div class="log-toolbar">
          <label>显示行数</label>
          <select id="logLines" onchange="loadLogs()">
            <option value="50">50</option><option value="100">100</option><option value="200" selected>200</option>
            <option value="500">500</option><option value="1000">1000</option>
          </select>
          <label style="margin-left:8px">级别</label>
          <select id="logLevelFilter" onchange="filterLogs()">
            <option value="ALL">全部</option><option value="CRITICAL">CRITICAL</option>
            <option value="ERROR">ERROR</option><option value="WARNING">WARNING</option>
            <option value="INFO">INFO</option><option value="DEBUG">DEBUG</option>
          </select>
          <label class="log-toggle" style="margin-left:8px">
            <input type="checkbox" id="autoRefresh" checked onchange="toggleAutoRefresh()">自动刷新
          </label>
          <button style="height:32px;font-size:12px;padding:0 12px" onclick="loadLogs()">刷新</button>
          <button style="height:32px;font-size:12px;padding:0 12px;margin-left:4px" onclick="scrollLogToBottom()">到底部</button>
          <span class="log-info" id="logInfo"></span>
        </div>
        <div class="log-container">
          <div class="log-header">
            <span>wemai.log</span>
            <span id="logFileInfo" style="color:#7899ac;font-weight:400;font-size:11px"></span>
          </div>
          <div class="log-output" id="logOutput">
            <div class="log-empty">正在加载日志...</div>
          </div>
        </div>
      </div>
    </main>
  </div>
</div>
<div class="toast-container" id="toastContainer"></div>
<script>
var form=document.querySelector("#form");var toastContainer=document.querySelector("#toastContainer");var configured={};var autoRefreshTimer=null;var allLogLines=[];var conversationOptions=[];var selectedConversationIds=new Set();
function notify(m,t){t=t||"info";var e=document.createElement("div");e.className="toast "+t;e.textContent=m;toastContainer.appendChild(e);setTimeout(function(){e.style.opacity="0";e.style.transform="translateY(8px)"},3500);setTimeout(function(){e.remove()},3900)}
function switchPage(p){document.querySelectorAll(".nav-item").forEach(function(n){n.classList.remove("active")});var i=document.querySelector('.nav-item[data-page="'+p+'"]');if(i)i.classList.add("active");document.querySelectorAll(".page-section").forEach(function(s){s.classList.remove("active")});var sec=document.getElementById("page-"+p);if(sec)sec.classList.add("active");if(p==="logs"){loadLogs();startAutoRefresh()}else{stopAutoRefresh()}}
function updateWarnings(){var h=form.elements.WEBUI_HOST.value.trim();document.querySelector("#networkWarning").style.display=(h!=="127.0.0.1"&&h!=="localhost"&&h!=="::1")?"block":"none"}
async function api(p,o){o=o||{};var h={"Content-Type":"application/json"};if(o.headers){for(var k in o.headers){h[k]=o.headers[k]}}var r=await fetch(p,{cache:"no-store",method:o.method,body:o.body,headers:h});var d=await r.json().catch(function(){return{}});if(!r.ok)throw new Error(d.error||JSON.stringify(d));return d}
function secretState(key,hintId,emptyText){var el=form.elements[key],hint=document.querySelector(hintId);var saved=configured.configuredSecrets.indexOf(key)>=0;el.value="";el.placeholder=saved?"已保存（留空表示不修改）":emptyText;hint.textContent=saved?"已保存，留空保持原值":emptyText;if(saved){hint.className="hint configured"}else{hint.className="hint"}}
async function loadCredentials(){var d=await api("/api/credentials?_="+Date.now());var a=document.querySelector("#credentialAccountDir"),w=document.querySelector("#credentialWxid"),s=document.querySelector("#credentialStatus");a.value=d.accountDir||"";w.value=d.wxid||"";document.querySelector("#credentialDatabaseKey").value=d.databaseKey||"";document.querySelector("#credentialImageXorKey").value="";document.querySelector("#credentialImageAesKey").value="";s.textContent=d.configured?"凭据有效，账号 "+d.wxid+"；数据库密钥已显示，图片密钥 "+((d.imageXorKeyConfigured&&d.imageAesKeyConfigured)?"完整":"未完整配置"):"尚未配置，请填写后保存";s.className=d.configured?"hint configured":"hint"}
async function loadWechatKeyStatus(){var s=document.querySelector("#wechatKeyStatus"),q=document.querySelector("#wechatAccountSelect"),b=document.querySelector("#acquireWechatKey");try{var d=await api("/api/wechat-key/status?_="+Date.now());q.innerHTML="";(d.accounts||[]).forEach(function(a){var o=document.createElement("option");o.value=a.id;o.textContent=a.wxid+" — "+a.accountDir;q.appendChild(o)});var ready=d.state==="logged_in"&&d.accounts&&d.accounts.length>0;q.disabled=!ready;b.disabled=!ready;s.textContent=d.state==="not_running"||d.state==="not_logged_in"?"请先启动并登录电脑版微信":ready?"已检测到登录账号，可直接进行只读内存扫描":"微信状态检测失败"}catch(err){q.disabled=true;b.disabled=true;s.textContent=err.message}}
async function acquireWechatKey(){var b=document.querySelector("#acquireWechatKey"),q=document.querySelector("#wechatAccountSelect"),s=document.querySelector("#wechatKeyStatus");if(!q.value)return notify("请选择微信账号","error");b.disabled=true;s.textContent="正在只读扫描微信内存并验证数据库，请稍候";try{var d=await api("/api/wechat-key/acquire",{method:"POST",body:JSON.stringify({accountId:q.value})});document.querySelector("#credentialAccountDir").value=d.accountDir||"";document.querySelector("#credentialWxid").value=d.wxid||"";document.querySelector("#credentialDatabaseKey").value=d.dbKeyHex||"";s.textContent="密钥获取成功并已通过 DPAPI 保存";notify("数据库密钥获取成功","success");await loadStatus()}catch(err){s.textContent=err.message;notify(err.message,"error")}finally{b.disabled=false}}
async function saveCredentials(){var b=document.querySelector("#saveCredentials");b.disabled=true;var pl={accountDir:document.querySelector("#credentialAccountDir").value,wxid:document.querySelector("#credentialWxid").value,databaseKey:document.querySelector("#credentialDatabaseKey").value,imageXorKey:document.querySelector("#credentialImageXorKey").value,imageAesKey:document.querySelector("#credentialImageAesKey").value};try{await api("/api/credentials",{method:"POST",body:JSON.stringify(pl)});notify("数据库凭据已加密保存，重启后生效","success");await loadCredentials()}catch(err){notify(err.message,"error")}finally{b.disabled=false}}
function listEntries(value){return String(value||"").split(/[\r\n,]+/).map(function(v){return v.trim()}).filter(Boolean)}
function filterMode(){var checked=document.querySelector('input[name="inboundFilterMode"]:checked');return checked?checked.value:"all"}
function setFilterMode(mode){var target=document.querySelector('input[name="inboundFilterMode"][value="'+mode+'"]');if(target)target.checked=true;updateFilterPicker()}
function updateFilterPicker(){var disabled=filterMode()==="all";document.querySelector("#conversationPicker").classList.toggle("filter-disabled",disabled);document.querySelector("#inboundManualEntries").disabled=disabled;if(conversationOptions.length)renderConversationOptions()}
function conversationSortMode(){return filterMode()==="allow"?"selected-first":filterMode()==="block"?"unselected-first":"stable"}
function sortConversationOptions(items){var mode=conversationSortMode();return items.slice().sort(function(a,b){if(mode!=="stable"){var as=selectedConversationIds.has(a.id)?1:0,bs=selectedConversationIds.has(b.id)?1:0;if(mode==="unselected-first"){as=1-as;bs=1-bs}if(as!==bs)return bs-as}var an=String(a.name||a.id).toLocaleLowerCase(),bn=String(b.name||b.id).toLocaleLowerCase();return an===bn?String(a.id).localeCompare(String(b.id)):an.localeCompare(bn)})}
function toggleConversation(id){if(selectedConversationIds.has(id))selectedConversationIds.delete(id);else selectedConversationIds.add(id);renderConversationOptions()}
function selectedConversationEntries(){return sortConversationOptions(conversationOptions).filter(function(item){return selectedConversationIds.has(item.id)}).map(function(item){return item.id})}
function renderConversationOptions(){var list=document.querySelector("#inboundConversationList"),query=document.querySelector("#conversationSearch").value.trim().toLocaleLowerCase();list.innerHTML="";var items=sortConversationOptions(conversationOptions);for(var i=0;i<items.length;i++){var item=items[i],hay=(item.name+" "+item.id).toLocaleLowerCase();if(query&&hay.indexOf(query)<0&&!selectedConversationIds.has(item.id))continue;var selected=selectedConversationIds.has(item.id),option=document.createElement("button");option.type="button";option.className="conversation-option"+(selected?" selected":"");option.dataset.id=item.id;option.setAttribute("role","option");option.setAttribute("aria-selected",selected?"true":"false");option.setAttribute("aria-pressed",selected?"true":"false");option.innerHTML='<span class="checkmark">✓</span><span class="conversation-label"></span><span class="conversation-id"></span>';option.querySelector(".conversation-label").textContent=(item.type==="group"?"[群聊] ":item.type==="private"?"[私聊] ":item.type==="system"?"[系统] ":"")+item.name;option.querySelector(".conversation-id").textContent=item.id;option.addEventListener("click",function(){toggleConversation(this.dataset.id)});list.appendChild(option)}}
async function loadConversations(entries){var d=await api("/api/conversations?_="+Date.now());conversationOptions=d.conversations||[];var pending=new Set(entries),manual=[];selectedConversationIds.clear();for(var i=0;i<conversationOptions.length;i++){var item=conversationOptions[i];if(pending.has(item.id)||pending.has(item.name)){selectedConversationIds.add(item.id);pending.delete(item.id);pending.delete(item.name)}}pending.forEach(function(value){manual.push(value)});document.querySelector("#inboundManualEntries").value=manual.join("\n");renderConversationOptions()}
async function load(){var d=await api("/api/config?_="+Date.now());configured=d;var allow=listEntries(d.values.LOCAL_INBOUND_ALLOWLIST),block=listEntries(d.values.LOCAL_INBOUND_BLOCKLIST),mode=allow.length?"allow":block.length?"block":"all",active=mode==="allow"?allow:mode==="block"?block:[];var ks=Object.keys(d.values);for(var i=0;i<ks.length;i++){var key=ks[i],val=d.values[key];var el=form.elements[key];if(!el||key==="LOCAL_INBOUND_ALLOWLIST"||key==="LOCAL_INBOUND_BLOCKLIST")continue;if(d.secrets.indexOf(key)>=0)continue;if(key==="FILE_MAX_BYTES"){var mb=Number(val||0)/1048576;el.value=Number.isFinite(mb)?String(Math.round(mb*1000)/1000):"200"}else{el.value=val||""}}setFilterMode(mode);secretState("WEBUI_PASSWORD","#passwordHint","本机访问可留空");updateWarnings();await Promise.all([loadStatus(),loadCredentials(),loadWechatKeyStatus(),loadConversations(active)])}
async function loadStatus(){try{var s=await api("/api/status");var dot=document.querySelector("#statusDot");var cd=document.querySelector("#statusCardDot");dot.className="dot "+s.state;if(cd)cd.className="dot "+s.state;document.querySelector("#statusText").textContent=s.label;var cl=document.querySelector("#statusCardLabel");if(cl)cl.textContent=s.label;var cdl=document.querySelector("#statusCardDetail");if(cdl)cdl.textContent=s.detail||"WeMai 运行状态";document.querySelector("#runtimeLine").textContent=s.detail||"WeMai 运行状态"}catch(e){document.querySelector("#statusText").textContent="状态不可用"}}
async function loadLogs(){var le=document.getElementById("logLines");var l=le?le.value:200;try{var d=await api("/api/logs?lines="+l+"&_="+Date.now());var lg=d.log||{};var info=document.getElementById("logInfo");if(lg.exists){info.textContent="最近 "+lg.shown+" 行 · "+fmtSize(lg.size);document.getElementById("logFileInfo").textContent="("+fmtSize(lg.size)+")"}else{info.textContent="日志文件不存在";document.getElementById("logFileInfo").textContent="(文件不存在)"}allLogLines=lg.lines||[];renderLogs(allLogLines)}catch(err){document.getElementById("logOutput").innerHTML='<div class="log-empty">加载日志失败: '+err.message+"</div>"}}
var curFilter="ALL";function filterLogs(){curFilter=document.getElementById("logLevelFilter").value;renderLogs(allLogLines)}
function renderLogs(lines){var out=document.getElementById("logOutput");if(!lines||lines.length===0){out.innerHTML='<div class="log-empty">暂无日志</div>';return}var h="";var fl=curFilter.toUpperCase();var lm={DEBUG:"debug",INFO:"info",WARNING:"warning",ERROR:"error",CRITICAL:"critical"};var sn=0;for(var i=0;i<lines.length;i++){var ln=lines[i];var lv="info";for(var l in lm){if(ln.indexOf(" - "+l+" - ")>=0){lv=lm[l];break}}if(fl!=="ALL"&&lv.toUpperCase()!==fl)continue;sn++;h+='<div class="line '+lv+'">'+escHtml(ln)+"</div>"}if(sn===0){h='<div class="log-empty">没有匹配的日志条目</div>'}out.innerHTML=h;scrollLogToBottom()}
function scrollLogToBottom(){var o=document.getElementById("logOutput");o.scrollTop=o.scrollHeight}
function toggleAutoRefresh(){var c=document.getElementById("autoRefresh");if(c.checked)startAutoRefresh();else stopAutoRefresh()}
function startAutoRefresh(){stopAutoRefresh();autoRefreshTimer=setInterval(function(){var le=document.getElementById("logLines");var l=le?le.value:200;api("/api/logs?lines="+l+"&_="+Date.now()).then(function(d){var lg=d.log||{};if(lg.exists){document.getElementById("logInfo").textContent="最近 "+lg.shown+" 行 · "+fmtSize(lg.size)}allLogLines=lg.lines||[];renderLogs(allLogLines)}).catch(function(){})},3000)}
function stopAutoRefresh(){if(autoRefreshTimer){clearInterval(autoRefreshTimer);autoRefreshTimer=null}}
function escHtml(t){var d=document.createElement("div");d.appendChild(document.createTextNode(t));return d.innerHTML}
function fmtSize(b){if(b<1024)return b+" B";if(b<1048576)return(b/1024).toFixed(1)+" KB";return(b/1048576).toFixed(1)+" MB"}
form.addEventListener("change",updateWarnings);form.elements.WEBUI_HOST.addEventListener("input",updateWarnings);document.querySelector("#inboundFilterMode").addEventListener("change",updateFilterPicker);document.querySelector("#conversationSearch").addEventListener("input",renderConversationOptions);
form.addEventListener("submit",async function(e){e.preventDefault();var b=document.querySelector("#save");b.disabled=true;var pl={};var ks=Object.keys(configured.values);for(var i=0;i<ks.length;i++){var key=ks[i],el=form.elements[key];if(!el||key==="LOCAL_INBOUND_ALLOWLIST"||key==="LOCAL_INBOUND_BLOCKLIST")continue;if(key==="FILE_MAX_BYTES"){var mb=Number(el.value);if(!Number.isFinite(mb)||mb<=0||mb>200){notify("普通文件上限必须大于 0 且不超过 200 MB","error");b.disabled=false;return}pl[key]=String(Math.max(1,Math.round(mb*1048576)))}else{pl[key]=el.value}}var entries=selectedConversationEntries();entries.push.apply(entries,listEntries(document.querySelector("#inboundManualEntries").value));entries=Array.from(new Set(entries));var mode=filterMode();pl.LOCAL_INBOUND_ALLOWLIST=mode==="allow"?entries.join("\n"):"";pl.LOCAL_INBOUND_BLOCKLIST=mode==="block"?entries.join("\n"):"";try{await api("/api/save",{method:"POST",body:JSON.stringify(pl)});notify("配置已保存，重启后生效","success");form.elements.WEBUI_PASSWORD.value="";await load()}catch(err){notify(err.message,"error")}finally{b.disabled=false}});
async function control(action,question,message){if(question&&!confirm(question))return;var b=document.querySelector("#"+action);b.disabled=true;try{await api("/api/"+action,{method:"POST",body:"{}"});notify(message,"success");setTimeout(loadStatus,900)}catch(err){notify(err.message,"error")}finally{setTimeout(function(){b.disabled=false},900)}}
document.querySelector("#start").addEventListener("click",function(){control("start","立即启动 WeMai?","正在启动 WeMai")});
document.querySelector("#stop").addEventListener("click",function(){control("stop","立即停止 WeMai?发送任务会先安全结束，并关闭独立窗口。","正在安全停止 WeMai")});
document.querySelector("#restart").addEventListener("click",function(){control("restart","立即重启 WeMai?当前发送任务会先安全停止。","正在重启 WeMai，页面稍后会重新连接")});
document.querySelector("#shutdown").addEventListener("click",function(){control("shutdown","完全退出 WeMai?核心服务、WebUI 和监督器都会关闭；再次使用需要重新双击 Start-WeMai.vbs。","正在完全退出 WeMai，页面断开属于正常现象")});
document.querySelector("#saveCredentials").addEventListener("click",saveCredentials);
document.querySelector("#acquireWechatKey").addEventListener("click",acquireWechatKey);
load().catch(function(err){notify(err.message,"error")});setInterval(loadStatus,3000);
</script>
</body></html>
"""







LOGIN_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="icon" type="image/png" href="/static/favicon.png">
<title>WeMai · 登录</title>
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
html{scroll-behavior:smooth}
body{margin:0;color:#1a3040;font:14px/1.6 -apple-system,"Segoe UI","Microsoft YaHei","PingFang SC",sans-serif;display:grid;place-items:center;min-height:100vh;background:#f0f6fc;background-image:radial-gradient(ellipse 80% 50% at 50% -20%,rgba(56,183,216,.08) 0%,transparent 70%),radial-gradient(ellipse 60% 40% at 20% 90%,rgba(22,139,195,.06) 0%,transparent 60%);background-attachment:fixed}
.login-box{width:min(380px,calc(100% - 32px));background:rgba(255,255,255,.75);backdrop-filter:blur(16px) saturate(1.5);border:1px solid rgba(255,255,255,.85);border-radius:16px;padding:34px 30px 30px;box-shadow:0 2px 8px rgba(0,0,0,.03),0 8px 32px rgba(22,139,195,.08);animation:fade-up .45s ease-out;text-align:center}
@keyframes fade-up{from{opacity:0;transform:translateY(14px)}to{opacity:1;transform:translateY(0)}}
.login-logo{width:56px;height:56px;border-radius:14px;margin:0 auto 16px;overflow:hidden;background:linear-gradient(135deg,#08658f,#168bc3);display:grid;place-items:center;box-shadow:0 3px 10px rgba(22,139,195,.25)}
.login-logo img{width:40px;height:40px;border-radius:8px;object-fit:cover}
h1{font-size:20px;font-weight:700;margin:0 0 4px;color:#0e2e40;letter-spacing:-.3px}
.subtitle{font-size:13px;color:#608094;margin-bottom:26px}
label{display:block;font-weight:600;font-size:13px;margin-bottom:7px;color:#1d4b63;text-align:left}
input{width:100%;height:44px;border:1.5px solid rgba(154,201,226,.4);border-radius:10px;background:rgba(255,255,255,.85);padding:0 14px;color:#1a3040;font-size:14px;font-family:inherit;outline:none;transition:border-color .2s,box-shadow .2s,background .2s}
input:focus{border-color:#168bc3;background:#fff;box-shadow:0 0 0 3px rgba(22,139,195,.1)}
button{width:100%;height:44px;margin-top:20px;cursor:pointer;border:1.5px solid #168bc3;border-radius:10px;background:linear-gradient(135deg,#08658f,#168bc3);box-shadow:0 2px 8px rgba(22,139,195,.2);color:#fff;font:inherit;font-weight:650;font-size:14px;transition:all .18s}
button:hover{background:linear-gradient(135deg,#075379,#1076a8);box-shadow:0 4px 14px rgba(22,139,195,.3)}
button:active{transform:scale(.97)}
.error{color:#c0394a;font-size:13px;min-height:20px;margin-top:12px}
.divider{height:1px;background:rgba(154,201,226,.2);margin:18px 0}
@media(max-width:480px){.login-box{padding:26px 22px 24px}input,button{font-size:16px}}
</style>
</head>
<body>
<form class="login-box" method="post" action="/login">
<div class="login-logo"><img src="/static/favicon.png" alt="WeMai"></div>
<h1>WeMai</h1>
<p class="subtitle">微信消息转发服务</p>
<div class="divider"></div>
<label for="password">访问密码</label>
<input id="password" name="password" type="password" autofocus autocomplete="current-password">
<button type="submit">登录</button>
<div class="error">{{ERROR}}</div>
</form>
</body>
</html>"""


class ConfigWebUIServer:
    def __init__(
        self,
        env_path: os.PathLike[str] | str,
        host: str,
        port: int,
        password: str = "",
        status_provider: Callable[[], dict] | None = None,
        start_callback: Callable[[], None] | None = None,
        stop_callback: Callable[[], None] | None = None,
        restart_callback: Callable[[], None] | None = None,
        shutdown_callback: Callable[[], None] | None = None,
        credentials_path: os.PathLike[str] | str | None = None,
        id_map_path: os.PathLike[str] | str | None = None,
        local_inbound_node_path: os.PathLike[str] | str | None = None,
        conversation_helper_path: os.PathLike[str] | str | None = None,
        wechat_key_bootstrap=None,
    ):
        self.env_path = Path(env_path)
        self.host = host
        self.port = int(port)
        self.password = password
        self.status_provider = status_provider or (lambda: {
            "state": "stopped", "label": "仅配置模式", "detail": "WeMai 尚未启动"
        })
        self.start_callback = start_callback
        self.stop_callback = stop_callback
        self.restart_callback = restart_callback
        self.shutdown_callback = shutdown_callback
        self.credentials_path = Path(credentials_path or (
            Path(__file__).resolve().parent / "data" / "local_inbound_credentials.json"
        ))
        self.id_map_path = Path(id_map_path or (
            Path(__file__).resolve().parent / "wemai_id_map.sqlite3"
        ))
        self.local_inbound_node_path = Path(local_inbound_node_path or (
            Path(__file__).resolve().parent / "local_inbound" / "runtime" / "node.exe"
        ))
        self.conversation_helper_path = Path(conversation_helper_path or (
            Path(__file__).resolve().parent / "local_inbound" / "list-conversations.js"
        ))
        self.wechat_key_bootstrap = wechat_key_bootstrap
        self._wechat_key_lock = threading.Lock()
        self._sessions: set[str] = set()
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._conversation_cache: list[dict[str, object]] = []
        self._conversation_cache_expires = 0.0
        self._conversation_cache_lock = threading.Lock()

    @property
    def url(self) -> str:
        display_host = "127.0.0.1" if self.host in {"0.0.0.0", "::"} else self.host
        return f"http://{display_host}:{self.port}/"

    def conversation_options(self) -> list[dict[str, object]]:
        """短暂缓存通讯录，避免 WebUI 搜索每次按键都重新解密数据库。"""

        now = time.monotonic()
        with self._conversation_cache_lock:
            if self._conversation_cache_expires > now:
                return list(self._conversation_cache)
        options = load_conversation_options(
            self.id_map_path,
            credentials_path=self.credentials_path,
            node_path=self.local_inbound_node_path,
            helper_path=self.conversation_helper_path,
        )
        with self._conversation_cache_lock:
            self._conversation_cache = list(options)
            self._conversation_cache_expires = time.monotonic() + 10.0
        return options

    def invalidate_conversation_cache(self) -> None:
        with self._conversation_cache_lock:
            self._conversation_cache_expires = 0.0

    def _make_handler(self):
        owner = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "WeMaiWebUI/1.0"

            def log_message(self, _format, *_args):
                return

            def _send(self, status: int, body: bytes, content_type: str, headers=None):
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("Pragma", "no-cache")
                self.send_header("Expires", "0")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("X-Frame-Options", "DENY")
                self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; frame-ancestors 'none'")
                for key, value in (headers or {}).items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(body)

            def _serve_favicon(self):
                favicon = Path(__file__).resolve().parent / "static" / "favicon.png"
                if not favicon.is_file():
                    self._json(HTTPStatus.NOT_FOUND, {"error": "图标不存在"})
                    return
                self._send(HTTPStatus.OK, favicon.read_bytes(), "image/png")

            @staticmethod
            def _tail_lines(path: Path, limit: int) -> tuple[list[str], int]:
                """从文件末尾读取限定行数，避免日志刷新扫描整个文件。"""
                size = path.stat().st_size
                if size == 0:
                    return [], 0
                chunks: list[bytes] = []
                remaining = size
                newline_count = 0
                with path.open("rb") as stream:
                    while remaining > 0 and newline_count <= limit:
                        block_size = min(64 * 1024, remaining)
                        remaining -= block_size
                        stream.seek(remaining)
                        block = stream.read(block_size)
                        chunks.append(block)
                        newline_count += block.count(b"\n")
                data = b"".join(reversed(chunks))
                lines = data.decode("utf-8", "replace").splitlines()
                return lines[-limit:], size

            def _handle_logs(self):
                params = parse_qs(urlsplit(self.path).query)
                lines_param = params.get("lines", ["200"])[0]
                try:
                    lines_count = max(1, min(1000, int(lines_param)))
                except ValueError:
                    lines_count = 200
                log_path = Path(__file__).resolve().parent / "wemai.log"
                if not log_path.is_file():
                    log = {"lines": [], "size": 0, "exists": False, "shown": 0}
                else:
                    lines, size = self._tail_lines(log_path, lines_count)
                    log = {
                        "lines": lines,
                        "size": size,
                        "exists": True,
                        "shown": len(lines),
                    }
                self._json(HTTPStatus.OK, {"log": log})

            def _json(self, status: int, value: object):
                body = json.dumps(value, ensure_ascii=False).encode("utf-8")
                self._send(status, body, "application/json; charset=utf-8")

            def _session(self) -> str:
                cookie = SimpleCookie(self.headers.get("Cookie", ""))
                morsel = cookie.get("wemai_session")
                return morsel.value if morsel else ""

            def _authenticated(self) -> bool:
                return not owner.password or self._session() in owner._sessions

            def _require_auth(self) -> bool:
                if self._authenticated():
                    return True
                if self.path.startswith("/api/"):
                    self._json(HTTPStatus.UNAUTHORIZED, {"error": "请先登录"})
                else:
                    body = LOGIN_HTML.replace("{{ERROR}}", "").encode("utf-8")
                    self._send(HTTPStatus.UNAUTHORIZED, body, "text/html; charset=utf-8")
                return False

            def _read_json(self):
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                except ValueError as exc:
                    raise ValueError("请求长度无效") from exc
                if length > 65536:
                    raise ValueError("请求内容过大")
                return json.loads(self.rfile.read(length) or b"{}")

            def _require_same_origin(self) -> bool:
                """拒绝其他网页向无密码本机 WebUI 静默提交控制请求。"""
                origin = self.headers.get("Origin", "").strip()
                if not origin:
                    return True
                parsed = urlsplit(origin)
                expected_host = self.headers.get("Host", "").casefold()
                if (
                    parsed.scheme.casefold() in {"http", "https"}
                    and parsed.netloc.casefold() == expected_host
                ):
                    return True
                self._json(HTTPStatus.FORBIDDEN, {"error": "拒绝跨站控制请求"})
                return False

            def do_GET(self):
                path = urlsplit(self.path).path
                if path == "/login":
                    body = LOGIN_HTML.replace("{{ERROR}}", "").encode("utf-8")
                    self._send(HTTPStatus.OK, body, "text/html; charset=utf-8")
                    return
                if path in {"/favicon.ico", "/static/favicon.png"}:
                    self._serve_favicon()
                    return
                if not self._require_auth():
                    return
                if path == "/":
                    self._send(HTTPStatus.OK, HTML.encode("utf-8"), "text/html; charset=utf-8")
                elif path == "/api/config":
                    values = read_env_file(owner.env_path)
                    public = {
                        key: "" if key in SECRET_KEYS else values[key]
                        for key in EDITABLE_KEYS
                    }
                    configured = [key for key in SECRET_KEYS if values.get(key)]
                    self._json(HTTPStatus.OK, {
                        "values": public,
                        "secrets": sorted(SECRET_KEYS),
                        "configuredSecrets": sorted(configured),
                    })
                elif path == "/api/credentials":
                    try:
                        credentials = load_credentials(owner.credentials_path)
                        payload = {
                            "configured": True,
                            "accountDir": credentials["account_dir"],
                            "wxid": credentials["wxid"],
                            "databaseKey": credentials["db_key"],
                            "databaseKeyConfigured": bool(credentials["db_key"]),
                            "imageXorKeyConfigured": bool(credentials["image_xor_key"]),
                            "imageAesKeyConfigured": bool(credentials["image_aes_key"]),
                        }
                    except (CredentialError, OSError):
                        payload = {
                            "configured": False,
                            "accountDir": "",
                            "wxid": "",
                            "databaseKey": "",
                            "databaseKeyConfigured": False,
                            "imageXorKeyConfigured": False,
                            "imageAesKeyConfigured": False,
                        }
                    self._json(HTTPStatus.OK, payload)
                elif path == "/api/status":
                    self._json(HTTPStatus.OK, owner.status_provider())
                elif path == "/api/wechat-key/status":
                    if owner.wechat_key_bootstrap is None:
                        raise ValueError("自动获取密钥助手不可用，请使用手工配置")
                    self._json(
                        HTTPStatus.OK,
                        owner.wechat_key_bootstrap.status().to_public_dict(),
                    )
                elif path == "/api/conversations":
                    self._json(HTTPStatus.OK, {
                        "conversations": owner.conversation_options(),
                    })
                elif path == "/health":
                    self._json(HTTPStatus.OK, {"ok": True})
                elif path == "/api/logs":
                    self._handle_logs()
                else:
                    self._json(HTTPStatus.NOT_FOUND, {"error": "页面不存在"})

            def do_POST(self):
                path = urlsplit(self.path).path
                if path == "/login":
                    length = min(int(self.headers.get("Content-Length", "0")), 4096)
                    form = parse_qs(self.rfile.read(length).decode("utf-8", "replace"))
                    supplied = form.get("password", [""])[0]
                    if owner.password and hmac.compare_digest(supplied, owner.password):
                        session = secrets.token_urlsafe(32)
                        owner._sessions.add(session)
                        self.send_response(HTTPStatus.SEE_OTHER)
                        self.send_header("Location", "/")
                        self.send_header("Set-Cookie", f"wemai_session={session}; HttpOnly; SameSite=Strict; Path=/")
                        self.end_headers()
                    else:
                        body = LOGIN_HTML.replace("{{ERROR}}", "密码不正确").encode("utf-8")
                        self._send(HTTPStatus.UNAUTHORIZED, body, "text/html; charset=utf-8")
                    return
                if not self._require_auth():
                    return
                if not self._require_same_origin():
                    return
                try:
                    if path == "/api/save":
                        current = read_env_file(owner.env_path)
                        supplied = self._read_json()
                        # 密码或 Token 留空表示保留原值，防止页面读取秘密。
                        for key in SECRET_KEYS:
                            if supplied.get(key, None) == "":
                                supplied.pop(key, None)
                        updates = validate_updates(supplied, current)
                        update_env_file(owner.env_path, updates)
                        self._json(HTTPStatus.OK, {"ok": True, "restartRequired": True})
                    elif path == "/api/credentials":
                        supplied = self._read_json()
                        if not isinstance(supplied, dict):
                            raise ValueError("凭据数据必须是对象")
                        existing = {}
                        with contextlib.suppress(CredentialError, OSError):
                            existing = load_credentials(owner.credentials_path)

                        def value(name, existing_name):
                            current_value = str(supplied.get(name) or "").strip()
                            return current_value or str(existing.get(existing_name) or "")

                        save_credentials(
                            owner.credentials_path,
                            account_dir=value("accountDir", "account_dir"),
                            wxid=value("wxid", "wxid"),
                            db_key=value("databaseKey", "db_key"),
                            image_xor_key=value("imageXorKey", "image_xor_key"),
                            image_aes_key=value("imageAesKey", "image_aes_key"),
                        )
                        # 立即回读校验 DPAPI 密文和路径，但不返回任何密钥。
                        verified = load_credentials(owner.credentials_path)
                        owner.invalidate_conversation_cache()
                        self._json(HTTPStatus.OK, {
                            "ok": True,
                            "restartRequired": True,
                            "wxid": verified["wxid"],
                            "accountDir": verified["account_dir"],
                        })
                    elif path == "/api/wechat-key/acquire":
                        if owner.wechat_key_bootstrap is None:
                            raise ValueError("自动获取密钥助手不可用，请使用手工配置")
                        supplied = self._read_json()
                        account_id = str(supplied.get("accountId") or "").strip()
                        if not account_id:
                            raise ValueError("请选择微信账号")
                        if not owner._wechat_key_lock.acquire(blocking=False):
                            self._json(
                                HTTPStatus.CONFLICT,
                                {"error": "正在获取密钥，请勿重复操作"},
                            )
                            return
                        try:
                            result = owner.wechat_key_bootstrap.acquire_and_save(account_id)
                        finally:
                            owner._wechat_key_lock.release()
                        owner.invalidate_conversation_cache()
                        self._json(HTTPStatus.OK, result)
                    elif path in {"/api/start", "/api/stop", "/api/restart", "/api/shutdown"}:
                        callbacks = {
                            "/api/start": owner.start_callback,
                            "/api/stop": owner.stop_callback,
                            "/api/restart": owner.restart_callback,
                            "/api/shutdown": owner.shutdown_callback,
                        }
                        callback = callbacks[path]
                        if callback is None:
                            raise ValueError("当前模式不支持该控制操作")
                        self._json(HTTPStatus.ACCEPTED, {"ok": True})
                        callback()
                    else:
                        self._json(HTTPStatus.NOT_FOUND, {"error": "接口不存在"})
                except (CredentialError, WechatKeyError, ValueError, json.JSONDecodeError) as exc:
                    self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                except Exception:
                    self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "服务器内部错误"})

        return Handler

    def start(self) -> None:
        if self._httpd is not None:
            return
        self._httpd = ThreadingHTTPServer((self.host, self.port), self._make_handler())
        self._httpd.daemon_threads = True
        self._thread = threading.Thread(
            target=self._httpd.serve_forever,
            name="wemai-webui",
            daemon=True,
        )
        self._thread.start()

    def close(self) -> None:
        httpd, self._httpd = self._httpd, None
        if httpd is not None:
            httpd.shutdown()
            httpd.server_close()
        if self._thread is not None:
            self._thread.join(timeout=3)
            self._thread = None
        self._sessions.clear()
