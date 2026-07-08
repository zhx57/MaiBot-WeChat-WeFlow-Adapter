"""WeChat WeFlow 桥接核心：SSE 入站流水线、消息缓冲合并与图片编排。

对应 spec「桥接核心」：将 ``WeFlowSSEClient`` 推送的事件经历史 / rawid / 自回复 /
内容 / 名单 / 正则多级过滤后，按 ``group_reply_mode`` 缓冲合并，构造 MaiBot
MessageDict 并经 ``ctx.gateway.route_message`` 注入 Host。图片消息在缓冲前完成
下载与视觉描述，把 image Seg 注入 ``data["_injected_raw_message"]`` 以便合并
阶段保留。

可变运行时状态（``pending_buffers``、``start_timestamp``、去重缓存等）放在
``RuntimeState`` 上，通过 ``state.lock`` 保护多步操作。
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

import aiohttp

from .codecs.inbound import build_message_dict, clean_group_name
from .constants import (
    DEFAULT_CONTENT_DEDUPE_MAXSIZE,
    DEFAULT_RAWID_DEDUPE_TTL_SEC,
    GATEWAY_NAME,
    PLATFORM,
    PROTOCOL,
    SCOPE,
)
from .filters import ChatFilter, ContentDeduper, RawIdDeduper, should_ignore
from .id_mapping import ContactMapper
from .image_caption import (
    caption_image,
    download_wechat_image,
    image_to_base64,
    sha256_of_file,
)
from .senders import BaseSender, create_sender
from .state import RuntimeState
from .transport import WeFlowSSEClient

if TYPE_CHECKING:
    from .config import WeChatWeFlowConfig


class WeChatBridge:
    """WeChat WeFlow 桥接核心。

    持有 SSE 客户端、过滤链与运行时状态，把 WeFlow 推送的事件转换为 MaiBot 入站
    消息。状态聚合（``pending_buffers``、去重缓存等）放在 ``RuntimeState`` 上，
    通过 ``state.lock`` 保护多步操作。
    """

    def __init__(
        self,
        config: "WeChatWeFlowConfig",
        ctx: Any,
        contact_mapper: ContactMapper,
        logger: logging.Logger,
        data_dir: Path,
    ) -> None:
        self._config = config
        self._ctx = ctx
        self._logger = logger
        self._data_dir = data_dir

        # 去重与过滤链
        content_deduper = ContentDeduper(
            ttl=config.filters.content_dedupe_ttl_sec,
            maxsize=DEFAULT_CONTENT_DEDUPE_MAXSIZE,
        )
        rawid_deduper = RawIdDeduper(ttl=DEFAULT_RAWID_DEDUPE_TTL_SEC)
        self._chat_filter = ChatFilter(
            group_list_type=config.chat.group_list_type,
            group_list=config.chat.group_list,
            private_list_type=config.chat.private_list_type,
            private_list=config.chat.private_list,
            ban_user_id=config.chat.ban_user_id,
            regex_enabled=config.filters.regex_filter_enabled,
            regex_mode=config.filters.regex_filter_mode,
            regex_patterns=config.filters.regex_filter_patterns,
        )

        # 出站发送器（随运行时状态聚合，供插件出站路径使用）
        sender: BaseSender = create_sender(config, logger)

        # 运行时状态聚合（pending_buffers / start_timestamp 等放在 state 上）
        self._state = RuntimeState(
            contact_mapper=contact_mapper,
            content_deduper=content_deduper,
            rawid_deduper=rawid_deduper,
            sender=sender,
        )

        # SSE 传输层
        self._transport = WeFlowSSEClient(
            base_url=config.weflow.base_url,
            access_token=config.weflow.access_token,
            request_timeout=config.weflow.request_timeout,
            reconnect_delay_sec=config.bridge.reconnect_delay_sec,
            on_message=self._on_message,
            on_connected=self._on_connected,
            on_disconnected=self._on_disconnected,
            logger=logger,
        )

        # 图片下载/描述用的 aiohttp session（惰性创建）
        self._aiohttp_session: Optional[aiohttp.ClientSession] = None
        self._attachments_dir = data_dir / config.image_caption.attachments_dir

        self._stopped = True

    # ================================================================
    # 生命周期
    # ================================================================

    async def start(self) -> None:
        """启动桥接：标记运行、记录启动时间戳、启动 SSE。"""

        self._stopped = False
        self._state.start_timestamp = time.time()
        await self._transport.start()

    async def stop(self) -> None:
        """停止桥接：停 SSE、取消缓冲定时器、关 session、清去重缓存。"""

        self._stopped = True
        await self._transport.stop()

        # 取消所有 pending buffer 定时器并等待其退出
        pending_timers: list[asyncio.Task[None]] = []
        for entry in self._state.pending_buffers.values():
            timer = entry.get("timer")
            if timer is not None and not timer.done():
                timer.cancel()
                pending_timers.append(timer)
        self._state.pending_buffers.clear()
        for timer in pending_timers:
            try:
                await timer
            except (asyncio.CancelledError, Exception):
                pass

        # 关闭 aiohttp session
        if self._aiohttp_session is not None and not self._aiohttp_session.closed:
            try:
                await self._aiohttp_session.close()
            except Exception:
                pass
        self._aiohttp_session = None

        # 清空去重缓存
        self._state.content_deduper.clear()
        self._state.rawid_deduper.clear()

    # ================================================================
    # 入站消息流水线
    # ================================================================

    async def _on_message(self, data: dict[str, Any]) -> None:
        """入站消息处理流水线，任一过滤命中即 return 丢弃。"""

        # 0. 事件类型过滤：仅处理 message.new，丢弃 message.revoke 等
        event = data.get("event", "") or ""
        if event and event != "message.new":
            self._logger.debug("丢弃非 message.new 事件：%s", event)
            return

        # 1. 历史消息过滤：丢弃启动前的消息
        if self._config.bridge.history_filter_enabled:
            try:
                ts = float(data.get("timestamp", 0) or 0)
            except (TypeError, ValueError):
                ts = 0.0
            if ts < self._state.start_timestamp:
                return

        # 2. rawid 去重
        rawid = str(data.get("rawid", "") or "")
        if self._state.rawid_deduper.is_duplicate(rawid):
            return

        # 3. 自回复/语音/表情/空内容过滤
        if should_ignore(data, self._config.bot.nicknames, self._config.bot.wxid):
            return

        # 4. 内容去重
        contact_key = self._compute_contact_key(data)
        content = data.get("content", "") or ""
        if self._state.content_deduper.is_duplicate(contact_key, content):
            return

        # 5. chat/正则过滤
        session_id = data.get("sessionId", "") or ""
        is_group = self._is_group(data)
        if is_group:
            group_name = clean_group_name(data.get("groupName", ""))
            group_id = self._state.contact_mapper.register_contact(
                session_id, True, group_name
            )
            if not self._chat_filter.allow_group(group_id, group_name):
                return
            sender_name = self._sender_name(data)
            sender_id = (
                self._state.contact_mapper.register_group_sender(session_id, sender_name)
                if sender_name
                else ""
            )
            if not self._chat_filter.allow_user(sender_id):
                return
            if not self._chat_filter.allow_content(content):
                return
        else:
            talker_id = data.get("talkerId", "") or session_id
            nickname = data.get("sourceName") or data.get("talkerName") or talker_id
            user_id = self._state.contact_mapper.register_contact(
                talker_id, False, user_nickname=nickname
            )
            if not self._chat_filter.allow_private(user_id, nickname):
                return
            if not self._chat_filter.allow_user(user_id):
                return
            if not self._chat_filter.allow_content(content):
                return

        # 6. 图片消息分支
        if content == "[图片]":
            await self._process_image_message(data)
        else:
            self._add_to_buffer(data)

    # ================================================================
    # 缓冲机制
    # ================================================================

    def _buffer_key(self, data: dict[str, Any]) -> str:
        """计算缓冲键。

        - 群 + batch：``__batch__{group_id}``（整群合并）
        - 群 + mention/all 且有发言人：``{session_id}_{sender_id}``（按发言人合并）
        - 私聊或群聊无发言人：``{session_id}``
        """

        session_id = data.get("sessionId", "") or ""
        if not self._is_group(data):
            return session_id

        if self._config.bridge.group_reply_mode == "batch":
            group_name = clean_group_name(data.get("groupName", ""))
            group_id = self._state.contact_mapper.register_contact(
                session_id, True, group_name
            )
            return f"__batch__{group_id}"

        sender_name = self._sender_name(data)
        if sender_name:
            sender_id = self._state.contact_mapper.register_group_sender(
                session_id, sender_name
            )
            return f"{session_id}_{sender_id}"
        return session_id

    def _add_to_buffer(self, data: dict[str, Any]) -> None:
        """追加消息到缓冲并（重）启动合并定时器。"""

        key = self._buffer_key(data)
        buffers = self._state.pending_buffers
        entry = buffers.get(key)
        if entry is None:
            entry = {"messages": [], "timer": None, "timer_version": 0}
            buffers[key] = entry

        entry["messages"].append(data)

        # 取消旧定时器（若有且未完成）
        old_timer = entry["timer"]
        if old_timer is not None and not old_timer.done():
            old_timer.cancel()

        # 递增版本号，新建定时器（timer_version 机制防止旧定时器误触发）
        entry["timer_version"] += 1
        version = entry["timer_version"]
        entry["timer"] = asyncio.create_task(
            self._buffer_timer(key, version, self._config.bridge.buffer_seconds)
        )

    async def _buffer_timer(self, key: str, version: int, delay: int) -> None:
        """缓冲定时器：延时后触发合并发送。被取消则静默退出。"""

        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return

        if self._stopped:
            return

        entry = self._state.pending_buffers.get(key)
        if entry is None or entry.get("timer_version") != version:
            return

        await self._process_sender(key)

    async def _process_sender(self, key: str) -> None:
        """缓冲合并并发送：pop 出缓冲、合并文本与图片 Seg、注入 Host。"""

        try:
            async with self._state.lock:
                entry = self._state.pending_buffers.pop(key, None)
                if entry is None:
                    return
                timer = entry.get("timer")
                if timer is not None and not timer.done():
                    timer.cancel()

                messages: list[dict[str, Any]] = entry["messages"]
                if not messages:
                    return

                # 以最后一条为 base，多条则把 content 用 "\n" 连接
                # （图片消息的 content="[图片]" 保持原样，图片 Seg 由下方注入）
                merged_data: dict[str, Any] = dict(messages[-1])
                if len(messages) > 1:
                    parts = [str(m.get("content", "") or "") for m in messages]
                    merged_data["content"] = "\n".join(parts)

                # 收集图片消息注入的 Seg（保留 _process_image_message 已生成的 raw_message）
                image_segs: list[dict[str, Any]] = []
                for m in messages:
                    injected = m.get("_injected_raw_message")
                    if injected:
                        image_segs.extend(injected)

                # 构造 MessageDict
                message_dict = build_message_dict(
                    merged_data,
                    self._state.contact_mapper,
                    self._config.bot.wxid,
                    self._config.bot.nicknames,
                    self._config.bridge.group_reply_mode,
                )

                # 追加图片 Seg 到 raw_message
                # （单条图片消息时 build_message_dict 产出空 raw_message，此处等价于覆盖；
                #  多条消息含图片时追加，保留图片 Seg 与文本 Seg）
                if image_segs:
                    existing = message_dict.get("raw_message") or []
                    message_dict["raw_message"] = list(existing) + image_segs

                # 记录内容去重（存入缓存，防止 AI 回复回流）
                merged_content = merged_data.get("content", "") or ""
                contact_key = self._compute_contact_key(merged_data)
                self._state.content_deduper.is_duplicate(contact_key, merged_content)

                rawid = str(merged_data.get("rawid", "") or "")

            # 锁外执行网络注入，避免网络耗时阻塞其它缓冲的合并
            result = await self._ctx.gateway.route_message(
                gateway_name=GATEWAY_NAME,
                message=message_dict,
                route_metadata={
                    "self_id": self._config.bot.wxid,
                    "connection_id": SCOPE,
                },
                external_message_id=rawid,
                dedupe_key=rawid,
            )
            if not result:
                self._logger.debug(
                    "Host 丢弃了入站消息：key=%s rawid=%s", key, rawid or "无"
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            self._logger.exception("缓冲合并发送异常：key=%s", key)

    # ================================================================
    # 图片编排
    # ================================================================

    async def _process_image_message(self, data: dict[str, Any]) -> None:
        """下载图片并按配置生成描述/二进制 Seg，注入缓冲。

        图片处理可能耗时（下载 + 视觉描述），此处直接 await；SSE 消息顺序到达，
        顺序处理可接受。图片消息在处理完成后才入缓冲。
        """

        session = self._get_session()
        session_id = data.get("sessionId", "") or ""

        image_path = await download_wechat_image(
            session,
            self._config.weflow.base_url,
            self._config.weflow.access_token,
            session_id,
            self._attachments_dir,
        )

        injected_raw_message: list[dict[str, Any]] = []

        if image_path is not None:
            # 视觉描述（provider != none 时）
            if self._config.image_caption.provider != "none":
                caption = await caption_image(
                    session,
                    image_path,
                    self._config.image_caption.provider,
                    self._config.image_caption.model,
                    self._config.image_caption.api_key,
                    self._config.image_caption.api_base,
                    self._config.image_caption.prompt,
                    self._config.image_caption.ollama_base_url,
                    self._config.image_caption.ollama_timeout,
                )
                injected_raw_message.append(
                    {"type": "text", "data": f"[图片: {caption}]"}
                )
            # 下载图片并以 image Seg 转发给 MaiBot
            if self._config.image_caption.download_images:
                img_b64 = image_to_base64(image_path)
                img_hash = sha256_of_file(image_path)
                injected_raw_message.append(
                    {
                        "type": "image",
                        "data": "",
                        "hash": img_hash,
                        "binary_data_base64": img_b64,
                    }
                )
        else:
            # 图片下载失败：占位描述
            injected_raw_message.append(
                {"type": "text", "data": "[图片: （图片内容无法描述）]"}
            )

        data["_injected_raw_message"] = injected_raw_message
        self._add_to_buffer(data)

    # ================================================================
    # 连接状态回调
    # ================================================================

    async def _on_connected(self) -> None:
        """SSE 连接建立：上报 ready=True。"""

        try:
            await self._ctx.gateway.update_state(
                gateway_name=GATEWAY_NAME,
                ready=True,
                platform=PLATFORM,
                account_id=self._config.bot.wxid,
                scope=SCOPE,
                metadata={
                    "protocol": PROTOCOL,
                    "weflow_url": self._config.weflow.base_url,
                },
            )
        except Exception:
            self._logger.exception("上报连接就绪状态失败")
        self._logger.info("WeFlow SSE 已连接，消息网关就绪")

    async def _on_disconnected(self, reason: str) -> None:
        """SSE 连接断开：上报 ready=False。"""

        try:
            await self._ctx.gateway.update_state(
                gateway_name=GATEWAY_NAME,
                ready=False,
                platform=PLATFORM,
                account_id=self._config.bot.wxid,
                scope=SCOPE,
                metadata={
                    "protocol": PROTOCOL,
                    "weflow_url": self._config.weflow.base_url,
                },
            )
        except Exception:
            self._logger.exception("上报断开状态失败")
        self._logger.warning("WeFlow SSE 断开：%s", reason)

    # ================================================================
    # 辅助
    # ================================================================

    def _get_session(self) -> aiohttp.ClientSession:
        """惰性创建/复用 aiohttp ClientSession，用于图片下载与描述。"""

        if self._aiohttp_session is None or self._aiohttp_session.closed:
            timeout = aiohttp.ClientTimeout(total=self._config.weflow.request_timeout)
            self._aiohttp_session = aiohttp.ClientSession(timeout=timeout)
        return self._aiohttp_session

    @staticmethod
    def _is_group(data: dict[str, Any]) -> bool:
        """判断是否群消息：sessionType=='group' 或 sessionId 含 '@chatroom'。"""

        session_id = data.get("sessionId", "") or ""
        return (data.get("sessionType", "") == "group") or ("@chatroom" in session_id)

    @staticmethod
    def _sender_name(data: dict[str, Any]) -> str:
        """提取群内发言人昵称，兼容多种字段名。"""

        return (
            data.get("senderName") or data.get("sender") or data.get("sourceName") or ""
        )

    def _compute_contact_key(self, data: dict[str, Any]) -> str:
        """计算内容去重的 contact 键：群用 session_id+sender，私聊用 talker_id。"""

        session_id = data.get("sessionId", "") or ""
        if self._is_group(data):
            return f"{session_id}_{self._sender_name(data)}"
        return data.get("talkerId", "") or session_id
