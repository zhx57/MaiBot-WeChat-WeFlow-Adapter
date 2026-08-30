"""线程安全地关联 UI 出站消息与随后出现的同内容本地回声。"""

import re
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass

from chat_name_utils import normalize_chat_name


def _normalize_content(value):
    text = str(value or "").replace("\u2005", " ").replace("\xa0", " ")
    return re.sub(r"\s+", " ", text).strip()


def _content_matches(expected, actual):
    if not expected or not actual:
        return False
    if expected == actual:
        return True
    shorter, longer = sorted((expected, actual), key=len)
    return len(shorter) >= 12 and shorter in longer


@dataclass
class _OutgoingRecord:
    reservation_id: str
    chat_key: str
    content: str
    expires_at: float
    remaining_hits: int


class OutgoingMessageRegistry:
    """在点击发送前预登记内容，防止极快的本地回声先于登记到达。

    ``reserve`` 只建立短期记录；确认微信已执行发送后调用 ``commit``，明确失败
    则调用 ``cancel``。这种两阶段登记避免机器人把自己的消息再次当作用户入站，
    同时不会因一次失败发送长期屏蔽真正的同内容消息。
    """

    def __init__(self, ttl_seconds=120.0, clock=None):
        self.ttl_seconds = max(float(ttl_seconds), 1.0)
        self._clock = clock or time.monotonic
        self._records = deque()
        self._lock = threading.RLock()

    def reserve(self, group, content, max_hits=1):
        return self.reserve_many(group, (content,), max_hits=max_hits)

    def reserve_many(self, group, contents, max_hits=1):
        chat_key = normalize_chat_name(group)
        normalized = []
        for content in contents:
            value = _normalize_content(content)
            if value and value not in normalized:
                normalized.append(value)
        if not chat_key or not normalized:
            return None

        reservation_id = uuid.uuid4().hex
        expires_at = self._clock() + self.ttl_seconds
        hits = max(int(max_hits), 1)
        with self._lock:
            self._purge_locked()
            self._records.extend(
                _OutgoingRecord(
                    reservation_id=reservation_id,
                    chat_key=chat_key,
                    content=content,
                    expires_at=expires_at,
                    remaining_hits=hits,
                )
                for content in normalized
            )
        return reservation_id

    def record(self, group, content, max_hits=1):
        """兼容 wx4py 旧版仅提供 ``record`` 的登记接口。"""
        return self.reserve(group, content, max_hits=max_hits)

    def commit(self, reservation_id):
        reservation_ids = self._reservation_ids(reservation_id)
        if not reservation_ids:
            return
        with self._lock:
            now = self._clock()
            expires_at = now + self.ttl_seconds
            for record in self._records:
                if record.reservation_id in reservation_ids:
                    record.expires_at = expires_at

    def cancel(self, reservation_id):
        reservation_ids = self._reservation_ids(reservation_id)
        if not reservation_ids:
            return
        with self._lock:
            self._records = deque(
                record
                for record in self._records
                if record.reservation_id not in reservation_ids
            )

    def should_ignore(self, group, content):
        chat_key = normalize_chat_name(group)
        actual = _normalize_content(content)
        if not chat_key or not actual:
            return False
        with self._lock:
            self._purge_locked()
            for record in self._records:
                if record.chat_key != chat_key:
                    continue
                if not _content_matches(record.content, actual):
                    continue
                reservation_id = record.reservation_id
                for candidate in self._records:
                    if candidate.reservation_id == reservation_id:
                        candidate.remaining_hits -= 1
                self._records = deque(
                    candidate
                    for candidate in self._records
                    if not (
                        candidate.reservation_id == reservation_id
                        and candidate.remaining_hits <= 0
                    )
                )
                return True
        return False

    @staticmethod
    def _reservation_ids(value):
        if isinstance(value, (list, tuple, set, frozenset)):
            return {item for item in value if item}
        return {value} if value else set()

    def _purge_locked(self):
        now = self._clock()
        self._records = deque(
            record for record in self._records if record.expires_at >= now
        )
