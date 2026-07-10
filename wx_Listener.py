"""Single-threaded wxauto/UIA listener and command executor."""

import logging
import queue
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
    def __init__(self, command_id, retry_safe):
        super().__init__(f"UI 命令超时 id={command_id} retry_safe={retry_safe}")
        self.command_id = command_id
        self.retry_safe = retry_safe


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
            retry_safe = command.cancel_if_pending()
            if not retry_safe:
                # Execution already started. Its outcome must be observed before the
                # caller may release media or decide whether a retry is safe.
                return command.future.result()
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
        self.chat_types = {normalize_chat_name(item["name"]): item.get("type") for item in self.target_specs
                           if item.get("type") in {"private", "group"}}
        self._failed_chats = {}
        self._last_retry_time = 0
        self._chatwnd_cache = {}
        self._command_active = False
        self._command_started = None
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
        time.sleep(1)
        names = [item["name"] for item in self.target_specs]
        if not names and WX_LISTEN_ALL_IF_EMPTY:
            names = [name for name in self.wx.GetSessionList(reset=True)
                     if name not in WX_EXCLUDED_CHATS]
        for name in names:
            self._add_listen_chat(name)
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
            self._drain_commands(limit=20)
            now = time.monotonic()
            if now - last_alive_check >= 5:
                last_alive_check = now
                if not self._is_wechat_alive():
                    alive_check_failures += 1
                    logger.warning("微信窗口探测失败 count=%d/3", alive_check_failures)
                    if alive_check_failures >= 3 and lost_since is None:
                        lost_since = now
                        logger.error("连续探测不到微信窗口，进入恢复模式")
                    if lost_since is not None and now - lost_since > 600:
                        raise RuntimeError("微信窗口丢失超过 600 秒")
                    time.sleep(0.2)
                    continue
                alive_check_failures = 0
                if lost_since is not None:
                    if now - lost_since > 600:
                        raise RuntimeError("微信窗口丢失超过 600 秒")
                    if now < next_reconnect:
                        continue
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
                        reconnect_attempts += 1
                        delay = min(30, 2 ** min(reconnect_attempts, 5))
                        next_reconnect = now + delay
                        logger.warning("微信窗口重连失败，将在 %d 秒后重试 attempt=%d",
                                       delay, reconnect_attempts)
                        continue
            self._check_new_messages()
            self._retry_failed_chats()
            if wait_for_command:
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

    def _drain_commands(self, limit):
        self._assert_ui_thread()
        for _ in range(limit):
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
            self._chatwnd_cache.pop(normalize_chat_name(receiver), None)
            raise

    def _ensure_chatwnd(self, receiver):
        key = normalize_chat_name(receiver)
        chat = self._chatwnd_cache.get(key)
        if chat is not None:
            import win32gui

            hwnd = getattr(chat, "HWND", None)
            if not hwnd:
                hwnd = win32gui.FindWindow("ChatWnd", chat.uia_name)
                chat.HWND = hwnd
            if hwnd and win32gui.IsWindow(hwnd):
                return chat
            self._chatwnd_cache.pop(key, None)

        from wxauto.elements import ChatWnd
        from wxauto import uiautomation as uia

        windows = [window for window in uia.GetRootControl().GetChildren()
                   if getattr(window, "ClassName", "") == "ChatWnd"
                   and normalize_chat_name(getattr(window, "Name", "")) == key]
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
                windows = [window for window in uia.GetRootControl().GetChildren()
                           if getattr(window, "ClassName", "") == "ChatWnd"
                           and normalize_chat_name(getattr(window, "Name", "")) == key]
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
        return chat

    def _add_listen_chat(self, name, max_retries=3):
        self._assert_ui_thread()
        key = normalize_chat_name(name)
        for attempt in range(1, max_retries + 1):
            try:
                self._touch_heartbeat()
                self._clear_search_box()
                self.wx.AddListenChat(name, savepic=IMAGE_AUTO_DOWNLOAD,
                                      savefile=False, savevoice=False)
                chat = self.wx.listen.get(key)
                if chat is None or not chat.UiaAPI.Exists(maxSearchSeconds=2):
                    raise RuntimeError("AddListenChat 未创建有效窗口")
                if not chat_names_equal(getattr(chat, "who", None), name):
                    raise RuntimeError(
                        f"监听窗口标题与目标不匹配 raw={name!r} normalized={key!r}")
                detected = self.chat_types.get(key) or self._detect_chat_type(chat)
                if detected not in {"private", "group"}:
                    raise RuntimeError("聊天类型不确定；请显式配置 {name,type}，拒绝监听")
                self.chat_types[key] = detected
                self.listen_chats[key] = name
                self._failed_chats.pop(key, None)
                logger.info("监听已建立 chat=%s normalized=%r type=%s", name, key, detected)
                return True
            except Exception as exc:
                logger.warning("添加监听失败 chat=%s attempt=%d/%d: %s",
                               name, attempt, max_retries, exc)
                self._remove_listen(name)
                if attempt < max_retries:
                    time.sleep(attempt)
        self._failed_chats[key] = name
        return False

    def _detect_chat_type(self, chat):
        """Probe group-only controls while the independent window is active."""
        try:
            chat._show()
            for label in ("聊天信息", "群成员", "查看更多群成员"):
                if chat.UiaAPI.ButtonControl(Name=label).Exists(maxSearchSeconds=0.2):
                    return "group"
            # Group windows normally expose the member panel/button without a stable label.
            if chat.UiaAPI.PaneControl(ClassName="ChatContactMenu").Exists(maxSearchSeconds=0.2):
                return "group"
        except Exception as exc:
            logger.debug("聊天类型探测失败 chat=%s: %s", chat.who, exc)
        logger.warning("无法可靠探测聊天类型 chat=%s；不会默认按私聊处理", chat.who)
        return None

    def _check_new_messages(self):
        try:
            all_messages = self.wx.GetListenMessage() or {}
            for chat, messages in all_messages.items():
                key = normalize_chat_name(chat.who)
                configured_name = self.listen_chats.get(key)
                if configured_name is None:
                    logger.warning("忽略未注册监听消息 raw=%r normalized=%r", chat.who, key)
                    continue
                for message in messages or []:
                    self._process_message(configured_name, message)
            self._msg_fail_count = 0
        except Exception:
            self._msg_fail_count = getattr(self, "_msg_fail_count", 0) + 1
            logger.exception("检查微信消息失败 count=%d", self._msg_fail_count)
            if self._msg_fail_count >= 5:
                failed = dict(self.listen_chats)
                self._cleanup_listeners()
                self._failed_chats.update(failed)
                self._msg_fail_count = 0

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
        if not self._failed_chats or now - self._last_retry_time < 30:
            return
        self._last_retry_time = now
        for name in list(self._failed_chats.values()):
            self._add_listen_chat(name, max_retries=1)

    def _clear_search_box(self):
        try:
            self.wx._show()
            self.wx.UiaAPI.SendKeys("{Esc}", waitTime=0.1)
            self.wx.UiaAPI.SendKeys("{Ctrl}f", waitTime=0.2)
            self.wx.B_Search.SendKeys("{Ctrl}a", waitTime=0.1)
            self.wx.B_Search.SendKeys("{Delete}", waitTime=0.1)
            self.wx.UiaAPI.SendKeys("{Esc}", waitTime=0.1)
        except Exception:
            logger.debug("清理微信搜索框失败", exc_info=True)

    @staticmethod
    def _is_wechat_alive():
        try:
            from wxauto.utils import FindWindow
            return bool(FindWindow(classname="WeChatMainWndForPC"))
        except Exception:
            return False

    def _remove_listen(self, name):
        key = normalize_chat_name(name)
        chat = self.wx.listen.get(key)
        if chat:
            try:
                chat.Close()
            except Exception:
                logger.debug("关闭旧监听窗口失败 chat=%s", name, exc_info=True)
        try:
            self.wx.RemoveListenChat(name)
        except Exception:
            logger.warning("移除监听窗口失败 chat=%s；本地状态仍将清理", name,
                           exc_info=True)
        finally:
            self.listen_chats.pop(key, None)
            self._chatwnd_cache.pop(key, None)

    def _cleanup_listeners(self):
        for name in list(getattr(self.wx, "listen", {})):
            self._remove_listen(name)
        try:
            self.wx.listen.clear()
        except Exception:
            logger.warning("清空 wxauto 监听状态失败", exc_info=True)
        self.listen_chats.clear()
        self._chatwnd_cache.clear()

    def _cleanup_wechat(self):
        if getattr(self, "wx", None):
            self._cleanup_listeners()

    def _reconnect_wechat(self):
        try:
            desired = [item["name"] for item in self.target_specs]
            if not desired and WX_LISTEN_ALL_IF_EMPTY:
                desired = list(self.listen_chats.values())
            self._cleanup_wechat()
            self.wx = self._wechat_class()
            if not desired and not WX_LISTEN_ALL_IF_EMPTY:
                return True
            return all(self._add_listen_chat(name) for name in desired)
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
