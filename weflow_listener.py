"""Receive WeChat messages from the documented WeFlow HTTP/SSE API."""

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
from urllib.request import Request, build_opener

from chat_name_utils import normalize_chat_name
from config import (
    IMAGE_AUTO_DOWNLOAD,
    IMAGE_SAVE_DIR,
    MAX_MEDIA_BYTES,
    MEDIA_DOWNLOAD_TIMEOUT_SECONDS,
    WEFLOW_API_TOKEN,
    WEFLOW_API_URL,
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


class WeFlowListener:
    """Maintain the WeFlow SSE connection and emit normalized inbound events."""

    def __init__(
        self,
        target_chats=None,
        callback=None,
        stop_event=None,
        api_url=WEFLOW_API_URL,
        api_token=WEFLOW_API_TOKEN,
        opener=None,
        outgoing_registry=None,
    ):
        self.api_url = str(api_url or "").rstrip("/")
        self.api_token = str(api_token or "").strip()
        if not self.api_url:
            raise ValueError("WEFLOW_API_URL 不能为空")
        if not self.api_token:
            raise ValueError("WEFLOW_API_TOKEN 为必填配置")
        self.callback = callback
        self.stop_event = stop_event or threading.Event()
        self._opener = opener or build_opener()
        self.outgoing_registry = outgoing_registry
        self._targets = self._normalize_targets(
            WX_TARGET_CHATS if target_chats is None else target_chats
        )
        self._seen = set()
        self._seen_order = deque()
        self._response = None
        self._response_lock = threading.Lock()
        self._thread = None
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
        with self._response_lock:
            response = self._response
        if response is not None:
            try:
                response.close()
            except Exception:
                logger.debug("关闭 WeFlow SSE 响应失败", exc_info=True)
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout)
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
                    self._dispatch_sse_frame(event_name, data_lines)
                    event_name, data_lines = "", []
                elif line.startswith(":"):
                    continue
                elif line.startswith("event:"):
                    event_name = line[6:].lstrip(" ")
                elif line.startswith("data:"):
                    data_lines.append(line[5:].lstrip(" "))
            if data_lines:
                self._dispatch_sse_frame(event_name, data_lines)
            if not self.stop_event.is_set():
                raise ConnectionError("WeFlow SSE 流已结束")
        finally:
            with self._response_lock:
                if self._response is response:
                    self._response = None
            response.close()

    def _dispatch_sse_frame(self, event_name, data_lines):
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
        if event_name == "message.revoke":
            return
        if event_name != "message.new":
            logger.debug("忽略未知 WeFlow 事件 event=%r", event_name)
            return

        rawid = str(payload.get("rawid", "")).strip()
        if not rawid:
            logger.warning("忽略缺少 rawid 的 WeFlow 消息")
            return
        deduplication_key = (event_name, rawid)
        if deduplication_key in self._seen:
            return
        if self._payload_is_outgoing(payload):
            self._remember_seen(deduplication_key)
            logger.debug("忽略 WeFlow 明确标记的自发消息 rawid=%s", rawid)
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
        empty_content = not content.strip()
        voice_marker = self._is_voice_marker(content)
        voice_media_hint = self._is_voice_media(payload)
        voice_hint = voice_media_hint or (voice_marker and not empty_content)
        emoji_hint = self._is_emoji_marker(content) or self._is_emoji_media(payload)
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
        if voice_hint or empty_content:
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
                media = payload if payload.get("mediaUrl") else self._voice_for_message(
                    session_id,
                    rawid,
                )
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
                media = payload if payload.get("mediaUrl") else self._emoji_for_message(
                    session_id,
                    rawid,
                )
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
        elif IMAGE_AUTO_DOWNLOAD:
            try:
                media = self._media_for_message(session_id, rawid)
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

        try:
            timestamp = float(payload.get("timestamp"))
        except (TypeError, ValueError):
            logger.warning("忽略时间戳无效的 WeFlow 消息 rawid=%s", rawid)
            return
        event = _InboundEvent(
            group=chat_name,
            content=content,
            timestamp=timestamp,
            raw=raw,
            message_type=message_type,
            sender=str(payload.get("sourceName", "") or "").strip(),
            chat_type=chat_type,
            avatar_url=str(payload.get("avatarUrl", "") or "").strip(),
        )
        if self.callback is not None:
            result = self.callback(event)
            if isinstance(result, dict) and not result.get("success"):
                raise RuntimeError(result.get("error") or "入站消息持久化失败")
        self._remember_seen(deduplication_key)
        logger.info(
            "收到 WeFlow 消息 chat=%s session=%s chat_type=%s sender=%s "
            "type=%s rawid=%s",
            chat_name,
            session_id,
            chat_type,
            event.sender,
            message_type,
            rawid,
        )

    def _matching_target(self, session_id, display_name, chat_type):
        candidate_keys = {
            normalize_chat_name(session_id),
            normalize_chat_name(display_name),
        } - {""}
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

    def _remember_seen(self, key):
        if key in self._seen:
            return
        self._seen.add(key)
        self._seen_order.append(key)
        while len(self._seen_order) > _DEDUPLICATION_LIMIT:
            self._seen.discard(self._seen_order.popleft())

    def _media_for_message(
        self,
        session_id,
        rawid,
        *,
        attempts=1,
        retry_seconds=0,
        history_type="媒体",
        strict=True,
    ):
        if not session_id:
            return None
        rawid = str(rawid)
        last_match = None
        for attempt in range(1, attempts + 1):
            response = self._request_json(
                _MESSAGES_PATH,
                query={
                    "talker": session_id,
                    "limit": 100,
                    "media": 1,
                },
            )
            if not response.get("success"):
                logger.debug(
                    "WeFlow %s历史查询失败 rawid=%s attempt=%d/%d",
                    history_type,
                    rawid,
                    attempt,
                    attempts,
                )
                if strict:
                    raise RuntimeError("WeFlow 获取消息历史失败")
                return None
            messages = response.get("messages")
            if not isinstance(messages, list):
                logger.debug(
                    "WeFlow %s历史响应缺少 messages rawid=%s attempt=%d/%d",
                    history_type,
                    rawid,
                    attempt,
                    attempts,
                )
                if strict:
                    raise ValueError("WeFlow 消息历史缺少 messages 数组")
                return None

            candidates = [
                {
                    "serverId": message.get("serverId"),
                    "localType": message.get("localType"),
                }
                for message in messages
                if isinstance(message, dict)
            ]
            logger.debug(
                "查询 WeFlow %s历史 rawid=%s attempt=%d/%d candidates=%s",
                history_type,
                rawid,
                attempt,
                attempts,
                candidates,
            )
            attempt_match = None
            for message in messages:
                if not isinstance(message, dict):
                    continue
                if str(message.get("serverId", "")) == rawid:
                    attempt_match = last_match = message
                    logger.debug(
                        "匹配 WeFlow %s历史 rawid=%s serverId=%s "
                        "localType=%r media_type=%r has_url=%s",
                        history_type,
                        rawid,
                        message.get("serverId"),
                        message.get("localType"),
                        self._media_type(message),
                        bool(message.get("mediaUrl")),
                    )
                    if message.get("mediaUrl"):
                        return message
                    break
            if attempt_match is None:
                logger.debug(
                    "WeFlow %s历史未匹配 rawid=%s attempt=%d/%d "
                    "candidates=%s",
                    history_type,
                    rawid,
                    attempt,
                    attempts,
                    candidates,
                )
            else:
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
        response = self._opener.open(
            request,
            timeout=MEDIA_DOWNLOAD_TIMEOUT_SECONDS,
        )
        try:
            declared_size = response.headers.get("Content-Length")
            if declared_size and int(declared_size) > MAX_MEDIA_BYTES:
                raise ValueError("WeFlow 媒体超过尺寸上限")
            chunks = []
            size = 0
            while True:
                chunk = response.read(min(65536, MAX_MEDIA_BYTES - size + 1))
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
                if size > MAX_MEDIA_BYTES:
                    raise ValueError("WeFlow 媒体超过尺寸上限")
            raw = b"".join(chunks)
        finally:
            response.close()
        if not raw:
            raise ValueError("WeFlow 媒体响应为空")

        image_suffix = self._image_suffix(raw[:16])
        source_name = str(message.get("mediaFileName", "") or "").strip()
        if not source_name:
            source_name = os.path.basename(urlsplit(absolute_url).path)
        suffix = image_suffix or os.path.splitext(source_name)[1][:16] or ".bin"
        safe_id = _SAFE_FILE_COMPONENT.sub("_", rawid)[:80] or "media"
        os.makedirs(IMAGE_SAVE_DIR, exist_ok=True)
        fd, temporary_path = tempfile.mkstemp(
            prefix=f"weflow_{safe_id}_",
            suffix=suffix,
            dir=IMAGE_SAVE_DIR,
        )
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(raw)
        except Exception:
            try:
                os.unlink(temporary_path)
            except OSError:
                pass
            raise
        logger.debug(
            "完成下载 WeFlow 媒体 rawid=%s expected_type=%s bytes=%d suffix=%s path=%s",
            rawid,
            expected_type,
            len(raw),
            suffix,
            temporary_path,
        )
        if expected_type == "voice":
            return temporary_path, "voice"
        if expected_type == "emoji":
            return temporary_path, "emoji"
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
        return Request(
            url,
            headers={
                "Authorization": f"Bearer {self.api_token}",
                "Accept": accept,
            },
            method="GET",
        )
