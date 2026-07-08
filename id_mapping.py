"""确定性 ID 生成与双向联系人映射，支持持久化。

修复原版 Akasha-WeChat 使用 ``hash()`` 受 ``PYTHONHASHSEED`` 影响导致跨进程
ID 不稳定的缺陷：改用 ``md5`` 确定性映射，并落盘 ``id_contact_map.json``
以便重启后出站反查发送目标。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional


def stable_id(seed: str) -> str:
    """确定性 ID：md5(seed) 前 16 位十六进制。

    跨进程稳定（修复原版 ``hash()`` 受 ``PYTHONHASHSEED`` 影响的缺陷）。
    """

    return hashlib.md5(seed.encode("utf-8")).hexdigest()[:16]


@dataclass
class ContactRef:
    """WeChat 联系人引用，用于出站时反查发送目标。"""

    is_group: bool
    # WeFlow 'to' 字段：群为 xxx@chatroom，私聊为 wxid
    session_id: str
    # UIA 搜索目标：群为群名，私聊为用户昵称
    display_name: str
    group_name: str = ""
    user_nickname: str = ""


@dataclass
class _ContactEntry:
    """映射表内部条目：联系人引用 + 该会话内已注册的群发言人。"""

    contact: ContactRef
    # 该会话内已注册的群发言人 sender_name -> sender_user_id
    group_senders: dict[str, str] = field(default_factory=dict)


class ContactMapper:
    """双向 ID↔ContactRef 映射，支持持久化。"""

    def __init__(self) -> None:
        self._by_id: dict[str, _ContactEntry] = {}
        # session_id -> entity_id
        self._by_session: dict[str, str] = {}

    def register_contact(
        self,
        session_id: str,
        is_group: bool,
        group_name: str = "",
        user_nickname: str = "",
    ) -> str:
        """注册会话联系人（群或私聊），返回 entity_id（group_id 或 user_id）。

        已注册则返回既有 ID 并更新名称（名称可能由空补全为实际值）。
        """

        existing_id = self._by_session.get(session_id)
        if existing_id is not None:
            entry = self._by_id[existing_id]
            if group_name:
                entry.contact.group_name = group_name
            if user_nickname:
                entry.contact.user_nickname = user_nickname
            # display_name：群为群名，私聊为用户昵称；仅在对应名称非空时更新
            if is_group and group_name:
                entry.contact.display_name = group_name
            elif not is_group and user_nickname:
                entry.contact.display_name = user_nickname
            return existing_id

        entity_id = stable_id(session_id)
        display_name = group_name if is_group else user_nickname
        contact = ContactRef(
            is_group=is_group,
            session_id=session_id,
            display_name=display_name,
            group_name=group_name,
            user_nickname=user_nickname,
        )
        self._by_id[entity_id] = _ContactEntry(contact=contact)
        self._by_session[session_id] = entity_id
        return entity_id

    def register_group_sender(self, session_id: str, sender_name: str) -> str:
        """注册群内发言人，返回 sender_user_id = stable_id(f"{session_id}_{sender_name}")。"""

        sender_user_id = stable_id(f"{session_id}_{sender_name}")
        entity_id = self._by_session.get(session_id)
        if entity_id is None:
            # 会话尚未注册，按群自动登记（名称待后续 register_contact 补全）
            entity_id = self.register_contact(session_id, is_group=True)
        self._by_id[entity_id].group_senders[sender_name] = sender_user_id
        return sender_user_id

    def resolve(self, entity_id: str) -> Optional[ContactRef]:
        """由 group_id/user_id 反查 ContactRef。"""

        entry = self._by_id.get(entity_id)
        if entry is None:
            return None
        return entry.contact

    def load(self, path: Path) -> None:
        """从 JSON 加载映射。文件不存在或损坏时静默忽略。"""

        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return

        if not isinstance(data, dict):
            return

        for entity_id, entry_data in data.items():
            if not isinstance(entry_data, dict):
                continue
            contact_data = entry_data.get("contact", {})
            if not isinstance(contact_data, dict):
                contact_data = {}
            contact = ContactRef(
                is_group=bool(contact_data.get("is_group", False)),
                session_id=str(contact_data.get("session_id", "")),
                display_name=str(contact_data.get("display_name", "")),
                group_name=str(contact_data.get("group_name", "")),
                user_nickname=str(contact_data.get("user_nickname", "")),
            )
            senders = entry_data.get("group_senders", {})
            group_senders: dict[str, str] = (
                {str(k): str(v) for k, v in senders.items()}
                if isinstance(senders, dict)
                else {}
            )
            self._by_id[entity_id] = _ContactEntry(
                contact=contact, group_senders=group_senders
            )
            if contact.session_id:
                self._by_session[contact.session_id] = entity_id

    def save(self, path: Path) -> None:
        """保存映射到 JSON（确保父目录存在）。"""

        path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            entity_id: {
                "contact": asdict(entry.contact),
                "group_senders": entry.group_senders,
            }
            for entity_id, entry in self._by_id.items()
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
