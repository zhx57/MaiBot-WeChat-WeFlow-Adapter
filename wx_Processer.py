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
import sqlite3
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


class OutboundDeliveryError(RuntimeError):
    """Final send failure retaining whether another delivery attempt is safe."""

    def __init__(self, cause):
        super().__init__("微信消息发送重试耗尽")
        self.retry_safe = getattr(cause, "retry_safe", True)
        self.command_future = getattr(cause, "command_future", None)


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
        self._inbound_task = None
        self._health_task = None
        self._cleanup_tasks = set()
        self._stop_requested = threading.Event()
        self._stopping = threading.Event()
        self._router_disconnect_since = None
        self._router_restart_count = 0
        self._router_last_error = None
        self._id_lock = threading.RLock()
        self._id_to_name = {}
        self._db_path = str(Path(ID_MAP_FILE).with_suffix(".sqlite3"))
        self._init_storage()
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
        self._stop_requested.clear()
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
            self._handler_semaphore = asyncio.Semaphore(min(32, SEND_QUEUE_SIZE))
            self._send_task = loop.create_task(self._process_send_queue())
            self._inbound_task = loop.create_task(self._process_inbound_queue())
            self._health_task = loop.create_task(self._health_monitor())
            self._router_task = loop.create_task(self.router.run())
            loop.run_until_complete(self._wait_router_ready())
            self.ready_event.set()
            loop.run_until_complete(self._supervise_router())
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

    async def _supervise_router(self):
        while not self._stop_requested.is_set():
            for name, task in (("发送队列", self._send_task),
                               ("入站队列", self._inbound_task),
                               ("健康监控", self._health_task)):
                if task.done():
                    await task
                    raise RuntimeError(f"Router {name}任务意外结束")

            try:
                connected = self.router.check_connection(self.platform)
            except Exception as exc:
                connected = False
                self._router_last_error = exc
                logger.warning("Router 连接状态检查失败，按断连处理: %s", exc)
            now = time.monotonic()
            if connected:
                if self._router_disconnect_since is not None:
                    logger.info("Router 连接已恢复 outage=%.1fs restarts=%d",
                                now - self._router_disconnect_since,
                                self._router_restart_count)
                self._router_disconnect_since = None
                self._router_restart_count = 0
                self._router_last_error = None
            elif self._router_disconnect_since is None:
                self._router_disconnect_since = now

            if (self._router_disconnect_since is not None
                    and now - self._router_disconnect_since > 600):
                raise ConnectionError("Router 连接持续 600 秒无法恢复") from self._router_last_error

            if self._router_task.done():
                try:
                    await self._router_task
                    error = RuntimeError("router.run() 意外正常返回")
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    error = exc
                self._router_last_error = error
                self._router_disconnect_since = self._router_disconnect_since or now
                self._router_restart_count += 1
                delay = min(30, 2 ** min(self._router_restart_count, 5))
                logger.warning("Router 任务退出，将在 %d 秒后重启 attempt=%d: %s",
                               delay, self._router_restart_count, error,
                               exc_info=(type(error), error, error.__traceback__))
                deadline = time.monotonic() + delay
                while not self._stop_requested.is_set():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    await asyncio.sleep(min(0.2, remaining))
                if not self._stop_requested.is_set():
                    self._router_task = asyncio.create_task(self.router.run())
                continue
            await asyncio.sleep(0.2)
        self.router._running = False
        try:
            await self.router.stop()
        except (asyncio.CancelledError, Exception):
            logger.debug("Router shutdown returned an exception", exc_info=True)

    async def _health_monitor(self):
        failures = 0
        while not self._stop_requested.is_set():
            await asyncio.sleep(5)
            try:
                connected = self.router.check_connection(self.platform)
            except Exception:
                connected = False
                logger.debug("Router 健康检查调用失败", exc_info=True)
            failures = 0 if connected else failures + 1
            if failures >= 3 and (failures == 3 or failures % 12 == 0):
                logger.warning("Router 连接健康检查连续失败 count=%d；等待后台重连", failures)

    def stop(self, timeout=15):
        """Stop websocket, cancel workers, close loop, and join its thread."""
        self._stopping.set()
        self._stop_requested.set()
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
                ok = await asyncio.to_thread(
                    self.ui_submit, "send", receiver, kind, data, timeout=15)
                if ok is not True:
                    raise RuntimeError("wxauto 返回发送失败")
                logger.info("消息已发送 target=%s type=%s", receiver, kind)
                return
            except Exception as exc:
                last_error = exc
                logger.warning("发送失败 target=%s type=%s attempt=%d/3: %s",
                               receiver, kind, attempt, exc)
                # 已开始执行但结果未知时绝不能重试，否则可能重复发送。
                if getattr(exc, "retry_safe", True) is False:
                    break
                if attempt < 3:
                    await asyncio.sleep(attempt)
        logger.error("消息进入死信 target=%s type=%s error=%s", receiver, kind, last_error)
        raise OutboundDeliveryError(last_error) from last_error

    async def _handle_maibot_response(self, message):
        if not self.outbound_enabled:
            return
        async with self._handler_semaphore:
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
          except Exception as exc:
            payload = message.to_dict() if hasattr(message, "to_dict") else message
            self._store_dead_letter(getattr(getattr(message, "message_info", None), "message_id", None),
                                    {"direction": "outbound", "message": payload}, exc)
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
            deferred_cleanup = False
            try:
                await self._queue_outbound(receiver, "image", path)
            except Exception as exc:
                command_future = getattr(exc, "command_future", None)
                if temporary and command_future is not None:
                    self._defer_temporary_cleanup(path, command_future)
                    deferred_cleanup = True
                raise
            finally:
                if temporary and not deferred_cleanup:
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

    def _defer_temporary_cleanup(self, path, command_future):
        task = asyncio.create_task(self._cleanup_after_ui_command(path, command_future))
        self._cleanup_tasks.add(task)
        task.add_done_callback(self._cleanup_tasks.discard)

    @staticmethod
    async def _cleanup_after_ui_command(path, command_future):
        try:
            await asyncio.wrap_future(command_future)
        except BaseException:
            pass
        try:
            await asyncio.to_thread(os.unlink, path)
        except OSError:
            logger.warning("延迟临时图片清理失败 path=%s", path, exc_info=True)

    async def _queue_outbound(self, receiver, kind, data):
        completion = asyncio.get_running_loop().create_future()
        try:
            await asyncio.wait_for(
                self._send_queue.put((receiver, kind, data, completion)), timeout=2
            )
        except asyncio.TimeoutError as exc:
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

    def enqueue_message(self, chat_name, message_data):
        """Persist an inbound message without waiting for websocket I/O."""
        if not self.inbound_enabled:
            return {"success": False, "error": "微信到 MaiBot 方向已禁用"}
        try:
            message = self._build_message(chat_name, message_data)
            payload = json.dumps(message.to_dict(), ensure_ascii=False)
            with self._connect() as db:
                pending = db.execute("SELECT COUNT(*) FROM inbound WHERE state='pending'").fetchone()[0]
                if pending >= SEND_QUEUE_SIZE:
                    raise RuntimeError("持久化入站队列已满")
                db.execute("INSERT OR IGNORE INTO inbound(message_id,payload,state,attempts,next_try,created) "
                           "VALUES(?,?,'pending',0,0,?)",
                           (message.message_info.message_id, payload, time.time()))
            return {"success": True}
        except Exception as exc:
            logger.error("转发至 MaiBot 失败: %s", exc)
            return {"success": False, "error": str(exc)}

    # Compatibility: unlike the old implementation this call is non-blocking.
    process_message = enqueue_message

    async def _process_inbound_queue(self):
        while not self._stop_requested.is_set():
            now = time.time()
            with self._connect() as db:
                row = db.execute("SELECT message_id,payload,attempts FROM inbound "
                                 "WHERE state='pending' AND next_try<=? ORDER BY created LIMIT 1",
                                 (now,)).fetchone()
            if not row:
                await asyncio.sleep(0.25)
                continue
            message_id, payload, attempts = row
            try:
                if not self.router.check_connection(self.platform):
                    raise ConnectionError("Router 未连接")
                await self.router.send_message(MessageBase.from_dict(json.loads(payload)))
                with self._connect() as db:
                    db.execute("UPDATE inbound SET state='sent' WHERE message_id=?", (message_id,))
                    db.execute("DELETE FROM inbound WHERE state='sent' AND created<?",
                               (time.time() - 86400 * 7,))
                logger.info("已转发至 MaiBot id=%s", message_id)
            except Exception as exc:
                attempts += 1
                if attempts >= 10:
                    self._store_dead_letter(message_id,
                                            {"direction": "inbound", "message": json.loads(payload)}, exc)
                    with self._connect() as db:
                        db.execute("UPDATE inbound SET state='dead',attempts=? WHERE message_id=?",
                                   (attempts, message_id))
                else:
                    with self._connect() as db:
                        db.execute("UPDATE inbound SET attempts=?,next_try=? WHERE message_id=?",
                                   (attempts, time.time() + min(300, 2 ** attempts), message_id))
                await asyncio.sleep(0.25)

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
        format_info = FormatInfo(content_format=[segment.type],
                                 accept_format=["text", "emoji", "image", "file"])
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
            with self._connect() as db:
                db.execute("INSERT OR REPLACE INTO id_map(identifier,name,type,updated) VALUES(?,?,?,?)",
                           (identifier, name, chat_type, time.time()))
                db.execute("DELETE FROM id_map WHERE identifier IN (SELECT identifier FROM id_map "
                           "ORDER BY updated DESC LIMIT -1 OFFSET 100000)")

    def _resolve_receiver(self, info):
        if info is None:
            return None
        candidates = []
        receiver = getattr(info, "receiver_info", None)
        if receiver:
            candidates.extend((getattr(receiver, "group_info", None),
                               getattr(receiver, "user_info", None)))
        candidates.extend((getattr(info, "group_info", None), getattr(info, "user_info", None)))
        bot_id = self._stable_id("bot", WX_BOT_NICKNAME or "self")
        bot_names = {"WeMai", WX_BOT_NICKNAME} - {""}
        # Only stable IDs are authoritative. Nicknames are deliberately not routing keys.
        for value in candidates:
            if not value:
                continue
            identifier = getattr(value, "group_id", None) or getattr(value, "user_id", None)
            if identifier and identifier != bot_id:
                mapped = self._id_to_name.get(identifier)
                if mapped:
                    name = mapped["name"] if isinstance(mapped, dict) else mapped
                    if name not in bot_names:
                        return name
        config = getattr(info, "additional_config", None)
        target = None
        if config:
            if isinstance(config, dict):
                target = config.get("platform_io_target_user_id")
            else:
                target = getattr(config, "platform_io_target_user_id", None)
        mapped = self._id_to_name.get(target) if target and target != bot_id else None
        name = (mapped.get("name") if isinstance(mapped, dict) else mapped) if mapped else None
        return name if name and name not in bot_names else None

    def _load_id_map(self):
        # One-time migration from the legacy {id: name|record} JSON file.
        try:
            with open(ID_MAP_FILE, "r", encoding="utf-8") as stream:
                value = json.load(stream)
            if isinstance(value, dict):
                with self._connect() as db:
                    for identifier, record in value.items():
                        if isinstance(record, dict):
                            name, kind = record.get("name"), record.get("type")
                        else:
                            name, kind = record, None
                        if name:
                            db.execute("INSERT OR IGNORE INTO id_map(identifier,name,type,updated) "
                                       "VALUES(?,?,?,?)", (identifier, name, kind, time.time()))
        except FileNotFoundError:
            pass
        except (OSError, ValueError):
            logger.warning("旧 ID 映射读取失败；SQLite 数据不受影响", exc_info=True)
        with self._connect() as db:
            self._id_to_name = {
                row[0]: {"name": row[1], "type": row[2], "updated": row[3]}
                for row in db.execute("SELECT identifier,name,type,updated FROM id_map")
            }

    def _connect(self):
        return sqlite3.connect(self._db_path, timeout=10)

    def _init_storage(self):
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA busy_timeout=10000")
            db.execute("CREATE TABLE IF NOT EXISTS id_map (identifier TEXT PRIMARY KEY, "
                       "name TEXT NOT NULL, type TEXT, updated REAL NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS inbound (message_id TEXT PRIMARY KEY, "
                       "payload TEXT NOT NULL, state TEXT NOT NULL, attempts INTEGER NOT NULL, "
                       "next_try REAL NOT NULL, created REAL NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS dead_letters (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                       "message_id TEXT, payload TEXT NOT NULL, error TEXT, created REAL NOT NULL, "
                       "replayed INTEGER NOT NULL DEFAULT 0)")

    def _store_dead_letter(self, message_id, payload, error):
        with self._connect() as db:
            count = db.execute("SELECT COUNT(*) FROM dead_letters WHERE replayed=0").fetchone()[0]
            if count >= SEND_QUEUE_SIZE * 10:
                logger.critical("死信存储已满 count=%d；删除最旧记录", count)
                db.execute("DELETE FROM dead_letters WHERE id=(SELECT id FROM dead_letters "
                           "WHERE replayed=0 ORDER BY id LIMIT 1)")
            db.execute("INSERT INTO dead_letters(message_id,payload,error,created) VALUES(?,?,?,?)",
                       (message_id, json.dumps(payload, ensure_ascii=False, default=str),
                        str(error), time.time()))

    def replay_dead_letters(self, limit=100):
        """Replay persisted inbound dead letters; returns number scheduled."""
        replayed = 0
        with self._connect() as db:
            rows = db.execute("SELECT id,message_id,payload FROM dead_letters "
                              "WHERE replayed=0 ORDER BY id LIMIT ?", (limit,)).fetchall()
            for dead_id, message_id, payload in rows:
                data = json.loads(payload)
                if data.get("direction") == "inbound":
                    db.execute("INSERT OR REPLACE INTO inbound(message_id,payload,state,attempts,next_try,created) "
                               "VALUES(?,?,'pending',0,0,?)",
                               (message_id, json.dumps(data["message"], ensure_ascii=False), time.time()))
                elif data.get("direction") == "outbound" and self._loop:
                    try:
                        asyncio.run_coroutine_threadsafe(
                            self._handle_maibot_response(MessageBase.from_dict(data["message"])),
                            self._loop)
                    except RuntimeError:
                        logger.warning("Router loop 已关闭，无法重放 outbound 死信 id=%s", dead_id)
                        continue
                else:
                    continue
                db.execute("UPDATE dead_letters SET replayed=1 WHERE id=?", (dead_id,))
                replayed += 1
        return replayed
