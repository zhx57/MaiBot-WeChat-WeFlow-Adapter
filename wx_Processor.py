"""MaiBot 消息格式转换与 Router 生命周期管理。

本模块不直接导入微信自动化库。微信窗口操作通过
:mod:`wx_Listener` 的按会话命令队列执行。
"""

import asyncio
import base64
import binascii
import concurrent.futures
import contextlib
import hashlib
import inspect
import ipaddress
import json
import logging
import os
import re
import socket
import sqlite3
import ssl
import tempfile
import threading
import time
from datetime import datetime, timedelta
from io import BytesIO
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener, url2pathname

import aiohttp
import maim_message.api as _maim_api
import maim_message.ws_connection as _maim_ws
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
from PIL import Image

from audio_segments import split_audio_file
from chat_name_utils import normalize_chat_name
from config import (
    FILE_MAX_BYTES,
    ID_MAP_FILE,
    ID_MAP_GC_INTERVAL_SECONDS,
    ID_MAP_MAX_ROWS,
    IMAGE_GC_INTERVAL_SECONDS,
    IMAGE_PERSISTED_PREFIX,
    IMAGE_PERSISTED_RETENTION_SECONDS,
    IMAGE_RECOGNITION_ENABLED,
    IMAGE_SAVE_DIR,
    IMAGE_TEMP_PREFIX,
    IMAGE_TEMP_RETENTION_SECONDS,
    INBOUND_ROUTER_CONCURRENCY,
    LOCAL_FILE_DELIVERY_ROOT,
    MAIBOT_API_URL,
    MAIBOT_API_TOKEN,
    MAIBOT_DATA_DIR,
    MAX_MEDIA_BYTES,
    MEDIA_DOWNLOAD_MAX_REDIRECTS,
    MEDIA_DOWNLOAD_TIMEOUT_SECONDS,
    PLATFORM_ID,
    SEND_QUEUE_SIZE,
    VOICE_COMMAND_TIMEOUT_SECONDS,
    WEMAI_BOT_NICKNAME,
)
from sender_identity_aliases import SenderIdentityAliasStore
from unicode_utils import sanitize_unicode
from wx_exceptions import ChatNotFoundError

logger = logging.getLogger(__name__)

_VOICE_SEGMENT_SECONDS = 60.0
_OUTBOUND_PRIORITY_NORMAL = 0
_OUTBOUND_PRIORITY_VOICE = 1

# 微信在暂时无法读取部分成员头像时会返回一张固定占位图。它的 URL
# 因成员而异，不能按 URL 黑名单判断；使用低分辨率差异哈希可以兼容 PNG/JPEG
# 重编码，同时把检测成本控制在后台线程中的亚毫秒级。
_UNAVAILABLE_AVATAR_DHASH = int(
    "080030007200721030240c6218d131806504c00090000e14b2f233420c2c2000",
    16,
)
_UNAVAILABLE_AVATAR_MAX_HASH_DISTANCE = 12
_UNAVAILABLE_AVATAR_MIN_SIZE = 96
_UNAVAILABLE_AVATAR_MAX_SIZE = 160


class _UncompressedMaimWebSocketClient(_maim_ws.WebSocketClient):
    """Avoid an aiohttp/permessage-deflate control-frame interoperability bug."""

    async def connect(self):
        maim_logger = _maim_ws.get_logger()
        if not self.url or not self.platform:
            raise ValueError("连接前必须先调用 configure 方法配置连接参数")

        ssl_context = None
        if self.url.startswith("wss://"):
            ssl_context = ssl.create_default_context()
            if self.ssl_verify:
                maim_logger.info("使用证书验证: %s", self.ssl_verify)
                ssl_context.load_verify_locations(self.ssl_verify)
            else:
                maim_logger.warning("未提供证书验证，当前连接将跳过 SSL 校验")
                ssl_context.check_hostname = False
                ssl_context.verify_mode = ssl.CERT_NONE

        try:
            timeout = aiohttp.ClientTimeout(total=30, connect=10)
            connector = aiohttp.TCPConnector(
                ssl=ssl_context,
                enable_cleanup_closed=True,
            )
            if self.session and not self.session.closed:
                await self.session.close()
            self.session = aiohttp.ClientSession(
                connector=connector,
                timeout=timeout,
                headers=self.headers,
            )
            self.ws = await self.session.ws_connect(
                self.url,
                heartbeat=self.heartbeat_interval,
                autoping=True,
                # MaiBot's current server and aiohttp 3.14 can disagree on
                # RSV1 after idle control frames. Messages already contain
                # compressed media, so transport compression is unnecessary.
                compress=0,
                autoclose=True,
                max_msg_size=self.max_message_size,
            )
            self.ws_connected = True
            self.retry_count = 0
            maim_logger.info("已成功连接到 %s（WebSocket 压缩已禁用）", self.url)
            return True
        except aiohttp.ClientConnectorError as exc:
            maim_logger.error("无法建立连接: %s", exc)
        except aiohttp.ClientSSLError as exc:
            maim_logger.error("SSL 错误: %s", exc)
        except aiohttp.ClientError as exc:
            maim_logger.error("连接错误: %s", exc)
        except Exception:
            maim_logger.exception("连接时发生未预期的错误")
        finally:
            if not self.ws_connected:
                await self._cleanup_session()
        return False


# Router 创建连接时会从该模块全局变量解析客户端类。补丁只作用于 WeMai 进程，
# 用于关闭大帧压缩兼容问题，不修改已安装的 maim_message 包。
_maim_api.WebSocketClient = _UncompressedMaimWebSocketClient

# 本地数据库把普通消息和微信系统事件放在同一消息表中。无法还原操作者的
# 系统事件使用独立虚拟用户：消息仍留在原群聊流内，又不会与真实成员冲突或继承
# 旧“未知发送者”的错误头像。上游账号 ``weixin`` 会被识别，但本地另用新 ID，
# 避免命中历史污染缓存。
_WECHAT_SYSTEM_SENDER_ID = "weixin_system_message"
_WECHAT_SYSTEM_SENDER_NAME = "微信系统消息"
_WECHAT_SYSTEM_UPSTREAM_IDS = {"weixin", _WECHAT_SYSTEM_SENDER_ID}

# UI 发送可能占用线程 10~30 秒，必须与图片 base64、缓存清理等短任务分池，
# 否则少量慢出站就会耗尽共享线程并拖延入站。UI 池允许多个独立窗口并行；辅助池
# 只处理短促任务，两个线程足够且可限制 CPU 峰值。
_UI_SUBMIT_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=8,
    thread_name_prefix="wemai-ui-submit",
)
_AUX_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=2,
    thread_name_prefix="wemai-aux",
)
# 保留旧名称供外部代码兼容；新调用应明确选择 UI 池或辅助池。
# ``_run_blocking`` 默认使用辅助池，避免无意中挤占微信 UI 发送容量。
_BLOCKING_EXECUTOR = _AUX_EXECUTOR
_RECEIVER_WORKER_IDLE_SECONDS = 300.0

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


class PartialVoiceDeliveryError(RuntimeError):
    """At least one voice part was sent, so replaying the source is unsafe."""

    retry_safe = False
    partial_delivery = True

    def __init__(self, sent_parts, total_parts, cause):
        super().__init__(
            f"语音已发送 {sent_parts}/{total_parts} 段，后续分段失败：{cause}"
        )
        self.sent_parts = int(sent_parts)
        self.total_parts = int(total_parts)
        self.cause = cause


class MessageProcessor:
    """MaiBot Router 双向转换、可靠入站队列和按接收方出站调度器。

    入站先写 SQLite，再按稳定 session ID 调度：同会话只允许一个头部任务执行，
    不同会话最多 ``INBOUND_ROUTER_CONCURRENCY`` 路并发。出站按接收方建立优先级
    队列，保证单个窗口内有序，并允许文本/图片/文件在语音分段之间优先执行。

    本类运行自己的 asyncio 线程；本地数据库和微信 UI 线程只能通过公开入队方法或
    ``ui_submit`` 回调与它交互，不得直接操作内部 asyncio 队列。
    """

    def __init__(self, platform=PLATFORM_ID, ui_submit=None, inbound_enabled=True,
                 outbound_enabled=True, voice_outbound_enabled=False):
        self.platform = platform
        self.ui_submit = ui_submit
        self.inbound_enabled = inbound_enabled
        self.outbound_enabled = outbound_enabled
        self.voice_outbound_enabled = bool(voice_outbound_enabled)
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
        self._platform_account_id = ""
        self._platform_account_nickname = ""
        self._avatar_lock = threading.RLock()
        self._avatar_downloads = set()
        self._db_path = str(Path(ID_MAP_FILE).with_suffix(".sqlite3"))
        self._init_storage()
        self._load_id_map()
        self._sender_aliases = SenderIdentityAliasStore(self._db_path)

        route = RouteConfig(route_config={
            platform: TargetConfig(url=MAIBOT_API_URL, token=MAIBOT_API_TOKEN or None)
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
            self._send_sequence = 0
            self._handler_semaphore = asyncio.Semaphore(min(32, SEND_QUEUE_SIZE))
            self._inbound_wakeup = asyncio.Event()
            self._inbound_delivery_tasks = {}
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
        delivery_tasks = list(
            getattr(self, "_inbound_delivery_tasks", {}).values()
        )
        if delivery_tasks:
            _, pending = await asyncio.wait(delivery_tasks, timeout=5)
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
        try:
            await self.router.stop()
        except (asyncio.CancelledError, Exception):
            logger.debug("Router shutdown returned an exception", exc_info=True)

    async def _health_monitor(self):
        failures = 0
        last_gc_at = 0.0
        last_id_map_gc_at = 0.0
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

            # Long-running housekeeping. Each branch is rate-limited so
            # a misconfiguration can't make health checks expensive.
            now = time.monotonic()
            if now - last_gc_at >= IMAGE_GC_INTERVAL_SECONDS:
                last_gc_at = now
                try:
                    await self._run_blocking(self._run_image_gc)
                except Exception:
                    logger.debug("图片目录 GC 失败", exc_info=True)
                try:
                    self._run_dead_letter_gc()
                except Exception:
                    logger.debug("死信清理失败", exc_info=True)
            if now - last_id_map_gc_at >= ID_MAP_GC_INTERVAL_SECONDS:
                last_id_map_gc_at = now
                try:
                    self._run_id_map_gc()
                except Exception:
                    logger.debug("id_map 清理失败", exc_info=True)

    def _run_image_gc(self):
        """删除超过保留期的入站媒体和用户可见图片。"""
        now = time.time()
        deleted = 0
        for prefix, max_age in (
            (IMAGE_TEMP_PREFIX, IMAGE_TEMP_RETENTION_SECONDS),
            (IMAGE_PERSISTED_PREFIX, IMAGE_PERSISTED_RETENTION_SECONDS),
        ):
            if max_age <= 0:
                continue
            cutoff = now - max_age
            try:
                entries = os.listdir(IMAGE_SAVE_DIR)
            except OSError:
                return
            for name in entries:
                if not name.startswith(prefix):
                    continue
                path = os.path.join(IMAGE_SAVE_DIR, name)
                try:
                    stat = os.stat(path)
                except OSError:
                    continue
                if stat.st_mtime >= cutoff:
                    continue
                try:
                    os.unlink(path)
                    deleted += 1
                except OSError:
                    logger.debug("删除旧媒体文件失败 path=%s", path, exc_info=True)
        if deleted:
            logger.info("图片目录 GC 完成 deleted=%d", deleted)

    def _run_dead_letter_gc(self):
        """Periodic prune of dead_letters and inbound rows."""
        try:
            with contextlib.closing(self._connect()) as db, db:
                db.execute(
                    "DELETE FROM dead_letters WHERE replayed=1 AND created<?",
                    (time.time() - 86400 * 7,),
                )
                db.execute(
                    "DELETE FROM dead_letters WHERE created<?",
                    (time.time() - 86400 * 30,),
                )
                db.execute(
                    "DELETE FROM inbound WHERE state='sent' AND created<?",
                    (time.time() - 86400 * 7,),
                )
        except Exception:
            logger.debug("死信周期清理失败", exc_info=True)

    def _run_id_map_gc(self):
        """Periodic cap of the id_map table at ID_MAP_MAX_ROWS.

        Previously this DELETE ran inside every ``_remember`` call, which
        meant a high-fan-out chat would do one full scan per inbound
        message. Pulling it out of the hot path keeps the per-message
        write to a single INSERT.
        """
        try:
            with self._connect() as db:
                count = db.execute("SELECT COUNT(*) FROM id_map").fetchone()[0]
                if count <= ID_MAP_MAX_ROWS:
                    return
                db.execute(
                    "DELETE FROM id_map WHERE identifier IN "
                    "(SELECT identifier FROM id_map "
                    "ORDER BY updated ASC LIMIT ?)",
                    (count - ID_MAP_MAX_ROWS,),
                )
        except Exception:
            logger.debug("id_map 周期清理失败", exc_info=True)

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
        sender_aliases = getattr(self, "_sender_aliases", None)
        if sender_aliases is not None:
            sender_aliases.close(timeout=min(5, timeout))
        # 关闭两个阻塞任务池，防止 URL/base64 或 UI 提交任务活得比事件循环更久。
        # 此时待处理协程已经取消，因此不等待仍在系统调用中的线程；shutdown 会阻止
        # 新任务并促使工作线程尽快释放 UIA 引用。重复关闭及空队列均安全。
        try:
            _UI_SUBMIT_EXECUTOR.shutdown(wait=False, cancel_futures=True)
        except Exception:
            logger.debug("关闭 UI 提交任务池失败", exc_info=True)
        try:
            _BLOCKING_EXECUTOR.shutdown(wait=False, cancel_futures=True)
        except Exception:
            logger.debug("关闭阻塞任务池失败", exc_info=True)

    async def _process_send_queue(self):
        """把全局出站项分派给每个接收方各自的有序工作协程。"""
        while True:
            item = await self._send_queue.get()
            try:
                receiver = item[0]
                receiver_key = normalize_chat_name(receiver)
                queue_for_receiver = self._receiver_send_queues.get(receiver_key)
                if queue_for_receiver is None:
                    queue_for_receiver = asyncio.PriorityQueue(maxsize=SEND_QUEUE_SIZE)
                    self._receiver_send_queues[receiver_key] = queue_for_receiver
                    task = asyncio.create_task(
                        self._process_receiver_send_queue(
                            receiver,
                            queue_for_receiver,
                        )
                    )
                    self._receiver_send_tasks[receiver_key] = task
                priority = item[4]
                self._send_sequence += 1
                await queue_for_receiver.put((priority, self._send_sequence, item))
            finally:
                self._send_queue.task_done()

    async def _process_receiver_send_queue(self, receiver, send_queue):
        """串行处理一个聊天，同时允许其他聊天工作协程并行。"""
        while True:
            try:
                item = await asyncio.wait_for(
                    send_queue.get(),
                    timeout=_RECEIVER_WORKER_IDLE_SECONDS,
                )
            except asyncio.TimeoutError:
                receiver_key = normalize_chat_name(receiver)
                current_task = asyncio.current_task()
                if (
                    send_queue.empty()
                    and self._receiver_send_queues.get(receiver_key) is send_queue
                    and self._receiver_send_tasks.get(receiver_key) is current_task
                ):
                    self._receiver_send_queues.pop(receiver_key, None)
                    self._receiver_send_tasks.pop(receiver_key, None)
                    return
                continue
            _priority, _sequence, item = item
            _receiver, kind, data, completion, _item_priority = item
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
                # 首次 UI 提交保留 15 秒，兼容能最终成功的慢窗口；重试缩短到 5 秒，
                # 避免异常目标长期占住接收方队列。UI 提交固定走专用线程池，单次慢
                # 发送不能饿死入站图片编码、缓存清理或其他辅助任务。
                if kind == "voice":
                    ui_timeout = VOICE_COMMAND_TIMEOUT_SECONDS
                else:
                    ui_timeout = 15 if attempt == 1 else 5
                ok = await self._run_blocking(
                    self.ui_submit, "send", receiver, kind, data,
                    timeout=ui_timeout,
                    executor=_UI_SUBMIT_EXECUTOR,
                )
                if ok is not True:
                    raise RuntimeError("WeMai 独立窗口发送失败")
                logger.info("消息已发送 target=%s type=%s", receiver, kind)
                return
            except Exception as exc:
                last_error = exc
                logger.warning("发送失败 target=%s type=%s attempt=%d/3: %s",
                               receiver, kind, attempt, exc)
                # 已开始执行但结果未知时绝不能重试，否则可能重复发送。
                if (
                    isinstance(exc, ChatNotFoundError)
                    or getattr(exc, "retry_safe", True) is False
                ):
                    break
                if attempt < 3:
                    await asyncio.sleep(0.1 * attempt)
        logger.error("消息进入死信 target=%s type=%s error=%s", receiver, kind, last_error)
        raise OutboundDeliveryError(last_error) from last_error

    async def _handle_maibot_response(self, message):
        if not self.outbound_enabled:
            return
        async with self._handler_semaphore:
          started_at = time.monotonic()
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
            elapsed = time.monotonic() - started_at
            # ``_process_segments`` awaits the outbound send command,
            # so this number covers router-handshake + image prep +
            # UI worker dispatch. Useful for the outbound P95 column.
            if elapsed >= 0.5:
                logger.info(
                    "MaiBot 回复处理耗时 %.2fs receiver=%s", elapsed, receiver
                )
          except Exception as exc:
            payload = message.to_dict() if hasattr(message, "to_dict") else message
            direction = (
                "outbound_partial"
                if getattr(exc, "partial_delivery", False)
                else "outbound"
            )
            self._store_dead_letter(getattr(getattr(message, "message_info", None), "message_id", None),
                                    {"direction": direction, "message": payload}, exc)
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
        if seg_type == "dict" and isinstance(data, dict):
            wrapped_type = str(data.get("type", "")).strip().lower()
            if wrapped_type in {"voice", "record"}:
                await self._process_segments(data, receiver)
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
            delivery_id = self._local_file_delivery_id(data)
            try:
                path = self._normalize_file(data)
                await self._queue_outbound(receiver, "file", path)
            except Exception as exc:
                self._write_local_file_receipt(
                    delivery_id,
                    success=False,
                    error=f"{type(exc).__name__}: {exc}",
                )
                raise
            else:
                self._write_local_file_receipt(delivery_id, success=True)
            return
        if seg_type == "at":
            data = f"[@{self._text(data)}]"
        elif seg_type in {"voice", "record"}:
            if self.voice_outbound_enabled:
                await self._send_voice_segment(receiver, data)
                return
            if seg_type == "record":
                logger.error(
                    "收到语音消息段，但当前运行模式未启用语音出站"
                )
                return
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
        delivery_task = None
        try:
            delivery_task = asyncio.create_task(
                self._queue_outbound(receiver, "image", path)
            )
            await asyncio.shield(delivery_task)
        except asyncio.CancelledError:
            if temporary and delivery_task is not None:
                self._defer_temporary_cleanup(
                    path,
                    command_future=delivery_task,
                )
                deferred_cleanup = True
            raise
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

    async def _send_voice_segment(self, receiver, data):
        original_path = await self._run_blocking(self._prepare_voice, data)
        cleanup_paths = {original_path}
        try:
            paths = await self._run_blocking(
                split_audio_file,
                original_path,
                _VOICE_SEGMENT_SECONDS,
            )
            cleanup_paths.update(paths)
            if paths != [original_path]:
                with contextlib.suppress(OSError):
                    os.unlink(original_path)
                cleanup_paths.discard(original_path)
                logger.info(
                    "语音已分段 count=%d max_seconds=%.1f",
                    len(paths),
                    _VOICE_SEGMENT_SECONDS,
                )
            sent_parts = 0
            for index, path in enumerate(paths, 1):
                deferred_cleanup = False
                delivery_task = None
                try:
                    delivery_task = asyncio.create_task(
                        self._queue_outbound(
                            receiver,
                            "voice",
                            path,
                            priority=_OUTBOUND_PRIORITY_VOICE,
                        )
                    )
                    await asyncio.shield(delivery_task)
                    logger.info(
                        "语音分段已发送 target=%s part=%d/%d",
                        receiver,
                        index,
                        len(paths),
                    )
                    sent_parts += 1
                except asyncio.CancelledError:
                    if delivery_task is not None:
                        self._defer_temporary_cleanup(
                            path,
                            command_future=delivery_task,
                        )
                        cleanup_paths.discard(path)
                        deferred_cleanup = True
                    raise
                except Exception as exc:
                    command_future = getattr(exc, "command_future", None)
                    cleanup_delay = getattr(exc, "cleanup_delay", 0.0)
                    if command_future is not None or cleanup_delay > 0:
                        self._defer_temporary_cleanup(
                            path,
                            command_future=command_future,
                            delay=cleanup_delay,
                        )
                        cleanup_paths.discard(path)
                        deferred_cleanup = True
                    if sent_parts:
                        raise PartialVoiceDeliveryError(
                            sent_parts,
                            len(paths),
                            exc,
                        ) from exc
                    raise
                finally:
                    if not deferred_cleanup and path in cleanup_paths:
                        with contextlib.suppress(OSError):
                            os.unlink(path)
                        cleanup_paths.discard(path)
        finally:
            for path in cleanup_paths:
                with contextlib.suppress(OSError):
                    os.unlink(path)

    def _prepare_voice(self, data):
        if isinstance(data, dict):
            sources = [
                data[key]
                for key in ("file", "path", "base64", "data")
                if data.get(key)
            ]
            if len(sources) != 1:
                raise ValueError(
                    "voice segment must specify exactly one audio source"
                )
            data = sources[0]
        if not isinstance(data, str) or not data.strip():
            raise ValueError("voice segment is missing audio data")
        source = data.strip()
        if os.path.isfile(source):
            if os.path.getsize(source) > MAX_MEDIA_BYTES:
                raise ValueError("voice file exceeds MAX_MEDIA_BYTES")
            with open(source, "rb") as stream:
                raw = stream.read(MAX_MEDIA_BYTES + 1)
        else:
            lowered = source.lower()
            if lowered.startswith("base64://"):
                encoded = source[len("base64://"):]
            elif lowered.startswith("data:audio/") and ";base64," in lowered:
                encoded = source.split(",", 1)[1]
            else:
                encoded = source
            encoded = "".join(encoded.split())
            if not encoded:
                raise ValueError("base64 voice content is empty")
            if len(encoded) > ((MAX_MEDIA_BYTES + 2) // 3) * 4 + 8:
                raise ValueError("base64 voice exceeds MAX_MEDIA_BYTES")
            try:
                raw = base64.b64decode(encoded, validate=True)
            except (binascii.Error, ValueError) as exc:
                raise ValueError("invalid base64 voice data") from exc
        if not raw or len(raw) > MAX_MEDIA_BYTES:
            raise ValueError("voice data is empty or exceeds MAX_MEDIA_BYTES")
        suffix = self._voice_suffix(raw[:16])
        fd, path = tempfile.mkstemp(prefix="wemai_voice_", suffix=suffix)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(raw)
            return path
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(path)
            raise

    @staticmethod
    def _voice_suffix(header):
        if header.startswith(b"RIFF") and header[8:12] == b"WAVE":
            return ".wav"
        if header.startswith(b"ID3") or (
            len(header) >= 2
            and header[0] == 0xFF
            and header[1] & 0xE0 == 0xE0
        ):
            return ".mp3"
        raise ValueError("only MP3 and WAV voice files are supported")

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
    async def _run_blocking(function, *args, executor=None, **kwargs):
        """在共享工作池执行阻塞任务，避免每次临时创建线程。

        图片密集时，单次创建线程约 5~10ms 的成本会持续累积，因此复用常驻任务池。
        正常情况由完成回调立即唤醒；同时以 1ms 轮询兜底，兼容少数不能可靠触发
        executor 回调的 Windows 嵌入式事件循环。

        需要 UI 专用池的调用方必须显式传入 ``_UI_SUBMIT_EXECUTOR``；默认任务池
        只用于图片 base64、缓存清理等短辅助操作。
        """
        pool = executor if executor is not None else _AUX_EXECUTOR
        future = pool.submit(function, *args, **kwargs)
        if not future.done():
            # Fast path: most calls finish well within the 1 ms
            # callback window. ``asyncio.get_running_loop`` is
            # cheap; creating a future is ~10 ns.
            loop = asyncio.get_running_loop()
            cb_done = loop.create_future()

            def mark_done():
                if not cb_done.done():
                    cb_done.set_result(None)

            def notify_loop(_future):
                with contextlib.suppress(RuntimeError):
                    loop.call_soon_threadsafe(mark_done)

            future.add_done_callback(notify_loop)
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(cb_done, timeout=0.001)
        while not future.done():
            # Backstop: 1 ms instead of the old 5 ms. Some embedded
            # IocpProactor builds do not deliver the callback in
            # time; for those, the worst-case extra latency is 1 ms
            # per call.
            await asyncio.sleep(0.001)
        return future.result()

    async def _queue_outbound(self, receiver, kind, data, priority=None):
        if priority is None:
            priority = (
                _OUTBOUND_PRIORITY_VOICE
                if kind == "voice"
                else _OUTBOUND_PRIORITY_NORMAL
            )
        completion = asyncio.get_running_loop().create_future()
        try:
            await asyncio.wait_for(
                self._send_queue.put((receiver, kind, data, completion, priority)),
                timeout=2,
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
            path = data.get("path")
            if not path and data.get("url"):
                path = MessageProcessor._trusted_local_file_url(data.get("url"))
            data = path
        if not isinstance(data, str) or not os.path.isfile(data):
            raise ValueError("file segment 必须是存在的本地文件路径")
        if os.path.getsize(data) > FILE_MAX_BYTES:
            raise ValueError("文件超过尺寸上限")
        return data

    @staticmethod
    def _trusted_local_file_url(value):
        """Resolve a file URI only inside the configured delivery spool."""

        if not LOCAL_FILE_DELIVERY_ROOT:
            raise ValueError("本地文件交付功能未启用")
        parsed = urlsplit(str(value or ""))
        if parsed.scheme.casefold() != "file" or parsed.netloc.casefold() not in {"", "localhost"}:
            raise ValueError("本地文件交付仅支持本机 file:// URL")
        raw_path = url2pathname(unquote(parsed.path))
        if os.name == "nt" and re.match(r"^/[A-Za-z]:/", raw_path):
            raw_path = raw_path[1:]
        source = Path(raw_path).resolve(strict=True)
        allowed = (Path(LOCAL_FILE_DELIVERY_ROOT).resolve() / "attachments").resolve()
        try:
            source.relative_to(allowed)
        except ValueError as exc:
            raise ValueError("本地文件路径超出可信交付目录") from exc
        if not source.is_file():
            raise ValueError("本地交付目标不是普通文件")
        return os.fspath(source)

    @staticmethod
    def _local_file_delivery_id(data):
        if not isinstance(data, dict) or not data.get("url"):
            return ""
        delivery_id = str(data.get("file_id") or "").strip()
        if not re.fullmatch(r"codex-[0-9a-f]{32}-[0-9]{3}", delivery_id):
            return ""
        return delivery_id

    @staticmethod
    def _write_local_file_receipt(delivery_id, *, success, error=""):
        """Atomically acknowledge completion of the actual WeChat UI send."""

        if not delivery_id or not LOCAL_FILE_DELIVERY_ROOT:
            return
        receipts = Path(LOCAL_FILE_DELIVERY_ROOT).resolve() / "receipts"
        receipts.mkdir(parents=True, exist_ok=True)
        destination = receipts / f"{delivery_id}.json"
        temporary = receipts / f".{delivery_id}.{os.getpid()}.tmp"
        payload = {
            "delivery_id": delivery_id,
            "success": bool(success),
            "created_at": time.time(),
        }
        if error:
            payload["error"] = str(error)[:500]
        try:
            with temporary.open("w", encoding="utf-8", newline="\n") as stream:
                json.dump(payload, stream, ensure_ascii=False, separators=(",", ":"))
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, destination)
        finally:
            with contextlib.suppress(OSError):
                temporary.unlink(missing_ok=True)

    def enqueue_message(self, chat_name, message_data):
        """只持久化轻量入站信封，不在生产者线程中转换媒体。

        该方法位于本地数据库入站热路径。先写 SQLite 再返回，确保进程异常退出后消息
        仍可恢复；图片转换、头像缓存和 Router 网络发送均由后台会话任务完成。
        """
        if not self.inbound_enabled:
            return {"success": False, "error": "微信到 MaiBot 方向已禁用"}
        try:
            chat_name = self._text(chat_name)
            data = dict(message_data or {})
            (chat_name, data), repaired_units = sanitize_unicode((chat_name, data))
            if repaired_units:
                logger.warning(
                    "入站消息包含不完整 UTF-16 字符，已在可靠队列前修复 "
                    "chat=%s units=%d",
                    chat_name,
                    repaired_units,
                )
            # 系统事件仍按微信会话类型进入原私聊或群聊；后续构建消息时
            # 根据明确系统标记识别，不能在这里放宽 chat_type。
            if data.get("chat_type") not in {"private", "group"}:
                raise ValueError(
                    f"缺少可靠 chat_type: {data.get('chat_type')!r}"
                )
            message_id = self._inbound_message_id(chat_name, data)
            # 把已计算的消息哈希随 SQLite 信封持久化，消费者构建消息时直接复用。
            # ``_wemai_*`` 为适配器内部保留字段，交给 MaiBot 前会被移除。
            data["_wemai_message_id"] = message_id
            session_key = self._inbound_session_key(chat_name, data)
            payload = json.dumps(
                {
                    "_wemai_inbound_event": 1,
                    "chat_name": chat_name,
                    "data": data,
                },
                ensure_ascii=False,
            )
            with self._connect() as db:
                db.execute(
                    "INSERT OR IGNORE INTO inbound("
                    "message_id,payload,state,attempts,next_try,created,session_key"
                    ") VALUES(?,?,'pending',0,0,?,?)",
                    (message_id, payload, time.time(), session_key),
                )
            # The SQLite queue is durable and must absorb temporary Router
            # outages. SEND_QUEUE_SIZE only bounds in-memory outbound work;
            # rejecting durable inbound rows at that threshold lost messages.
            loop = getattr(self, "_loop", None)
            wakeup = getattr(self, "_inbound_wakeup", None)
            if loop is not None and wakeup is not None and loop.is_running():
                with contextlib.suppress(RuntimeError):
                    loop.call_soon_threadsafe(wakeup.set)
            return {"success": True}
        except Exception as exc:
            logger.error("转发至 MaiBot 失败: %s", exc)
            return {"success": False, "error": str(exc)}

    def enqueue_inbound_event(self, event):
        """把监听层的 ``_InboundEvent`` 标准化后写入可靠队列。"""
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
        group_avatar_url = self._text(
            getattr(event, "group_avatar_url", "")
        )
        if group_avatar_url and data["chat_type"] == "group":
            data["group_avatar_url"] = group_avatar_url
        system_event = self._text(raw.get("wechatSystemEvent"))
        if system_event:
            data["wx_notice_type"] = system_event
        # 传递微信会话ID，使 ``_build_message`` 按稳定 ID 计算 group_id，
        # 而不是使用可能改名的显示名称。显示名仍原样交给 MaiBot 自行刷新。
        sid = self._text(getattr(event, "session_id", ""))
        if sid:
            data["chat_id"] = sid
        # 发送者微信真 ID 用于生成跨私聊/群聊一致的 user_id。昵称和群名片可变，
        # 不能作为身份主键，否则改名后会丢失 MaiBot 中已有的人物资料与记忆。
        swid = self._text(getattr(event, "sender_wxid", ""))
        if swid:
            data["sender_id"] = swid
        sender_identity_source = self._text(raw.get("senderIdentitySource"))
        if sender_identity_source:
            data["sender_identity_source"] = sender_identity_source
        profile_nickname = self._text(
            getattr(event, "user_nickname", "")
        ) or self._text(raw.get("fromNickName")) or self._text(
            raw.get("sourceName")
        )
        if profile_nickname:
            data["user_nickname"] = profile_nickname
        explicit_group_card = self._text(
            getattr(event, "group_card_name", "")
        ) or self._text(raw.get("displayName"))
        if explicit_group_card:
            data["group_card_name"] = explicit_group_card
        for name in ("event", "rawid", "sessionId", "sessionType"):
            if name in raw:
                data[name] = raw[name]
        data["wx_message_type"] = self._text(
            raw.get("wxMessageType") or data.get("type")
        ) or "text"
        data["wx_wechat_type"] = raw.get("localType")
        data["wx_app_type"] = raw.get("appType")
        data["wx_local_message_id"] = raw.get("localMessageId")
        data["wx_server_message_id"] = raw.get("serverMessageId") or raw.get("rawid")
        self_id = self._text(raw.get("selfId") or raw.get("self_id"))
        if self_id:
            data["self_id"] = self_id
        # 引用元数据必须随可靠队列一起持久化，让 MaiBot 能区分引用回复和普通文本，
        # 且无需了解微信 XML 格式。
        if isinstance(raw.get("quote"), dict):
            data["quote"] = raw["quote"]
        if isinstance(raw.get("app"), dict):
            data["wx_app"] = raw["app"]
        if isinstance(raw.get("location"), dict):
            data["wx_location"] = raw["location"]
        if isinstance(raw.get("video"), dict):
            data["wx_video"] = raw["video"]
        if isinstance(raw.get("contactCard"), dict):
            data["wx_contact_card"] = raw["contactCard"]
        if isinstance(raw.get("notice"), dict):
            data["wx_notice"] = raw["notice"]
        if raw.get("fileSize") not in (None, ""):
            data["file_size"] = raw["fileSize"]
        file_md5 = self._text(raw.get("fileMd5"))
        if file_md5:
            data["file_md5"] = file_md5
        reply_to = self._text(raw.get("replyToMessageId"))
        if reply_to:
            data["replyToMessageId"] = reply_to
            data["reply_to_message_id"] = reply_to
        for name in (
            "groupCardName",
            "groupCard",
            "cardName",
            "senderCardName",
        ):
            value = self._text(raw.get(name))
            if value and not data.get("group_card_name"):
                data["group_card_name"] = value
                break
        if "content" in raw:
            data["raw_content"] = raw["content"]
        if isinstance(raw.get("chatRecordList"), list):
            # 合并记录仍以文本交给当前 MaiBot Legacy 转换器；同时把结构化节点放进
            # additional_config，保留能可靠解析出的内部成员真 ID，供后续版本使用。
            data["wx_chat_record_title"] = self._text(raw.get("chatRecordTitle"))
            data["wx_chat_record_list"] = raw["chatRecordList"]
        if isinstance(raw.get("mentionUserIds"), list):
            data["mention_user_ids"] = raw["mentionUserIds"]
        if isinstance(raw.get("mentionUsers"), list):
            data["mention_users"] = raw["mentionUsers"]
        data["pat_from_wxid"] = self._text(raw.get("patFromWxid"))
        data["pat_target_wxid"] = self._text(raw.get("patTargetWxid"))
        return self.enqueue_message(getattr(event, "group", ""), data)

    def observe_account_profile(self, profile):
        """记录微信当前账号身份，并独立缓存机器人头像。"""
        if not isinstance(profile, dict):
            return
        user_id = self._text(profile.get("userId") or profile.get("user_id"))
        if not user_id:
            return
        self._platform_account_id = user_id
        nickname = self._text(profile.get("nickname"))
        if nickname:
            self._platform_account_nickname = nickname
        avatar_url = self._text(profile.get("avatarUrl") or profile.get("avatar_url"))
        if avatar_url:
            self._platform_account_avatar_url = avatar_url
            self._schedule_avatar_cache(avatar_url, user_id)
        logger.info(
            "微信机器人账号资料已刷新 user_id=%s nickname=%s avatar=%s",
            user_id,
            self._text(profile.get("nickname")) or user_id,
            bool(avatar_url),
        )

    # 兼容旧调用名；当前实现只入队持久化，不阻塞等待网络发送。
    process_message = enqueue_message

    def _inbound_session_key(self, chat_name, data):
        """返回一个会话的稳定串行键，优先使用微信会话ID。"""
        data = data if isinstance(data, dict) else {}
        chat_type = self._text(data.get("chat_type")) or "unknown"
        native_id = self._text(
            data.get("chat_id")
            or data.get("sessionId")
            or data.get("session_id")
        )
        if native_id:
            return f"{chat_type}:{native_id}"
        fallback = normalize_chat_name(self._text(chat_name))
        return f"{chat_type}:name:{fallback or '<unknown>'}"

    def _inbound_session_key_from_stored(self, stored, message_id=""):
        """为旧版本写入的队列记录恢复可用的会话串行键。"""
        if not isinstance(stored, dict):
            return f"legacy:{message_id}"
        if stored.get("_wemai_inbound_event") == 1:
            return self._inbound_session_key(
                stored.get("chat_name", ""),
                stored.get("data", {}),
            )
        info = stored.get("message_info") or stored.get("messageInfo") or {}
        group = info.get("group_info") or info.get("groupInfo") or {}
        user = info.get("user_info") or info.get("userInfo") or {}
        group_id = self._text(group.get("group_id") or group.get("groupId"))
        if group_id:
            return f"group:legacy:{group_id}"
        user_id = self._text(user.get("user_id") or user.get("userId"))
        if user_id:
            return f"private:legacy:{user_id}"
        return f"legacy:{message_id}"

    async def _process_inbound_queue(self):
        """每个会话只取队首一条，在全局并发上限内投递可靠队列。"""
        while not self._stop_requested.is_set():
            active = getattr(self, "_inbound_delivery_tasks", {})
            capacity = max(0, INBOUND_ROUTER_CONCURRENCY - len(active))
            claimed = 0
            if capacity and self._router_is_connected():
                now = time.time()
                active_sessions = tuple(active)
                active_filter = ""
                if active_sessions:
                    placeholders = ",".join("?" for _ in active_sessions)
                    active_filter = (
                        "AND current.session_key NOT IN ("
                        f"{placeholders}) "
                    )
                with contextlib.closing(self._connect()) as db, db:
                    rows = db.execute(
                        "SELECT current.message_id,current.payload,current.attempts,"
                        "current.session_key FROM inbound AS current "
                        "WHERE current.state='pending' AND current.next_try<=? "
                        + active_filter +
                        "AND NOT EXISTS ("
                        "SELECT 1 FROM inbound AS older "
                        "WHERE older.session_key=current.session_key "
                        "AND older.state IN ('pending','sending') "
                        "AND (older.created<current.created OR "
                        "(older.created=current.created AND older.rowid<current.rowid))"
                        ") ORDER BY current.created,current.rowid LIMIT ?",
                        (now, *active_sessions, capacity),
                    ).fetchall()
                    for row in rows:
                        message_id, payload, attempts, session_key = row
                        updated = db.execute(
                            "UPDATE inbound SET state='sending' "
                            "WHERE message_id=? AND state='pending'",
                            (message_id,),
                        ).rowcount
                        if updated != 1:
                            continue
                        task = asyncio.create_task(
                            self._deliver_inbound_row(
                                message_id,
                                payload,
                                attempts,
                                session_key,
                            )
                        )
                        active[session_key] = task
                        task.add_done_callback(
                            lambda done, key=session_key: self._inbound_delivery_done(
                                key, done
                            )
                        )
                        claimed += 1
            if claimed:
                continue
            wakeup = self._inbound_wakeup
            wakeup.clear()
            # 先清事件再做最后一次就绪查询，可封住“查询为空后、开始等待前”入队的
            # 竞态：新记录要么被下方查询看到，要么会再次设置唤醒事件。
            if capacity and self._router_is_connected():
                active_sessions = tuple(active)
                active_filter = ""
                if active_sessions:
                    placeholders = ",".join("?" for _ in active_sessions)
                    active_filter = (
                        "AND current.session_key NOT IN ("
                        f"{placeholders}) "
                    )
                with contextlib.closing(self._connect()) as db, db:
                    ready = db.execute(
                        "SELECT 1 FROM inbound AS current "
                        "WHERE current.state='pending' AND current.next_try<=? "
                        + active_filter +
                        "AND NOT EXISTS ("
                        "SELECT 1 FROM inbound AS older "
                        "WHERE older.session_key=current.session_key "
                        "AND older.state IN ('pending','sending') "
                        "AND (older.created<current.created OR "
                        "(older.created=current.created AND older.rowid<current.rowid))"
                        ") LIMIT 1",
                        (time.time(), *active_sessions),
                    ).fetchone()
                if ready:
                    continue
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(wakeup.wait(), timeout=0.5)

    def _inbound_delivery_done(self, session_key, task):
        active = getattr(self, "_inbound_delivery_tasks", {})
        if active.get(session_key) is task:
            active.pop(session_key, None)
        if not task.cancelled():
            error = task.exception()
            if error is not None:
                logger.error(
                    "入站会话投递任务异常 session=%s: %s",
                    session_key,
                    error,
                    exc_info=(type(error), error, error.__traceback__),
                )
        wakeup = getattr(self, "_inbound_wakeup", None)
        if wakeup is not None:
            wakeup.set()

    async def _deliver_inbound_row(
        self, message_id, payload, attempts, session_key
    ):
        """构建并投递一条已认领记录；任何失败都会可靠退回待处理状态。"""
        stored = json.loads(payload)
        event_data = {}
        try:
            if stored.get("_wemai_inbound_event") == 1:
                event_data = dict(stored["data"])
                cached_message_id = (
                    event_data.pop("_wemai_message_id", None) or message_id
                )
                message = await self._run_blocking(
                    self._build_message,
                    stored["chat_name"],
                    event_data,
                    cached_message_id,
                )
            else:
                message = MessageBase.from_dict(stored)
            router_started_at = time.monotonic()
            delivered = await self.router.send_message(message)
            if delivered is False:
                raise ConnectionError("MaiBot Router 未确认 WebSocket 写出")
            router_elapsed = time.monotonic() - router_started_at
            with contextlib.closing(self._connect()) as db, db:
                db.execute(
                    "UPDATE inbound SET state='sent' WHERE message_id=? "
                    "AND state='sending'",
                    (message_id,),
                )
            self._delete_delivered_inbound_file(event_data)
            logger.info(
                "已转发至 MaiBot id=%s session=%s router_elapsed=%.3fs",
                message_id,
                session_key,
                router_elapsed,
            )
        except asyncio.CancelledError:
            with contextlib.closing(self._connect()) as db, db:
                db.execute(
                    "UPDATE inbound SET state='pending' WHERE message_id=? "
                    "AND state='sending'",
                    (message_id,),
                )
            raise
        except Exception as exc:
            attempts += 1
            permanent = isinstance(exc, (TypeError, ValueError))
            if permanent or attempts >= 10:
                self._store_dead_letter(
                    message_id,
                    {"direction": "inbound", "message": stored},
                    exc,
                )
                with contextlib.closing(self._connect()) as db, db:
                    db.execute(
                        "UPDATE inbound SET state='dead',attempts=? "
                        "WHERE message_id=?",
                        (attempts, message_id),
                    )
            else:
                with contextlib.closing(self._connect()) as db, db:
                    db.execute(
                        "UPDATE inbound SET state='pending',attempts=?,next_try=? "
                        "WHERE message_id=?",
                        (
                            attempts,
                            time.time() + min(300, 2 ** attempts),
                            message_id,
                        ),
                    )
            if permanent:
                logger.error(
                    "入站消息包含不可恢复的数据，已立即进入死信以解除会话阻塞 "
                    "id=%s session=%s: %s",
                    message_id,
                    session_key,
                    exc,
                )
            else:
                logger.warning(
                    "入站投递失败，将按会话顺序重试 id=%s session=%s attempt=%d: %s",
                    message_id,
                    session_key,
                    attempts,
                    exc,
                )

    @staticmethod
    def _delete_delivered_inbound_file(event_data):
        """释放已投递的入站临时媒体，绝不触碰用户文件。"""
        if not event_data:
            return
        # 文件段通过本地路径交给 MaiBot，Router 返回只表示 WebSocket
        # 已发出，不代表插件已经读取文件；保留文件到周期 GC，避免
        # 大文件在下游异步读取前被删除。图片/语音已经编码进消息，可即时清理。
        if str(event_data.get("type", "")).casefold() == "file":
            return
        expected_dir = os.path.abspath(IMAGE_SAVE_DIR)

        def remove_path(content):
            if not isinstance(content, str) or not content:
                return
            try:
                abs_path = os.path.abspath(content)
                if os.path.commonpath([abs_path, expected_dir]) != expected_dir:
                    return
            except (TypeError, ValueError):
                return
            if not os.path.basename(abs_path).startswith(IMAGE_TEMP_PREFIX):
                return
            try:
                os.unlink(abs_path)
            except FileNotFoundError:
                return
            except OSError:
                logger.debug(
                    "释放入站媒体文件失败 path=%s", abs_path, exc_info=True
                )

        remove_path(event_data.get("content"))

        def walk(nodes, depth=0):
            if not isinstance(nodes, list) or depth > 4:
                return
            for node in nodes:
                if not isinstance(node, dict):
                    continue
                remove_path(node.get("localPath"))
                walk(node.get("chatRecordList"), depth + 1)

        walk(event_data.get("wx_chat_record_list"))

    def _build_message(self, chat_name, data, message_id=None):
        chat_name = self._text(chat_name)
        # 微信 4.x 的 Qt UIA 不提供可靠发送者名称。
        sender_name = self._text(data.get("sender")) or "unknown"
        content = self._text(data.get("content"))
        chat_type = data.get("chat_type")
        # chat_type 必须由本地数据库会话明确给出。系统事件仍属于原私聊或
        # 群聊，仅在下方通过明确系统标记识别，不能发明第三种聊天类型。
        if chat_type not in {"private", "group"}:
            raise ValueError(f"缺少可靠 chat_type: {chat_type!r}")
        # 群聊优先以微信会话ID生成稳定 group_id，群改名不会创建新聊天流；
        # chat_name 仍保留最新显示值。纯 UI 入站拿不到真 ID，只能按名称兼容回退。
        stable_chat_id = self._text(data.get("chat_id")) or chat_name
        # 用户优先以任意格式的微信真 ID 生成稳定 user_id；显示昵称只负责展示，
        # 修改资料或群名片不能改变人物主键。纯 UI 入站没有真 ID 时再按显示名回退。
        stable_sender_id = self._text(data.get("sender_id"))
        sender_identity_source = self._text(
            data.get("sender_identity_source")
        )
        if (
            not stable_sender_id
            and chat_type == "group"
            and self._text(data.get("chat_id"))
        ):
            # 兼容升级前已经进入可靠队列、但尚未构建的消息，也让直接调用处理器
            # 的嵌入方式获得相同身份恢复能力。此处仍是纯内存查询。
            stable_sender_id = self.resolve_sender_alias(
                stable_chat_id,
                sender_name,
                data.get("user_nickname"),
                data.get("group_card_name"),
            )
            if stable_sender_id:
                sender_identity_source = "persistent_alias"
        # 正常路径复用生产者预先计算的 message_id；旧调用和测试未传值时才补算。
        if not message_id:
            message_id = self._inbound_message_id(chat_name, data)
        # 可靠的微信原生 ID 必须原样交给 MaiBot。MaiBot 的 bot account、权限、
        # @ 判断和 Platform IO 路由都以原生账号为契约；只有上游确实拿不到原生
        # ID 时才使用隔离的哈希回退身份。
        profile_nickname = self._text(data.get("user_nickname")) or sender_name
        # 空 sender_id 不能据此
        # 判成系统消息。系统身份必须来自监听层的明确事件标记，或上游明确
        # 返回的系统账号 ID。
        is_wechat_system = bool(self._text(data.get("chat_id"))) and (
            self._text(data.get("wx_notice_type")) == "system"
            or (
                self._text(data.get("wx_message_type")) == "system"
                and stable_sender_id.casefold() in _WECHAT_SYSTEM_UPSTREAM_IDS
            )
        )
        if is_wechat_system:
            user_id = _WECHAT_SYSTEM_SENDER_ID
            display_nickname = _WECHAT_SYSTEM_SENDER_NAME
            user_id_source = "wechat_system_weixin"
        elif stable_sender_id:
            user_id = stable_sender_id
            display_nickname = profile_nickname
            user_id_source = (
                "wechat_persisted_sender_alias"
                if sender_identity_source == "persistent_alias"
                else "wechat_sender_wxid"
            )
        elif chat_type == "private" and self._text(data.get("chat_id")):
            # 私聊推送通常没有 talker，但 sessionId 就是对方微信真 ID。以它生成
            # user_id，可与同一用户在群聊中的身份合并；sender_name 仅作为最新展示名。
            user_id = self._text(data.get("chat_id"))
            display_nickname = profile_nickname
            user_id_source = "wechat_private_session_id"
        elif chat_type == "group" and self._text(data.get("chat_id")):
            # 群成员真 ID 暂不可用时，以稳定群 ID + 当前显示名建立隔离的
            # 临时身份。该命名空间不会与任何格式的真实微信 ID 冲突，且
            # 最重要的是不会再污染“微信系统消息”这一全局虚拟用户。
            fallback_identity = (
                f"{stable_chat_id}|{sender_name.casefold()}"
            )
            user_id = self._stable_id("user_fallback", fallback_identity)
            display_nickname = profile_nickname
            user_id_source = "wechat_group_display_fallback"
        else:
            # 纯 UIA 入站无法取得 chat_id 和 sender_id，只能按显示名建立隔离身份。
            # 这会在改名后产生新人物，但比把所有未知用户合并成系统账号更安全。
            legacy_identity = (
                f"{chat_name}|{sender_name}"
                if chat_type == "group"
                else sender_name
            )
            user_id = self._stable_id(chat_type, legacy_identity)
            display_nickname = profile_nickname
            user_id_source = f"ui_display_name_{chat_type}"
        logger.info(
            "user_id 计算 chat=%s chat_type=%s sender=%s "
            "source=%s user_id=%s user_nickname=%s sender_id=%s chat_id=%s",
            chat_name,
            chat_type,
            sender_name,
            user_id_source,
            user_id[:8],
            display_nickname,
            self._text(data.get("sender_id")) or "<empty>",
            self._text(data.get("chat_id")) or "<empty>",
        )
        group_card_name = self._text(data.get("group_card_name"))
        user = self._build_user_info(
            platform=self.platform,
            user_id=user_id,
            user_nickname=display_nickname,
            user_cardname=(group_card_name or display_nickname) if chat_type == "group" else None,
        )
        # 只要 chat_type 为 group 就构造 GroupInfo；群内系统事件同样保留在该群流，
        # 发送者使用稳定的“微信系统消息”虚拟身份。
        group = None
        group_id = None
        if chat_type == "group":
            group_id = stable_chat_id
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
        group_avatar_url = self._text(data.get("group_avatar_url"))
        if group_avatar_url and group_id:
            self._schedule_avatar_cache(
                group_avatar_url,
                "",
                group_id=group_id,
                target_type="group",
            )
        segment = self._inbound_segment(
            content,
            data.get("type"),
            file_name=data.get("file_name"),
            file_size=data.get("file_size"),
        )
        try:
            message_time = int(float(data.get("timestamp", time.time())))
        except (TypeError, ValueError) as exc:
            raise ValueError("消息 timestamp 必须是可转换为整数的时间戳") from exc
        additional_config = {}
        platform_account_id = self._text(
            data.get("self_id")
            or data.get("selfId")
            or getattr(self, "_platform_account_id", "")
        )
        if platform_account_id:
            self._platform_account_id = platform_account_id
            # 与 NapCat 入站保持一致。MaiBot 使用 account_id 参与 session_id
            # 计算；缺失该字段会继续命中升级前的旧聊天流。
            additional_config["self_id"] = platform_account_id
            additional_config["platform_io_account_id"] = platform_account_id
        additional_config["platform_io_scope"] = "wx"
        native_chat_id = self._text(data.get("chat_id"))
        if chat_type == "group" and native_chat_id:
            additional_config["platform_io_target_group_id"] = native_chat_id
        elif chat_type == "private":
            native_target_user_id = stable_sender_id or native_chat_id
            if native_target_user_id:
                additional_config["platform_io_target_user_id"] = native_target_user_id
        wx_message_type = self._text(
            data.get("wx_message_type") or data.get("type")
        ) or "text"
        additional_config.update({
            "wx_chat_type": chat_type,
            "wx_message_type": wx_message_type,
            "wx_wechat_type": data.get("wx_wechat_type"),
            "wx_app_type": data.get("wx_app_type"),
            "wx_local_message_id": data.get("wx_local_message_id"),
            "wx_server_message_id": self._text(
                data.get("wx_server_message_id") or data.get("rawid")
            ),
            "wx_sender_id": user_id,
            "wx_source_sender_id": stable_sender_id or user_id,
            "wx_conversation_id": native_chat_id or stable_chat_id,
            "wx_is_self": False,
        })
        raw_xml = self._text(data.get("raw_content"))
        if raw_xml.startswith("<"):
            additional_config["wx_raw_xml"] = raw_xml
        if avatar_url:
            additional_config["wx_user_avatar_url"] = avatar_url
        if group_avatar_url and group_id:
            additional_config["wx_group_avatar_url"] = group_avatar_url
        self_avatar_url = self._text(
            getattr(self, "_platform_account_avatar_url", "")
        )
        if self_avatar_url:
            additional_config["wx_self_avatar_url"] = self_avatar_url
        chat_record_list = data.get("wx_chat_record_list")
        if isinstance(chat_record_list, list):
            additional_config["wx_chat_record"] = {
                "title": self._text(data.get("wx_chat_record_title")),
                "nodes": chat_record_list,
            }
        for source_key, config_key in (
            ("wx_location", "wx_location"),
            ("wx_contact_card", "wx_contact_card"),
        ):
            value = data.get(source_key)
            if isinstance(value, dict):
                additional_config[config_key] = value
        app_data = data.get("wx_app")
        if isinstance(app_data, dict) and wx_message_type in {
            "link", "mini_program", "transfer", "red_packet", "video_channel"
        }:
            additional_config[f"wx_{wx_message_type}"] = app_data
        if wx_message_type in {"image", "emoji", "voice", "video", "file"}:
            media = {
                "name": self._text(data.get("file_name")),
                "size": data.get("file_size") or 0,
            }
            if data.get("file_md5"):
                media["md5"] = self._text(data.get("file_md5"))
            if isinstance(data.get("wx_video"), dict):
                media.update(data["wx_video"])
            additional_config["wx_media"] = media
        if wx_message_type in {"system", "recall", "pat"}:
            additional_config["wx_notice_type"] = wx_message_type
            additional_config["wx_notice_text"] = content
        if wx_message_type == "recall" and isinstance(data.get("wx_notice"), dict):
            additional_config["wx_recalled_message_id"] = self._text(
                data["wx_notice"].get("recalled_message_id")
            )
        if wx_message_type == "pat":
            pat_from_id = self._text(data.get("pat_from_wxid")) or user_id
            pat_target_id = self._text(data.get("pat_target_wxid"))
            pat_from_name = display_nickname or sender_name or pat_from_id
            pat_target_name = (
                self._text(getattr(self, "_platform_account_nickname", ""))
                if pat_target_id and pat_target_id == platform_account_id
                else pat_target_id
            ) or pat_target_id
            pat_text = content
            if pat_from_name and pat_target_name:
                pat_text = f'"{pat_from_name}" 拍了拍 "{pat_target_name}"'
            additional_config.update({
                "wx_pat_from_user_id": pat_from_id,
                "wx_pat_from_user_name": pat_from_name,
                "wx_patted_user_id": pat_target_id,
                "wx_patted_user_name": pat_target_name,
                "wx_pat_template": self._text(data.get("raw_content")),
                "wx_pat_text": pat_text,
                "wx_notice_text": pat_text,
            })
            # Legacy 时间线只渲染消息段；通知字段继续供插件识别。
            segment = Seg(type="text", data=pat_text or "[拍一拍]")
        if data.get("file_md5"):
            additional_config["wx_file_md5"] = self._text(data.get("file_md5"))
        info = BaseMessageInfo(
            platform=self.platform,
            message_id=message_id,
            time=message_time,
            user_info=user,
            group_info=group,
            additional_config=additional_config or None,
        )
        raw_message = self._text(data.get("raw_content"))
        if not raw_message and segment.type == "text":
            raw_message = segment.data
        segments = []
        mention_ids = data.get("mention_user_ids") or data.get("mentionUserIds") or []
        mention_users = data.get("mention_users") or data.get("mentionUsers") or []
        if chat_type == "group" and isinstance(mention_ids, (list, tuple)):
            if isinstance(mention_users, (list, tuple)):
                mention_users_by_id = {
                    self._text(item.get("userId") or item.get("user_id") or item.get("id")): item
                    for item in mention_users
                    if isinstance(item, dict)
                    and self._text(item.get("userId") or item.get("user_id") or item.get("id"))
                }
            else:
                mention_users_by_id = {}
            for mention_id in mention_ids:
                value = self._text(mention_id)
                if not value:
                    continue
                metadata = mention_users_by_id.get(value, {})
                target_nickname = self._text(
                    metadata.get("nickname") or metadata.get("userNickname")
                )
                target_cardname = self._text(
                    metadata.get("cardName") or metadata.get("card_name") or metadata.get("groupCardName")
                )
                at_data = {"target_user_id": value}
                if target_nickname:
                    at_data["target_user_nickname"] = target_nickname
                if target_cardname:
                    at_data["target_user_cardname"] = target_cardname
                segments.append(Seg(type="at", data=at_data))
        reply_to = self._text(
            data.get("reply_to_message_id")
            or data.get("replyToMessageId")
        )
        quote = data.get("quote")
        if not reply_to and isinstance(quote, dict):
            reply_to = self._text(
                quote.get("platformMessageId")
                or quote.get("messageId")
                or quote.get("message_id")
            )
        if reply_to:
            # MaiBot 只识别原生 reply 段。微信引用字段保存原消息服务器 rawid，
            # WeMai 对外使用 md5(platform/event/rawid)，因此在这里复现同一映射，
            # 让 MaiBot 能从消息缓存或数据库还原引用发送者和正文。
            self_quote = self._is_self_quote(quote)
            target_message_id = ""
            if self_quote:
                target_message_id = self._resolve_outgoing_reply_id(
                    quote,
                    user_id=user_id,
                    group_id=group_id,
                )
            else:
                target_message_id = self._wechat_message_id(reply_to)
            if target_message_id:
                segments.append(Seg(type="reply", data=target_message_id))
            elif self_quote and segment.type == "text":
                quote_sender = self._text(quote.get("accountName")) or WEMAI_BOT_NICKNAME
                quote_content = self._text(quote.get("content"))[:2000]
                segment = Seg(
                    type="text",
                    data=f"[回复了{quote_sender}的消息：{quote_content}]\n{segment.data}",
                )
                raw_message = segment.data
            quoted_type = 0
            if isinstance(quote, dict):
                try:
                    quoted_type = int(quote.get("wechatType") or 0)
                except (TypeError, ValueError):
                    quoted_type = 0
                quoted_kind = {
                    1: "text", 3: "image", 34: "voice", 42: "contact_card",
                    43: "video", 47: "emoji", 48: "location", 49: "link",
                }.get(quoted_type, "system")
                additional_config.update({
                    "reply_to": target_message_id,
                    "wx_quoted_server_message_id": reply_to,
                    "wx_quoted_wechat_type": quoted_type,
                    "wx_quoted_kind": quoted_kind,
                    "wx_quoted_sender_id": self._text(quote.get("senderWxid")),
                    "wx_quoted_sender_name": self._text(quote.get("accountName")),
                    "wx_quoted_is_self": self_quote,
                    "wx_quoted_preview": self._text(quote.get("content"))[:2000],
                })
            logger.info(
                "构建 MaiBot 引用段 source_rawid=%s target_rawid=%s target_id=%s self_quote=%s quote_content=%r",
                self._text(data.get("rawid")),
                reply_to,
                target_message_id or "<text-fallback>",
                self_quote,
                self._text(quote.get("content"))[:120]
                if isinstance(quote, dict)
                else "",
            )
        segments.append(segment)
        segments.extend(self._chat_record_media_segments(chat_record_list))
        segment_list = Seg(type="seglist", data=segments)
        return MessageBase(
            message_info=info,
            message_segment=segment_list,
            raw_message=raw_message or None,
        )

    def _inbound_message_id(self, chat_name, data):
        rawid = self._text(data.get("rawid"))
        if rawid:
            event_name = self._text(data.get("event")) or "message.new"
            return self._wechat_message_id(rawid, event_name)
        sender_name = self._text(data.get("sender")) or "unknown"
        content = self._text(data.get("content"))
        chat_type = data.get("chat_type")
        return hashlib.md5(
            (
                f"{self.platform}|message|{chat_type}|{chat_name}|{sender_name}|"
                f"{data.get('timestamp')}|{content}"
            ).encode(),
            usedforsecurity=False,
        ).hexdigest()

    def _wechat_message_id(self, rawid, event_name="message.new"):
        """把微信服务器消息 ID 映射为 WeMai 的确定性消息 ID。"""
        rawid = self._text(rawid)
        event_name = self._text(event_name) or "message.new"
        if not rawid:
            return ""
        return hashlib.md5(
            f"{self.platform}|{event_name}|{rawid}".encode(),
            usedforsecurity=False,
        ).hexdigest()

    @staticmethod
    def _quote_truthy(value):
        if value is True or value == 1:
            return True
        return isinstance(value, str) and value.strip().casefold() in {
            "1", "true", "yes", "sent", "outgoing",
        }

    def _is_self_quote(self, quote):
        if not isinstance(quote, dict):
            return False
        if self._quote_truthy(quote.get("targetIsSend")):
            return True
        account_name = self._text(quote.get("accountName"))
        return bool(
            account_name
            and WEMAI_BOT_NICKNAME
            and account_name.casefold() == WEMAI_BOT_NICKNAME.casefold()
        )

    @staticmethod
    def _normalize_quote_text(value):
        normalized = re.sub(r"\s+", " ", str(value or "")).strip()
        if normalized.casefold() in {
            "[image]",
            "[图片]",
            "[图片消息]",
            "[图片，识别中.....]",
        }:
            return "[image]"
        return normalized

    def _resolve_outgoing_reply_id(self, quote, *, user_id, group_id=None):
        """Resolve a WeChat self-message rawid to MaiBot's original send_api ID."""
        if not isinstance(quote, dict) or not MAIBOT_DATA_DIR:
            return ""
        quote_content = self._normalize_quote_text(quote.get("content"))
        try:
            target_time = datetime.fromtimestamp(
                float(quote.get("targetCreateTime"))
            )
        except (TypeError, ValueError, OSError, OverflowError):
            return ""
        if not quote_content:
            return ""
        database = Path(MAIBOT_DATA_DIR) / "MaiBot.db"
        if not database.is_file():
            return ""
        start = (target_time - timedelta(seconds=5)).isoformat(
            sep=" ", timespec="microseconds"
        )
        end = (target_time + timedelta(seconds=5)).isoformat(
            sep=" ", timespec="microseconds"
        )
        try:
            uri = database.resolve().as_uri() + "?mode=ro"
            with contextlib.closing(
                sqlite3.connect(uri, uri=True, timeout=0.1)
            ) as db:
                db.execute("PRAGMA query_only=ON")
                params = [self.platform, start, end]
                if group_id:
                    group_ids = [group_id]
                    legacy_group_id = self._stable_id("group", group_id)
                    if legacy_group_id not in group_ids:
                        group_ids.append(legacy_group_id)
                    placeholders = ",".join("?" for _ in group_ids)
                    scope_sql = f"group_id IN ({placeholders})"
                    params.extend(group_ids)
                else:
                    user_ids = [user_id]
                    legacy_user_id = self._stable_id("user", user_id)
                    if legacy_user_id not in user_ids:
                        user_ids.append(legacy_user_id)
                    user_placeholders = ",".join("?" for _ in user_ids)
                    sessions = [
                        row[0]
                        for row in db.execute(
                            "SELECT session_id FROM chat_sessions "
                            f"WHERE platform=? AND user_id IN ({user_placeholders}) "
                            "AND group_id IS NULL "
                            "ORDER BY last_active_timestamp DESC LIMIT 5",
                            (self.platform, *user_ids),
                        )
                        if row[0]
                    ]
                    if not sessions:
                        sessions = [
                            row[0]
                            for row in db.execute(
                                "SELECT session_id FROM mai_messages "
                                f"WHERE platform=? AND user_id IN ({user_placeholders}) "
                                "AND group_id IS NULL "
                                "GROUP BY session_id ORDER BY MAX(timestamp) DESC LIMIT 5",
                                (self.platform, *user_ids),
                            )
                            if row[0]
                        ]
                    if not sessions:
                        return ""
                    placeholders = ",".join("?" for _ in sessions)
                    scope_sql = f"session_id IN ({placeholders})"
                    params.extend(sessions)
                rows = db.execute(
                    "SELECT message_id,timestamp,processed_plain_text "
                    "FROM mai_messages WHERE platform=? "
                    "AND message_id LIKE 'send_api_%' "
                    "AND timestamp BETWEEN ? AND ? AND " + scope_sql,
                    params,
                ).fetchall()
        except (OSError, sqlite3.Error):
            logger.warning("查询 MaiBot 自发引用失败", exc_info=True)
            return ""
        matches = []
        for message_id, timestamp, content in rows:
            if self._normalize_quote_text(content) != quote_content:
                continue
            try:
                candidate_time = datetime.fromisoformat(str(timestamp))
            except (TypeError, ValueError):
                continue
            matches.append(
                (abs((candidate_time - target_time).total_seconds()), str(message_id))
            )
        matches.sort()
        if not matches or matches[0][0] > 5:
            return ""
        if len(matches) > 1 and abs(matches[1][0] - matches[0][0]) < 0.25:
            logger.warning(
                "MaiBot 自发引用匹配不唯一 target_time=%s candidates=%s",
                target_time,
                matches[:3],
            )
            return ""
        logger.info(
            "MaiBot 自发引用已解析 target_id=%s time_delta=%.3fs group_id=%s",
            matches[0][1],
            matches[0][0],
            group_id or "<private>",
        )
        return matches[0][1]

    def is_inbound_known(self, rawid, event_name="message.new"):
        """快速检查本地数据库重放事件是否已经进入可靠队列。"""
        message_id = self._wechat_message_id(rawid, event_name)
        if not message_id:
            return False
        with self._connect() as db:
            return db.execute(
                "SELECT 1 FROM inbound WHERE message_id=? LIMIT 1",
                (message_id,),
            ).fetchone() is not None

    def _inbound_segment(
        self,
        content,
        message_type=None,
        file_name=None,
        file_size=None,
    ):
        is_declared_image = str(message_type or "").casefold() == "image"
        is_declared_file = str(message_type or "").casefold() == "file"
        is_declared_voice = str(message_type or "").casefold() == "voice"
        is_declared_emoji = str(message_type or "").casefold() == "emoji"
        has_local_file = isinstance(content, (str, os.PathLike)) and os.path.isfile(content)
        if is_declared_image:
            if has_local_file:
                try:
                    size = os.path.getsize(content)
                    if size > MAX_MEDIA_BYTES:
                        raise ValueError("入站图片超过尺寸上限")
                    raw = Path(content).read_bytes()
                    self._validate_image(raw[:16])
                    return Seg(type="image", data=base64.b64encode(raw).decode("ascii"))
                except (OSError, ValueError) as exc:
                    logger.warning("入站图片无法转换，降级为文字 path=%s: %s", content, exc)
            else:
                logger.warning("入站图片缺少可读本地文件，降级为文字 path=%r", content)
            return Seg(type="text", data="[图片消息]")
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
            if has_local_file:
                try:
                    source_path = Path(content).resolve()
                    media_root = Path(IMAGE_SAVE_DIR).resolve()
                    if source_path.is_relative_to(media_root):
                        size = source_path.stat().st_size
                        if size > FILE_MAX_BYTES:
                            raise ValueError("入站文件超过 FILE_MAX_BYTES")
                        # 文件内容保留为本地路径，避免把 200 MiB 文件编码
                        # 成 base64 放进 WebSocket 消息；入站文件由 MaiBot
                        # 的文件段消费者读取该受控媒体目录。
                        return Seg(
                            type="file",
                            data={
                                "name": source_path.name,
                                "size": size,
                                "url": source_path.as_uri(),
                            },
                        )
                except (OSError, ValueError) as exc:
                    logger.warning("读取入站文件失败 path=%s: %s", content, exc)
            source_name = str(file_name or "").strip()
            if not source_name and content:
                source_name = Path(os.fspath(content)).name
            source_name = Path(source_name.replace("\\", "/")).name
            safe_name = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", source_name).strip(" .")
            safe_name = safe_name or "微信文件.bin"
            payload = {"name": safe_name}
            try:
                normalized_size = int(file_size)
            except (TypeError, ValueError):
                normalized_size = 0
            if normalized_size > 0:
                payload["size"] = normalized_size
            return Seg(type="file", data=payload)
        return Seg(type="text", data=content)

    def _chat_record_media_segments(self, nodes):
        """把合并聊天记录中已恢复的图片和表情追加为原生媒体段。"""

        if not isinstance(nodes, list):
            return []
        media_root = Path(IMAGE_SAVE_DIR).resolve()
        segments = []
        seen = set()

        def walk(items, depth=0):
            if depth > 4 or len(segments) >= 12:
                return
            for item in items:
                if not isinstance(item, dict):
                    continue
                local_path = self._text(item.get("localPath"))
                media_type = self._text(item.get("localMediaType")).casefold()
                if local_path and media_type in {"image", "emoji"}:
                    try:
                        source = Path(local_path).resolve(strict=True)
                        if not source.is_relative_to(media_root):
                            raise ValueError("合并记录媒体路径超出 WeMai 媒体目录")
                        key = os.path.normcase(os.fspath(source))
                        if key not in seen:
                            converted = self._inbound_segment(source, media_type)
                            if converted.type in {"image", "emoji"}:
                                seen.add(key)
                                segments.append(converted)
                    except (OSError, ValueError) as exc:
                        logger.warning(
                            "读取合并聊天记录媒体失败 path=%s: %s",
                            local_path,
                            exc,
                        )
                nested = item.get("chatRecordList")
                if isinstance(nested, list):
                    walk(nested, depth + 1)
                if len(segments) >= 12:
                    return

        walk(nodes)
        return segments

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
        # avatar_url on an inbound group message belongs to the SENDER, not
        # the chatroom.  Writing it as ``group_<group_id>`` made whichever
        # member spoke first become the group's avatar and mixed two distinct
        # cache namespaces.  Each sender already has a stable wxid-derived
        # user_id, so one user target is both sufficient and correct.
        return [(directory, user)]

    def _group_avatar_paths(self, group_id):
        """Return MaiBot's separate ``group_<group_id>`` cache target."""
        if not MAIBOT_DATA_DIR:
            return []
        platform = self._avatar_component(str(self.platform).lower())
        group = self._avatar_component(group_id)
        if not platform or not group:
            return []
        directory = Path(MAIBOT_DATA_DIR) / "avatar" / platform
        return [(directory, f"group_{group}")]

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

    @staticmethod
    def _avatar_difference_hash(raw):
        """Return a compact visual fingerprint without retaining decoded pixels."""
        with Image.open(BytesIO(raw)) as image:
            width, height = image.size
            gray = image.convert("L").resize((17, 16), Image.Resampling.LANCZOS)
            fingerprint = 0
            for y in range(16):
                for x in range(16):
                    fingerprint = (fingerprint << 1) | int(
                        gray.getpixel((x, y)) > gray.getpixel((x + 1, y))
                    )
        return (width, height), fingerprint

    @classmethod
    def _is_unavailable_avatar(cls, raw):
        """识别微信“暂时无法查看”头像，不把占位图绑定给真实用户。"""
        try:
            (width, height), fingerprint = cls._avatar_difference_hash(raw)
        except Exception:
            # 图片头已经在下载阶段校验；识别器异常时保持原有兼容行为。
            logger.debug("微信头像占位图识别失败", exc_info=True)
            return False
        if width != height or not (
            _UNAVAILABLE_AVATAR_MIN_SIZE
            <= width
            <= _UNAVAILABLE_AVATAR_MAX_SIZE
        ):
            return False
        distance = (fingerprint ^ _UNAVAILABLE_AVATAR_DHASH).bit_count()
        return distance <= _UNAVAILABLE_AVATAR_MAX_HASH_DISTANCE

    def _discard_cached_unavailable_avatar(self, targets):
        """删除已经落盘的同类占位图；任何非占位头像都原样保留。"""
        removed = 0
        for directory, stem in targets:
            for old_suffix in _AVATAR_SUFFIXES:
                old_path = directory / f"{stem}{old_suffix}"
                if not old_path.is_file():
                    continue
                try:
                    is_placeholder = self._is_unavailable_avatar(
                        old_path.read_bytes()
                    )
                except OSError:
                    continue
                if not is_placeholder:
                    continue
                try:
                    old_path.unlink()
                    removed += 1
                except OSError:
                    logger.debug("删除旧头像占位图失败 path=%s", old_path, exc_info=True)
        return removed

    def _cache_avatar(
        self,
        avatar_url,
        user_id,
        group_id=None,
        *,
        target_type="user",
    ):
        """把微信头像下载到 MaiBot 文件缓存。"""
        if target_type == "group":
            targets = self._group_avatar_paths(group_id)
            target_label = f"group:{group_id}"
        else:
            targets = self._avatar_paths(user_id, group_id=group_id)
            target_label = f"user:{user_id}"
        if not avatar_url or not targets:
            return False
        try:
            raw, suffix = self._download_avatar(avatar_url)
            if self._is_unavailable_avatar(raw):
                removed = self._discard_cached_unavailable_avatar(targets)
                logger.info(
                    "忽略微信暂不可用头像 user_id=%s removed_stale=%d",
                    target_label,
                    removed,
                )
                # 视为本轮处理完成，让相同 URL 进入短期去重；头像 URL 变化后
                # 仍会立即下载真实头像，不会覆盖已有的正确本地文件。
                return True
            for directory, stem in targets:
                directory.mkdir(parents=True, exist_ok=True)
                destination = directory / f"{stem}{suffix}"
                # The old adapter cached the first image forever, which left
                # wrong/group-wide placeholders attached to a user.  Compare
                # content and atomically replace stale files.  Remove a stale
                # file with another supported suffix only after the new image
                # is safely in place.
                if destination.is_file() and destination.read_bytes() == raw:
                    continue
                fd, temporary = tempfile.mkstemp(prefix=f".{stem}.", dir=directory)
                try:
                    with os.fdopen(fd, "wb") as stream:
                        stream.write(raw)
                    os.replace(temporary, destination)
                    for old_suffix in _AVATAR_SUFFIXES:
                        old_path = directory / f"{stem}{old_suffix}"
                        if old_path != destination:
                            with contextlib.suppress(OSError):
                                old_path.unlink()
                except BaseException:
                    with contextlib.suppress(OSError):
                        os.unlink(temporary)
                    raise
            return True
        except Exception as exc:
            logger.warning(
                "缓存 MaiBot 头像失败 user_id=%s group_id=%s url=%s: %s",
                target_label,
                group_id,
                avatar_url,
                exc,
            )
            return False

    def _schedule_avatar_cache(
        self,
        avatar_url,
        user_id,
        group_id=None,
        *,
        target_type="user",
    ):
        if not MAIBOT_DATA_DIR:
            return
        lock = getattr(self, "_avatar_lock", None)
        if lock is None:
            lock = self._avatar_lock = threading.RLock()
            self._avatar_downloads = set()
            self._avatar_cache_urls = {}
        completed = getattr(self, "_avatar_cache_urls", None)
        if completed is None:
            completed = self._avatar_cache_urls = {}
        target_id = group_id if target_type == "group" else user_id
        if not target_id:
            return
        target_key = (target_type, target_id)
        key = (avatar_url, target_type, target_id)
        with lock:
            cached = completed.get(target_key)
            if cached and cached[0] == avatar_url and cached[1] > time.monotonic():
                return
            if key in self._avatar_downloads:
                return
            self._avatar_downloads.add(key)

        def cache():
            try:
                if self._cache_avatar(
                    avatar_url,
                    user_id,
                    group_id=group_id,
                    target_type=target_type,
                ):
                    with lock:
                        completed[target_key] = (
                            avatar_url,
                            time.monotonic() + 6 * 60 * 60,
                        )
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
        return hashlib.md5(value.encode("utf-8"), usedforsecurity=False).hexdigest()

    def resolve_sender_alias(self, chat_id, *display_names):
        """从内存中恢复群成员真 ID；歧义或未命中时返回空字符串。"""
        aliases = getattr(self, "_sender_aliases", None)
        if aliases is None:
            return ""
        return aliases.resolve(chat_id, *display_names)

    def observe_sender_alias(self, chat_id, sender_wxid, *display_names):
        """异步持久化由微信本地数据库确认的群成员名称。"""
        aliases = getattr(self, "_sender_aliases", None)
        if aliases is None:
            return 0
        return aliases.observe(chat_id, sender_wxid, *display_names)

    def _remember(self, identifier, name, chat_type):
        with self._id_lock:
            self._id_to_name[identifier] = {"name": name, "type": chat_type, "updated": time.time()}
            with self._connect() as db:
                db.execute(
                    "INSERT OR REPLACE INTO id_map(identifier,name,type,updated) VALUES(?,?,?,?)",
                    (identifier, name, chat_type, time.time()),
                )
                # The cap is enforced periodically by ``_run_id_map_gc``;
                # doing it on every INSERT meant a high-fan-out chat
                # would do a full scan per inbound message.

    def _resolve_receiver(self, info):
        if info is None:
            return None
        candidates = []
        receiver = getattr(info, "receiver_info", None)
        if receiver:
            candidates.extend((getattr(receiver, "group_info", None),
                               getattr(receiver, "user_info", None)))
        candidates.extend((getattr(info, "group_info", None), getattr(info, "user_info", None)))
        platform_account_id = self._text(
            getattr(self, "_platform_account_id", "")
        )
        bot_ids = {
            self._stable_id("bot", WEMAI_BOT_NICKNAME or "self"),
            platform_account_id,
        } - {""}
        bot_names = {"WeMai", WEMAI_BOT_NICKNAME} - {""}
        # 只有稳定 ID 可作为权威路由依据，昵称不作为路由键。
        for value in candidates:
            if not value:
                continue
            identifier = getattr(value, "group_id", None) or getattr(value, "user_id", None)
            if identifier and identifier not in bot_ids:
                mapped = self._id_to_name.get(identifier)
                if mapped:
                    name = mapped["name"] if isinstance(mapped, dict) else mapped
                    if name not in bot_names:
                        return name
        config = getattr(info, "additional_config", None)
        if config:
            # MaiBot normally echoes the destination through receiver_info.
            # Keep the native-ID metadata as a complete fallback for both
            # group and private replies, matching the inbound fields above.
            for key in (
                "platform_io_target_group_id",
                "platform_io_target_user_id",
            ):
                if isinstance(config, dict):
                    target = config.get(key)
                else:
                    target = getattr(config, key, None)
                target = self._text(target)
                if not target or target in bot_ids:
                    continue
                mapped = self._id_to_name.get(target)
                name = (
                    mapped.get("name")
                    if isinstance(mapped, dict)
                    else mapped
                ) if mapped else None
                if name and name not in bot_names:
                    return name
        return None

    def _load_id_map(self):
        # 从旧版 {id: name|record} JSON 文件执行一次性迁移。
        try:
            with open(ID_MAP_FILE, encoding="utf-8") as stream:
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
        db = sqlite3.connect(self._db_path, timeout=10)
        # 这两个 PRAGMA 都是连接级设置；每个新连接都必须重复设置。
        db.execute("PRAGMA busy_timeout=10000")
        db.execute("PRAGMA wal_autocheckpoint=100")
        return db

    def _init_storage(self):
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        with contextlib.closing(self._connect()) as db, db:
            db.execute("PRAGMA journal_mode=WAL")
            # 入站表每条消息至少产生一次 INSERT 和一次状态 UPDATE，属于高写入面。
            # 将自动 checkpoint 从默认约 1000 页降到 100 页，可把稳定态 WAL 从
            # 约 4MB 控制到约 400KB；额外 fsync 成本相对单条入站写入可以忽略。
            db.execute("PRAGMA wal_autocheckpoint=100")
            db.execute("PRAGMA busy_timeout=10000")
            db.execute("CREATE TABLE IF NOT EXISTS id_map (identifier TEXT PRIMARY KEY, "
                       "name TEXT NOT NULL, type TEXT, updated REAL NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS inbound (message_id TEXT PRIMARY KEY, "
                       "payload TEXT NOT NULL, state TEXT NOT NULL, attempts INTEGER NOT NULL, "
                       "next_try REAL NOT NULL, created REAL NOT NULL, "
                       "session_key TEXT NOT NULL DEFAULT '')")
            inbound_columns = {
                row[1] for row in db.execute("PRAGMA table_info(inbound)")
            }
            if "session_key" not in inbound_columns:
                db.execute(
                    "ALTER TABLE inbound ADD COLUMN "
                    "session_key TEXT NOT NULL DEFAULT ''"
                )
            legacy_rows = db.execute(
                "SELECT message_id,payload FROM inbound "
                "WHERE session_key='' AND state IN ('pending','sending')"
            ).fetchall()
            for message_id, payload in legacy_rows:
                try:
                    stored = json.loads(payload)
                    session_key = self._inbound_session_key_from_stored(
                        stored, message_id
                    )
                except (TypeError, ValueError, json.JSONDecodeError):
                    session_key = f"legacy:{message_id}"
                db.execute(
                    "UPDATE inbound SET session_key=? WHERE message_id=?",
                    (session_key, message_id),
                )
            # A process can stop after claiming a row but before recording its
            # result. Requeue those rows; message IDs keep MaiBot dedup stable.
            db.execute("UPDATE inbound SET state='pending' WHERE state='sending'")
            db.execute(
                "CREATE INDEX IF NOT EXISTS inbound_ready_idx "
                "ON inbound(state,next_try,created)"
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS inbound_session_order_idx "
                "ON inbound(session_key,state,created)"
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
                    stored = data["message"]
                    session_key = self._inbound_session_key_from_stored(
                        stored, message_id
                    )
                    db.execute(
                        "INSERT OR REPLACE INTO inbound("
                        "message_id,payload,state,attempts,next_try,created,session_key"
                        ") VALUES(?,?,'pending',0,0,?,?)",
                        (
                            message_id,
                            json.dumps(stored, ensure_ascii=False),
                            time.time(),
                            session_key,
                        ),
                    )
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
