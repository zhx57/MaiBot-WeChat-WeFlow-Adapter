"""入站消息过滤与去重。

职责：
1. ``should_ignore`` —— 自回复/语音/表情/空内容过滤，对应 spec「自回复防护」第 ② 层。
2. ``ContentDeduper`` —— 内容去重（TTL 缓存），防止 AI 回复被 SSE 回流再次触发。
3. ``RawIdDeduper`` —— rawid 去重（TTL 集合），防止同一消息重复处理。
4. ``ChatFilter`` —— 白/黑名单 + ban + 正则过滤。
"""

from __future__ import annotations

import re
from typing import Any

from cachetools import TTLCache

from .constants import (
    DEFAULT_CONTENT_DEDUPE_MAXSIZE,
    DEFAULT_CONTENT_DEDUPE_TTL_SEC,
    DEFAULT_RAWID_DEDUPE_TTL_SEC,
    WECHAT_VOICE_TYPE,
)


def should_ignore(
    data: dict[str, Any],
    bot_nicknames: list[str],
    bot_wxid: str,
    ignore_self_message: bool = True,
) -> bool:
    """自回复/语音/表情/空内容过滤。返回 True 表示丢弃。

    规则（任一命中即丢弃）：
    - ``sourceName ∈ bot_nicknames``（机器人自身发送的回流消息）
    - 私聊会话对方即机器人自身（``talkerId`` 或 ``sessionId`` 等于 ``bot_wxid``）
    - ``type`` 或 ``msgType`` 等于 ``WECHAT_VOICE_TYPE``(34)（语音消息，本插件暂不处理）
    - ``content`` 含 ``[语音]`` 或 ``[表情]``
    - ``content`` 为空或纯空白

    WeFlow SSE 事件不含 ``talkerId``/``type`` 字段，私聊对方用 ``sessionId``
    兜底；语音靠 ``content`` 含 ``[语音]`` 兜底。
    """

    content = data.get("content", "") or ""
    msg_type = data.get("type", 0) or data.get("msgType", 0) or 0

    if ignore_self_message:
        # 机器人自身发送的回流消息（self-reply 防护核心）
        source_name = data.get("sourceName", "") or ""
        if source_name and source_name in bot_nicknames:
            return True

        # 会话对方即机器人自身：优先 talkerId，SSE 事件无此字段时用 sessionId 兜底
        talker_id = data.get("talkerId", "") or data.get("sessionId", "") or ""
        if bot_wxid and talker_id and talker_id == bot_wxid:
            return True

    # 语音消息（SSE 事件无 type 字段时此分支不命中，靠下方 content 兜底）
    if msg_type == WECHAT_VOICE_TYPE:
        return True

    # 表情/语音标记
    if content and ("[语音]" in content or "[表情]" in content):
        return True

    # 空内容
    if not content or not content.strip():
        return True

    return False


class ContentDeduper:
    """内容去重，防止 AI 回复被 SSE 回流再次触发。

    使用 ``cachetools.TTLCache`` 自动过期清理，修复原版 ``_sent_recently``
    字典内存泄漏缺陷（spec「自回复防护」第 ③ 层）。
    """

    def __init__(self, ttl: int = DEFAULT_CONTENT_DEDUPE_TTL_SEC,
                 maxsize: int = DEFAULT_CONTENT_DEDUPE_MAXSIZE) -> None:
        self._cache: TTLCache[str, bool] = TTLCache(maxsize=maxsize, ttl=ttl)

    def is_duplicate(self, contact: str, content: str, unique_hint: str = "") -> bool:
        """存在返回 True，否则存入并返回 False。

        ``unique_hint`` 用于图片、文件等占位内容，避免同一会话连续多张 ``[图片]``
        被误判为重复。
        """

        key = f"{contact}:{content}:{unique_hint}" if unique_hint else f"{contact}:{content}"
        if key in self._cache:
            return True
        self._cache[key] = True
        return False

    def clear(self) -> None:
        """清空去重缓存。"""

        self._cache.clear()


class RawIdDeduper:
    """rawid 去重，TTL 集合，防止同一消息重复处理。

    对应 spec「WeFlow SSE 消息接收」中的 ``rawid`` 去重。
    """

    def __init__(self, ttl: int = DEFAULT_RAWID_DEDUPE_TTL_SEC, maxsize: int = 10000) -> None:
        self._cache: TTLCache[str, bool] = TTLCache(maxsize=maxsize, ttl=ttl)

    def is_duplicate(self, rawid: str) -> bool:
        """存在返回 True，否则存入并返回 False。空 rawid 直接放行（不缓存）。"""

        if not rawid:
            return False
        if rawid in self._cache:
            return True
        self._cache[rawid] = True
        return False

    def clear(self) -> None:
        """清空 rawid 缓存。"""

        self._cache.clear()


class ChatFilter:
    """白/黑名单 + ban + 正则过滤。

    对应 spec 中 ``[chat]`` 与 ``[filters].regex_*`` 配置。
    """

    def __init__(
        self,
        group_list_type: str,
        group_list: list[str],
        private_list_type: str,
        private_list: list[str],
        ban_user_id: list[str],
        regex_enabled: bool,
        regex_mode: str,
        regex_patterns: list[str],
    ) -> None:
        self._group_list_type = group_list_type
        self._group_list = set(group_list or [])
        self._private_list_type = private_list_type
        self._private_list = set(private_list or [])
        self._ban_user_id = set(ban_user_id or [])
        self._regex_enabled = bool(regex_enabled)
        self._regex_mode = regex_mode or "blacklist"
        # 预编译正则，避免每条消息重复编译
        self._regexes: list[re.Pattern[str]] = []
        if self._regex_enabled:
            for pat in regex_patterns or []:
                try:
                    self._regexes.append(re.compile(pat))
                except re.error:
                    # 非法正则忽略，避免影响整条过滤链
                    continue

    def allow_group(self, group_id: str, group_name: str) -> bool:
        """群白/黑名单过滤。

        - whitelist：群名或群ID任一命中名单才放行
        - blacklist：群名或群ID任一命中名单则拒绝
        """

        in_list = (group_id in self._group_list) or (group_name in self._group_list)
        if self._group_list_type == "blacklist":
            return not in_list
        # whitelist
        return in_list

    def allow_private(self, user_id: str, user_nickname: str) -> bool:
        """私聊白/黑名单过滤。"""

        in_list = (user_id in self._private_list) or (user_nickname in self._private_list)
        if self._private_list_type == "blacklist":
            return not in_list
        return in_list

    def allow_user(self, user_id: str) -> bool:
        """``ban_user_id`` 中的用户一律拒绝。"""

        if not user_id:
            return True
        return user_id not in self._ban_user_id

    def allow_content(self, text: str) -> bool:
        """正则内容过滤。

        - ``regex_enabled=False``：总是放行
        - blacklist 模式：匹配任一模式则拒绝
        - whitelist 模式：匹配任一模式才放行
        """

        if not self._regex_enabled or not self._regexes:
            return True
        matched = any(rx.search(text) for rx in self._regexes)
        if self._regex_mode == "whitelist":
            return matched
        # blacklist
        return not matched
