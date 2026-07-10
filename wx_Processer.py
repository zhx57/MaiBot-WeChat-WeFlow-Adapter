"""MaiBot message conversion and Router lifecycle.

This module deliberately has no wxauto imports.  UI work is submitted to the
single UI thread owned by :mod:`wx_Listener`.
"""

import asyncio
import base64
import binascii
import hashlib
import json
import logging
import os
import tempfile
import threading
import time
from concurrent.futures import CancelledError as FutureCancelledError
from concurrent.futures import TimeoutError as FutureTimeoutError
from pathlib import Path

from maim_message import (
    BaseMessageInfo,
    FormatInfo,
    GroupInfo,
    MessageBase,
    ReceiverInfo,
    RouteConfig,
    Router,
    Seg,
    SenderInfo,
    TargetConfig,
    UserInfo,
)

from config import (
    ID_MAP_FILE,
    IMAGE_RECOGNITION_ENABLED,
    MAIBOT_API_URL,
    MAX_MEDIA_BYTES,
    PLATFORM_ID,
    SEND_QUEUE_SIZE,
    WX_BOT_NICKNAME,
)

logger = logging.getLogger(__name__)

_IMAGE_MAGIC = {
    b"\x89PNG\r\n\x1a\n": ".png",
    b"\xff\xd8\xff": ".jpg",
    b"GIF87a": ".gif",
    b"GIF89a": ".gif",
    b"RIFF": ".webp",
}


class MessageProcessor:
    def __init__(self, platform=PLATFORM_ID, ui_submit=None, inbound_enabled=True,
                 outbound_enabled=True):
        self.platform = platform
        self.ui_submit = ui_submit
        self.inbound_enabled = inbound_enabled
        self.outbound_enabled = outbound_enabled
        self.ready_event = threading.Event()
        self.startup_error = None
        self._thread = None
        self._loop = None
        self._router_task = None
        self._send_task = None
        self._stopping = threading.Event()
        self._id_lock = threading.RLock()
        self._id_to_name = {}
        self._dead_letters = []
        self._load_id_map()

        route = RouteConfig(route_config={
            platform: TargetConfig(url=MAIBOT_API_URL, token=None, ssl_verify=None)
        })
        self.router = Router(route, custom_logger=logger)
        self.router.register_class_handler(self._handle_maibot_response)

    def set_ui_submit(self, submit):
        self.ui_submit = submit

    def register_target(self, name, chat_type):
        """Preload routable configured targets for replies arriving after restart."""
        if chat_type == "group":
            self._remember(self._stable_id("group", name), name, chat_type)
        elif chat_type == "private":
            self._remember(self._stable_id("private", name), name, chat_type)

    def start(self, timeout=20):
        """Start Router and wait until its websocket connection is usable."""
        if self._thread and self._thread.is_alive():
            return
        self.ready_event.clear()
        self.startup_error = None
        self._stopping.clear()
        self._thread = threading.Thread(target=self._router_thread, name="maibot-router", daemon=True)
        self._thread.start()
        if not self.ready_event.wait(timeout):
            self.stop()
            raise TimeoutError(f"Router 启动超过 {timeout} 秒")
        if self.startup_error:
            error = self.startup_error
            self.stop()
            raise RuntimeError("Router 启动失败") from error

    def _router_thread(self):
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        try:
            self._send_queue = asyncio.Queue(maxsize=SEND_QUEUE_SIZE)
            self._send_task = loop.create_task(self._process_send_queue())
            self._router_task = loop.create_task(self.router.run())
            loop.run_until_complete(self._wait_router_ready())
            self.ready_event.set()
            loop.run_until_complete(self._router_task)
        except BaseException as exc:
            if not self._stopping.is_set():
                self.startup_error = exc
                logger.exception("Router 线程异常")
            self.ready_event.set()
        finally:
            pending = [task for task in asyncio.all_tasks(loop) if not task.done()]
            for task in pending:
                task.cancel()
            if pending:
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            loop.run_until_complete(loop.shutdown_asyncgens())
            loop.close()
            self._loop = None

    async def _wait_router_ready(self):
        while not self._router_task.done():
            if self.router.check_connection(self.platform):
                return
            await asyncio.sleep(0.05)
        await self._router_task
        raise RuntimeError("Router 在连接就绪前退出")

    def stop(self, timeout=15):
        """Stop websocket, cancel workers, close loop, and join its thread."""
        self._stopping.set()
        loop = self._loop
        if loop and loop.is_running():
            async def shutdown():
                self.router._running = False
                await self.router.stop()

            future = asyncio.run_coroutine_threadsafe(shutdown(), loop)
            try:
                future.result(timeout=timeout)
            except FutureCancelledError:
                logger.warning("Router stop future 被底层取消，继续等待线程清理")
            except (FutureTimeoutError, RuntimeError):
                logger.error("Router 停止超时")
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout)
            if self._thread.is_alive():
                logger.error("Router 线程未在时限内退出")

    async def _process_send_queue(self):
        while True:
            item = await self._send_queue.get()
            try:
                receiver, kind, data, completion = item
                try:
                    await self._deliver_with_retry(receiver, kind, data)
                    if not completion.done():
                        completion.set_result(True)
                except BaseException as exc:
                    if not completion.done():
                        completion.set_exception(exc)
                    logger.error("发送队列项目最终失败: %s", exc)
            finally:
                self._send_queue.task_done()

    async def _deliver_with_retry(self, receiver, kind, data):
        last_error = None
        for attempt in range(1, 4):
            try:
                if not self.ui_submit:
                    raise RuntimeError("UI 命令执行器尚未绑定")
                ok = await asyncio.to_thread(self.ui_submit, "send", receiver, kind, data, 15)
                if ok is not True:
                    raise RuntimeError("wxauto 返回发送失败")
                logger.info("消息已发送 target=%s type=%s", receiver, kind)
                return
            except Exception as exc:
                last_error = exc
                logger.warning("发送失败 target=%s type=%s attempt=%d/3: %s",
                               receiver, kind, attempt, exc)
                if attempt < 3:
                    await asyncio.sleep(attempt)
        dead = {"receiver": receiver, "type": kind, "error": str(last_error), "time": time.time()}
        self._dead_letters.append(dead)
        logger.error("消息进入死信 target=%s type=%s error=%s", receiver, kind, last_error)
        raise RuntimeError("微信消息发送重试耗尽") from last_error

    async def _handle_maibot_response(self, message):
        if not self.outbound_enabled:
            return
        try:
            if isinstance(message, dict):
                message = MessageBase.from_dict(message)
            info = message.message_info
            receiver = self._resolve_receiver(info)
            if not receiver:
                raise ValueError("无法从 receiver_info 或兼容字段解析微信会话")
            message_id = getattr(info, "message_id", None)
            logger.info("收到 MaiBot 回复 id=%s segment_type=%s", message_id,
                        self._segment_value(message.message_segment, "type"))
            await self._process_segments(message.message_segment, receiver)
        except Exception:
            logger.exception("处理 MaiBot 回复失败")

    @staticmethod
    def _segment_value(segment, key, default=None):
        if isinstance(segment, dict):
            return segment.get(key, default)
        return getattr(segment, key, default)

    async def _process_segments(self, segment, receiver):
        seg_type = str(self._segment_value(segment, "type", "")).lower()
        data = self._segment_value(segment, "data")
        if seg_type == "seglist":
            for child in data or []:
                await self._process_segments(child, receiver)
            return
        if seg_type in {"reply", "notify"}:
            return
        if seg_type == "image":
            path, temporary = self._prepare_image(data)
            try:
                await self._queue_outbound(receiver, "image", path)
            finally:
                if temporary:
                    try:
                        os.unlink(path)
                    except OSError:
                        logger.warning("临时图片清理失败 path=%s", path, exc_info=True)
            return
        if seg_type == "file":
            path = self._normalize_file(data)
            await self._queue_outbound(receiver, "file", path)
            return
        if seg_type == "at":
            data = f"[@{self._text(data)}]"
        elif seg_type == "voice":
            data = "[语音消息]"
        elif seg_type not in {"text", "emoji"}:
            logger.error("拒绝未知消息段 type=%s", seg_type)
            return
        text = self._text(data)
        if text:
            await self._queue_outbound(receiver, "text", text)

    async def _queue_outbound(self, receiver, kind, data):
        completion = asyncio.get_running_loop().create_future()
        try:
            await asyncio.wait_for(
                self._send_queue.put((receiver, kind, data, completion)), timeout=2
            )
        except asyncio.TimeoutError as exc:
            self._dead_letters.append({"receiver": receiver, "type": kind, "error": "queue full"})
            raise RuntimeError("微信发送队列已满") from exc
        await completion

    @staticmethod
    def _text(value):
        if value is None:
            return ""
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="replace").strip()
        if isinstance(value, str):
            return value.strip()
        return json.dumps(value, ensure_ascii=False, default=str).strip()

    def _prepare_image(self, data):
        if isinstance(data, dict):
            data = data.get("base64") or data.get("path")
        if not isinstance(data, str) or not data:
            raise ValueError("image segment 缺少字符串 data")
        if os.path.isfile(data):
            if os.path.getsize(data) > MAX_MEDIA_BYTES:
                raise ValueError("图片超过尺寸上限")
            with open(data, "rb") as stream:
                self._validate_image(stream.read(16))
            return data, False
        encoded = data.split(",", 1)[1] if data.startswith("data:image/") and "," in data else data
        if len(encoded) > ((MAX_MEDIA_BYTES + 2) // 3) * 4 + 8:
            raise ValueError("base64 图片超过尺寸上限")
        try:
            raw = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError("无效 base64 图片") from exc
        if len(raw) > MAX_MEDIA_BYTES:
            raise ValueError("图片超过尺寸上限")
        suffix = self._validate_image(raw[:16])
        fd, path = tempfile.mkstemp(prefix="wemai_", suffix=suffix)
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
        return path, True

    @staticmethod
    def _validate_image(header):
        for magic, suffix in _IMAGE_MAGIC.items():
            if header.startswith(magic):
                if magic == b"RIFF" and header[8:12] != b"WEBP":
                    continue
                return suffix
        raise ValueError("不支持或伪造的图片格式")

    @staticmethod
    def _normalize_file(data):
        if isinstance(data, dict):
            data = data.get("path")
        if not isinstance(data, str) or not os.path.isfile(data):
            raise ValueError("file segment 必须是存在的本地文件路径")
        if os.path.getsize(data) > MAX_MEDIA_BYTES:
            raise ValueError("文件超过尺寸上限")
        return data

    def process_message(self, chat_name, message_data):
        if not self.inbound_enabled:
            return {"success": False, "error": "微信到 MaiBot 方向已禁用"}
        if not self.ready_event.is_set() or self.startup_error or not self._loop:
            return {"success": False, "error": "Router 未就绪"}
        try:
            message = self._build_message(chat_name, message_data)
            future = asyncio.run_coroutine_threadsafe(self.router.send_message(message), self._loop)
            future.result(timeout=10)
            info = message.message_info
            logger.info("已转发至 MaiBot id=%s type=%s",
                        info.message_id, message.message_segment.type)
            return {"success": True}
        except Exception as exc:
            logger.error("转发至 MaiBot 失败: %s", exc)
            return {"success": False, "error": str(exc)}

    def _build_message(self, chat_name, data):
        chat_name = self._text(chat_name)
        sender_name = self._text(data.get("sender")) or "未知用户"
        content = self._text(data.get("content"))
        chat_type = data.get("chat_type")
        if chat_type not in {"private", "group"}:
            raise ValueError(f"缺少可靠 chat_type: {chat_type!r}")
        message_id = hashlib.md5(
            f"{self.platform}|message|{chat_type}|{chat_name}|{sender_name}|{data.get('timestamp')}|{content}".encode()
        ).hexdigest()
        user_id = self._stable_id(
            chat_type, chat_name if chat_type == "private" else f"{chat_name}|{sender_name}"
        )
        user = UserInfo(platform=self.platform, user_id=user_id,
                        user_nickname=sender_name,
                        user_cardname=sender_name if chat_type == "group" else None)
        bot = UserInfo(platform=self.platform,
                       user_id=self._stable_id("bot", WX_BOT_NICKNAME or "self"),
                       user_nickname=WX_BOT_NICKNAME or "WeMai")
        group = None
        if chat_type == "group":
            group_id = self._stable_id("group", chat_name)
            group = GroupInfo(platform=self.platform, group_id=group_id, group_name=chat_name)
            self._remember(group_id, chat_name, "group")
        else:
            self._remember(user_id, chat_name, "private")
        sender = SenderInfo(group_info=group, user_info=user)
        receiver = ReceiverInfo(group_info=group, user_info=None if group else bot)
        segment = self._inbound_segment(content)
        format_info = FormatInfo(content_format=["text"], accept_format=["text", "emoji"])
        info = BaseMessageInfo(
            platform=self.platform, message_id=message_id, time=time.time(),
            group_info=group, user_info=user, format_info=format_info, template_info=None,
            additional_config=None, sender_info=sender, receiver_info=receiver,
        )
        return MessageBase(message_info=info, message_segment=segment, raw_message=content or None)

    def _inbound_segment(self, content):
        if IMAGE_RECOGNITION_ENABLED and content and os.path.isfile(content):
            size = os.path.getsize(content)
            if size > MAX_MEDIA_BYTES:
                raise ValueError("入站图片超过尺寸上限")
            raw = Path(content).read_bytes()
            self._validate_image(raw[:16])
            return Seg(type="image", data=base64.b64encode(raw).decode("ascii"))
        return Seg(type="text", data=content)

    def _stable_id(self, chat_type, identifier):
        value = f"{self.platform}|{chat_type}|{identifier}"
        return hashlib.md5(value.encode("utf-8")).hexdigest()

    def _remember(self, identifier, name, chat_type):
        with self._id_lock:
            self._id_to_name[identifier] = {"name": name, "type": chat_type, "updated": time.time()}
            self._save_id_map()

    def _resolve_receiver(self, info):
        if info is None:
            return None
        candidates = []
        receiver = getattr(info, "receiver_info", None)
        if receiver:
            candidates.extend((getattr(receiver, "group_info", None),
                               getattr(receiver, "user_info", None)))
        candidates.extend((getattr(info, "group_info", None), getattr(info, "user_info", None)))
        for value in candidates:
            if not value:
                continue
            identifier = getattr(value, "group_id", None) or getattr(value, "user_id", None)
            name = getattr(value, "group_name", None) or getattr(value, "user_nickname", None)
            if identifier:
                mapped = self._id_to_name.get(identifier)
                if mapped:
                    return mapped["name"] if isinstance(mapped, dict) else mapped
            if name and name != WX_BOT_NICKNAME:
                return name
        config = getattr(info, "additional_config", None)
        target = None
        if config:
            if isinstance(config, dict):
                target = config.get("platform_io_target_user_id")
            else:
                target = getattr(config, "platform_io_target_user_id", None)
        mapped = self._id_to_name.get(target)
        return (mapped.get("name") if isinstance(mapped, dict) else mapped) if mapped else None

    def _load_id_map(self):
        try:
            with open(ID_MAP_FILE, "r", encoding="utf-8") as stream:
                value = json.load(stream)
            if isinstance(value, dict):
                self._id_to_name = value
        except FileNotFoundError:
            pass
        except (OSError, ValueError):
            logger.warning("ID 映射读取失败，将使用空映射", exc_info=True)

    def _save_id_map(self):
        path = Path(ID_MAP_FILE)
        temporary = path.with_suffix(path.suffix + ".tmp")
        try:
            temporary.parent.mkdir(parents=True, exist_ok=True)
            with temporary.open("w", encoding="utf-8") as stream:
                json.dump(self._id_to_name, stream, ensure_ascii=False, indent=2)
            os.replace(temporary, path)
        except OSError:
            logger.error("ID 映射持久化失败 path=%s", path, exc_info=True)
