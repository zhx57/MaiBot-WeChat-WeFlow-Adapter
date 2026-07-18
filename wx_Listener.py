"""基于微信 4.0 主窗口和独立聊天窗口的 UIAutomation 适配层。

控件定位以微信 4.0 的 ``mmui`` 层级为准：会话操作限定在
``ChatMasterView``，聊天操作限定在 ``ChatMessagePage/XSplitterView``。
wx4py 的通用选择器只作为旧版本兼容回退。
"""

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

from chat_name_utils import chat_names_equal, normalize_chat_name
from config import (
    IMAGE_AUTO_DOWNLOAD,
    IMAGE_SAVE_DIR,
    IMAGE_SAVE_TIMEOUT_SECONDS,
    MAX_MEDIA_BYTES,
    UI_QUEUE_SIZE,
    WX4PY_AUTO_CONNECT,
    WX4PY_BATCH_SIZE,
    WX4PY_SEARCH_TIMEOUT,
    WX4PY_SUBWINDOW_TIMEOUT,
    WX4PY_TAIL_SIZE,
    WX4PY_TICK,
    WX4PY_UI_LOOKUP_TIMEOUT,
    WX4PY_UI_POLL_INTERVAL,
    WX4PY_WINDOW_CHECK_INTERVAL,
    WX_BLACKLIST_MODE,
    WX_BOT_NICKNAME,
    WX_EXCLUDED_CHATS,
    WX_LISTEN_ALL_IF_EMPTY,
    WX_OPEN_WINDOWS_ON_DEMAND,
    WX_TARGET_CHATS,
)

logger = logging.getLogger(__name__)

# The Windows clipboard belongs to the desktop, not to an individual chat
# window.  Keep only clipboard preparation/paste inside this lock so sends in
# other independent windows may still confirm concurrently.
_CLIPBOARD_LOCK = threading.RLock()

_DYNAMIC_TITLE_SUFFIX = re.compile(
    r"\s*(?:\(\d+\)|（\d+）|\[\d+\])\s*$"
)
_SEARCH_GROUP_CONTACTS = "联系人"
_SEARCH_GROUP_CHATS = "群聊"
_SEARCH_GROUP_FUNCTIONS = "功能"
_SEARCH_GROUP_FREQUENT = "最常使用"
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
_UI_LOOKUP_TIMEOUT = WX4PY_UI_LOOKUP_TIMEOUT
_UI_POLL_INTERVAL = WX4PY_UI_POLL_INTERVAL
_SEARCH_TIMEOUT = WX4PY_SEARCH_TIMEOUT
_SEARCH_INPUT_STEP_DELAY = 0.1
_SEARCH_HOTKEY_ACTIVATION_DELAY = 0.3
_SEARCH_HOTKEY_OPEN_DELAY = 0.5
_SEARCH_HOTKEY_RESULT_DELAY = 0.5
_SEARCH_HOTKEY_SUBMIT_DELAY = 1.0
# Compatibility knobs retained for deployments that import them. Direct search
# intentionally performs neither an initial settle sleep nor a stability wait.
_SEARCH_INITIAL_SETTLE_DELAY = 0.3
_SEARCH_RESULT_STABLE_TIME = 0.0
_TEXT_PREPARE_TIMEOUT = 0.5
_TEXT_SEND_CONFIRM_TIMEOUT = 0.75
_FILE_PREPARE_TIMEOUT = 1.5
_FILE_SEND_CONFIRM_TIMEOUT = 1.0
_SEND_POLL_INTERVAL = min(_UI_POLL_INTERVAL, 0.01)
_WINDOW_ACTIVATION_TIMEOUT = 0.3
_WINDOW_ACTIVATION_POLL_INTERVAL = min(_UI_POLL_INTERVAL, 0.03)
_IMAGE_SAVE_MAX_ATTEMPTS = 3
_CHAT_TYPE_SOURCE_PRIORITY = {
    "fallback": 0,
    "message": 1,
    "header": 2,
    "window": 3,
    "search": 4,
    "config": 5,
}


class _UIOperationCancelled(RuntimeError):
    """The process is stopping while a bounded UI wait is in progress."""


class _SendOutcomeUnknown(RuntimeError):
    """Enter was attempted, so retrying could duplicate a delivered message."""

    retry_safe = False
    cleanup_delay = 10.0


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
            if (
                is_wechat_process
                and callable(is_visible)
                and not is_visible(hwnd)
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
            if callable(is_visible) and not is_visible(hwnd):
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
    deadline = time.monotonic() + max(float(timeout), 0.0)
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


def _force_native_window_foreground(hwnd):
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
        for attempt in range(3):
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

            if _wait_for_native_window_foreground(hwnd, 0.15):
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

            if _wait_for_native_window_foreground(hwnd, 0.15):
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

    try:
        from wx4py.core.win32 import bring_window_to_front

        bring_window_to_front(hwnd)
        if _wait_for_native_window_foreground(hwnd, 0.15):
            return True
    except Exception:
        logger.debug("wx4py 前台激活窗口失败 hwnd=%s", hwnd, exc_info=True)

    try:
        root = _control_from_handle(hwnd)
        set_active = getattr(root, "SetActive", None)
        if callable(set_active):
            set_active(waitTime=0)
            if _wait_for_native_window_foreground(hwnd, 0.15):
                return True

        switch_to = getattr(root, "SwitchToThisWindow", None)
        if callable(switch_to):
            switch_to(waitTime=0)
            if _wait_for_native_window_foreground(hwnd, 0.15):
                return True
    except Exception:
        logger.debug("UIA 激活窗口失败 hwnd=%s", hwnd, exc_info=True)

    if _force_native_window_foreground(hwnd):
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
    try:
        root = _control_from_handle(hwnd)
    except Exception:
        pass

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
            try:
                has_main_structure = (
                    _find_main_tab_bar(root) is not None
                    or _find_session_panel(root) is not None
                )
            except Exception:
                pass
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
    if current_chat_name and not current_score:
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
    if _get_uia_window_class(hwnd) == _MAIN_WINDOW_CLASS:
        return False
    return True


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


def _double_click_control(control):
    """Bring a session row into view and issue one real double-click.

    A separate preliminary ``Click`` changes the current chat and can rebuild
    Qt's accessibility row before ``DoubleClick`` runs.  Calling DoubleClick
    directly both selects the row and opens the independent window.
    """
    try:
        parent = control.GetParentControl()
        _roll_control_into_view(parent, control)
    except Exception:
        logger.debug("滚动会话项到可见区域失败", exc_info=True)

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

    try:
        import win32api
        import win32con

        rect = control.BoundingRectangle
        x = (rect.left + rect.right) // 2
        y = (rect.top + rect.bottom) // 2
        win32api.SetCursorPos((x, y))
        for _ in range(2):
            win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
            time.sleep(min(_UI_POLL_INTERVAL, 0.02))
            win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
            time.sleep(min(_UI_POLL_INTERVAL, 0.04))
        return True
    except Exception:
        logger.debug("物理双击会话项失败", exc_info=True)
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
            try:
                os.unlink(os.fspath(screenshot))
            except OSError:
                pass

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
        return WX_BOT_NICKNAME or "self"
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

            def collect(hwnd, _extra):
                try:
                    class_name = str(win32gui.GetClassName(hwnd) or "")
                    if "QWindowToolSaveBits" not in class_name:
                        return True
                    extra_windows.append(
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

        def collect_dialog(hwnd, _extra):
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
                dialogs.append(hwnd)
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

            def collect_child(hwnd, _extra):
                try:
                    class_name = str(win32gui.GetClassName(hwnd) or "")
                    text = str(win32gui.GetWindowText(hwnd) or "")
                    if class_name == "Edit":
                        edits.append((bool(text.strip()), hwnd))
                    elif class_name == "Button" and _is_save_dialog_button(text):
                        buttons.append(hwnd)
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
        try:
            os.unlink(source_path)
        except OSError:
            pass
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
            value = getattr(edit, "Value")
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


def _parse_search_result_controls(controls):
    groups = {}
    current_group = "未知"
    known_groups = {
        _SEARCH_GROUP_CONTACTS,
        _SEARCH_GROUP_CHATS,
        _SEARCH_GROUP_FUNCTIONS,
        _SEARCH_GROUP_FREQUENT,
    }
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
            or not is_result
        ):
            current_group = normalized
            groups.setdefault(current_group, [])
            continue
        if "查看全部" in name:
            continue
        if not is_result:
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
    """Return the first usable SearchContentPopover frame without stabilizing."""
    _raise_if_stopped(stop_event)
    search_box = _find_search_box(root)
    initial_result_list = None
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
                _sleep_during_search(_SEARCH_HOTKEY_RESULT_DELAY, stop_event)
        except (ImportError, AttributeError, OSError):
            # Keep the existing wx4py search path usable in environments where
            # its low-level UIAutomation module is unavailable.
            logger.debug("全局 UIAutomation 按键不可用，回退 wx4py 搜索", exc_info=True)
            return None
        except RuntimeError:
            raise
        except Exception as exc:
            raise RuntimeError("无法通过 Ctrl+F 在微信主窗口输入搜索词") from exc

        initial_result_list = _find_search_result_list(root)
        if initial_result_list is None:
            try:
                _send_global_uia_keys("{Enter}")
            except Exception as exc:
                raise RuntimeError("微信搜索结果控件不可见且回车提交失败") from exc
            logger.warning(
                "微信未暴露搜索结果控件，已通过回车提交首个匹配项 chat=%r",
                keyword,
            )
            _sleep_during_search(_SEARCH_HOTKEY_SUBMIT_DELAY, stop_event)
            return {}
    else:
        # WeChat 4.0.5+ QML search result popover is extremely short-lived.
        # Skip result-panel reading entirely: paste the keyword and press Enter
        # to jump directly to the first match in the main chat list.
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
            _sleep_during_search(_SEARCH_HOTKEY_RESULT_DELAY, stop_event)
            # Press Enter to jump to the first matching chat in the main list.
            _call_uia_without_wait(search_box.SendKeys, "{Enter}")
            _sleep_during_search(_SEARCH_HOTKEY_SUBMIT_DELAY, stop_event)
        logger.info(
            "微信搜索已通过回车提交搜索词，跳转主界面 chat=%r",
            keyword,
        )
        return {}

    # Fallback: poll for SearchContentPopover (only reached when search_box was
    # found via UIA but the Enter-submit path above was not taken, which should
    # not happen in normal operation. Kept as a safety net.)
    deadline = time.monotonic() + max(float(timeout), 0.0)
    latest = {}
    saw_result_list = False
    logged_disappearance = False
    while time.monotonic() < deadline:
        _raise_if_stopped(stop_event)
        result_list = initial_result_list or _find_search_result_list(root)
        initial_result_list = None
        if result_list is not None:
            saw_result_list = True
            logged_disappearance = False
            try:
                candidate = _parse_search_result_controls(result_list.GetChildren())
            except Exception as exc:
                raise RuntimeError("读取微信搜索结果失败") from exc
            if not _control_exists_now(result_list):
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
    if latest:
        logger.debug("微信 4.0 搜索结果等待超时，返回最后一个结果快照")
    return latest


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
    """Submit the existing query only after its confirmed result panel vanished."""
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
    """A new top-level window exists, so another double click is unsafe."""


class UICommandQueue:
    """把 Router 线程的发送请求交给微信会话工作线程执行。"""

    def __init__(self, maxsize=UI_QUEUE_SIZE):
        self._queue = queue.Queue(maxsize=maxsize)
        self._wake_event = threading.Event()

    def submit(self, action, *args, timeout=15):
        command = UICommand(action=action, args=args, timeout=float(timeout))
        try:
            self._queue.put(command, timeout=2)
        except queue.Full as exc:
            raise RuntimeError("微信命令队列已满") from exc
        self._wake_event.set()
        try:
            return command.future.result(timeout=timeout)
        except FutureTimeoutError as exc:
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
    interval: float = WX4PY_TICK
    media_failures: dict = field(default_factory=dict)
    chat_type: str = ""
    at_bottom: bool = True
    empty_read_count: int = 0
    next_identity_check_at: float = 0.0
    recovered_items: list = field(default_factory=list)


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
    sender: str = ""
    chat_type: str = ""
    avatar_url: str = ""


class _ListenerStatus:
    """提供 main.py 期望的 processor 状态接口，不创建额外 UI 线程。"""

    def __init__(self, listener):
        self._listener = listener

    @property
    def is_running(self):
        return self._listener.running

    def stop(self):
        self._listener.running = False


class WeChatListener:
    """在 WeMai UI 工作线程中管理独立聊天窗口和消息轮询。"""

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
        self._excluded_keys = {
            normalize_chat_name(name) for name in WX_EXCLUDED_CHATS if name.strip()
        }
        self._blacklist_mode = WX_BLACKLIST_MODE
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
        self._failed_target_names = []
        self._next_failed_target_retry_at = 0.0
        self._pending_media = []
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

        self.wx = WeChatClient(auto_connect=WX4PY_AUTO_CONNECT)
        if not WX4PY_AUTO_CONNECT and self.wx.connect() is not True:
            raise RuntimeError("wx4py 微信客户端连接失败")
        if not self.wx.is_connected:
            raise RuntimeError("wx4py 未连接到微信")
        self.outgoing_registry = (
            outgoing_registry
            if outgoing_registry is not None
            else getattr(self.wx, "outgoing_registry", None)
        )
        self._connected = True
        logger.info("wx4py 微信客户端连接成功")

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

    def _remember_chat_alias(self, requested_name, actual_name):
        actual_name = str(actual_name or "").strip()
        if not actual_name:
            return str(requested_name)
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

    @property
    def is_running(self):
        return self.running

    def start_listening(self):
        """打开配置会话的独立窗口，并以当前可见消息建立去重基线。"""
        self._assert_owner_thread()
        if self.running:
            return self.processor

        self.running = True
        if self.callback is None:
            logger.info("微信到 MaiBot 方向已禁用；仍将打开并监控目标独立窗口")

        # 按需打开模式：不主动打开窗口，仅准备发送通道；窗口在发送时按需打开，
        # 打开后由 _monitor_session_windows 持续检测。
        if self._open_on_demand:
            self.listening_target_names = []
            self._failed_target_names = []
            self.processor = _ListenerStatus(self)
            blacklisted = [
                name for name in self.target_names
                if normalize_chat_name(name) in self._excluded_keys
            ]
            logger.info(
                "按需打开窗口模式已启动；窗口将在发送消息时按需打开并保持检测 "
                "targets=%s excluded=%s",
                self.target_names,
                blacklisted,
            )
            self._touch_heartbeat()
            return self.processor

        if not self.target_names:
            if WX_LISTEN_ALL_IF_EMPTY:
                self.running = False
                raise RuntimeError(
                    "微信 4.x UIA 无法可靠枚举全部聊天；"
                    "WX_LISTEN_ALL_IF_EMPTY=true 时仍必须显式配置 "
                    "WX_TARGET_CHATS"
                )
            logger.info("没有有效监听目标；当前仅处理 MaiBot 到微信的消息")
            self._touch_heartbeat()
            return None

        # 过滤黑名单目标
        effective_target_names = [
            name for name in self.target_names
            if normalize_chat_name(name) not in self._excluded_keys
        ]
        blacklisted = [
            name for name in self.target_names
            if normalize_chat_name(name) in self._excluded_keys
        ]
        if blacklisted:
            logger.info("黑名单过滤已排除以下目标: %s", blacklisted)
        if not effective_target_names:
            logger.info("全部目标均被黑名单排除；当前仅处理 MaiBot 到微信的消息")
            self._touch_heartbeat()
            return None

        untyped_targets = [
            item["name"]
            for item in self.target_specs
            if item.get("type") not in {"private", "group"}
            and normalize_chat_name(item["name"]) not in self._excluded_keys
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
        self.listening_target_names = []
        self._failed_target_names = []
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
                time.monotonic() + WX4PY_WINDOW_CHECK_INTERVAL
            )
            logger.warning(
                "监听目标初始化存在失败项，程序将继续运行并稍后重试 "
                "opened=%s failed=%s",
                self.listening_target_names,
                self._failed_target_names,
            )

        self.processor = _ListenerStatus(self)
        logger.info("微信独立窗口监听已启动 chats=%s", self.listening_target_names)
        self._touch_heartbeat()
        return self.processor

    def stop_listening(self):
        self.running = False

    def process_commands(self, limit=1):
        """Collect due inbound rows, then execute one outbound UI command."""
        self._assert_owner_thread()
        if self.running:
            self._retry_failed_targets()
            if self.callback is not None:
                self._poll_due_sessions()
            else:
                self._monitor_session_windows()
        self._drain_commands(limit=limit)

    def _retry_failed_targets(self):
        """Retry startup targets independently without stopping the listener."""
        if (
            not self._failed_target_names
            or time.monotonic() < self._next_failed_target_retry_at
        ):
            return

        pending = list(self._failed_target_names)
        self._next_failed_target_retry_at = (
            time.monotonic() + WX4PY_WINDOW_CHECK_INTERVAL
        )
        for target in pending:
            try:
                self._raise_if_stopping()
                session = self._get_or_open_session(target)
            except _UIOperationCancelled:
                return
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
                self._require_matching_window_title(session.hwnd, session.name)
                session.next_identity_check_at = now + WX4PY_WINDOW_CHECK_INTERVAL
            except Exception:
                logger.warning(
                    "独立聊天窗口状态检查失败，准备恢复 chat=%s hwnd=%s",
                    session.name,
                    session.hwnd,
                    exc_info=True,
                )
                self._recover_session(session)

    def _drain_commands(self, limit=1):
        for _ in range(min(max(int(limit), 0), 1)):
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
                continue
            try:
                if not command.begin():
                    continue
                if command.action == "stop":
                    self.stop_listening()
                    command.future.set_result(True)
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
            worker.queue.put(command, timeout=2)
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
                command = worker.queue.get()
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
        """Restore the main window through wx4py's tray-aware activation path."""
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
                        "wx4py 绑定了独立窗口且无法改绑到微信主窗口"
                    ) from exc
            bind = getattr(wrapper, "bind", None)
            if not callable(bind):
                raise RuntimeError("wx4py 绑定了错误窗口且 UIAutomation 不支持重绑")
            bind(actual_main_hwnd)
            previous_hwnd = actual_main_hwnd
            logger.warning(
                "wx4py 初始 HWND 不是微信主窗口，已按 mmui::MainWindow 改绑 "
                "hwnd=%s",
                actual_main_hwnd,
            )
        activate = getattr(window, "activate", None)
        wx4py_activation_error = None
        if callable(activate):
            try:
                activated = activate()
            except Exception as exc:
                activated = False
                wx4py_activation_error = exc
                logger.debug("wx4py 主窗口激活失败", exc_info=True)
            if activated is not True:
                logger.debug("wx4py 未确认主窗口激活 hwnd=%s", previous_hwnd)

        hwnd = window.hwnd
        resolved_after_activation = (
            _find_actual_main_window(hwnd) if hwnd != previous_hwnd else None
        )
        if resolved_after_activation and resolved_after_activation != hwnd:
            wrapper = window.uia
            if hasattr(window, "_hwnd"):
                window._hwnd = resolved_after_activation
            else:
                window.hwnd = resolved_after_activation
            bind = getattr(wrapper, "bind", None)
            if not callable(bind):
                raise RuntimeError("微信恢复后无法重新绑定真实主窗口")
            bind(resolved_after_activation)
            hwnd = resolved_after_activation
            previous_hwnd = hwnd
        if not _activate_native_window(hwnd):
            error = RuntimeError(f"恢复并激活微信主窗口失败: hwnd={hwnd}")
            if wx4py_activation_error is not None:
                raise error from wx4py_activation_error
            raise error

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
                    f"wx4py 当前绑定的是独立聊天窗口，不是微信主窗口: "
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
        key = normalize_chat_name(chat_name)
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
            try:
                self._require_matching_window_title(session.hwnd, session.name)
                return session
            except Exception as exc:
                logger.warning(
                    "缓存的独立聊天窗口已失效，准备重新打开 chat=%s hwnd=%s error=%s",
                    session.name,
                    session.hwnd,
                    exc,
                )
                for session_key, candidate in list(self._sessions.items()):
                    if candidate is session:
                        self._sessions.pop(session_key, None)

        main_hwnd = self.wx.window.hwnd
        hwnd = self._open_verified_subwindow(chat_name, main_hwnd)
        resolved_name = self._resolved_chat_name(chat_name)

        self._raise_if_stopping()
        root = _control_from_handle(hwnd)
        message_list = _require_subwindow_message_list(
            root,
            chat_name,
            hwnd=hwnd,
        )
        baseline = _read_visible_items(message_list)
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

    def _open_verified_subwindow(self, chat_name, main_hwnd, attempts=2):
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
        attempts=2,
    ):
        last_error = None
        for attempt in range(1, attempts + 1):
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

            # Check again after tray restoration because activation can change
            # the main HWND and can also make a minimized independent window
            # observable to Win32/UIA.  Never click when it is already open.
            already_open = _find_window_by_title(
                operation_name,
                exclude_hwnd=main_hwnd,
            )
            if already_open:
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

            opened_from_search = item is None
            if opened_from_search:
                logger.info(
                    "会话列表中无匹配，回退到微信搜索 chat=%s attempt=%d/%d",
                    chat_name,
                    attempt,
                    attempts,
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
                        "搜索打开目标失败 chat=%s attempt=%d/%d error=%s",
                        chat_name,
                        attempt,
                        attempts,
                        exc,
                    )
                    continue

                # Search clicks rebuild the Qt accessibility tree. Rebind once
                # after search, then wait briefly for the newly added row.
                try:
                    main_hwnd, main_root = self._activate_main_window()
                except Exception as exc:
                    last_error = exc
                    logger.warning(
                        "搜索后无法恢复微信主窗口 chat=%s attempt=%d/%d error=%s",
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
                # The search click switches the main chat and refreshes the Qt
                # accessibility tree asynchronously. Let that transition settle
                # before polling the rebuilt session list.
                time.sleep(0.5)
                item = self._wait_for_session_item(
                    main_root,
                    operation_name,
                    timeout=3.0,
                    stop_event=self.stop_event,
                    allow_fuzzy=True,
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
                last_error = RuntimeError(
                    f"搜索打开后左侧会话列表仍无匹配: {chat_name}"
                )
                logger.warning(
                    "搜索后会话项未出现 chat=%s attempt=%d/%d",
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
            if opened_from_search:
                try:
                    parent = item.GetParentControl()
                    if not _roll_control_into_view(parent, item):
                        logger.debug(
                            "搜索后会话项未能确认滚动到可见区域 chat=%s",
                            chat_name,
                        )
                except Exception:
                    logger.debug(
                        "搜索后滚动会话项到可见区域失败 chat=%s",
                        chat_name,
                        exc_info=True,
                    )
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
                return self._wait_for_subwindow(
                    operation_name,
                    main_hwnd,
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

        raise RuntimeError(
            f"无法打开标题匹配的独立聊天窗口: {chat_name}; "
            f"last_error={last_error}"
        ) from last_error

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
                # Ctrl+F may have already submitted the first match with Enter.
                # Rebinding and exact session-row matching is handled by the
                # caller, which is also the only reliable path when QML hides
                # both the search field and SearchContentPopover from UIA.
                if using_direct_controls:
                    _clear_direct_search(root)
                else:
                    self._clear_search_safely(chat_window)
                logger.info(
                    "微信搜索未返回可见结果控件，转由主界面匹配会话项 chat=%r",
                    chat_name,
                )
                return chat_name
            result = self._find_exact_search_result(
                results,
                chat_name,
                target_type,
                allow_contains=False,
            )
            if result is None:
                raise RuntimeError(
                    f"搜索结果中无可唯一匹配的目标: {chat_name}"
                )
            result_list = None
            actual_name = self._search_result_actual_name(result, chat_name)
            clicked = self._click_search_result(
                result.ctrl,
                result_list,
                immediate=using_direct_controls,
            )
            submitted_with_enter = False
            if (
                not clicked
                and using_direct_controls
                and _find_search_result_list(root) is None
            ):
                submitted_with_enter = _submit_direct_search_with_enter(
                    root,
                    chat_name,
                )
            if not clicked and not submitted_with_enter:
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
            ]
        elif target_type == "group":
            group_specs = [
                (_SEARCH_GROUP_CHATS, [_SEARCH_GROUP_CHATS]),
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
        timeout=WX4PY_SUBWINDOW_TIMEOUT,
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
            for candidate, title, _class_name in windows:
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
            # 防御性过滤：黑名单中的目标不轮询接收消息
            if normalize_chat_name(name) in self._excluded_keys:
                continue
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
        # WX4PY_BATCH_SIZE used to truncate this list.  The omitted sessions
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
                self._require_matching_window_title(session.hwnd, session.name)
                session.next_identity_check_at = now + WX4PY_WINDOW_CHECK_INTERVAL
            except Exception as exc:
                session.fail_count += 1
                session.next_scan_at = now + min(
                    max(WX4PY_TICK, 0.2) * session.fail_count,
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
                session.next_scan_at = time.monotonic() + min(WX4PY_TICK, 0.03)
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
            # of any burst larger than WX4PY_TAIL_SIZE.  The tail limit applies
            # only to locating image rows that are already pending a retry.
            visible_tail = (
                visible_items[-WX4PY_TAIL_SIZE:]
                if WX4PY_TAIL_SIZE > 0
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
                max(WX4PY_TICK, 0.2) * session.fail_count,
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
        """Build and durably hand off one already-classified inbound row."""
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
                try:
                    os.unlink(temporary_source)
                except OSError:
                    pass
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
        session.interval = WX4PY_TICK
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
            main_hwnd = self.wx.window.hwnd
            hwnd = _find_window_by_title(
                session.name,
                exclude_hwnd=main_hwnd,
            )
            if hwnd:
                try:
                    self._require_matching_window_title(hwnd, session.name)
                except _WrongChatWindow:
                    hwnd = None
            if not hwnd:
                logger.warning("独立聊天窗口已消失，准备重新打开 chat=%s", session.name)
                hwnd = self._open_verified_subwindow(session.name, main_hwnd)
            self._require_matching_window_title(hwnd, session.name)
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
            session.hwnd = hwnd
            session.root = root
            session.message_list = message_list
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
                time.monotonic() + WX4PY_WINDOW_CHECK_INTERVAL
            )
            session.fail_count = 0
            session.next_scan_at = time.monotonic() + WX4PY_TICK
            logger.info(
                "已重新绑定独立聊天窗口 chat=%s hwnd=%s recovered=%d anchored=%s",
                session.name,
                hwnd,
                len(recovered),
                anchored,
            )
            return True
        except Exception as exc:
            logger.warning("重新绑定独立聊天窗口失败 chat=%s error=%s", session.name, exc)
            return False

    @staticmethod
    def _activate_session_window(session):
        WeChatListener._require_matching_window_title(session.hwnd, session.name)
        if not _activate_native_window(session.hwnd):
            raise RuntimeError(
                f"无法恢复并激活独立聊天窗口: {session.name} "
                f"hwnd={session.hwnd} "
                f"foreground_hwnd={_get_native_foreground_window()}"
            )
        WeChatListener._require_matching_window_title(session.hwnd, session.name)

    def send(self, receiver, kind, data):
        """在目标独立聊天窗口中发送文本、图片或文件。"""
        self._assert_owner_thread()
        return self._send_from_worker(receiver, kind, data)

    def _send_from_worker(self, receiver, kind, data):
        """Send from one chat-bound worker after that thread initializes UIA."""
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
        """Send while holding only this receiver's serialization lock."""

        started_at = time.monotonic()
        with self._window_lifecycle_lock:
            session = self._get_or_open_session(receiver)
        registry = self.outgoing_registry
        content = str(data) if kind == "text" else None
        paths = (
            list(data) if isinstance(data, (list, tuple)) else [data]
        ) if kind != "text" else None
        last_error = None

        for attempt in range(1, 3):
            reservation_ids = []

            def reserve_outgoing():
                if registry is None:
                    return
                if kind == "text":
                    reserve = getattr(registry, "reserve", None)
                    if callable(reserve):
                        reservation_ids.append(
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
                        reservation_ids.append(
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
                try:
                    self._require_matching_window_title(
                        session.hwnd,
                        session.name,
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
                with self._foreground_ui_lock:
                    self._activate_session_window(session)
                    edit = _find_chat_input(session.root)
                    if not edit or not _control_exists_now(edit):
                        raise RuntimeError(
                            f"独立窗口中未找到聊天输入框: {receiver}"
                        )

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

                if result is not True:
                    cancel = getattr(registry, "cancel", None)
                    if callable(cancel):
                        cancel(reservation_ids)
                    reservation_ids = []
                    raise RuntimeError(
                        f"wx4py 独立窗口发送返回失败: "
                        f"target={receiver!r} kind={kind!r}"
                    )
                commit = getattr(registry, "commit", None)
                if callable(commit):
                    commit(reservation_ids)
                logger.info(
                    "微信发送完成 target=%s type=%s attempt=%d elapsed=%.3fs",
                    receiver,
                    kind,
                    attempt,
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
            f"wx4py 独立窗口发送失败 target={receiver!r} kind={kind!r}: "
            f"{last_error}"
        ) from last_error

    def _handle_event(self, event):
        chat_name = str(event.group)
        content = "" if event.content is None else str(event.content)
        message_type = str(getattr(event, "message_type", "text") or "text")
        if message_type not in {"text", "image"}:
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
        data = {
            "chat": chat_name,
            "chat_type": chat_type,
            "sender": sender or "unknown",
            "type": message_type,
            "content": content,
            "timestamp": event.timestamp,
        }
        avatar_url = str(getattr(event, "avatar_url", "") or "").strip()
        if avatar_url:
            data["avatar_url"] = avatar_url
        logger.info(
            "收到微信消息 chat=%s chat_type=%s sender=%s type=%s length=%d",
            chat_name,
            chat_type,
            data["sender"],
            message_type,
            len(content),
        )
        if self.callback:
            self.callback(chat_name, data)

    def _touch_heartbeat(self):
        if self.heartbeat:
            self.heartbeat()

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
                for session in list(self._sessions.values()):
                    if session.hwnd in closed_hwnds:
                        continue
                    try:
                        self._require_matching_window_title(
                            session.hwnd,
                            session.name,
                        )
                        _close_window(session.hwnd)
                        closed_hwnds.add(session.hwnd)
                    except Exception:
                        logger.debug(
                            "关闭微信独立聊天窗口失败 chat=%s hwnd=%s",
                            session.name,
                            session.hwnd,
                            exc_info=True,
                        )
                self._sessions.clear()
            self._pending_media.clear()
            self._chat_aliases.clear()
            self._window_chat_types.clear()
            self._chat_type_sources.clear()
            try:
                self.wx.disconnect()
            finally:
                self._connected = False
        logger.info("wx4py 微信客户端已断开")

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
                try:
                    worker.queue.put_nowait(None)
                except queue.Full:
                    pass
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
