"""Single-threaded wxauto/UIA listener and command executor."""

import logging
import queue
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeoutError
from datetime import datetime

from chat_name_utils import chat_names_equal, normalize_chat_name

from config import (
    IMAGE_AUTO_DOWNLOAD,
    UI_QUEUE_SIZE,
    WX_EXCLUDED_CHATS,
    WX_LISTEN_ALL_IF_EMPTY,
    WX_TARGET_CHATS,
)

logger = logging.getLogger(__name__)

CHAT_RETRY_MAX_DELAY = 30
CHAT_RETRY_CRITICAL_AFTER = 8
LISTENER_HEALTH_INTERVAL = 5
CHAT_TITLE_REFRESH_INTERVAL = 30
MESSAGE_FAILURE_REBUILD_AFTER = 3
_DYNAMIC_TITLE_SUFFIX = re.compile(r"\s*(?:\(\d+\)|（\d+）|\[\d+\])\s*$")


@dataclass
class UICommand:
    action: str
    args: tuple
    timeout: float
    command_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    created: float = field(default_factory=time.monotonic)
    future: Future = field(default_factory=Future)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    started: bool = False
    cancelled: bool = False

    @property
    def deadline(self):
        return self.created + self.timeout

    def cancel_if_pending(self):
        with self._lock:
            if self.started:
                return False
            self.cancelled = True
            self.future.cancel()
            return True

    def begin(self):
        with self._lock:
            if self.cancelled or time.monotonic() >= self.deadline:
                self.cancelled = True
                self.future.cancel()
                return False
            self.started = True
            return True


class UICommandTimeout(TimeoutError):
    def __init__(self, command_id, retry_safe, command_future=None):
        super().__init__(f"UI 命令超时 id={command_id} retry_safe={retry_safe}")
        self.command_id = command_id
        self.retry_safe = retry_safe
        self.command_future = command_future


@dataclass
class ChatRecoveryState:
    name: str
    failures: int = 0
    next_retry: float = 0
    disabled: bool = False
    reason: str = ""


class UICommandQueue:
    def __init__(self, maxsize=UI_QUEUE_SIZE):
        self._queue = queue.Queue(maxsize=maxsize)
        self._wake_event = threading.Event()

    def submit(self, action, *args, timeout=15):
        command = UICommand(action=action, args=args, timeout=float(timeout))
        try:
            self._queue.put(command, timeout=2)
        except queue.Full as exc:
            raise RuntimeError("UI 命令队列已满") from exc
        self._wake_event.set()
        try:
            return command.future.result(timeout=timeout)
        except FutureTimeoutError as exc:
            if command.future.done():
                return command.future.result()
            retry_safe = command.cancel_if_pending()
            if not retry_safe:
                try:
                    # Total caller wait is bounded at three times the requested timeout.
                    return command.future.result(timeout=max(float(timeout) * 2, 0))
                except FutureTimeoutError as started_exc:
                    if command.future.done():
                        return command.future.result()
                    raise UICommandTimeout(
                        command.command_id, retry_safe=False,
                        command_future=command.future,
                    ) from started_exc
            raise UICommandTimeout(command.command_id, retry_safe) from exc

    def get_nowait(self):
        return self._queue.get_nowait()

    def task_done(self):
        self._queue.task_done()

    def wait(self, timeout):
        """Sleep until a command arrives, retaining the listener poll timeout."""
        if self._queue.empty():
            self._wake_event.wait(timeout)
        self._wake_event.clear()


class WeChatListener:
    """Owns every wxauto object and uses it only from its creating thread."""

    def __init__(self, target_chats=None, callback=None, command_queue=None, stop_event=None,
                 heartbeat=None):
        from wxauto import WeChat

        self._owner_thread = threading.get_ident()
        self._wechat_class = WeChat
        self.wx = WeChat()
        self.target_specs = self._normalize_targets(target_chats)
        self.callback = callback
        self.commands = command_queue or UICommandQueue()
        self.stop_event = stop_event
        self.heartbeat = heartbeat
        self.listen_chats = {}
        self._desired_chats = {}
        self.chat_types = {normalize_chat_name(item["name"]): item.get("type") for item in self.target_specs
                           if item.get("type") in {"private", "group"}}
        self._failed_chats = {}
        self._chatwnd_cache = {}
        self._last_health_check = None
        self._title_check_at = {}
        self._message_poll_cursor = 0
        self._message_failures = {}
        self._command_active = False
        self._command_started = None
        self._recovery_active = False
        self._recovery_started = None
        self._reconnecting = False
        self.running = False
        logger.info("微信监听器初始化成功 account=%s", self.wx.nickname)

    @staticmethod
    def _normalize_targets(targets):
        result = []
        seen = set()
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
            if name and key not in seen:
                seen.add(key)
                result.append({"name": name, "type": chat_type})
        return result

    def _assert_ui_thread(self):
        if threading.get_ident() != self._owner_thread:
            raise RuntimeError("wxauto/UIA 操作只能在专用 UI 线程执行")

    def start_listening(self):
        self._assert_ui_thread()
        self.running = True
        names = [item["name"] for item in self.target_specs]
        if not names and WX_LISTEN_ALL_IF_EMPTY:
            names = [name for name in self.wx.GetSessionList(reset=True)
                     if name not in WX_EXCLUDED_CHATS]
        self._desired_chats = {normalize_chat_name(name): name for name in names}
        if names:
            retry_at = time.monotonic()
            self._failed_chats = {
                normalize_chat_name(name): ChatRecoveryState(
                    name=name, next_retry=retry_at, reason="等待初始化监听")
                for name in names
            }
        if not names:
            logger.info("最终有效配置为零监听；仅处理 MaiBot 到微信命令")

        lost_since = None
        last_alive_check = 0
        alive_check_failures = 0
        reconnect_attempts = 0
        next_reconnect = 0
        wait_for_command = getattr(self.commands, "wait", None)
        while self.running and not (self.stop_event and self.stop_event.is_set()):
            self._touch_heartbeat()
            self._drain_commands(limit=1)
            now = time.monotonic()
            use_poll_sleep = False
            if now - last_alive_check >= 5:
                last_alive_check = now
                if not self._is_wechat_alive():
                    use_poll_sleep = True
                    alive_check_failures += 1
                    logger.warning("微信窗口探测失败 count=%d/3", alive_check_failures)
                    if alive_check_failures >= 3 and lost_since is None:
                        lost_since = now
                        logger.error("连续探测不到微信窗口，进入恢复模式")
                    if lost_since is not None and now - lost_since > 600:
                        raise RuntimeError("微信窗口丢失超过 600 秒")
                else:
                    alive_check_failures = 0
                if lost_since is not None and not use_poll_sleep:
                    if now - lost_since > 600:
                        raise RuntimeError("微信窗口丢失超过 600 秒")
                    if now < next_reconnect:
                        self._touch_heartbeat()
                    else:
                        self._touch_heartbeat()
                        self._reconnecting = True
                        try:
                            reconnected = self._reconnect_wechat()
                        finally:
                            self._reconnecting = False
                            self._touch_heartbeat()
                        if reconnected:
                            logger.info("微信窗口重连成功 attempts=%d", reconnect_attempts + 1)
                            lost_since = None
                            reconnect_attempts = 0
                            next_reconnect = 0
                        else:
                            use_poll_sleep = True
                            reconnect_attempts += 1
                            delay = min(30, 2 ** min(reconnect_attempts, 5))
                            next_reconnect = now + delay
                            logger.warning("微信窗口重连失败，将在 %d 秒后重试 attempt=%d",
                                           delay, reconnect_attempts)
            self._check_listener_health()
            self._check_new_messages()
            self._retry_failed_chats()
            if use_poll_sleep:
                time.sleep(0.2)
            elif wait_for_command:
                wait_for_command(0.2)
            else:
                time.sleep(0.2)

    def stop_listening(self):
        self.running = False

    def close(self):
        self._assert_ui_thread()
        self.running = False
        self._cleanup_wechat()

    def _touch_heartbeat(self):
        if self.heartbeat:
            self.heartbeat()

    def _drain_commands(self, limit=1):
        self._assert_ui_thread()
        # A synchronous wxauto send may consume its whole internal timeout. Never
        # amplify that delay by executing a batch before polling inbound messages.
        for _ in range(min(max(int(limit), 0), 1)):
            if self.stop_event and self.stop_event.is_set():
                return
            try:
                command = self.commands.get_nowait()
            except queue.Empty:
                return
            try:
                if not command.begin():
                    continue
                self._command_active = True
                self._command_started = time.monotonic()
                self._touch_heartbeat()
                if command.action == "send":
                    command.future.set_result(self._send(*command.args))
                elif command.action == "stop":
                    self.running = False
                    command.future.set_result(True)
                else:
                    raise ValueError(f"未知 UI 命令: {command.action}")
            except BaseException as exc:
                if not command.future.done():
                    command.future.set_exception(exc)
            finally:
                self._touch_heartbeat()
                self._command_active = False
                self._command_started = None
                self.commands.task_done()

    def _send(self, receiver, kind, data):
        self._assert_ui_thread()
        if kind not in {"text", "image", "file"}:
            raise ValueError(f"不支持发送类型: {kind}")
        chat = self._ensure_chatwnd(receiver)
        try:
            if kind == "text":
                result = chat.SendMsg(data)
                return result is not False
            result = chat.SendFiles(data)
            if result is False:
                raise RuntimeError("SendFiles 返回 False")
            return True
        except Exception:
            key = normalize_chat_name(receiver)
            if not self._chatwnd_is_alive(chat):
                if key in getattr(self, "_desired_chats", {}):
                    self._mark_chat_failed(key, "发送时发现聊天窗口失效")
                else:
                    self._chatwnd_cache.pop(key, None)
            raise

    def _ensure_chatwnd(self, receiver):
        key = normalize_chat_name(receiver)
        wx = getattr(self, "wx", None)
        listen_chat = getattr(wx, "listen", {}).get(key)
        cached_chat = self._chatwnd_cache.get(key)
        for chat in (listen_chat, cached_chat):
            if chat is not None and self._chatwnd_is_alive(chat):
                self._chatwnd_cache[key] = chat
                if wx is not None and key in getattr(self, "_desired_chats", {}):
                    wx.listen[key] = chat
                return chat
        self._chatwnd_cache.pop(key, None)
        if listen_chat is not None and wx is not None:
            wx.listen.pop(key, None)

        from wxauto.elements import ChatWnd
        from wxauto import uiautomation as uia

        windows = self._find_chat_windows(receiver, uia)
        if len(windows) > 1:
            raise RuntimeError(f"存在多个规范化同名窗口: raw={receiver!r} normalized={key!r}")
        if not windows:
            selected = self.wx.ChatWith(receiver)
            if selected is False or not chat_names_equal(selected, receiver):
                raise RuntimeError(
                    f"ChatWith 未精确打开目标: raw={receiver!r} normalized={key!r} "
                    f"result={selected!r}")
            matches = []
            for item in self.wx.SessionBox.ListControl().GetChildren():
                try:
                    item_name, _ = self.wx.GetSessionAmont(item)
                except Exception:
                    continue
                if chat_names_equal(item_name, receiver):
                    matches.append(item)
            if len(matches) > 1:
                raise RuntimeError(f"存在 {len(matches)} 个同名会话，拒绝自动选择: {receiver!r}")
            if not matches:
                raise RuntimeError(
                    f"会话列表中不存在目标: raw={receiver!r} normalized={key!r}")
            matches[0].DoubleClick(simulateMove=False)
            deadline = time.time() + 5
            while time.time() < deadline:
                windows = self._find_chat_windows(receiver, uia)
                if len(windows) == 1:
                    break
                time.sleep(0.03)
            if len(windows) != 1:
                raise RuntimeError(
                    f"独立聊天窗口未出现: raw={receiver!r} normalized={key!r}")
        chat = ChatWnd(
            receiver,
            self.wx.language,
            uia_name=windows[0].Name,
            hwnd=getattr(windows[0], "NativeWindowHandle", None),
        )
        self._chatwnd_cache[key] = chat
        if key in getattr(self, "_desired_chats", {}):
            self._activate_listen_chat(key, receiver, chat)
        return chat

    @staticmethod
    def _find_chat_windows(name, uia):
        """Use the native exact-title lookup before the expensive UIA root scan."""
        key = normalize_chat_name(name)
        try:
            import win32gui

            hwnd = win32gui.FindWindow("ChatWnd", name)
            if hwnd and win32gui.IsWindow(hwnd):
                window = uia.ControlFromHandle(hwnd)
                if (window is not None
                        and getattr(window, "ClassName", "") == "ChatWnd"
                        and normalize_chat_name(getattr(window, "Name", "")) == key):
                    return [window]
        except Exception:
            logger.debug("原生窗口快速定位失败 chat=%s", name, exc_info=True)
        return [
            window for window in uia.GetRootControl().GetChildren()
            if getattr(window, "ClassName", "") == "ChatWnd"
            and normalize_chat_name(getattr(window, "Name", "")) == key
        ]

    @staticmethod
    def _chatwnd_is_alive(chat):
        try:
            import win32gui

            hwnd = getattr(chat, "HWND", None)
            if not hwnd or not win32gui.IsWindow(hwnd):
                return False
            get_class_name = getattr(win32gui, "GetClassName", None)
            return not get_class_name or get_class_name(hwnd) == "ChatWnd"
        except Exception:
            return False

    def _activate_listen_chat(self, key, name, chat):
        chat.savepic = IMAGE_AUTO_DOWNLOAD
        chat.savefile = False
        chat.savevoice = False
        self.wx.listen[key] = chat
        self._chatwnd_cache[key] = chat
        self.listen_chats[key] = name
        self._desired_chats.setdefault(key, name)
        self._failed_chats.pop(key, None)

    def _add_listen_chat(self, name, max_retries=1):
        self._assert_ui_thread()
        key = normalize_chat_name(name)
        self._desired_chats.setdefault(key, name)
        last_error = None
        for attempt in range(1, max_retries + 1):
            try:
                self._touch_heartbeat()
                self.wx.AddListenChat(name, savepic=IMAGE_AUTO_DOWNLOAD,
                                      savefile=False, savevoice=False)
                chat = self.wx.listen.get(key)
                if chat is None or not self._chatwnd_is_alive(chat):
                    raise RuntimeError("AddListenChat 未创建有效窗口")
                if not chat_names_equal(getattr(chat, "who", None), name):
                    raise RuntimeError(
                        f"监听窗口标题与目标不匹配 raw={name!r} normalized={key!r}")
                detected = self.chat_types.get(key) or self._detect_chat_type(chat)
                if detected not in {"private", "group"}:
                    raise RuntimeError("聊天类型不确定；请显式配置 {name,type}，拒绝监听")
                self.chat_types[key] = detected
                self._activate_listen_chat(key, name, chat)
                logger.info("监听已建立 chat=%s normalized=%r type=%s", name, key, detected)
                return True
            except Exception as exc:
                last_error = exc
                logger.warning("添加监听失败 chat=%s attempt=%d/%d: %s",
                               name, attempt, max_retries, exc)
                self._remove_listen(name, key=key, close=False)
                if attempt < max_retries:
                    time.sleep(attempt)
            finally:
                self._touch_heartbeat()
        self._schedule_chat_retry(key, name, last_error)
        return False

    def _detect_chat_type(self, chat):
        """Read the independent window title structure used by WeChat 3.9."""
        try:
            chat._show()
            title_parts = chat.UiaAPI.GetProgenyControl(11).GetChildren()
            if len(title_parts) == 1:
                return "private"
            if (len(title_parts) == 2
                    and re.fullmatch(r"\s*[（(]\d+[）)]\s*",
                                     str(getattr(title_parts[-1], "Name", "")))):
                return "group"
        except Exception as exc:
            logger.debug("聊天类型探测失败 chat=%s: %s", chat.who, exc)
        logger.warning("无法可靠探测聊天类型 chat=%s；不会默认按私聊处理", chat.who)
        return None

    def _check_new_messages(self):
        keys = list(getattr(self, "listen_chats", {}))
        if not keys or not getattr(self, "wx", None):
            return
        cursor = getattr(self, "_message_poll_cursor", 0)
        selected_key = keys[cursor % len(keys)]
        self._message_poll_cursor = cursor + 1
        try:
            polled = self.wx.GetListenMessage(selected_key) or []
            for key, exc in getattr(self.wx, "listen_errors", {}).items():
                failures = getattr(self, "_message_failures", None)
                if failures is None:
                    failures = self._message_failures = {}
                failures[key] = failures.get(key, 0) + 1
                count = failures[key]
                logger.warning("读取消息暂时失败 chat=%s count=%d/%d: %s",
                               key, count, MESSAGE_FAILURE_REBUILD_AFTER, exc)
                if count >= MESSAGE_FAILURE_REBUILD_AFTER:
                    failures.pop(key, None)
                    self._mark_chat_failed(key, f"连续读取消息失败: {exc}")
            if isinstance(polled, dict):
                message_batches = polled.items()
            else:
                chat = getattr(self.wx, "listen", {}).get(selected_key)
                message_batches = ((chat, polled),) if chat is not None else ()
            for chat, messages in message_batches:
                key = normalize_chat_name(chat.who)
                configured_name = self.listen_chats.get(key)
                if configured_name is None:
                    logger.warning("忽略未注册监听消息 raw=%r normalized=%r", chat.who, key)
                    continue
                for message in messages or []:
                    try:
                        self._process_message(configured_name, message)
                    except Exception:
                        logger.exception("处理微信消息失败，已隔离 chat=%s", configured_name)
            self._msg_fail_count = 0
            if selected_key not in getattr(self.wx, "listen_errors", {}):
                getattr(self, "_message_failures", {}).pop(selected_key, None)
        except Exception as exc:
            self._msg_fail_count = getattr(self, "_msg_fail_count", 0) + 1
            failures = getattr(self, "_message_failures", None)
            if failures is None:
                failures = self._message_failures = {}
            failures[selected_key] = failures.get(selected_key, 0) + 1
            count = failures[selected_key]
            logger.exception("检查微信消息失败 chat=%s count=%d/%d",
                             selected_key, count, MESSAGE_FAILURE_REBUILD_AFTER)
            if count >= MESSAGE_FAILURE_REBUILD_AFTER:
                logger.error("读取消息连续失败，重建单个监听 chat=%s", selected_key)
                failures.pop(selected_key, None)
                self._mark_chat_failed(selected_key, f"连续读取消息失败: {exc}")

    def _process_message(self, chat_name, message):
        key = normalize_chat_name(chat_name)
        msg_type = str(getattr(message, "type", ""))
        sender = getattr(message, "sender", None)
        content = getattr(message, "content", None)
        if content is None:
            content = ""
        elif not isinstance(content, str):
            content = str(content)
        if msg_type == "self" or sender == "Self":
            return
        if msg_type == "sys" and ("新消息" in content or
                                  (len(content.strip()) <= 10 and ":" in content)):
            return
        data = {"chat": chat_name, "chat_type": self.chat_types.get(key),
                "sender": str(sender or "未知用户"), "type": msg_type,
                "content": content, "timestamp": datetime.now().isoformat()}
        logger.info("收到微信消息 chat=%s type=%s length=%d", chat_name, msg_type, len(content))
        if self.callback:
            self.callback(chat_name, data)

    def _retry_failed_chats(self):
        now = time.monotonic()
        for key, state in list(self._failed_chats.items()):
            if state.disabled:
                logger.warning("恢复遗留的 disabled 监听状态 chat=%s", state.name)
                state.disabled = False
            if now < state.next_retry:
                continue
            logger.info("重试恢复监听 chat=%s attempt=%d", state.name, state.failures + 1)
            self._recovery_active = True
            self._recovery_started = time.monotonic()
            self._touch_heartbeat()
            try:
                self._add_listen_chat(state.name, max_retries=1)
            finally:
                self._touch_heartbeat()
                self._recovery_active = False
                self._recovery_started = None
            return

    def _schedule_chat_retry(self, key, name, error):
        state = self._failed_chats.get(key)
        if state is None:
            state = ChatRecoveryState(name=name)
            self._failed_chats[key] = state
        state.name = name
        state.failures += 1
        state.reason = str(error or "未知错误")
        if (state.failures >= CHAT_RETRY_CRITICAL_AFTER
                and (state.failures == CHAT_RETRY_CRITICAL_AFTER
                     or state.failures % 10 == 0)):
            logger.error("监听持续恢复失败，但仍会自动重试 chat=%s failures=%d reason=%s",
                         name, state.failures, state.reason)
        delay = min(CHAT_RETRY_MAX_DELAY, 2 ** min(max(0, state.failures - 1), 5))
        state.next_retry = time.monotonic() + delay
        logger.warning("监听恢复失败，将在 %d 秒后重试 chat=%s failures=%d reason=%s",
                       delay, name, state.failures, state.reason)

    def _mark_chat_failed(self, key, reason):
        name = (getattr(self, "_desired_chats", {}).get(key)
                or self.listen_chats.get(key) or key)
        state = self._failed_chats.get(key)
        if state is None:
            self._failed_chats[key] = ChatRecoveryState(
                name=name, next_retry=time.monotonic(), reason=reason)
            logger.warning("监听窗口失效，等待单独重建 chat=%s reason=%s", name, reason)
        else:
            state.reason = reason
        self._discard_listen_state(key)

    def _check_listener_health(self):
        if not getattr(self, "wx", None):
            return
        now = time.monotonic()
        last_check = getattr(self, "_last_health_check", None)
        if last_check is not None and now - last_check < LISTENER_HEALTH_INTERVAL:
            return
        self._last_health_check = now
        invalid = []
        for key, name in list(getattr(self, "listen_chats", {}).items()):
            chat = getattr(self.wx, "listen", {}).get(key)
            if chat is None or not self._chatwnd_is_alive(chat):
                invalid.append(key)
                continue
            self._chatwnd_cache[key] = chat
            self._refresh_chat_title(key, chat)
        for key in invalid:
            self._mark_chat_failed(key, "HWND 已失效或监听对象丢失")

        registered = set(getattr(self, "listen_chats", {}))
        for key in set(getattr(self.wx, "listen", {})) - registered:
            if key in getattr(self, "_desired_chats", {}):
                self._mark_chat_failed(key, "wx.listen 存在未注册残留对象")
            else:
                logger.warning("清理 wx.listen 多余对象 normalized=%r", key)
                self._discard_listen_state(key)
        for key, name in list(getattr(self, "_desired_chats", {}).items()):
            if key not in self.listen_chats and key not in self._failed_chats:
                self._failed_chats[key] = ChatRecoveryState(
                    name=name, next_retry=time.monotonic(), reason="监听对象缺失")

    def _refresh_chat_title(self, key, chat):
        now = time.monotonic()
        title_checks = getattr(self, "_title_check_at", None)
        if title_checks is None:
            title_checks = self._title_check_at = {}
        last_check = title_checks.get(key)
        if last_check is not None and now - last_check < CHAT_TITLE_REFRESH_INTERVAL:
            return
        title_checks[key] = now
        try:
            import win32gui

            actual_name = win32gui.GetWindowText(chat.HWND).strip()
        except Exception:
            return
        if not actual_name or actual_name == getattr(chat, "uia_name", None):
            return
        stable_name = _DYNAMIC_TITLE_SUFFIX.sub("", actual_name).strip()
        new_key = normalize_chat_name(stable_name)
        if not new_key or new_key == key:
            return
        if new_key != key and (new_key in self.listen_chats or new_key in self.wx.listen):
            logger.error("聊天窗口改名后与现有监听冲突 old=%s new=%s", key, new_key)
            return
        old_type = self.chat_types.pop(key, None)
        self._failed_chats.pop(key, None)
        self.wx.listen.pop(key, None)
        self.listen_chats.pop(key, None)
        self._chatwnd_cache.pop(key, None)
        desired_name = self._desired_chats.pop(key, stable_name)
        getattr(self, "_message_failures", {}).pop(key, None)
        self._title_check_at.pop(key, None)
        if hasattr(chat, "Rebind"):
            chat.Rebind(stable_name, chat.HWND)
        else:
            chat.uia_name = stable_name
            chat.usedmsgid = []
        chat.who = stable_name
        chat.chat_key = new_key
        self._desired_chats[new_key] = stable_name
        if old_type:
            self.chat_types[new_key] = old_type
        self._activate_listen_chat(new_key, stable_name, chat)
        logger.warning("监听聊天标题已更新并重新绑定 old=%s new=%s configured=%s",
                       key, new_key, desired_name)

    @staticmethod
    def _is_wechat_alive():
        try:
            from wxauto.utils import FindWindow
            return bool(FindWindow(classname="WeChatMainWndForPC"))
        except Exception:
            return False

    def _discard_listen_state(self, key):
        if getattr(self, "wx", None):
            getattr(self.wx, "listen", {}).pop(key, None)
        getattr(self, "listen_chats", {}).pop(key, None)
        getattr(self, "_chatwnd_cache", {}).pop(key, None)
        getattr(self, "_title_check_at", {}).pop(key, None)
        getattr(self, "_message_failures", {}).pop(key, None)

    def _remove_listen(self, name, key=None, close=True):
        key = key or normalize_chat_name(name)
        chat = getattr(self.wx, "listen", {}).pop(key, None)
        if chat and close:
            try:
                chat.Close()
            except Exception:
                logger.debug("关闭旧监听窗口失败 chat=%s", name, exc_info=True)
        self._discard_listen_state(key)

    def _cleanup_listeners(self):
        for key in list(getattr(self.wx, "listen", {})):
            name = getattr(self, "_desired_chats", {}).get(key, key)
            self._remove_listen(name, key=key)
        try:
            self.wx.listen.clear()
        except Exception:
            logger.warning("清空 wxauto 监听状态失败", exc_info=True)
        self.listen_chats.clear()
        self._chatwnd_cache.clear()
        getattr(self, "_title_check_at", {}).clear()
        getattr(self, "_message_failures", {}).clear()

    def _cleanup_wechat(self):
        if getattr(self, "wx", None):
            self._cleanup_listeners()

    def _reconnect_wechat(self):
        try:
            desired = list(getattr(self, "_desired_chats", {}).values())
            if not desired:
                desired = [item["name"] for item in self.target_specs]
            self.wx = self._wechat_class()
            self.listen_chats.clear()
            self._chatwnd_cache.clear()
            self._title_check_at = {}
            self._message_failures = {}
            self._last_health_check = None
            self._message_poll_cursor = 0
            self._desired_chats = {normalize_chat_name(name): name for name in desired}
            retry_at = time.monotonic()
            self._failed_chats = {
                normalize_chat_name(name): ChatRecoveryState(
                    name=name, next_retry=retry_at, reason="微信主窗口重连后等待重建")
                for name in desired
            }
            # A missing/deleted chat is handled by its own retry state and must not
            # keep the main-window reconnect loop tearing down healthy listeners.
            return True
        except Exception:
            logger.exception("微信重连失败")
            return False


global_processor = None


def set_global_processor(processor):
    global global_processor
    global_processor = processor


def create_message_processor(**kwargs):
    from wx_Processer import MessageProcessor
    return MessageProcessor(**kwargs)


def message_callback(chat_name, message_data):
    if not global_processor:
        logger.error("消息处理器未初始化")
        return
    result = global_processor.enqueue_message(chat_name, message_data)
    if not result.get("success"):
        logger.error("微信消息转发失败 chat=%s error=%s", chat_name, result.get("error"))


if __name__ == "__main__":
    listener = WeChatListener(target_chats=WX_TARGET_CHATS, callback=message_callback)
    listener.start_listening()
