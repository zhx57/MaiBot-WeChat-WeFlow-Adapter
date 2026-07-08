"""WeFlow SSE 事件 → MaiBot MessageDict 入站编解码。

对应 spec「群聊回复模式」「自回复防护」中 MessageDict 构造部分，以及原版
``ob_protocol.make_message_event`` 的职责迁移。本模块只负责单条事件的 Seg
构造与字段映射；缓冲合并、图片 Seg 注入由 ``bridge.py`` 完成。
"""

from __future__ import annotations

import re
import time
import uuid
from typing import Any

from ..id_mapping import ContactMapper, ContactRef, stable_id  # noqa: F401

# 群名末尾的人数标记，如「测试群 (12)」
_GROUP_NAME_TAIL_RE = re.compile(r"\s*\(\d+\)\s*$")


def clean_group_name(raw: str) -> str:
    """去除群名末尾 ``(123)`` 人数标记并 strip。"""

    return _GROUP_NAME_TAIL_RE.sub("", raw or "").strip()


def detect_mention(content: str, bot_nicknames: list[str]) -> tuple[bool, str]:
    """检测 content 是否含 ``@bot_nickname``。

    返回 ``(is_mentioned, cleaned_content)``：遍历 nicknames，若 content 含
    ``f"@{nick}"`` 则移除该子串；命中任一即视为 mentioned，并对结果 strip。
    """

    cleaned = content
    mentioned = False
    for nick in bot_nicknames or []:
        if not nick:
            continue
        at_pattern = f"@{nick}"
        if at_pattern in cleaned:
            cleaned = cleaned.replace(at_pattern, "")
            mentioned = True
    if mentioned:
        cleaned = cleaned.strip()
    return mentioned, cleaned


def build_message_dict(
    data: dict[str, Any],
    contact_mapper: ContactMapper,
    bot_wxid: str,
    bot_nicknames: list[str],
    group_reply_mode: str,
) -> dict[str, Any]:
    """构造 MaiBot MessageDict。

    严格匹配 MaiBot MessageDict 契约：
    - ``timestamp`` 为字符串（``str(float(ts))``）
    - ``message_id`` 缺失时用 ``f"wechat-{uuid4().hex}"``
    - 群聊 ``message_info`` 含 ``group_info``，私聊不含
    - 图片消息（content=='[图片]'）产出空 ``raw_message``，由 bridge 注入 image Seg
    """

    content = data.get("content", "") or ""
    session_id = data.get("sessionId", "") or ""
    is_group = (data.get("sessionType", "") == "group") or ("@chatroom" in session_id)

    # ---------- 时间戳与消息 ID ----------
    # WeFlow SSE 的 timestamp 为秒级 Unix 时间戳（整数），保持字符串形式。
    ts_raw = data.get("timestamp", time.time())
    try:
        timestamp = str(int(ts_raw)) if float(ts_raw).is_integer() else str(float(ts_raw))
    except (TypeError, ValueError):
        timestamp = str(int(time.time()))

    rawid = data.get("rawid", "")
    message_id = str(rawid) if rawid else f"wechat-{uuid.uuid4().hex}"

    # ---------- 注册联系人，填充 user_info / group_info ----------
    if is_group:
        group_name = clean_group_name(data.get("groupName", ""))
        entity_id = contact_mapper.register_contact(session_id, True, group_name)
        sender_name = (
            data.get("senderName") or data.get("sender") or data.get("sourceName") or ""
        )
        # 群内发言人独立 user_id；无发言人时退化为群 entity_id
        user_id = (
            contact_mapper.register_group_sender(session_id, sender_name)
            if sender_name
            else entity_id
        )
        user_nickname = sender_name or "未知"
        group_info: dict[str, Any] | None = {
            "group_id": entity_id,
            "group_name": group_name,
        }
    else:
        talker_id = data.get("talkerId") or session_id
        user_nickname = data.get("sourceName") or data.get("talkerName") or talker_id
        user_id = contact_mapper.register_contact(
            talker_id, False, user_nickname=user_nickname
        )
        group_info = None

    # ---------- Seg 构造与 processed_plain_text ----------
    is_mentioned = False
    is_picture = False
    raw_message: list[dict[str, Any]] = []
    processed_plain_text = ""

    if content == "[图片]":
        # 图片消息：raw_message 留空，由 bridge 在缓冲阶段注入 image Seg
        is_picture = True
        processed_plain_text = ""
    elif is_group and group_reply_mode == "mention":
        is_mentioned, cleaned = detect_mention(content, bot_nicknames)
        if is_mentioned:
            # 前置 at Seg 表示 @机器人，text 段用去除 @ 后的纯文本
            raw_message.append(
                {
                    "type": "at",
                    "data": {
                        "target_user_id": bot_wxid,
                        "target_user_nickname": (
                            bot_nicknames[0] if bot_nicknames else ""
                        ),
                        "target_user_cardname": None,
                    },
                }
            )
            raw_message.append({"type": "text", "data": cleaned})
            processed_plain_text = cleaned
        else:
            raw_message.append({"type": "text", "data": content})
            processed_plain_text = content
    elif is_group and group_reply_mode == "batch":
        # 批处理：raw_message 保留原文，processed_plain_text 用预格式化串
        raw_message.append({"type": "text", "data": content})
        processed_plain_text = (
            f'成员"{sender_name}"在群"{group_name}"中对你说：{content}'
        )
    else:
        # all 模式群聊 / 私聊：is_mentioned 按实际 @ 设置（spec.md all 模式约定）
        if is_group:
            is_mentioned, _ = detect_mention(content, bot_nicknames)
        raw_message.append({"type": "text", "data": content})
        processed_plain_text = content

    is_command = processed_plain_text.startswith("/")

    # ---------- 组装 message_info ----------
    message_info: dict[str, Any] = {
        "user_info": {
            "user_id": user_id,
            "user_nickname": user_nickname,
            "user_cardname": None,
        },
    }
    if group_info is not None:
        message_info["group_info"] = group_info
    message_info["additional_config"] = {
        "self_id": bot_wxid,
        "wechat_session_type": "group" if is_group else "private",
    }

    return {
        "message_id": message_id,
        "timestamp": timestamp,
        "platform": "wechat",
        "message_info": message_info,
        "raw_message": raw_message,
        "is_mentioned": is_mentioned,
        "is_at": is_mentioned,
        "is_command": is_command,
        "is_notify": False,
        "is_emoji": False,
        "is_picture": is_picture,
        "session_id": "",
        "processed_plain_text": processed_plain_text,
        "display_message": processed_plain_text,
    }
