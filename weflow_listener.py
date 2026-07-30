"""接收 WeFlow HTTP/SSE 推送并标准化为 WeMai 入站事件。

性能不变量：SSE 读取线程只负责拆帧和入队，不执行历史查询、媒体下载等慢操作；
同一 ``sessionId`` 严格按序处理，不同会话由共享线程池并行处理。群成员头像等
可选元数据使用独立低优先级线程，任何失败都不得阻塞正文投递。

数据职责：WeFlow 原始字段在这里完成私聊/群聊判定、发送者身份提取、引用消息
补全、合并聊天记录展开及媒体落盘，然后才交给 ``wx_Processor`` 持久化投递。
"""

import contextlib
import concurrent.futures
import json
import logging
import os
import re
import tempfile
import threading
import time
import xml.etree.ElementTree as ET
from collections import deque
from urllib.parse import urlencode, urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from chat_name_utils import normalize_chat_name
from config import (
    FILE_MAX_BYTES,
    IMAGE_AUTO_DOWNLOAD,
    IMAGE_SAVE_DIR,
    MAX_MEDIA_BYTES,
    MEDIA_DOWNLOAD_TIMEOUT_SECONDS,
    WEFLOW_API_TOKEN,
    WEFLOW_API_URL,
    WEFLOW_DISPATCH_WORKERS,
    WX_TARGET_CHATS,
)
from wx_Listener import _InboundEvent

logger = logging.getLogger(__name__)

_SSE_PATH = "/api/v1/push/messages"
_MESSAGES_PATH = "/api/v1/messages"
_DEDUPLICATION_LIMIT = 10000
_SSE_READ_TIMEOUT_SECONDS = 60
_RECONNECT_INITIAL_SECONDS = 1.0
_RECONNECT_MAX_SECONDS = 30.0
_EMOJI_HISTORY_ATTEMPTS = 3
_EMOJI_HISTORY_RETRY_SECONDS = 0.2
_VOICE_HISTORY_ATTEMPTS = 3
_VOICE_HISTORY_RETRY_SECONDS = 0.2
_GROUP_MEMBERS_CACHE_SECONDS = 15 * 60
_GROUP_MEMBERS_FAILURE_CACHE_SECONDS = 30
_HISTORY_ROWS_CACHE_SECONDS = 30
_HISTORY_FAST_LIMIT = 10
_HISTORY_FALLBACK_LIMIT = 100
_SSE_SESSION_BATCH_SIZE = 8
_SSE_PENDING_LIMIT = 10000
_CHAT_RECORD_MAX_XML_CHARS = 2 * 1024 * 1024
_CHAT_RECORD_MAX_DEPTH = 4
_CHAT_RECORD_MAX_ITEMS = 200
_CHAT_RECORD_MAX_OUTPUT_CHARS = 30000
_CHAT_RECORD_ITEM_TEXT_CHARS = 2000
_CHAT_RECORD_HISTORY_ATTEMPTS = 3
_CHAT_RECORD_HISTORY_RETRY_SECONDS = 0.2
_SYSTEM_SOURCE_NAMES = {
    "未知发送者",
    "系统消息",
    "微信系统消息",
    "weixin",
    "weixin_system_message",
}
_SYSTEM_SOURCE_NAME_KEYS = {name.casefold() for name in _SYSTEM_SOURCE_NAMES}
_SYSTEM_CONTENT_MARKER = re.compile(
    r"(?:撤回了一条消息|拍了拍|修改群名|修改了群名|更改群名|"
    r"邀请.+加入了群聊|加入了群聊|移出了群聊|被移出群聊|"
    r"群公告|群主已将|开启了群聊邀请确认|关闭了群聊邀请确认)"
)
_SAFE_FILE_COMPONENT = re.compile(r"[^A-Za-z0-9._-]+")
_IMAGE_MAGIC = {
    b"\x89PNG\r\n\x1a\n": ".png",
    b"\xff\xd8\xff": ".jpg",
    b"GIF87a": ".gif",
    b"GIF89a": ".gif",
    b"RIFF": ".webp",
    b"BM": ".bmp",
}
_VOICE_MARKER = re.compile(
    r"^\s*(?:"
    r"(?:语音(?:消息)?|voice(?:\s+message)?)"
    r"(?:\s*[:：-]?\s*[^\r\n]+)?"
    r"|[\[【（(「『]\s*"
    r"(?:语音(?:消息)?|voice(?:\s+message)?)"
    r"(?:\s*[:：-]?\s*[^\]】）)」』\r\n]+)?\s*[\]】）)」』]"
    r"(?:\s*[:：-]?\s*[^\r\n]+)?"
    r")\s*$",
    re.IGNORECASE,
)
_EMOJI_MARKER = re.compile(
    r"^\s*(?:"
    r"(?:表情(?:包|消息)?|动画表情(?:包)?|动态表情(?:包)?|"
    r"emoji|sticker|animated\s+(?:emoji|sticker))"
    r"(?:\s*[:：-]\s*[^\r\n]+)?"
    r"|[\[【（(「『]\s*"
    r"(?:表情(?:包|消息)?|动画表情(?:包)?|动态表情(?:包)?|"
    r"emoji|sticker|animated\s+(?:emoji|sticker))"
    r"(?:\s*[:：-]?\s*[^\]】）)」』\r\n]+)?\s*[\]】）)」』]"
    r"(?:\s*[:：-]?\s*[^\r\n]+)?"
    r")\s*$",
    re.IGNORECASE,
)
_EMOJI_TYPE_NAMES = {
    "emoji",
    "sticker",
    "animatedemoji",
    "animatedsticker",
    "customemoji",
    "emoticon",
    "expression",
    "表情",
    "表情包",
    "动画表情",
    "动画表情包",
    "动态表情",
    "动态表情包",
}
_VOICE_TYPE_NAMES = {
    "voice",
    "voicemsg",
    "voicemessage",
    "audio",
    "audiomsg",
    "audiomessage",
    "语音",
    "语音消息",
}
_MEDIA_TYPE_FIELDS = (
    "mediaType",
    "media_type",
    "messageType",
    "message_type",
    "msgType",
    "msg_type",
    "contentType",
    "content_type",
    "type",
)


def _url_origin(url):
    parsed = urlsplit(str(url))
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname:
        raise ValueError("WeFlow URL must use HTTP(S) and include a host")
    default_port = 443 if parsed.scheme.casefold() == "https" else 80
    return parsed.scheme.casefold(), parsed.hostname.casefold(), parsed.port or default_port


class _SameOriginRedirectHandler(HTTPRedirectHandler):
    def __init__(self, allowed_origin):
        super().__init__()
        self.allowed_origin = allowed_origin

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if _url_origin(newurl) != self.allowed_origin:
            raise ValueError("WeFlow redirect changed origin; refusing to forward token")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class WeFlowListener:
    """维持 WeFlow SSE 长连接，并输出标准化、按会话有序的入站事件。"""

    def __init__(
        self,
        target_chats=None,
        callback=None,
        stop_event=None,
        api_url=WEFLOW_API_URL,
        api_token=WEFLOW_API_TOKEN,
        opener=None,
        outgoing_registry=None,
        recency_recorder=None,
        known_message_checker=None,
        dispatch_workers=WEFLOW_DISPATCH_WORKERS,
    ):
        self.api_url = str(api_url or "").rstrip("/")
        self.api_token = str(api_token or "").strip()
        if not self.api_url:
            raise ValueError("WEFLOW_API_URL 不能为空")
        if not self.api_token:
            raise ValueError("WEFLOW_API_TOKEN 为必填配置")
        self.callback = callback
        self.stop_event = stop_event or threading.Event()
        self._api_origin = _url_origin(self.api_url)
        self._opener = opener or build_opener(
            _SameOriginRedirectHandler(self._api_origin)
        )
        self.outgoing_registry = outgoing_registry
        self._known_message_checker = known_message_checker
        self._dispatch_workers = int(dispatch_workers)
        if not 1 <= self._dispatch_workers <= 32:
            raise ValueError("WeFlow 入站解析并发必须在 1..32 范围内")
        # 跨线程的轻量回调：每次派发 WeFlow 消息时，把聊天名称、session ID 和
        # 类型记录到微信 UI 层。session ID 是不随群名变化的真实标识，可把改名后
        # 的新名称重新绑定到标题仍为旧名称的已打开独立窗口。该回调位于入站热路径，
        # 必须保持线程安全且近似 O(1)，不得在其中执行 UI 查询或网络请求。
        self._recency_recorder = recency_recorder
        self._targets = self._normalize_targets(
            WX_TARGET_CHATS if target_chats is None else target_chats
        )
        self._seen = set()
        self._seen_order = deque()
        # 一次 group-members 响应已经包含全体成员，因此按稳定 chatroom ID 缓存，
        # 不能按每条消息发起请求。15 分钟缓存兼顾入站速度与昵称、头像后续刷新。
        self._group_members_cache = {}
        self._group_members_cache_lock = threading.RLock()
        self._group_members_refreshing = set()
        self._metadata_executor = None
        self._history_cache = {}
        self._history_cache_lock = threading.RLock()
        self._response = None
        self._response_lock = threading.Lock()
        self._thread = None
        self._dispatch_executor = None
        self._dispatch_lock = threading.RLock()
        self._dispatch_condition = threading.Condition(self._dispatch_lock)
        self._dispatch_queues = {}
        self._dispatch_active_sessions = set()
        self._dispatch_pending = 0
        self.ready_event = threading.Event()
        self.startup_error = None
        self.running = False

    @staticmethod
    def _normalize_targets(targets):
        result = []
        for item in targets or []:
            if isinstance(item, str):
                name, chat_type = item.strip(), None
            elif isinstance(item, dict):
                name = str(item.get("name", "")).strip()
                chat_type = item.get("type")
                if chat_type not in {None, "private", "group"}:
                    raise ValueError(f"无效聊天类型: {chat_type!r}")
            else:
                raise TypeError("聊天配置必须是字符串或 {name,type} 字典")
            key = normalize_chat_name(name)
            if key and not any(target["key"] == key for target in result):
                result.append({"name": name, "key": key, "type": chat_type})
        return result

    @property
    def thread(self):
        return self._thread

    @property
    def is_running(self):
        return self.running and self._thread is not None and self._thread.is_alive()

    def start(self, timeout=20):
        if self._thread and self._thread.is_alive():
            return
        if self._dispatch_executor is None:
            # 每会话邮箱保证严格顺序；共享线程池避免固定分片产生哈希碰撞。单次只
            # 消费有限批次，繁忙群聊会重新排到队尾，避免安静会话长期得不到处理。
            self._dispatch_executor = concurrent.futures.ThreadPoolExecutor(
                max_workers=self._dispatch_workers,
                thread_name_prefix="weflow-inbound",
            )
        if self._metadata_executor is None:
            # 群成员头像、昵称属于可选增强信息，绝不能占用入站消息工作池。
            # 单独一个低并发后台线程即可暖缓存，也避免多个活跃群同时向
            # WeFlow 发起大量 group-members 请求。
            self._metadata_executor = concurrent.futures.ThreadPoolExecutor(
                max_workers=1,
                thread_name_prefix="weflow-metadata",
            )
        self.ready_event.clear()
        self.startup_error = None
        self._thread = threading.Thread(
            target=self.run_forever,
            name="weflow-sse",
            daemon=True,
        )
        self._thread.start()
        if not self.ready_event.wait(timeout):
            self.close()
            detail = f": {self.startup_error}" if self.startup_error else ""
            raise TimeoutError(f"连接 WeFlow SSE 超时{detail}")

    def close(self, timeout=10):
        self.stop_event.set()
        with self._dispatch_condition:
            self._dispatch_condition.notify_all()
        with self._response_lock:
            response = self._response
        if response is not None:
            try:
                response.close()
            except Exception:
                logger.debug("关闭 WeFlow SSE 响应失败", exc_info=True)
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout)
        executor, self._dispatch_executor = self._dispatch_executor, None
        if executor is not None:
            executor.shutdown(wait=False, cancel_futures=True)
        metadata_executor, self._metadata_executor = self._metadata_executor, None
        if metadata_executor is not None:
            metadata_executor.shutdown(wait=False, cancel_futures=True)
        with self._dispatch_condition:
            self._dispatch_queues.clear()
            self._dispatch_active_sessions.clear()
            self._dispatch_pending = 0
            self._dispatch_condition.notify_all()
        self.running = False

    def run_forever(self):
        self.running = True
        reconnect_delay = _RECONNECT_INITIAL_SECONDS
        try:
            while not self.stop_event.is_set():
                connected = False
                try:
                    self._consume_once()
                    connected = True
                except Exception as exc:
                    self.startup_error = exc
                    if not self.stop_event.is_set():
                        logger.warning(
                            "WeFlow SSE 连接中断，将在 %.1f 秒后重连: %s",
                            reconnect_delay,
                            exc,
                        )
                if self.stop_event.is_set():
                    break
                if connected:
                    reconnect_delay = _RECONNECT_INITIAL_SECONDS
                if self.stop_event.wait(reconnect_delay):
                    break
                reconnect_delay = min(
                    reconnect_delay * 2, _RECONNECT_MAX_SECONDS
                )
        finally:
            self.running = False

    def _consume_once(self):
        request = self._request(
            _SSE_PATH,
            query={"access_token": self.api_token},
            accept="text/event-stream",
        )
        response = self._opener.open(
            request,
            timeout=_SSE_READ_TIMEOUT_SECONDS,
        )
        with self._response_lock:
            self._response = response
        try:
            content_type = response.headers.get_content_type()
            if content_type != "text/event-stream":
                raise RuntimeError(
                    f"WeFlow SSE 响应类型无效: {content_type!r}"
                )
            self.startup_error = None
            self.ready_event.set()
            logger.info("WeFlow SSE 已连接")
            event_name = ""
            data_lines = []
            for raw_line in response:
                if self.stop_event.is_set():
                    return
                line = raw_line.decode("utf-8").rstrip("\r\n")
                if not line:
                    self._submit_sse_frame(event_name, data_lines)
                    event_name, data_lines = "", []
                elif line.startswith(":"):
                    continue
                elif line.startswith("event:"):
                    event_name = line[6:].lstrip(" ")
                elif line.startswith("data:"):
                    data_lines.append(line[5:].lstrip(" "))
            if data_lines:
                self._submit_sse_frame(event_name, data_lines)
            if not self.stop_event.is_set():
                raise ConnectionError("WeFlow SSE 流已结束")
        finally:
            with self._response_lock:
                if self._response is response:
                    self._response = None
            response.close()

    def _submit_sse_frame(self, event_name, data_lines):
        """Submit one SSE frame without blocking the socket reader."""
        if not data_lines:
            return
        executor = getattr(self, "_dispatch_executor", None)
        if executor is None:
            # Direct unit tests and legacy callers keep synchronous behavior.
            self._dispatch_sse_frame(event_name, data_lines)
            return
        try:
            payload = json.loads("\n".join(data_lines))
            session_key = str(
                payload.get("sessionId")
                or payload.get("rawid")
                or event_name
            )
        except (TypeError, ValueError, AttributeError):
            session_key = str(event_name or "invalid")
        frame = (str(event_name or ""), tuple(data_lines))
        schedule = False
        with self._dispatch_condition:
            while (
                self._dispatch_pending >= _SSE_PENDING_LIMIT
                and not self.stop_event.is_set()
            ):
                # Extreme overload applies backpressure to the local SSE
                # socket instead of dropping or reordering messages.
                self._dispatch_condition.wait(0.1)
            if self.stop_event.is_set():
                return
            queue = self._dispatch_queues.setdefault(session_key, deque())
            queue.append(frame)
            self._dispatch_pending += 1
            if session_key not in self._dispatch_active_sessions:
                self._dispatch_active_sessions.add(session_key)
                schedule = True
        if schedule:
            try:
                executor.submit(self._drain_dispatch_session, session_key)
            except RuntimeError:
                if not self.stop_event.is_set():
                    raise

    def _drain_dispatch_session(self, session_key):
        """Process a fair-sized ordered batch from one chat session."""
        processed = 0
        while processed < _SSE_SESSION_BATCH_SIZE:
            with self._dispatch_condition:
                queue = self._dispatch_queues.get(session_key)
                if not queue:
                    break
                event_name, data_lines = queue.popleft()
                self._dispatch_pending = max(0, self._dispatch_pending - 1)
                self._dispatch_condition.notify()
            started = time.monotonic()
            try:
                self._dispatch_sse_frame(event_name, data_lines)
            except Exception:
                logger.exception("处理 WeFlow 异步入站帧失败 event=%s", event_name)
            finally:
                elapsed = time.monotonic() - started
                if elapsed >= 0.5:
                    logger.info(
                        "WeFlow 帧处理耗时 %.2fs event=%s",
                        elapsed,
                        event_name,
                    )
            processed += 1

        reschedule = False
        with self._dispatch_condition:
            queue = self._dispatch_queues.get(session_key)
            if queue and not self.stop_event.is_set():
                reschedule = True
            else:
                if queue:
                    self._dispatch_pending = max(
                        0,
                        self._dispatch_pending - len(queue),
                    )
                self._dispatch_queues.pop(session_key, None)
                self._dispatch_active_sessions.discard(session_key)
                self._dispatch_condition.notify_all()
        if reschedule:
            executor = self._dispatch_executor
            if executor is None:
                return
            try:
                executor.submit(self._drain_dispatch_session, session_key)
            except RuntimeError:
                with self._dispatch_condition:
                    self._dispatch_active_sessions.discard(session_key)

    def _dispatch_sse_frame(self, event_name, data_lines):
        dispatch_started_at = time.monotonic()
        if not data_lines:
            return
        try:
            payload = json.loads("\n".join(data_lines))
        except (TypeError, ValueError):
            logger.warning("忽略无效 WeFlow SSE JSON", exc_info=True)
            return
        if not isinstance(payload, dict):
            logger.warning("忽略非对象 WeFlow SSE data")
            return
        payload_event = str(payload.get("event", ""))
        if event_name != payload_event:
            logger.warning(
                "忽略事件名不一致的 WeFlow 推送 frame=%r data=%r",
                event_name,
                payload_event,
            )
            return
        if event_name not in {"message.new", "message.revoke"}:
            logger.debug("忽略未知 WeFlow 事件 event=%r", event_name)
            return

        rawid = str(payload.get("rawid", "")).strip()
        if not rawid:
            logger.warning("忽略缺少 rawid 的 WeFlow 消息")
            return
        deduplication_key = (event_name, rawid)
        if deduplication_key in self._seen:
            return
        if event_name == "message.revoke":
            # 撤回与原消息共用 rawid，但事件名不同。按文档使用
            # event+rawid 独立去重；当前不把撤回通知投递给 MaiBot。
            self._remember_seen(deduplication_key)
            logger.debug("忽略 WeFlow 撤回事件 rawid=%s", rawid)
            return
        checker = getattr(self, "_known_message_checker", None)
        if checker is not None:
            try:
                if checker(rawid, event_name):
                    self._remember_seen(deduplication_key)
                    logger.info(
                        "忽略 WeFlow 重放的已处理消息 rawid=%s event=%s",
                        rawid,
                        event_name,
                    )
                    return
            except Exception:
                # 去重预查失败时必须放行：临时 SQLite 读取错误不能导致新消息丢失，
                # 后续持久化 INSERT 仍会执行最终的权威去重。
                logger.debug(
                    "检查 WeFlow 重放消息失败 rawid=%s",
                    rawid,
                    exc_info=True,
                )
        if self._payload_is_outgoing(payload):
            self._remember_seen(deduplication_key)
            outgoing_type = (
                payload.get("type")
                or payload.get("messageType")
                or payload.get("localType")
                or "unknown"
            )
            logger.info(
                "忽略 WeFlow 明确标记的自发消息 type=%s rawid=%s",
                outgoing_type,
                rawid,
            )
            return

        session_type = payload.get("sessionType")
        if session_type == "group":
            chat_type = "group"
            display_name = str(payload.get("groupName", "") or "").strip()
        elif session_type == "other":
            chat_type = "private"
            display_name = str(payload.get("sourceName", "") or "").strip()
        else:
            logger.warning(
                "忽略未知 sessionType 的 WeFlow 消息 rawid=%s type=%r",
                rawid,
                session_type,
            )
            return
        session_id = str(payload.get("sessionId", "") or "").strip()
        target = self._matching_target(session_id, display_name, chat_type)
        if target is None:
            return

        chat_name = display_name or target["name"] or session_id
        content = "" if payload.get("content") is None else str(payload["content"])
        registry = self.outgoing_registry
        if registry is not None and any(
            registry.should_ignore(chat_name, candidate)
            for candidate in self._outgoing_content_candidates(payload, content)
        ):
            self._remember_seen(deduplication_key)
            logger.info(
                "忽略 WeFlow 自发消息回流 chat=%s rawid=%s",
                chat_name,
                rawid,
            )
            return
        message_type = "text"
        raw = dict(payload)
        history_message = None
        history_lookup_attempted = False
        # WeFlow SSE 不提供群成员真 ID，但会提供当前显示名称。尽早用名称
        # 触发群成员缓存预热，使它能与下面的历史查询并行，不增加入站耗时。
        sender_raw = str(payload.get("sourceName", "") or "").strip()
        sender_nick = str(payload.get("fromNickName", "") or "").strip()
        sender_group_nick = str(payload.get("displayName", "") or "").strip()
        cached_sender_wxid = ""
        cached_member = {}
        if chat_type == "group":
            cached_sender_wxid, cached_member = self._group_member_identity(
                session_id,
                sender_group_nick,
                sender_nick,
                sender_raw,
            )
        file_name = self._file_name_from_message(payload, content)
        empty_content = not content.strip()
        voice_marker = self._is_voice_marker(content)
        voice_media_hint = self._is_voice_media(payload)
        voice_hint = voice_media_hint or (voice_marker and not empty_content)
        emoji_hint = self._is_emoji_marker(content) or self._is_emoji_media(payload)
        chat_record_hint = self._is_chat_record_marker(content)
        command_text = (
            content.lstrip().startswith(("/", "／"))
            and not payload.get("mediaUrl")
            and self._media_type(payload) in (None, "")
        )
        logger.debug(
            "检测 WeFlow 语音候选 rawid=%s content=%r marker=%s "
            "payload_media=%s empty=%s media_type=%r",
            rawid,
            content[:160],
            voice_marker,
            voice_media_hint,
            empty_content,
            self._media_type(payload),
        )
        if emoji_hint:
            logger.debug(
                "准备解析 WeFlow 表情媒体 rawid=%s content=%r payload_hint=%s",
                rawid,
                content[:160],
                emoji_hint,
            )
        if file_name:
            message_type = "file"
            logger.debug(
                "识别 WeFlow 普通文件消息 rawid=%s name=%s",
                rawid,
                file_name,
            )
            # 文件消息不能只保留文件名：如果 WeFlow 提供 mediaUrl，
            # 立即保存到 WeMai媒体，后续 MaiBot 才能读取真实文件内容。
            try:
                if payload.get("mediaUrl"):
                    media = payload
                else:
                    media = self._media_for_message(
                        session_id,
                        rawid,
                        history_type="文件",
                        strict=False,
                        include_media=True,
                    )
                    history_message = media
                if media and media.get("mediaUrl"):
                    content, message_type = self._download_media(
                        media,
                        rawid,
                        expected_type="file",
                    )
                    raw["message"] = media
            except Exception:
                logger.exception(
                    "读取或下载 WeFlow 文件失败，降级为文件名 rawid=%s",
                    rawid,
                )
        elif command_text:
            logger.debug("WeFlow 命令文本走快速通道 rawid=%s", rawid)
        elif voice_hint or empty_content:
            # Voice export is optional on the WeFlow side.  A missing/invalid
            # export must never prevent the original SSE event from flowing.
            try:
                logger.debug(
                    "准备解析 WeFlow 语音媒体 rawid=%s marker=%s "
                    "payload_media=%s empty=%s",
                    rawid,
                    voice_marker,
                    voice_media_hint,
                    empty_content,
                )
                if payload.get("mediaUrl"):
                    media = payload
                else:
                    media = self._voice_for_message(session_id, rawid)
                    history_message = media
                if media and media.get("mediaUrl"):
                    media_is_voice = self._is_voice_media(media)
                    media_is_emoji = self._is_emoji_media(media)
                    if voice_hint or media_is_voice:
                        expected_type = "voice"
                    elif media_is_emoji:
                        expected_type = "emoji"
                    else:
                        expected_type = None
                    logger.debug(
                        "分类 WeFlow 空内容/语音媒体 rawid=%s media_type=%r "
                        "voice=%s emoji=%s expected_type=%s",
                        rawid,
                        self._media_type(media),
                        media_is_voice,
                        media_is_emoji,
                        expected_type,
                    )
                    content, message_type = self._download_media(
                        media,
                        rawid,
                        expected_type=expected_type,
                    )
                    raw["message"] = media
                else:
                    logger.debug(
                        "WeFlow 语音历史未提供媒体 rawid=%s media_type=%r has_url=%s",
                        rawid,
                        self._media_type(media),
                        bool(media and media.get("mediaUrl")),
                    )
            except Exception:
                logger.debug(
                    "WeFlow 语音导出不可用，保留原消息文本 rawid=%s",
                    rawid,
                    exc_info=True,
                )
        elif emoji_hint:
            try:
                if payload.get("mediaUrl"):
                    media = payload
                else:
                    media = self._emoji_for_message(session_id, rawid)
                    history_message = media
                if media and media.get("mediaUrl"):
                    expected_type = (
                        "emoji"
                        if emoji_hint or self._is_emoji_media(media)
                        else None
                    )
                    content, message_type = self._download_media(
                        media,
                        rawid,
                        expected_type=expected_type,
                    )
                    raw["message"] = media
                else:
                    logger.debug(
                        "WeFlow 表情历史未提供媒体 rawid=%s media_type=%r has_url=%s",
                        rawid,
                        self._media_type(media),
                        bool(media and media.get("mediaUrl")),
                    )
            except Exception:
                logger.debug(
                    "WeFlow 表情导出不可用，保留原消息文本 rawid=%s",
                    rawid,
                    exc_info=True,
                )
        elif chat_record_hint:
            # SSE only exposes ``[聊天记录]``.  The same history row contains
            # appmsg type 19 with a complete recorditem XML document.  Use the
            # cheap metadata query (10 rows, then 100 only on miss); nested
            # media is described in that XML and does not need media=1.
            try:
                history_message = self._media_for_message(
                    session_id,
                    rawid,
                    attempts=_CHAT_RECORD_HISTORY_ATTEMPTS,
                    retry_seconds=_CHAT_RECORD_HISTORY_RETRY_SECONDS,
                    strict=False,
                    history_type="合并聊天记录",
                    include_media=False,
                    bypass_cache=True,
                )
                rendered_record = self._render_chat_record(history_message)
                if rendered_record:
                    content = rendered_record
                    raw["weflowChatRecord"] = True
                    logger.info(
                        "WeFlow 合并聊天记录已展开 rawid=%s chars=%d",
                        rawid,
                        len(content),
                    )
                else:
                    logger.warning(
                        "WeFlow 合并聊天记录缺少可解析的 type=19 XML，"
                        "保留占位文本 rawid=%s",
                        rawid,
                    )
            except Exception:
                # A malformed or temporarily unavailable history row must not
                # hold up this session or drop the original placeholder.
                logger.warning(
                    "解析 WeFlow 合并聊天记录失败，保留占位文本 rawid=%s",
                    rawid,
                    exc_info=True,
                )
        elif IMAGE_AUTO_DOWNLOAD and not self._is_plain_text_message(
            payload, content
        ):
            try:
                media = self._media_for_message(session_id, rawid)
                history_message = media
                if (
                    media
                    and self._is_voice_media(media)
                    and not media.get("mediaUrl")
                ):
                    logger.debug(
                        "WeFlow 通用历史已识别语音但 URL 未就绪，转入语音重试 "
                        "rawid=%s media_type=%r",
                        rawid,
                        self._media_type(media),
                    )
                    media = self._voice_for_message(session_id, rawid)
                    history_message = media
                if media and media.get("mediaUrl"):
                    if self._is_voice_media(media):
                        expected_type = "voice"
                    elif self._is_emoji_media(media):
                        expected_type = "emoji"
                    else:
                        expected_type = None
                    logger.debug(
                        "分类 WeFlow 通用媒体 rawid=%s media_type=%r expected_type=%s",
                        rawid,
                        self._media_type(media),
                        expected_type,
                    )
                    content, message_type = self._download_media(
                        media,
                        rawid,
                        expected_type=expected_type,
                    )
                    raw["message"] = media
            except Exception:
                logger.exception(
                    "读取或下载 WeFlow 媒体失败，降级为原消息文本 rawid=%s",
                    rawid,
                )

        # WeFlow SSE 的普通文本 frame 不包含 quote/replyToMessageId。
        # 历史接口仍会返回完整引用元数据，因此在文本消息上按
        # sessionId + rawid 补查一次；失败时 fail-open，不影响正文投递。
        if message_type == "text" and rawid and session_id:
            if isinstance(history_message, dict):
                self._attach_quote_metadata(raw, history_message, rawid)
            else:
                try:
                    history_lookup_attempted = True
                    history_message = self._media_for_message(
                        session_id,
                        rawid,
                        strict=False,
                        history_type="引用",
                        include_media=False,
                    )
                    self._attach_quote_metadata(raw, history_message, rawid)
                except Exception as exc:
                    logger.debug(
                        "WeFlow 引用历史查询失败 rawid=%s: %s",
                        rawid,
                        exc,
                    )

        try:
            timestamp = float(payload.get("timestamp"))
        except (TypeError, ValueError):
            logger.warning("忽略时间戳无效的 WeFlow 消息 rawid=%s", rawid)
            return
        # 必须在构造事件前登记最近入站，使由本条消息立即触发的首次回复也能使用
        # session ID 重绑定窗口，避免把微信会话列表尚未刷新误判为永久找不到。
        recorder = self._recency_recorder
        if recorder is not None and chat_name:
            try:
                recorder(chat_name, session_id, chat_type)
            except Exception:
                logger.debug(
                    "记录 WeFlow 最近 inbound 失败 chat=%s", chat_name,
                    exc_info=True,
                )
        if not file_name:
            file_name = self._file_name_from_message(history_message, content)
            if file_name:
                message_type = "file"
        if message_type == "file" and not file_name:
            file_metadata = history_message if isinstance(history_message, dict) else payload
            for key in ("mediaFileName", "fileName", "filename", "name"):
                file_name = str(file_metadata.get(key, "") or "").strip()
                if file_name:
                    break
        sender_wxid = str(
            payload.get("talker", "") or cached_sender_wxid or ""
        ).strip()
        # WeFlow 4.0 推送不保证携带 talker。群聊缺少发送者真 ID 时，按
        # sessionId+rawid 查询历史详情中的 senderUsername；每条消息最多查询一次。
        # 查询失败采用 fail-open，正文照常投递，身份增强稍后由其他回退路径处理。
        # 私聊的 sessionId 就是对方稳定不变的微信真 ID。直接使用它既能
        # 保持跨私聊/群聊的一致身份，也能省掉每条私聊消息一次同步历史查询。
        if not sender_wxid and chat_type == "private" and session_id:
            sender_wxid = session_id
        if (
            not sender_wxid
            and history_message is None
            and not history_lookup_attempted
            and rawid
            and session_id
        ):
            try:
                history_lookup_attempted = True
                history_message = self._media_for_message(
                    session_id,
                    rawid,
                    strict=False,
                    history_type="sender",
                    # 普通文本只需要 senderUsername；媒体详情会显著增大
                    # 历史响应并阻塞 SSE 线程，只有媒体候选才请求。
                    include_media=bool(
                        file_name
                        or voice_hint
                        or emoji_hint
                        or payload.get("mediaUrl")
                        or self._media_type(payload)
                    ),
                )
            except Exception as exc:
                logger.debug(
                    "WeFlow history API 反查失败 session=%s rawid=%s: %s",
                    session_id, rawid, exc,
                )
        if not sender_wxid and isinstance(history_message, dict):
            sender_wxid = str(
                history_message.get("senderUsername", "") or ""
            ).strip()
        # 历史接口与 SSE 存在同步窗口。若历史仍未出现当前消息，再读一次
        # 已在后台并行预热的群成员缓存，用显示名反查任意格式的微信真 ID。
        # 不校验 wxid_/wx_ 前缀，早期自定义原生 ID 同样有效；仅在名称唯一
        # 命中时使用，避免同名群成员串号。
        if not sender_wxid and chat_type == "group":
            resolved_wxid, resolved_member = self._group_member_identity(
                session_id,
                sender_group_nick,
                sender_nick,
                sender_raw,
            )
            if resolved_wxid:
                sender_wxid = resolved_wxid
                cached_member = resolved_member
        # “拍一拍”外形是系统行，顶层 senderUsername 为空，但内嵌 patinfo 带有真实
        # 操作者 ID。恢复该 ID 以复用普通消息的人物与头像；顶层 avatarUrl 不保证
        # 属于操作者，因此此处禁止写入成员头像缓存。
        pat_actor_wxid, pat_actor_name = self._pat_identity(history_message)
        is_pat_event = bool(pat_actor_wxid)
        if is_pat_event:
            sender_wxid = pat_actor_wxid
            if pat_actor_name:
                sender_raw = pat_actor_name
                sender_nick = pat_actor_name
                sender_group_nick = pat_actor_name
            raw["weflowSystemEvent"] = "pat"
            raw["systemActorWxid"] = pat_actor_wxid

        # “缺少 senderUsername”只表示历史尚未同步，不能代表系统消息。
        # 只有上游明确的系统账号、系统标签或系统事件文本才进入系统身份。
        system_event = self._system_event_kind(
            payload,
            history_message,
            sender_wxid,
            is_pat_event=is_pat_event,
        )
        if system_event:
            raw["weflowSystemEvent"] = system_event

        has_real_sender = bool(sender_wxid) and sender_wxid.casefold() != "weixin"
        member = (
            cached_member or self._group_member_metadata(session_id, sender_wxid)
            if chat_type == "group" and has_real_sender
            else {}
        )
        if member:
            # 资料昵称与群名片分开保存：displayName 可能已受本地备注影响，而明确的
            # groupNickname 才是群消息气泡上方显示的名称。
            member_profile_name = (
                member.get("nickname")
                or member.get("remark")
                or member.get("displayName")
            )
            member_group_name = (
                member.get("groupNickname")
                or member.get("displayName")
                or member_profile_name
            )
            if member_profile_name:
                sender_nick = member_profile_name
                sender_raw = member_profile_name
            if member_group_name:
                sender_group_nick = member_group_name
        logger.info(
            "WeFlow payload 字段 keys=%s talker=%r fromUserName=%r fromWxid=%r senderId=%r sender_wxid=%r",
            sorted(payload.keys()),
            payload.get("talker"),
            payload.get("fromUserName"),
            payload.get("fromWxid"),
            payload.get("senderId"),
            sender_wxid,
        )
        # 群聊优先显示群名片（也是 @ 时可见名称），私聊优先显示资料昵称。
        sender_candidate = (
            sender_group_nick or sender_nick or sender_raw
        )
        event = _InboundEvent(
            # group 始终使用微信当前显示名称；稳定身份由 session_id 单独承载。
            group=chat_name,
            content=content,
            timestamp=timestamp,
            raw=raw,
            message_type=message_type,
            # sender 是当前显示名称，优先级为群名片 > 资料昵称 > 推送来源名。
            sender=sender_candidate or sender_raw or chat_name,
            chat_type=chat_type,
            avatar_url=(
                str(member.get("avatarUrl") or "").strip()
                if member
                else (
                    ""
                    # WeFlow 群聊 SSE 的 avatarUrl 常是群头像，不能把它
                    # 写入成员 wxid。成员缓存尚未暖好时宁可暂不更新头像。
                    if (
                        chat_type == "group"
                        or is_pat_event
                        or not has_real_sender
                    )
                    else str(payload.get("avatarUrl", "") or "").strip()
                )
            ),
            # 群聊 SSE 的顶层 avatarUrl 是会话头像，单独传递到群头像
            # 命名空间；绝不再把它写成成员头像。
            group_avatar_url=(
                str(payload.get("avatarUrl", "") or "").strip()
                if chat_type == "group"
                else ""
            ),
            # 传递稳定会话真 ID，供窗口层在群改名后重新绑定已有 HWND。
            session_id=session_id,
            # 发送者级稳定身份。缺失时已在上方通过历史 senderUsername 或成员缓存
            # 回查；处理层会据此计算跨私聊、群聊一致的 user_id。
            sender_wxid=sender_wxid,
            user_nickname=sender_nick or sender_raw,
            group_card_name=sender_group_nick if chat_type == "group" else "",
            file_name=file_name,
        )
        if self.callback is not None:
            result = self.callback(event)
            if isinstance(result, dict) and not result.get("success"):
                raise RuntimeError(result.get("error") or "入站消息持久化失败")
        self._remember_seen(deduplication_key)
        source_timestamp = timestamp / 1000 if timestamp > 10_000_000_000 else timestamp
        source_lag = max(time.time() - source_timestamp, 0.0)
        logger.info(
            "收到 WeFlow 消息 chat=%s session=%s chat_type=%s sender=%s "
            "type=%s rawid=%s source_lag=%.3fs adapter_elapsed=%.3fs",
            chat_name,
            session_id,
            chat_type,
            event.sender,
            message_type,
            rawid,
            source_lag,
            time.monotonic() - dispatch_started_at,
        )

    def _matching_target(self, session_id, display_name, chat_type):
        candidate_keys = {
            normalize_chat_name(session_id),
            normalize_chat_name(display_name),
        } - {""}
        # Empty WX_TARGET_CHATS means "monitor everything by default".
        # Operators can still narrow by setting WX_TARGET_CHATS to a
        # JSON or comma-separated list of chat names (optionally with
        # type hints).
        if not self._targets:
            name = display_name or session_id
            return {
                "name": name,
                "key": normalize_chat_name(name),
                "type": None,
            }
        for target in self._targets:
            if target["key"] in candidate_keys and target["type"] in {
                None,
                chat_type,
            }:
                return target
        return None

    @staticmethod
    def _payload_is_outgoing(payload):
        for key in ("isSend", "isSelf", "fromSelf", "isOutgoing"):
            value = payload.get(key)
            if value is True or value == 1:
                return True
            if isinstance(value, str) and value.strip().lower() in {
                "1", "true", "yes", "sent", "outgoing",
            }:
                return True
        direction = str(payload.get("direction", "") or "").strip().lower()
        return direction in {"sent", "send", "outgoing", "right"}

    @staticmethod
    def _outgoing_content_candidates(payload, content):
        candidates = []
        for value in (
            content,
            payload.get("fileName"),
            payload.get("mediaFileName"),
        ):
            value = str(value or "").strip()
            if value and value not in candidates:
                candidates.append(value)
        return candidates

    @staticmethod
    def _pat_identity(history_message):
        """Recover the real actor from a WeChat ``拍一拍`` history row.

        WeFlow reports these events without ``senderUsername`` at the row
        level, but WeChat keeps the actor wxid in ``appmsg/patinfo``.  The
        human-readable title contains the current display name.  Returning an
        empty pair for every other XML/system event keeps the normal system
        message fallback unchanged.
        """
        if not isinstance(history_message, dict):
            return "", ""
        xml_text = str(
            history_message.get("rawContent")
            or history_message.get("content")
            or ""
        ).strip()
        if not xml_text or "<patinfo>" not in xml_text:
            return "", ""
        try:
            root = ET.fromstring(xml_text)
        except (ET.ParseError, ValueError):
            return "", ""
        patinfo = root.find(".//patinfo")
        if patinfo is None:
            return "", ""
        actor_wxid = str(patinfo.findtext("fromusername") or "").strip()
        if not actor_wxid:
            return "", ""

        title = str(root.findtext(".//appmsg/title") or "").strip()
        actor_name = ""
        # Typical title: '"张三" 拍了拍 "李四"'.  Split on the event
        # phrase first, then remove only the surrounding quote characters so
        # emoji and non-ASCII names are preserved verbatim.
        if "拍了拍" in title:
            actor_name = title.split("拍了拍", 1)[0].strip()
            actor_name = actor_name.strip('"\'“”‘’ ').strip()
        return actor_wxid, actor_name

    @staticmethod
    def _system_event_kind(payload, history_message, sender_wxid, *, is_pat_event=False):
        """Return an explicit system-event kind, never inferred from a blank ID.

        WeFlow may publish an ordinary member message before the history table
        exposes ``senderUsername``.  It may also reuse the same serverId after
        a revoke, so a later ``localType=10000`` row is not authoritative when
        its text disagrees with the live SSE payload.  Prefer the live payload
        and require positive system evidence.
        """
        if is_pat_event:
            return ""
        stable_sender = str(sender_wxid or "").strip().casefold()
        payload = payload if isinstance(payload, dict) else {}
        source_name = str(payload.get("sourceName") or "").strip()
        live_content = str(payload.get("content") or "").strip()
        if source_name.casefold() in _SYSTEM_SOURCE_NAME_KEYS:
            return "generic"
        if live_content.startswith("<sysmsg") or _SYSTEM_CONTENT_MARKER.search(live_content):
            if "撤回了一条消息" in live_content:
                return "revoke"
            return "generic"
        if stable_sender and stable_sender not in {
            "weixin",
            "weixin_system_message",
        }:
            return ""

        history_sender = ""
        history_xml = ""
        if isinstance(history_message, dict):
            history_sender = str(
                history_message.get("senderUsername") or ""
            ).strip().casefold()
            history_xml = str(
                history_message.get("rawContent")
                or history_message.get("content")
                or ""
            ).strip()
        if history_sender in {"weixin", "weixin_system_message"}:
            return "generic"
        # A system XML row is trusted only when the live text also describes
        # that event. This avoids turning the original member text into a
        # system message after WeFlow mutates the row into a revoke record.
        if history_xml.startswith("<sysmsg") and _SYSTEM_CONTENT_MARKER.search(live_content):
            return "generic"
        return ""

    def _group_member_identity(self, chatroom_id, *display_names):
        """Resolve one uniquely named group member without blocking inbound.

        Any non-empty member ID is accepted; its prefix is intentionally not
        validated because older WeChat accounts may use user-chosen IDs.
        Cache misses schedule one whole-room refresh on the metadata executor
        and return immediately.
        """
        chatroom_id = str(chatroom_id or "").strip()
        wanted_names = {
            str(name or "").strip().casefold()
            for name in display_names
            if str(name or "").strip()
        }
        if not chatroom_id.endswith("@chatroom") or not wanted_names:
            return "", {}

        cache = getattr(self, "_group_members_cache", None)
        if cache is None:
            cache = self._group_members_cache = {}
        lock = getattr(self, "_group_members_cache_lock", None)
        if lock is None:
            lock = self._group_members_cache_lock = threading.RLock()
        refreshing = getattr(self, "_group_members_refreshing", None)
        if refreshing is None:
            refreshing = self._group_members_refreshing = set()

        now = time.monotonic()
        with lock:
            cached = cache.get(chatroom_id)
            if cached and cached[0] > now:
                matches = []
                for member_id, member in cached[1].items():
                    member_names = {
                        str(member.get(key) or "").strip().casefold()
                        for key in (
                            "displayName",
                            "nickname",
                            "remark",
                            "groupNickname",
                        )
                        if str(member.get(key) or "").strip()
                    }
                    if member_names & wanted_names:
                        matches.append((str(member_id).strip(), dict(member)))
                unique = {
                    member_id: member
                    for member_id, member in matches
                    if member_id
                }
                if len(unique) == 1:
                    member_id, member = next(iter(unique.items()))
                    return member_id, member
                return "", {}
            if chatroom_id in refreshing:
                return "", {}
            executor = getattr(self, "_metadata_executor", None)
            if executor is None or self.stop_event.is_set():
                return "", {}
            refreshing.add(chatroom_id)

        try:
            executor.submit(self._refresh_group_members, chatroom_id)
        except RuntimeError:
            with lock:
                refreshing.discard(chatroom_id)
        return "", {}

    def _group_member_metadata(self, chatroom_id, sender_wxid):
        """Return cached group metadata and refresh misses off the hot path.

        The group-members endpoint can take hundreds of milliseconds.  Inbound
        delivery is the primary contract, so a cache miss returns immediately
        and a dedicated single-worker executor warms the whole room in the
        background.  The first message may omit its avatar; it is never delayed
        and never receives the room avatar as a false member avatar.
        """
        chatroom_id = str(chatroom_id or "").strip()
        sender_wxid = str(sender_wxid or "").strip()
        if not chatroom_id.endswith("@chatroom") or not sender_wxid:
            return {}
        now = time.monotonic()
        cache = getattr(self, "_group_members_cache", None)
        if cache is None:
            cache = self._group_members_cache = {}
        lock = getattr(self, "_group_members_cache_lock", None)
        if lock is None:
            lock = self._group_members_cache_lock = threading.RLock()
        refreshing = getattr(self, "_group_members_refreshing", None)
        if refreshing is None:
            refreshing = self._group_members_refreshing = set()
        with lock:
            cached = cache.get(chatroom_id)
            if cached and cached[0] > now:
                return dict(cached[1].get(sender_wxid, {}))
            if chatroom_id in refreshing:
                return {}
            executor = getattr(self, "_metadata_executor", None)
            if executor is None or self.stop_event.is_set():
                return {}
            refreshing.add(chatroom_id)

        try:
            executor.submit(self._refresh_group_members, chatroom_id)
        except RuntimeError:
            with lock:
                refreshing.discard(chatroom_id)
        return {}

    def _refresh_group_members(self, chatroom_id):
        """Warm one room's optional member cache without blocking inbound."""
        cache = self._group_members_cache
        lock = self._group_members_cache_lock
        try:
            response = self._request_json(
                "/api/v1/group-members",
                query={"chatroomId": chatroom_id},
            )
            if not response.get("success") or not isinstance(
                response.get("members"), list
            ):
                raise ValueError("WeFlow 群成员响应无效")
            members = {}
            for row in response["members"]:
                if not isinstance(row, dict):
                    continue
                wxid = str(row.get("wxid") or "").strip()
                if not wxid:
                    continue
                members[wxid] = {
                    key: str(row.get(key) or "").strip()
                    for key in (
                        "displayName",
                        "nickname",
                        "remark",
                        "groupNickname",
                        "avatarUrl",
                    )
                }
            with lock:
                cache[chatroom_id] = (
                    time.monotonic() + _GROUP_MEMBERS_CACHE_SECONDS,
                    members,
                )
            logger.info(
                "后台刷新 WeFlow 群成员缓存 chatroom=%s count=%d from_cache=%s",
                chatroom_id,
                len(members),
                bool(response.get("fromCache")),
            )
        except Exception as exc:
            # Fail open: sender identity from message history remains valid;
            # only the richer name/avatar data is temporarily unavailable.
            with lock:
                cache[chatroom_id] = (
                    time.monotonic() + _GROUP_MEMBERS_FAILURE_CACHE_SECONDS,
                    {},
                )
            logger.debug(
                "后台读取 WeFlow 群成员失败 chatroom=%s: %s",
                chatroom_id,
                exc,
            )
        finally:
            with lock:
                self._group_members_refreshing.discard(chatroom_id)

    def _remember_seen(self, key):
        if key in self._seen:
            return
        self._seen.add(key)
        self._seen_order.append(key)
        while len(self._seen_order) > _DEDUPLICATION_LIMIT:
            self._seen.discard(self._seen_order.popleft())

    @staticmethod
    def _attach_quote_metadata(raw, history_message, rawid=""):
        """Copy compact quote metadata from a history row into the inbound raw payload."""
        if not isinstance(raw, dict) or not isinstance(history_message, dict):
            return False
        quote = history_message.get("quote")
        if not isinstance(quote, dict):
            return False
        # Keep the provider's stable field names while dropping any accidental
        # non-JSON values from a future WeFlow response.
        compact_quote = {
            key: value
            for key, value in quote.items()
            if isinstance(key, str)
            and isinstance(value, (str, int, float, bool, type(None)))
        }
        if not compact_quote:
            return False
        if not compact_quote.get("targetCreateTime"):
            raw_content = str(history_message.get("rawContent") or "").strip()
            if raw_content.startswith("<"):
                try:
                    root = ET.fromstring(raw_content)
                    create_time = root.findtext(".//refermsg/createtime")
                    if create_time and create_time.strip():
                        compact_quote["targetCreateTime"] = create_time.strip()
                except (ET.ParseError, ValueError):
                    logger.debug(
                        "WeFlow 引用 XML 解析失败 rawid=%s",
                        rawid,
                        exc_info=True,
                    )
        raw["quote"] = compact_quote
        reply_id = history_message.get("replyToMessageId")
        if reply_id is not None and str(reply_id).strip():
            raw["replyToMessageId"] = str(reply_id).strip()
        logger.info(
            "WeFlow 引用消息已补全 rawid=%s reply_to=%s quote_sender=%s quote_content=%r",
            rawid,
            raw.get("replyToMessageId", ""),
            compact_quote.get("sender", ""),
            str(compact_quote.get("content", ""))[:120],
        )
        return True

    @staticmethod
    def _enrich_quote_targets(messages):
        """Attach authoritative target direction/time from the same history page."""
        if not isinstance(messages, list):
            return
        by_server_id = {
            str(message.get("serverId", "")): message
            for message in messages
            if isinstance(message, dict) and message.get("serverId") is not None
        }
        for message in messages:
            if not isinstance(message, dict):
                continue
            quote = message.get("quote")
            if not isinstance(quote, dict):
                continue
            target_id = str(
                message.get("replyToMessageId")
                or quote.get("platformMessageId")
                or quote.get("messageId")
                or ""
            ).strip()
            target = by_server_id.get(target_id)
            if not isinstance(target, dict):
                continue
            if target.get("createTime") is not None:
                quote["targetCreateTime"] = target["createTime"]
            if target.get("isSend") is not None:
                quote["targetIsSend"] = target["isSend"]

    def _media_for_message(
        self,
        session_id,
        rawid,
        *,
        attempts=1,
        retry_seconds=0,
        history_type="媒体",
        strict=True,
        include_media=True,
        bypass_cache=False,
    ):
        if not session_id:
            return None
        rawid = str(rawid)
        cached = None
        if not bypass_cache:
            cached = self._history_cache_get(session_id, rawid, include_media)
        if cached is not None:
            logger.debug(
                "WeFlow %s历史命中缓存 rawid=%s media=%s",
                history_type,
                rawid,
                include_media,
            )
            return cached
        last_match = None
        for attempt in range(1, attempts + 1):
            # 普通文本每条都需要历史详情来恢复引用与群成员真 ID，但
            # WeFlow 在 limit>=50 时响应会明显变慢。先查最近 10 条，绝大
            # 多数实时 SSE 消息可在数毫秒内命中；仅在高流量积压导致目标
            # 不在最近 10 条时，才扩大到 100 条兜底。媒体查询仍直接使用
            # 100 条，因为它还需要 WeFlow 完成 mediaUrl 导出。
            limits = (
                (_HISTORY_FALLBACK_LIMIT,)
                if include_media
                else (_HISTORY_FAST_LIMIT, _HISTORY_FALLBACK_LIMIT)
            )
            attempt_match = None
            for history_limit in limits:
                query = {
                    "talker": session_id,
                    "limit": history_limit,
                }
                if include_media:
                    query["media"] = 1
                request_started_at = time.monotonic()
                response = self._request_json(_MESSAGES_PATH, query=query)
                request_elapsed = time.monotonic() - request_started_at
                log = logger.info if request_elapsed >= 0.1 else logger.debug
                log(
                    "WeFlow history 请求耗时 %.3fs rawid=%s media=%s "
                    "limit=%d attempt=%d/%d",
                    request_elapsed,
                    rawid,
                    include_media,
                    history_limit,
                    attempt,
                    attempts,
                )
                if not response.get("success"):
                    logger.debug(
                        "WeFlow %s历史查询失败 rawid=%s limit=%d "
                        "attempt=%d/%d",
                        history_type,
                        rawid,
                        history_limit,
                        attempt,
                        attempts,
                    )
                    if strict:
                        raise RuntimeError("WeFlow 获取消息历史失败")
                    return None
                messages = response.get("messages")
                if not isinstance(messages, list):
                    logger.debug(
                        "WeFlow %s历史响应缺少 messages rawid=%s limit=%d "
                        "attempt=%d/%d",
                        history_type,
                        rawid,
                        history_limit,
                        attempt,
                        attempts,
                    )
                    if strict:
                        raise ValueError("WeFlow 消息历史缺少 messages 数组")
                    return None

                self._enrich_quote_targets(messages)
                self._history_cache_store(session_id, messages, include_media)

                candidates = [
                    {
                        "serverId": message.get("serverId"),
                        "localType": message.get("localType"),
                    }
                    for message in messages
                    if isinstance(message, dict)
                ]
                logger.debug(
                    "查询 WeFlow %s历史 rawid=%s limit=%d attempt=%d/%d "
                    "candidates=%s",
                    history_type,
                    rawid,
                    history_limit,
                    attempt,
                    attempts,
                    candidates,
                )
                for message in messages:
                    if not isinstance(message, dict):
                        continue
                    if str(message.get("serverId", "")) == rawid:
                        attempt_match = last_match = message
                        logger.debug(
                            "匹配 WeFlow %s历史 rawid=%s serverId=%s "
                            "localType=%r media_type=%r has_url=%s limit=%d",
                            history_type,
                            rawid,
                            message.get("serverId"),
                            message.get("localType"),
                            self._media_type(message),
                            bool(message.get("mediaUrl")),
                            history_limit,
                        )
                        if message.get("mediaUrl"):
                            return message
                        break
                if attempt_match is not None:
                    break

                logger.debug(
                    "WeFlow %s历史未匹配 rawid=%s limit=%d attempt=%d/%d "
                    "candidates=%s",
                    history_type,
                    rawid,
                    history_limit,
                    attempt,
                    attempts,
                    candidates,
                )

            if attempt_match is not None:
                logger.debug(
                    "WeFlow %s媒体 URL 尚未就绪 rawid=%s serverId=%s "
                    "localType=%r attempt=%d/%d",
                    history_type,
                    rawid,
                    attempt_match.get("serverId"),
                    attempt_match.get("localType"),
                    attempt,
                    attempts,
                )
            if attempt < attempts and self.stop_event.wait(retry_seconds):
                break
        return last_match

    def _history_cache_get(self, session_id, rawid, include_media):
        """Look up a recent history row without blocking the SSE receiver."""
        cache = getattr(self, "_history_cache", None)
        if cache is None:
            cache = self._history_cache = {}
        lock = getattr(self, "_history_cache_lock", None)
        if lock is None:
            lock = self._history_cache_lock = threading.RLock()
        session_id = str(session_id or "").strip()
        rawid = str(rawid or "").strip()
        if not session_id or not rawid:
            return None
        modes = (True, False) if not include_media else (True,)
        now = time.monotonic()
        with lock:
            for mode in modes:
                entry = cache.get((session_id, mode))
                if not entry:
                    continue
                expires, rows = entry
                if expires <= now:
                    cache.pop((session_id, mode), None)
                    continue
                row = rows.get(rawid)
                if not isinstance(row, dict):
                    continue
                # A media lookup must not consume a cached row whose export
                # URL was not ready yet; let the documented retry path query
                # WeFlow again in that case.
                if include_media and not row.get("mediaUrl"):
                    continue
                return dict(row)
        return None

    def _history_cache_store(self, session_id, messages, include_media):
        if not isinstance(messages, list):
            return
        rows = {}
        for message in messages:
            if not isinstance(message, dict):
                continue
            server_id = str(message.get("serverId") or "").strip()
            if server_id:
                rows[server_id] = dict(message)
        if not rows:
            return
        cache = getattr(self, "_history_cache", None)
        if cache is None:
            cache = self._history_cache = {}
        lock = getattr(self, "_history_cache_lock", None)
        if lock is None:
            lock = self._history_cache_lock = threading.RLock()
        with lock:
            cache[(str(session_id).strip(), bool(include_media))] = (
                time.monotonic() + _HISTORY_ROWS_CACHE_SECONDS,
                rows,
            )

    def _voice_for_message(self, session_id, rawid):
        """Return the documented history row matching one voice SSE rawid."""
        return self._media_for_message(
            session_id,
            rawid,
            attempts=_VOICE_HISTORY_ATTEMPTS,
            retry_seconds=_VOICE_HISTORY_RETRY_SECONDS,
            history_type="语音",
            strict=False,
        )

    def _emoji_for_message(self, session_id, rawid):
        """Return the documented history row matching one emoji SSE rawid."""
        return self._media_for_message(
            session_id,
            rawid,
            attempts=_EMOJI_HISTORY_ATTEMPTS,
            retry_seconds=_EMOJI_HISTORY_RETRY_SECONDS,
            history_type="表情",
            strict=False,
        )

    def _download_media(self, message, rawid, expected_type=None):
        media_url = str(message.get("mediaUrl", "") or "").strip()
        if not media_url:
            raise ValueError("WeFlow 媒体消息缺少 mediaUrl")
        absolute_url = urljoin(f"{self.api_url}/", media_url)
        logger.debug(
            "开始下载 WeFlow 媒体 rawid=%s expected_type=%s media_type=%r url=%s",
            rawid,
            expected_type,
            self._media_type(message),
            absolute_url,
        )
        request = self._request(absolute_url, accept="*/*")
        limit = FILE_MAX_BYTES if expected_type == "file" else MAX_MEDIA_BYTES
        response = self._opener.open(request, timeout=MEDIA_DOWNLOAD_TIMEOUT_SECONDS)
        source_name = str(message.get("mediaFileName", "") or "").strip()
        if not source_name:
            source_name = os.path.basename(urlsplit(absolute_url).path)
        suffix_hint = os.path.splitext(source_name)[1][:16] or ".bin"
        safe_id = _SAFE_FILE_COMPONENT.sub("_", rawid)[:80] or "media"
        os.makedirs(IMAGE_SAVE_DIR, exist_ok=True)
        temporary_path = None
        try:
            declared_size = response.headers.get("Content-Length")
            if declared_size and int(declared_size) > limit:
                raise ValueError("WeFlow 媒体超过尺寸上限")
            fd, temporary_path = tempfile.mkstemp(
                prefix=f"weflow_{safe_id}_",
                suffix=suffix_hint,
                dir=IMAGE_SAVE_DIR,
            )
            with os.fdopen(fd, "wb") as stream:
                first = response.read(min(65536, limit + 1))
                if not first:
                    raise ValueError("WeFlow 媒体响应为空")
                stream.write(first)
                size = len(first)
                while size <= limit:
                    chunk = response.read(min(65536, limit - size + 1))
                    if not chunk:
                        break
                    stream.write(chunk)
                    size += len(chunk)
                if size > limit:
                    raise ValueError("WeFlow 媒体超过尺寸上限")
        except BaseException:
            with contextlib.suppress(OSError):
                if temporary_path:
                    os.unlink(temporary_path)
            raise
        finally:
            response.close()

        try:
            with open(temporary_path, "rb") as stream:
                header = stream.read(16)
            image_suffix = self._image_suffix(header)
            suffix = image_suffix or suffix_hint
            if suffix != suffix_hint:
                corrected_path = f"{temporary_path}{suffix}"
                os.replace(temporary_path, corrected_path)
                temporary_path = corrected_path
        except Exception:
            with contextlib.suppress(OSError):
                if temporary_path:
                    os.unlink(temporary_path)
            raise
        logger.debug(
            "完成下载 WeFlow 媒体 rawid=%s expected_type=%s bytes=%d suffix=%s path=%s",
            rawid,
            expected_type,
            os.path.getsize(temporary_path),
            suffix,
            temporary_path,
        )
        if expected_type == "voice":
            return temporary_path, "voice"
        if expected_type == "emoji":
            return temporary_path, "emoji"
        if expected_type == "file":
            return temporary_path, "file"
        return temporary_path, "image" if image_suffix else "file"

    @staticmethod
    def _is_voice_marker(content):
        content = str(content or "").strip()
        if not content:
            return True
        if _VOICE_MARKER.fullmatch(content):
            return True
        if not re.search(r"<msg\b", content, re.IGNORECASE):
            return False
        return bool(re.search(r"(?:voice|audio|语音)", content, re.IGNORECASE))

    @staticmethod
    def _file_name_from_message(message, content=""):
        """Return the title of a WeChat app-message file (appmsg type 6)."""
        candidates = [content]
        if isinstance(message, dict):
            candidates.extend(
                message.get(key)
                for key in ("content", "rawContent", "raw_content", "xml")
            )
        for candidate in candidates:
            text = str(candidate or "").strip()
            if not text.startswith("<"):
                continue
            try:
                root = ET.fromstring(text)
            except ET.ParseError:
                continue
            for appmsg in root.iter():
                if str(appmsg.tag).rsplit("}", 1)[-1].casefold() != "appmsg":
                    continue
                fields = {
                    str(child.tag).rsplit("}", 1)[-1].casefold(): str(child.text or "").strip()
                    for child in appmsg
                }
                if fields.get("type") != "6":
                    continue
                title = fields.get("title", "")
                if title:
                    return title
        return ""

    @classmethod
    def _is_plain_private_text(cls, payload, content, chat_type):
        """判断无需访问历史接口的普通私聊文本。"""
        if chat_type != "private" or not isinstance(payload, dict):
            return False
        if payload.get("mediaUrl") or cls._media_type(payload) not in (None, ""):
            return False
        text = str(content or "").strip()
        if not text:
            return False
        # WeFlow 的图片、文件等占位消息通常使用括号标记或 XML；这些消息
        # 继续进入原媒体查询路径。这里只跳过明确的普通文本。
        return not text.startswith(("[", "【", "(", "（", "<"))

    @classmethod
    def _is_plain_text_message(cls, payload, content):
        """Return True for text that cannot require media history export."""
        if not isinstance(payload, dict):
            return False
        if payload.get("mediaUrl") or cls._media_type(payload) not in (None, ""):
            return False
        text = str(content or "").strip()
        if not text:
            return False
        # Bracketed placeholders, XML appmsg rows, and explicit media markers
        # remain on the media path.  Ordinary group text only needs the cheap
        # sender/quote history lookup.
        return not text.startswith(("[", "【", "(", "（", "<"))

    @staticmethod
    def _is_chat_record_marker(content):
        compact = re.sub(r"\s+", "", str(content or "")).casefold()
        return compact in {
            "[聊天记录]",
            "【聊天记录】",
            "[合并聊天记录]",
            "【合并聊天记录】",
        }

    @staticmethod
    def _xml_local_name(element):
        return str(getattr(element, "tag", "")).rsplit("}", 1)[-1].casefold()

    @classmethod
    def _xml_direct_child(cls, element, name):
        expected = str(name).casefold()
        if element is None:
            return None
        for child in list(element):
            if cls._xml_local_name(child) == expected:
                return child
        return None

    @classmethod
    def _xml_direct_text(cls, element, name):
        child = cls._xml_direct_child(element, name)
        return str(child.text or "").strip() if child is not None else ""

    @staticmethod
    def _compact_record_text(value, limit=_CHAT_RECORD_ITEM_TEXT_CHARS):
        text = re.sub(r"\s+", " ", str(value or "")).strip()
        if len(text) > limit:
            return text[:limit].rstrip() + "..."
        return text

    @staticmethod
    def _format_record_size(value):
        try:
            size = max(0, int(str(value or "0")))
        except (TypeError, ValueError):
            return ""
        if not size:
            return ""
        if size < 1024:
            return f"{size}B"
        if size < 1024 * 1024:
            return f"{size / 1024:.1f}KB"
        return f"{size / (1024 * 1024):.2f}MB"

    @classmethod
    def _record_link_url(cls, item):
        """Extract a real HTTP(S) target from a forwarded link item."""
        url_tags = ("streamweburl", "weburl", "linkurl", "link", "url")
        descendants = list(item.iter())
        for tag_name in url_tags:
            for node in descendants:
                if cls._xml_local_name(node) != tag_name:
                    continue
                candidate = cls._compact_record_text(node.text, 4096)
                parsed = urlsplit(candidate)
                if parsed.scheme.casefold() in {"http", "https"} and parsed.netloc:
                    return candidate
        return ""

    @classmethod
    def _record_info_from_outer_xml(cls, raw_xml):
        text = str(raw_xml or "").strip()
        if not text or len(text) > _CHAT_RECORD_MAX_XML_CHARS:
            return None
        # Group history prefixes rawContent with the sender's real ID. Locate
        # the XML boundary without assuming that real IDs start with wxid_.
        if not text.startswith("<"):
            xml_start = re.search(r"<\?xml\b|<msg\b", text, re.IGNORECASE)
            if xml_start is None:
                return None
            text = text[xml_start.start():]
        try:
            root = ET.fromstring(text)
        except ET.ParseError:
            return None
        appmsg = next(
            (node for node in root.iter() if cls._xml_local_name(node) == "appmsg"),
            None,
        )
        if appmsg is None or cls._xml_direct_text(appmsg, "type") != "19":
            return None
        recorditem = cls._xml_direct_child(appmsg, "recorditem")
        record_xml = str(recorditem.text or "").strip() if recorditem is not None else ""
        if not record_xml or len(record_xml) > _CHAT_RECORD_MAX_XML_CHARS:
            return None
        try:
            record_root = ET.fromstring(record_xml)
        except ET.ParseError:
            return None
        if cls._xml_local_name(record_root) == "recordinfo":
            return record_root
        return next(
            (
                node
                for node in record_root.iter()
                if cls._xml_local_name(node) == "recordinfo"
            ),
            None,
        )

    @classmethod
    def _nested_record_info(cls, item):
        recordxml = cls._xml_direct_child(item, "recordxml")
        if recordxml is None:
            return None
        for child in list(recordxml):
            if cls._xml_local_name(child) == "recordinfo":
                return child
        record_text = str(recordxml.text or "").strip()
        if not record_text or len(record_text) > _CHAT_RECORD_MAX_XML_CHARS:
            return None
        try:
            root = ET.fromstring(record_text)
        except ET.ParseError:
            return None
        if cls._xml_local_name(root) == "recordinfo":
            return root
        return next(
            (node for node in root.iter() if cls._xml_local_name(node) == "recordinfo"),
            None,
        )

    @classmethod
    def _record_items(cls, record_info):
        datalist = cls._xml_direct_child(record_info, "datalist")
        if datalist is None:
            return []
        return [
            child
            for child in list(datalist)
            if cls._xml_local_name(child) == "dataitem"
        ]

    @classmethod
    def _record_item_body(cls, item):
        data_type = str(item.attrib.get("datatype", "")).strip()
        title = cls._compact_record_text(cls._xml_direct_text(item, "datatitle"))
        description = cls._compact_record_text(cls._xml_direct_text(item, "datadesc"))
        data_format = cls._compact_record_text(cls._xml_direct_text(item, "datafmt"), 32)
        size = cls._format_record_size(cls._xml_direct_text(item, "datasize"))

        voice_match = re.match(
            r"^[\[【（(]\s*语音\s*[\]】）)]\s*(?:(\d+(?:\.\d+)?)\s*(?:秒|\"))?",
            description,
            re.IGNORECASE,
        )
        if voice_match or data_type == "3":
            duration = voice_match.group(1) if voice_match else ""
            if not duration:
                raw_duration = cls._xml_direct_text(item, "duration")
                try:
                    numeric_duration = float(raw_duration)
                    if numeric_duration > 1000:
                        numeric_duration /= 1000
                    duration = f"{numeric_duration:g}"
                except (TypeError, ValueError):
                    duration = ""
            label = f"语音 {duration}秒" if duration else "语音"
            return f"[{label}，原始音频不可提取]"
        if data_type == "2":
            detail = f"，{size}" if size else ""
            return f"[图片{detail}]"
        if data_type in {"7", "8"} or (title and data_format):
            file_name = title or (f"未命名.{data_format}" if data_format else "未命名文件")
            detail = f"，{size}" if size else ""
            return f"[文件：{file_name}{detail}]"
        if data_type == "4":
            return f"[视频{('：' + title) if title else ''}]"
        if data_type == "5":
            web_item = cls._xml_direct_child(item, "weburlitem")
            link_title = title or cls._compact_record_text(
                cls._xml_direct_text(web_item, "title")
            )
            link_description = description or cls._compact_record_text(
                cls._xml_direct_text(web_item, "desc")
            )
            label = f"[链接：{link_title}]" if link_title else "[链接]"
            link_url = cls._record_link_url(item)
            return " ".join(
                part for part in (label, link_description, link_url) if part
            )
        if data_type == "6":
            return f"[位置{('：' + title) if title else ''}]"
        if data_type == "19" or cls._xml_direct_child(item, "appbranditem") is not None:
            return f"[小程序{('：' + title) if title else ''}]"
        finder_live = cls._xml_direct_child(item, "finderLive") is not None
        if (
            data_type in {"22", "23"}
            or cls._xml_direct_child(item, "finderFeed") is not None
            or finder_live
        ):
            kind = "视频号直播" if data_type == "23" or finder_live else "视频号"
            label = f"[{kind}{('：' + title) if title else ''}]"
            return f"{label} {description}".strip()
        if description:
            return description
        if title:
            return title
        return f"[不支持的消息类型：{data_type or '未知'}]"

    @classmethod
    def _render_record_info(cls, record_info):
        lines = []
        state = {"items": 0, "truncated": False}

        def walk(current, prefix="", depth=0):
            if depth > _CHAT_RECORD_MAX_DEPTH:
                state["truncated"] = True
                return
            for index, item in enumerate(cls._record_items(current), 1):
                if state["items"] >= _CHAT_RECORD_MAX_ITEMS:
                    state["truncated"] = True
                    return
                state["items"] += 1
                item_number = f"{prefix}{index}"
                source = cls._compact_record_text(
                    cls._xml_direct_text(item, "sourcename"), 120
                ) or "未知发送者"
                source_time = cls._compact_record_text(
                    cls._xml_direct_text(item, "sourcetime"), 40
                )
                identity = f"{source}（{source_time}）" if source_time else source
                nested = cls._nested_record_info(item)
                if nested is not None:
                    nested_items = cls._record_items(nested)
                    nested_title = (
                        cls._compact_record_text(
                            cls._xml_direct_text(item, "datatitle"), 240
                        )
                        or cls._compact_record_text(
                            cls._xml_direct_text(nested, "title"), 240
                        )
                        or "未命名聊天记录"
                    )
                    lines.append(
                        f"{item_number}. {identity}：[嵌套合并聊天记录] "
                        f"{nested_title}（{len(nested_items)}条）"
                    )
                    walk(nested, prefix=f"{item_number}.", depth=depth + 1)
                else:
                    lines.append(
                        f"{item_number}. {identity}：{cls._record_item_body(item)}"
                    )

        walk(record_info)
        if state["truncated"]:
            lines.append("[其余聊天记录因数量或嵌套层级过多已省略]")
        return lines

    @classmethod
    def _render_chat_record(cls, message):
        if not isinstance(message, dict):
            return None
        record_info = None
        for field in ("rawContent", "raw_content", "xml", "content"):
            record_info = cls._record_info_from_outer_xml(message.get(field))
            if record_info is not None:
                break
        if record_info is None:
            return None
        items = cls._record_items(record_info)
        title = cls._compact_record_text(
            cls._xml_direct_text(record_info, "title"), 240
        ) or "未命名聊天记录"
        lines = [f"[合并聊天记录] {title}（{len(items)}条）"]
        lines.extend(cls._render_record_info(record_info))
        rendered = "\n".join(lines)
        if len(rendered) > _CHAT_RECORD_MAX_OUTPUT_CHARS:
            rendered = (
                rendered[:_CHAT_RECORD_MAX_OUTPUT_CHARS].rstrip()
                + "\n[其余聊天记录内容过长已省略]"
            )
        return rendered

    @staticmethod
    def _is_emoji_marker(content):
        content = str(content or "").strip()
        if not content:
            return False
        if _EMOJI_MARKER.fullmatch(content):
            return True
        if not (content.startswith("<") and ">" in content):
            return False
        try:
            root = ET.fromstring(content)
        except ET.ParseError:
            return bool(re.search(r"<(?:emoji|sticker)\b", content, re.IGNORECASE))
        for element in root.iter():
            tag = str(element.tag).rsplit("}", 1)[-1]
            if WeFlowListener._is_emoji_type_value(tag):
                return True
            for name, value in element.attrib.items():
                if WeFlowListener._is_emoji_type_value(name):
                    return True
                if WeFlowListener._is_emoji_type_value(value):
                    return True
        return False

    @staticmethod
    def _is_emoji_type_value(value):
        compact = re.sub(r"[\s_.:/-]+", "", str(value or "")).casefold()
        return compact in _EMOJI_TYPE_NAMES or compact == "47"

    @staticmethod
    def _is_voice_type_value(value):
        compact = re.sub(r"[\s_.:/-]+", "", str(value or "")).casefold()
        return compact in _VOICE_TYPE_NAMES

    @classmethod
    def _media_type(cls, message):
        if not isinstance(message, dict):
            return None
        for field in _MEDIA_TYPE_FIELDS:
            value = message.get(field)
            if value not in (None, ""):
                return value
        return None

    @classmethod
    def _is_emoji_media(cls, message):
        if not isinstance(message, dict):
            return False
        if any(
            cls._is_emoji_type_value(message.get(field))
            for field in _MEDIA_TYPE_FIELDS
        ):
            return True
        for field in ("message", "media"):
            nested = message.get(field)
            if isinstance(nested, dict) and cls._is_emoji_media(nested):
                return True
        media_url = str(message.get("mediaUrl", "") or "").casefold()
        return bool(re.search(r"(?:^|[/_.-])(?:emoji|sticker)s?(?:[/_.-]|$)", media_url))

    @classmethod
    def _is_voice_media(cls, message):
        if not isinstance(message, dict):
            return False
        if any(
            cls._is_voice_type_value(message.get(field))
            for field in _MEDIA_TYPE_FIELDS
        ):
            return True
        for field in ("message", "media"):
            nested = message.get(field)
            if isinstance(nested, dict) and cls._is_voice_media(nested):
                return True
        media_url = str(message.get("mediaUrl", "") or "").casefold()
        return bool(re.search(r"(?:^|[/_.-])(?:voice|audio)(?:[/_.-]|$)", media_url))

    @staticmethod
    def _image_suffix(header):
        for magic, suffix in _IMAGE_MAGIC.items():
            if not header.startswith(magic):
                continue
            if magic == b"RIFF" and header[8:12] != b"WEBP":
                continue
            return suffix
        return None

    def _request_json(self, path, query=None):
        response = self._opener.open(
            self._request(path, query=query, accept="application/json"),
            timeout=MEDIA_DOWNLOAD_TIMEOUT_SECONDS,
        )
        try:
            raw = response.read(MAX_MEDIA_BYTES + 1)
        finally:
            response.close()
        if len(raw) > MAX_MEDIA_BYTES:
            raise ValueError("WeFlow API 响应超过尺寸上限")
        result = json.loads(raw.decode("utf-8"))
        if not isinstance(result, dict):
            raise ValueError("WeFlow API 响应不是 JSON 对象")
        return result

    def _request(self, path_or_url, query=None, accept="application/json"):
        if str(path_or_url).startswith(("http://", "https://")):
            url = str(path_or_url)
        else:
            url = f"{self.api_url}/{str(path_or_url).lstrip('/')}"
        if query:
            separator = "&" if "?" in url else "?"
            url = f"{url}{separator}{urlencode(query)}"
        if _url_origin(url) != self._api_origin:
            raise ValueError("WeFlow URL changed origin; refusing to forward token")
        return Request(
            url,
            headers={
                "Authorization": f"Bearer {self.api_token}",
                "Accept": accept,
            },
            method="GET",
        )
