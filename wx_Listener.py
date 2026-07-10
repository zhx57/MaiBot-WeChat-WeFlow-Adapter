"""Single-threaded wxauto/UIA listener and command executor."""

import logging
import queue
import re
import threading
import time
from concurrent.futures import Future
from datetime import datetime

from config import (
    IMAGE_AUTO_DOWNLOAD,
    UI_QUEUE_SIZE,
    WX_EXCLUDED_CHATS,
    WX_LISTEN_ALL_IF_EMPTY,
    WX_TARGET_CHATS,
)

logger = logging.getLogger(__name__)


class UICommandQueue:
    def __init__(self, maxsize=UI_QUEUE_SIZE):
        self._queue = queue.Queue(maxsize=maxsize)

    def submit(self, action, *args, timeout=15):
        future = Future()
        try:
            self._queue.put((action, args, future), timeout=2)
        except queue.Full as exc:
            raise RuntimeError("UI 命令队列已满") from exc
        return future.result(timeout=timeout)

    def get_nowait(self):
        return self._queue.get_nowait()

    def task_done(self):
        self._queue.task_done()


class WeChatListener:
    """Owns every wxauto object and uses it only from its creating thread."""

    def __init__(self, target_chats=None, callback=None, command_queue=None, stop_event=None):
        from wxauto import WeChat

        self._owner_thread = threading.get_ident()
        self._wechat_class = WeChat
        self.wx = WeChat()
        self.target_specs = self._normalize_targets(target_chats)
        self.callback = callback
        self.commands = command_queue or UICommandQueue()
        self.stop_event = stop_event
        self.listen_chats = {}
        self.chat_types = {item["name"]: item.get("type") for item in self.target_specs
                           if item.get("type") in {"private", "group"}}
        self._failed_chats = set()
        self._last_retry_time = 0
        self._chatwnd_cache = {}
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
            if name and name not in seen:
                seen.add(name)
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
        while self.running and not (self.stop_event and self.stop_event.is_set()):
            self._drain_commands(limit=20)
            now = time.monotonic()
            if now - last_alive_check >= 5:
                last_alive_check = now
                if not self._is_wechat_alive():
                    lost_since = lost_since or now
                    if now - lost_since > 600:
                        raise RuntimeError("微信窗口丢失超过 600 秒")
                    time.sleep(0.2)
                    continue
                if lost_since is not None:
                    if not self._reconnect_wechat():
                        raise RuntimeError("微信窗口重连失败")
                    lost_since = None
            self._check_new_messages()
            self._retry_failed_chats()
            time.sleep(0.2)

    def stop_listening(self):
        self.running = False

    def close(self):
        self._assert_ui_thread()
        self.running = False
        self._cleanup_wechat()

    def _drain_commands(self, limit):
        self._assert_ui_thread()
        for _ in range(limit):
            try:
                action, args, future = self.commands.get_nowait()
            except queue.Empty:
                return
            try:
                if action == "send":
                    future.set_result(self._send(*args))
                elif action == "stop":
                    self.running = False
                    future.set_result(True)
                else:
                    raise ValueError(f"未知 UI 命令: {action}")
            except BaseException as exc:
                future.set_exception(exc)
            finally:
                self.commands.task_done()

    def _send(self, receiver, kind, data, _caller_timeout=None):
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
            self._chatwnd_cache.pop(receiver, None)
            raise

    def _ensure_chatwnd(self, receiver):
        from wxauto.elements import ChatWnd
        from wxauto.utils import FindWindow

        escaped = f"^{re.escape(receiver)}$"
        if not FindWindow(name=receiver, classname="ChatWnd"):
            selected = self.wx.ChatWith(receiver)
            if selected is False or selected != receiver:
                raise RuntimeError(f"ChatWith 未精确打开目标: {receiver!r}, result={selected!r}")
            matches = [item for item in self.wx.SessionBox.ListControl().GetChildren()
                       if getattr(item, "Name", None) == receiver]
            if len(matches) > 1:
                raise RuntimeError(f"存在 {len(matches)} 个同名会话，拒绝自动选择: {receiver!r}")
            control = self.wx.SessionBox.ListItemControl(RegexName=escaped)
            if not control.Exists(maxSearchSeconds=2):
                raise RuntimeError(f"会话列表中不存在精确目标: {receiver!r}")
            control.DoubleClick(simulateMove=False)
            window = self.wx.UiaAPI.WindowControl(searchDepth=1, ClassName="ChatWnd", Name=receiver)
            if not window.Exists(maxSearchSeconds=5):
                raise RuntimeError(f"独立聊天窗口未出现: {receiver!r}")
        chat = self._chatwnd_cache.get(receiver)
        if chat is None:
            chat = object.__new__(ChatWnd)
            chat.who = receiver
            chat.language = self.wx.language
            chat.usedmsgid = []
            from wxauto import uiautomation as uia
            chat.UiaAPI = uia.WindowControl(searchDepth=1, ClassName="ChatWnd", Name=receiver)
            chat.editbox = chat.UiaAPI.EditControl()
            chat.C_MsgList = chat.UiaAPI.ListControl()
            chat.savepic = False
            self._chatwnd_cache[receiver] = chat
        return chat

    def _add_listen_chat(self, name, max_retries=3):
        self._assert_ui_thread()
        for attempt in range(1, max_retries + 1):
            try:
                self._clear_search_box()
                self.wx.AddListenChat(name, savepic=IMAGE_AUTO_DOWNLOAD,
                                      savefile=False, savevoice=False)
                chat = self.wx.listen.get(name)
                if chat is None or not chat.UiaAPI.Exists(maxSearchSeconds=2):
                    raise RuntimeError("AddListenChat 未创建有效窗口")
                if getattr(chat, "who", None) != name:
                    raise RuntimeError("监听窗口标题与目标不匹配")
                self.chat_types[name] = self.chat_types.get(name) or self._detect_chat_type(chat)
                self.listen_chats[name] = True
                self._failed_chats.discard(name)
                logger.info("监听已建立 chat=%s type=%s", name, self.chat_types[name])
                return True
            except Exception as exc:
                logger.warning("添加监听失败 chat=%s attempt=%d/%d: %s",
                               name, attempt, max_retries, exc)
                self._remove_listen(name)
                if attempt < max_retries:
                    time.sleep(attempt)
        self._failed_chats.add(name)
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
        # The group-only controls were probed successfully and none exists.
        return "private"

    def _check_new_messages(self):
        try:
            all_messages = self.wx.GetListenMessage() or {}
            for chat, messages in all_messages.items():
                for message in messages or []:
                    self._process_message(chat.who, message)
            self._msg_fail_count = 0
        except Exception:
            self._msg_fail_count = getattr(self, "_msg_fail_count", 0) + 1
            logger.exception("检查微信消息失败 count=%d", self._msg_fail_count)
            if self._msg_fail_count >= 5:
                names = list(self.listen_chats)
                self._cleanup_listeners()
                self._failed_chats.update(names)
                self._msg_fail_count = 0

    def _process_message(self, chat_name, message):
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
        data = {"chat": chat_name, "chat_type": self.chat_types.get(chat_name),
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
        for name in list(self._failed_chats):
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
        chat = self.wx.listen.get(name)
        if chat:
            try:
                chat.Close()
            except Exception:
                logger.debug("关闭旧监听窗口失败 chat=%s", name, exc_info=True)
        self.wx.RemoveListenChat(name)
        self.listen_chats.pop(name, None)
        self._chatwnd_cache.pop(name, None)

    def _cleanup_listeners(self):
        for name in list(self.wx.listen):
            self._remove_listen(name)
        self.wx.listen.clear()
        self.listen_chats.clear()
        self._chatwnd_cache.clear()

    def _cleanup_wechat(self):
        if getattr(self, "wx", None):
            self._cleanup_listeners()

    def _reconnect_wechat(self):
        try:
            desired = [item["name"] for item in self.target_specs]
            if not desired and WX_LISTEN_ALL_IF_EMPTY:
                desired = list(self.listen_chats)
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
    result = global_processor.process_message(chat_name, message_data)
    if not result.get("success"):
        logger.error("微信消息转发失败 chat=%s error=%s", chat_name, result.get("error"))


if __name__ == "__main__":
    listener = WeChatListener(target_chats=WX_TARGET_CHATS, callback=message_callback)
    listener.start_listening()
