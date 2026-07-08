"""运行时状态聚合，asyncio.Lock 保护并发访问。

对应 spec「运行时状态」：将桥接运行期间需要跨方法共享且需并发保护的可变状态
（``pending_buffers``、``start_timestamp``）与无状态依赖（``contact_mapper``、
内容/rawid 去重器、出站 ``sender``）聚合到一个轻量容器，由 ``bridge.py`` 持有
并通过 ``lock`` 属性保护多步操作。
"""

from __future__ import annotations

import asyncio

from .filters import ContentDeduper, RawIdDeduper
from .id_mapping import ContactMapper
from .senders import BaseSender


class RuntimeState:
    """运行时状态聚合，asyncio.Lock 保护并发访问。

    Attributes:
        contact_mapper: 双向 ID↔ContactRef 映射，入站注册 / 出站反查共用。
        content_deduper: 内容去重缓存，防止 AI 回复被 SSE 回流再次触发。
        rawid_deduper: rawid 去重集合，防止同一消息重复处理。
        sender: 出站消息发送器（UIA / WeFlow API）。
        pending_buffers: ``buffer_key`` → ``{"messages", "timer", "timer_version"}``。
        start_timestamp: 桥接启动时间戳，用于丢弃启动前的历史消息。
    """

    def __init__(
        self,
        contact_mapper: ContactMapper,
        content_deduper: ContentDeduper,
        rawid_deduper: RawIdDeduper,
        sender: BaseSender,
    ) -> None:
        self.contact_mapper = contact_mapper
        self.content_deduper = content_deduper
        self.rawid_deduper = rawid_deduper
        self.sender = sender
        # buffer_key -> {"messages": list[dict], "timer": Optional[Task], "timer_version": int}
        self.pending_buffers: dict[str, dict] = {}
        # 启动时间戳，用于过滤启动前的历史消息
        self.start_timestamp: float = 0.0
        self._lock = asyncio.Lock()

    @property
    def lock(self) -> asyncio.Lock:
        """暴露锁供 bridge 在多步操作时持有。"""
        return self._lock
