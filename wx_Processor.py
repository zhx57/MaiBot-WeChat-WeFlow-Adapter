"""MaiBot 消息格式转换与 Router 生命周期管理。

本模块不直接导入微信自动化库。微信窗口操作通过
:mod:`wx_Listener` 的按会话命令队列执行。
"""

import asyncio
import base64
import binascii
import hashlib
import inspect
import ipaddress
import json
import logging
import os
import re
import socket
import sqlite3
import tempfile
import threading
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from chat_name_utils import normalize_chat_name

from maim_message import (
    BaseMessageInfo,
    GroupInfo,
    MessageBase,
    RouteConfig,
    Router,
    Seg,
    TargetConfig,
    UserInfo,
)

from config import (
    ID_MAP_FILE,
    IMAGE_RECOGNITION_ENABLED,
    MAIBOT_DATA_DIR,
    MAIBOT_API_URL,
    MAX_MEDIA_BYTES,
    MEDIA_DOWNLOAD_MAX_REDIRECTS,
    MEDIA_DOWNLOAD_TIMEOUT_SECONDS,
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
    b"BM": ".bmp",
}

_IMAGE_DATA_URI = re.compile(
    r"^data:image/[a-z0-9.+-]+(?:;[a-z0-9._+-]+(?:=[^;,]*)?)*;base64,",
    re.IGNORECASE,
)
_BASE64_TEXT = re.compile(r"^[A-Za-z0-9+/]+={0,2}$")
_MARKDOWN_IMAGE = re.compile(r"!\[[^\]]*\]\((https?://[^\s)]+)(?:\s+[^)]*)?\)", re.IGNORECASE)
_URL_START = re.compile(r"https?://", re.IGNORECASE)
_AVATAR_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}


class _SafeRedirectHandler(HTTPRedirectHandler):
    def __init__(self, validator, max_redirects):
        super().__init__()
        self.validator = validator
        self.max_redirects = max_redirects

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        redirects = int(req.headers.get("X-WeMai-Redirects", "0")) + 1
        if redirects > self.max_redirects:
            raise ValueError("图片重定向次数过多")
        self.validator(newurl)
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected is not None:
            redirected.add_header("X-WeMai-Redirects", str(redirects))
        return redirected


class OutboundDeliveryError(RuntimeError):
    """发送重试耗尽，并保留是否可安全重试的信息。"""

    def __init__(self, cause):
        super().__init__("微信消息发送重试耗尽")
        self.retry_safe = getattr(cause, "retry_safe", True)
        self.command_future = getattr(cause, "command_future", None)
        self.cleanup_delay = getattr(cause, "cleanup_delay", 0.0)


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
        self._receiver_send_queues = {}
        self._receiver_send_tasks = {}
        self._inbound_task = None
        self._health_task = None
        self._cleanup_tasks = set()
        self._stop_requested = threading.Event()
        self._stopping = threading.Event()
        self._router_disconnect_since = None
        self._router_restart_count = 0
        self._router_last_error = None
        self._router_connected = False
        self._router_heartbeat = time.monotonic()
        self._id_lock = threading.RLock()
        self._id_to_name = {}
        self._avatar_lock = threading.RLock()
        self._avatar_downloads = set()
        self._db_path = str(Path(ID_MAP_FILE).with_suffix(".sqlite3"))
        self._init_storage()
        self._load_id_map()

        route = RouteConfig(route_config={
            platform: TargetConfig(url=MAIBOT_API_URL, token=None)
        })
        self.router = Router(route)
        self.router.register_class_handler(self._handle_maibot_response)

    def set_ui_submit(self, submit):
        self.ui_submit = submit

    def register_target(self, name, chat_type):
        """预加载可路由目标，确保重启后收到的回复仍能找到微信会话。"""
        if chat_type == "group":
            self._remember(self._stable_id("group", name), name, chat_type)
        elif chat_type == "private":
            self._remember(self._stable_id("private", name), name, chat_type)

    def start(self, timeout=20):
        """启动 Router，并等待 WebSocket 连接可用。"""
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
            self._receiver_send_queues = {}
            self._receiver_send_tasks = {}
            self._handler_semaphore = asyncio.Semaphore(min(32, SEND_QUEUE_SIZE))
            self._inbound_wakeup = asyncio.Event()
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
        while (
            not self._router_task.done()
            and not self._stop_requested.is_set()
        ):
            connected = self._router_is_connected()
            self._router_connected = bool(connected)
            self._router_heartbeat = time.monotonic()
            if connected:
                return
            await asyncio.sleep(0.05)
        if self._stop_requested.is_set():
            self._router_task.cancel()
            raise asyncio.CancelledError
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
                connected = self._router_is_connected()
            except Exception as exc:
                connected = False
                self._router_last_error = exc
                logger.warning("Router 连接状态检查失败，按断连处理: %s", exc)
            now = time.monotonic()
            self._router_connected = bool(connected)
            self._router_heartbeat = now
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
        self._router_connected = False
        try:
            await self.router.stop()
        except (asyncio.CancelledError, Exception):
            logger.debug("Router shutdown returned an exception", exc_info=True)

    async def _health_monitor(self):
        failures = 0
        while not self._stop_requested.is_set():
            await asyncio.sleep(5)
            try:
                connected = self._router_is_connected()
            except Exception:
                connected = False
                logger.debug("Router 健康检查调用失败", exc_info=True)
            self._router_connected = bool(connected)
            self._router_heartbeat = time.monotonic()
            failures = 0 if connected else failures + 1
            if failures >= 3 and (failures == 3 or failures % 12 == 0):
                logger.warning("Router 连接健康检查连续失败 count=%d；等待后台重连", failures)

    def stop(self, timeout=15):
        """停止 WebSocket 和后台任务，关闭事件循环并回收线程。"""
        self._stopping.set()
        self._stop_requested.set()
        self._router_connected = False
        loop = self._loop
        if loop is not None and loop.is_running():
            try:
                def wake_for_stop():
                    wakeup = getattr(self, "_inbound_wakeup", None)
                    if wakeup is not None:
                        wakeup.set()

                loop.call_soon_threadsafe(wake_for_stop)
            except RuntimeError:
                pass
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout)
            if self._thread.is_alive():
                logger.error("Router 线程未在时限内退出")

    async def _process_send_queue(self):
        """Dispatch outbound items to one ordered worker per receiver."""
        while True:
            item = await self._send_queue.get()
            try:
                receiver = item[0]
                receiver_key = normalize_chat_name(receiver)
                queue_for_receiver = self._receiver_send_queues.get(receiver_key)
                if queue_for_receiver is None:
                    queue_for_receiver = asyncio.Queue(maxsize=SEND_QUEUE_SIZE)
                    self._receiver_send_queues[receiver_key] = queue_for_receiver
                    task = asyncio.create_task(
                        self._process_receiver_send_queue(
                            receiver,
                            queue_for_receiver,
                        )
                    )
                    self._receiver_send_tasks[receiver_key] = task
                await queue_for_receiver.put(item)
            finally:
                self._send_queue.task_done()

    async def _process_receiver_send_queue(self, receiver, send_queue):
        """Serialize one chat while allowing other chat workers to run."""
        while True:
            _receiver, kind, data, completion = await send_queue.get()
            try:
                try:
                    await self._deliver_with_retry(receiver, kind, data)
                    if not completion.done():
                        completion.set_result(True)
                except BaseException as exc:
                    if not completion.done():
                        completion.set_exception(exc)
                    logger.error(
                        "发送队列项目最终失败 target=%s: %s",
                        receiver,
                        exc,
                    )
            finally:
                send_queue.task_done()

    async def _deliver_with_retry(self, receiver, kind, data):
        last_error = None
        for attempt in range(1, 4):
            try:
                if not self.ui_submit:
                    raise RuntimeError("UI 命令执行器尚未绑定")
                ok = await self._run_blocking(
                    self.ui_submit, "send", receiver, kind, data, timeout=15)
                if ok is not True:
                    raise RuntimeError("wx4py 返回发送失败")
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
                    await asyncio.sleep(0.1 * attempt)
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
        seg_type = str(self._segment_value(segment, "type", "")).strip().lower()
        data = self._segment_value(segment, "data")
        if seg_type == "seglist":
            if data is None:
                return
            if not isinstance(data, (list, tuple)):
                raise ValueError("seglist segment 的 data 必须是数组")
            for child in data:
                await self._process_segments(child, receiver)
            return
        if seg_type in {"reply", "notify"}:
            return
        if seg_type == "image":
            for source in self._image_sources(data):
                await self._send_image_segment(receiver, source)
            return
        if seg_type == "emoji":
            try:
                await self._send_image_segment(receiver, data)
            except ValueError:
                text = self._textual_emoji(data)
                if text:
                    await self._queue_outbound(receiver, "text", text)
            return
        if seg_type == "file":
            path = self._normalize_file(data)
            await self._queue_outbound(receiver, "file", path)
            return
        if seg_type == "at":
            data = f"[@{self._text(data)}]"
        elif seg_type == "voice":
            data = "[语音消息]"
        elif seg_type != "text":
            logger.error("拒绝未知消息段 type=%s", seg_type)
            return
        text = self._text(data)
        if text:
            if _IMAGE_DATA_URI.match(text):
                await self._send_image_segment(receiver, text)
            else:
                image_urls = self._standalone_image_urls(text)
                if image_urls:
                    for url in image_urls:
                        await self._send_image_segment(receiver, url)
                else:
                    await self._queue_outbound(receiver, "text", text)

    @staticmethod
    def _image_sources(data):
        if isinstance(data, (list, tuple)):
            if not data:
                raise ValueError("image segment 的图片列表为空")
            return list(data)
        return [data]

    @staticmethod
    def _standalone_image_urls(text):
        markdown_urls = _MARKDOWN_IMAGE.findall(text)
        if markdown_urls:
            remainder = _MARKDOWN_IMAGE.sub("", text)
            if not remainder.strip(" \t\r\n,;，；"):
                return markdown_urls
            return []

        starts = [match.start() for match in _URL_START.finditer(text)]
        if not starts or text[:starts[0]].strip(" \t\r\n,;，；"):
            return []
        urls = []
        for index, start in enumerate(starts):
            end = starts[index + 1] if index + 1 < len(starts) else len(text)
            candidate = text[start:end].strip(" \t\r\n,;，；")
            if any(char.isspace() for char in candidate):
                return []
            parsed = urlsplit(candidate)
            if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
                return []
            urls.append(candidate)
        return urls

    async def _send_image_segment(self, receiver, data):
        # URL downloads, base64 decoding and file validation are blocking. A
        # slow outbound image must not pause inbound text delivery or Router
        # health checks on the event-loop thread.
        path, temporary = await self._run_blocking(self._prepare_image, data)
        deferred_cleanup = False
        try:
            await self._queue_outbound(receiver, "image", path)
        except Exception as exc:
            command_future = getattr(exc, "command_future", None)
            cleanup_delay = getattr(exc, "cleanup_delay", 0.0)
            if temporary and (command_future is not None or cleanup_delay > 0):
                self._defer_temporary_cleanup(
                    path,
                    command_future=command_future,
                    delay=cleanup_delay,
                )
                deferred_cleanup = True
            raise
        finally:
            if temporary and not deferred_cleanup:
                try:
                    os.unlink(path)
                except OSError:
                    logger.warning("临时图片清理失败 path=%s", path, exc_info=True)

    @staticmethod
    def _textual_emoji(data):
        if not isinstance(data, str):
            raise ValueError("emoji segment 缺少有效图片数据")
        text = data.strip()
        if not text or len(text) > 64:
            raise ValueError("emoji segment 包含无效或过长的媒体数据")
        compact = "".join(text.split())
        if len(compact) >= 16 and _BASE64_TEXT.fullmatch(compact):
            raise ValueError("emoji segment 包含无效 base64 图片")
        return text

    def _defer_temporary_cleanup(self, path, command_future=None, delay=0.0):
        task = asyncio.create_task(self._cleanup_after_ui_command(
            path,
            command_future=command_future,
            delay=delay,
        ))
        self._cleanup_tasks.add(task)
        task.add_done_callback(self._cleanup_tasks.discard)

    @staticmethod
    async def _cleanup_after_ui_command(path, command_future=None, delay=0.0):
        try:
            if command_future is not None:
                while not command_future.done():
                    await asyncio.sleep(0.01)
                command_future.result()
        except BaseException:
            pass
        try:
            if delay > 0:
                await asyncio.sleep(float(delay))
        except BaseException:
            # Shutdown cancellation skips the grace period but still removes
            # the temporary file below.
            pass
        try:
            await MessageProcessor._run_blocking(os.unlink, path)
        except OSError:
            logger.warning("延迟临时图片清理失败 path=%s", path, exc_info=True)

    @staticmethod
    async def _run_blocking(function, *args, **kwargs):
        """Run blocking work without depending on asyncio's default executor.

        Some embedded Windows event-loop policies do not reliably wake for a
        default-executor completion.  A short async poll around a daemon worker
        keeps Router I/O responsive and has deterministic shutdown behavior.
        """
        completed = threading.Event()
        outcome = {}

        def run():
            try:
                outcome["result"] = function(*args, **kwargs)
            except BaseException as exc:
                outcome["error"] = exc
            finally:
                completed.set()

        worker = threading.Thread(
            target=run,
            name="wemai-blocking-io",
            daemon=True,
        )
        worker.start()
        while not completed.is_set():
            await asyncio.sleep(0.01)
        worker.join()
        if "error" in outcome:
            raise outcome["error"]
        return outcome.get("result")

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
            sources = [data[key] for key in ("base64", "data", "path", "url") if data.get(key)]
            if len(sources) != 1:
                raise ValueError("image segment 必须且只能指定一种图片来源")
            data = sources[0]
        if not isinstance(data, str) or not data:
            raise ValueError("image segment 缺少字符串 data")
        data = data.strip()
        if data.lower().startswith(("http://", "https://")):
            return self._download_image(data)
        if os.path.isfile(data):
            if os.path.getsize(data) > MAX_MEDIA_BYTES:
                raise ValueError("图片超过尺寸上限")
            with open(data, "rb") as stream:
                self._validate_image(stream.read(16))
            return data, False
        if data.lower().startswith("data:image/"):
            match = _IMAGE_DATA_URI.match(data)
            if not match:
                raise ValueError("图片 data URI 必须使用 base64 编码")
            encoded = data[match.end():]
        else:
            encoded = data
        encoded = "".join(encoded.split())
        if not encoded:
            raise ValueError("base64 图片内容为空")
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

    def _download_image(self, url):
        self._validate_public_url(url)
        opener = build_opener(_SafeRedirectHandler(
            self._validate_public_url, MEDIA_DOWNLOAD_MAX_REDIRECTS
        ))
        request = Request(url, headers={
            "User-Agent": "WeMai/1.0",
            "Accept": "image/png,image/jpeg,image/gif,image/webp;q=0.9,*/*;q=0.1",
        })
        try:
            with opener.open(request, timeout=MEDIA_DOWNLOAD_TIMEOUT_SECONDS) as response:
                content_length = response.headers.get("Content-Length")
                if content_length and int(content_length) > MAX_MEDIA_BYTES:
                    raise ValueError("远程图片超过尺寸上限")
                raw = response.read(MAX_MEDIA_BYTES + 1)
        except (HTTPError, URLError, TimeoutError, socket.timeout) as exc:
            raise ValueError("远程图片下载失败") from exc
        if len(raw) > MAX_MEDIA_BYTES:
            raise ValueError("远程图片超过尺寸上限")
        suffix = self._validate_image(raw[:16])
        fd, path = tempfile.mkstemp(prefix="wemai_", suffix=suffix)
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
        return path, True

    @staticmethod
    def _validate_public_url(url):
        if not isinstance(url, str) or len(url) > 8192:
            raise ValueError("图片 URL 无效或过长")
        parsed = urlsplit(url)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
            raise ValueError("图片 URL 仅支持 HTTP(S)")
        if parsed.username or parsed.password:
            raise ValueError("图片 URL 禁止包含用户凭据")
        try:
            addresses = socket.getaddrinfo(parsed.hostname, parsed.port or 443, type=socket.SOCK_STREAM)
        except socket.gaierror as exc:
            raise ValueError("图片 URL 域名解析失败") from exc
        if not addresses:
            raise ValueError("图片 URL 域名没有可用地址")
        for address in addresses:
            ip = ipaddress.ip_address(address[4][0])
            if not ip.is_global:
                raise ValueError("图片 URL 指向非公网地址")

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
        """Persist a small inbound envelope without doing media conversion."""
        if not self.inbound_enabled:
            return {"success": False, "error": "微信到 MaiBot 方向已禁用"}
        try:
            chat_name = self._text(chat_name)
            data = dict(message_data or {})
            if data.get("chat_type") not in {"private", "group"}:
                raise ValueError(
                    f"缺少可靠 chat_type: {data.get('chat_type')!r}"
                )
            message_id = self._inbound_message_id(chat_name, data)
            payload = json.dumps(
                {
                    "_wemai_inbound_event": 1,
                    "chat_name": chat_name,
                    "data": data,
                },
                ensure_ascii=False,
            )
            with self._connect() as db:
                db.execute("INSERT OR IGNORE INTO inbound(message_id,payload,state,attempts,next_try,created) "
                           "VALUES(?,?,'pending',0,0,?)",
                           (message_id, payload, time.time()))
            # The SQLite queue is durable and must absorb temporary Router
            # outages. SEND_QUEUE_SIZE only bounds in-memory outbound work;
            # rejecting durable inbound rows at that threshold lost messages.
            loop = getattr(self, "_loop", None)
            wakeup = getattr(self, "_inbound_wakeup", None)
            if loop is not None and wakeup is not None and loop.is_running():
                try:
                    loop.call_soon_threadsafe(wakeup.set)
                except RuntimeError:
                    pass
            return {"success": True}
        except Exception as exc:
            logger.error("转发至 MaiBot 失败: %s", exc)
            return {"success": False, "error": str(exc)}

    def enqueue_inbound_event(self, event):
        """Normalize a listener ``_InboundEvent`` into the durable queue."""
        raw = getattr(event, "raw", None)
        raw = raw if isinstance(raw, dict) else {}
        data = {
            "chat_type": getattr(event, "chat_type", ""),
            "sender": getattr(event, "sender", ""),
            "type": getattr(event, "message_type", "text"),
            "content": getattr(event, "content", ""),
            "timestamp": getattr(event, "timestamp", time.time()),
        }
        avatar_url = self._text(getattr(event, "avatar_url", ""))
        if avatar_url:
            data["avatar_url"] = avatar_url
        for name in ("event", "rawid", "sessionId", "sessionType"):
            if name in raw:
                data[name] = raw[name]
        for name in (
            "groupCardName",
            "groupCard",
            "cardName",
            "senderCardName",
        ):
            value = self._text(raw.get(name))
            if value:
                data["group_card_name"] = value
                break
        if "content" in raw:
            data["raw_content"] = raw["content"]
        return self.enqueue_message(getattr(event, "group", ""), data)

    # 兼容旧调用名；当前实现只入队持久化，不阻塞等待网络发送。
    process_message = enqueue_message

    async def _process_inbound_queue(self):
        while not self._stop_requested.is_set():
            now = time.time()
            with self._connect() as db:
                rows = db.execute(
                    "SELECT message_id,payload,attempts FROM inbound "
                    "WHERE state='pending' AND next_try<=? "
                    "ORDER BY created LIMIT 32",
                    (now,),
                ).fetchall()
            if not rows:
                wakeup = self._inbound_wakeup
                wakeup.clear()
                # Recheck after clear so an enqueue racing with the first query
                # cannot leave the consumer asleep with work available.
                with self._connect() as db:
                    ready = db.execute(
                        "SELECT 1 FROM inbound WHERE state='pending' "
                        "AND next_try<=? LIMIT 1",
                        (time.time(),),
                    ).fetchone()
                if ready:
                    continue
                try:
                    await asyncio.wait_for(wakeup.wait(), timeout=0.05)
                except asyncio.TimeoutError:
                    pass
                continue
            if not self._router_is_connected():
                # A connection outage is not a malformed-message attempt. Keep
                # every row immediately eligible and resume quickly on reconnect.
                self._inbound_wakeup.clear()
                try:
                    await asyncio.wait_for(
                        self._inbound_wakeup.wait(), timeout=0.05
                    )
                except asyncio.TimeoutError:
                    pass
                continue
            for message_id, payload, attempts in rows:
                if self._stop_requested.is_set():
                    return
                stored = json.loads(payload)
                try:
                    if stored.get("_wemai_inbound_event") == 1:
                        event_data = stored["data"]
                        if str(event_data.get("type", "")).casefold() in {
                            "image", "voice"
                        }:
                            # Reading and base64-encoding a large saved image is
                            # the expensive path; keep it off both the WeChat UI
                            # owner thread and the Router event loop.
                            message = await self._run_blocking(
                                self._build_message,
                                stored["chat_name"],
                                event_data,
                            )
                        else:
                            message = self._build_message(
                                stored["chat_name"], event_data
                            )
                    else:
                        # Backward compatibility for rows persisted by earlier
                        # releases as a fully-built MessageBase dictionary.
                        message = MessageBase.from_dict(stored)
                    await self.router.send_message(message)
                    with self._connect() as db:
                        db.execute(
                            "UPDATE inbound SET state='sent' WHERE message_id=?",
                            (message_id,),
                        )
                        db.execute(
                            "DELETE FROM inbound WHERE state='sent' AND created<?",
                            (time.time() - 86400 * 7,),
                        )
                    logger.info("已转发至 MaiBot id=%s", message_id)
                except Exception as exc:
                    attempts += 1
                    if attempts >= 10:
                        self._store_dead_letter(
                            message_id,
                            {"direction": "inbound", "message": stored},
                            exc,
                        )
                        with self._connect() as db:
                            db.execute(
                                "UPDATE inbound SET state='dead',attempts=? "
                                "WHERE message_id=?",
                                (attempts, message_id),
                            )
                    else:
                        with self._connect() as db:
                            db.execute(
                                "UPDATE inbound SET attempts=?,next_try=? "
                                "WHERE message_id=?",
                                (
                                    attempts,
                                    time.time() + min(300, 2 ** attempts),
                                    message_id,
                                ),
                            )

    def _build_message(self, chat_name, data):
        chat_name = self._text(chat_name)
        # 微信 4.x 的 Qt UIA 不提供可靠发送者名称。
        sender_name = self._text(data.get("sender")) or "unknown"
        content = self._text(data.get("content"))
        chat_type = data.get("chat_type")
        if chat_type not in {"private", "group"}:
            raise ValueError(f"缺少可靠 chat_type: {chat_type!r}")
        message_id = self._inbound_message_id(chat_name, data)
        user_id = self._stable_id(
            chat_type, chat_name if chat_type == "private" else f"{chat_name}|{sender_name}"
        )
        group_card_name = self._text(data.get("group_card_name"))
        user = self._build_user_info(
            platform=self.platform,
            user_id=user_id,
            user_nickname=sender_name,
            user_cardname=(group_card_name or sender_name) if chat_type == "group" else None,
        )
        group = None
        group_id = None
        if chat_type == "group":
            group_id = self._stable_id("group", chat_name)
            group = GroupInfo(
                platform=self.platform,
                group_id=group_id,
                group_name=chat_name,
            )
            self._remember(group_id, chat_name, "group")
        else:
            self._remember(user_id, chat_name, "private")
        avatar_url = self._text(data.get("avatar_url"))
        if avatar_url:
            self._schedule_avatar_cache(avatar_url, user_id, group_id=group_id)
        segment = self._inbound_segment(content, data.get("type"))
        try:
            message_time = int(float(data.get("timestamp", time.time())))
        except (TypeError, ValueError) as exc:
            raise ValueError("消息 timestamp 必须是可转换为整数的时间戳") from exc
        info = BaseMessageInfo(
            platform=self.platform,
            message_id=message_id,
            time=message_time,
            user_info=user,
            group_info=group,
        )
        raw_message = self._text(data.get("raw_content"))
        if not raw_message and segment.type == "text":
            raw_message = segment.data
        segment_list = Seg(type="seglist", data=[segment])
        return MessageBase(
            message_info=info,
            message_segment=segment_list,
            raw_message=raw_message or None,
        )

    def _inbound_message_id(self, chat_name, data):
        rawid = self._text(data.get("rawid"))
        if rawid:
            event_name = self._text(data.get("event")) or "message.new"
            return hashlib.md5(
                f"{self.platform}|{event_name}|{rawid}".encode("utf-8")
            ).hexdigest()
        sender_name = self._text(data.get("sender")) or "unknown"
        content = self._text(data.get("content"))
        chat_type = data.get("chat_type")
        return hashlib.md5(
            f"{self.platform}|message|{chat_type}|{chat_name}|{sender_name}|"
            f"{data.get('timestamp')}|{content}".encode("utf-8")
        ).hexdigest()

    def _inbound_segment(self, content, message_type=None):
        is_declared_image = str(message_type or "").casefold() == "image"
        is_declared_file = str(message_type or "").casefold() == "file"
        is_declared_voice = str(message_type or "").casefold() == "voice"
        is_declared_emoji = str(message_type or "").casefold() == "emoji"
        has_local_file = isinstance(content, (str, os.PathLike)) and os.path.isfile(content)
        if is_declared_image and not has_local_file:
            raise ValueError("入站图片消息缺少已保存的本地文件")
        if is_declared_voice:
            logger.debug(
                "准备转换入站语音为 MaiBot voice 段 path=%r has_local_file=%s",
                (
                    os.fspath(content)
                    if isinstance(content, (str, os.PathLike))
                    else content
                ),
                has_local_file,
            )
            if has_local_file:
                try:
                    raw = Path(content).read_bytes()
                    logger.debug(
                        "转换入站语音为 MaiBot voice 段 path=%s bytes=%d",
                        content,
                        len(raw),
                    )
                    return Seg(
                        type="voice",
                        data=base64.b64encode(raw).decode("ascii"),
                    )
                except OSError as exc:
                    logger.warning("读取入站语音文件失败 path=%s: %s", content, exc)
            logger.debug("入站语音缺少可读本地文件，降级为文字 path=%r", content)
            return Seg(type="text", data="[语音消息]")
        if is_declared_emoji:
            if has_local_file:
                try:
                    size = os.path.getsize(content)
                    if size > MAX_MEDIA_BYTES:
                        raise ValueError("入站表情图片超过尺寸上限")
                    raw = Path(content).read_bytes()
                    suffix = self._validate_image(raw[:16])
                    logger.debug(
                        "转换入站表情为 MaiBot emoji 段 path=%s bytes=%d format=%s",
                        content,
                        len(raw),
                        suffix,
                    )
                    return Seg(type="emoji", data=base64.b64encode(raw).decode("ascii"))
                except (OSError, ValueError) as exc:
                    logger.warning("读取入站表情文件失败 path=%s: %s", content, exc)
            return Seg(type="text", data="[表情]")
        if is_declared_file:
            file_name = Path(os.fspath(content)).name if content else ""
            description = f"[文件消息：{file_name}]" if file_name else "[文件消息]"
            return Seg(type="text", data=description)
        if IMAGE_RECOGNITION_ENABLED and is_declared_image and has_local_file:
            size = os.path.getsize(content)
            if size > MAX_MEDIA_BYTES:
                raise ValueError("入站图片超过尺寸上限")
            raw = Path(content).read_bytes()
            self._validate_image(raw[:16])
            return Seg(type="image", data=base64.b64encode(raw).decode("ascii"))
        if is_declared_image:
            return Seg(type="text", data="[图片消息]")
        return Seg(type="text", data=content)

    @staticmethod
    def _build_user_info(*, platform, user_id, user_nickname, user_cardname):
        return UserInfo(
            platform=platform,
            user_id=user_id,
            user_nickname=user_nickname,
            user_cardname=user_cardname,
        )

    @staticmethod
    def _avatar_component(value):
        return re.sub(
            r"[^A-Za-z0-9_-]+", "_", str(value).strip()
        ).strip("_")

    def _avatar_paths(self, user_id, group_id=None):
        if not MAIBOT_DATA_DIR:
            return []
        platform = self._avatar_component(str(self.platform).lower())
        user = self._avatar_component(user_id)
        if not platform or not user:
            return []
        directory = Path(MAIBOT_DATA_DIR) / "avatar" / platform
        targets = [(directory, user)]
        if group_id is not None:
            group = self._avatar_component(group_id)
            if group:
                targets.append((directory, f"group_{group}"))
        return targets

    def _download_avatar(self, avatar_url):
        self._validate_public_url(avatar_url)
        opener = build_opener(_SafeRedirectHandler(
            self._validate_public_url, MEDIA_DOWNLOAD_MAX_REDIRECTS
        ))
        request = Request(avatar_url, headers={
            "User-Agent": "WeMai/1.0",
            "Accept": "image/png,image/jpeg,image/gif,image/webp,image/bmp,*/*;q=0.1",
        })
        with opener.open(request, timeout=MEDIA_DOWNLOAD_TIMEOUT_SECONDS) as response:
            content_length = response.headers.get("Content-Length")
            if content_length and int(content_length) > MAX_MEDIA_BYTES:
                raise ValueError("头像图片超过尺寸上限")
            raw = response.read(MAX_MEDIA_BYTES + 1)
        if len(raw) > MAX_MEDIA_BYTES:
            raise ValueError("头像图片超过尺寸上限")
        detected_suffix = self._validate_image(raw[:16])
        url_suffix = Path(urlsplit(avatar_url).path).suffix.lower()
        suffix = url_suffix if url_suffix in _AVATAR_SUFFIXES else detected_suffix
        return raw, suffix

    def _cache_avatar(self, avatar_url, user_id, group_id=None):
        """Download one WeFlow avatar into MaiBot's filesystem cache."""
        targets = self._avatar_paths(user_id, group_id=group_id)
        if not avatar_url or not targets:
            return
        missing = [
            (directory, stem)
            for directory, stem in targets
            if not any(
                (directory / f"{stem}{suffix}").is_file()
                for suffix in _AVATAR_SUFFIXES
            )
        ]
        if not missing:
            return
        try:
            raw, suffix = self._download_avatar(avatar_url)
            for directory, stem in missing:
                directory.mkdir(parents=True, exist_ok=True)
                destination = directory / f"{stem}{suffix}"
                if destination.is_file():
                    continue
                fd, temporary = tempfile.mkstemp(prefix=f".{stem}.", dir=directory)
                try:
                    with os.fdopen(fd, "wb") as stream:
                        stream.write(raw)
                    os.replace(temporary, destination)
                except BaseException:
                    try:
                        os.unlink(temporary)
                    except OSError:
                        pass
                    raise
        except Exception as exc:
            logger.warning(
                "缓存 MaiBot 头像失败 user_id=%s group_id=%s url=%s: %s",
                user_id,
                group_id,
                avatar_url,
                exc,
            )

    def _schedule_avatar_cache(self, avatar_url, user_id, group_id=None):
        if not MAIBOT_DATA_DIR:
            return
        lock = getattr(self, "_avatar_lock", None)
        if lock is None:
            lock = self._avatar_lock = threading.RLock()
            self._avatar_downloads = set()
        key = (avatar_url, user_id, group_id)
        with lock:
            if key in self._avatar_downloads:
                return
            self._avatar_downloads.add(key)

        def cache():
            try:
                self._cache_avatar(avatar_url, user_id, group_id=group_id)
            finally:
                with lock:
                    self._avatar_downloads.discard(key)

        threading.Thread(
            target=cache,
            name="maibot-avatar-cache",
            daemon=True,
        ).start()

    def _router_is_connected(self):
        """Use the installed Legacy Router connection probe only if present."""
        check_connection = getattr(self.router, "check_connection", None)
        if not callable(check_connection):
            raise RuntimeError(
                "当前 maim_message Router 未提供 check_connection(platform)"
            )
        try:
            signature = inspect.signature(check_connection)
        except (TypeError, ValueError):
            signature = None
        if signature is not None:
            try:
                signature.bind(self.platform)
            except TypeError as exc:
                raise RuntimeError(
                    "当前 maim_message Router.check_connection 签名不兼容"
                ) from exc
        try:
            return bool(check_connection(self.platform))
        except TypeError as exc:
            raise RuntimeError(
                "当前 maim_message Router.check_connection 调用不兼容"
            ) from exc

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
        # 只有稳定 ID 可作为权威路由依据，昵称不作为路由键。
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
        # 从旧版 {id: name|record} JSON 文件执行一次性迁移。
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
            db.execute(
                "CREATE INDEX IF NOT EXISTS inbound_ready_idx "
                "ON inbound(state,next_try,created)"
            )
            db.execute("CREATE TABLE IF NOT EXISTS dead_letters (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                       "message_id TEXT, payload TEXT NOT NULL, error TEXT, created REAL NOT NULL, "
                       "replayed INTEGER NOT NULL DEFAULT 0)")
            # Clean up old replayed dead letters and ancient sent inbound rows
            # to prevent unbounded SQLite growth.
            db.execute("DELETE FROM dead_letters WHERE replayed=1 AND created<?",
                       (time.time() - 86400 * 7,))
            db.execute("DELETE FROM inbound WHERE state='sent' AND created<?",
                       (time.time() - 86400 * 7,))
            db.execute("DELETE FROM dead_letters WHERE created<?",
                       (time.time() - 86400 * 30,))

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
        """重放已持久化的死信，并返回已安排重放的数量。"""
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
