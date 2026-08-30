"""基于微信 4.0 主窗口和独立聊天窗口的 UIAutomation 适配层。

控件定位以微信 4.0 的 ``mmui`` 层级为准：会话操作限定在
``ChatMasterView``，聊天操作限定在 ``ChatMessagePage/XSplitterView``。
wx4py 的通用选择器只作为旧版本兼容回退。
"""

import contextlib
import logging
import math
import ntpath
import os
import queue
import re
import shutil
import tempfile
import threading
import time
import uuid
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import dataclass, field
from typing import Optional

from chat_name_utils import chat_names_equal, normalize_chat_name
from config import (
    IMAGE_AUTO_DOWNLOAD,
    IMAGE_SAVE_DIR,
    IMAGE_SAVE_TIMEOUT_SECONDS,
    MAX_MEDIA_BYTES,
    UI_QUEUE_SIZE,
    UI_WORKER_IDLE_TIMEOUT_SECONDS,
    WEMAI_AUTO_DISMISS_UPDATE,
    WEMAI_BOT_NICKNAME,
    WEMAI_SEARCH_TIMEOUT,
    WEMAI_SUBWINDOW_TIMEOUT,
    WEMAI_SUBWINDOW_TITLE_REFRESHES,
    WEMAI_TAIL_SIZE,
    WEMAI_TICK,
    WEMAI_UI_LOOKUP_TIMEOUT,
    WEMAI_UI_POLL_INTERVAL,
    WEMAI_UPDATE_DIALOG_SCAN_INTERVAL,
    WEMAI_UPDATE_DISMISS_DELAY,
    WEMAI_WINDOW_CHECK_INTERVAL,
    WX_OPEN_WINDOWS_ON_DEMAND,
    WX_TARGET_CHATS,
)
from wx_exceptions import ChatNotFoundError

logger = logging.getLogger(__name__)

# region ==== 模块级常量 ================================================
# 下方集中保存微信 4.x UIA 控件类名、按钮文本和超时参数。微信升级改变布局时，
# 应优先在此处及相邻选择器中适配，避免同一控件规则散落在多段发送逻辑里。
# Windows 剪贴板属于整个桌面而非某个聊天窗口；锁只覆盖“写剪贴板+粘贴”，
# 让其他独立窗口仍可并行完成发送确认。
_CLIPBOARD_LOCK = threading.RLock()

# 最近入站窗口用于独立窗口打开时的软失败判断：刚收到该会话消息、但会话列表尚未
# 出现目标时，视为微信 UI 刷新延迟并继续使用搜索框，而不是立即抛出永久失败。
# 新群聊通常需要 1~2 秒才进入会话列表，这个窗口可避免对刚触发回复的会话误判。
_RECENT_INBOUND_TTL_SECONDS = 30.0
_RECENT_INBOUND_CACHE_MAX_SIZE = 100

_DYNAMIC_TITLE_SUFFIX = re.compile(
    r"\s*(?:\(\d+\)|（\d+）|\[\d+\])\s*$"
)
_SEARCH_GROUP_CONTACTS = "联系人"
_SEARCH_GROUP_CHATS = "群聊"
_SEARCH_GROUP_FUNCTIONS = "功能"
_SEARCH_GROUP_FREQUENT = "最常使用"
_SEARCH_GROUP_HISTORY = "聊天记录"
# 微信 4.x 的 Ctrl+F 结果按分类分区。“搜索网络结果/网页/搜一搜”中的条目外观
# 很像聊天名称，回车却会打开网页搜索。优先按分区标题排除这些结果；若未来版本
# 不再提供分区标题，再使用行名称前缀兜底，防止误开后进入重复搜索循环。
_WEB_SEARCH_GROUPS = frozenset({
    "搜索网络结果",
    "网页",
    "搜一搜",
    "Web Search",
    "Internet Search",
    "search_web",
})
_NON_CHAT_SEARCH_GROUPS = _WEB_SEARCH_GROUPS | {_SEARCH_GROUP_HISTORY}
_WEB_SEARCH_ROW_PREFIXES = (
    "搜一搜",
    "搜索 ",
    "搜索:",
    "搜索:",
    "网页",
    "Search ",
    "Search:",
    "search ",
    "search:",
)
# How many Enter attempts to allow when the verify-and-skip loop has
# to recover from a misrouted Enter. Three is enough to clear
# "搜一搜" + "搜索:<query> 朋友圈" + the auto-selected chat row that
# followed them in observed WeChat 4.0.5+ builds; larger numbers
# burn real time on a chat that is genuinely not in the list.
_SEARCH_VERIFY_MAX_SKIPS = 3
_MESSAGE_CLASSES = {
    "mmui::ChatTextItemView",
    "mmui::ChatBubbleItemView",
    "mmui::ChatVoiceItemView",
    "mmui::ChatPersonalCardItemView",
}
_IMAGE_MESSAGE_CLASS = "mmui::ChatBubbleItemView"
_IMAGE_MESSAGE_NAMES = {"图片", "圖片", "photo", "image"}
_TIME_MESSAGE_CLASS = "mmui::ChatItemView"
_CHAT_INPUT_AUTOMATION_IDS = (
    "chat_input_field",
    "input_field",
    "msg_input",
    "edit_input",
)
_CHAT_INPUT_CLASS_NAMES = (
    "mmui::ChatInputField",
    "mmui::XTextEdit",
    "mmui::XValidatorTextEdit",
    "mmui::XEditEx",
    "mmui::XRichEdit",
)
_WECHAT_EXE_NAMES = {"wechat.exe", "weixin.exe"}
_WECHAT_NATIVE_WINDOW_CLASS = "Qt51514QWindowIcon"
_MAIN_WINDOW_CLASS = "mmui::MainWindow"
_SUB_WINDOW_CLASS = "mmui::FramelessMainWindow"
_MAIN_TAB_BAR_CLASS = "mmui::MainTabBar"
_CHAT_MASTER_VIEW_CLASS = "mmui::ChatMasterView"
_SEARCH_FIELD_CLASS = "mmui::XSearchField"
_SEARCH_POPOVER_CLASS = "mmui::SearchContentPopover"
_SESSION_LIST_CONTAINER_CLASS = "mmui::ChatSessionList"
_TABLE_VIEW_CLASS = "mmui::XTableView"
_CHAT_PAGE_CLASS = "mmui::ChatMessagePage"
_CHAT_SPLITTER_CLASS = "mmui::XSplitterView"
_MESSAGE_VIEW_CLASS = "mmui::MessageView"
_CHAT_INPUT_CLASS = "mmui::ChatInputField"
_SEND_BUTTON_NAMES = ("发送(S)", "发送")
_PREVIEW_WINDOW_NAMES = ("预览", "預覽", "Preview")
_PREVIEW_TOOLBAR_CLASS = "mmui::PreviewToolbarView"
_PREVIEW_MENU_CLASS = "mmui::XMenu"
_PREVIEW_MORE_BUTTON_NAMES = ("更多", "更多选项", "More")
_PREVIEW_COPY_MENU_NAMES = ("复制", "複製", "Copy")
_PREVIEW_SAVE_AS_NAMES = (
    "保存",
    "儲存",
    "储存",
    "Save",
    "Save image",
    "Save Image",
    "另存为...",
    "另存为…",
    "另存为",
    "另存為...",
    "另存為…",
    "另存為",
    "另存爲...",
    "另存爲…",
    "另存爲",
    "Save as...",
    "Save as…",
    "Save As",
)
_PREVIEW_EXPIRED_NAMES = (
    "图片过期或已被清理",
    "圖片過期或已被清理",
    "Image expired or was deleted",
)
_UPDATE_DIALOG_TITLES = frozenset({
    "新版本",
    "发现新版本",
    "版本更新",
    "new version",
    "update available",
})
_UPDATE_DIALOG_PRODUCT_NAMES = frozenset({"微信", "wechat", "weixin"})
_UPDATE_DIALOG_UPDATE_BUTTON_NAMES = frozenset({"更新", "update"})
_UPDATE_DIALOG_DISMISS_BUTTON_NAMES = frozenset({
    "忽略本次更新",
    "忽略此次更新",
    "忽略這次更新",
    "skip this update",
    "skip this version",
})
_UPDATE_DIALOG_DISCOVERY_SCAN_SECONDS = 5.0
_UPDATE_DIALOG_CLICK_RETRY_SECONDS = 5.0
_SAVE_DIALOG_BUTTON_NAMES = (
    "保存(&S)",
    "保存",
    "儲存(&S)",
    "儲存",
    "&Save",
    "Save",
)
_IMAGE_MAGIC = {
    b"\x89PNG\r\n\x1a\n": ".png",
    b"\xff\xd8\xff": ".jpg",
    b"GIF87a": ".gif",
    b"GIF89a": ".gif",
    b"RIFF": ".webp",
    b"BM": ".bmp",
}
_CURRENT_CHAT_NAME_AUTOMATION_ID = (
    "top_content_h_view.top_spacing_v_view.top_left_info_v_view."
    "big_title_line_h_view.current_chat_name_label"
)
_CURRENT_CHAT_COUNT_AUTOMATION_IDS = (
    "top_content_h_view.top_spacing_v_view.top_left_info_v_view."
    "big_title_line_h_view.current_chat_count_label",
    "current_chat_count_label",
)
_GROUP_HEADER_METADATA_TOKENS = (
    "chat_count", "member_count", "members_count", "group_count",
    "群成员", "群聊人数", "成员数",
)
_GROUP_COUNT_PATTERN = re.compile(r"^[\(（\[]\s*\d+\s*[\)）\]]$")
_SENDER_METADATA_TOKENS = ("sender", "nickname", "user_name", "username")
_QML_SENDER_PREFIX_PATTERN = re.compile(
    r"^\s*([^\r\n:：]{1,80}?)\s*[:：]\s*(\S[\s\S]*?)\s*$"
)
_NON_SENDER_NAMES = {
    "复制", "转发", "引用", "撤回", "删除", "更多", "多选", "翻译",
    "copy", "forward", "quote", "recall", "delete", "more", "translate",
}
_UI_LOOKUP_TIMEOUT = WEMAI_UI_LOOKUP_TIMEOUT
_UI_POLL_INTERVAL = WEMAI_UI_POLL_INTERVAL
_SEARCH_TIMEOUT = WEMAI_SEARCH_TIMEOUT
_SEARCH_INPUT_STEP_DELAY = 0.1
_SEARCH_HOTKEY_ACTIVATION_DELAY = 0.3
_SEARCH_HOTKEY_OPEN_DELAY = 0.5
# 微信搜索弹层会分批渲染。粘贴后完整等待 3 秒，再读取本地联系人/群聊
# 结果，避免过早点击尚未稳定的网络搜索行或错误候选。
_SEARCH_HOTKEY_RESULT_DELAY = 3.0
_SEARCH_HOTKEY_SUBMIT_DELAY = 1.0
# After N consecutive ``ChatNotFoundError`` raises for the same chat,
# the outbound send path mutes that chat for
# ``_SEARCH_FAIL_COOLDOWN_SECONDS`` seconds. Without this, a chat that
# was renamed in WeChat (so the bot's lookup no longer matches) would
# burn three full send cycles (~17 s each) on every incoming reply
# from MaiBot, starving the rest of the queue. The cooldown is
# per-target and expires automatically; other chats are unaffected.
#
# Round 20: tuned for the user's "重试1次后放弃 + 20秒冷却"
# spec — a single failed open (each open does
# ``_SEARCH_OPEN_ATTEMPTS=2`` internal search tries) arms the
# cooldown. The bot still attempts 2 internal searches per
# message (per the spec) but the NEXT message to the same
# broken target short-circuits with
# ``ChatNotFoundError("搜索冷却中")`` so the queue can drain to
# other chats within the 20-second window.
_SEARCH_FAIL_COOLDOWN_SECONDS = 20.0
_SEARCH_FAIL_COOLDOWN_THRESHOLD = 1
# 每次打开最多搜索两次。每次粘贴后等待 3 秒再读取本地结果；
# 两次都找不到时进入该目标的冷却，不影响其他会话。
_SEARCH_OPEN_ATTEMPTS = 2
# 两次搜索之间不增加固定暂停。
_SEARCH_INTER_ATTEMPT_PAUSE_SECONDS = 0.0
# 独立窗口在这台机器上通常会在 0.5-1.3 秒内发布 HWND 和 UIA 树。
# 旧逻辑第一轮固定等 3 秒、第二轮最长等 8 秒，双击没有生效时会独占
# 微信焦点十余秒。第一轮快速判定，第二轮给足 3 秒兜底；真正出现了
# 新 HWND 但结构尚未就绪时，_wait_for_subwindow 会保留窗口且停止重击。
_SUBWINDOW_FIRST_WAIT_SECONDS = 1.5
_SUBWINDOW_RETRY_WAIT_SECONDS = 3.0
# 首屏搜索结果没有精确目标时，在结果列表内持续向下滚动一小段时间。
# 微信 4.x 会虚拟化弹层列表，联系人/群聊分组有时位于首屏下方；直接读取
# GetChildren 只能看到当前可见行，因此需要用真实 WheelDown 逐页触发渲染。
_SEARCH_RESULT_SCROLL_SECONDS = 2.0
_SEARCH_RESULT_SCROLL_INTERVAL = 0.2
# Cap on the "rename suffix" accepted by ``_is_rename_pattern`` /
# ``_find_renamed_open_window``. The user renames "测试群" to
# "测试群v2" / "测试群(2)" / "测试群改名" — these suffixes are 1-4
# characters. We cap at 20 to leave headroom for longer remark
# names ("测试群项目的所有讨论") while still rejecting obviously
# unrelated window titles that happen to embed the target word as a
# tiny substring of a much longer phrase.
_RENAME_SUFFIX_LIMIT = 20
# 保留旧版兼容常量；当前搜索的完整稳定等待由上方 3 秒常量统一控制。
_SEARCH_INITIAL_SETTLE_DELAY = 0.3
_SEARCH_RESULT_STABLE_TIME = 0.0
_TEXT_PREPARE_TIMEOUT = 0.5
_TEXT_SEND_CONFIRM_TIMEOUT = 0.75
_FILE_PREPARE_TIMEOUT = 1.5
_FILE_SEND_CONFIRM_TIMEOUT = 1.0
_SEND_POLL_INTERVAL = min(_UI_POLL_INTERVAL, 0.01)
_WINDOW_ACTIVATION_TIMEOUT = 0.3
# Throttle window-title identity checks per session. Each check is a
# UIA tree walk (~50-100 ms); within this TTL we trust the cached
# result. The next paste / send will fail fast if the title has
# actually drifted, and ``_recover_session`` re-validates.
_IDENTITY_CHECK_TTL_SECONDS = 5.0
# Round 10: throttle for the global rename auto-rebind pass in
# ``_rebind_renamed_cached_sessions``. The pass reads the native
# title of every cached session's hwnd, so it must not run on
# every send. 1 second is a tight cadence: long enough to avoid
# reading the title on every send (a busy send worker can fire
# hundreds of ``_get_or_open_session`` calls per second), short
# enough that a rename that happened 1s ago is reflected in the
# next send attempt. The title read is a single Win32 call per
# cached session (microseconds), so the per-tick cost is
# negligible even with 10+ cached sessions.
_GLOBAL_RENAME_REBIND_INTERVAL_SECONDS = 1.0
_WINDOW_ACTIVATION_POLL_INTERVAL = min(_UI_POLL_INTERVAL, 0.03)
_ACTIVATION_STRATEGY_LOCK = threading.RLock()
_ACTIVATION_STRATEGY_BY_HWND = {}
_IMAGE_SAVE_MAX_ATTEMPTS = 3
_CHAT_TYPE_SOURCE_PRIORITY = {
    "fallback": 0,
    "message": 1,
    "header": 2,
    "window": 3,
    "search": 4,
    "config": 5,
}

# endregion ==============================================================


# region ==== Exception types ============================================
# Internal exceptions raised by UI helpers.  Kept private; main.py never
# imports these.  ``_UIOperationCancelled`` and ``_SendOutcomeUnknown``
# carry retry-safety hints that the send worker relies on.
class _UIOperationCancelled(RuntimeError):
    """The process is stopping while a bounded UI wait is in progress."""


class _SendOutcomeUnknown(RuntimeError):
    """Enter was attempted, so retrying could duplicate a delivered message."""

    retry_safe = False
    cleanup_delay = 10.0

# endregion ==============================================================


# region ==== UI helpers: control lookup, window, image preview ==========
# Pure (mostly) helper functions used by WeChatListener.  Order roughly
# mirrors the call graph from low-level UIA lookups up to image-preview
# dialog handling:
#   1. Control/control-tree helpers      (visible/search items, walks)
#   2. Native window enumeration + focus (win32 / native window hacks)
#   3. Main/sub window identification
#   4. Session list / message list scans
#   5. Image message handling: click → preview window → save-as / copy
def _raise_if_stopped(stop_event):
    if stop_event is not None and stop_event.is_set():
        raise _UIOperationCancelled("微信 UI 操作已因停止请求取消")


@dataclass(frozen=True)
class _VisibleUIItem:
    kind: str
    name: str
    class_name: str
    runtime_id: tuple
    control: object = None
    message_type: str = "text"
    direction: str = ""
    sender: str = ""

    @property
    def key(self):
        return self.runtime_id, self.class_name, self.name


@dataclass(frozen=True)
class _SearchUIItem:
    name: str
    ctrl: object
    group: str = "未知"


# wx4py 的 UIA 模块依赖 pywin32，只能在 Windows 上导入。使用这些薄包装
# 既能直接复用 wx4py 已验证的实现，也让本模块可以在非 Windows CI 中导入。
def _safe_control_text(control, attribute):
    try:
        return str(getattr(control, attribute, "") or "")
    except Exception:
        return ""


def _walk_controls(root, max_depth):
    from wx4py.core import uiautomation as uia

    return uia.WalkControl(root, includeTop=False, maxDepth=max_depth)


def _safe_control_children(control):
    try:
        return list(control.GetChildren())
    except Exception:
        return []


def _call_uia_without_wait(method, *args, **kwargs):
    """Invoke a UIA action without its library-default 0.5 second tail wait."""
    try:
        return method(*args, waitTime=0, **kwargs)
    except TypeError:
        return method(*args, **kwargs)


def _send_global_uia_keys(keys):
    """Send keys to the foreground window without requiring a child control."""
    from wx4py.core import uiautomation as uia

    return _call_uia_without_wait(uia.SendKeys, keys)


def _safe_runtime_id(control):
    try:
        runtime_id = control.GetRuntimeId()
    except Exception:
        runtime_id = None
    if not runtime_id:
        try:
            runtime_id = getattr(control, "runtimeid", None)
        except Exception:
            runtime_id = None
    try:
        return tuple(runtime_id or ())
    except (TypeError, ValueError):
        return ()


def _normalized_media_label(value):
    label = normalize_chat_name(value).strip("[]【】").casefold()
    return label


def _has_image_metadata(*values):
    metadata = " ".join(str(value or "") for value in values).casefold()
    compact_metadata = re.sub(r"[^a-z0-9图片圖片]+", "", metadata)
    return any(
        token in compact_metadata
        for token in (
            "imagemessage",
            "imagebubble",
            "chatimage",
            "photomessage",
            "photobubble",
            "图片消息",
            "圖片消息",
        )
    )


def _classify_message_type(class_name, name, control=None):
    control_type = _safe_control_text(control, "ControlTypeName")
    automation_id = _safe_control_text(control, "AutomationId")
    known_non_image_class = class_name in {
        "mmui::ChatTextItemView",
        "mmui::ChatVoiceItemView",
        "mmui::ChatPersonalCardItemView",
        _TIME_MESSAGE_CLASS,
    }
    row_has_image_metadata = _has_image_metadata(
        class_name,
        automation_id,
    )
    possible_image_row = (
        class_name == _IMAGE_MESSAGE_CLASS
        or row_has_image_metadata
        or (
            control_type == "ListItemControl"
            and not known_non_image_class
        )
    )

    if (
        possible_image_row
        and _normalized_media_label(name) in _IMAGE_MESSAGE_NAMES
    ):
        return "image"
    if row_has_image_metadata and not known_non_image_class:
        return "image"
    if not possible_image_row:
        return "text"
    if normalize_chat_name(name):
        return "text"

    # Some WeChat 4.x builds leave the row Name empty while publishing the
    # media label on a nested bubble control.  Prefer an explicit image token;
    # a generic edge ImageControl is also used for avatars and is insufficient.
    if control is not None:
        unnamed_visuals = []
        for child, _depth in _walk_control_children(control, max_depth=5):
            child_name = _safe_control_text(child, "Name")
            if _normalized_media_label(child_name) in _IMAGE_MESSAGE_NAMES:
                return "image"
            if (
                not child_name.strip()
                and _safe_control_text(child, "ControlTypeName")
                in {"ButtonControl", "ImageControl"}
            ):
                unnamed_visuals.append(child)
            if _has_image_metadata(
                _safe_control_text(child, "AutomationId"),
                _safe_control_text(child, "ClassName"),
            ):
                return "image"

        # Last-resort structural signature: an unnamed, sizeable visual well
        # inside the row is the image bubble used by the original WeMai.  The
        # edge avatar is intentionally excluded by the inset requirement.
        row_rect = _control_rectangle(control)
        if row_rect is not None:
            row_width = max(0, row_rect[2] - row_rect[0])
            minimum_inset = max(48, row_width * 0.06)
            for visual in unnamed_visuals:
                rect = _control_rectangle(visual)
                if rect is None:
                    continue
                width = max(0, rect[2] - rect[0])
                height = max(0, rect[3] - rect[1])
                center_x = (rect[0] + rect[2]) / 2
                inset = min(center_x - row_rect[0], row_rect[2] - center_x)
                if width >= 48 and height >= 48 and inset >= minimum_inset:
                    return "image"
    return "text"


def _control_exists(control, timeout):
    exists = getattr(control, "Exists", None)
    if not callable(exists):
        return True
    try:
        return bool(
            exists(
                maxSearchSeconds=timeout,
                searchIntervalSeconds=min(max(timeout / 3, 0), 0.1),
            )
        )
    except TypeError:
        try:
            return bool(exists(maxSearchSeconds=timeout))
        except Exception:
            return False
    except Exception:
        return False


def _control_exists_now(control):
    """Check a cached UIA control without waiting or creating a new search."""
    return _control_exists(control, 0)


def _find_control(parent, getter_name, timeout=_UI_LOOKUP_TIMEOUT, **criteria):
    """Resolve one UIA child without leaking a lazy, non-existing proxy."""
    if parent is None:
        return None
    getter = getattr(parent, getter_name, None)
    if not callable(getter):
        return None
    try:
        control = getter(**criteria)
    except Exception:
        return None
    return control if _control_exists(control, timeout) else None


def _top_level_control(control):
    try:
        return control.GetTopLevelControl()
    except Exception:
        return None


def _find_main_tab_bar(root):
    return _find_control(
        root,
        "ToolBarControl",
        ClassName=_MAIN_TAB_BAR_CLASS,
        AutomationId="main_tabbar",
    )


def _switch_to_chat_page(root):
    tab_bar = _find_main_tab_bar(root)
    chat_button = _find_control(tab_bar, "ButtonControl", Name="微信")
    if chat_button is None:
        return False
    try:
        _call_uia_without_wait(chat_button.Click)
        return True
    except Exception:
        logger.debug("点击微信导航栏失败", exc_info=True)
        return False


def _find_session_panel(root):
    if _safe_control_text(root, "ClassName") == _CHAT_MASTER_VIEW_CLASS:
        return root
    return _find_control(
        root,
        "GroupControl",
        ClassName=_CHAT_MASTER_VIEW_CLASS,
    )


def _find_search_box(root):
    panel = _find_session_panel(root)
    search_field = _find_control(
        panel,
        "GroupControl",
        ClassName=_SEARCH_FIELD_CLASS,
    )
    return _find_control(search_field, "EditControl")


def _find_search_result_list(root):
    popup = _find_control(
        root,
        "WindowControl",
        ClassName=_SEARCH_POPOVER_CLASS,
    )
    return _find_control(popup, "ListControl")


def _find_chat_box(root):
    """Locate the XSplitterView which owns one WeChat 4.0 chat surface."""
    if _safe_control_text(root, "ClassName") == _CHAT_SPLITTER_CLASS:
        return root
    page = _find_control(root, "GroupControl", ClassName=_CHAT_PAGE_CLASS)
    return _find_control(
        page,
        "CustomControl",
        ClassName=_CHAT_SPLITTER_CLASS,
    )


def _find_send_button(root):
    chat_box = _find_chat_box(root)
    for name in _SEND_BUTTON_NAMES:
        button = _find_control(chat_box, "ButtonControl", Name=name)
        if button is not None:
            return button
    return None


def _find_current_chat_name(root):
    chat_box = _find_chat_box(root)
    if chat_box is None:
        return ""
    try:
        chat_page = chat_box.GetParentControl()
    except Exception:
        chat_page = root
    info = _find_control(chat_page, "GroupControl", ClassName="mmui::ChatInfoView")
    if info is None:
        info = _find_control(root, "GroupControl", ClassName="mmui::ChatInfoView")
    label = _find_control(
        info,
        "TextControl",
        AutomationId=_CURRENT_CHAT_NAME_AUTOMATION_ID,
    )
    if label is None:
        label = _find_control(
            info,
            "TextControl",
            AutomationId="current_chat_name_label",
        )
    return _safe_control_text(label, "Name").strip()


def _chat_type_from_header(root):
    """Return group only when the 4.x header contains positive group evidence.

    WeChat builds frequently omit or rename ``current_chat_count_label``.
    Its absence is therefore unknown, not proof that the chat is private.
    """
    chat_box = _find_chat_box(root)
    if chat_box is None:
        return None
    try:
        chat_page = chat_box.GetParentControl()
    except Exception:
        chat_page = root
    info = _find_control(chat_page, "GroupControl", ClassName="mmui::ChatInfoView")
    if info is None:
        info = _find_control(root, "GroupControl", ClassName="mmui::ChatInfoView")
    if info is None:
        return None
    for automation_id in _CURRENT_CHAT_COUNT_AUTOMATION_IDS:
        label = _find_control(info, "TextControl", AutomationId=automation_id)
        if label is not None and _control_exists_now(label):
            return "group"
    for control, _depth in _walk_control_children(info, max_depth=4):
        name = _safe_control_text(control, "Name").strip()
        metadata = " ".join((
            _safe_control_text(control, "AutomationId"),
            _safe_control_text(control, "ClassName"),
        )).casefold()
        if (
            any(token in metadata for token in _GROUP_HEADER_METADATA_TOKENS)
            or _GROUP_COUNT_PATTERN.fullmatch(name)
        ):
            return "group"
    return None


def _walk_control_children(root, max_depth):
    """Walk descendants without creating additional UIA search objects."""
    stack = [(child, 1) for child in reversed(_safe_control_children(root))]
    while stack:
        control, depth = stack.pop()
        yield control, depth
        if depth >= max_depth:
            continue
        children = _safe_control_children(control)
        stack.extend((child, depth + 1) for child in reversed(children))


def _normalized_update_dialog_label(value):
    return re.sub(r"\s+", "", str(value or "")).casefold()


def _native_title_may_be_update_dialog(value):
    label = _normalized_update_dialog_label(value)
    if not label:
        return False
    expected = {
        _normalized_update_dialog_label(name)
        for name in (*_UPDATE_DIALOG_TITLES, *_UPDATE_DIALOG_PRODUCT_NAMES)
    }
    return label in expected or any(item in label for item in expected)


def _find_update_dialog_dismiss_button(root, native_title=""):
    """Return the dismiss button only for a verified WeChat update dialog."""
    if root is None:
        return None

    # Recent WeChat 4.x builds expose the updater as ``UpdateWindow`` but its
    # native/root title is only ``微信``.  On that window wx4py's generic
    # ``WalkControl`` can return an incomplete tree even though direct typed
    # lookups still work.  Use the stable root id plus both action buttons as
    # strong evidence before falling back to the normal tree-based verifier.
    root_automation_id = _safe_control_text(root, "AutomationId").casefold()
    if root_automation_id == "updatewindow":
        direct_dismiss = None
        for name in _UPDATE_DIALOG_DISMISS_BUTTON_NAMES:
            direct_dismiss = _find_control(
                root,
                "ButtonControl",
                timeout=0,
                Name=name,
            )
            if direct_dismiss is not None:
                break
        direct_update = None
        for name in _UPDATE_DIALOG_UPDATE_BUTTON_NAMES:
            direct_update = _find_control(
                root,
                "ButtonControl",
                timeout=0,
                Name=name,
            )
            if direct_update is not None:
                break
        if direct_dismiss is not None and direct_update is not None:
            return direct_dismiss

    controls = [root]
    controls.extend(
        control for control, _depth in _walk_control_children(root, max_depth=8)
    )
    labels = {
        _normalized_update_dialog_label(_safe_control_text(control, "Name"))
        for control in controls
    }
    labels.discard("")
    native_label = _normalized_update_dialog_label(native_title)
    title_labels = {
        _normalized_update_dialog_label(name) for name in _UPDATE_DIALOG_TITLES
    }
    product_labels = {
        _normalized_update_dialog_label(name)
        for name in _UPDATE_DIALOG_PRODUCT_NAMES
    }
    update_labels = {
        _normalized_update_dialog_label(name)
        for name in _UPDATE_DIALOG_UPDATE_BUTTON_NAMES
    }
    dismiss_labels = {
        _normalized_update_dialog_label(name)
        for name in _UPDATE_DIALOG_DISMISS_BUTTON_NAMES
    }

    has_title = native_label in title_labels or bool(labels & title_labels)
    has_product = bool(labels & product_labels)
    has_update_action = bool(labels & update_labels)
    if not has_title or not (has_product or has_update_action):
        return None

    for control in controls:
        label = _normalized_update_dialog_label(
            _safe_control_text(control, "Name")
        )
        if label not in dismiss_labels:
            continue
        control_type = _safe_control_text(control, "ControlTypeName")
        class_name = _safe_control_text(control, "ClassName").casefold()
        click = getattr(control, "Click", None)
        if (
            control_type == "ButtonControl"
            or "button" in class_name
            or callable(click)
        ):
            return control
    return None


def _control_has_exact_chat_name(control, chat_name, max_depth=4):
    if chat_names_equal(_safe_control_text(control, "Name"), chat_name):
        return True
    for child, _depth in _walk_control_children(control, max_depth=max_depth):
        if chat_names_equal(_safe_control_text(child, "Name"), chat_name):
            return True
    return False


def _window_title_match_score(window_title, chat_name):
    actual = normalize_chat_name(window_title)
    target = normalize_chat_name(chat_name)
    if not actual or not target:
        return 0
    if actual == target:
        return 3
    if normalize_chat_name(_DYNAMIC_TITLE_SUFFIX.sub("", actual)) == target:
        return 2
    if target in actual:
        return 1
    return 0


def _strong_chat_name_match_score(actual_name, chat_name):
    """Match a UIA chat name without accepting an unrelated containing name."""
    score = _window_title_match_score(actual_name, chat_name)
    return score if score >= 2 else 0


def _escape_uia_send_keys_text(value):
    """Escape braces so wx4py's UIA SendKeys types a literal search term."""
    return "".join(
        "{{}" if char == "{" else "{}}" if char == "}" else char
        for char in str(value)
    )


def _get_process_image_name(pid):
    """Return a process image path without adding a psutil dependency."""
    try:
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        open_process = kernel32.OpenProcess
        close_handle = kernel32.CloseHandle
        query_name = kernel32.QueryFullProcessImageNameW

        open_process.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        open_process.restype = ctypes.c_void_p
        close_handle.argtypes = [ctypes.c_void_p]
        close_handle.restype = ctypes.c_int
        query_name.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_wchar_p,
            ctypes.POINTER(ctypes.c_uint32),
        ]
        query_name.restype = ctypes.c_int

        process_query_limited_information = 0x1000
        handle = open_process(process_query_limited_information, 0, int(pid))
        if not handle:
            return ""
        try:
            size = ctypes.c_uint32(1024)
            buffer = ctypes.create_unicode_buffer(size.value)
            if query_name(handle, 0, buffer, ctypes.byref(size)):
                return str(buffer.value or "")
        finally:
            close_handle(handle)
    except Exception:
        pass
    return ""


def _is_qt_top_level_window_class(class_name):
    """Accept Qt version changes while retaining the historical class hint."""
    class_name = str(class_name or "")
    return (
        class_name == _WECHAT_NATIVE_WINDOW_CLASS
        or (class_name.startswith("Qt") and "QWindow" in class_name)
    )


def _list_wechat_windows():
    """Enumerate WeChat top-level windows, including minimized windows.

    wx4py identifies windows by the owning ``wechat.exe``/``weixin.exe``
    process.  The older wxauto 4.0 code used one version-specific Qt class.
    Use the process identity as the primary signal and retain a broad Qt class
    fallback for installations where querying another process is denied.
    """
    import win32gui

    try:
        import win32process
    except Exception:
        win32process = None

    windows = []
    executable_names = {}

    def collect(hwnd, _extra):
        try:
            class_name = str(win32gui.GetClassName(hwnd) or "")
            executable_name = ""
            if win32process is not None:
                try:
                    _thread_id, pid = win32process.GetWindowThreadProcessId(hwnd)
                    if pid not in executable_names:
                        executable_names[pid] = ntpath.basename(
                            _get_process_image_name(pid)
                        ).lower()
                    executable_name = executable_names[pid]
                except Exception:
                    executable_name = ""
            is_wechat_process = executable_name in _WECHAT_EXE_NAMES
            class_fallback = (
                not executable_name
                and _is_qt_top_level_window_class(class_name)
            )
            if not is_wechat_process and not class_fallback:
                return True
            is_visible = getattr(win32gui, "IsWindowVisible", None)
            is_iconic = getattr(win32gui, "IsIconic", None)
            # User report: "名称B的独立窗口不在前台焦点，就卡在这里
            # 了" — the independent chat window is hidden from the
            # foreground but the program is still expected to find and
            # use it. WeChat's idle-close / Windows minimize operation
            # leaves the hwnd alive but flips ``IsWindowVisible`` to
            # False; the previous filter dropped those windows and the
            # open path then fell through to a Ctrl+F search + list
            # double-click that never restored the window. A window
            # that's iconic (``IsIconic``) is just minimized — the
            # user can still bring it to the foreground with one click,
            # and the bot must recognize it so the rename auto-rebind
            # can update ``session.name`` to the new title. Truly
            # hidden windows (not visible AND not iconic AND not a Qt
            # top-level class) are still excluded so we don't latch
            # onto Qt helper sub-windows that the user never sees.
            is_minimized = bool(
                callable(is_iconic) and is_iconic(hwnd)
            )
            if (
                is_wechat_process
                and callable(is_visible)
                and not is_visible(hwnd)
                and not is_minimized
                and not _is_qt_top_level_window_class(class_name)
            ):
                return True
            windows.append(
                (hwnd, str(win32gui.GetWindowText(hwnd) or ""), class_name)
            )
        except Exception:
            pass
        return True

    win32gui.EnumWindows(collect, None)
    return windows


def _list_top_level_windows_by_pid(process_id):
    """Enumerate visible top-level windows owned by one process.

    This mirrors wxauto-4.0's ``get_windows_by_pid`` lookup.  It is an
    important fallback for newly-created Qt windows: querying the executable
    path can briefly fail (or report a helper executable), even though Win32
    can already associate the HWND with the main WeChat process.
    """
    if process_id is None:
        return []

    try:
        import win32gui
        import win32process
    except Exception:
        return []

    windows = []

    def collect(hwnd, _extra):
        try:
            _thread_id, candidate_pid = (
                win32process.GetWindowThreadProcessId(hwnd)
            )
            if int(candidate_pid) != int(process_id):
                return True
            is_visible = getattr(win32gui, "IsWindowVisible", None)
            is_iconic = getattr(win32gui, "IsIconic", None)
            # Same rule as ``_list_wechat_windows``: an iconic
            # window is just minimized, not gone — keep it so the
            # bot can restore it via the alias / rename auto-rebind
            # path on the next send.
            if (
                callable(is_visible)
                and not is_visible(hwnd)
                and not (callable(is_iconic) and is_iconic(hwnd))
            ):
                return True
            windows.append(
                (
                    hwnd,
                    str(win32gui.GetWindowText(hwnd) or ""),
                    str(win32gui.GetClassName(hwnd) or ""),
                )
            )
        except Exception:
            pass
        return True

    try:
        win32gui.EnumWindows(collect, None)
    except Exception:
        logger.debug(
            "按微信进程枚举顶层窗口失败 pid=%s",
            process_id,
            exc_info=True,
        )
    return windows


def _merge_window_lists(*window_lists):
    """Merge HWND tuples while keeping the freshest title/class values."""
    order = []
    by_hwnd = {}
    for windows in window_lists:
        for hwnd, title, class_name in windows:
            if hwnd not in by_hwnd:
                order.append(hwnd)
            by_hwnd[hwnd] = (hwnd, title, class_name)
    return [by_hwnd[hwnd] for hwnd in order]


def _list_wechat_windows_for_main(main_hwnd):
    """Return normal WeChat candidates plus every window in the main PID."""
    try:
        windows = list(_list_wechat_windows())
    except Exception:
        logger.debug("枚举微信窗口失败", exc_info=True)
        windows = []

    if not main_hwnd:
        return windows

    main_pid = _get_window_process_id(main_hwnd)
    process_windows = _list_top_level_windows_by_pid(main_pid)
    merged = _merge_window_lists(windows, process_windows)
    if len(merged) > len(windows):
        logger.debug(
            "通过主窗口进程补充微信窗口 main_hwnd=%s pid=%s added=%r",
            main_hwnd,
            main_pid,
            [
                {"hwnd": hwnd, "title": title, "class": class_name}
                for hwnd, title, class_name in merged
                if hwnd not in {item[0] for item in windows}
            ],
        )
    return merged


def _get_window_title(hwnd):
    import win32gui

    return str(win32gui.GetWindowText(hwnd) or "")


def _close_window(hwnd):
    import win32con
    import win32gui

    win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)


def _minimize_window(hwnd):
    """最小化已存在窗口，不读取或点击系统托盘。"""
    import win32con
    import win32gui

    win32gui.ShowWindow(hwnd, win32con.SW_MINIMIZE)
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        if win32gui.IsIconic(hwnd):
            return True
        time.sleep(0.02)
    return bool(win32gui.IsIconic(hwnd))


def _send_native_message(hwnd, message, wparam, lparam, timeout_ms=2000):
    """Send a dialog message without waiting forever on a hung UI thread."""
    import win32con
    import win32gui

    flags = getattr(win32con, "SMTO_ABORTIFHUNG", 0x0002)
    timeout_ms = max(1, int(timeout_ms))
    send_timeout = getattr(win32gui, "SendMessageTimeout", None)
    if callable(send_timeout):
        try:
            return send_timeout(
                hwnd,
                message,
                wparam,
                lparam,
                flags,
                timeout_ms,
            )
        except TypeError:
            # Some pywin32 builds only accept an integer lParam here.  Use the
            # native wide API for WM_SETTEXT instead of falling back to the
            # potentially unbounded SendMessage call.
            if not isinstance(lparam, str):
                raise

    try:
        import ctypes
        from ctypes import wintypes

        result = ctypes.c_size_t()
        text_buffer = None
        native_lparam = lparam
        if isinstance(lparam, str):
            text_buffer = ctypes.create_unicode_buffer(lparam)
            native_lparam = ctypes.cast(text_buffer, ctypes.c_void_p).value
        send_message_timeout = ctypes.windll.user32.SendMessageTimeoutW
        send_message_timeout.restype = wintypes.LPARAM
        ok = send_message_timeout(
            int(hwnd),
            int(message),
            int(wparam),
            int(native_lparam),
            int(flags),
            timeout_ms,
            ctypes.byref(result),
        )
        if not ok:
            raise TimeoutError(
                f"原生窗口消息超时 hwnd={hwnd} message={message}"
            )
        return result.value
    except (ImportError, AttributeError):
        pass
    return win32gui.SendMessage(hwnd, message, wparam, lparam)


def _get_native_foreground_window():
    try:
        import win32gui

        return win32gui.GetForegroundWindow()
    except Exception:
        return None


def _is_native_window_foreground(hwnd):
    """Return whether hwnd, or one of its owned popups, owns foreground."""
    if not hwnd:
        return False
    try:
        import win32con
        import win32gui

        foreground = _get_native_foreground_window()
        if foreground == hwnd:
            return True
        if not foreground:
            return False

        get_ancestor = getattr(win32gui, "GetAncestor", None)
        if not callable(get_ancestor):
            return False
        for flag_name in ("GA_ROOT", "GA_ROOTOWNER"):
            flag = getattr(win32con, flag_name, None)
            if flag is None:
                continue
            try:
                if get_ancestor(foreground, flag) == hwnd:
                    return True
            except Exception:
                continue
    except Exception:
        return False
    return False


def _wait_for_native_window_foreground(hwnd, timeout):
    # The three-second settle above is the full result wait. Keep only a tiny
    # UIA read-retry window here; the outer open path owns the second search.
    deadline = time.monotonic() + min(max(float(timeout), 0.0), 0.25)
    while True:
        if _is_native_window_foreground(hwnd):
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(_WINDOW_ACTIVATION_POLL_INTERVAL, remaining))


def _restore_native_window(hwnd):
    """Show a Qt window using the same topmost pulse as wxauto 4.0."""
    try:
        import win32con
        import win32gui

        if not hwnd or not win32gui.IsWindow(hwnd):
            return False
        if win32gui.IsIconic(hwnd):
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        else:
            win32gui.ShowWindow(
                hwnd,
                getattr(win32con, "SW_SHOWNORMAL", win32con.SW_SHOW),
            )

        _pulse_native_window_to_top(hwnd, win32con, win32gui)

        deadline = time.monotonic() + _WINDOW_ACTIVATION_TIMEOUT
        while time.monotonic() < deadline:
            if (
                win32gui.IsWindow(hwnd)
                and win32gui.IsWindowVisible(hwnd)
                and not win32gui.IsIconic(hwnd)
            ):
                return True
            time.sleep(_WINDOW_ACTIVATION_POLL_INTERVAL)
    except Exception:
        logger.debug("恢复原生窗口失败 hwnd=%s", hwnd, exc_info=True)
        return False
    return False


def _pulse_native_window_to_top(hwnd, win32con, win32gui):
    """Raise hwnd without leaving it permanently topmost."""
    flags = (
        win32con.SWP_NOMOVE
        | win32con.SWP_NOSIZE
        | getattr(win32con, "SWP_SHOWWINDOW", 0)
    )
    was_topmost = False
    try:
        style = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
        was_topmost = bool(style & win32con.WS_EX_TOPMOST)
    except Exception:
        pass

    made_topmost = False
    try:
        win32gui.SetWindowPos(
            hwnd,
            win32con.HWND_TOPMOST,
            0,
            0,
            0,
            0,
            flags,
        )
        made_topmost = not was_topmost
    finally:
        if made_topmost:
            win32gui.SetWindowPos(
                hwnd,
                win32con.HWND_NOTOPMOST,
                0,
                0,
                0,
                0,
                flags,
            )


def _force_native_window_foreground(hwnd, attempts=3, wait_timeout=0.15):
    """Retry foregrounding from the target/foreground input-thread context."""
    try:
        import win32api
        import win32con
        import win32gui
        import win32process
    except Exception:
        logger.debug("导入 Win32 窗口激活模块失败", exc_info=True)
        return False

    try:
        current_tid = threading.get_native_id()
        target_tid, _target_pid = win32process.GetWindowThreadProcessId(hwnd)
        foreground = win32gui.GetForegroundWindow()
        foreground_tid = 0
        if foreground:
            foreground_tid, _foreground_pid = (
                win32process.GetWindowThreadProcessId(foreground)
            )
    except Exception:
        logger.debug("读取窗口线程信息失败 hwnd=%s", hwnd, exc_info=True)
        return False

    attach_thread_input = getattr(win32process, "AttachThreadInput", None)
    if not callable(attach_thread_input):
        try:
            import ctypes

            attach_thread_input = ctypes.windll.user32.AttachThreadInput
        except Exception:
            attach_thread_input = None

    attached_pairs = []
    seen_pairs = set()
    if callable(attach_thread_input):
        for source_tid, target_input_tid in (
            (current_tid, foreground_tid),
            (current_tid, target_tid),
            (target_tid, foreground_tid),
        ):
            pair_key = frozenset((source_tid, target_input_tid))
            if (
                not source_tid
                or not target_input_tid
                or source_tid == target_input_tid
                or pair_key in seen_pairs
            ):
                continue
            seen_pairs.add(pair_key)
            try:
                attached = attach_thread_input(
                    source_tid,
                    target_input_tid,
                    True,
                )
                if attached is not False:
                    attached_pairs.append((source_tid, target_input_tid))
            except Exception:
                logger.debug(
                    "附加窗口输入线程失败 source=%s target=%s",
                    source_tid,
                    target_input_tid,
                    exc_info=True,
                )

    try:
        for attempt in range(max(int(attempts), 1)):
            alt_pressed = False
            try:
                # An ALT key transition releases the foreground lock for the
                # attached input queue without typing into the active window.
                win32api.keybd_event(win32con.VK_MENU, 0, 0, 0)
                alt_pressed = True
            except Exception:
                logger.debug("发送 ALT 前台解锁按下事件失败", exc_info=True)
            finally:
                if alt_pressed:
                    try:
                        win32api.keybd_event(
                            win32con.VK_MENU,
                            0,
                            win32con.KEYEVENTF_KEYUP,
                            0,
                        )
                    except Exception:
                        logger.debug(
                            "发送 ALT 前台解锁释放事件失败",
                            exc_info=True,
                        )

            try:
                win32gui.BringWindowToTop(hwnd)
            except Exception:
                logger.debug("BringWindowToTop 失败 hwnd=%s", hwnd, exc_info=True)
            try:
                win32gui.SetActiveWindow(hwnd)
            except Exception:
                logger.debug("SetActiveWindow 失败 hwnd=%s", hwnd, exc_info=True)
            try:
                win32gui.SetForegroundWindow(hwnd)
            except Exception:
                logger.debug("SetForegroundWindow 失败 hwnd=%s", hwnd, exc_info=True)

            if _wait_for_native_window_foreground(hwnd, wait_timeout):
                return True

            if attempt == 1:
                switch_to = getattr(win32gui, "SwitchToThisWindow", None)
                if callable(switch_to):
                    try:
                        switch_to(hwnd, True)
                    except Exception:
                        logger.debug(
                            "SwitchToThisWindow 失败 hwnd=%s",
                            hwnd,
                            exc_info=True,
                        )
            elif attempt == 2:
                try:
                    _pulse_native_window_to_top(hwnd, win32con, win32gui)
                    win32gui.SetForegroundWindow(hwnd)
                except Exception:
                    logger.debug("窗口置顶脉冲失败 hwnd=%s", hwnd, exc_info=True)

            if _wait_for_native_window_foreground(hwnd, wait_timeout):
                return True
    finally:
        for source_tid, target_input_tid in reversed(attached_pairs):
            try:
                attach_thread_input(source_tid, target_input_tid, False)
            except Exception:
                logger.debug(
                    "分离窗口输入线程失败 source=%s target=%s",
                    source_tid,
                    target_input_tid,
                    exc_info=True,
                )
    return False


def _activate_native_window(hwnd):
    """Restore, show and activate a WeChat 4.0 top-level window."""
    if _is_native_window_foreground(hwnd):
        return True
    if not _restore_native_window(hwnd):
        return False
    try:
        root = _control_from_handle(hwnd)
        show = getattr(root, "Show", None)
        if callable(show):
            _call_uia_without_wait(show)
    except Exception:
        logger.debug("通过 UIA 显示微信窗口失败 hwnd=%s", hwnd, exc_info=True)
    if _is_native_window_foreground(hwnd):
        return True

    with _ACTIVATION_STRATEGY_LOCK:
        preferred_strategy = _ACTIVATION_STRATEGY_BY_HWND.get(hwnd)
    if preferred_strategy == "force":
        if _force_native_window_foreground(
            hwnd,
            attempts=1,
            wait_timeout=0.05,
        ):
            return True
        with _ACTIVATION_STRATEGY_LOCK:
            _ACTIVATION_STRATEGY_BY_HWND.pop(hwnd, None)

    try:
        from wx4py.core.win32 import bring_window_to_front

        bring_window_to_front(hwnd)
        if _wait_for_native_window_foreground(hwnd, 0.15):
            with _ACTIVATION_STRATEGY_LOCK:
                _ACTIVATION_STRATEGY_BY_HWND[hwnd] = "wx4py"
            return True
    except Exception:
        logger.debug("WeMai 前台激活窗口失败 hwnd=%s", hwnd, exc_info=True)

    try:
        root = _control_from_handle(hwnd)
        set_active = getattr(root, "SetActive", None)
        if callable(set_active):
            set_active(waitTime=0)
            if _wait_for_native_window_foreground(hwnd, 0.15):
                with _ACTIVATION_STRATEGY_LOCK:
                    _ACTIVATION_STRATEGY_BY_HWND[hwnd] = "uia"
                return True

        switch_to = getattr(root, "SwitchToThisWindow", None)
        if callable(switch_to):
            switch_to(waitTime=0)
            if _wait_for_native_window_foreground(hwnd, 0.15):
                with _ACTIVATION_STRATEGY_LOCK:
                    _ACTIVATION_STRATEGY_BY_HWND[hwnd] = "uia"
                return True
    except Exception:
        logger.debug("UIA 激活窗口失败 hwnd=%s", hwnd, exc_info=True)

    if _force_native_window_foreground(hwnd):
        with _ACTIVATION_STRATEGY_LOCK:
            _ACTIVATION_STRATEGY_BY_HWND[hwnd] = "force"
        return True
    logger.warning(
        "原生窗口激活后仍未取得前台焦点 hwnd=%s foreground_hwnd=%s",
        hwnd,
        _get_native_foreground_window(),
    )
    return False


def _get_window_process_id(hwnd):
    try:
        import win32process

        return int(win32process.GetWindowThreadProcessId(hwnd)[1])
    except Exception:
        try:
            return int(_control_from_handle(hwnd).ProcessId)
        except Exception:
            return None


def _get_uia_window_class(hwnd):
    try:
        return _safe_control_text(_control_from_handle(hwnd), "ClassName")
    except Exception:
        return ""


def _read_window_identity(hwnd, native_title=None):
    """Read independent-window identity from both Win32 and UIAutomation."""
    if native_title is None:
        try:
            native_title = _get_window_title(hwnd)
        except Exception:
            native_title = ""
    root = None
    with contextlib.suppress(Exception):
        root = _control_from_handle(hwnd)

    ui_class = _safe_control_text(root, "ClassName") if root is not None else ""
    root_name = _safe_control_text(root, "Name").strip() if root is not None else ""
    current_chat_name = ""
    has_main_structure = ui_class == _MAIN_WINDOW_CLASS
    if root is not None and not has_main_structure:
        try:
            # WeChat 4.0 publishes the independent chat name directly on its
            # stable root class. Avoid walking ChatMessagePage again when that
            # authoritative identity is already available.
            if ui_class != _SUB_WINDOW_CLASS or not root_name:
                current_chat_name = _find_current_chat_name(root).strip()
        except Exception:
            pass
        if ui_class != _SUB_WINDOW_CLASS and not has_main_structure:
            with contextlib.suppress(Exception):
                has_main_structure = (
                    _find_main_tab_bar(root) is not None
                    or _find_session_panel(root) is not None
                )
    return {
        "native_title": str(native_title or ""),
        "root_name": root_name,
        "current_chat_name": current_chat_name,
        "ui_class": ui_class,
        "has_main_structure": has_main_structure,
    }


def _window_identity_match_score(identity, chat_name):
    """Score corroborating native/UIA names and reject explicit UIA mismatch."""
    if (
        identity.get("ui_class") == _MAIN_WINDOW_CLASS
        or identity.get("has_main_structure")
    ):
        return 0
    native_score = _window_title_match_score(
        identity.get("native_title", ""),
        chat_name,
    )
    root_name = identity.get("root_name", "")
    current_chat_name = identity.get("current_chat_name", "")
    root_score = _strong_chat_name_match_score(root_name, chat_name)
    current_score = _strong_chat_name_match_score(current_chat_name, chat_name)

    # wxauto 4.0 exposes the independent chat nickname as the UIA root Name,
    # while wx4py can also expose the current-chat label.  Either is stronger
    # evidence than a Win32 title containing the configured text.
    #
    # User report: "改完群名后 ... 适配器就会在列表把鼠标放在名称B，
    # 但因为名称B的独立窗口是已经打开的" — after the user renames a
    # chat in WeChat, the native window title is updated synchronously
    # (Win32 SetWindowText) but the Qt accessibility tree
    # (``current_chat_name`` / ``root_name``) is rebuilt asynchronously
    # and lags the title by a few seconds. During that gap the strict
    # rules below would reject the (otherwise correct) window and
    # force the bot to fall through to the list path, where it
    # double-clicks the new name in a window that's already open and
    # gets stuck. The relaxation: if the UIA tree is *stale* (its
    # published name doesn't strongly match the target) but the
    # native title is a *strong* match (>= 2: exact or dynamic
    # suffix), trust the native title. A weak native match (1 =
    # substring) is still rejected because the substring could be a
    # coincidental overlap with a different chat whose name happens
    # to embed the target.
    #
    # Round 9 safety: a weak native match (1 = substring) must not
    # be rescued by a UIA tree that happens to match the target —
    # the open window could belong to a *different* chat whose name
    # contains the target as a prefix (e.g. bot configured for
    # "测试群", open window is "测试群v2的讨论" which is a wholly
    # different chat).  The UIA tree reporting the target by
    # coincidence does not change that, because the UIA is itself
    # rebuilt asynchronously and can be stale in either direction.
    # The rule:
    #   * If UIA is empty (no accessibility info yet) and native
    #     is a small-suffix substring, trust the native — the
    #     bot is in a pure "rename in progress" situation where
    #     the cached session should rebind to the new title.
    #   * If UIA is available (current_chat_name or root_name
    #     non-empty) and native is a substring, reject — the UIA
    #     match is not strong enough evidence to override a weak
    #     native match, and the open window is more likely a
    #     different chat than a rename.
    # An empty native (0) is left alone so the existing
    # "UIA-only" trust path (e.g. ``test_window_lookup_uses_uia_
    # root_name_when_native_title_is_blank``) still works.
    if native_score == 1:
        if current_chat_name:
            return 0
        if (
            root_name
            and normalize_chat_name(root_name).casefold()
            not in {"微信", "wechat", "weixin"}
        ):
            return 0
    if current_chat_name and not current_score and native_score < 2:
        return 0
    if (
        root_name
        and normalize_chat_name(root_name).casefold()
        not in {"微信", "wechat", "weixin"}
        and not root_score
        and native_score < 2
    ):
        return 0
    if current_score:
        return 300 + current_score
    if root_score:
        return 200 + root_score
    return native_score


def _match_window_identity(hwnd, chat_name, native_title=None):
    identity = _read_window_identity(hwnd, native_title=native_title)
    return _window_identity_match_score(identity, chat_name), identity


def _main_window_structure_score(root):
    """Return positive evidence that one UIA root is WeChat's main window."""
    if root is None:
        return 0
    root_class = _safe_control_text(root, "ClassName")
    if root_class == _SUB_WINDOW_CLASS:
        return 0
    if root_class == _MAIN_WINDOW_CLASS:
        return 1000
    score = 0
    try:
        if _find_main_tab_bar(root) is not None:
            score += 100
    except Exception:
        pass
    try:
        if _find_session_panel(root) is not None:
            score += 200
    except Exception:
        pass
    return score


def _find_actual_main_window(preferred_hwnd=None):
    """Resolve the real ``mmui::MainWindow`` instead of trusting wx4py's HWND.

    wx4py's native window scorer can tie the main window with an already-open
    independent chat because both belong to Weixin.exe and use the same Qt
    native class.  The UIA hierarchy is authoritative for this distinction.
    """
    if preferred_hwnd:
        try:
            preferred_root = _control_from_handle(preferred_hwnd)
        except Exception:
            preferred_root = None
        if _safe_control_text(preferred_root, "ClassName") == _MAIN_WINDOW_CLASS:
            return preferred_hwnd

    try:
        windows = _list_wechat_windows_for_main(preferred_hwnd)
    except Exception:
        windows = []
    if preferred_hwnd and preferred_hwnd not in {item[0] for item in windows}:
        try:
            windows.insert(
                0,
                (
                    preferred_hwnd,
                    _get_window_title(preferred_hwnd),
                    "",
                ),
            )
        except Exception:
            windows.insert(0, (preferred_hwnd, "", ""))

    preferred_pid = _get_window_process_id(preferred_hwnd) if preferred_hwnd else None
    candidates = []
    for hwnd, title, _native_class in windows:
        if preferred_pid is not None:
            candidate_pid = _get_window_process_id(hwnd)
            if candidate_pid is not None and candidate_pid != preferred_pid:
                continue
        try:
            root = _control_from_handle(hwnd)
        except Exception:
            continue
        score = _main_window_structure_score(root)
        if not score:
            continue
        if normalize_chat_name(title).casefold() in {"微信", "wechat", "weixin"}:
            score += 10
        if hwnd == preferred_hwnd:
            score += 1
        candidates.append((score, hwnd))

    if not candidates:
        return None
    candidates.sort(reverse=True)
    best_score = candidates[0][0]
    best = [hwnd for score, hwnd in candidates if score == best_score]
    if len(best) != 1:
        logger.error("存在多个微信主窗口候选，拒绝猜测 hwnds=%s", best)
        return None
    return best[0]


def _is_subwindow_candidate(hwnd, main_pid=None):
    """Apply process affinity and reject an explicit UIA main-window root.

    WeChat 4.0 gives its main and independent windows the same native Qt
    class (historically ``Qt51514QWindowIcon``), so the native class is not a
    discriminator.  A published ``mmui::MainWindow`` UIA class is definitive;
    generic/transitional classes are validated by chat identity and structure
    later in the opening path.
    """
    if not hwnd:
        return False
    if main_pid is not None:
        candidate_pid = _get_window_process_id(hwnd)
        if candidate_pid is not None and candidate_pid != main_pid:
            return False
    return _get_uia_window_class(hwnd) != _MAIN_WINDOW_CLASS


def _list_new_window_candidates(main_hwnd, existing_hwnds):
    """Return top-level windows that appeared after the opening click.

    The foreground fallback matters when process/class enumeration is briefly
    stale just after Qt creates a native window.
    """
    windows = _list_wechat_windows_for_main(main_hwnd)

    known_hwnds = {hwnd for hwnd, _title, _class_name in windows}
    foreground = _get_native_foreground_window()
    if (
        foreground
        and foreground != main_hwnd
        and foreground not in existing_hwnds
        and foreground not in known_hwnds
    ):
        main_pid = _get_window_process_id(main_hwnd)
        foreground_pid = _get_window_process_id(foreground)
        if (
            main_pid is None
            or foreground_pid is None
            or foreground_pid == main_pid
        ):
            try:
                import win32gui

                windows.append(
                    (
                        foreground,
                        str(win32gui.GetWindowText(foreground) or ""),
                        str(win32gui.GetClassName(foreground) or ""),
                    )
                )
            except Exception:
                windows.append((foreground, "", ""))

    return [
        (hwnd, title, class_name)
        for hwnd, title, class_name in windows
        if hwnd != main_hwnd and hwnd not in existing_hwnds
    ]


def _find_window_by_title(title, exclude_hwnd=None):
    """Find one unambiguous chat window using native and UIA identities."""
    if not normalize_chat_name(title):
        return None

    windows = _list_wechat_windows_for_main(exclude_hwnd)
    logger.debug(
        "微信窗口标题候选 target=%r exclude_hwnd=%s windows=%r",
        title,
        exclude_hwnd,
        [
            {"hwnd": hwnd, "title": window_title, "class": class_name}
            for hwnd, window_title, class_name in windows
        ],
    )
    main_pid = _get_window_process_id(exclude_hwnd) if exclude_hwnd else None
    matches = []
    for hwnd, window_title, class_name in windows:
        if hwnd == exclude_hwnd:
            logger.debug(
                "窗口标题匹配判断 target=%r hwnd=%s title=%r class=%r "
                "result=excluded",
                title,
                hwnd,
                window_title,
                class_name,
            )
            continue
        native_name = normalize_chat_name(window_title)
        native_score = _window_title_match_score(window_title, title)
        if (
            native_name
            and native_name.casefold() not in {"微信", "wechat", "weixin"}
            and not native_score
        ):
            # A different, explicit native title is conclusive. Avoid several
            # lazy UIA lookups for every already-open unrelated chat window.
            logger.debug(
                "按原生标题快速排除窗口 target=%r hwnd=%s title=%r class=%r",
                title,
                hwnd,
                window_title,
                class_name,
            )
            continue
        if main_pid is not None:
            candidate_pid = _get_window_process_id(hwnd)
            if candidate_pid is not None and candidate_pid != main_pid:
                logger.debug(
                    "忽略非当前微信进程的独立窗口 hwnd=%s title=%r class=%r",
                    hwnd,
                    window_title,
                    class_name,
                )
                continue
        score, identity = _match_window_identity(
            hwnd,
            title,
            native_title=window_title,
        )
        if (
            identity["ui_class"] == _MAIN_WINDOW_CLASS
            or identity["has_main_structure"]
        ):
            logger.debug(
                "忽略微信主窗口候选 hwnd=%s title=%r class=%r",
                hwnd,
                window_title,
                class_name,
            )
            continue
        logger.debug(
            "窗口标题匹配判断 target=%r target_key=%r hwnd=%s title=%r "
            "title_key=%r class=%r ui_class=%r root_name=%r "
            "current_chat_name=%r score=%d",
            title,
            normalize_chat_name(title),
            hwnd,
            window_title,
            normalize_chat_name(window_title),
            class_name,
            identity["ui_class"],
            identity["root_name"],
            identity["current_chat_name"],
            score,
        )
        if score:
            matches.append((score, hwnd, window_title))

    if not matches:
        logger.debug("微信窗口标题无匹配 target=%r", title)
        return None
    best_score = max(item[0] for item in matches)
    best = [item for item in matches if item[0] == best_score]
    if len(best) != 1:
        logger.error(
            "存在多个同名微信窗口，拒绝自动选择 chat=%s windows=%s",
            title,
            [(hwnd, window_title) for _score, hwnd, window_title in best],
        )
        return None
    logger.debug(
        "微信窗口标题匹配成功 target=%r hwnd=%s title=%r score=%d",
        title,
        best[0][1],
        best[0][2],
        best[0][0],
    )
    return best[0][1]


def _find_session_list(root):
    """Locate ChatMasterView/ChatSessionList/XTableView from WeChat 4.0."""
    panel = _find_session_panel(root)
    container = _find_control(
        panel,
        "GroupControl",
        ClassName=_SESSION_LIST_CONTAINER_CLASS,
    )
    session_list = _find_control(
        container,
        "ListControl",
        ClassName=_TABLE_VIEW_CLASS,
        Name="会话",
    )
    if session_list is not None:
        return session_list

    # Older wx4py releases expose an AutomationId synthesized by UIA. Keep it
    # as a compatibility path, after the native 4.0 hierarchy.
    try:
        session_list = root.ListControl(AutomationId="session_list")
        if _control_exists(session_list, min(_UI_LOOKUP_TIMEOUT, 0.1)):
            return session_list
    except Exception:
        pass

    candidates = []
    try:
        for control, depth in _walk_controls(root, max_depth=8):
            if _safe_control_text(control, "ControlTypeName") != "ListControl":
                continue
            automation_id = _safe_control_text(control, "AutomationId")
            name = _safe_control_text(control, "Name")
            if automation_id == "session_list":
                candidates.append((2, -depth, control))
            elif name == "会话":
                candidates.append((1, -depth, control))
    except Exception:
        logger.debug("遍历微信主窗口查找 session_list 失败", exc_info=True)
        return None

    if not candidates:
        return None
    candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return candidates[0][2]


def _ensure_chat_page(root, timeout=1.0, stop_event=None):
    _raise_if_stopped(stop_event)
    session_list = _find_session_list(root)
    if session_list is not None:
        return session_list
    if not _switch_to_chat_page(root):
        return None
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        _raise_if_stopped(stop_event)
        session_list = _find_session_list(root)
        if session_list is not None:
            return session_list
        time.sleep(_UI_POLL_INTERVAL)
    return None


def _find_session_item(root, chat_name, allow_contains=False):
    """Find a chat row, preferring exact over reasonable contained names."""
    session_list = _find_session_list(root)
    if session_list is None:
        return None

    target_key = normalize_chat_name(chat_name)
    if not target_key:
        return None

    max_extra_length = max(8, len(target_key))
    list_item_names = []
    exact_candidates = []
    contains_candidates = []
    try:
        direct_rows = _safe_control_children(session_list)
        if direct_rows:
            candidate_rows = [(control, 1) for control in direct_rows]
        else:
            candidate_rows = list(_walk_controls(session_list, max_depth=6))
        for control, depth in candidate_rows:
            control_type = _safe_control_text(control, "ControlTypeName")
            class_name = _safe_control_text(control, "ClassName")
            if (
                control_type not in {"ListItemControl", "DataItemControl"}
                and "Session" not in class_name
                and "XTableCell" not in class_name
            ):
                continue
            control_name = _safe_control_text(control, "Name")
            control_key = normalize_chat_name(control_name)
            list_item_names.append(control_name)
            first_line = next(
                (
                    line.strip()
                    for line in control_name.splitlines()
                    if line.strip()
                ),
                "",
            )
            stable_first_line = _DYNAMIC_TITLE_SUFFIX.sub("", first_line).strip()
            if (
                chat_names_equal(first_line, chat_name)
                or chat_names_equal(stable_first_line, chat_name)
            ):
                match_kind = "exact"
                exact_specificity = 2
            elif _control_has_exact_chat_name(control, chat_name):
                match_kind = "exact-child"
                exact_specificity = 1
            elif target_key in control_key:
                match_kind = "contains"
                exact_specificity = 0
            else:
                match_kind = None
                exact_specificity = 0

            extra_length = max(0, len(control_key) - len(target_key))
            length_ok = (
                match_kind != "contains" or extra_length <= max_extra_length
            )
            class_score = 30 * int(
                any(
                    marker in class_name
                    for marker in ("Session", "Conversation", "Cell")
                )
            )
            try:
                selected = bool(control.IsSelected)
            except Exception:
                selected = False
            selected_score = 80 if selected else 0
            match_score = 100 if match_kind in {"exact", "exact-child"} else 50
            score = match_score + selected_score + class_score
            logger.debug(
                "会话列表匹配判断 chat=%r target_key=%r name=%r name_key=%r "
                "class=%r depth=%s match=%s extra_length=%d max_extra=%d "
                "length_ok=%s selected=%s score=%d",
                chat_name,
                target_key,
                control_name,
                control_key,
                class_name,
                depth,
                match_kind or "none",
                extra_length,
                max_extra_length,
                length_ok,
                selected,
                score if match_kind else 0,
            )
            if match_kind is None or not length_ok:
                continue
            candidate = (
                score,
                exact_specificity,
                -depth,
                control,
                control_name,
                match_kind,
            )
            if match_kind in {"exact", "exact-child"}:
                exact_candidates.append(candidate)
            else:
                contains_candidates.append(candidate)
    except Exception:
        logger.debug("遍历微信会话列表失败 chat=%s", chat_name, exc_info=True)
        logger.debug(
            "微信会话列表项 chat=%r count=%d names=%r traversal=failed",
            chat_name,
            len(list_item_names),
            list_item_names,
        )
        return None

    logger.debug(
        "微信会话列表项 chat=%r count=%d names=%r",
        chat_name,
        len(list_item_names),
        list_item_names,
    )
    # WeChat 4.0 exposes the chat title either as the first row line or as a
    # descendant label. A contains-only row can be another real conversation.
    candidates = exact_candidates
    if not candidates and not allow_contains:
        logger.debug(
            "微信会话列表无精确匹配 chat=%r contains=%r",
            chat_name,
            [candidate[4] for candidate in contains_candidates],
        )
        return None
    if not candidates:
        candidates = contains_candidates
        if not candidates:
            return None
    candidates.sort(key=lambda item: (item[0], item[1], item[2]), reverse=True)
    chosen = candidates[0]
    logger.debug(
        "微信会话列表匹配成功 chat=%r name=%r match=%s score=%d",
        chat_name,
        chosen[4],
        chosen[5],
        chosen[0],
    )
    return chosen[3]


def _find_fuzzy_session_item(root, chat_name):
    """Find a bounded contains match after an exact session lookup fails."""
    return _find_session_item(root, chat_name, allow_contains=True)


def _scroll_container_percent(container):
    """Return the container's vertical scroll percent, or ``None`` if unknown.

    UIA returns ``-1`` for "no scroll" (the list is shorter than the
    viewport) and ``0..100`` for the percent scrolled. ``None`` means
    the container does not expose a scroll pattern at all.
    """
    try:
        pattern = container.GetScrollPattern()
    except Exception:
        return None
    if pattern is None:
        return None
    try:
        return float(pattern.VerticalScrollPercent)
    except (AttributeError, TypeError, ValueError):
        return None


def _session_list_scroll_container(session_list):
    """Walk up from ``session_list`` to the first ancestor that exposes a scroll pattern.

    WeChat 4.x nests the virtualized list inside a few
    ``GroupControl`` wrappers; the scroll pattern lives on the outer
    ``mmui::XTableView`` (or equivalent), not on the
    ``ListControl`` we feed ``_find_session_list``. ``None`` means
    no scrollable ancestor was found within a few levels, in which
    case the caller cannot scroll the list and must rely on the
    direct (in-viewport) lookup.
    """
    container = session_list
    for _ in range(4):
        try:
            parent = container.GetParentControl()
        except Exception:
            parent = None
        if parent is None:
            return None
        if _scroll_container_percent(parent) is not None:
            return parent
        container = parent
    return None


def _find_session_item_with_scroll(
    root,
    chat_name,
    max_scrolls=6,
    stop_event=None,
):
    """Find a session row even when it has been scrolled off the viewport.

    ``_find_session_item`` walks the children of the
    ``ChatSessionList``. WeChat 4.x virtualizes that list, so a
    chat that has been pushed below the viewport by other messages
    simply is not in the subtree. This function scrolls the list
    one page at a time, re-checks, and stops as soon as the
    vertical scroll position stops advancing (which is the
    end-of-list signal — a no-op ``WheelDown`` does not change
    the percent). The match is still done by ``_find_session_item``
    (exact + bounded contains), so a renamed chat is correctly
    rejected and surfaces to the caller's ``ChatNotFoundError``
    fail-fast path.

    Returns the matched control, or ``None`` if the chat is not
    in the list even after scrolling to the bottom. The function
    does not raise; the caller decides what to do with a miss.
    """
    if stop_event is not None:
        _raise_if_stopped(stop_event)
    item = _find_session_item(root, chat_name)
    if item is not None:
        return item
    session_list = _find_session_list(root)
    if session_list is None:
        return None
    container = _session_list_scroll_container(session_list)
    if container is None:
        return None
    last_percent = _scroll_container_percent(container)
    for _ in range(max_scrolls):
        if stop_event is not None:
            _raise_if_stopped(stop_event)
        try:
            container.WheelDown(waitTime=0)
        except Exception:
            logger.debug(
                "会话列表向下滚动失败，停止滚屏查找 chat=%r",
                chat_name,
                exc_info=True,
            )
            return None
        # Let Qt virtualize the next page of rows before reading.
        time.sleep(_UI_POLL_INTERVAL)
        item = _find_session_item(root, chat_name)
        if item is not None:
            logger.info(
                "会话列表滚屏后找到目标 chat=%r",
                chat_name,
            )
            return item
        current_percent = _scroll_container_percent(container)
        if (
            current_percent is not None
            and last_percent is not None
            and current_percent <= last_percent
        ):
            # WheelDown did not advance the viewport — we are at
            # the bottom of the list. The chat is not here.
            logger.debug(
                "会话列表滚屏到底仍未命中 chat=%r last=%s current=%s",
                chat_name,
                last_percent,
                current_percent,
            )
            return None
        if current_percent is not None:
            last_percent = current_percent
    logger.info(
        "会话列表滚屏 %d 次仍未命中 chat=%r",
        max_scrolls,
        chat_name,
    )
    return None


def _physical_double_click_control(control):
    """Issue a real Win32 double-click at the current control center.

    WeChat 4.x occasionally treats wx4py/UIAutomation ``DoubleClick`` as a
    single selection click when the main window has just regained focus.  A
    normal Win32 mouse sequence matches a user double-click and reliably opens
    the independent window on the first attempt.  No preliminary single click
    is sent, so Qt cannot rebuild the row between selection and activation.
    """
    try:
        import win32api
        import win32con

        rect = control.BoundingRectangle
        left, top = int(rect.left), int(rect.top)
        right, bottom = int(rect.right), int(rect.bottom)
        if right <= left or bottom <= top:
            return False
        x = (left + right) // 2
        y = (top + bottom) // 2
        win32api.SetCursorPos((x, y))
        # Let the foreground transition and cursor move settle before the
        # first press.  Keep the two clicks comfortably inside the system
        # double-click interval while still looking like physical input.
        time.sleep(min(_UI_POLL_INTERVAL, 0.05))
        for index in range(2):
            win32api.mouse_event(
                win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0
            )
            time.sleep(0.02)
            win32api.mouse_event(
                win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0
            )
            if index == 0:
                time.sleep(0.08)
        logger.debug(
            "已提交 Win32 物理双击 control_rect=(%d,%d,%d,%d)",
            left,
            top,
            right,
            bottom,
        )
        return True
    except Exception:
        logger.debug("Win32 物理双击会话项失败", exc_info=True)
        return False


def _physical_click_control(control):
    """Issue one real Win32 click at the current control center."""
    try:
        import win32api
        import win32con

        rect = control.BoundingRectangle
        left, top = int(rect.left), int(rect.top)
        right, bottom = int(rect.right), int(rect.bottom)
        if right <= left or bottom <= top:
            return False
        x = (left + right) // 2
        y = (top + bottom) // 2
        win32api.SetCursorPos((x, y))
        time.sleep(min(_UI_POLL_INTERVAL, 0.05))
        win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
        time.sleep(0.02)
        win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
        logger.debug(
            "已提交 Win32 物理单击 control_rect=(%d,%d,%d,%d)",
            left,
            top,
            right,
            bottom,
        )
        return True
    except Exception:
        logger.debug("Win32 物理单击控件失败", exc_info=True)
        return False


def _double_click_control(control):
    """Bring a session row into view and issue one real double-click.

    A separate preliminary ``Click`` changes the current chat and can rebuild
    Qt's accessibility row before ``DoubleClick`` runs.  Physical double-click
    is the primary path; wx4py UIA is retained only as a compatibility fallback.
    """
    try:
        parent = control.GetParentControl()
        _roll_control_into_view(parent, control)
    except Exception:
        logger.debug("滚动会话项到可见区域失败", exc_info=True)

    if _physical_double_click_control(control):
        return True

    double_click = getattr(control, "DoubleClick", None)
    if callable(double_click):
        for kwargs in (
            {"simulateMove": False, "waitTime": 0},
            {"simulateMove": False},
            {},
        ):
            try:
                double_click(**kwargs)
                return True
            except TypeError:
                continue
            except Exception:
                logger.debug(
                    "UIA 双击会话项失败，回退到物理双击",
                    exc_info=True,
                )
                break

    return False


def _control_from_handle(hwnd):
    from wx4py.core import uiautomation as uia

    return uia.ControlFromHandle(hwnd)


def _find_message_list(root):
    chat_box = _find_chat_box(root)
    message_view = _find_control(
        chat_box,
        "GroupControl",
        ClassName=_MESSAGE_VIEW_CLASS,
    )
    message_list = _find_control(message_view, "ListControl")
    if message_list is not None:
        return message_list

    try:
        message_list = root.ListControl(AutomationId="chat_message_list")
        if _control_exists(message_list, _UI_LOOKUP_TIMEOUT):
            return message_list
    except Exception:
        pass

    candidates = []
    try:
        for control, depth in _walk_controls(root, max_depth=8):
            if _safe_control_text(control, "ControlTypeName") != "ListControl":
                continue
            score = 0
            for child in _safe_control_children(control)[-12:]:
                class_name = _safe_control_text(child, "ClassName")
                control_type = _safe_control_text(child, "ControlTypeName")
                if class_name in _MESSAGE_CLASSES:
                    score += 10
                elif class_name == _TIME_MESSAGE_CLASS:
                    score += 2
                elif control_type == "ListItemControl":
                    score += 1
            if score:
                candidates.append((score, -depth, control))
    except Exception:
        logger.debug("遍历微信聊天区查找消息列表失败", exc_info=True)
        return None
    if not candidates:
        return None
    candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return candidates[0][2]


def _require_subwindow_message_list(root, chat_name, hwnd=None):
    """Validate an independent chat by its UI structure, not root class.

    ``mmui::FramelessMainWindow`` remains a useful hint, but it is not a
    stable gate across WeChat 4.0 builds.  A usable independent chat must have
    a message list and, unlike the main window, must not own ChatMasterView's
    session panel.
    """
    root_class = _safe_control_text(root, "ClassName")
    if root_class == _MAIN_WINDOW_CLASS:
        raise RuntimeError(
            f"目标窗口是微信主窗口，不能作为独立聊天窗口: "
            f"chat={chat_name!r} class={root_class!r} hwnd={hwnd}"
        )
    if root_class != _SUB_WINDOW_CLASS and (
        _find_main_tab_bar(root) is not None
        or _find_session_panel(root) is not None
    ):
        raise RuntimeError(
            f"目标窗口包含主窗口导航或 ChatMasterView，不能作为独立聊天窗口: "
            f"chat={chat_name!r} class={root_class!r} hwnd={hwnd}"
        )

    message_list = _find_message_list(root)
    if not message_list or not _control_exists_now(message_list):
        raise RuntimeError(
            f"独立窗口中未找到 MessageView 消息列表: {chat_name}; "
            f"class={root_class!r} hwnd={hwnd}"
        )

    if root_class and root_class != _SUB_WINDOW_CLASS:
        logger.debug(
            "独立窗口使用非标准 UIA 根类，已通过聊天结构验证 "
            "chat=%r hwnd=%s class=%r",
            chat_name,
            hwnd,
            root_class,
        )
    return message_list


def _read_visible_items(message_list):
    """Read visible rows while preserving UIA child-enumeration failures."""
    children = list(message_list.GetChildren())
    items = []
    for child in children:
        class_name = _safe_control_text(child, "ClassName")
        name = _safe_control_text(child, "Name").strip()
        control_type = _safe_control_text(child, "ControlTypeName")
        automation_id = _safe_control_text(child, "AutomationId")
        message_type = _classify_message_type(class_name, name, child)
        if not name and message_type == "image":
            name = "[图片]"
        if not name:
            continue

        if class_name == _TIME_MESSAGE_CLASS:
            kind = "time/system"
        elif message_type == "image":
            # The concrete image signature is stronger evidence than an empty
            # AutomationId on newer WeChat 4.x builds.
            kind = "message"
        elif not automation_id and (
            class_name in _MESSAGE_CLASSES
            or control_type == "ListItemControl"
        ):
            kind = "time/system"
        elif class_name in _MESSAGE_CLASSES or control_type == "ListItemControl":
            kind = "message"
        else:
            continue
        items.append(
            _VisibleUIItem(
                kind=kind,
                name=name,
                class_name=class_name,
                runtime_id=_safe_runtime_id(child),
                control=child,
                message_type=message_type,
            )
        )
    return items


def _initial_session_baseline(message_list, callback):
    """Read UI message history only when UIA is the inbound source.

    In WeFlow mode ``callback`` is deliberately ``None`` for the UI worker;
    all inbound messages arrive through SSE.  Walking every visible chat bubble
    during the first outbound open costs roughly 0.6 seconds and produces a
    snapshot that is never consumed, so keep only the cheap empty baseline.
    """
    if callback is None:
        return []
    return _read_visible_items(message_list)


def _direction_from_message_screenshot(image_path):
    """Use wxauto 4.0's pixel-column algorithm to locate the message side."""
    from PIL import Image

    with Image.open(image_path) as source:
        image = source.convert("RGB") if source.mode != "RGB" else source.copy()
    width, height = image.size
    if width <= 0 or height <= 0:
        return None
    band_height = max(1, int(height * 0.8))
    y0 = max(0, (height - band_height) // 2)
    y1 = min(height, y0 + band_height)
    pixels = image.load()

    def is_uniform_column(x):
        base = pixels[x, y0]
        return all(pixels[x, y] == base for y in range(y0, y1))

    left_distance = math.inf
    for x in range(width):
        if not is_uniform_column(x):
            left_distance = x
            break
    right_distance = math.inf
    for offset, x in enumerate(range(width - 1, -1, -1)):
        if not is_uniform_column(x):
            right_distance = offset
            break
    if left_distance == math.inf and right_distance == math.inf:
        return None
    return "left" if left_distance <= right_distance else "right"


def _message_direction(control):
    """Return ``left`` for a received message and ``right`` for a sent one."""
    explicit = _safe_control_text(control, "MessageDirection").casefold()
    if explicit in {"left", "right"}:
        return explicit

    # wxauto 4.0 determines direction from the complete row screenshot.  A
    # named descendant is not reliable: menus and action buttons may be the
    # first named ButtonControl and can sit on the opposite side of the row.
    screenshot = None
    try:
        capture = getattr(control, "ScreenShot", None)
        if callable(capture):
            screenshot = capture()
        if screenshot and os.path.isfile(os.fspath(screenshot)):
            direction = _direction_from_message_screenshot(os.fspath(screenshot))
            if direction in {"left", "right"}:
                return direction
    except Exception:
        logger.debug("判断微信图片消息方向失败", exc_info=True)
    finally:
        if screenshot:
            with contextlib.suppress(OSError):
                os.unlink(os.fspath(screenshot))

    row_rect = _control_rectangle(control)
    if row_rect is not None:
        midpoint = (row_rect[0] + row_rect[2]) / 2
        for child, _depth in _walk_control_children(control, max_depth=4):
            if _safe_control_text(child, "ControlTypeName") != "ButtonControl":
                continue
            if not _safe_control_text(child, "Name").strip():
                continue
            child_rect = _control_rectangle(child)
            if child_rect is None:
                continue
            child_midpoint = (child_rect[0] + child_rect[2]) / 2
            return "left" if child_midpoint < midpoint else "right"
    return None


def _message_sender(control, content, chat_type, chat_name, direction=None):
    """Extract a sender from a WeChat 4.x row, with structural fallbacks."""
    if direction == "right":
        return WEMAI_BOT_NICKNAME or "self"
    if chat_type == "private":
        return str(chat_name or "").strip()
    if control is None:
        return ""

    qml_sender = _sender_from_qml_message_name(control, content)
    if qml_sender:
        return qml_sender

    content_key = normalize_chat_name(content)
    explicit = []
    avatar = []
    labels = []
    for child, _depth in _walk_control_children(control, max_depth=5):
        name = _safe_control_text(child, "Name").strip()
        if not name or normalize_chat_name(name) == content_key:
            continue
        if name.casefold() in _NON_SENDER_NAMES:
            continue
        control_type = _safe_control_text(child, "ControlTypeName")
        metadata = " ".join((
            _safe_control_text(child, "AutomationId"),
            _safe_control_text(child, "ClassName"),
        )).casefold()
        if any(token in metadata for token in _SENDER_METADATA_TOKENS):
            explicit.append(name)
        elif control_type == "ButtonControl":
            avatar.append(name)
        elif control_type == "TextControl":
            labels.append(name)
    candidates = explicit or avatar or labels
    return candidates[0] if candidates else ""


def _group_sender_from_message(control, content, direction=None):
    """Return a sender only when a row exposes group-specific sender data."""
    if direction == "right" or control is None:
        return ""
    qml_sender, qml_content = _qml_message_parts(control, content)
    content_key = normalize_chat_name(content)
    qml_content_key = normalize_chat_name(qml_content)
    qml_structurally_confirmed = bool(
        qml_sender
        and qml_content_key
        and qml_content_key == content_key
    )
    for child, _depth in _walk_control_children(control, max_depth=5):
        name = _safe_control_text(child, "Name").strip()
        name_key = normalize_chat_name(name)
        if qml_sender and qml_content_key and name_key == qml_content_key:
            qml_structurally_confirmed = True
        if not name or name_key == content_key:
            continue
        if name.casefold() in _NON_SENDER_NAMES:
            continue
        metadata = " ".join((
            _safe_control_text(child, "AutomationId"),
            _safe_control_text(child, "ClassName"),
        )).casefold()
        if any(token in metadata for token in _SENDER_METADATA_TOKENS):
            return name
    if qml_structurally_confirmed:
        return qml_sender
    # Both private and group rows contain a named avatar button.  Treating an
    # avatar as group-only evidence permanently misclassifies untyped private
    # chats.  Avatar names remain useful *after* a chat is known to be a group
    # (see _message_sender), but cannot establish the type by themselves.
    return ""


def _sender_from_qml_message_name(control, content):
    """Parse ``sender: content`` exposed directly by a Qt Quick message row."""
    sender, _row_content = _qml_message_parts(control, content)
    return sender


def _qml_message_parts(control, content):
    """Return a validated ``(sender, content)`` pair from a QML row Name."""
    row_name = _safe_control_text(control, "Name").strip()
    match = _QML_SENDER_PREFIX_PATTERN.fullmatch(row_name)
    if match is None:
        return "", ""

    sender, row_content = (part.strip() for part in match.groups())
    sender_key = normalize_chat_name(sender)
    if (
        not sender_key
        or sender_key.casefold() in _NON_SENDER_NAMES
        or sender_key.casefold() in {"http", "https"}
    ):
        return "", ""

    # Callers may pass either the bubble content or the row's complete Name.
    # Require one of those exact forms so a private message such as
    # ``状态: 正常`` is not treated as group evidence through a nested label.
    content_key = normalize_chat_name(content)
    row_key = normalize_chat_name(row_name)
    body_key = normalize_chat_name(row_content)
    if content_key and content_key not in {row_key, body_key}:
        return "", ""
    return sender, row_content


def _double_click_image_control(control, **kwargs):
    double_click = getattr(control, "DoubleClick", None)
    if not callable(double_click):
        return False

    without_simulated_move = {
        key: value for key, value in kwargs.items() if key != "simulateMove"
    }
    attempts = (kwargs, without_simulated_move, {})
    for candidate in attempts:
        try:
            _call_uia_without_wait(double_click, **candidate)
            return True
        except TypeError:
            continue
        except Exception:
            break
    return False


def _double_click_image_row(control, direction):
    kwargs = {
        "x": 102 if direction == "left" else -102,
        "y": 30,
        "ratioX": 0 if direction == "left" else 1,
        "ratioY": 0,
        "simulateMove": False,
    }
    return _double_click_image_control(control, **kwargs)


def _click_image_message(control, direction, strategy=0):
    """Double-click one image bubble to open WeChat's image preview."""
    if direction not in {"left", "right"}:
        direction = "left"
    if int(strategy) % 2 and _double_click_image_row(control, direction):
        return True
    row_rect = _control_rectangle(control)
    expected_x = None
    if row_rect is not None:
        expected_x = row_rect[0] + 102 if direction == "left" else row_rect[2] - 102

    candidates = []
    direct_unnamed = _find_control(
        control,
        "ButtonControl",
        timeout=0.2,
        Name="",
    )
    if direct_unnamed is not None:
        candidates.append((direct_unnamed, 0))
    for child, depth in _walk_control_children(control, max_depth=5):
        control_type = _safe_control_text(child, "ControlTypeName")
        if control_type not in {"ImageControl", "ButtonControl"}:
            continue
        candidates.append((child, depth))

    ranked = []
    seen_controls = set()
    for child, depth in candidates:
        marker = id(child)
        if marker in seen_controls:
            continue
        seen_controls.add(marker)
        name = _safe_control_text(child, "Name")
        if (
            _normalized_media_label(name) in _IMAGE_MESSAGE_NAMES
            or _has_image_metadata(
                name,
                _safe_control_text(child, "AutomationId"),
                _safe_control_text(child, "ClassName"),
            )
        ):
            semantic_rank = 0
        elif not name.strip():
            # The original WeMai image downloader targets ButtonControl(Name='').
            semantic_rank = 1
        elif _safe_control_text(child, "ControlTypeName") == "ImageControl":
            semantic_rank = 2
        else:
            semantic_rank = 3
        rect = _control_rectangle(child)
        if rect is not None and expected_x is not None:
            center_x = (rect[0] + rect[2]) / 2
            position_rank = abs(center_x - expected_x)
        else:
            position_rank = math.inf
        ranked.append((semantic_rank, position_rank, depth, child))

    ranked.sort(key=lambda item: item[:3])
    for _semantic_rank, _position_rank, _depth, child in ranked:
        if _double_click_image_control(child, simulateMove=False):
            return True

    return _double_click_image_row(control, direction)


def _canonical_preview_action_name(value):
    name = normalize_chat_name(value).casefold().replace("…", "...")
    name = name.replace("&", "").strip()
    return re.sub(r"\([a-z]\)$", "", name).strip()


def _preview_action_name_matches(value, names):
    actual = _canonical_preview_action_name(value)
    expected = {_canonical_preview_action_name(name) for name in names}
    if actual in expected:
        return True
    save_requested = bool(
        expected.intersection({"保存", "儲存", "储存", "save"})
    )
    return save_requested and actual.startswith(
        ("保存", "儲存", "储存", "save")
    )


def _find_preview_toolbar(root):
    if _safe_control_text(root, "ClassName") == _PREVIEW_TOOLBAR_CLASS:
        return root
    toolbar = _find_control(
        root,
        "GroupControl",
        ClassName=_PREVIEW_TOOLBAR_CLASS,
    )
    if toolbar is not None:
        return toolbar
    descendants = list(_walk_control_children(root, max_depth=8))
    for control, _depth in descendants:
        if _safe_control_text(control, "ClassName") == _PREVIEW_TOOLBAR_CLASS:
            return control

    # Toolbar class names have changed between WeChat 4.x builds.  A toolbar
    # (or preview root) containing the visible Save action is the stable
    # behavior exposed to the user and is also what the original WeMai drives.
    candidates = [
        control
        for control, _depth in descendants
        if (
            _safe_control_text(control, "ControlTypeName") == "ToolBarControl"
            or "toolbar" in _safe_control_text(control, "ClassName").casefold()
        )
    ]
    candidates.append(root)
    for candidate in candidates:
        if _find_preview_button(candidate, _PREVIEW_SAVE_AS_NAMES) is not None:
            return candidate

    for control, _depth in descendants:
        if (
            _safe_control_text(control, "ControlTypeName") == "ButtonControl"
            and _preview_action_name_matches(
                _safe_control_text(control, "Name"),
                _PREVIEW_SAVE_AS_NAMES,
            )
        ):
            return root
    return None


def _find_preview_button(toolbar, names):
    """Find a preview action regardless of its wrapper position."""
    for name in names:
        button = _find_control(
            toolbar,
            "ButtonControl",
            timeout=0.2,
            Name=name,
        )
        if button is not None:
            return button

    controls = list(_walk_control_children(toolbar, max_depth=4))
    for control, _depth in controls:
        if _safe_control_text(control, "ControlTypeName") != "ButtonControl":
            continue
        if _preview_action_name_matches(
            _safe_control_text(control, "Name"),
            names,
        ):
            return control

    # wxauto 4.0 exposes some toolbar buttons through an unnamed wrapper's
    # ButtonControl() lookup even when GetChildren() does not return the button.
    for wrapper in _safe_control_children(toolbar):
        button = _find_control(wrapper, "ButtonControl", timeout=0)
        if (
            button is not None
            and _preview_action_name_matches(
                _safe_control_text(button, "Name"),
                names,
            )
        ):
            return button
    return None


def _click_uia_control(control):
    try:
        _call_uia_without_wait(control.Click, simulateMove=False)
    except TypeError:
        _call_uia_without_wait(control.Click)


def _preview_image_expired(root):
    expected = {
        normalize_chat_name(name).casefold() for name in _PREVIEW_EXPIRED_NAMES
    }
    if normalize_chat_name(_safe_control_text(root, "Name")).casefold() in expected:
        return True
    for control, _depth in _walk_control_children(root, max_depth=8):
        if normalize_chat_name(
            _safe_control_text(control, "Name")
        ).casefold() in expected:
            return True
    return False


def _preview_window_from_pid(process_id, preferred_hwnds=()):
    windows = list(_list_top_level_windows_by_pid(process_id))
    try:
        windows = _merge_window_lists(windows, _list_wechat_windows())
    except Exception:
        logger.debug("枚举微信图片预览候选窗口失败", exc_info=True)
    known = {item[0] for item in windows}
    foreground = _get_native_foreground_window()
    if foreground and foreground not in known:
        try:
            import win32gui

            windows.append(
                (
                    foreground,
                    str(win32gui.GetWindowText(foreground) or ""),
                    str(win32gui.GetClassName(foreground) or ""),
                )
            )
            known.add(foreground)
        except Exception:
            pass
    try:
        import win32gui

        expected_titles = {
            normalize_chat_name(name).casefold() for name in _PREVIEW_WINDOW_NAMES
        }

        def collect(hwnd, _extra):
            if hwnd in known:
                return True
            try:
                title = str(win32gui.GetWindowText(hwnd) or "")
                if normalize_chat_name(title).casefold() not in expected_titles:
                    return True
                windows.append(
                    (hwnd, title, str(win32gui.GetClassName(hwnd) or ""))
                )
                known.add(hwnd)
            except Exception:
                pass
            return True

        win32gui.EnumWindows(collect, None)
    except Exception:
        pass
    preferred = set(preferred_hwnds or ())
    # Prefer a window created by the click, while still supporting WeChat
    # reusing an already-created hidden preview HWND.
    windows.sort(key=lambda item: item[0] in preferred)
    for hwnd, _native_title, _native_class in windows:
        try:
            root = _control_from_handle(hwnd)
        except Exception:
            continue
        toolbar = _find_preview_toolbar(root)
        if toolbar is None:
            continue
        return hwnd, root, toolbar
    return None


def _wait_for_image_preview(process_id, existing_hwnds, timeout):
    deadline = time.monotonic() + max(float(timeout), 0.1)
    while time.monotonic() < deadline:
        preview = _preview_window_from_pid(process_id, preferred_hwnds=existing_hwnds)
        if preview is not None:
            return preview
        time.sleep(_UI_POLL_INTERVAL)
    raise TimeoutError("等待微信图片预览窗口超时")


def _find_popup_menu_item(process_id, names, timeout=2.0):
    deadline = time.monotonic() + max(float(timeout), 0.1)
    while time.monotonic() < deadline:
        windows = list(_list_top_level_windows_by_pid(process_id))
        try:
            windows = _merge_window_lists(windows, _list_wechat_windows())
        except Exception:
            logger.debug("枚举微信预览菜单候选窗口失败", exc_info=True)
        try:
            import win32gui

            extra_windows = []

            def collect(hwnd, _extra, target=extra_windows):
                try:
                    class_name = str(win32gui.GetClassName(hwnd) or "")
                    if "QWindowToolSaveBits" not in class_name:
                        return True
                    target.append(
                        (
                            hwnd,
                            str(win32gui.GetWindowText(hwnd) or ""),
                            class_name,
                        )
                    )
                except Exception:
                    pass
                return True

            win32gui.EnumWindows(collect, None)
            windows = _merge_window_lists(windows, extra_windows)
        except Exception:
            pass

        for hwnd, _title, _class_name in windows:
            try:
                root = _control_from_handle(hwnd)
            except Exception:
                continue
            menu_root = root
            if _safe_control_text(menu_root, "ClassName") != _PREVIEW_MENU_CLASS:
                menu_root = next(
                    (
                        child
                        for child, _depth in _walk_control_children(root, max_depth=4)
                        if _safe_control_text(child, "ClassName")
                        == _PREVIEW_MENU_CLASS
                    ),
                    None,
                )
            if menu_root is None:
                continue
            controls = [menu_root]
            controls.extend(_safe_control_children(menu_root))
            controls.extend(
                child
                for child, _depth in _walk_control_children(menu_root, max_depth=3)
            )
            for control in controls:
                if _safe_control_text(control, "ControlTypeName") != "MenuItemControl":
                    continue
                if _preview_action_name_matches(
                    _safe_control_text(control, "Name"),
                    names,
                ):
                    return control
        time.sleep(_UI_POLL_INTERVAL)
    return None


def _read_clipboard_file_paths():
    try:
        import win32clipboard
        import win32con
    except Exception:
        return []

    value = ()
    for attempt in range(3):
        try:
            win32clipboard.OpenClipboard()
            try:
                available = getattr(
                    win32clipboard,
                    "IsClipboardFormatAvailable",
                    None,
                )
                if callable(available) and not available(win32con.CF_HDROP):
                    return []
                value = win32clipboard.GetClipboardData(win32con.CF_HDROP)
            finally:
                win32clipboard.CloseClipboard()
            break
        except Exception:
            if attempt == 2:
                return []
            time.sleep(min(_UI_POLL_INTERVAL, 0.03))
    if isinstance(value, (str, os.PathLike)):
        value = [value]
    try:
        return [os.path.abspath(os.fspath(path)) for path in value if path]
    except TypeError:
        return []


def _invoke_preview_action(toolbar, process_id, action_names, timeout):
    action = _find_preview_button(toolbar, action_names)
    if action is not None:
        _click_uia_control(action)
        return

    more_button = _find_preview_button(toolbar, _PREVIEW_MORE_BUTTON_NAMES)
    if more_button is None:
        raise RuntimeError("图片预览工具栏中未找到“更多”按钮")
    _click_uia_control(more_button)
    menu_item = _find_popup_menu_item(
        process_id,
        action_names,
        timeout=max(0.1, float(timeout)),
    )
    if menu_item is None:
        action_label = "/".join(action_names)
        raise RuntimeError(f"图片预览菜单中未找到操作: {action_label}")
    _click_uia_control(menu_item)


def _copy_image_from_preview(toolbar, process_id, timeout):
    with _CLIPBOARD_LOCK:
        return _copy_image_from_preview_unlocked(toolbar, process_id, timeout)


def _copy_image_from_preview_unlocked(toolbar, process_id, timeout):
    deadline = time.monotonic() + max(float(timeout), 0.1)
    _set_clipboard_text("")
    last_error = None
    try:
        while time.monotonic() < deadline:
            preview_root = _top_level_control(toolbar)
            if preview_root is not None and _preview_image_expired(preview_root):
                raise RuntimeError("图片过期或已被清理")
            try:
                _invoke_preview_action(
                    toolbar,
                    process_id,
                    _PREVIEW_COPY_MENU_NAMES,
                    timeout=min(1.0, max(0.1, deadline - time.monotonic())),
                )
            except Exception as exc:
                last_error = exc
                time.sleep(_UI_POLL_INTERVAL)
                continue

            clipboard_deadline = min(deadline, time.monotonic() + 1.5)
            while time.monotonic() < clipboard_deadline:
                for path in _read_clipboard_file_paths():
                    if os.path.isfile(path) and os.path.getsize(path) > 0:
                        stable_deadline = min(
                            clipboard_deadline,
                            time.monotonic() + 0.4,
                        )
                        if _wait_for_stable_file(path, stable_deadline):
                            return path
                time.sleep(_UI_POLL_INTERVAL)
            last_error = RuntimeError("微信未把图片文件写入剪贴板")
        raise TimeoutError(f"复制微信图片超时: {last_error}")
    finally:
        # This fallback owns its clipboard mutation.  The primary Save flow
        # never touches the user's clipboard.
        _set_clipboard_text("")


def _is_save_dialog_title(value):
    title = normalize_chat_name(value).casefold().replace("…", "...")
    return any(
        token in title
        for token in (
            "另存为",
            "另存為",
            "另存爲",
            "保存",
            "儲存",
            "储存",
            "save",
        )
    )


def _is_save_dialog_button(value):
    name = normalize_chat_name(value).casefold()
    expected = {
        normalize_chat_name(candidate).casefold()
        for candidate in _SAVE_DIALOG_BUTTON_NAMES
    }
    return name in expected or name.startswith(("保存", "save"))


def _find_save_dialog_controls(process_id, timeout):
    """Return native (dialog, filename edit, save button) handles."""
    try:
        import win32gui
        import win32process
    except Exception:
        return None

    deadline = time.monotonic() + max(float(timeout), 0.1)
    while time.monotonic() < deadline:
        dialogs = []

        def collect_dialog(hwnd, _extra, target=dialogs):
            try:
                if not _is_save_dialog_title(win32gui.GetWindowText(hwnd)):
                    return True
                is_visible = getattr(win32gui, "IsWindowVisible", None)
                if callable(is_visible) and not is_visible(hwnd):
                    return True
                if process_id is not None:
                    _thread_id, candidate_pid = (
                        win32process.GetWindowThreadProcessId(hwnd)
                    )
                    if int(candidate_pid) != int(process_id):
                        return True
                target.append(hwnd)
            except Exception:
                pass
            return True

        try:
            win32gui.EnumWindows(collect_dialog, None)
        except Exception:
            dialogs = []

        for dialog_hwnd in dialogs:
            edits = []
            buttons = []

            def collect_child(
                hwnd,
                _extra,
                target_edits=edits,
                target_buttons=buttons,
            ):
                try:
                    class_name = str(win32gui.GetClassName(hwnd) or "")
                    text = str(win32gui.GetWindowText(hwnd) or "")
                    if class_name == "Edit":
                        target_edits.append((bool(text.strip()), hwnd))
                    elif class_name == "Button" and _is_save_dialog_button(text):
                        target_buttons.append(hwnd)
                except Exception:
                    pass
                return True

            try:
                win32gui.EnumChildWindows(dialog_hwnd, collect_child, None)
            except Exception:
                continue
            if edits and buttons:
                edits.sort(key=lambda item: item[0], reverse=True)
                return dialog_hwnd, edits[0][1], buttons[0]
        time.sleep(_UI_POLL_INTERVAL)
    return None


def _wait_for_stable_file(path, deadline):
    previous_size = -1
    stable_polls = 0
    while time.monotonic() < deadline:
        try:
            size = os.path.getsize(path)
        except OSError:
            size = 0
        if size > 0 and size == previous_size:
            stable_polls += 1
            if stable_polls >= 2:
                return True
        else:
            stable_polls = 0
        previous_size = size
        time.sleep(_UI_POLL_INTERVAL)
    return False


def _save_image_via_save_as(toolbar, process_id, timeout):
    """Click Preview's Save action and complete its native file dialog."""
    deadline = time.monotonic() + max(float(timeout), 0.1)
    preview_root = _top_level_control(toolbar)
    if preview_root is not None and _preview_image_expired(preview_root):
        raise RuntimeError("图片过期或已被清理")

    source_path = os.path.abspath(
        os.path.join(
            tempfile.gettempdir(),
            f"wemai_wechat_image_{uuid.uuid4().hex}.jpg",
        )
    )
    dialog_hwnd = None
    try:
        _invoke_preview_action(
            toolbar,
            process_id,
            _PREVIEW_SAVE_AS_NAMES,
            timeout=min(2.0, max(0.1, deadline - time.monotonic())),
        )
        remaining = deadline - time.monotonic()
        controls = _find_save_dialog_controls(process_id, remaining)
        if controls is None:
            raise TimeoutError("等待微信图片“另存为”对话框超时")
        dialog_hwnd, edit_hwnd, save_hwnd = controls

        import win32con

        _send_native_message(
            edit_hwnd,
            win32con.WM_SETTEXT,
            0,
            source_path,
        )
        _send_native_message(save_hwnd, win32con.BM_CLICK, 0, 0)
        if not _wait_for_stable_file(source_path, deadline):
            raise TimeoutError("等待微信写入另存为图片超时")
        return source_path
    except Exception:
        if dialog_hwnd is not None:
            try:
                _close_window(dialog_hwnd)
            except Exception:
                logger.debug(
                    "关闭失败的微信图片保存对话框失败 hwnd=%s",
                    dialog_hwnd,
                    exc_info=True,
                )
        with contextlib.suppress(OSError):
            os.unlink(source_path)
        raise


def _acquire_image_from_preview(toolbar, process_id, timeout):
    """Get a preview image path and whether that path needs cleanup."""
    total_timeout = max(float(timeout), 0.1)
    deadline = time.monotonic() + total_timeout
    # Clipboard copy normally completes immediately.  Keep it bounded so a
    # missing Copy action cannot consume the Save As fallback's whole budget.
    copy_timeout = min(2.0, max(0.1, total_timeout * 0.3))
    copy_error = None
    try:
        return (
            _copy_image_from_preview(toolbar, process_id, copy_timeout),
            False,
        )
    except Exception as exc:
        copy_error = exc
        logger.warning(
            "通过微信图片预览复制图片失败，改用另存为回退: %s",
            exc,
        )

    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError(f"保存微信图片超时: {copy_error}") from copy_error
    try:
        return (
            _save_image_via_save_as(toolbar, process_id, remaining),
            True,
        )
    except Exception as save_error:
        raise RuntimeError(
            "读取微信图片失败: "
            f"复制路径错误={copy_error}; 另存为回退错误={save_error}"
        ) from save_error


def _validated_image_suffix(path):
    size = os.path.getsize(path)
    if size <= 0:
        raise ValueError("微信保存的图片为空")
    if size > MAX_MEDIA_BYTES:
        raise ValueError("入站图片超过尺寸上限")
    with open(path, "rb") as stream:
        header = stream.read(16)
    suffix = None
    for magic, candidate in _IMAGE_MAGIC.items():
        if not header.startswith(magic):
            continue
        if magic == b"RIFF" and header[8:12] != b"WEBP":
            continue
        suffix = candidate
        break
    if suffix is None:
        raise ValueError("微信保存的文件不是受支持的图片")
    return suffix


def _persist_received_image(source_path):
    suffix = _validated_image_suffix(source_path)
    os.makedirs(IMAGE_SAVE_DIR, exist_ok=True)
    filename = (
        f"微信图片_{time.strftime('%Y%m%d%H%M%S')}_"
        f"{time.time_ns() % 1_000_000_000:09d}_{uuid.uuid4().hex[:8]}{suffix}"
    )
    destination = os.path.abspath(os.path.join(IMAGE_SAVE_DIR, filename))
    shutil.copyfile(source_path, destination)
    _validated_image_suffix(destination)
    return destination


def _close_preview_window(hwnd, root):
    try:
        root.SendKeys("{Esc}", waitTime=0)
        return
    except TypeError:
        try:
            root.SendKeys("{Esc}")
            return
        except Exception:
            pass
    except Exception:
        pass
    try:
        _close_window(hwnd)
    except Exception:
        logger.debug("关闭微信图片预览窗口失败 hwnd=%s", hwnd, exc_info=True)


def _control_rectangle(control):
    try:
        rect = control.BoundingRectangle
        left = int(rect.left)
        top = int(rect.top)
        right = int(rect.right)
        bottom = int(rect.bottom)
    except Exception:
        return None
    if right <= left or bottom <= top:
        return None
    return left, top, right, bottom


def _roll_control_into_view(container, control, max_attempts=30):
    """Scroll one Qt table item into view using UIA, then wheel fallback."""
    try:
        pattern = control.GetScrollItemPattern()
        if pattern is not None and pattern.ScrollIntoView(waitTime=0):
            return True
    except Exception:
        pass

    for _ in range(max_attempts):
        container_rect = _control_rectangle(container)
        control_rect = _control_rectangle(control)
        if container_rect is None or control_rect is None:
            return False
        if (
            control_rect[1] >= container_rect[1]
            and control_rect[3] <= container_rect[3]
        ):
            return True
        try:
            if control_rect[1] < container_rect[1]:
                _call_uia_without_wait(container.WheelUp)
            else:
                _call_uia_without_wait(container.WheelDown)
        except Exception:
            return False
        time.sleep(_UI_POLL_INTERVAL)
    return False


def _find_chat_input(root):
    """Find ChatMessagePage/XSplitterView/ChatInputField from WeChat 4.0."""
    if not root:
        return None

    chat_box = _find_chat_box(root)
    edit = _find_control(
        chat_box,
        "EditControl",
        ClassName=_CHAT_INPUT_CLASS,
    )
    if edit is not None:
        return edit

    for automation_id in _CHAT_INPUT_AUTOMATION_IDS:
        try:
            edit = root.EditControl(AutomationId=automation_id)
            if _control_exists(edit, _UI_LOOKUP_TIMEOUT):
                return edit
        except Exception:
            continue

    root_rect = _control_rectangle(root)
    for class_name in _CHAT_INPUT_CLASS_NAMES:
        try:
            edit = root.EditControl(ClassName=class_name)
            if not _control_exists(edit, _UI_LOOKUP_TIMEOUT):
                continue
            rect = _control_rectangle(edit)
            if root_rect is None or rect is None:
                return edit
            root_height = root_rect[3] - root_rect[1]
            if rect[1] >= root_rect[1] + root_height * 0.45:
                return edit
        except Exception:
            continue

    candidates = []
    try:
        for control, depth in _walk_controls(root, max_depth=12):
            if _safe_control_text(control, "ControlTypeName") != "EditControl":
                continue
            rect = _control_rectangle(control)
            if rect is None:
                continue
            width = rect[2] - rect[0]
            if width <= 100:
                continue
            name = _safe_control_text(control, "Name")
            if "搜索" in name:
                continue
            if root_rect is not None:
                root_height = root_rect[3] - root_rect[1]
                if rect[1] < root_rect[1] + root_height * 0.45:
                    continue
                vertical_score = rect[1] - root_rect[1]
            else:
                vertical_score = rect[1]
            class_name = _safe_control_text(control, "ClassName")
            automation_id = _safe_control_text(control, "AutomationId")
            score = width + vertical_score - depth
            if class_name in _CHAT_INPUT_CLASS_NAMES:
                score += 10000
            if automation_id in _CHAT_INPUT_AUTOMATION_IDS:
                score += 20000
            candidates.append((score, control))
    except Exception:
        logger.debug("遍历独立窗口查找聊天输入框失败", exc_info=True)
        return None

    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[0][1]


def _set_clipboard_text(content):
    deadline = time.monotonic() + 1.0
    last_error = None
    while time.monotonic() < deadline:
        try:
            import pyperclip

            pyperclip.copy(str(content))
            return True
        except Exception as exc:
            last_error = exc
            time.sleep(_UI_POLL_INTERVAL)
    logger.error("写入文本到剪贴板失败: %s", last_error)
    return False


def _read_input_value(edit):
    """Return (is_readable, value) for the Qt chat editor."""
    try:
        pattern = edit.GetValuePattern()
        return True, str(pattern.Value or "")
    except Exception:
        try:
            value = edit.Value
            return True, str(value or "")
        except Exception:
            return False, ""


def _activate_chat_input(edit):
    try:
        if getattr(edit, "HasKeyboardFocus", False):
            return True
    except Exception:
        pass
    for method_name in ("MiddleClick", "Click", "SetFocus"):
        method = getattr(edit, method_name, None)
        if not callable(method):
            continue
        try:
            _call_uia_without_wait(method)
            return True
        except Exception:
            continue
    return False


def _clear_chat_input(edit):
    if not _activate_chat_input(edit):
        return False
    try:
        edit.SendKeys("{Ctrl}a", waitTime=0)
    except TypeError:
        edit.SendKeys("{Ctrl}a")
    try:
        edit.SendKeys("{DELETE}", waitTime=0)
    except TypeError:
        edit.SendKeys("{DELETE}")
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        readable, value = _read_input_value(edit)
        if not readable or not value:
            return True
        time.sleep(_UI_POLL_INTERVAL)
    return False


def _paste_into_chat_input(edit):
    if not _activate_chat_input(edit):
        return False
    try:
        _call_uia_without_wait(edit.SendKeys, "{Ctrl}v")
        return True
    except Exception:
        logger.debug("通过 UIA 粘贴聊天内容失败", exc_info=True)
    try:
        from wx4py.features.chat import ChatWindow

        ChatWindow._send_ctrl_hotkey_static(0x56)
        return True
    except Exception:
        logger.debug("通过 Win32 粘贴聊天内容失败", exc_info=True)
        return False


def _submit_chat_input(
    edit,
    timeout=_TEXT_SEND_CONFIRM_TIMEOUT,
    before_submit=None,
):
    """Send the prepared editor content with Enter and confirm it cleared."""
    if not _activate_chat_input(edit):
        logger.error("微信 4.0 聊天输入框无法获得焦点")
        return False
    if before_submit is not None:
        before_submit()
    deadline = time.monotonic() + timeout
    try:
        _call_uia_without_wait(edit.SendKeys, "{Enter}")
    except Exception as exc:
        raise _SendOutcomeUnknown(
            "按回车时微信返回异常，消息是否送达无法确认"
        ) from exc

    while time.monotonic() < deadline:
        readable, value = _read_input_value(edit)
        if readable and not value:
            return True
        if readable and not value.replace("\ufffc", "").strip():
            return True
        time.sleep(_SEND_POLL_INTERVAL)
    raise _SendOutcomeUnknown("发送后输入框未清空，消息是否送达无法确认")


def _send_text_via_input(edit, content, before_submit=None):
    """Paste text into ChatInputField and send it with Enter."""
    content = str(content)
    if not content:
        return False
    with _CLIPBOARD_LOCK:
        if not _clear_chat_input(edit) or not _set_clipboard_text(content):
            return False

        deadline = time.monotonic() + _TEXT_PREPARE_TIMEOUT
        while time.monotonic() < deadline:
            if not _paste_into_chat_input(edit):
                return False
            readable, value = _read_input_value(edit)
            if not readable or value.replace("\ufffc", "").strip():
                break
            time.sleep(_SEND_POLL_INTERVAL)
        else:
            logger.error("粘贴微信消息超时：输入框始终为空")
            return False
    return _submit_chat_input(
        edit,
        timeout=_TEXT_SEND_CONFIRM_TIMEOUT,
        before_submit=before_submit,
    )


def _send_files_via_input(edit, paths, before_submit=None):
    """Paste files into one verified ChatInputField and send with Enter."""
    from wx4py.utils.clipboard_utils import set_files_to_clipboard

    if isinstance(paths, (str, os.PathLike)):
        paths = [paths]
    paths = [os.path.abspath(os.fspath(path)) for path in paths]
    if not paths:
        return False
    with _CLIPBOARD_LOCK:
        if not _clear_chat_input(edit):
            return False
        try:
            copied = set_files_to_clipboard(paths)
        except ValueError as exc:
            logger.error("复制文件到剪贴板失败: %s", exc)
            return False
        if not copied:
            logger.error("复制文件到剪贴板失败")
            return False

        if not _paste_into_chat_input(edit):
            return False
        deadline = time.monotonic() + _FILE_PREPARE_TIMEOUT
        while time.monotonic() < deadline:
            readable, value = _read_input_value(edit)
            if not readable or value:
                break
            time.sleep(_SEND_POLL_INTERVAL)
        else:
            logger.error("粘贴微信文件超时：输入框始终为空")
            return False
    return _submit_chat_input(
        edit,
        timeout=_FILE_SEND_CONFIRM_TIMEOUT,
        before_submit=before_submit,
    )


def _is_web_search_row(name=None, *, group=None):
    """True when a search result row is a WeChat 4.x web-search shortcut.

    The Ctrl+F popover in WeChat 4.x groups rows into category bands.
    The web-search band is labelled "搜索网络结果" (in observed Chinese
    builds), "Web Search" (English), or older aliases like "搜一搜"
    / "网页" — pressing Enter on any row in that band routes the
    user to a WeChat web search instead of the matching chat. The
    individual row names inside that band look like ordinary chat
    names ("<query> 表情包", "<query> 是什么"), so the group label is
    the only reliable identifier. We fall back to a name-prefix
    check for the rare build that publishes a web row without a
    group header.

    Callers SHOULD pass both ``name`` and ``group`` when they have
    both; the name-only form is kept for the verify-and-skip path
    that only has access to the current-chat header text.
    """
    if group is not None and normalize_chat_name(group) in _WEB_SEARCH_GROUPS:
        return True
    text = str(name or "").lstrip()
    if not text:
        return False
    return any(text.startswith(prefix) for prefix in _WEB_SEARCH_ROW_PREFIXES)


def _parse_search_result_controls(controls):
    groups = {}
    current_group = "未知"
    known_groups = {
        _SEARCH_GROUP_CONTACTS,
        _SEARCH_GROUP_CHATS,
        _SEARCH_GROUP_FUNCTIONS,
        _SEARCH_GROUP_FREQUENT,
    } | _NON_CHAT_SEARCH_GROUPS
    for control in controls:
        name = _safe_control_text(control, "Name").strip()
        if not name:
            continue
        class_name = _safe_control_text(control, "ClassName")
        automation_id = _safe_control_text(control, "AutomationId")
        normalized = normalize_chat_name(name)
        is_result = (
            "SearchContentCellView" in class_name
            or automation_id.startswith("search_item_")
            or _safe_control_text(control, "ControlTypeName")
            in {"ListItemControl", "DataItemControl"}
        )
        # WeChat 4.x has published category headers as XTableCell, TextControl
        # and generic wrapper controls in different builds.  The category text
        # is stable; recognize it before applying result-row class filters.
        if normalized in known_groups and (
            (class_name == "mmui::XTableCell" and not automation_id)
            or (normalized in _NON_CHAT_SEARCH_GROUPS and not automation_id)
            or not is_result
        ):
            current_group = normalized
            # 网络搜索和聊天记录分区都不是可打开的聊天结果。微信会把
            # “聊天记录”标题暴露为 ListItemControl，因此还需用无
            # AutomationId 的稳定标题文本识别它。
            if normalized not in _NON_CHAT_SEARCH_GROUPS:
                groups.setdefault(current_group, [])
            continue
        if "查看全部" in name:
            continue
        if not is_result:
            continue
        # Drop WeChat 4.x's "搜一搜 / 搜索:<query> 朋友圈 / 网页" rows
        # from the very top of the popover. They are not chats and
        # Enter on them routes the user to a web search instead of
        # opening the target conversation. The rows live in a
        # "搜索网络结果" group band in 4.0.5+ builds; their names are
        # ordinary chat-like text ("<query> 表情包",
        # "<query> 是什么", ...) so we MUST key off the group label
        # first, with the name-prefix check as a fallback for
        # headerless rows. See ``_is_web_search_row``.
        if current_group in _NON_CHAT_SEARCH_GROUPS:
            logger.debug(
                "忽略微信搜索结果中的非聊天行 name=%r group=%s",
                name,
                current_group,
            )
            continue
        if _is_web_search_row(name, group=current_group):
            logger.debug(
                "忽略微信搜索结果中的网页搜索行 name=%r group=%s",
                name,
                current_group,
            )
            continue
        group = (
            _SEARCH_GROUP_FUNCTIONS
            if automation_id.startswith("search_item_function")
            else current_group
        )
        groups.setdefault(group, []).append(
            _SearchUIItem(name=name, ctrl=control, group=group)
        )
    return groups


def _sleep_during_search(delay, stop_event=None):
    """Sleep in short slices so shutdown can still interrupt UI stabilization."""
    deadline = time.monotonic() + max(float(delay), 0.0)
    while True:
        _raise_if_stopped(stop_event)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(_UI_POLL_INTERVAL, remaining))


def _scroll_search_results_for_match(
    root,
    keyword,
    result_list,
    accept_results,
    duration=_SEARCH_RESULT_SCROLL_SECONDS,
    stop_event=None,
):
    """Wheel through virtualized search rows until an accepted result appears."""
    if result_list is None or accept_results is None or duration <= 0:
        return {}

    deadline = time.monotonic() + float(duration)
    latest = {}
    wheel_count = 0
    while time.monotonic() < deadline:
        _raise_if_stopped(stop_event)
        if not _control_exists_now(result_list):
            result_list = _find_search_result_list(root)
            if result_list is None:
                _sleep_during_search(_UI_POLL_INTERVAL, stop_event)
                continue
        try:
            result_list.WheelDown(waitTime=0)
            wheel_count += 1
        except Exception:
            logger.debug(
                "微信搜索结果列表滚轮下翻失败 chat=%r",
                keyword,
                exc_info=True,
            )
            break

        remaining = max(deadline - time.monotonic(), 0.0)
        _sleep_during_search(
            min(_SEARCH_RESULT_SCROLL_INTERVAL, remaining),
            stop_event,
        )
        refreshed = _find_search_result_list(root)
        if refreshed is None:
            continue
        result_list = refreshed
        try:
            candidate = _parse_search_result_controls(result_list.GetChildren())
        except Exception:
            logger.debug(
                "微信搜索结果滚动后控件读取失败 chat=%r",
                keyword,
                exc_info=True,
            )
            continue
        if not any(candidate.values()):
            continue
        latest = candidate
        if accept_results(candidate):
            logger.info(
                "微信搜索结果下翻后命中 chat=%r wheels=%d groups=%r",
                keyword,
                wheel_count,
                {group: len(items) for group, items in candidate.items()},
            )
            return candidate

    logger.info(
        "微信搜索结果下翻 %.1f 秒仍未命中 chat=%r wheels=%d",
        float(duration),
        keyword,
        wheel_count,
    )
    return latest


def _search_result_is_clickable(result_list, control):
    """Reject stale rows and rows whose click center is outside the popover list."""
    if not _control_exists_now(result_list) or not _control_exists_now(control):
        return False
    list_rect = _control_rectangle(result_list)
    control_rect = _control_rectangle(control)
    if list_rect is None or control_rect is None:
        # Some WeChat/UIA builds do not publish rectangles consistently. The
        # existence checks still protect against the common stale-control case.
        return True
    center_x = (control_rect[0] + control_rect[2]) / 2
    center_y = (control_rect[1] + control_rect[3]) / 2
    return (
        list_rect[0] <= center_x <= list_rect[2]
        and list_rect[1] <= center_y <= list_rect[3]
    )


def _search_with_wxauto40_controls(
    root,
    keyword,
    timeout=_SEARCH_TIMEOUT,
    stop_event=None,
    accept_results=None,
    main_hwnd=None,
):
    """Send the search text and return the first usable popover frame.

    搜索弹层可能分批渲染，所以这里先完整等待再做有上限的快速轮询：
    1. Send the search text (Ctrl+F + Ctrl+V, via either the
       XSearchField's UIA SendKeys or global SendKeys).
    2. 粘贴后等待 3 秒，再读取本地联系人或群聊结果。
    3. Return the parsed popover dict (with the web-search rows
       already filtered by ``_WEB_SEARCH_GROUPS``) once one is
       acceptable.
    4. Fall back to ``_search_enter_with_verify`` only when the
       popover never appeared at all — that helper still does the
       Down + Enter skip loop as a last-resort recovery.
    """
    _raise_if_stopped(stop_event)
    search_box = _find_search_box(root)

    # ---- Step 1: send the search text ----
    if search_box is None:
        # WeChat 4.0.5+ can render its QML tree without publishing XSearchField.
        # Ctrl+F focuses the native search field even when no child UIA provider
        # is available, so only the top-level HWND and global SendKeys are needed.
        if main_hwnd is None:
            return None
        if not _activate_native_window(main_hwnd):
            raise RuntimeError(f"无法激活微信主窗口进行快捷键搜索: hwnd={main_hwnd}")
        _sleep_during_search(_SEARCH_HOTKEY_ACTIVATION_DELAY, stop_event)

        try:
            with _CLIPBOARD_LOCK:
                _send_global_uia_keys("{Ctrl}f")
                _sleep_during_search(_SEARCH_HOTKEY_OPEN_DELAY, stop_event)
                if not _set_clipboard_text(keyword):
                    raise RuntimeError("无法把搜索词写入剪贴板")
                _send_global_uia_keys("{Ctrl}v")
        except (ImportError, AttributeError, OSError):
            # Keep the existing wx4py search path usable in environments where
            # its low-level UIAutomation module is unavailable.
            logger.debug("全局 UIAutomation 按键不可用，回退兼容搜索", exc_info=True)
            return None
        except RuntimeError:
            raise
        except Exception as exc:
            raise RuntimeError("无法通过 Ctrl+F 在微信主窗口输入搜索词") from exc
    else:
        # WeChat 4.0.5+ QML popover. Focus the search field, clear it,
        # then paste the keyword. The popover render happens after the
        # paste and is read in the polling loop below.
        with _CLIPBOARD_LOCK:
            if not _set_clipboard_text(keyword):
                raise RuntimeError("无法把搜索词写入剪贴板")
            try:
                _call_uia_without_wait(search_box.Click)
            except Exception:
                _call_uia_without_wait(search_box.SetFocus)
            _sleep_during_search(_SEARCH_INPUT_STEP_DELAY, stop_event)
            try:
                has_focus = bool(search_box.HasKeyboardFocus)
            except Exception:
                has_focus = None
            if has_focus is False:
                _call_uia_without_wait(search_box.SetFocus)
                _sleep_during_search(_SEARCH_INPUT_STEP_DELAY, stop_event)
            _call_uia_without_wait(search_box.SendKeys, "{Ctrl}a")
            _sleep_during_search(_SEARCH_INPUT_STEP_DELAY, stop_event)
            _call_uia_without_wait(search_box.SendKeys, "{DELETE}")
            _sleep_during_search(_SEARCH_INPUT_STEP_DELAY, stop_event)
            _call_uia_without_wait(search_box.SendKeys, "{Ctrl}v")

    # ---- Step 2: wait for the complete popover, then poll briefly ----
    _sleep_during_search(_SEARCH_HOTKEY_RESULT_DELAY, stop_event)
    # ``timeout`` 表示从粘贴到完成读取的总预算。默认同为 3 秒，因此
    # 固定等待后至少执行一次读取，不会额外再等 3 秒。
    remaining_timeout = max(
        float(timeout) - _SEARCH_HOTKEY_RESULT_DELAY,
        _UI_POLL_INTERVAL,
    )
    deadline = time.monotonic() + remaining_timeout
    latest = {}
    saw_result_list = False
    logged_disappearance = False
    while time.monotonic() < deadline:
        _raise_if_stopped(stop_event)
        result_list = _find_search_result_list(root)
        if result_list is not None:
            saw_result_list = True
            logged_disappearance = False
            try:
                candidate = _parse_search_result_controls(
                    result_list.GetChildren()
                )
            except Exception:
                # Transient enumeration failure — log and keep polling.
                # The popover may be mid-refresh between the 搜索网络结果
                # band being drawn and the 群聊 band being drawn, in
                # which case the same GetChildren call can raise a
                # COM RPC error on some Qt versions. A retry on the
                # next tick is cheaper than falling through to a
                # blind Enter that opens the web-search page.
                logger.debug(
                    "微信 4.0 搜索结果控件读取失败，将重试 keyword=%r",
                    keyword,
                    exc_info=True,
                )
                candidate = None
            if candidate is None:
                pass
            elif not _control_exists_now(result_list):
                logger.debug("微信 4.0 搜索结果列表在读取期间失效，将重新查找")
                latest = {}
            elif any(candidate.values()):
                latest = candidate
                logger.debug(
                    "微信 4.0 搜索结果已出现 keyword=%r groups=%r counts=%r",
                    keyword,
                    list(candidate),
                    {group: len(items) for group, items in candidate.items()},
                )
                if accept_results is None or accept_results(candidate):
                    logger.info(
                        "微信搜索已读取弹层结果 chat=%r groups=%r",
                        keyword,
                        {group: len(items) for group, items in candidate.items()},
                    )
                    return candidate
            else:
                latest = {}
        else:
            latest = {}
            if saw_result_list and not logged_disappearance:
                logged_disappearance = True
                logger.debug(
                    "微信 4.0 搜索结果面板在目标出现前消失 keyword=%r，将继续等待",
                    keyword,
                )
        _sleep_during_search(_UI_POLL_INTERVAL, stop_event)

    # ---- Step 3: the first viewport missed; reveal virtualized rows ----
    result_list = _find_search_result_list(root)
    scrolled = _scroll_search_results_for_match(
        root,
        keyword,
        result_list,
        accept_results,
        stop_event=stop_event,
    )
    if scrolled and (accept_results is None or accept_results(scrolled)):
        return scrolled

    # ---- Step 4: polling and bounded scrolling timed out ----
    if latest:
        # We got at least one snapshot but the caller did not accept
        # any of them. Return the latest so the caller's downstream
        # ``_find_exact_search_result`` still has *something* to
        # pick from — better than a blind Enter that lands on the
        # web-search shortcut.
        logger.debug("微信 4.0 搜索结果等待超时，返回最后一个结果快照")
        return latest
    if scrolled:
        return scrolled

    # No local result popover appeared. Never press Enter or Down here: when
    # there is no local exact match WeChat selects its network-search row.
    logger.warning(
        "WeChat local search popover unavailable after %.1fs chat=%r; "
        "Enter/Down fallback disabled",
        _SEARCH_HOTKEY_RESULT_DELAY,
        keyword,
    )
    return {}


def _search_enter_with_verify(root, search_box, keyword, stop_event):
    """Press Enter, then check whether the jumped-to chat matches the keyword.

    The Ctrl+F popover in WeChat 4.x puts one or two non-chat rows
    ("搜一搜", "搜索:<query> 朋友圈", etc.) at the very top before
    any chat rows. The first Enter therefore often lands on the
    web-search shortcut, not on the matching chat. The bot then
    thinks the search succeeded, the independent window never opens,
    and the outer retry loop fires — burning 30-60 s per send.

    ``_is_recently_active_inbound`` is the preferred mitigation when
    the chat is provably alive (it lets the verify-and-skip path
    short-circuit and we still get a fast ChatNotFoundError on
    renames), but for the genuinely-present case we still need to
    recover here: if the current chat after Enter is not the
    keyword, send Down + Enter to skip past the non-chat rows, and
    stop once the verify passes or ``_SEARCH_VERIFY_MAX_SKIPS`` is
    exhausted.

    The caller already pressed Enter (or the global Ctrl+F
    equivalent), so this function does not issue the first Enter
    itself. It always returns ``{}`` to mirror the original
    behaviour — the caller's downstream code only needs the
    side-effect (the main-window current chat), not the popover
    contents.
    """
    _raise_if_stopped(stop_event)
    logger.warning(
        "Keyboard search submission is disabled to avoid opening network "
        "results chat=%r",
        keyword,
    )
    return {}

    # Retained below for source compatibility only. This code is deliberately
    # unreachable; local results must be selected by their exact UIA control.
    target_key = normalize_chat_name(keyword)
    if not target_key:
        return {}

    def _read_current_header():
        """Return ``(label, key)`` for the current chat header.

        Reads the UIA tree exactly once per call — some test mocks
        advance their state on every call, so calling
        ``_find_current_chat_name`` twice per verify cycle would
        double-consume the state and make the test behaviour
        diverge from the real WeChat path.
        """
        try:
            label = _find_current_chat_name(root) or ""
        except Exception:
            label = ""
        return label, normalize_chat_name(label)

    def _press_enter():
        if search_box is not None:
            _call_uia_without_wait(search_box.SendKeys, "{Enter}")
        else:
            _send_global_uia_keys("{Enter}")
        _sleep_during_search(_SEARCH_HOTKEY_SUBMIT_DELAY, stop_event)

    def _press_down_and_enter():
        if search_box is not None:
            _call_uia_without_wait(search_box.SendKeys, "{Down}")
        else:
            _send_global_uia_keys("{Down}")
        _sleep_during_search(_SEARCH_INPUT_STEP_DELAY, stop_event)
        _press_enter()

    def _is_match(label, key):
        return (
            chat_names_equal(key, target_key)
            and not _is_web_search_row(name=label)
        )

    initial_label, initial_key = _read_current_header()
    if _is_match(initial_label, initial_key):
        logger.info(
            "微信搜索回车已命中目标聊天 chat=%r current_chat_key=%r",
            keyword,
            initial_key,
        )
        return {}

    logger.warning(
        "微信搜索回车未命中目标 chat=%r current_chat_key=%r，开始 Down+Enter 跳过非聊天行",
        keyword,
        initial_key,
    )
    for attempt in range(1, _SEARCH_VERIFY_MAX_SKIPS + 1):
        _press_down_and_enter()
        current_label, current_key = _read_current_header()
        if _is_match(current_label, current_key):
            logger.info(
                "微信搜索跳过 %d 个非聊天行后命中目标 chat=%r",
                attempt,
                keyword,
            )
            return {}
        if not current_key:
            # Some WeChat builds do not publish the current chat
            # header mid-transition; keep pressing Down to be sure
            # the next Enter lands on a real row, not back on the
            # first item (Down at the bottom of the list wraps to
            # top in some Qt versions).
            logger.debug(
                "微信搜索跳过过程中 current_chat 为空 attempt=%d",
                attempt,
            )
    logger.error(
        "微信搜索回车后 %d 次 Down+Enter 仍未命中目标 chat=%r，"
        "最可能的非聊天行包括：搜索网络结果 / 搜一搜 / 搜索:<query> 朋友圈 / 网页",
        _SEARCH_VERIFY_MAX_SKIPS,
        keyword,
    )
    return {}


def _clear_direct_search(root):
    # Once a result has been clicked the popover is expected to disappear. Do
    # not send Esc into the newly opened chat or close unrelated WeChat UI.
    result_list = _find_search_result_list(root)
    if result_list is None or not _control_exists_now(result_list):
        logger.debug("微信 4.0 搜索面板已关闭，跳过 Esc 清理")
        return
    search_box = _find_search_box(root)
    if search_box is None:
        return
    if (
        not _control_exists_now(result_list)
        or _find_search_result_list(root) is None
    ):
        logger.debug("微信 4.0 搜索面板在清理前关闭，跳过 Esc")
        return
    try:
        _call_uia_without_wait(search_box.SendKeys, "{Esc}")
    except Exception:
        logger.debug("关闭微信 4.0 搜索面板失败", exc_info=True)


def _submit_direct_search_with_enter(root, keyword):
    """Retired compatibility helper; keyboard search submission is unsafe."""
    logger.warning(
        "Direct search Enter submission is disabled to avoid network results "
        "chat=%r",
        keyword,
    )
    return False

    # Retained below for source compatibility only. Exact local-result controls
    # are the sole permitted search selection mechanism.
    if _find_search_result_list(root) is not None:
        return False
    search_box = _find_search_box(root)
    if search_box is None or not _control_exists_now(search_box):
        return False
    readable, value = _read_input_value(search_box)
    if not readable or value != str(keyword):
        logger.debug(
            "搜索面板消失后搜索框内容不可确认，跳过回车 chat=%r value=%r",
            keyword,
            value if readable else None,
        )
        return False
    try:
        _call_uia_without_wait(search_box.SetFocus)
        _call_uia_without_wait(search_box.SendKeys, "{Enter}")
        logger.debug("搜索面板消失后已通过回车提交精确搜索 chat=%r", keyword)
        return True
    except Exception:
        logger.debug("搜索面板消失后回车提交失败 chat=%r", keyword, exc_info=True)
        return False

# endregion ==============================================================


# region ==== UI 命令队列、工作线程与异常 ===============================
# ``UICommand`` 和 ``UICommandQueue`` 是 Router 事件循环与 UI 执行线程之间的
# 跨线程边界。异常中的 ``retry_safe`` 明确表示相同命令能否重跑：已经可能点击
# “发送”的超时绝不能自动重试，否则会产生重复消息。
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
        super().__init__(f"微信命令超时 id={command_id} retry_safe={retry_safe}")
        self.command_id = command_id
        self.retry_safe = retry_safe
        self.command_future = command_future


class _WrongChatWindow(RuntimeError):
    """双击后出现了标题不匹配的独立聊天窗口。"""


class _SubwindowOpenedButUnverified(RuntimeError):
    """已出现新顶层窗口但尚未验证，此时再次双击可能打开错误窗口。"""


class _SubwindowOpenError(RuntimeError):
    """聊天确实存在，但其独立窗口未能打开或通过身份验证。"""


@dataclass(frozen=True)
class UIWorkerStatus:
    """UI 工作线程对外公开的不可变状态快照。

    ``main`` 的看门狗只读取该快照，不跨模块窥探或修改私有活动标记。冻结的数据类
    迫使调用方每次重新取快照，使状态写入始终归 UI 模块自身管理。
    """

    command_active: bool = False
    command_started: Optional[float] = None
    recovery_active: bool = False
    recovery_started: Optional[float] = None
    media_active: bool = False
    media_started: Optional[float] = None
    reconnecting: bool = False
    connected: bool = False
    listening_target_names: tuple = ()
    wemai_ui_processor_running: bool = False
    phase: str = ""


class UICommandQueue:
    """把 Router 线程的发送请求交给微信会话工作线程执行。"""

    def __init__(self, maxsize=UI_QUEUE_SIZE):
        self._queue = queue.Queue(maxsize=maxsize)
        self._wake_event = threading.Event()

    def submit(self, action, *args, timeout=15):
        command = UICommand(action=action, args=args, timeout=float(timeout))
        # UI 所有者线程每 10ms 检查一次队列，0.1 秒已覆盖约十次调度机会。队列满
        # 表示工作线程异常积压，应快速失败，不能让三次出站重试各额外等待数秒。
        try:
            self._queue.put(command, timeout=0.1)
        except queue.Full as exc:
            raise RuntimeError("微信命令队列已满") from exc
        self._wake_event.set()
        submitted_at = time.monotonic()
        try:
            return command.future.result(timeout=timeout)
        except FutureTimeoutError as exc:
            elapsed = time.monotonic() - submitted_at
            # 即使最终成功也记录慢发送，让维护者在触发 15 秒硬超时前发现 UI 积压。
            if elapsed >= 3.0:
                logger.warning(
                    "微信命令执行缓慢 action=%s elapsed=%.2fs id=%s",
                    action, elapsed, command.command_id,
                )
            if command.future.done():
                return command.future.result()
            retry_safe = command.cancel_if_pending()
            if not retry_safe:
                try:
                    return command.future.result(timeout=max(float(timeout) * 2, 0))
                except FutureTimeoutError as started_exc:
                    if command.future.done():
                        return command.future.result()
                    raise UICommandTimeout(
                        command.command_id,
                        retry_safe=False,
                        command_future=command.future,
                    ) from started_exc
            raise UICommandTimeout(command.command_id, retry_safe) from exc

    def get_nowait(self):
        return self._queue.get_nowait()

    def task_done(self):
        self._queue.task_done()

    def wait(self, timeout):
        if self._queue.empty():
            self._wake_event.wait(timeout)
        self._wake_event.clear()


def _visible_item_semantic_key(item):
    return (
        getattr(item, "kind", ""),
        getattr(item, "class_name", ""),
        getattr(item, "name", ""),
    )


def _suffix_prefix_overlap(previous, current):
    for size in range(min(len(previous), len(current)), 0, -1):
        if previous[-size:] == current[:size]:
            return size
    return 0


def _appended_visible_items(previous, current, disjoint_is_append=False):
    """Return newly appended rows and whether the latest snapshot is anchored.

    Qt virtualizes the message list and may eventually reuse RuntimeIds.  A
    permanent set therefore drops later identical image rows and grows without
    bound.  Ordered suffix/prefix overlap detects an append even when an old ID
    is reused.  A disjoint or backward snapshot is treated as history scrolling
    and does not replace the last known bottom anchor.
    """
    previous = list(previous or ())
    current = list(current or ())
    if not current:
        return [], False
    if not previous:
        return current, True

    previous_keys = tuple(item.key for item in previous)
    current_keys = tuple(item.key for item in current)
    if previous_keys == current_keys:
        return [], True

    overlap = _suffix_prefix_overlap(previous_keys, current_keys)
    if overlap:
        return current[overlap:], True

    previous_semantic = tuple(_visible_item_semantic_key(item) for item in previous)
    current_semantic = tuple(_visible_item_semantic_key(item) for item in current)
    semantic_overlap = _suffix_prefix_overlap(previous_semantic, current_semantic)
    if semantic_overlap:
        return current[semantic_overlap:], True

    if (
        _suffix_prefix_overlap(current_keys, previous_keys)
        or _suffix_prefix_overlap(current_semantic, previous_semantic)
    ):
        return [], False
    if disjoint_is_append:
        # When the list is known to be at its bottom, a fully disjoint window
        # is not history scrolling: enough messages arrived between scans to
        # evict every old visible row.  Dropping it here loses the whole burst.
        return current, True
    return [], False


def _message_list_at_bottom(message_list):
    """Return True/False when UIA exposes scroll position, else None."""
    try:
        pattern = message_list.GetScrollPattern()
    except Exception:
        pattern = None
    if pattern is None:
        return None
    try:
        if not bool(pattern.VerticallyScrollable):
            return True
    except Exception:
        pass
    try:
        percent = float(pattern.VerticalScrollPercent)
    except (AttributeError, TypeError, ValueError):
        return None
    # UIA uses -1 for NoScroll and 100 for the end position.
    if percent < 0:
        return True
    return percent >= 99.5

# endregion ==============================================================


# region ==== Session / status / inbound dataclasses =====================
# Plain dataclasses shared between the UI worker and the send workers.
# ``_ChatSession`` tracks per-chat polling state; ``_InboundEvent`` is the
# normalized message envelope produced for the Router thread;
# ``_ListenerStatus`` is the status proxy exposed to ``main.py``.
@dataclass
class _ChatSession:
    name: str
    hwnd: int
    root: object
    message_list: object
    seen: set
    snapshot: tuple = ()
    visible_count: int = 0
    new_count: int = 0
    scan_count: int = 0
    fail_count: int = 0
    last_message_at: float = field(default_factory=time.monotonic)
    next_scan_at: float = field(default_factory=time.monotonic)
    interval: float = WEMAI_TICK
    media_failures: dict = field(default_factory=dict)
    chat_type: str = ""
    at_bottom: bool = True
    empty_read_count: int = 0
    next_identity_check_at: float = 0.0
    recovered_items: list = field(default_factory=list)
    # WeFlow 会话真 ID 不随群名改变，可在微信没有刷新独立窗口标题时完成改名重绑。
    # 尚未收到该会话入站（例如纯出站冷启动）时允许为空；反向索引保存在
    # ``WeChatListener._sessions_by_session_id``。
    session_id: str = ""
    chat_input: object = None


@dataclass
class _ChatSendWorker:
    name: str
    queue: queue.Queue
    thread: threading.Thread = None


@dataclass(frozen=True)
class _InboundEvent:
    group: str
    content: str
    timestamp: float
    raw: object = None
    message_type: str = "text"
    file_name: str = ""
    sender: str = ""
    chat_type: str = ""
    avatar_url: str = ""
    # WeFlow 群聊 SSE 顶层头像属于会话本身；与成员头像分开传递，
    # 防止两个缓存命名空间再次互相污染。
    group_avatar_url: str = ""
    # 会话级稳定身份：群聊通常为 ``...@chatroom``，私聊通常为对方微信真 ID。
    # 它用于出站 HWND 重绑和稳定名称映射；旧事件没有 sessionId 时允许为空。
    session_id: str = ""
    # 发送者级稳定身份：群聊中是成员微信真 ID（不是群 ID），私聊中等于对方
    # session_id。推送缺失时由 WeFlow 历史 senderUsername 回查，处理层据此生成
    # 跨私聊、群聊一致的 user_id。
    sender_wxid: str = ""
    user_nickname: str = ""
    group_card_name: str = ""


class _ListenerStatus:
    """提供 main.py 期望的 processor 状态接口，不创建额外 UI 线程。"""

    def __init__(self, listener):
        self._listener = listener

    @property
    def is_running(self):
        return self._listener.running

    def stop(self):
        self._listener.running = False

# endregion ==============================================================


# region ==== WeChatListener ==============================================
# 本类编排上方所有 UI 工具：持有 UIA 所有者线程、按需打开并验证独立窗口、
# 轮询窗口新消息、消费跨线程命令，并把 send 分派给每聊天工作线程。看门狗只通过
# ``status`` 获取活动状态，不直接操作 UI 内部字段。
class WeChatListener:
    """在 WeMai UI 工作线程中管理独立窗口、会话绑定和消息收发。

    安全不变量：发送前必须验证目标 HWND；同一接收方串行，不同独立窗口可并行；
    只有窗口创建、销毁和重绑需要全局生命周期锁；剪贴板锁不得覆盖等待发送确认。
    """

    def __init__(
        self,
        target_chats=None,
        callback=None,
        command_queue=None,
        stop_event=None,
        heartbeat=None,
        outgoing_registry=None,
    ):
        from wx4py import WeChatClient

        self._owner_thread = threading.get_ident()
        self.target_specs = self._normalize_targets(target_chats)
        self.target_names = [item["name"] for item in self.target_specs]
        self.listening_target_names = []
        self._open_on_demand = WX_OPEN_WINDOWS_ON_DEMAND
        self._configured_chat_types = {
            normalize_chat_name(item["name"]): item["type"]
            for item in self.target_specs
            if item.get("type") in {"private", "group"}
        }
        self.chat_types = {
            normalize_chat_name(item["name"]): item.get("type")
            for item in self.target_specs
        }
        self._chat_type_sources = {
            key: "config" for key in self._configured_chat_types
        }
        self._window_chat_types = {}
        self.callback = callback
        self.commands = command_queue or UICommandQueue()
        self.stop_event = stop_event
        self.heartbeat = heartbeat
        self.processor = None
        self.running = False
        self._closed = False
        self._command_active = False
        self._command_started = None
        self._recovery_active = False
        self._recovery_started = None
        self._media_active = False
        self._media_started = None
        self._sessions = {}
        self._chat_aliases = {}
        # Round 10: throttle for the global rename auto-rebind pass
        # in ``_rebind_renamed_cached_sessions``. The pass reads the
        # native title of every cached session's hwnd, so it must
        # not run on every call to ``_get_or_open_session`` (one
        # send per inbound message). The cached-session path's own
        # identity check already does the per-session work on a
        # 5-second TTL, so the global pass only needs to refresh
        # sessions that have never been verified since startup.
        # 30 seconds is a comfortable cadence: long enough to
        # avoid the title read on every send, short enough that a
        # rename that happened 30s ago is reflected in the next
        # send attempt without waiting for a bot restart.
        self._next_global_rename_rebind_at = 0.0
        self._failed_target_names = []
        self._next_failed_target_retry_at = 0.0
        # Names that have failed the chat-not-found check at
        # least once. The retry loop skips them so the listener
        # does not keep running the same failing open every
        # ``WEMAI_WINDOW_CHECK_INTERVAL`` seconds. Populated in
        # ``start_listening`` and ``_retry_failed_targets``; the
        # ``__init__`` initialization keeps tests that bypass
        # ``start_listening`` from hitting AttributeError.
        self._unrecoverable_target_names = set()
        # Per-target search-failure cooldown. The OUTBOUND send path
        # raises ``ChatNotFoundError`` when a chat cannot be opened
        # (renamed / deleted / never existed). After
        # ``_SEARCH_FAIL_COOLDOWN_THRESHOLD`` consecutive failures for
        # the same target, the listener stops trying to open that
        # target for ``_SEARCH_FAIL_COOLDOWN_SECONDS`` seconds —
        # otherwise the retry loop would burn the rest of the send
        # queue on a chat that is provably unreachable. Other chats
        # are unaffected. A successful open (or a stop/start cycle)
        # clears the counter and the cooldown. Locked because the
        # inbound handler and the send worker touch the same dicts
        # from different threads.
        self._search_fail_counts = {}
        self._search_cooldown_until = {}
        self._search_cooldown_lock = threading.Lock()
        self._pending_media = []
        # TTL cache of chats seen in the inbound stream (WeFlow SSE or our
        # own UI poll). Keyed by ``normalize_chat_name(chat)`` so a name
        # with different whitespace/casing resolves to the same entry.
        # The value is the ``time.monotonic()`` of the last inbound event
        # for that chat. Used by ``_is_recently_active_inbound`` to
        # decide whether the fail-fast in
        # ``_open_verified_subwindow_with_foreground`` should wait for
        # WeChat's session list to refresh (recently active → fall
        # through to search) or fail fast (stale or never seen → chat is
        # genuinely gone). ``_recent_inbound_seq`` is a monotonic
        # counter used as a tiebreaker when the OS clock resolution is
        # coarser than the per-message spacing (Windows
        # ``time.monotonic`` ticks at ~1 ms; a fast burst of 100+
        # distinct chats can land on the same millisecond). Without the
        # tiebreaker Python's stable ``sorted`` would keep the FIRST 100
        # by insertion order — i.e. the oldest, which is the opposite of
        # what the cache is for. A lock serializes the few writes so
        # ``_record_recent_inbound`` is safe to call from the WeFlow
        # SSE thread in addition to the UI owner thread.
        self._recent_inbound_chats = {}
        self._recent_inbound_seq = 0
        self._recent_inbound_lock = threading.Lock()
        # Round 11: WeFlow room-handle → chat-name map. The
        # session ID is the chat's persistent identity that
        # survives WeChat group renames, e.g. both "测试菲比群"
        # and its rename "测试1群" report the same
        # ``45808329857@chatroom`` room handle. Keyed by
        # ``normalize_chat_name(chat)`` so the same chat with
        # different whitespace/casing resolves to the same
        # entry. The value is the room handle as reported by
        # WeFlow (``str``). Written from
        # ``_record_recent_inbound`` and consumed by
        # ``_resolve_cached_session_by_session_id`` to bridge
        # a renamed chat's NEW-name outbound to its OLD-name
        # cached session.
        self._recent_inbound_sessions = {}
        # session_id -> 最新显示名称。独立窗口消失后，恢复路径必须使用
        # WeFlow 最近推送的新名称打开窗口，不能继续搜索改名前的旧名称。
        self._latest_inbound_name_by_session_id = {}
        # Round 11: session ID → cached session. Populated
        # when a cached session is created (the listener
        # looks up the session ID from
        # ``_recent_inbound_sessions`` by the chat name the
        # session was opened for) and when a session is
        # rebound to a new chat name. Consumed by
        # ``_resolve_cached_session_by_session_id`` to find
        # the cached session for a chat whose NEW name has
        # no cache entry but whose WeFlow session ID
        # matches a still-live session. Values are the
        # ``_ChatSession`` objects themselves (not hwnds or
        # names) so the rebind path can mutate
        # ``session.name`` and re-key the session in
        # ``_sessions`` in one pass.
        self._sessions_by_session_id = {}
        # Round 13: hwnd → session ID. Reverse of the
        # session_id → session map above. Used by the
        # title-check relaxation in
        # ``_require_matching_window_title_relaxed``: when
        # an open-path or pre-send title check needs to
        # verify that a hwnd is the right WeChat window,
        # the listener first looks up the hwnd's session
        # ID and if it's set (bound via a WeFlow inbound),
        # trusts the hwnd regardless of its title. WeChat
        # 4.0's independent chat window does NOT
        # auto-update its title on group rename, so a
        # title-only check would reject a hwnd that is in
        # fact the right chat for the renamed group.
        self._hwnd_to_session_id = {}
        self._subwindow_title_refreshes = WEMAI_SUBWINDOW_TITLE_REFRESHES
        self._window_lifecycle_lock = threading.RLock()
        # Windows has one foreground window. Serialize focus-dependent UIA
        # sequences so a send worker cannot dismiss the main search popover by
        # activating an independent chat midway through search and click.
        self._foreground_ui_lock = threading.RLock()
        self._send_workers_lock = threading.RLock()
        self._chat_send_locks = {}
        self._send_workers = {}
        self._send_shutdown = threading.Event()
        self._active_send_workers = 0
        self._next_update_dialog_scan_at = 0.0
        self._next_update_dialog_full_scan_at = 0.0
        self._update_dialog_first_seen = {}
        self._update_dialog_last_click_at = {}
        self._known_wechat_top_level_hwnds = set()

        self.wx = WeChatClient(auto_connect=False)
        window = self.wx.window
        startup_activate = getattr(window, "_activate_hwnd", None)
        if callable(startup_activate):
            # 上游 connect() 会在窗口不可见时读取并点击系统托盘。WeMai
            # 启动时只绑定已存在的微信窗口，不检查、不展开也不点击托盘。
            window._activate_hwnd = lambda _hwnd: True
        try:
            connected = self.wx.connect()
        finally:
            if callable(startup_activate):
                try:
                    del window._activate_hwnd
                except AttributeError:
                    window._activate_hwnd = startup_activate
        if connected is not True:
            raise RuntimeError("WeMai 微信客户端连接失败")
        if not self.wx.is_connected:
            raise RuntimeError("WeMai 未连接到微信")
        self.outgoing_registry = (
            outgoing_registry
            if outgoing_registry is not None
            else getattr(self.wx, "outgoing_registry", None)
        )
        self._connected = True
        logger.info("WeMai 微信客户端连接成功")

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

    def _assert_owner_thread(self):
        if threading.get_ident() != self._owner_thread:
            raise RuntimeError("微信窗口操作只能在专用工作线程执行")

    def _raise_if_stopping(self):
        _raise_if_stopped(self.stop_event)

    def _resolved_chat_name(self, chat_name):
        return self._chat_aliases.get(normalize_chat_name(chat_name), chat_name)

    @staticmethod
    def _window_chat_type_key(window_title):
        title = normalize_chat_name(window_title)
        return normalize_chat_name(_DYNAMIC_TITLE_SUFFIX.sub("", title))

    def _configured_chat_type_for(self, chat_name):
        key = normalize_chat_name(chat_name)
        configured = self._configured_chat_types.get(key)
        if configured:
            return configured
        resolved_key = normalize_chat_name(self._resolved_chat_name(chat_name))
        return self._configured_chat_types.get(resolved_key)

    def _resolved_chat_type(self, chat_name, window_title=None):
        """Resolve type using explicit config, aliases, and window-title cache."""
        configured = self._configured_chat_type_for(chat_name)
        if configured:
            return configured

        key = normalize_chat_name(chat_name)
        cached = self.chat_types.get(key)
        if cached in {"private", "group"}:
            return cached
        resolved_key = normalize_chat_name(self._resolved_chat_name(chat_name))
        cached = self.chat_types.get(resolved_key)
        if cached in {"private", "group"}:
            return cached
        title_key = self._window_chat_type_key(window_title)
        cached = self._window_chat_types.get(title_key)
        return cached if cached in {"private", "group"} else None

    def _chat_type_source_for(self, chat_name, window_title=None):
        key = normalize_chat_name(chat_name)
        if self._configured_chat_type_for(chat_name):
            return "config"
        source = self._chat_type_sources.get(key)
        if source:
            return source
        resolved_key = normalize_chat_name(self._resolved_chat_name(chat_name))
        source = self._chat_type_sources.get(resolved_key)
        if source:
            return source
        title_key = self._window_chat_type_key(window_title)
        return self._chat_type_sources.get(title_key, "unknown")

    def _cache_detected_chat_type(
        self,
        chat_name,
        detected_type,
        *,
        source,
        window_title=None,
    ):
        """Cache one detection without allowing it to override user config."""
        if detected_type not in {"private", "group"}:
            return self._resolved_chat_type(chat_name, window_title)
        if source not in _CHAT_TYPE_SOURCE_PRIORITY:
            source = "message"

        names = [chat_name, self._resolved_chat_name(chat_name), window_title]
        keys = []
        for name in names:
            key = normalize_chat_name(name)
            if key and key not in keys:
                keys.append(key)

        configured = next(
            (
                self._configured_chat_types[key]
                for key in keys
                if key in self._configured_chat_types
            ),
            None,
        )
        effective_type = configured or detected_type
        effective_source = "config" if configured else source
        if configured and configured != detected_type:
            logger.warning(
                "聊天类型自动检测与显式配置冲突，保留显式配置 "
                "chat=%r window_title=%r configured=%s detected=%s source=%s",
                chat_name,
                window_title,
                configured,
                detected_type,
                source,
            )
        new_priority = _CHAT_TYPE_SOURCE_PRIORITY[effective_source]
        for key in keys:
            explicit = self._configured_chat_types.get(key)
            if explicit:
                self.chat_types[key] = explicit
                self._chat_type_sources[key] = "config"
                continue
            old_source = self._chat_type_sources.get(key, "fallback")
            old_priority = _CHAT_TYPE_SOURCE_PRIORITY.get(old_source, -1)
            if new_priority >= old_priority:
                self.chat_types[key] = effective_type
                self._chat_type_sources[key] = effective_source

        title_key = self._window_chat_type_key(window_title)
        if title_key:
            old_source = self._chat_type_sources.get(title_key, "fallback")
            if new_priority >= _CHAT_TYPE_SOURCE_PRIORITY.get(old_source, -1):
                self._window_chat_types[title_key] = effective_type
                self._chat_type_sources[title_key] = effective_source

        logger.info(
            "缓存聊天类型 chat=%r window_title=%r detected=%s effective=%s "
            "source=%s keys=%r",
            chat_name,
            window_title,
            detected_type,
            effective_type,
            effective_source,
            keys,
        )
        return effective_type

    @staticmethod
    def _is_rename_pattern(actual_name, chat_name):
        """Return True if ``actual_name`` is a renamed/extended version of ``chat_name``.

        Heuristic: the normalized target appears as a substring of the
        normalized actual, and the actual carries a small "suffix" on
        top of the target (e.g. "测试群" → "测试群v2", "测试群(2)"). The
        suffix is capped at ``_RENAME_SUFFIX_LIMIT`` characters so a
        generic "项目群" lookup cannot accidentally match a long
        unrelated window title that happens to embed the target word.
        Exact matches return False (handled by the regular identity
        check, not as a rename).
        """
        target = normalize_chat_name(chat_name)
        actual = normalize_chat_name(actual_name)
        if not target or not actual:
            return False
        if target == actual:
            return False
        if target not in actual:
            return False
        extra = len(actual) - len(target)
        return 0 < extra <= _RENAME_SUFFIX_LIMIT

    def _find_renamed_open_window(self, chat_name, exclude_hwnd=None):
        """Find an already-open independent window whose title is a
        renamed version of ``chat_name``.

        User report: "改名后不仅无法发送，而且还有可能搜索旧名字"
        combined with "独立窗口永远不会自动关闭" — the user renames
        a group in WeChat but never closes the independent chat
        window. WeChat updates the window's title in place to the
        new name while keeping the same hwnd, so the cached session
        row points at a live window whose title now reads, for
        example, "测试群v2" while the bot's target is still the
        original "测试群". The exact-match ``_find_window_by_title``
        in the open path returns ``None`` for that case (the new
        title is a *substring match* of the target, but the UIA
        root_name/current_chat_name also report the new name, so
        ``_match_window_identity`` rejects the window as "different
        chat"). The bot then falls through to a Ctrl+F search that
        either times out or — worse — picks up a different chat
        because the old name has no exact entry in the session
        list anymore. The recover path's rename auto-rebind cannot
        help either: the alias has not been recorded yet on the
        first send after the rename (and the window is not gone, so
        the recover path's "open a new one" branch does not fire).

        This helper is a targeted second pass: it enumerates live
        WeChat windows and returns the single unambiguous
        substring-with-small-suffix match. The caller is expected to
        record the alias via ``_remember_chat_alias`` so subsequent
        lookups go through the regular exact-match path.

        Returns ``(hwnd, actual_title)`` on a unique match, otherwise
        ``(None, None)``.
        """
        target = normalize_chat_name(chat_name)
        if not target:
            return None, None
        try:
            windows = _list_wechat_windows_for_main(exclude_hwnd)
        except Exception:
            logger.debug(
                "枚举独立窗口以寻找改名匹配时失败 chat=%r",
                chat_name,
                exc_info=True,
            )
            return None, None
        matches = []
        for hwnd, window_title, class_name in windows:
            if hwnd == exclude_hwnd:
                continue
            if not window_title:
                continue
            actual = normalize_chat_name(window_title)
            if not actual or actual.casefold() in {"微信", "wechat", "weixin"}:
                continue
            if class_name and _MAIN_WINDOW_CLASS in str(class_name):
                continue
            if not self._is_rename_pattern(actual, target):
                continue
            matches.append((hwnd, window_title))
        if len(matches) == 1:
            logger.info(
                "通过子串匹配定位已打开的改名窗口 chat=%r hwnd=%s "
                "actual_title=%r",
                chat_name,
                matches[0][0],
                matches[0][1],
            )
            return matches[0]
        if len(matches) > 1:
            logger.warning(
                "多个改名窗口候选无法自动选择 chat=%r candidates=%r",
                chat_name,
                [
                    {"hwnd": hwnd, "title": title}
                    for hwnd, title in matches
                ],
            )
        return None, None

    def _remember_chat_alias(self, requested_name, actual_name):
        requested_key = normalize_chat_name(requested_name)
        actual_key = normalize_chat_name(actual_name)
        self._chat_aliases[requested_key] = actual_name
        self._chat_aliases.setdefault(actual_key, actual_name)
        configured_type = self._configured_chat_types.get(requested_key)
        if configured_type:
            self._configured_chat_types.setdefault(actual_key, configured_type)
            self.chat_types[actual_key] = configured_type
            self._chat_type_sources[actual_key] = "config"
        else:
            cached_type = self.chat_types.get(requested_key)
            if cached_type in {"private", "group"}:
                self.chat_types.setdefault(actual_key, cached_type)
                self._chat_type_sources.setdefault(
                    actual_key,
                    self._chat_type_sources.get(requested_key, "message"),
                )
        return actual_name

    def _rebind_renamed_cached_sessions(self):
        """Round 10: globally refresh cached sessions whose hwnd title
        has changed since the session was first created.

        The cached-session path inside ``_get_or_open_session`` already
        runs ``_require_matching_window_title`` which calls the rename
        auto-rebind logic — but only AFTER the cache lookup succeeds.
        The lookup is keyed by ``normalize_chat_name(session.name)``,
        so a send to the NEW name (after a rename) misses the cache
        entirely. The rename auto-rebind never runs, the open path
        takes over, the title update is still in flight, the list
        double-click finds nothing (window already open), 30s
        timeout, cooldown, and the bot is temporarily stuck
        on every rename.

        This pass runs at the start of every ``_get_or_open_session``
        call (throttled by ``_GLOBAL_RENAME_REBIND_INTERVAL_SECONDS``
        to avoid reading every hwnd's title on every send). For each
        cached session whose hwnd's current title differs from
        ``session.name``, the pass:

        1. Reads the current native title.
        2. Updates ``session.name`` to the new title.
        3. Re-keys ``self._sessions`` so the NEW name is the primary
           key. The OLD key is also kept as a fallback (a concurrent
           lookup of the OLD name still hits the same session).
        4. Records the alias via ``_remember_chat_alias`` so the
           alias map and the chat-type cache stay in sync.
        5. Refreshes ``next_identity_check_at`` so the cached-session
           path's throttled check does not redundantly re-read the
           title we just verified.

        The pass is a no-op when all cached sessions' hwnd titles
        match their cached names — the common case where no rename
        has happened.
        """
        now = time.monotonic()
        if now < self._next_global_rename_rebind_at:
            return
        self._next_global_rename_rebind_at = (
            now + _GLOBAL_RENAME_REBIND_INTERVAL_SECONDS
        )
        # Snapshot the values to avoid mutating the dict while
        # iterating. Multiple rebinds in the same call can race on
        # the dict, so we collect (session, new_title) pairs first
        # and apply them in a second pass.
        rebinds = []
        for session in list(self._sessions.values()):
            try:
                actual_title = _get_window_title(session.hwnd)
            except Exception:
                continue
            if not actual_title:
                continue
            actual_key = normalize_chat_name(actual_title)
            if not actual_key:
                continue
            if actual_key == normalize_chat_name(session.name):
                continue
            # Round 13: skip the title-based rebind for
            # sessions that are bound to a WeFlow
            # session_id. The session_id is the
            # authoritative identity (it survives
            # WeChat group renames where the window
            # title stays at the OLD name). If we
            # allowed the title-based rebind here,
            # it would overwrite ``session.name``
            # with the hwnd's title (the OLD name
            # like "测试菲比群") and undo the
            # round 11/12 session_id-based rebind
            # that had set ``session.name`` to the
            # NEW name "测试1群" — the next cache
            # lookup by "测试1群" would then miss.
            if (
                getattr(session, "session_id", "")
                and not self._subwindow_title_refreshes
            ):
                continue
            rebinds.append((session, actual_title, actual_key))
        for session, actual_title, actual_key in rebinds:
            old_name = session.name
            old_key = normalize_chat_name(old_name)
            session.name = actual_title
            # Re-key: the OLD key still works (fallback for
            # concurrent lookups), the NEW key is now the primary.
            if (
                old_key in self._sessions
                and self._sessions[old_key] is session
            ):
                # Keep the OLD key in place as a fallback so a
                # concurrent lookup of the OLD name still hits
                # the same session. Just add the NEW key as
                # another reference.
                self._sessions.setdefault(actual_key, session)
            else:
                self._sessions.setdefault(actual_key, session)
            self._remember_chat_alias(old_name, actual_title)
            self._chat_aliases.setdefault(old_key, actual_title)
            # Round 11 + 12: re-bind the WeFlow session ID to
            # the new name. The chat is the same — only the
            # display name changed — so the session_id is
            # unchanged, but the inbound map may have already
            # recorded a NEW-name entry pointing at the same
            # ID. Re-attach so future inbound messages
            # continue to map to the session. Round 12: if
            # neither the new name nor the old name has a
            # direct session_id hit (the rebind happened
            # before round 11 recorded the inbound), the
            # ``_attach_session_id`` fallback walks the
            # inbound history for any unbound ID and binds
            # it.
            existing_sid = getattr(session, "session_id", "") or ""
            if existing_sid:
                self._recent_inbound_sessions[actual_key] = existing_sid
            if not existing_sid:
                direct_sid = ""
                # Try the new name first (most recent inbound),
                # then the old name (still in the map with TTL).
                for lookup_name in (actual_title, old_name):
                    sid = self._recent_inbound_sessions.get(
                        normalize_chat_name(lookup_name)
                    )
                    if sid:
                        direct_sid = sid
                        break
                self._attach_session_id(session, direct_sid)
            # The throttled identity check inside the cached
            # session path will see the rebind for free on its
            # next call; bump the TTL so we don't re-read the
            # title we just verified.
            session.next_identity_check_at = (
                now + _IDENTITY_CHECK_TTL_SECONDS
            )
            logger.info(
                "全局改名自动重绑：cached session 跟随窗口标题更新 "
                "old=%r new=%r hwnd=%s",
                old_name,
                actual_title,
                session.hwnd,
            )

    def _find_renamed_via_alias(self, chat_name, exclude_hwnd=None):
        """Round 10: reverse-alias recovery for the open path.

        When the user is sending to a NEW name (``chat_name``) and
        no open window matches that name, check whether the
        ``_chat_aliases`` map says the NEW name was previously
        known as some OLD name. If so, and a window with that OLD
        name is currently open, treat the OLD window as the target
        (the rename auto-rebind's reverse direction). Returns
        ``(hwnd, actual_title)`` on a unique match, otherwise
        ``(None, None)``.

        This handles the rename pattern where the window title
        hasn't been updated yet by WeChat at the moment of the
        first send attempt. Even if the title is still the OLD
        name, the alias map (populated by the cached-session path
        on a previous send, or by an earlier round 5 recover
        pass) tells the bot that "OLD name == NEW name" and the
        still-open window IS the target.

        Multiple alias candidates (e.g. the alias map points to
        different OLD names for different windows) are rejected
        with a warning — the bot refuses to guess.
        """
        target_key = normalize_chat_name(chat_name)
        if not target_key:
            return None, None
        # The target itself is its own alias, so first check the
        # alias map for a direct OLD-name resolution.
        direct_alias = self._chat_aliases.get(target_key)
        # The reverse direction: look for any other name that
        # currently maps TO the target name.
        reverse_aliases = [
            name
            for name, resolved in self._chat_aliases.items()
            if normalize_chat_name(resolved) == target_key
            and name != target_key
        ]
        candidate_old_names = set()
        if direct_alias and normalize_chat_name(direct_alias) != target_key:
            candidate_old_names.add(direct_alias)
        candidate_old_names.update(reverse_aliases)
        if not candidate_old_names:
            return None, None
        try:
            windows = _list_wechat_windows_for_main(exclude_hwnd)
        except Exception:
            logger.debug(
                "反向 alias 扫描枚举窗口失败 chat=%r",
                chat_name,
                exc_info=True,
            )
            return None, None
        matches = []
        for hwnd, window_title, class_name in windows:
            if hwnd == exclude_hwnd:
                continue
            if not window_title:
                continue
            if normalize_chat_name(window_title) in {
                "微信", "wechat", "weixin"
            }:
                continue
            if class_name and _MAIN_WINDOW_CLASS in str(class_name):
                continue
            window_key = normalize_chat_name(window_title)
            if window_key not in candidate_old_names:
                continue
            # Reject a window whose title also matches the target
            # exactly — that's the normal ``_find_window_by_title``
            # path and should have already returned. If we got
            # here, the exact match must have been filtered out
            # by UIA; let the caller retry the exact match with
            # a fresh UIA read rather than treat this as a
            # rename.
            if chat_names_equal(window_title, chat_name):
                continue
            matches.append((hwnd, window_title))
        if len(matches) == 1:
            logger.info(
                "反向 alias 命中已打开的旧名称窗口 chat=%r hwnd=%s "
                "actual_title=%r",
                chat_name,
                matches[0][0],
                matches[0][1],
            )
            return matches[0]
        if len(matches) > 1:
            logger.warning(
                "反向 alias 多个候选无法自动选择 chat=%r candidates=%r",
                chat_name,
                [
                    {"hwnd": hwnd, "title": title}
                    for hwnd, title in matches
                ],
            )
        return None, None

    def _is_rename_suspected(self, chat_name):
        """Round 10: cheap heuristic for "this failure might be a rename".

        Used by ``_open_verified_subwindow_with_foreground`` to
        decide whether to arm the per-target cooldown
        after a ``ChatNotFoundError``. Renames are transient —
        the chat is still there, just under a different name —
        so the cooldown is overkill and was the primary
        user-visible symptom ("改完群名后进入冷却").

        The heuristic is intentionally conservative: it only
        returns ``True`` when there is concrete in-memory
        evidence that the target is the renamed version of a
        cached session or alias. A "True" is a strong signal
        that the failure is NOT a permanent "chat gone" case.

        Three signals, in priority order:

        1. **Alias map direct hit**: the target name is a
           key in ``_chat_aliases`` with a different value
           (i.e. the target was previously known as another
           name). Even if the alias was recorded minutes ago,
           a send to the target that fails is suspicious.

        2. **Alias map reverse hit**: any cached session's
           name maps to the target via the alias map.

        3. **Cached-session name mismatch**: any cached
           session exists (i.e. the bot was previously
           monitoring this chat under some other name) AND
           the cached session's hwnd is still alive with a
           title that doesn't match the cached name. This is
           the "title already updated, rebind pass didn't
           catch up yet" case — a clear rename signal.

        A bare "no cached sessions at all" returns ``False``
        because the chat might genuinely be unknown to the bot.
        """
        target_key = normalize_chat_name(chat_name)
        if not target_key:
            return False
        # 1. Direct alias hit.
        direct = self._chat_aliases.get(target_key)
        if direct and normalize_chat_name(direct) != target_key:
            return True
        # 2. Reverse alias hit.
        for resolved in self._chat_aliases.values():
            if normalize_chat_name(resolved) == target_key:
                return True
        # 3. Cached session whose hwnd title has drifted from
        # its cached name. The drift is the rename signal; we
        # don't need to also check that the new title equals
        # the target — if the session is alive and the title
        # has changed, a send to the new name is in the
        # rename scenario.
        for session in list(self._sessions.values()):
            try:
                actual_title = _get_window_title(session.hwnd)
            except Exception:
                continue
            if not actual_title:
                continue
            if not chat_names_equal(actual_title, session.name):
                return True
        # 4. Round 11: WeFlow session ID match. The most
        # robust signal — works even when WeChat leaves the
        # independent window's title stuck at the OLD name.
        # If the inbound map has a session ID for the target
        # and that ID is bound to a still-live cached
        # session whose name is NOT the target, the failure
        # is almost certainly a rename.
        target_session_id = self._recent_inbound_sessions.get(
            target_key
        )
        if target_session_id:
            bound_session = self._sessions_by_session_id.get(
                target_session_id
            )
            if (
                bound_session is not None
                and not chat_names_equal(bound_session.name, chat_name)
            ):
                return True
        return False

    def _fast_rebind_for_target(self, chat_name):
        """Round 10: targeted fast-rebind for one target before the cache lookup.

        Runs at the start of every ``_get_or_open_session`` call.
        Cheap (one Win32 ``GetWindowText`` per relevant session).
        Complements the throttled global pass in
        ``_rebind_renamed_cached_sessions``:

        1. **Alias map hit on the target itself**: if
           ``_chat_aliases[target]`` is a different name, the
           target was previously known as that name. Find the
           cached session whose name is the old name and read
           its hwnd's title. If the title is now the target
           name, the rename has been published by WeChat and
           the session is ready to rebind — do it now, even if
           the global pass is in its 1-second throttle.

        2. **Reverse-alias hit**: if any cached session's
           ``name`` is in the alias map pointing to the
           target, the same logic applies: check the hwnd's
           title; if it's the target name, rebind.

        Both paths share the rebind bookkeeping
        (``_remember_chat_alias`` + re-key + TTL refresh) so
        the cache lookup below the next line finds the session
        by the NEW name.
        """
        target_key = normalize_chat_name(chat_name)
        if not target_key:
            return
        # 1. Direct alias lookup: the target was previously known
        # as some other name. Find that session.
        direct_old = self._chat_aliases.get(target_key)
        candidate_sessions = []
        if direct_old:
            direct_old_key = normalize_chat_name(direct_old)
            if direct_old_key and direct_old_key != target_key:
                for session in self._sessions.values():
                    if normalize_chat_name(session.name) == direct_old_key:
                        candidate_sessions.append((direct_old, session))
                        break
        # 2. Reverse-alias: any cached session whose name
        # currently maps to the target.
        for session in list(self._sessions.values()):
            session_key = normalize_chat_name(session.name)
            if not session_key or session_key == target_key:
                continue
            if (
                self._chat_aliases.get(session_key)
                and normalize_chat_name(
                    self._chat_aliases[session_key]
                ) == target_key
            ):
                # Avoid duplicating the session we already
                # found via the direct-alias path.
                if any(s is session for _, s in candidate_sessions):
                    continue
                candidate_sessions.append((session.name, session))
        if not candidate_sessions:
            return
        for old_name, session in candidate_sessions:
            try:
                actual_title = _get_window_title(session.hwnd)
            except Exception:
                continue
            if not actual_title:
                continue
            if not chat_names_equal(actual_title, chat_name):
                continue
            old_key = normalize_chat_name(old_name)
            new_key = normalize_chat_name(actual_title)
            if old_key == new_key:
                continue
            session.name = actual_title
            self._remember_chat_alias(old_name, actual_title)
            self._chat_aliases.setdefault(old_key, actual_title)
            self._sessions.setdefault(new_key, session)
            session.next_identity_check_at = (
                time.monotonic() + _IDENTITY_CHECK_TTL_SECONDS
            )
            logger.info(
                "快速改名自动重绑：cached session 跟随窗口标题更新 "
                "old=%r new=%r hwnd=%s",
                old_name,
                actual_title,
                session.hwnd,
            )

    def _resolve_cached_session_by_session_id(self, chat_name):
        """Round 11: bridge a renamed chat's NEW name to its OLD-name
        cached session via the WeFlow room handle.

        Real WeChat 4.0 does NOT auto-update an independent chat
        window's title when a group is renamed — the title stays
        stuck at the OLD name. That breaks every title-based
        rebind path (round 10's global pass, fast-rebind,
        reverse-alias) because they all key on the title having
        changed. The only signal that survives the rename is the
        WeFlow room handle (e.g. ``45808329857@chatroom``) which
        is the chat's persistent identity and is the same before
        and after the rename.

        This method:

        1. Looks up the WeFlow session ID for the inbound
           ``chat_name`` (``_recent_inbound_sessions[chat_name]``).
           This is the ID WeFlow just reported for the renamed
           chat.
        2. Looks up the cached session bound to that session ID
           (``_sessions_by_session_id[session_id]``).
        3. If found, and the cached session's ``name`` differs
           from the target, this is a rename: update
           ``session.name`` to the target, re-key the session in
           ``_sessions``, update the alias map, and return the
           session so the cache lookup below the next line hits.

        The result is that a chat renamed in WeChat — even when
        WeChat leaves the window title stuck at the OLD name —
        resolves through the cache on the very first send to the
        NEW name. No 30-second wait and no search cooldown.

        Round 15 / 方案一: this method only updates
        ``session.name`` (the chat label). It NEVER changes
        ``session.hwnd`` — the hwnd is the WeChat-operation
        identity and stays bound to the session_id across
        the rename. The hwnd-title binding is only refreshed
        on the two natural triggers: the independent window
        being closed (handled by
        ``_evict_session_bindings`` in
        ``_get_or_open_session`` and the recover path), or
        the adapter being manually stopped (``close()``).
        No active window manipulation, no UI flicker, no
        unread message loss.

        Returns the rebound ``_ChatSession`` or ``None``.
        """
        target_key = normalize_chat_name(chat_name)
        if not target_key:
            return None
        session_id = self._recent_inbound_sessions.get(target_key)
        if not session_id:
            return None
        session = self._sessions_by_session_id.get(session_id)
        if session is None:
            # The independent window may have been opened before the first
            # WeFlow event (for example after a WeMai restart). In that
            # case the cached session has an HWND but no session_id yet.
            # Bind it using the exact display-name history that WeFlow has
            # associated with this stable ID. This is deliberately limited
            # to an unbound cached session and a unique candidate; never
            # guess between multiple windows with the same name.
            candidates = []
            recent_names = [
                name
                for name, sid in self._recent_inbound_sessions.items()
                if sid == session_id
            ]
            for name in recent_names:
                candidate = self._sessions.get(normalize_chat_name(name))
                if candidate is None:
                    continue
                candidate_sid = str(
                    getattr(candidate, "session_id", "") or ""
                ).strip()
                if candidate_sid and candidate_sid != session_id:
                    continue
                if all(existing is not candidate for existing in candidates):
                    candidates.append(candidate)
            if len(candidates) == 1:
                session = candidates[0]
                self._attach_session_id(session, session_id)
                logger.info(
                    "已将已有独立窗口按微信会话ID 绑定 "
                    "chat=%r session_id=%s hwnd=%s",
                    session.name,
                    session_id,
                    session.hwnd,
                )
            elif len(candidates) > 1:
                logger.warning(
                    "同一微信会话ID 对应多个缓存窗口，拒绝猜测 "
                    "session_id=%s candidates=%s",
                    session_id,
                    [
                        {"chat": item.name, "hwnd": item.hwnd}
                        for item in candidates
                    ],
                )
        if session is None:
            return None
        # Avoid touching the same-name case (no rebind needed).
        if chat_names_equal(session.name, chat_name):
            return session
        old_name = session.name
        old_key = normalize_chat_name(old_name)
        new_key = target_key
        # Re-key and update aliases. The session's hwnd is
        # still the right window — WeChat doesn't move chats
        # between windows on rename, it just leaves the title
        # stuck at the OLD name. The bot trusts the hwnd as
        # the persistent identity.
        session.name = chat_name
        if (
            old_key in self._sessions
            and self._sessions[old_key] is session
        ):
            # Keep the OLD key in place as a fallback for
            # concurrent lookups of the OLD name. Add the
            # NEW key as another reference.
            self._sessions.setdefault(new_key, session)
        else:
            self._sessions.setdefault(new_key, session)
        self._remember_chat_alias(old_name, chat_name)
        self._chat_aliases.setdefault(old_key, chat_name)
        session.next_identity_check_at = (
            time.monotonic() + _IDENTITY_CHECK_TTL_SECONDS
        )
        # The session_id is unchanged (the chat is the same,
        # only the display name changed). The
        # ``_sessions_by_session_id`` mapping does not need
        # an update.
        logger.info(
            "微信会话ID 改名自动重绑：cached session 跟随 chat 名更新 "
            "old=%r new=%r session_id=%s hwnd=%s",
            old_name,
            chat_name,
            session_id,
            session.hwnd,
        )
        return session

    def _attach_session_id(self, session, session_id=""):
        """Bind an explicitly resolved WeFlow session ID to a cached session.

        The caller must resolve the ID from the same chat name that produced
        the verified HWND. Guessing from a global "most recent unbound ID"
        can associate one chat's native ID with another chat's window when
        several conversations are active concurrently.
        """
        sid = str(session_id or "").strip()
        if not sid:
            return
        # Drop the previous binding if the session already
        # had a different session_id (e.g. a rebind to a chat
        # that itself has multiple WeFlow IDs across a bot
        # restart — vanishingly rare in practice but cheap to
        # handle).
        previous = getattr(session, "session_id", "") or ""
        if (
            previous
            and previous != sid
            and self._sessions_by_session_id.get(previous) is session
        ):
            self._sessions_by_session_id.pop(previous, None)
        # Round 19: the old hwnd → session_id cleanup
        # block that used to live here was a no-op (it
        # keyed on ``previous`` — a session_id string —
        # against ``_hwnd_to_session_id`` which is keyed
        # by hwnd int, so the ``in`` check was always
        # false). The proper hwnd cleanup lives in
        # ``_evict_session_bindings`` (round 15), which
        # uses the session's recorded ``old_hwnd`` and
        # guards against racing with a concurrent
        # rebind. No cleanup is needed at attach time:
        # the line below re-writes
        # ``_hwnd_to_session_id[session.hwnd] = sid``
        # for the same hwnd, which is idempotent.
        session.session_id = sid
        self._sessions_by_session_id[sid] = session
        # Round 13: write the hwnd → session_id reverse
        # map so the relaxed title check can verify a
        # hwnd by its session ID. The binding survives
        # WeChat group renames (the hwnd stays the same;
        # only the title and the chat name change).
        self._hwnd_to_session_id[session.hwnd] = sid
        logger.info(
            "微信会话ID 已绑定到 cached session: "
            "session_id=%s chat=%r hwnd=%s",
            sid,
            session.name,
            session.hwnd,
        )

    @property
    def is_running(self):
        return self.running

    @property
    def status(self):
        """Return a frozen snapshot of externally observable UI state.

        The watchdog in :mod:`main` consumes this on each tick. Snapshotting
        here means a caller's view is internally consistent: the activity
        flags, timestamps, and connection state are read under the same
        GIL scheduling slice. Callers must not cache the returned object.
        """
        processor = self.processor
        return UIWorkerStatus(
            command_active=self._command_active,
            command_started=self._command_started,
            recovery_active=self._recovery_active,
            recovery_started=self._recovery_started,
            media_active=self._media_active,
            media_started=self._media_started,
            # WeChatListener does not currently track reconnect state
            # separately; surface False until a reconnect path lands.
            reconnecting=False,
            connected=bool(self._connected),
            listening_target_names=tuple(self.listening_target_names),
            wemai_ui_processor_running=bool(
                getattr(processor, "is_running", False)
            ),
        )

    def start_listening(self):
        """打开配置会话的独立窗口，并以当前可见消息建立去重基线。"""
        self._assert_owner_thread()
        if self.running:
            return self.processor

        self.running = True
        if self.callback is None:
            logger.info(
                "微信 UIA 入站监控已禁用（本地数据库入站不受影响）；"
                "仍将打开并监控目标独立窗口"
            )

        # 按需打开模式：不主动打开窗口，仅准备发送通道；窗口在发送时按需打开，
        # 打开后由 _monitor_session_windows 持续检测。
        if self._open_on_demand:
            self.listening_target_names = []
            self._failed_target_names = []
            self.processor = _ListenerStatus(self)
            logger.info(
                "按需打开窗口模式已启动；窗口将在发送消息时按需打开并保持检测 "
                "targets=%s",
                self.target_names,
            )
            self._touch_heartbeat()
            return self.processor

        if not self.target_names:
            # Empty WX_TARGET_CHATS: the inbound path is fully handled by
            # the WeFlow SSE listener (which accepts every chat by
            # default). The UIA owner thread has no per-chat sessions to
            # open, so it returns a no-op processor and just stays
            # around to handle outbound send commands on demand.
            self.processor = _ListenerStatus(self)
            logger.info(
                "未配置 WX_TARGET_CHATS：入站消息由本地数据库监听，"
                "UI 线程仅在需要发送时按目标打开独立窗口"
            )
            self._touch_heartbeat()
            return self.processor

        effective_target_names = list(self.target_names)

        untyped_targets = [
            item["name"]
            for item in self.target_specs
            if item.get("type") not in {"private", "group"}
        ]
        if untyped_targets:
            logger.warning(
                "以下监听目标未在 WX_TARGET_CHATS 中显式指定 type，将先按"
                "窗口/消息结构推断，仅在会话列表找不到时使用搜索分类，"
                "无法判断时回退为 group: %s；"
                "可使用 JSON 配置，例如 "
                "[{\"name\":\"张总\",\"type\":\"private\"}]",
                untyped_targets,
            )

        failures = []
        unrecoverable = []
        self.listening_target_names = []
        self._failed_target_names = []
        # ``_unrecoverable_target_names`` is initialized in
        # ``__init__``; reset it here so a second ``start_listening``
        # call (e.g. after a configuration reload) re-evaluates
        # every target.
        self._unrecoverable_target_names = set()
        total_targets = len(effective_target_names)
        cancelled = False
        for index, target in enumerate(effective_target_names, 1):
            try:
                self._raise_if_stopping()
            except _UIOperationCancelled:
                cancelled = True
                break
            started_at = time.monotonic()
            self._touch_heartbeat()
            logger.info(
                "正在初始化监听目标 chat=%s progress=%d/%d",
                target,
                index,
                total_targets,
            )
            try:
                session = self._get_or_open_session(target)
            except _UIOperationCancelled:
                cancelled = True
                break
            except ChatNotFoundError as exc:
                # Permanent failure: the chat is not in WeChat's
                # session list (renamed, deleted, never existed).
                # Adding it to ``_failed_target_names`` would make
                # ``_retry_failed_targets`` re-run the same failing
                # open every ``WEMAI_WINDOW_CHECK_INTERVAL`` seconds
                # for the lifetime of the listener. Track it
                # separately so the retry loop skips it.
                unrecoverable.append((target, exc))
                self._unrecoverable_target_names.add(
                    normalize_chat_name(target)
                )
                logger.error(
                    "监听目标已不存在，跳过并不再重试 chat=%s error=%s",
                    target,
                    exc,
                )
                continue
            except Exception as exc:
                failures.append((target, exc))
                logger.warning(
                    "无法打开监听目标，已跳过 chat=%s error=%s",
                    target,
                    exc,
                )
                continue
            self.listening_target_names.append(target)
            self._touch_heartbeat()
            logger.info(
                "监听目标初始化完成 chat=%s resolved_chat=%s chat_type=%s "
                "type_source=%s progress=%d/%d elapsed=%.2fs",
                target,
                session.name,
                session.chat_type,
                self._chat_type_source_for(session.name),
                index,
                total_targets,
                time.monotonic() - started_at,
            )

        if cancelled:
            self.running = False
            logger.info(
                "监听目标初始化已由停止请求中断 opened=%d/%d",
                len(self.listening_target_names),
                total_targets,
            )
            self._touch_heartbeat()
            return None

        if failures:
            self._failed_target_names = [name for name, _exc in failures]
            self._next_failed_target_retry_at = (
                time.monotonic() + WEMAI_WINDOW_CHECK_INTERVAL
            )
            logger.warning(
                "监听目标初始化存在失败项，程序将继续运行并稍后重试 "
                "opened=%s failed=%s unrecoverable=%s",
                self.listening_target_names,
                self._failed_target_names,
                sorted(self._unrecoverable_target_names),
            )
        elif unrecoverable:
            logger.warning(
                "监听目标初始化中部分目标已不存在（不再重试） "
                "unrecoverable=%s",
                sorted(self._unrecoverable_target_names),
            )

        self.processor = _ListenerStatus(self)
        logger.info("微信独立窗口监听已启动 chats=%s", self.listening_target_names)
        self._touch_heartbeat()
        return self.processor

    def ensure_main_window_visible(self):
        """启动时恢复微信主窗口；独立聊天窗口仍保持按需打开。"""
        self._assert_owner_thread()
        hwnd, _root = self._activate_main_window()
        logger.info(
            "适配器启动已恢复微信主窗口 hwnd=%s；独立窗口继续按需打开",
            hwnd,
        )
        return hwnd

    def stop_listening(self):
        self.running = False

    def process_commands(self, limit=1):
        """Collect due inbound rows, then execute up to ``limit`` UI commands.

        ``limit=0`` skips the outbound drain entirely (used by tests that want
        to advance session polling without consuming commands). ``limit>=1``
        processes at most one command per call: the UI worker ticks this
        method on a fixed cadence and processing more would lengthen the tick
        and risk starving the heartbeat watchdog.
        """
        self._assert_owner_thread()
        if self.running:
            self._check_wechat_update_dialog()
            self._retry_failed_targets()
            if self.callback is not None:
                self._poll_due_sessions()
            else:
                self._monitor_session_windows()
        if limit <= 0:
            return
        self._drain_commands()

    def _check_wechat_update_dialog(self):
        """Dismiss a verified WeChat update dialog after a five-second grace."""
        if not WEMAI_AUTO_DISMISS_UPDATE:
            return
        now = time.monotonic()
        if now < self._next_update_dialog_scan_at:
            return
        self._next_update_dialog_scan_at = (
            now + _UPDATE_DIALOG_DISCOVERY_SCAN_SECONDS
        )
        full_scan_due = now >= self._next_update_dialog_full_scan_at

        # A search/send worker may currently own the foreground. Do not let an
        # update-popup click interrupt that focus-sensitive sequence.
        if not self._foreground_ui_lock.acquire(blocking=False):
            self._next_update_dialog_scan_at = min(
                self._next_update_dialog_scan_at,
                now + _UI_POLL_INTERVAL,
            )
            return
        try:
            if full_scan_due:
                self._next_update_dialog_full_scan_at = (
                    now + WEMAI_UPDATE_DIALOG_SCAN_INTERVAL
                )
            main_hwnd = getattr(getattr(self.wx, "window", None), "hwnd", 0)
            windows = _list_wechat_windows_for_main(main_hwnd)
            current_hwnds = {hwnd for hwnd, _title, _class_name in windows if hwnd}
            new_hwnds = current_hwnds - self._known_wechat_top_level_hwnds
            self._known_wechat_top_level_hwnds = current_hwnds
            matched_hwnds = set()
            for hwnd, title, _class_name in windows:
                if not hwnd or hwnd == main_hwnd:
                    continue
                if (
                    not full_scan_due
                    and hwnd not in new_hwnds
                    and hwnd not in self._update_dialog_first_seen
                    and not _native_title_may_be_update_dialog(title)
                ):
                    continue
                try:
                    root = _control_from_handle(hwnd)
                    button = _find_update_dialog_dismiss_button(
                        root,
                        native_title=title,
                    )
                except Exception:
                    logger.debug(
                        "检查微信更新弹窗控件失败 hwnd=%s title=%r",
                        hwnd,
                        title,
                        exc_info=True,
                    )
                    continue
                if button is None:
                    continue

                matched_hwnds.add(hwnd)
                first_seen = self._update_dialog_first_seen.setdefault(hwnd, now)
                if first_seen == now:
                    logger.info(
                        "检测到微信更新弹窗，%.1f 秒后将忽略本次更新 "
                        "hwnd=%s title=%r",
                        WEMAI_UPDATE_DISMISS_DELAY,
                        hwnd,
                        title,
                    )
                if now - first_seen < WEMAI_UPDATE_DISMISS_DELAY:
                    self._next_update_dialog_scan_at = min(
                        self._next_update_dialog_scan_at,
                        first_seen + WEMAI_UPDATE_DISMISS_DELAY,
                    )
                    continue
                last_click = self._update_dialog_last_click_at.get(hwnd, 0.0)
                if now - last_click < _UPDATE_DIALOG_CLICK_RETRY_SECONDS:
                    self._next_update_dialog_scan_at = min(
                        self._next_update_dialog_scan_at,
                        last_click + _UPDATE_DIALOG_CLICK_RETRY_SECONDS,
                    )
                    continue

                if not _activate_native_window(hwnd):
                    logger.warning(
                        "微信更新弹窗无法取得前台焦点，将在下次扫描重试 "
                        "hwnd=%s",
                        hwnd,
                    )
                    continue

                # Re-resolve after activation. Qt can rebuild the accessibility
                # tree while the dialog animates or changes foreground state.
                root = _control_from_handle(hwnd)
                button = _find_update_dialog_dismiss_button(
                    root,
                    native_title=_get_window_title(hwnd) or title,
                )
                if button is None:
                    continue
                if not _physical_click_control(button):
                    _call_uia_without_wait(button.Click, simulateMove=True)
                self._update_dialog_last_click_at[hwnd] = now
                self._next_update_dialog_scan_at = min(
                    self._next_update_dialog_scan_at,
                    now + _UPDATE_DIALOG_CLICK_RETRY_SECONDS,
                )
                self._touch_heartbeat()
                logger.info(
                    "已向微信更新弹窗提交“忽略本次更新”物理点击 "
                    "hwnd=%s waited=%.1fs",
                    hwnd,
                    now - first_seen,
                )

            stale_hwnds = set(self._update_dialog_first_seen) - matched_hwnds
            for hwnd in stale_hwnds:
                self._update_dialog_first_seen.pop(hwnd, None)
                self._update_dialog_last_click_at.pop(hwnd, None)
        except Exception:
            logger.warning("微信更新弹窗自动处理失败，将在下次扫描重试", exc_info=True)
        finally:
            self._foreground_ui_lock.release()

    def _retry_failed_targets(self):
        """Retry startup targets independently without stopping the listener."""
        if (
            not self._failed_target_names
            or time.monotonic() < self._next_failed_target_retry_at
        ):
            return

        pending = [
            name
            for name in self._failed_target_names
            if normalize_chat_name(name) not in self._unrecoverable_target_names
        ]
        self._next_failed_target_retry_at = (
            time.monotonic() + WEMAI_WINDOW_CHECK_INTERVAL
        )
        for target in pending:
            try:
                self._raise_if_stopping()
                session = self._get_or_open_session(target)
            except _UIOperationCancelled:
                return
            except ChatNotFoundError as exc:
                # Promote a previously-retryable failure to
                # permanent on its first retry, matching the
                # startup path. Without this promotion the loop
                # would keep hitting the same fail-fast every
                # ``WEMAI_WINDOW_CHECK_INTERVAL`` seconds.
                self._unrecoverable_target_names.add(
                    normalize_chat_name(target)
                )
                self._failed_target_names.remove(target)
                logger.error(
                    "监听目标在重试时确认已存在，不再重试 chat=%s error=%s",
                    target,
                    exc,
                )
                continue
            except Exception as exc:
                logger.warning(
                    "重试打开监听目标失败，将保留后续重试 chat=%s error=%s",
                    target,
                    exc,
                )
                continue

            self._failed_target_names.remove(target)
            if target not in self.listening_target_names:
                self.listening_target_names.append(target)
            logger.info(
                "重试打开监听目标成功 chat=%s resolved_chat=%s chat_type=%s",
                target,
                session.name,
                session.chat_type,
            )
            self._touch_heartbeat()

    def _monitor_session_windows(self):
        now = time.monotonic()
        sessions = {id(session): session for session in self._sessions.values()}
        for session in sessions.values():
            if now < session.next_identity_check_at:
                continue
            try:
                # Round 13: relaxed title check. The
                # monitor loop runs every
                # ``WEMAI_WINDOW_CHECK_INTERVAL`` for
                # every live session. When the session
                # is bound to a WeFlow session_id the
                # title check would otherwise evict the
                # session on every tick because WeChat
                # doesn't update the title on group
                # rename, then re-open a new window,
                # which is wasteful. The relaxed check
                # trusts the session_id binding and
                # just updates the TTL.
                self._require_matching_window_title_relaxed(
                    session.hwnd, session.name, session=session,
                )
                session.next_identity_check_at = now + WEMAI_WINDOW_CHECK_INTERVAL
            except Exception:
                logger.warning(
                    "独立聊天窗口状态检查失败，准备恢复 chat=%s hwnd=%s",
                    session.name,
                    session.hwnd,
                    exc_info=True,
                )
                self._recover_session(session)

    def _drain_commands(self):
        if self.stop_event and self.stop_event.is_set():
            return
        try:
            command = self.commands.get_nowait()
        except queue.Empty:
            return
        if command.action == "send":
            try:
                self._enqueue_send_command(command)
            except BaseException as exc:
                if not command.future.done():
                    command.future.set_exception(exc)
                self.commands.task_done()
            return
        try:
            if not command.begin():
                return
            if command.action == "stop":
                self.stop_listening()
                command.future.set_result(True)
            elif command.action == "resolve_window_handle":
                receiver = str(command.args[0]).strip()
                if not receiver:
                    raise ValueError("发送目标不能为空")
                with self._window_lifecycle_lock:
                    session = self._get_or_open_session(receiver)
                hwnd = int(session.hwnd)
                if hwnd <= 0:
                    raise RuntimeError(
                        f"独立聊天窗口句柄无效 chat={receiver} hwnd={hwnd}"
                    )
                command.future.set_result(hwnd)
            else:
                raise ValueError(f"未知微信命令: {command.action}")
        except BaseException as exc:
            if not command.future.done():
                command.future.set_exception(exc)
        finally:
            self._touch_heartbeat()
            self.commands.task_done()

    def _enqueue_send_command(self, command):
        receiver = str(command.args[0])
        key = normalize_chat_name(receiver)
        if not key:
            raise ValueError("发送目标不能为空")
        with self._send_workers_lock:
            if self._send_shutdown.is_set():
                raise RuntimeError("微信发送工作线程已停止")
            worker = self._send_workers.get(key)
            if worker is None or not worker.thread.is_alive():
                send_queue = queue.Queue(maxsize=UI_QUEUE_SIZE)
                worker = _ChatSendWorker(name=receiver, queue=send_queue)
                worker.thread = threading.Thread(
                    target=self._send_worker_loop,
                    args=(key, worker),
                    name=f"wechat-send-{len(self._send_workers) + 1}",
                    daemon=True,
                )
                self._send_workers[key] = worker
                worker.thread.start()
        try:
            # Round 24 (B6): see UICommandQueue.submit — 0.1 s
            # is enough headroom for the per-receiver worker
            # to pick up the next command. The old 2 s added
            # unnecessary latency to every outbound enqueue.
            worker.queue.put(command, timeout=0.1)
        except queue.Full as exc:
            raise RuntimeError(f"聊天发送队列已满: {receiver}") from exc

    def _send_worker_loop(self, key, worker):
        uia = None
        uia_initialized = False
        try:
            try:
                from wx4py.core import uiautomation as uia

                uia.InitializeUIAutomationInCurrentThread()
                uia_initialized = True
            except (ImportError, AttributeError):
                # Non-Windows tests provide only the lightweight wx4py facade.
                uia = None

            while True:
                try:
                    command = worker.queue.get(
                        timeout=UI_WORKER_IDLE_TIMEOUT_SECONDS
                    )
                except queue.Empty:
                    # 聊天长期没有出站消息时回收该线程；下一次发送会
                    # 按需创建新 worker，避免每个历史聊天永久占用 UIA 线程。
                    with self._send_workers_lock:
                        if (
                            self._send_workers.get(key) is worker
                            and worker.queue.empty()
                        ):
                            self._send_workers.pop(key, None)
                    return
                try:
                    if command is None:
                        return
                    if not command.begin():
                        continue
                    with self._send_workers_lock:
                        self._active_send_workers += 1
                        self._command_active = True
                        if self._command_started is None:
                            self._command_started = time.monotonic()
                    self._touch_heartbeat()
                    result = self._send_from_worker(*command.args)
                    if not command.future.done():
                        command.future.set_result(result)
                except BaseException as exc:
                    if command is not None and not command.future.done():
                        command.future.set_exception(exc)
                finally:
                    if command is not None:
                        with self._send_workers_lock:
                            if command.started:
                                self._active_send_workers = max(
                                    0,
                                    self._active_send_workers - 1,
                                )
                            self._command_active = self._active_send_workers > 0
                            if not self._command_active:
                                self._command_started = None
                        self.commands.task_done()
                    worker.queue.task_done()
                    self._touch_heartbeat()
        finally:
            if uia_initialized:
                try:
                    uia.UninitializeUIAutomationInCurrentThread()
                except Exception:
                    logger.debug("释放发送线程 UIAutomation 失败", exc_info=True)
            with self._send_workers_lock:
                if self._send_workers.get(key) is worker:
                    self._send_workers.pop(key, None)

    def _activate_main_window(self):
        """绑定并激活已存在的微信主窗口，不读取或操作系统托盘。"""
        window = self.wx.window
        previous_hwnd = window.hwnd
        actual_main_hwnd = _find_actual_main_window(previous_hwnd)
        if actual_main_hwnd and actual_main_hwnd != previous_hwnd:
            wrapper = window.uia
            if hasattr(window, "_hwnd"):
                window._hwnd = actual_main_hwnd
            else:
                try:
                    window.hwnd = actual_main_hwnd
                except Exception as exc:
                    raise RuntimeError(
                        "WeMai 绑定了独立窗口且无法改绑到微信主窗口"
                    ) from exc
            bind = getattr(wrapper, "bind", None)
            if not callable(bind):
                raise RuntimeError("WeMai 绑定了错误窗口且 UIAutomation 不支持重绑")
            bind(actual_main_hwnd)
            previous_hwnd = actual_main_hwnd
            logger.warning(
                "WeMai 初始 HWND 不是微信主窗口，已按 mmui::MainWindow 改绑 "
                "hwnd=%s",
                actual_main_hwnd,
            )
        hwnd = window.hwnd
        if not _activate_native_window(hwnd):
            raise RuntimeError(f"激活微信主窗口失败: hwnd={hwnd}")

        wrapper = window.uia
        root = wrapper.root
        if hwnd != previous_hwnd or not _control_exists_now(root):
            bind = getattr(wrapper, "bind", None)
            if not callable(bind):
                raise RuntimeError("微信主窗口句柄变化后无法重新绑定 UIAutomation")
            bind(hwnd)
            root = wrapper.root
        if not root or not _control_exists_now(root):
            raise RuntimeError("微信主窗口 UIAutomation 根控件不可用")
        root_class = _safe_control_text(root, "ClassName")
        structure_score = _main_window_structure_score(root)
        if root_class and not structure_score:
            if root_class == _SUB_WINDOW_CLASS:
                raise RuntimeError(
                    f"WeMai 当前绑定的是独立聊天窗口，不是微信主窗口: "
                    f"hwnd={hwnd} class={root_class!r}"
                )
            raise RuntimeError(
                f"微信主窗口 UI 类型错误且无主窗口结构: "
                f"expected={_MAIN_WINDOW_CLASS!r} actual={root_class!r}"
            )
        if root_class and root_class != _MAIN_WINDOW_CLASS:
            logger.debug(
                "微信主窗口使用非标准 UIA 根类，已通过主窗口结构验证 "
                "hwnd=%s class=%r",
                hwnd,
                root_class,
            )
        return hwnd, root

    def _get_or_open_session(self, chat_name):
        self._raise_if_stopping()
        # Round 10: refresh cached sessions whose hwnd title has
        # changed since they were created. Without this pass, a
        # send to the NEW name (after a rename) misses the cache
        # because the cache is keyed by the OLD name, falls through
        # to the open path, and is exposed to all the rename-edge-
        # case failure modes (race with WeChat's title update, list
        # double-click into a still-open window, timeout and search
        # cooldown). With this
        # pass, a rename is reflected in the cache within
        # ``_GLOBAL_RENAME_REBIND_INTERVAL_SECONDS`` (30s) and
        # subsequent lookups by EITHER the old or the new name
        # hit the cached session directly. Throttled internally
        # so the title read runs at most once per
        # ``_GLOBAL_RENAME_REBIND_INTERVAL_SECONDS`` (1s) across
        # all calls to this method.
        self._rebind_renamed_cached_sessions()
        # Round 10: targeted fast-rebind for the specific target.
        # The global pass handles "any cached session's hwnd
        # changed title since last check", but on the very first
        # send after a rename the bot wants a guarantee that THIS
        # target's cache is up to date NOW — not "within 1s".
        # Look up the alias map and, if the target has a known
        # OLD name, check the corresponding hwnd's title. If the
        # title is the NEW name, rebind immediately. This is a
        # constant-time operation (1 Win32 call) so it adds no
        # meaningful overhead even on the hot send path.
        self._fast_rebind_for_target(chat_name)
        # Round 11: WeFlow session ID rebind. Runs LAST among
        # the rebind paths because it depends on the inbound
        # map (``_recent_inbound_sessions``) which is only
        # populated by the WeFlow SSE thread. If the user just
        # sent a message to the renamed chat, the inbound map
        # already has the target's NEW name → the same WeFlow
        # session ID as the still-live cached session. This
        # path is the ONLY one that works when WeChat leaves
        # the independent window's title stuck at the OLD
        # name (round 10's title-based rebind all fail in
        # that case — the user's rename cooldown bug). The
        # rebind is in-place: the session's hwnd stays, the
        # name updates, the cache re-keys, and the lookup
        # below the next line finds the session.
        session = self._resolve_cached_session_by_session_id(chat_name)
        key = normalize_chat_name(chat_name)
        if session is None:
            session = self._sessions.get(key)
        if session is None:
            resolved_name = self._resolved_chat_name(chat_name)
            session = next(
                (
                    candidate
                    for candidate in self._sessions.values()
                    if chat_names_equal(candidate.name, resolved_name)
                ),
                None,
            )
            if session is not None:
                self._sessions[key] = session
        if session is not None:
            # The expensive UIA title check is throttled per session:
            # if the window was verified within the last
            # ``_IDENTITY_CHECK_TTL_SECONDS``, skip the re-check. The
            # next paste or send will fail fast if the title has
            # actually drifted, and the recover path will re-verify.
            now = time.monotonic()
            if now >= session.next_identity_check_at:
                # User report: "名称B的独立窗口不在前台焦点，就卡在
                # 这里了" — the cached hwnd's window may be iconic
                # (minimized to the taskbar) but still alive. UIA
                # identity checks on a minimized window can raise
                # transient exceptions that would evict the cached
                # session and force a re-open from scratch, leaving
                # the user staring at a non-foreground window. Restoring
                # the window BEFORE the check keeps the session alive
                # and brings the window back to the foreground as a
                # side effect.
                if self._is_window_minimized(session.hwnd):
                    _restore_native_window(session.hwnd)
                    time.sleep(_UI_POLL_INTERVAL)
                try:
                    # Round 13: relaxed title check. When
                    # the session is bound to a WeFlow
                    # session_id (via the inbound history
                    # + ``_attach_session_id``), the
                    # session_id is the authoritative
                    # identity and the title check is
                    # skipped — WeChat 4.0's independent
                    # chat window does NOT auto-update
                    # its title on group rename, so a
                    # strict title check would reject the
                    # hwnd because the window's title
                    # stays at the OLD name while
                    # ``session.name`` was rebound to the
                    # NEW name by the round 11/12
                    # ``_resolve_cached_session_by_session_id``
                    # path. Without this relaxation the
                    # bot evicts the session on every
                    # cache hit after a rename and the
                    # open path opens a duplicate window.
                    actual_title = (
                        self._require_matching_window_title_relaxed(
                            session.hwnd,
                            session.name,
                            session=session,
                        )
                    )
                    session.next_identity_check_at = (
                        now + _IDENTITY_CHECK_TTL_SECONDS
                    )
                    # User report: "已经打开的群聊独立窗口，修改群名称
                    # 会导致这个群聊无法正确发送" — the user renames
                    # the group in WeChat; the cached ``session.name``
                    # is now the OLD name while the window title is
                    # the NEW name. The title check used to accept the
                    # substring match (e.g. "测试群" inside "测试群v2")
                    # and the listener happily kept sending to the
                    # cached session whose UIA element references were
                    # no longer stable, which manifested as "send goes
                    # nowhere" on the bot side. When the title check
                    # passes but the actual title is a *different*
                    # name (the strong-match case is score 3, the
                    # dynamic-suffix case is score 2, anything weaker
                    # is a rename that must be picked up), we update
                    # the cached ``session.name`` AND record the
                    # ``old → new`` alias so a future lookup of the
                    # OLD name resolves to the NEW name without
                    # burning another search. The session stays
                    # alive — the hwnd did not change, just its label.
                    if (
                        actual_title
                        and not chat_names_equal(actual_title, session.name)
                        and not getattr(session, "session_id", "")
                    ):
                        # Round 13: don't overwrite
                        # ``session.name`` with the
                        # hwnd's title when the session
                        # is bound to a WeFlow
                        # session_id. WeChat 4.0's
                        # independent chat window
                        # does NOT auto-update on
                        # group rename — the title
                        # stays at the OLD name while
                        # ``session.name`` is the NEW
                        # name (from the round 11/12
                        # ``_resolve_cached_session_by_session_id``
                        # rebind). Overwriting
                        # ``session.name`` here would
                        # break the next cache lookup
                        # by the NEW name. The
                        # session_id is the
                        # authoritative identity; the
                        # hwnd's title is just a
                        # stale display label.
                        old_name = session.name
                        session.name = actual_title
                        self._remember_chat_alias(old_name, actual_title)
                        # 新名称成为主查询键，旧键继续作为并发回退。
                        old_key = normalize_chat_name(old_name)
                        new_key = normalize_chat_name(actual_title)
                        if new_key != old_key:
                            self._sessions[new_key] = session
                        self._chat_aliases.setdefault(old_key, actual_title)
                        logger.info(
                            "检测到群聊改名，已更新 session 缓存与 alias "
                            "old=%r new=%r hwnd=%s",
                            old_name,
                            actual_title,
                            session.hwnd,
                        )
                except Exception as exc:
                    logger.warning(
                        "缓存的独立聊天窗口已失效，准备重新打开 chat=%s hwnd=%s error=%s",
                        session.name,
                        session.hwnd,
                        exc,
                    )
                    # Round 15 / 方案一: the
                    # cached hwnd is gone (the
                    # user closed the independent
                    # window, or WeChat's idle
                    # timer reaped it). Drop the
                    # session from ALL three maps
                    # so the next access goes
                    # through the open path and
                    # acquires a fresh hwnd (with
                    # the current chat name as
                    # its title). The hwnd-title
                    # binding is updated
                    # naturally — no active
                    # window manipulation.
                    self._evict_session_bindings(
                        session, reason="hwnd_invalid_in_get_or_open"
                    )
                    session = None
            else:
                return session
        if session is not None:
            return session

        main_hwnd = self.wx.window.hwnd
        try:
            hwnd = self._open_verified_subwindow(chat_name, main_hwnd)
        except ChatNotFoundError:
            # The fail-fast raised before any session was created —
            # the chat is permanently gone (renamed to an unrelated
            # name, deleted, or never existed in WeChat's session
            # list). Re-raise so the caller (per-receiver send
            # worker, ``process_commands``, ``start_listening``, etc.)
            # can record the failure and stop retrying the same name
            # within the same send attempt.
            raise

        resolved_name = self._resolved_chat_name(chat_name)

        self._raise_if_stopping()
        root = _control_from_handle(hwnd)
        message_list = _require_subwindow_message_list(
            root,
            chat_name,
            hwnd=hwnd,
        )
        baseline = _initial_session_baseline(message_list, self.callback)
        detected_chat_type = _chat_type_from_header(root)
        chat_key = normalize_chat_name(resolved_name)
        try:
            window_title = _get_window_title(hwnd)
        except Exception:
            window_title = resolved_name
        if detected_chat_type:
            self._cache_detected_chat_type(
                resolved_name,
                detected_chat_type,
                source="header",
                window_title=window_title,
            )
        chat_type = self._resolved_chat_type(
            resolved_name,
            window_title=window_title,
        )
        if chat_type not in {"private", "group"}:
            chat_type = self._cache_detected_chat_type(
                resolved_name,
                "group",
                source="fallback",
                window_title=window_title,
            )
        else:
            # Bind search/config evidence to the native independent-window
            # title as well. This covers result display names that differ from
            # the eventual title (for example searches by WeChat ID).
            self._cache_detected_chat_type(
                resolved_name,
                chat_type,
                source=self._chat_type_source_for(resolved_name, window_title),
                window_title=window_title,
            )
        session = _ChatSession(
            name=resolved_name,
            hwnd=hwnd,
            root=root,
            message_list=message_list,
            seen={item.key for item in baseline},
            snapshot=tuple(baseline),
            visible_count=len(baseline),
            chat_type=chat_type,
            at_bottom=_message_list_at_bottom(message_list) is not False,
        )
        # Bind the WeFlow session ID to the new
        # session so a future rename can rebind via
        # ``_resolve_cached_session_by_session_id`` even when
        # WeChat leaves the window title stuck at the OLD name.
        # The session ID comes from the recent inbound map
        # keyed by the chat name the user used to open the
        # session. ``resolved_name`` is the alias-resolved
        # name; try both the original ``chat_name`` and the
        # resolved name because the open path's
        # ``_open_main_chat_from_exact_search`` rewrites
        # ``operation_name`` to the search result's actual
        # name, which may differ from the original target.
        direct_sid = ""
        for lookup_name in (chat_name, resolved_name):
            sid = self._recent_inbound_sessions.get(
                normalize_chat_name(lookup_name)
            )
            if sid:
                direct_sid = sid
                break
        self._attach_session_id(session, direct_sid)
        self._sessions[key] = session
        self._sessions.setdefault(chat_key, session)
        logger.info(
            "独立聊天窗口已就绪 chat=%s window_title=%r hwnd=%s "
            "baseline=%d chat_type=%s type_source=%s",
            chat_name,
            window_title,
            hwnd,
            len(baseline),
            chat_type,
            self._chat_type_source_for(resolved_name, window_title),
        )
        return session

    def _open_verified_subwindow(self, chat_name, main_hwnd, attempts=_SEARCH_OPEN_ATTEMPTS):
        with self._foreground_ui_lock:
            return self._open_verified_subwindow_with_foreground(
                chat_name,
                main_hwnd,
                attempts=attempts,
            )

    def _open_verified_subwindow_with_foreground(
        self,
        chat_name,
        main_hwnd,
        attempts=_SEARCH_OPEN_ATTEMPTS,
    ):
        # Per-target search-failure cooldown. After
        # ``_SEARCH_FAIL_COOLDOWN_THRESHOLD`` consecutive failures
        # the chat is muted for ``_SEARCH_FAIL_COOLDOWN_SECONDS``
        # so the outbound retry loop does not burn the rest of the
        # send queue on a chat that has been renamed/deleted or
        # whose WeFlow name no longer matches anything in WeChat.
        # Other chats continue to be processed normally.
        #
        # Round 10 carve-out: rename scenarios do NOT arm the
        # cooldown. The bot's silence after a rename was the
        # primary user complaint. A
        # rename is not "target gone" — the chat is still there,
        # just under a different name. The next send attempt (a
        # few seconds later, after WeChat has finished updating
        # the window title and the global/fast rebind passes have
        # caught up) will succeed. The cooldown is intended for
        # "this target is permanently unreachable" cases; renames
        # are transient, so they re-enter the open path on the
        # next retry instead of being muted.
        rename_suspected = self._is_rename_suspected(chat_name)
        if self._is_in_search_cooldown(chat_name):
            if rename_suspected:
                # Force-clear the cooldown for rename scenarios.
                # This is a one-shot relief — the next
                # non-rename failure will let the cooldown
                # re-arm naturally.
                self._clear_search_failure(chat_name)
                logger.info(
                    "目标处于搜索冷却中，但检测到改名嫌疑，"
                    "清零冷却立即重试 chat=%r",
                    chat_name,
                )
            else:
                logger.info(
                    "目标处于搜索冷却中，跳过打开 chat=%r",
                    chat_name,
                )
                raise ChatNotFoundError(
                    f"目标处于搜索冷却中: {chat_name!r}"
                )
        success = False
        try:
            result = self._do_open_verified_subwindow_with_foreground(
                chat_name, main_hwnd, attempts=attempts,
            )
            success = True
            return result
        except ChatNotFoundError:
            if rename_suspected:
                logger.info(
                    "改名嫌疑场景下打开失败，不冷却以允许下次重试 chat=%r",
                    chat_name,
                )
                # Do not record a search failure for renames —
                # the chat is still there, the bot just needs
                # another shot at the rebind passes. Raising
                # ChatNotFoundError here surfaces the failure
                # to the per-receiver send worker, which will
                # surface it to the retry / dead-letter queue.
                raise
            self._record_search_failure(chat_name)
            raise
        finally:
            if success:
                self._clear_search_failure(chat_name)

    def _do_open_verified_subwindow_with_foreground(
        self,
        chat_name,
        main_hwnd,
        attempts=_SEARCH_OPEN_ATTEMPTS,
    ):
        # Body of the original ``_open_verified_subwindow_with_foreground``,
        # factored out so the cooldown wrapper can record failures on
        # ``ChatNotFoundError`` and clear them on a successful return.
        last_error = None
        for attempt in range(1, attempts + 1):
            self._raise_if_stopping()
            # 仅兼容用户显式设置的重试间隔，默认不等待。
            if attempt > 1 and _SEARCH_INTER_ATTEMPT_PAUSE_SECONDS > 0:
                _sleep_during_search(
                    _SEARCH_INTER_ATTEMPT_PAUSE_SECONDS,
                    self.stop_event,
                )
                self._raise_if_stopping()
            operation_name = self._resolved_chat_name(chat_name)
            try:
                main_hwnd, main_root = self._activate_main_window()
            except Exception as exc:
                last_error = exc
                logger.warning(
                    "微信主窗口恢复失败 chat=%s attempt=%d/%d error=%s",
                    chat_name,
                    attempt,
                    attempts,
                    exc,
                )
                continue

            # 主窗口激活后再次检查独立窗口，已打开时不得重复点击。
            already_open = _find_window_by_title(
                operation_name,
                exclude_hwnd=main_hwnd,
            )
            if already_open and self._is_window_minimized(already_open):
                # User report: "名称B的独立窗口不在前台焦点，就卡在
                # 这里了" — the independent window is iconic
                # (minimized) but the program can still see it via the
                # enumeration. Restoring it before the title check
                # ensures the UIA tree is fully rendered so the
                # identity check below does not raise a transient
                # exception (which would otherwise evict the cached
                # session and re-open the chat from scratch, leaving
                # the user staring at a non-foreground window).
                _restore_native_window(already_open)
                # Give Qt a moment to repaint after the restore.
                time.sleep(_UI_POLL_INTERVAL)
            if not already_open:
                # The exact-match path missed. The user may have
                # renamed the group in WeChat and the cached
                # independent window is still alive with a NEW
                # title that is a *substring-with-suffix* of the
                # configured name (e.g. configured "测试群" but
                # the live window's title is now "测试群v2"). The
                # WeChat window never auto-closes per the user's
                # spec, so we need to recognize the still-open
                # window, record the alias, and use it — otherwise
                # the code falls through to a Ctrl+F search for
                # the OLD name (which finds nothing because the
                # session list also has the new name) and raises
                # ``ChatNotFoundError``, arming a per-target
                # cooldown on the renamed target. The next send
                # then short-circuits on the cooldown and the
                # bot is "stuck" on this target until the user
                # restarts the process to drop the in-memory
                # alias. ``_find_renamed_open_window`` is the
                # targeted second pass: it enumerates live
                # windows and returns the single unambiguous
                # substring-with-small-suffix candidate.
                renamed_hwnd, renamed_title = self._find_renamed_open_window(
                    operation_name, exclude_hwnd=main_hwnd,
                )
                if renamed_hwnd is not None:
                    already_open = renamed_hwnd
                    # Record the rename so future lookups hit
                    # the exact-match path. Use ``chat_name``
                    # (the user-supplied name) as the alias
                    # key — the inner function may have already
                    # resolved an older alias to ``operation_name``,
                    # and we want the new alias anchored to the
                    # original lookup target.
                    self._remember_chat_alias(chat_name, renamed_title)
                    # Update ``operation_name`` so the rest of
                    # this attempt (and any later check inside
                    # the loop) uses the new name consistently.
                    operation_name = renamed_title
                    logger.info(
                        "已通过子串匹配识别已打开的改名窗口 "
                        "chat=%s hwnd=%s new_title=%r — 跳过双击, "
                        "后续查询将通过 alias 直接命中",
                        chat_name,
                        renamed_hwnd,
                        renamed_title,
                    )
            if not already_open:
                # Round 10: reverse-alias recovery. The substring
                # match above only catches renames where the NEW
                # title is a small-suffix superset of the OLD
                # title (e.g. "测试群" → "测试群v2"). It cannot
                # help for renames where the new and old names
                # share no character prefix (e.g. "测试菲比群"
                # → "测试1群"). For those, the window title
                # either has already updated to the new name (in
                # which case ``_find_window_by_title`` above
                # should have returned it) OR is still the OLD
                # name because WeChat's title update is
                # asynchronous and our first send attempt landed
                # in the gap. The alias map (populated by the
                # cached-session path on a previous send, by the
                # round 4/5 recover, or by the substring-match
                # path on a related chat) bridges the gap: if
                # any alias resolves to the target name, look
                # for a window with that old name. This is the
                # critical safety net for the rename-cooldown
                # bug — without it, the first send after a
                # rename in the title-update gap goes to the
                # list double-click path, which finds nothing
                # (window already open), timeout and search
                # cooldown.
                alias_hwnd, alias_title = self._find_renamed_via_alias(
                    operation_name, exclude_hwnd=main_hwnd,
                )
                if alias_hwnd is not None:
                    already_open = alias_hwnd
                    # Record the rename so future lookups go
                    # through the regular path.
                    self._remember_chat_alias(chat_name, alias_title)
                    operation_name = alias_title
                    logger.info(
                        "已通过反向 alias 识别已打开的旧名称窗口 "
                        "chat=%s hwnd=%s old_title=%r — 跳过双击, "
                        "后续查询将通过 alias 直接命中",
                        chat_name,
                        alias_hwnd,
                        alias_title,
                    )
            if already_open:
                # Round 13: relaxed check. The hwnd
                # was just found by one of the open
                # path's window-by-title scans
                # (exact / substring / reverse-alias
                # / session_id). If the hwnd is bound
                # to a WeFlow session_id, the title
                # check is redundant and would
                # otherwise reject the hwnd because
                # the window's title might be the
                # OLD name (WeChat doesn't update
                # on rename) while
                # ``operation_name`` is the NEW
                # name. The session_id binding is
                # the authoritative identity.
                sid = self._hwnd_to_session_id.get(already_open)
                if sid:
                    with contextlib.suppress(Exception):
                        _get_window_title(already_open)
                else:
                    self._require_matching_window_title(
                        already_open,
                        operation_name,
                        log_success=True,
                    )
                logger.info(
                    "检测到已打开的独立聊天窗口，跳过双击 chat=%s hwnd=%s",
                    chat_name,
                    already_open,
                )
                return already_open

            # _find_session_item already resolves ChatSessionList. On the normal
            # chat page this avoids a full duplicate hierarchy lookup before
            # every target. Only switch pages and retry when the direct read
            # cannot find the row.
            item = _find_session_item(main_root, operation_name)
            if item is None:
                if not _ensure_chat_page(
                    main_root,
                    stop_event=self.stop_event,
                ):
                    last_error = RuntimeError(
                        "未找到微信 4.0 ChatMasterView/ChatSessionList"
                    )
                    logger.warning(
                        "微信主窗口会话列表不可用 chat=%s attempt=%d/%d",
                        chat_name,
                        attempt,
                        attempts,
                    )
                    continue
                item = _find_session_item(main_root, operation_name)

            if item is None:
                # The session list is virtualized: a chat that has
                # been pushed below the viewport by other messages
                # is not in the UIA subtree at all, so the direct
                # ``_find_session_item`` above returns ``None`` even
                # though the chat is provably in WeChat. Two recovery
                # paths are stacked:
                #
                # 1. **Scroll** the session list down a few pages
                #    and re-check. Pure list walk — no Ctrl+F — so
                #    there is no risk of landing on the
                #    "搜索网络结果" web-search band. This is the
                #    cheap, deterministic case for chats that exist
                #    in the list but are scrolled off.
                #
                # 2. **Search** via Ctrl+F as a last-resort
                #    fallthrough when the scroll did not find the
                #    row. WeChat's search is the only lookup that
                #    can find a chat by ``wxid_…`` / remark name /
                #    partial nickname, so it is the only path that
                #    can recover from a name mismatch (e.g. the
                #    user has the contact stored under a remark
                #    name that does not match what WeFlow reports
                #    as the sender). The popover read path uses
                #    ``_WEB_SEARCH_GROUPS`` to drop the
                #    "搜索网络结果" band at parse time, and the
                #    ``_search_enter_with_verify`` helper does a
                #    Down + Enter skip loop when the popover is
                #    not reachable — both of which were added
                #    specifically so the Ctrl+F path no longer
                #    loops on the web shortcut.
                session_list = _find_session_list(main_root)
                list_non_empty = bool(session_list) and bool(
                    list(_safe_control_children(session_list))
                )
                if list_non_empty and self._is_recently_active_inbound(
                    operation_name
                ):
                    item = _find_session_item_with_scroll(
                        main_root,
                        operation_name,
                        stop_event=self.stop_event,
                    )
                if item is None and list_non_empty:
                    # Scroll did not find the row even after
                    # walking to the bottom of the list. Two
                    # remaining explanations: the chat is gone
                    # (renamed/deleted — fail fast) or the chat
                    # is in WeChat under a different name that
                    # the strict list-walk cannot find. Try the
                    # Ctrl+F search as a recovery; only fail fast
                    # if the search also misses.
                    logger.info(
                        "滚屏未命中目标 chat=%r，尝试 Ctrl+F 搜索作为兜底",
                        operation_name,
                    )
                    try:
                        operation_name = self._open_main_chat_from_exact_search(
                            operation_name
                        )
                        if normalize_chat_name(chat_name) != normalize_chat_name(
                            operation_name
                        ):
                            self._remember_chat_alias(chat_name, operation_name)
                    except _UIOperationCancelled:
                        raise
                    except Exception as exc:
                        last_error = exc
                        logger.warning(
                            "Ctrl+F 搜索打开目标失败 chat=%s "
                            "attempt=%d/%d error=%s",
                            chat_name,
                            attempt,
                            attempts,
                            exc,
                        )
                        continue

                    # Search clicks rebuild the Qt accessibility
                    # tree. Rebind once after search, then wait
                    # briefly for the newly added row.
                    try:
                        main_hwnd, main_root = self._activate_main_window()
                    except Exception as exc:
                        last_error = exc
                        logger.warning(
                            "搜索后无法恢复微信主窗口 chat=%s "
                            "attempt=%d/%d error=%s",
                            chat_name,
                            attempt,
                            attempts,
                            exc,
                        )
                        continue
                    if not _ensure_chat_page(
                        main_root,
                        stop_event=self.stop_event,
                    ):
                        last_error = RuntimeError(
                            "搜索后微信主窗口未回到聊天导航页"
                        )
                        continue
                    time.sleep(0.5)
                    item = self._wait_for_session_item(
                        main_root,
                        operation_name,
                        timeout=3.0,
                        stop_event=self.stop_event,
                        allow_fuzzy=True,
                    )
                    if item is None:
                        # Scroll miss + search miss. The chat
                        # is genuinely gone from WeChat, the
                        # user renamed it without telling the
                        # bot, or the search popover is being
                        # uncooperative in a way we cannot recover
                        # from. Fail fast with the same diagnostic
                        # the user saw when the scroll-only
                        # implementation shipped, plus a hint
                        # about the search also having missed.
                        logger.error(
                            "会话列表(滚屏到底)与 Ctrl+F 搜索均无匹配: "
                            "%r 群名已改/被删？chat=%r "
                            "(可能已删除或重命名到完全不相关的名字)",
                            operation_name,
                            operation_name,
                        )
                        raise ChatNotFoundError(
                            f"会话列表与 Ctrl+F 搜索均无匹配: "
                            f"{operation_name!r}"
                        )

            if item is None:
                # Reached only when ``list_non_empty`` was False:
                # the session list is empty. Distinguish "we have
                # never heard of this chat" (fail fast) from "the
                # chat was just seen in inbound and the list has
                # not refreshed yet" (give the UI a brief moment).
                if not self._is_recently_active_inbound(operation_name):
                    logger.error(
                        "会话列表为空且未在最近 inbound 中见过 chat=%r "
                        "(可能已删除或从未被该实例收信)",
                        operation_name,
                    )
                    raise ChatNotFoundError(
                        f"会话列表为空且未在最近 inbound 中见过: "
                        f"{operation_name!r}"
                    )
                logger.info(
                    "会话列表为空，等待 WeChat 刷新新行 chat=%r",
                    operation_name,
                )
                item = self._wait_for_session_item(
                    main_root,
                    operation_name,
                    timeout=1.5,
                    stop_event=self.stop_event,
                )
                if item is None:
                    logger.error(
                        "会话列表持续为空，等待刷新后仍无目标 chat=%r",
                        operation_name,
                    )
                    raise ChatNotFoundError(
                        f"会话列表持续为空: {operation_name!r}"
                    )
            elif not _control_exists_now(item):
                # The direct row can become stale only if Qt refreshed while it
                # was being read. Avoid a second activation on the common path.
                item = self._wait_for_session_item(
                    main_root,
                    operation_name,
                    timeout=0.5,
                    stop_event=self.stop_event,
                )

            if item is None:
                # Defensive: every code path above either assigns
                # ``item`` or raises, so this branch is only hit
                # on an unexpected race with Qt (the row vanished
                # between the lookup and the click). Surface a
                # normal RuntimeError so the outer retry loop can
                # take a second look.
                last_error = RuntimeError(
                    f"主窗口会话项在双击前消失: {chat_name}"
                )
                logger.warning(
                    "主窗口会话项在双击前消失 chat=%s attempt=%d/%d",
                    chat_name,
                    attempt,
                    attempts,
                )
                continue

            self._raise_if_stopping()
            existing_hwnds = self._window_handles(main_hwnd)
            logger.info(
                "正在双击主窗口会话项以打开独立窗口 chat=%s attempt=%d/%d",
                chat_name,
                attempt,
                attempts,
            )
            # The item is guaranteed to be in the viewport at this
            # point: the direct lookup found it in-view, the
            # scroll-based lookup scrolled to it, or the post-
            # empty-list wait polled until the new row appeared.
            # Double-click immediately.
            if not _double_click_control(item):
                last_error = RuntimeError(f"双击会话项失败: {chat_name}")
                logger.warning(
                    "双击会话项失败 chat=%s attempt=%d/%d",
                    chat_name,
                    attempt,
                    attempts,
                )
                continue

            try:
                wait_timeout = min(
                    WEMAI_SUBWINDOW_TIMEOUT,
                    (
                        _SUBWINDOW_FIRST_WAIT_SECONDS
                        if attempt == 1 and attempts > 1
                        else _SUBWINDOW_RETRY_WAIT_SECONDS
                    ),
                )
                return self._wait_for_subwindow(
                    operation_name,
                    main_hwnd,
                    timeout=wait_timeout,
                    existing_hwnds=existing_hwnds,
                    stop_event=self.stop_event,
                )
            except _UIOperationCancelled:
                raise
            except _SubwindowOpenedButUnverified as exc:
                # A handle created by this click is still present.  Clicking
                # again can toggle/focus the same row indefinitely, which is
                # precisely the failure mode this guard prevents.
                last_error = exc
                logger.error(
                    "双击后已检测到新窗口但身份尚未就绪，停止再次点击 "
                    "chat=%s attempt=%d/%d error=%s",
                    chat_name,
                    attempt,
                    attempts,
                    exc,
                )
                break
            except Exception as exc:
                last_error = exc
                logger.warning(
                    "独立窗口打开或标题验证失败 chat=%s attempt=%d/%d error=%s",
                    chat_name,
                    attempt,
                    attempts,
                    exc,
                )

        message = (
            f"无法打开标题匹配的独立聊天窗口: {chat_name}; "
            f"last_error={last_error}"
        )
        # 只有搜索确实没有唯一目标时才进入 20 秒冷却。会话项已经找到、
        # 但双击未生效或新窗口 UIA 尚未就绪属于瞬态窗口故障；把它误报为
        # ChatNotFoundError 会让一个真实存在且刚发来消息的用户被冷却。
        if isinstance(last_error, ChatNotFoundError):
            raise ChatNotFoundError(message) from last_error
        raise _SubwindowOpenError(message) from last_error

    def _open_main_chat_from_exact_search(
        self,
        chat_name,
        allow_legacy_fallback=True,
    ):
        with self._foreground_ui_lock:
            return self._open_main_chat_from_exact_search_with_foreground(
                chat_name,
                allow_legacy_fallback=allow_legacy_fallback,
            )

    def _open_main_chat_from_exact_search_with_foreground(
        self,
        chat_name,
        allow_legacy_fallback=True,
    ):
        chat_window = getattr(self.wx, "chat_window", None)
        search = getattr(chat_window, "search", None)
        root = self.wx.window.uia.root
        using_direct_controls = True
        target_type = self._target_type_for(chat_name)

        def has_exact_result(candidate):
            return self._find_exact_search_result(
                candidate,
                chat_name,
                target_type,
                allow_contains=False,
            ) is not None

        try:
            results = _search_with_wxauto40_controls(
                root,
                chat_name,
                stop_event=self.stop_event,
                accept_results=has_exact_result,
                main_hwnd=self.wx.window.hwnd,
            )
            if results is None:
                using_direct_controls = False
                if not allow_legacy_fallback:
                    return None
                if not callable(search):
                    raise RuntimeError("微信 4.0 主搜索框不可用")
                with _CLIPBOARD_LOCK:
                    results = search(_escape_uia_send_keys_text(chat_name))
            if not results:
                raise ChatNotFoundError(
                    f"WeChat local search found no exact chat result: {chat_name}"
                )
            result = self._find_exact_search_result(
                results,
                chat_name,
                target_type,
                allow_contains=False,
            )
            if result is None:
                raise ChatNotFoundError(
                    f"搜索结果中无可唯一匹配的目标: {chat_name}"
                )
            result_list = (
                _find_search_result_list(root) if using_direct_controls else None
            )
            if using_direct_controls and (
                result_list is None or not _control_exists_now(result_list)
            ):
                raise RuntimeError(
                    f"WeChat local search panel expired before click: {chat_name}"
                )
            actual_name = self._search_result_actual_name(result, chat_name)
            clicked = self._click_search_result(
                result.ctrl,
                result_list,
                immediate=using_direct_controls,
            )
            if not clicked:
                raise RuntimeError(f"点击搜索结果失败: {chat_name}")
            result_group = self._search_group_for_result(results, result)
            detected_type = self._chat_type_from_search_group(
                result_group
            )
            if self._wait_for_main_chat_ready(actual_name) is None:
                raise RuntimeError(f"点击搜索结果后主窗口聊天未完成切换: {chat_name}")
            self._remember_chat_alias(chat_name, actual_name)
            if detected_type:
                self._cache_detected_chat_type(
                    chat_name,
                    detected_type,
                    source="search",
                    window_title=actual_name,
                )
                self._cache_detected_chat_type(
                    actual_name,
                    detected_type,
                    source="search",
                    window_title=actual_name,
                )
            logger.info(
                "已通过微信 4.0 搜索打开主窗口聊天 "
                "chat=%s target_type=%s detected_chat_type=%s group=%s "
                "name=%r direct=%s",
                chat_name,
                target_type or "auto",
                detected_type or "unknown",
                result_group,
                getattr(result, "name", ""),
                using_direct_controls,
            )
            if not using_direct_controls:
                self._clear_search_safely(chat_window)
            return actual_name
        except Exception:
            if using_direct_controls:
                _clear_direct_search(root)
            else:
                self._clear_search_safely(chat_window)
            raise

    def _wait_for_main_chat_ready(self, chat_name, timeout=_SEARCH_TIMEOUT):
        """Wait until the selected session row and main chat input both exist."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self._raise_if_stopping()
            try:
                root = self.wx.window.uia.root
                item = _find_session_item(root, chat_name)
                edit = _find_chat_input(root) if item is not None else None
                current_chat_name = _find_current_chat_name(root)
                current_matches = (
                    not current_chat_name
                    or chat_names_equal(current_chat_name, chat_name)
                )
                if (
                    item is not None
                    and edit
                    and _control_exists_now(edit)
                    and current_matches
                ):
                    return item
            except _UIOperationCancelled:
                raise
            except Exception:
                logger.debug(
                    "等待主窗口聊天切换时读取控件失败 chat=%s",
                    chat_name,
                    exc_info=True,
                )
            time.sleep(_UI_POLL_INTERVAL)
        return None

    def _target_type_for(self, chat_name):
        chat_type = self._resolved_chat_type(chat_name)
        if chat_type == "private":
            return "contact"
        if chat_type == "group":
            return "group"
        return None

    @staticmethod
    def _chat_type_from_search_group(group):
        group = normalize_chat_name(group)
        if group == _SEARCH_GROUP_CONTACTS:
            return "private"
        if group == _SEARCH_GROUP_CHATS:
            return "group"
        return None

    @staticmethod
    def _search_group_for_result(results, result):
        group = normalize_chat_name(getattr(result, "group", ""))
        if group and group != "未知":
            return group
        if isinstance(results, dict):
            for raw_group, items in results.items():
                if any(item is result for item in items or ()):
                    return normalize_chat_name(raw_group)
        return group

    @staticmethod
    def _search_result_match_kind(item, chat_name):
        target_key = normalize_chat_name(chat_name)
        name = getattr(item, "name", None)
        name_key = normalize_chat_name(name)
        if not target_key:
            return None
        lines = [
            normalize_chat_name(line)
            for line in str(name or "").splitlines()
            if normalize_chat_name(line)
        ]
        if name_key == target_key or target_key in lines:
            return "exact"
        lowered_target = target_key.casefold()
        for marker in (" 微信号: ", " 昵称: "):
            if marker not in str(name or ""):
                continue
            value = normalize_chat_name(str(name).rsplit(marker, 1)[-1])
            if value.casefold() == lowered_target:
                return "exact"
        control = getattr(item, "ctrl", None)
        if control and _control_has_exact_chat_name(
            control, chat_name, max_depth=3
        ):
            return "exact"
        if target_key in name_key:
            return "contains"
        return None

    @staticmethod
    def _search_result_name_matches(item, chat_name):
        return WeChatListener._search_result_match_kind(item, chat_name) is not None

    @staticmethod
    def _search_result_actual_name(item, query):
        text = str(getattr(item, "name", "") or "").strip()
        match = re.match(
            r"^(.*?)\s+(?:\u5fae\u4fe1\u53f7|\u6635\u79f0)\s*[:\uff1a]\s*(.*?)\s*$",
            text,
            re.DOTALL,
        )
        if match:
            display_name, identifier = match.groups()
            if normalize_chat_name(identifier).casefold() == normalize_chat_name(
                query
            ).casefold():
                lines = [line.strip() for line in display_name.splitlines() if line.strip()]
                if lines:
                    return lines[0]
        for line in text.splitlines():
            if chat_names_equal(line, query):
                return line.strip()
        return str(query)

    @staticmethod
    def _find_exact_search_result(
        results,
        chat_name,
        target_type,
        allow_contains=True,
    ):
        """兼容原方法名；跨搜索分组执行精确优先、包含兜底匹配。"""
        if not isinstance(results, dict):
            logger.info(
                "微信搜索结果格式无效 chat=%r target_type=%s type=%s",
                chat_name,
                target_type,
                type(results).__name__,
            )
            return None

        target_key = normalize_chat_name(chat_name)
        if not target_key:
            return None

        grouped_matches = {}
        result_summary = {}
        for raw_group, items in results.items():
            group = normalize_chat_name(raw_group)
            group_items = list(items or [])
            result_summary[str(raw_group)] = [
                str(getattr(item, "name", "") or "") for item in group_items
            ]
            for item in group_items:
                match_kind = WeChatListener._search_result_match_kind(
                    item,
                    chat_name,
                )
                logger.debug(
                    "搜索结果匹配判断 chat=%r target_key=%r group=%s "
                    "name=%r name_key=%r match=%s",
                    chat_name,
                    target_key,
                    group,
                    getattr(item, "name", ""),
                    normalize_chat_name(getattr(item, "name", "")),
                    match_kind or "none",
                )
                if match_kind:
                    grouped_matches.setdefault(group, {}).setdefault(
                        match_kind,
                        [],
                    ).append(item)
        logger.info(
            "微信搜索结果 chat=%r target_type=%s groups=%r",
            chat_name,
            target_type,
            result_summary,
        )

        if target_type == "contact":
            group_specs = [
                (_SEARCH_GROUP_CONTACTS, [_SEARCH_GROUP_CONTACTS]),
                (_SEARCH_GROUP_FREQUENT, [_SEARCH_GROUP_FREQUENT]),
            ]
        elif target_type == "group":
            group_specs = [
                (_SEARCH_GROUP_CHATS, [_SEARCH_GROUP_CHATS]),
                (_SEARCH_GROUP_FREQUENT, [_SEARCH_GROUP_FREQUENT]),
            ]
        else:
            group_specs = [
                (
                    f"{_SEARCH_GROUP_CONTACTS}/{_SEARCH_GROUP_CHATS}",
                    [_SEARCH_GROUP_CONTACTS, _SEARCH_GROUP_CHATS],
                ),
                (_SEARCH_GROUP_FREQUENT, [_SEARCH_GROUP_FREQUENT]),
                (_SEARCH_GROUP_FUNCTIONS, [_SEARCH_GROUP_FUNCTIONS]),
                ("未知", ["未知"]),
            ]

        match_kinds = ("exact", "contains") if allow_contains else ("exact",)
        for match_kind in match_kinds:
            for display_group, source_groups in group_specs:
                matches = []
                for source_group in source_groups:
                    matches.extend(
                        grouped_matches.get(source_group, {}).get(match_kind, [])
                    )
                if len(matches) > 1:
                    logger.error(
                        "搜索结果存在多个候选，拒绝自动选择 chat=%r group=%s "
                        "match=%s count=%d names=%r",
                        chat_name,
                        display_group,
                        match_kind,
                        len(matches),
                        [getattr(item, "name", "") for item in matches],
                    )
                    return None
                if len(matches) == 1:
                    chosen = matches[0]
                    logger.info(
                        "微信搜索结果匹配成功 chat=%r group=%s name=%r match=%s",
                        chat_name,
                        display_group,
                        getattr(chosen, "name", ""),
                        match_kind,
                    )
                    return chosen

        logger.info("微信搜索结果无匹配 chat=%r target_type=%s", chat_name, target_type)
        return None

    @staticmethod
    def _click_search_result(control, result_list=None, immediate=False):
        if not _control_exists_now(control):
            logger.debug("微信搜索结果控件在点击前已失效")
            return False
        if not immediate:
            container = result_list
            try:
                if container is None:
                    container = control.GetParentControl()
                _roll_control_into_view(container, control)
            except Exception:
                logger.debug("滚动微信搜索结果到可见区域失败", exc_info=True)
            if result_list is not None:
                time.sleep(_UI_POLL_INTERVAL)
        if result_list is not None and not _search_result_is_clickable(
            result_list,
            control,
        ):
            logger.debug(
                "微信搜索结果点击中心不在当前面板可见区域 list_rect=%r "
                "control_rect=%r",
                _control_rectangle(result_list),
                _control_rectangle(control),
            )
            return False
        logger.debug(
            "准备点击微信搜索结果 control_rect=%r list_rect=%r",
            _control_rectangle(control),
            _control_rectangle(result_list) if result_list is not None else None,
        )
        for kwargs in ({"simulateMove": False}, {}):
            if not _control_exists_now(control):
                logger.debug("微信搜索结果控件在点击重试前已失效")
                return False
            try:
                _call_uia_without_wait(control.Click, **kwargs)
                logger.debug("微信搜索结果点击已提交 kwargs=%s", kwargs)
                return True
            except Exception:
                logger.debug("点击微信搜索结果失败 kwargs=%s", kwargs, exc_info=True)
        if not _control_exists_now(control):
            logger.debug("微信搜索结果控件在双击回退前已失效")
            return False
        try:
            _call_uia_without_wait(control.DoubleClick, simulateMove=False)
            logger.debug("微信搜索结果双击回退已提交")
            return True
        except Exception:
            logger.debug("双击微信搜索结果失败", exc_info=True)
            return False

    @staticmethod
    def _clear_search_safely(chat_window):
        clear = getattr(chat_window, "_clear_search", None)
        if not callable(clear):
            return
        try:
            clear()
        except Exception:
            logger.debug("清理微信搜索框失败", exc_info=True)

    @staticmethod
    def _wait_for_session_item(
        root,
        chat_name,
        timeout=1.0,
        stop_event=None,
        allow_fuzzy=False,
    ):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            _raise_if_stopped(stop_event)
            item = _find_session_item(root, chat_name)
            if item is None and allow_fuzzy:
                item = _find_fuzzy_session_item(root, chat_name)
            if item is not None:
                return item
            time.sleep(_UI_POLL_INTERVAL)
        return None

    @staticmethod
    def _window_handles(main_hwnd=None):
        try:
            return {
                hwnd
                for hwnd, _title, _class_name in (
                    _list_wechat_windows_for_main(main_hwnd)
                    if main_hwnd
                    else _list_wechat_windows()
                )
            }
        except Exception:
            logger.debug("双击前枚举微信窗口失败", exc_info=True)
            return None

    @staticmethod
    def _require_matching_window_title(hwnd, chat_name, log_success=False):
        score, identity = _match_window_identity(hwnd, chat_name)
        if not score:
            raise _WrongChatWindow(
                f"独立窗口标题不匹配（综合身份校验）: expected={chat_name!r} "
                f"native_title={identity['native_title']!r} "
                f"root_name={identity['root_name']!r} "
                f"current_chat_name={identity['current_chat_name']!r} "
                f"ui_class={identity['ui_class']!r} "
                f"has_main_structure={identity['has_main_structure']!r} "
                f"hwnd={hwnd}"
            )
        if log_success:
            logger.info(
                "独立窗口身份复核通过 expected=%r native_title=%r "
                "root_name=%r current_chat_name=%r ui_class=%r "
                "hwnd=%s score=%d",
                chat_name,
                identity["native_title"],
                identity["root_name"],
                identity["current_chat_name"],
                identity["ui_class"],
                hwnd,
                score,
            )
        return identity["native_title"]

    def _require_matching_window_title_relaxed(
        self, hwnd, chat_name, session=None, log_success=False,
    ):
        """Round 13: title check that trusts the WeFlow session_id
        binding when present.

        WeChat 4.0's independent chat window does NOT
        auto-update its title when a group is renamed — the
        title stays stuck at the OLD name while WeFlow reports
        the new name. The WeFlow session_id (e.g.
        ``45808329857@chatroom``) is the chat's persistent
        identity that survives renames.

        When ``session`` is provided AND its
        ``session_id`` is non-empty, the hwnd is bound to a
        WeFlow session_id (via ``_attach_session_id``'s
        first-unbound-wins history walk). In that case the
        session_id is the authoritative identity and the
        title check is redundant — the hwnd is the right
        WeChat window regardless of what its title says.
        Return the current native title without raising so
        the caller's pre-send / cached-session path doesn't
        evict the session and re-open a new window (which
        would duplicate the window for the same chat).

        When no session is provided, OR session_id is
        empty (the bot has never seen a WeFlow inbound for
        this hwnd), fall back to the strict
        ``_require_matching_window_title`` check. This
        preserves the round 4-9 safety net for the
        "two chats with similar names" false-positive case
        — if the bot has no inbound evidence, the title
        check is the only signal that the hwnd is the
        right chat.

        Returns the actual native title on success, raises
        ``_WrongChatWindow`` on failure (when no session
        binding exists and the title doesn't match).
        """
        if session is not None and getattr(session, "session_id", ""):
            # Round 19: do NOT swallow the dead-hwnd exception
            # here. If the cached hwnd has been closed / reaped
            # by WeChat, ``_get_window_title`` raises and the
            # caller's existing ``except`` branch fires
            # ``_evict_session_bindings`` so the next access
            # goes through the open path. Previously the
            # ``try/except`` below returned "" for a dead hwnd
            # and the bot took ~3 polling cycles to discover
            # the closed window via the message-list probe,
            # which then forced the longer
            # ``_recover_session`` → ``_recover_session_impl``
            # → ``_open_verified_subwindow`` path. The
            # round-13 contract (skip the title check when a
            # WeFlow session_id is bound) is about NOT
            # evicting on a *title* mismatch; a dead hwnd is
            # a different failure and should be propagated.
            actual = _get_window_title(hwnd)
            if (
                self._subwindow_title_refreshes
                and actual
                and not chat_names_equal(actual, session.name)
            ):
                self._adopt_refreshed_window_title(session, actual)
            if log_success:
                logger.info(
                    "微信会话ID 已绑定，跳过标题检查 "
                    "session_id=%s chat=%r hwnd=%s actual_title=%r",
                    session.session_id,
                    chat_name,
                    hwnd,
                    actual,
                )
            return actual
        return self._require_matching_window_title(
            hwnd, chat_name, log_success=log_success,
        )

    def _adopt_refreshed_window_title(self, session, actual_title):
        """Update one stable-ID session after its HWND title follows a rename."""
        actual_title = str(actual_title or "").strip()
        if not actual_title:
            return
        with self._window_lifecycle_lock:
            if chat_names_equal(actual_title, session.name):
                return
            old_name = session.name
            old_key = normalize_chat_name(old_name)
            new_key = normalize_chat_name(actual_title)
            if not new_key:
                return
            session.name = actual_title
            self._sessions[new_key] = session
            self._remember_chat_alias(old_name, actual_title)
            self._chat_aliases.setdefault(old_key, actual_title)
            session_id = getattr(session, "session_id", "") or ""
            if session_id:
                self._recent_inbound_sessions[new_key] = session_id
            session.next_identity_check_at = (
                time.monotonic() + _IDENTITY_CHECK_TTL_SECONDS
            )
            logger.info(
                "独立窗口 HWND 标题已随改名刷新 old=%r new=%r "
                "session_id=%s hwnd=%s",
                old_name,
                actual_title,
                session_id or "<none>",
                session.hwnd,
            )

    @staticmethod
    def _require_verified_subwindow(hwnd, chat_name, main_hwnd, log_success=False):
        if not hwnd or hwnd == main_hwnd:
            raise RuntimeError(
                f"没有获得独立于主窗口的新 HWND: chat={chat_name!r} "
                f"main_hwnd={main_hwnd} candidate={hwnd}"
            )
        main_pid = _get_window_process_id(main_hwnd)
        candidate_pid = _get_window_process_id(hwnd)
        if (
            main_pid is not None
            and candidate_pid is not None
            and candidate_pid != main_pid
        ):
            raise RuntimeError(
                f"候选窗口不是当前微信进程的独立窗口: "
                f"chat={chat_name!r} hwnd={hwnd}"
            )
        title = WeChatListener._require_matching_window_title(
            hwnd,
            chat_name,
            log_success=log_success,
        )
        root = _control_from_handle(hwnd)
        _require_subwindow_message_list(root, chat_name, hwnd=hwnd)
        return title

    @staticmethod
    def _wait_for_subwindow(
        chat_name,
        main_hwnd,
        timeout=WEMAI_SUBWINDOW_TIMEOUT,
        existing_hwnds=None,
        stop_event=None,
    ):
        _raise_if_stopped(stop_event)
        detect_wrong_new_window = existing_hwnds is not None
        existing_hwnds = set(existing_hwnds or ())
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            _raise_if_stopped(stop_event)
            hwnd = _find_window_by_title(chat_name, exclude_hwnd=main_hwnd)
            if hwnd:
                try:
                    WeChatListener._require_verified_subwindow(
                        hwnd,
                        chat_name,
                        main_hwnd,
                        log_success=True,
                    )
                except _WrongChatWindow:
                    if detect_wrong_new_window and hwnd in existing_hwnds:
                        logger.debug(
                            "忽略双击前已存在的相似标题窗口 "
                            "chat=%s hwnd=%s",
                            chat_name,
                            hwnd,
                        )
                        time.sleep(_UI_POLL_INTERVAL)
                        continue
                    logger.error(
                        "双击后命中了错误聊天窗口，正在关闭 chat=%s hwnd=%s",
                        chat_name,
                        hwnd,
                    )
                    _close_window(hwnd)
                    raise
                except RuntimeError as exc:
                    logger.debug(
                        "标题命中的窗口尚未形成独立聊天结构 "
                        "chat=%s hwnd=%s error=%s",
                        chat_name,
                        hwnd,
                        exc,
                    )
                    time.sleep(_UI_POLL_INTERVAL)
                    continue
                return hwnd

            windows = []
            if detect_wrong_new_window:
                windows = _list_new_window_candidates(
                    main_hwnd,
                    existing_hwnds,
                )
            for candidate, _title, _class_name in windows:
                try:
                    WeChatListener._require_verified_subwindow(
                        candidate,
                        chat_name,
                        main_hwnd,
                        log_success=True,
                    )
                    return candidate
                except _WrongChatWindow:
                    # Qt often publishes the HWND before its native/UIA names.
                    # Keep polling instead of declaring it to be the wrong chat
                    # based on one transitional read.
                    continue
                except RuntimeError:
                    # The HWND/title can be published before MessageView and
                    # the independent-window UIA class are ready.
                    continue
            time.sleep(_UI_POLL_INTERVAL)

        _raise_if_stopped(stop_event)
        new_windows = []
        if detect_wrong_new_window:
            new_windows = [
                (candidate, title)
                for candidate, title, _class_name in _list_new_window_candidates(
                    main_hwnd,
                    existing_hwnds,
                )
            ]
        if len(new_windows) == 1:
            candidate, title = new_windows[0]
            identity_error = None
            try:
                WeChatListener._require_verified_subwindow(
                    candidate,
                    chat_name,
                    main_hwnd,
                    log_success=True,
                )
                return candidate
            except (RuntimeError, _WrongChatWindow) as exc:
                identity_error = exc
                identity_score, identity = _match_window_identity(
                    candidate,
                    chat_name,
                    native_title=title,
                )

            if identity_score:
                raise _SubwindowOpenedButUnverified(
                    f"独立窗口身份已匹配但聊天结构仍未就绪: "
                    f"expected={chat_name!r} hwnd={candidate}"
                ) from identity_error

            if (
                identity.get("ui_class") == _MAIN_WINDOW_CLASS
                or identity.get("has_main_structure")
            ):
                raise _WrongChatWindow(
                    f"双击后候选仍是微信主窗口，未获得独立窗口: "
                    f"expected={chat_name!r} hwnd={candidate}"
                ) from identity_error

            # No usable title/name means UIA is still initializing.  Preserve
            # the window and stop the outer click loop; a second click cannot
            # improve identity publication and may repeatedly hit the row.
            generic_names = {"微信", "wechat", "weixin"}
            has_identity = any(
                normalize_chat_name(identity[field]).casefold()
                not in generic_names
                for field in (
                    "native_title",
                    "root_name",
                    "current_chat_name",
                )
                if normalize_chat_name(identity[field])
            )
            if not has_identity:
                raise _SubwindowOpenedButUnverified(
                    f"独立窗口已出现但标题/UIA 名称仍为空: "
                    f"expected={chat_name!r} hwnd={candidate}"
                ) from identity_error

            logger.error(
                "双击后出现标题不匹配的新微信窗口，正在关闭 "
                "expected=%r actual=%r hwnd=%s",
                chat_name,
                title,
                candidate,
            )
            _close_window(candidate)
            raise _WrongChatWindow(
                f"双击打开了错误窗口: expected={chat_name!r} "
                f"actual={title!r} hwnd={candidate}"
            )
        if new_windows:
            logger.error(
                "双击后出现多个新微信窗口，无法安全判断目标 expected=%r windows=%s",
                chat_name,
                new_windows,
            )
            raise _SubwindowOpenedButUnverified(
                f"双击后出现多个尚未验证的新微信窗口: "
                f"expected={chat_name!r} windows={new_windows!r}"
            )
        raise RuntimeError(f"等待独立聊天窗口超时: {chat_name}")

    def _poll_due_sessions(self):
        now = time.monotonic()
        sessions = []
        session_ids = set()
        for name in self.listening_target_names:
            session = self._sessions.get(normalize_chat_name(name))
            if (
                session is None
                or id(session) in session_ids
                or session.next_scan_at > now
            ):
                continue
            session_ids.add(id(session))
            sessions.append(session)
        sessions.sort(key=lambda item: item.next_scan_at)
        # WEMAI_BATCH_SIZE used to truncate this list.  The omitted sessions
        # stayed overdue, but the outer worker slept before looking again and
        # could repeatedly lag behind under load.  Scan every session that was
        # due at the start of this pass; heartbeat updates keep the watchdog
        # informed during large target sets.
        for session in sessions:
            try:
                self._poll_session(session)
            finally:
                self._touch_heartbeat()
        # Image preview automation is necessarily serialized on the UI owner
        # thread.  Run at most one job only after all due chats have had their
        # text rows collected, so a slow image cannot hide concurrent text.
        self._process_pending_media(limit=1)
        self._touch_heartbeat()

    def _poll_session(self, session):
        session.scan_count += 1
        now = time.monotonic()
        if now >= session.next_identity_check_at:
            try:
                # Round 13: relaxed title check — see the
                # long comment in
                # ``_require_matching_window_title_relaxed``.
                self._require_matching_window_title_relaxed(
                    session.hwnd, session.name, session=session,
                )
                session.next_identity_check_at = now + WEMAI_WINDOW_CHECK_INTERVAL
            except Exception as exc:
                session.fail_count += 1
                session.next_scan_at = now + min(
                    max(WEMAI_TICK, 0.2) * session.fail_count,
                    3.0,
                )
                logger.warning(
                    "独立聊天窗口句柄或标题已失效 chat=%s hwnd=%s "
                    "failures=%d error=%s",
                    session.name,
                    session.hwnd,
                    session.fail_count,
                    exc,
                )
                self._recover_session(session)
                return

        try:
            if not _control_exists_now(session.message_list):
                replacement = _find_message_list(session.root)
                if replacement is None or not _control_exists_now(replacement):
                    raise RuntimeError("缓存的 MessageView 消息列表已失效")
                session.message_list = replacement
                logger.info("已就地刷新 MessageView 缓存 chat=%s", session.name)
            visible_items = _read_visible_items(session.message_list)
            visible_count = len(visible_items)
            if not visible_items and session.snapshot:
                session.empty_read_count += 1
                # A Qt accessibility-tree refresh briefly returns zero
                # children.  Preserve the old anchor and retry promptly; after
                # repeated empties, refresh the cached MessageView proxy.
                if session.empty_read_count >= 2:
                    replacement = _find_message_list(session.root)
                    if replacement is not None and _control_exists_now(replacement):
                        session.message_list = replacement
                session.next_scan_at = time.monotonic() + min(WEMAI_TICK, 0.03)
                logger.debug(
                    "MessageView 暂时返回空列表，保留快照并快速重试 "
                    "chat=%s empty_reads=%d",
                    session.name,
                    session.empty_read_count,
                )
                return
            session.empty_read_count = 0
            bottom_state = _message_list_at_bottom(session.message_list)
            if bottom_state is not None:
                session.at_bottom = bottom_state
            appended, anchored = _appended_visible_items(
                session.snapshot,
                visible_items,
                disjoint_is_append=session.at_bottom,
            )
            session.visible_count = visible_count
            if anchored:
                session.snapshot = tuple(visible_items)
                # Retain only the current snapshot for diagnostics and backward
                # compatibility; this set must never grow for the process life.
                session.seen = {item.key for item in visible_items}
            elif visible_items:
                logger.debug(
                    "消息列表快照没有底部锚点，按历史滚动处理 chat=%s "
                    "previous=%d current=%d",
                    session.name,
                    len(session.snapshot),
                    len(visible_items),
                )

            # Never truncate appended: doing so silently drops the first rows
            # of any burst larger than WEMAI_TAIL_SIZE.  The tail limit applies
            # only to locating image rows that are already pending a retry.
            visible_tail = (
                visible_items[-WEMAI_TAIL_SIZE:]
                if WEMAI_TAIL_SIZE > 0
                else visible_items
            )

            items = list(session.recovered_items)
            session.recovered_items.clear()
            known_item_keys = {item.key for item in items}
            items.extend(item for item in appended if item.key not in known_item_keys)
            candidate_keys = {item.key for item in items}
            if session.media_failures:
                for item in visible_tail:
                    if (
                        item.key in session.media_failures
                        and item.key not in candidate_keys
                    ):
                        items.append(item)
                        candidate_keys.add(item.key)
        except Exception as exc:
            session.fail_count += 1
            session.next_scan_at = time.monotonic() + min(
                max(WEMAI_TICK, 0.2) * session.fail_count,
                3.0,
            )
            logger.warning(
                "读取独立聊天窗口失败 chat=%s failures=%d error=%s",
                session.name,
                session.fail_count,
                exc,
            )
            if session.fail_count >= 3:
                self._recover_session(session)
            return

        session.fail_count = 0
        added = 0
        registry = self.outgoing_registry
        for item in items:
            if item.kind != "message":
                continue
            is_image = getattr(item, "message_type", "text") == "image"
            direction = _message_direction(getattr(item, "control", None))
            if direction == "right":
                logger.info(
                    "检测到右侧自发微信消息，已忽略 chat=%s runtime_id=%r",
                    session.name,
                    item.runtime_id,
                )
                continue
            image_direction = direction if is_image else None
            if is_image:
                logger.info(
                    "检测到收到的微信图片 chat=%s runtime_id=%r direction=%s "
                    "auto_download=%s",
                    session.name,
                    item.runtime_id,
                    image_direction or "unknown",
                    IMAGE_AUTO_DOWNLOAD,
                )
            if (
                registry is not None
                and hasattr(registry, "should_ignore")
                # Every image row has the same accessible text.  Content-only
                # outgoing deduplication would suppress unrelated received
                # images whenever direction detection is unavailable.
                and not is_image
                # A real left-side row is authoritative.  Content-only
                # deduplication otherwise drops a genuine reply whose text is
                # equal to something the bot recently sent.  Keep the legacy
                # fallback only for rows without a usable UIA control.
                and getattr(item, "control", None) is None
                and registry.should_ignore(session.name, item.name)
            ):
                continue
            if is_image and IMAGE_AUTO_DOWNLOAD:
                if image_direction is None:
                    logger.warning(
                        "无法确定图片消息方向，按收到的图片处理 chat=%s "
                        "runtime_id=%r",
                        session.name,
                        item.runtime_id,
                    )
                media_key = (id(session), item.key)
                if not any(
                    (id(candidate_session), candidate_item.key) == media_key
                    for candidate_session, candidate_item, _direction
                    in self._pending_media
                ):
                    self._pending_media.append(
                        (session, item, image_direction or "left")
                    )
                continue
            event_content = "[图片]" if is_image else item.name
            if self._dispatch_inbound_item(
                session,
                item,
                direction,
                event_content,
                "text",
            ):
                added += 1
            else:
                session.recovered_items.append(item)
        self._schedule_next_scan(session, added)

    def _dispatch_inbound_item(
        self,
        session,
        item,
        direction,
        event_content,
        event_type,
    ):
        """构造并可靠移交一条已经完成方向、类型判断的 UI 入站消息。"""
        chat_type = session.chat_type or self._resolved_chat_type(session.name)
        structural_sender = ""
        type_source = self._chat_type_source_for(session.name)
        if (
            chat_type not in {"private", "group"}
            or type_source in {"fallback", "message", "unknown"}
        ):
            structural_sender = _group_sender_from_message(
                getattr(item, "control", None),
                item.name,
                direction=direction,
            )
            if structural_sender:
                chat_type = self._cache_detected_chat_type(
                    session.name,
                    "group",
                    source="message",
                    window_title=session.name,
                )
                session.chat_type = chat_type
        sender = _message_sender(
            getattr(item, "control", None),
            item.name,
            chat_type,
            session.name,
            direction=direction,
        )
        if structural_sender:
            sender = structural_sender
        if event_type == "text" and chat_type == "group":
            _qml_sender, qml_content = _qml_message_parts(
                getattr(item, "control", None),
                item.name,
            )
            if qml_content:
                event_content = qml_content
        try:
            self._handle_event(_InboundEvent(
                group=session.name,
                content=event_content,
                timestamp=time.time(),
                raw=getattr(item, "control", None),
                message_type=event_type,
                sender=sender,
                chat_type=chat_type or "",
            ))
        except Exception:
            logger.exception(
                "微信消息回调执行失败，已保留等待重试 chat=%s runtime_id=%r",
                session.name,
                item.runtime_id,
            )
            return False
        session.new_count += 1
        return True

    def _process_pending_media(self, limit=1):
        for _index in range(max(0, int(limit))):
            if not self._pending_media:
                return
            session, item, direction = self._pending_media.pop(0)
            try:
                self._media_active = True
                self._media_started = time.monotonic()
                self._touch_heartbeat()
                saved_path = self._save_received_image(session, item, direction)
                session.media_failures.pop(item.key, None)
                if not self._dispatch_inbound_item(
                    session,
                    item,
                    direction,
                    saved_path,
                    "image",
                ):
                    self._pending_media.append((session, item, direction))
            except Exception as exc:
                attempts = session.media_failures.get(item.key, 0) + 1
                session.media_failures[item.key] = attempts
                if attempts < _IMAGE_SAVE_MAX_ATTEMPTS:
                    logger.warning(
                        "自动保存微信图片失败，将重试 "
                        "chat=%s runtime_id=%r attempt=%d/%d error=%s",
                        session.name,
                        item.runtime_id,
                        attempts,
                        _IMAGE_SAVE_MAX_ATTEMPTS,
                        exc,
                    )
                    self._pending_media.append((session, item, direction))
                else:
                    session.media_failures.pop(item.key, None)
                    logger.exception(
                        "自动保存微信图片连续失败，降级为图片占位文本 "
                        "chat=%s runtime_id=%r attempts=%d error=%s",
                        session.name,
                        item.runtime_id,
                        attempts,
                        exc,
                    )
                    if not self._dispatch_inbound_item(
                        session,
                        item,
                        direction,
                        "[图片]",
                        "text",
                    ):
                        session.recovered_items.append(item)
            finally:
                self._media_active = False
                self._media_started = None
                self._touch_heartbeat()

    def _save_received_image(self, session, item, direction):
        """Open and persist one received WeChat 4.0 image message."""
        deadline = time.monotonic() + max(float(IMAGE_SAVE_TIMEOUT_SECONDS), 0.1)
        self._activate_session_window(session)
        self._require_verified_subwindow(
            session.hwnd,
            session.name,
            self.wx.window.hwnd,
        )
        process_id = _get_window_process_id(session.hwnd)
        if process_id is None:
            raise RuntimeError("无法获取微信独立窗口进程 ID")
        existing_hwnds = {
            hwnd
            for hwnd, _title, _class_name in _list_top_level_windows_by_pid(
                process_id
            )
        }
        _roll_control_into_view(session.message_list, item.control)
        click_strategy = session.media_failures.get(item.key, 0)
        if not _click_image_message(
            item.control,
            direction,
            strategy=click_strategy,
        ):
            raise RuntimeError("无法点击微信图片消息")

        preview = None
        temporary_source = None
        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("激活微信图片消息后保存时限已耗尽")
            preview = _wait_for_image_preview(
                process_id,
                existing_hwnds,
                remaining,
            )
            preview_hwnd, preview_root, toolbar = preview
            preview_process_id = (
                _get_window_process_id(preview_hwnd) or process_id
            )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("等待微信图片预览后保存时限已耗尽")
            source_path, should_cleanup_source = _acquire_image_from_preview(
                toolbar,
                preview_process_id,
                remaining,
            )
            if should_cleanup_source:
                temporary_source = source_path
            saved_path = _persist_received_image(source_path)
            logger.info(
                "微信图片已自动保存 chat=%s path=%s size=%d",
                session.name,
                saved_path,
                os.path.getsize(saved_path),
            )
            return saved_path
        finally:
            if temporary_source is not None:
                with contextlib.suppress(OSError):
                    os.unlink(temporary_source)
            if preview is not None:
                _close_preview_window(preview[0], preview[1])
            else:
                late_preview = _preview_window_from_pid(
                    process_id,
                    preferred_hwnds=existing_hwnds,
                )
                if late_preview is not None:
                    _close_preview_window(late_preview[0], late_preview[1])

    @staticmethod
    def _schedule_next_scan(session, added):
        now = time.monotonic()
        if added:
            session.last_message_at = now
        # Adaptive 1-3 second idle backoff made an otherwise healthy listener
        # appear to miss the first message in a quiet chat.  Cached-control
        # scans are now cheap enough to honor the configured interval at all
        # times; failed media jobs are handled separately after text polling.
        session.interval = WEMAI_TICK
        session.next_scan_at = now + session.interval

    def _recover_session(self, session):
        """Rebind an existing window or reopen a vanished independent chat."""
        with self._window_lifecycle_lock:
            outermost = not self._recovery_active
            if outermost:
                self._recovery_active = True
                self._recovery_started = time.monotonic()
                self._touch_heartbeat()
            try:
                return self._recover_session_impl(session)
            finally:
                if outermost:
                    self._recovery_active = False
                    self._recovery_started = None
                    self._touch_heartbeat()

    def _recover_session_impl(self, session):
        try:
            self._refresh_session_name_from_latest_inbound(session)
            main_hwnd = self.wx.window.hwnd
            hwnd = _find_window_by_title(
                session.name,
                exclude_hwnd=main_hwnd,
            )
            if hwnd:
                # Round 13: relaxed check. When the
                # session is bound to a WeFlow
                # session_id, the title check is
                # skipped — the title might not match
                # ``session.name`` because of a
                # rename, but the hwnd is still the
                # right WeChat window. Without this
                # the recover path would mark the
                # session as "window gone" and try to
                # open a duplicate window for the
                # same chat.
                try:
                    self._require_matching_window_title_relaxed(
                        hwnd, session.name, session=session,
                    )
                except _WrongChatWindow:
                    hwnd = None
            if not hwnd:
                logger.warning("独立聊天窗口已消失，准备重新打开 chat=%s", session.name)
                hwnd = self._open_verified_subwindow(session.name, main_hwnd)
            # User report: "改名后不仅无法发送，而且还有可能搜索旧名字" —
            # when the cached window is gone (WeChat's "独立窗口定时
            # 保留机制" closed the idle independent window) and the
            # user has since renamed the group, the re-open path is
            # driven by the alias (so the search uses the NEW name),
            # but the freshly-opened window's title is the NEW name
            # while ``session.name`` is still the OLD name. The strict
            # identity check would raise ``_WrongChatWindow`` here, the
            # outer ``except Exception`` would log "重新绑定失败" and
            # the bot would re-loop on the next send. Mirror the
            # rename auto-rebind pattern used in
            # ``_get_or_open_session``: if the actual title differs
            # from ``session.name``, update the session, re-key it
            # under the new name, and record the alias. The OLD key
            # is kept as a fallback so a concurrent lookup of the
            # OLD name still hits the same window.
            # Round 13: relaxed post-recover title
            # check. The hwnd was either just found
            # by ``_find_window_by_title`` above
            # (already verified) or just opened by
            # ``_open_verified_subwindow`` (also
            # already verified). If the session is
            # bound to a WeFlow session_id, the
            # title check is skipped — the
            # session_id is the authoritative
            # identity and the title might not
            # match ``session.name`` because of a
            # rename.
            actual_title = (
                self._require_matching_window_title_relaxed(
                    hwnd, session.name, session=session,
                )
            )
            if actual_title and not chat_names_equal(
                actual_title, session.name
            ):
                old_name = session.name
                new_name = actual_title
                session.name = new_name
                self._remember_chat_alias(old_name, new_name)
                old_key = normalize_chat_name(old_name)
                new_key = normalize_chat_name(new_name)
                if new_key != old_key:
                    self._sessions[new_key] = session
                self._chat_aliases.setdefault(old_key, new_name)
                logger.info(
                    "恢复时检测到群聊改名，已更新 session 缓存与 alias "
                    "old=%r new=%r hwnd=%s",
                    old_name,
                    new_name,
                    hwnd,
                )
            root = _control_from_handle(hwnd)
            message_list = _require_subwindow_message_list(
                root,
                session.name,
                hwnd=hwnd,
            )
            baseline = _read_visible_items(message_list)
            bottom_state = _message_list_at_bottom(message_list)
            recovered, anchored = _appended_visible_items(
                session.snapshot,
                baseline,
                disjoint_is_append=(
                    session.at_bottom if bottom_state is None else bottom_state
                ),
            )
            old_hwnd = session.hwnd
            if old_hwnd != hwnd:
                with _ACTIVATION_STRATEGY_LOCK:
                    _ACTIVATION_STRATEGY_BY_HWND.pop(old_hwnd, None)
                sid = str(getattr(session, "session_id", "") or "").strip()
                if sid:
                    if self._hwnd_to_session_id.get(old_hwnd) == sid:
                        self._hwnd_to_session_id.pop(old_hwnd, None)
                    self._hwnd_to_session_id[hwnd] = sid
            session.hwnd = hwnd
            session.root = root
            session.message_list = message_list
            session.chat_input = None
            if anchored:
                session.snapshot = tuple(baseline)
                session.seen = {item.key for item in baseline}
                existing_recovered = {
                    item.key for item in session.recovered_items
                }
                session.recovered_items.extend(
                    item for item in recovered
                    if item.key not in existing_recovered
                )
            elif not session.snapshot:
                session.snapshot = tuple(baseline)
                session.seen = {item.key for item in baseline}
            session.visible_count = len(baseline)
            if bottom_state is not None:
                session.at_bottom = bottom_state
            session.empty_read_count = 0
            session.next_identity_check_at = (
                time.monotonic() + WEMAI_WINDOW_CHECK_INTERVAL
            )
            session.fail_count = 0
            session.next_scan_at = time.monotonic() + WEMAI_TICK
            logger.info(
                "已重新绑定独立聊天窗口 chat=%s old_hwnd=%s new_hwnd=%s "
                "native_title=%r recovered=%d anchored=%s",
                session.name,
                old_hwnd,
                hwnd,
                _get_window_title(hwnd),
                len(recovered),
                anchored,
            )
            return True
        except ChatNotFoundError as exc:
            # Propagate so the caller (the monitor loop or the send
            # path's recovery) can evict the session from
            # ``self._sessions`` and stop retrying. Other exceptions
            # still get the existing "log + return False" treatment
            # because they may be transient (e.g. a Qt refresh that
            # will succeed on the next check).
            logger.warning(
                "重新绑定独立聊天窗口失败（聊天已不存在） chat=%s error=%s",
                session.name,
                exc,
            )
            self._evict_session(session)
            return False
        except Exception as exc:
            logger.warning("重新绑定独立聊天窗口失败 chat=%s error=%s", session.name, exc)
            return False

    def _evict_session_bindings(self, session, reason="hwnd_invalid"):
        """Round 15 / 方案一: full eviction of a session from ALL
        cached maps.

        The previous in-line eviction at
        ``_get_or_open_session`` (and the older
        ``_evict_session`` helper) only removed the session
        from ``_sessions``. They left the
        ``_sessions_by_session_id[sid]`` and
        ``_hwnd_to_session_id[hwnd]`` entries pointing at a
        dead session. The next call into
        ``_resolve_cached_session_by_session_id`` would
        return the dead session, the relaxed title check
        would silently pass (a missing hwnd raises no
        identity exception in the session_id-bound path),
        and the send path would either crash inside UIA or
        trigger a noisy recovery. The "force close the
        window to refresh" hack (方案二/三) is not needed if
        this map hygiene runs every time a window is
        detected closed.

        方案一 trigger: this method is the single point
        where the hwnd-title binding for a session_id is
        declared invalid. The next access through the open
        path (``_open_verified_subwindow``) will acquire
        a fresh hwnd — the new window's title is the
        current chat name from the WeChat chat list, so
        the hwnd-title binding is naturally updated to
        the latest. No active window manipulation, no
        UI flicker, no unread message loss.

        Adapter-stop trigger: ``close()`` calls this for
        every cached session just before clearing the
        maps wholesale. The next bot startup is a clean
        slate — there are no stale bindings to fight.

        Safe to call multiple times (idempotent) and safe
        to call with ``None``. The function only mutates
        the maps whose entry actually points at the given
        session, so it cannot accidentally clear an
        unrelated session's binding.
        """
        if session is None:
            return
        sid = (
            getattr(session, "session_id", "") or ""
        ).strip()
        old_hwnd = getattr(session, "hwnd", None)
        removed_keys = []
        # 1. _sessions: drop every key whose value is this
        #    session object. The chat may be cached under
        #    multiple normalized keys (round 11's re-key
        #    after rename), so we walk the whole map.
        for key, candidate in list(self._sessions.items()):
            if candidate is session:
                removed_keys.append(key)
                self._sessions.pop(key, None)
        # 2. _sessions_by_session_id: drop the entry ONLY
        #    if it still points at this session. Another
        #    session might have claimed this session_id
        #    concurrently (vanishingly rare — one
        #    session_id per chat — but possible if the
        #    user is running two bots). The guard keeps
        #    the active binding alive in that case.
        if sid and self._sessions_by_session_id.get(sid) is session:
            self._sessions_by_session_id.pop(sid, None)
        # 3. _hwnd_to_session_id: drop the entry ONLY if
        #    the reverse map still points back at this
        #    session_id. The hwnd is dead now (that's
        #    why we're evicting), but the same hwnd
        #    could theoretically have been re-bound to a
        #    different session_id by a concurrent
        #    open path; the guard preserves that newer
        #    binding.
        if (
            old_hwnd is not None
            and self._hwnd_to_session_id.get(old_hwnd) == sid
        ):
            self._hwnd_to_session_id.pop(old_hwnd, None)
        with _ACTIVATION_STRATEGY_LOCK:
            _ACTIVATION_STRATEGY_BY_HWND.pop(old_hwnd, None)
        session.chat_input = None
        logger.info(
            "方案一 hwnd 失效已清理 session_id=%s hwnd=%s "
            "removed_session_keys=%d reason=%s",
            sid or "<none>",
            old_hwnd,
            len(removed_keys),
            reason,
        )

    def _evict_session(self, session):
        """Remove a session from the cache; safe to call multiple times.

        Used when ``_open_verified_subwindow`` raises
        ``ChatNotFoundError`` to drop the cache entry so the next
        send/monitor pass does not waste time re-running the same
        failing open. Round 15 / 方案一: delegates to
        ``_evict_session_bindings`` so all three maps are
        cleared consistently, not just ``_sessions``.
        """
        self._evict_session_bindings(
            session, reason="chat_not_found"
        )

    @staticmethod
    def _activate_session_window(session):
        # The pre-activate check is throttled: if the session's title
        # was verified within ``_IDENTITY_CHECK_TTL_SECONDS``, skip the
        # second UIA walk. The post-activate check stays as a safety
        # net — it catches the case where the foreground tricks
        # returned success but the activated window is not actually
        # the target chat (rare; would only happen on a misbehaving
        # window manager).
        now = time.monotonic()
        if now >= session.next_identity_check_at:
            # Round 13: relaxed check — trust the
            # session_id binding when present. The static
            # method is preserved for backward
            # compatibility; the relaxed version is
            # available on instances.
            sid = getattr(session, "session_id", "") or ""
            if sid:
                with contextlib.suppress(Exception):
                    _get_window_title(session.hwnd)
            else:
                WeChatListener._require_matching_window_title(
                    session.hwnd, session.name
                )
            session.next_identity_check_at = (
                now + _IDENTITY_CHECK_TTL_SECONDS
            )
        if not _activate_native_window(session.hwnd):
            raise RuntimeError(
                f"无法恢复并激活独立聊天窗口: {session.name} "
                f"hwnd={session.hwnd} "
                f"foreground_hwnd={_get_native_foreground_window()}"
            )
        # The post-activate check is a true safety net: skip the
        # expensive walk on a recently-verified session, but only when
        # the title is otherwise believed stable. If activate silently
        # failed (e.g. foreground tricks return success but actually
        # left a different window in front), the next paste/submit
        # would fail and ``_recover_session`` re-validates. The
        # cost-saved here is the redundant UIA walk that always ran
        # right after a successful activate.
        now = time.monotonic()
        if now >= session.next_identity_check_at:
            WeChatListener._require_matching_window_title(
                session.hwnd, session.name
            )
            session.next_identity_check_at = (
                now + _IDENTITY_CHECK_TTL_SECONDS
            )

    def send(self, receiver, kind, data):
        """在目标独立聊天窗口中发送文本、图片或文件。"""
        self._assert_owner_thread()
        return self._send_from_worker(receiver, kind, data)

    def _send_from_worker(self, receiver, kind, data):
        """在绑定会话的工作线程完成 UIA 初始化后执行发送。"""
        if kind not in {"text", "image", "file"}:
            raise ValueError(f"不支持发送类型: {kind}")
        key = normalize_chat_name(receiver)
        with self._send_workers_lock:
            send_lock = self._chat_send_locks.setdefault(
                key,
                threading.RLock(),
            )
        with send_lock:
            return self._send_locked(receiver, kind, data)

    def _send_locked(self, receiver, kind, data):
        """仅持有当前接收方的串行锁执行发送，其他独立窗口仍可并行。"""

        started_at = time.monotonic()
        with self._window_lifecycle_lock:
            session = self._get_or_open_session(receiver)
        session_lookup_elapsed = time.monotonic() - started_at
        registry = self.outgoing_registry
        content = str(data) if kind == "text" else None
        paths = (
            list(data) if isinstance(data, (list, tuple)) else [data]
        ) if kind != "text" else None
        last_error = None

        for attempt in range(1, 3):
            reservation_ids = []

            def reserve_outgoing(reservations=reservation_ids):
                if registry is None:
                    return
                if kind == "text":
                    reserve = getattr(registry, "reserve", None)
                    if callable(reserve):
                        reservations.append(
                            reserve(session.name, content)
                        )
                    else:
                        registry.record(session.name, content)
                    return

                file_names = [
                    ntpath.basename(str(path).strip())
                    for path in paths
                ]
                reserve_many = getattr(registry, "reserve_many", None)
                if callable(reserve_many):
                    for file_name in file_names:
                        registry_contents = (
                            ("图片", "[图片]", file_name)
                            if kind == "image"
                            else (file_name,)
                        )
                        reservations.append(
                            reserve_many(session.name, registry_contents)
                        )
                    return

                registry_contents = list(file_names)
                if kind == "image":
                    registry_contents.extend(("图片", "[图片]"))
                for registry_content in registry_contents:
                    if not registry_content:
                        continue
                    try:
                        registry.record(
                            session.name,
                            registry_content,
                            max_hits=1,
                        )
                    except TypeError:
                        registry.record(session.name, registry_content)

            try:
                recovered_before_send = False
                now = time.monotonic()
                if now >= session.next_identity_check_at:
                    try:
                        # Round 13: relaxed title check.
                        # Critical for the rename case —
                        # without this the strict check
                        # rejects the hwnd because the
                        # window's title stays at the OLD
                        # name while session.name was
                        # rebound to the NEW name, the
                        # session is "recovered" (which
                        # opens a duplicate window for the
                        # same chat), and the bot has two
                        # WeChat windows for one group. The
                        # session_id binding is the
                        # authoritative identity when
                        # present; the title is just a
                        # display label that WeChat 4.0
                        # doesn't update on rename.
                        self._require_matching_window_title_relaxed(
                            session.hwnd,
                            session.name,
                            session=session,
                        )
                        session.next_identity_check_at = (
                            now + _IDENTITY_CHECK_TTL_SECONDS
                        )
                    except Exception:
                        logger.warning(
                            "发送前发现窗口已消失，准备恢复 chat=%s",
                            receiver,
                            exc_info=True,
                        )
                        with self._window_lifecycle_lock:
                            recovered = self._recover_session(session)
                        if not recovered:
                            if attempt < 2:
                                continue
                            raise RuntimeError(f"无法重新绑定独立聊天窗口: {receiver}")
                        recovered_before_send = True

                if attempt > 1 and not recovered_before_send:
                    with self._window_lifecycle_lock:
                        recovered = self._recover_session(session)
                    if not recovered:
                        raise RuntimeError(f"无法重新绑定独立聊天窗口: {receiver}")
                # Windows 只有一个全局前台焦点。不同会话的查找、恢复和媒体
                # 准备仍可并行，但从激活窗口到输入框清空、粘贴、提交和确认
                # 必须作为一个短事务串行，避免另一个窗口在回车前抢走焦点。
                with self._foreground_ui_lock:
                    activation_started_at = time.monotonic()
                    self._activate_session_window(session)
                    activation_elapsed = time.monotonic() - activation_started_at
                    input_started_at = time.monotonic()
                    edit = session.chat_input
                    if not edit or not _control_exists_now(edit):
                        edit = _find_chat_input(session.root)
                        session.chat_input = edit
                    input_elapsed = time.monotonic() - input_started_at
                    if not edit or not _control_exists_now(edit):
                        raise RuntimeError(
                            f"独立窗口中未找到聊天输入框: {receiver}"
                        )

                    submit_started_at = time.monotonic()
                    if kind == "text":
                        result = _send_text_via_input(
                            edit,
                            content,
                            before_submit=reserve_outgoing,
                        )
                    else:
                        result = _send_files_via_input(
                            edit,
                            paths,
                            before_submit=reserve_outgoing,
                        )
                    submit_elapsed = time.monotonic() - submit_started_at

                if result is not True:
                    cancel = getattr(registry, "cancel", None)
                    if callable(cancel):
                        cancel(reservation_ids)
                    reservation_ids = []
                    raise RuntimeError(
                        f"WeMai 独立窗口发送返回失败: "
                        f"target={receiver!r} kind={kind!r}"
                    )
                commit = getattr(registry, "commit", None)
                if callable(commit):
                    commit(reservation_ids)
                logger.info(
                    "微信发送完成 target=%s type=%s attempt=%d "
                    "lookup=%.3fs activate=%.3fs input=%.3fs "
                    "submit=%.3fs total=%.3fs",
                    receiver,
                    kind,
                    attempt,
                    session_lookup_elapsed,
                    activation_elapsed,
                    input_elapsed,
                    submit_elapsed,
                    time.monotonic() - started_at,
                )
                return True
            except Exception as exc:
                last_error = exc
                if getattr(exc, "retry_safe", True) is False:
                    commit = getattr(registry, "commit", None)
                    if callable(commit):
                        commit(reservation_ids)
                    raise
                cancel = getattr(registry, "cancel", None)
                if callable(cancel):
                    cancel(reservation_ids)
                if attempt < 2:
                    logger.warning(
                        "独立窗口发送首次尝试失败，准备重绑后重试 "
                        "target=%s type=%s error=%s",
                        receiver,
                        kind,
                        exc,
                    )

        raise RuntimeError(
            f"WeMai 独立窗口发送失败 target={receiver!r} kind={kind!r}: "
            f"{last_error}"
        ) from last_error

    def _handle_event(self, event):
        chat_name = str(event.group)
        content = "" if event.content is None else str(event.content)
        message_type = str(getattr(event, "message_type", "text") or "text")
        if message_type not in {"text", "image", "file"}:
            message_type = "text"
        chat_type = getattr(event, "chat_type", "") or self._resolved_chat_type(
            chat_name
        )
        if chat_type not in {"private", "group"}:
            # Compatibility for callers that inject events directly. Normal
            # UI sessions have already used search-group and header evidence.
            chat_type = self._cache_detected_chat_type(
                chat_name,
                "group",
                source="fallback",
                window_title=chat_name,
            )
            logger.warning("无法从窗口确认聊天类型，兼容回退为群聊 chat=%s", chat_name)
        sender = str(getattr(event, "sender", "") or "").strip()
        if chat_type == "private" and not sender:
            sender = chat_name
        # Round 11: extract the WeFlow session_id from the
        # event so the record_recent_inbound path (called
        # below) can register the rename-bridge mapping
        # ``_recent_inbound_sessions[chat_name] = session_id``.
        # The session_id is the chat's persistent identity
        # that survives WeChat group renames; rounds 11/12/13
        # use it to find the cached session by ID when the
        # title-based lookup would fail.
        real_id = str(
            getattr(event, "session_id", "") or ""
        ).strip()
        # Round 17: extract the sender's WeChat wxid from
        # the event. This is the SENDER-side persistent
        # identity (WeChat native ID like
        # ``wxid_ev4txoa8f2mm22force``) that survives
        # profile-nickname changes (``fromNickName``) and
        # group-nickname changes (``displayName``). The
        # processor's ``_build_message`` uses this as the
        # stable id base for the user_id sent to mai_mai,
        # keyed on ``person.get_id(platform, user_id)`` in
        # the maibot ``person`` API, so a nickname change
        # does not create a fresh person_id (and lose the
        # associated memory / persona / chat history).
        sender_wxid = str(
            getattr(event, "sender_wxid", "") or ""
        ).strip()
        data = {
            "chat": chat_name,
            "chat_type": chat_type,
            "sender": sender or "unknown",
            "type": message_type,
            "content": content,
            "timestamp": event.timestamp,
        }
        file_name = str(getattr(event, "file_name", "") or "").strip()
        if file_name:
            data["file_name"] = file_name
        # Round 16: pass the WeFlow room handle to the
        # processor as ``data["chat_id"]`` so the message's
        # ``group_id`` in mai_mai is computed from the
        # STABLE session_id rather than the display
        # ``chat_name``. The ``data["chat"]`` field above
        # is still the latest WeChat label so mai_mai's
        # own display-update logic can refresh the name
        # without us rewriting it. UI-only events (no
        # WeFlow push) have empty ``session_id`` and fall
        # back to the legacy chat_name-based path.
        if real_id:
            data["chat_id"] = real_id
        # Round 17: pass the sender's WeChat wxid through
        # to the processor so the user_id sent to mai_mai
        # is keyed on the stable wxid rather than the
        # display ``sender_name`` (which changes on profile
        # / group nickname edits). Falls back to the
        # display-name path for UI-only events where
        # ``sender_wxid`` is empty.
        if sender_wxid:
            data["sender_id"] = sender_wxid
        user_nickname = str(
            getattr(event, "user_nickname", "") or ""
        ).strip()
        if user_nickname:
            data["user_nickname"] = user_nickname
        group_card_name = str(
            getattr(event, "group_card_name", "") or ""
        ).strip()
        if group_card_name:
            data["group_card_name"] = group_card_name
        avatar_url = str(getattr(event, "avatar_url", "") or "").strip()
        if avatar_url:
            data["avatar_url"] = avatar_url
        group_avatar_url = str(
            getattr(event, "group_avatar_url", "") or ""
        ).strip()
        if group_avatar_url and chat_type == "group":
            data["group_avatar_url"] = group_avatar_url
        raw = getattr(event, "raw", None)
        if isinstance(raw, dict):
            system_event = str(raw.get("weflowSystemEvent") or "").strip()
            if system_event and system_event != "pat":
                data["weflow_system_event"] = system_event
        logger.info(
            "收到微信消息 chat=%s chat_type=%s sender=%s type=%s "
            "length=%d real_id=%s sender_id=%s",
            chat_name,
            chat_type,
            sender or "unknown",
            message_type,
            len(content),
            real_id,
            sender_wxid,
        )
        # Stamp the recent-inbound cache so the send path's fail-fast
        # knows the chat is provably alive (e.g. just received a
        # message) even when WeChat's session list has not refreshed
        # yet. See ``_is_recently_active_inbound`` and the recency
        # comment near ``_RECENT_INBOUND_TTL_SECONDS``.
        self._record_recent_inbound(chat_name, session_id=real_id)
        if self.callback:
            self.callback(chat_name, data)

    def record_recent_inbound(self, chat_name, session_id="", chat_type=""):
        """Public hook for non-UI inbound sources (e.g. WeFlow SSE).

        Equivalent to the internal ``_record_recent_inbound`` stamp but
        safe to call from a different thread (the WeFlow listener runs
        in the main async loop while ``_record_recent_inbound`` is
        normally driven from the UI owner thread via ``_handle_event``).

        Round 11: also accepts an optional WeFlow ``session_id``
        (e.g. ``45808329857@chatroom``). When provided, the listener
        records ``_recent_inbound_sessions[chat_name] = session_id``
        in addition to the timestamp — this is the persistent chat
        identity that survives WeChat group renames, and it is what
        the rename-recovery path in ``_get_or_open_session`` keys on
        when the independent window's title hasn't been updated by
        WeChat. Older callers that only pass ``chat_name`` are still
        supported: an empty ``session_id`` is silently dropped.
        """
        if chat_type in {"private", "group"}:
            self._cache_detected_chat_type(
                chat_name,
                chat_type,
                source="message",
                window_title=chat_name,
            )
        self._record_recent_inbound(chat_name, session_id=session_id)
        return None

    def _record_recent_inbound(self, chat_name, session_id=""):
        """Mark ``chat_name`` as recently active in the inbound stream.

        Called from ``_handle_event`` whenever a UIA-classified message
        is dispatched, and from ``record_recent_inbound`` whenever the
        WeFlow SSE listener receives a message. Updates the bounded
        TTL cache that ``_is_recently_active_inbound`` consults to
        soften the fail-fast in
        ``_open_verified_subwindow_with_foreground``. The cache is
        bounded by ``_RECENT_INBOUND_CACHE_MAX_SIZE``; once it is
        full the oldest entry (lowest ``last_seen``) is dropped so
        the memory footprint stays small even on long-running adapters.

        A monotonic counter (``_recent_inbound_seq``) is used as a
        secondary sort key so the eviction is well-defined even when
        many records land on the same ``time.monotonic()`` tick
        (Windows millisecond clock + a burst of distinct chats). The
        write is locked so the WeFlow SSE thread can also stamp the
        cache without racing against the UI owner thread.

        Round 11: when ``session_id`` is non-empty, also records
        ``_recent_inbound_sessions[chat_name] = session_id``. This is
        the WeFlow room handle (e.g. ``45808329857@chatroom``) — the
        chat's persistent identity that survives WeChat group
        renames. ``_get_or_open_session`` uses this mapping to look up
        the cached session for a renamed chat when the independent
        window's title hasn't been updated by WeChat. An empty
        ``session_id`` is silently dropped (older callers and private
        chats without a room handle).
        """
        key = normalize_chat_name(chat_name)
        if not key:
            return
        with self._recent_inbound_lock:
            self._recent_inbound_seq += 1
            seq = self._recent_inbound_seq
            self._recent_inbound_chats[key] = (time.monotonic(), seq)
            if len(self._recent_inbound_chats) > _RECENT_INBOUND_CACHE_MAX_SIZE:
                sorted_items = sorted(
                    self._recent_inbound_chats.items(),
                    key=lambda kv: kv[1],
                    reverse=True,
                )
                self._recent_inbound_chats = dict(
                    sorted_items[:_RECENT_INBOUND_CACHE_MAX_SIZE]
                )
            sid = str(session_id or "").strip()
            if sid:
                self._recent_inbound_sessions[key] = sid
                # 重复写入时移动到末尾，便于按最近使用顺序淘汰。
                self._latest_inbound_name_by_session_id.pop(sid, None)
                self._latest_inbound_name_by_session_id[sid] = str(chat_name).strip()
                if (
                    len(self._recent_inbound_sessions)
                    > _RECENT_INBOUND_CACHE_MAX_SIZE
                ):
                    # Keep only the most recent N entries by
                    # insertion order. The session map is
                    # auxiliary — bounded so a chat flood can't
                    # bloat the dict.
                    items = list(
                        self._recent_inbound_sessions.items()
                    )
                    self._recent_inbound_sessions = dict(
                        items[-_RECENT_INBOUND_CACHE_MAX_SIZE:]
                    )
                if (
                    len(self._latest_inbound_name_by_session_id)
                    > _RECENT_INBOUND_CACHE_MAX_SIZE
                ):
                    items = list(
                        self._latest_inbound_name_by_session_id.items()
                    )
                    self._latest_inbound_name_by_session_id = dict(
                        items[-_RECENT_INBOUND_CACHE_MAX_SIZE:]
                    )

    def _refresh_session_name_from_latest_inbound(self, session):
        """窗口消失后，以真实 ID 对应的最新显示名称恢复会话。"""
        sid = str(getattr(session, "session_id", "") or "").strip()
        if not sid:
            return False
        with self._recent_inbound_lock:
            latest_name = self._latest_inbound_name_by_session_id.get(sid, "")
        latest_name = str(latest_name or "").strip()
        if not latest_name or chat_names_equal(session.name, latest_name):
            return False
        old_name = session.name
        old_key = normalize_chat_name(old_name)
        new_key = normalize_chat_name(latest_name)
        session.name = latest_name
        self._sessions[new_key] = session
        self._remember_chat_alias(old_name, latest_name)
        self._chat_aliases.setdefault(old_key, latest_name)
        logger.info(
            "独立窗口恢复前已按真实 ID 更新最新名称 "
            "session_id=%s old=%r new=%r old_hwnd=%s",
            sid,
            old_name,
            latest_name,
            session.hwnd,
        )
        return True

    def _is_recently_active_inbound(self, chat_name):
        """True if ``chat_name`` was seen in the inbound stream within
        the last ``_RECENT_INBOUND_TTL_SECONDS`` seconds.

        Used to decide whether the fail-fast in
        ``_open_verified_subwindow_with_foreground`` should wait for
        WeChat's session list to refresh (recently active → fall
        through to search) or fail fast (stale or never seen → chat is
        genuinely gone, e.g. renamed or deleted in WeChat).
        """
        key = normalize_chat_name(chat_name)
        if not key:
            return False
        with self._recent_inbound_lock:
            entry = self._recent_inbound_chats.get(key)
        if entry is None:
            return False
        last_seen = entry[0]
        return (time.monotonic() - last_seen) < _RECENT_INBOUND_TTL_SECONDS

    @staticmethod
    def _is_window_minimized(hwnd):
        """Return True if ``hwnd`` is currently in the iconic (minimized) state.

        Used by the "already_open" / cached-session restore path so the
        bot can call ``_restore_native_window`` BEFORE the UIA identity
        check runs. ``IsIconic`` returns True for minimized windows even
        when ``IsWindowVisible`` is False (Win32 hides minimized windows
        by flipping the visibility bit), so the two checks must be
        read together.
        """
        if not hwnd:
            return False
        try:
            import win32gui
        except Exception:
            return False
        is_iconic = getattr(win32gui, "IsIconic", None)
        if not callable(is_iconic):
            return False
        try:
            return bool(is_iconic(hwnd))
        except Exception:
            return False

    def _is_in_search_cooldown(self, chat_name):
        """True if ``chat_name`` is currently muted by the search-failure
        cooldown.

        After ``_SEARCH_FAIL_COOLDOWN_THRESHOLD`` consecutive
        ``ChatNotFoundError`` raises for the same chat, the listener
        mutes the chat for ``_SEARCH_FAIL_COOLDOWN_SECONDS`` seconds so
        the outbound retry loop does not burn the rest of the send
        queue on a chat that is provably unreachable (renamed,
        deleted, or whose WeFlow name no longer matches anything in
        the WeChat session list).

        The check expires the cooldown lazily — if the deadline has
        passed, the counters are cleared and the method returns
        ``False`` so the next outbound attempt goes through the full
        open path. Other chats are unaffected.
        """
        key = normalize_chat_name(chat_name)
        if not key:
            return False
        now = time.monotonic()
        with self._search_cooldown_lock:
            deadline = self._search_cooldown_until.get(key)
            if deadline is None:
                return False
            if now < deadline:
                return True
            # Cooldown expired — clean up so the next failure starts
            # from a fresh count instead of inheriting the old one.
            self._search_cooldown_until.pop(key, None)
            self._search_fail_counts.pop(key, None)
            return False

    def _record_search_failure(self, chat_name):
        """Increment the consecutive-failure counter for ``chat_name``
        and arm the cooldown once the threshold is reached.

        Called by ``_open_verified_subwindow_with_foreground`` when
        the open path raises ``ChatNotFoundError`` (both list + scroll
        + Ctrl+F search have missed). Returns ``True`` if this
        failure caused the chat to enter cooldown. A subsequent
        successful open or a stop/start cycle clears the counter.
        """
        key = normalize_chat_name(chat_name)
        if not key:
            return False
        with self._search_cooldown_lock:
            # Do not double-count while a cooldown is already active.
            # Otherwise a steady stream of inbound-triggered retries
            # during the cooldown window would push the counter past
            # the threshold and re-arm an already-armed cooldown, and
            # a successful open after the cooldown expired would
            # inherit a high count and re-arm on the very next
            # failure.
            if self._search_cooldown_until.get(key, 0.0) > time.monotonic():
                return False
            count = self._search_fail_counts.get(key, 0) + 1
            self._search_fail_counts[key] = count
            if count < _SEARCH_FAIL_COOLDOWN_THRESHOLD:
                return False
            self._search_cooldown_until[key] = (
                time.monotonic() + _SEARCH_FAIL_COOLDOWN_SECONDS
            )
            logger.warning(
                "目标连续 %d 次搜索失败，进入 %.0f 秒冷却 chat=%r，"
                "期间不再尝试打开该聊天",
                count,
                _SEARCH_FAIL_COOLDOWN_SECONDS,
                chat_name,
            )
            return True

    def _clear_search_failure(self, chat_name):
        """Clear the failure counter and the cooldown for ``chat_name``.

        Called when an open succeeds — the chat is provably reachable
        again, so any leftover counter from a previous rename storm
        should be wiped instead of inheriting into the next failure.
        """
        key = normalize_chat_name(chat_name)
        if not key:
            return
        with self._search_cooldown_lock:
            had_any = (
                self._search_fail_counts.pop(key, None) is not None
                or self._search_cooldown_until.pop(key, None) is not None
            )
        if had_any:
            logger.info(
                "搜索冷却/失败计数已清零 chat=%r",
                chat_name,
            )

    def _touch_heartbeat(self):
        if self.heartbeat:
            self.heartbeat()

    def _finish_window_cleanup(self, closed_hwnds):
        """关闭未缓存的聊天窗口，并把微信主窗口最小化。"""
        try:
            main_hwnd = (
                _find_actual_main_window(self.wx.window.hwnd)
                or self.wx.window.hwnd
            )
        except Exception:
            main_hwnd = self.wx.window.hwnd
            logger.warning("退出时重新识别微信主窗口失败，使用已绑定 HWND", exc_info=True)

        try:
            windows = _list_wechat_windows_for_main(main_hwnd)
        except Exception:
            windows = []
            logger.warning("退出时枚举微信独立窗口失败", exc_info=True)
        main_pid = _get_window_process_id(main_hwnd)
        for hwnd, title, _class_name in windows:
            if hwnd == main_hwnd or hwnd in closed_hwnds:
                continue
            try:
                if not _is_subwindow_candidate(hwnd, main_pid=main_pid):
                    continue
                root = _control_from_handle(hwnd)
                _require_subwindow_message_list(
                    root,
                    title or "<未命名独立窗口>",
                    hwnd=hwnd,
                )
                _close_window(hwnd)
                closed_hwnds.add(hwnd)
            except Exception:
                logger.debug(
                    "跳过无法验证为聊天窗口的微信顶层窗口 "
                    "hwnd=%s title=%r",
                    hwnd,
                    title,
                    exc_info=True,
                )

        try:
            minimized = _minimize_window(main_hwnd)
        except Exception:
            minimized = False
            logger.warning(
                "退出时最小化微信主窗口失败 hwnd=%s",
                main_hwnd,
                exc_info=True,
            )
        logger.info(
            "适配器停止窗口清理完成 closed_subwindows=%d "
            "main_hwnd=%s minimized=%s",
            len(closed_hwnds),
            main_hwnd,
            minimized,
        )

    def close(self):
        self._assert_owner_thread()
        if self._closed:
            return
        self._closed = True
        self.running = False
        if self.stop_event is not None:
            self.stop_event.set()
        self._stop_send_workers()
        try:
            if self.processor is not None:
                self.processor.stop()
        finally:
            with self._window_lifecycle_lock:
                closed_hwnds = set()
                # Round 15 / 方案一: adapter-stop
                # trigger. Logging this once makes it
                # trivial to grep for "方案一 适配器停止"
                # in the log to confirm the hwnd-title
                # bindings are being torn down on
                # shutdown. On next bot startup there
                # are no stale bindings to fight — the
                # open path rebuilds from scratch with
                # the current chat-list titles.
                logger.info(
                    "方案一 适配器停止：关闭所有独立窗口，hwnd-title "
                    "绑定将随进程退出全部失效 active_sessions=%d",
                    len(self._sessions),
                )
                for session in list(self._sessions.values()):
                    if session.hwnd in closed_hwnds:
                        continue
                    try:
                        if session.session_id:
                            # A verified session-id binding remains authoritative
                            # when WeChat leaves the native title at the old group
                            # name. Confirm only that the HWND is still alive.
                            _get_window_title(session.hwnd)
                        else:
                            self._require_matching_window_title(
                                session.hwnd,
                                session.name,
                            )
                        _close_window(session.hwnd)
                        with _ACTIVATION_STRATEGY_LOCK:
                            _ACTIVATION_STRATEGY_BY_HWND.pop(session.hwnd, None)
                        closed_hwnds.add(session.hwnd)
                    except Exception:
                        logger.debug(
                            "关闭微信独立聊天窗口失败 chat=%s hwnd=%s",
                            session.name,
                            session.hwnd,
                            exc_info=True,
                        )
                self._finish_window_cleanup(closed_hwnds)
                self._sessions.clear()
            self._pending_media.clear()
            # Round 11: also clear the session-id → session
            # binding map so a bot restart starts with a
            # clean state. ``_recent_inbound_sessions`` is
            # also reset because the inbound map is only
            # meaningful while sessions are live.
            self._sessions_by_session_id.clear()
            # Round 13: also clear the hwnd → session_id
            # reverse map. Paired with the forward map.
            self._hwnd_to_session_id.clear()
            self._recent_inbound_sessions.clear()
            self._latest_inbound_name_by_session_id.clear()
            self._chat_aliases.clear()
            self._window_chat_types.clear()
            self._chat_type_sources.clear()
            try:
                self.wx.disconnect()
            finally:
                self._connected = False
        logger.info("WeMai 微信客户端已断开")

    def _stop_send_workers(self):
        self._send_shutdown.set()
        with self._send_workers_lock:
            workers = list(self._send_workers.values())
        for worker in workers:
            try:
                worker.queue.put_nowait(None)
            except queue.Full:
                # Make room for shutdown and fail a command which never began.
                try:
                    pending = worker.queue.get_nowait()
                except queue.Empty:
                    pending = None
                else:
                    worker.queue.task_done()
                    if pending is not None:
                        pending.cancel_if_pending()
                        self.commands.task_done()
                with contextlib.suppress(queue.Full):
                    worker.queue.put_nowait(None)
        for worker in workers:
            if worker.thread is not threading.current_thread():
                worker.thread.join(5)


global_processor = None


def set_global_processor(processor):
    global global_processor
    global_processor = processor


def create_message_processor(**kwargs):
    from wx_Processor import MessageProcessor

    return MessageProcessor(**kwargs)


def message_callback(chat_name, message_data):
    if not global_processor:
        raise RuntimeError("消息处理器未初始化")
    result = global_processor.enqueue_message(chat_name, message_data)
    if not result.get("success"):
        error = result.get("error") or "未知错误"
        logger.error("微信消息转发失败 chat=%s error=%s", chat_name, error)
        # Let the listener retain and retry the row.  Logging-and-returning here
        # used to acknowledge a message even when SQLite rejected it.
        raise RuntimeError(f"微信消息未能持久化入队: {error}")
    return True


if __name__ == "__main__":
    listener = WeChatListener(target_chats=WX_TARGET_CHATS, callback=message_callback)
    try:
        listener.start_listening()
        while listener.is_running:
            listener.process_commands()
            listener.commands.wait(0.05)
    finally:
        listener.close()

# endregion ==============================================================
