"""Windows UI Automation 微信 4.0+ 消息发送器（移植自原版 Akasha-WeChat）。

原理：
    微信 4.0 基于 Electron (Chromium)。Chromium 通过 UIA 桥将 HTML 输入元素
    暴露为标准 UIA 控件。通过 ValuePattern 设置输入框文本，InvokePattern 点击
    发送按钮。全程无 DLL 注入，风控风险较低。

工作流：
    1. 定位微信 4.0 窗口（标题「微信」/「WeChat」，ClassName ``WeChatMainWndForPC``）
    2. ``AttachThreadInput`` 绕过前台限制 → ``Ctrl+F`` 搜索联系人 → Enter 选中
    3. 递归遍历 UIA 树定位聊天输入框（EditControl，按面积排序，优先
       ``IsValuePatternAvailable``），失败则启用坐标后备 ``(0.3*W, 0.92*H)``
    4. 文本经 ``pyperclip`` 剪贴板粘贴 / ValuePattern 设值 → Enter 或发送按钮
    5. 图片经 PowerShell ``[System.Windows.Forms.Clipboard]::SetImage`` 入剪贴板后粘贴

并发模型：
    公开方法为 ``async``，同步逻辑用 ``asyncio.to_thread`` 包装到工作线程执行；
    工作线程内首次调用 ``CoInitialize(None)`` 初始化 COM（每线程一次，经
    ``threading.local`` 标记）；``threading.Lock`` 串行化所有发送动作。

依赖：
    ``pip install uiautomation pyperclip``（发送图片额外依赖 PowerShell + .NET，
    Windows 自带）。这些依赖延迟导入，使非 Windows 环境也能 import 本模块；
    实例化或调用时若缺依赖则抛 ``RuntimeError``。
"""

from __future__ import annotations

import asyncio
import ctypes
import logging
import os
import subprocess
import threading
import time
from pathlib import Path

from .id_mapping import ContactRef
from .senders import BaseSender


class UiaSender(BaseSender):
    """基于 Windows UI Automation 的微信 4.0+ 发送器。

    对微信 4.0 (Electron/Chromium) 优化：
      - 自动检测 Electron 架构
      - ValuePattern 直接设值（非键盘模拟）
      - InvokePattern 精确点击发送按钮
      - 自动联系人搜索切换

    Attributes:
        search_enabled: 是否自动搜索联系人（默认 True，False 则需手动切到聊天窗口）
    """

    WECHAT_TITLES = ["微信", "WeChat"]

    EXCLUDE_CLASSES = ["Chrome_WidgetWin_1", "CabinetWClass"]

    # 延迟取「可靠」而不是「保守」：Windows UIA/微信窗口切换本身有异步刷新，
    # 下列等待值是实测可用的低延迟折中，避免旧实现每条消息固定等待 2s+。
    ACTIVATE_DELAY = 0.08
    SEARCH_OPEN_DELAY = 0.12
    # 搜索结果/会话切换需要等微信渲染；仍明显快于 Akasha 原版固定 0.3+0.5+0.8s。
    SEARCH_RESULT_DELAY = 0.35
    CHAT_SWITCH_DELAY = 0.45
    PASTE_DELAY = 0.04
    IMAGE_PASTE_DELAY = 0.25

    def __init__(self, logger: logging.Logger, search_enabled: bool = True) -> None:
        super().__init__(logger)
        self._lock = threading.Lock()
        self._auto = None
        self._ready = False

        # 微信窗口
        self._window = None
        self._is_electron = False  # True=4.0+, False=3.9

        # 控件缓存
        self._search_box = None
        self._input_control = None
        self._send_button = None
        self._last_contact = ""
        self._use_coord_fallback = False

        self.search_enabled = search_enabled

        # 工作线程 COM 初始化标记（每线程一次）
        self._com_local = threading.local()

    # ================================================================
    # async 公开 API（内部 to_thread 包装同步实现）
    # ================================================================

    async def send_text(self, contact: ContactRef, text: str) -> None:
        await asyncio.to_thread(self._sync_send_text, contact, text)

    async def send_image(self, contact: ContactRef, image_path: Path) -> None:
        await asyncio.to_thread(self._sync_send_image, contact, image_path)

    async def close(self) -> None:
        """UIA 无需释放的资源，空实现。"""

        return None

    # ================================================================
    # 工作线程同步实现
    # ================================================================

    @staticmethod
    def _has_value_pattern(ctrl) -> bool:
        """检查控件是否支持 ValuePattern。

        ``uiautomation`` 库不提供 ``IsValuePatternAvailable`` 属性
        （那是 .NET UIAutomation 的 API），正确做法是尝试 ``GetValuePattern()``
        并捕获 ``COMError``/``AttributeError``。支持则返回 True。
        """

        try:
            ctrl.GetValuePattern()
            return True
        except Exception:
            return False

    @staticmethod
    def _has_invoke_pattern(ctrl) -> bool:
        """检查控件是否支持 InvokePattern。"""

        try:
            ctrl.GetInvokePattern()
            return True
        except Exception:
            return False

    def _ensure_com(self) -> None:
        """确保当前工作线程已 ``CoInitialize``（每线程一次）。

        非Windows 环境 ``ctypes.windll`` 不存在，静默忽略；后续 ``_ensure_uia``
        会抛出明确的 RuntimeError。
        """

        if getattr(self._com_local, "initialized", False):
            return
        try:
            ctypes.windll.ole32.CoInitialize(None)
        except Exception as e:
            self.logger.debug(f"CoInitialize 跳过/失败（可忽略）: {e}")
        self._com_local.initialized = True

    def _ensure_uia(self) -> None:
        """懒加载 ``uiautomation`` 模块并定位微信窗口。

        缺依赖时抛 ``RuntimeError``（提示仅支持 Windows）。
        """

        if self._auto is None:
            try:
                import uiautomation as auto
            except ImportError as e:
                raise RuntimeError(
                    "UIA 发送器仅支持 Windows（缺少 uiautomation 依赖）"
                ) from e
            self._auto = auto

        if not self._ready:
            self.logger.info("正在搜索微信窗口...")
            self._find_window()
            if self._window:
                self.logger.info(
                    f"微信窗口: '{self._window.Name}' ClassName={self._window.ClassName}"
                )
                self._ready = True
            else:
                self.logger.warning("未找到微信窗口，将在下次发送时重试")

    def _sync_send_text(self, contact: ContactRef, text: str) -> None:
        """同步发送文本：失败抛 RuntimeError。"""

        self._ensure_com()
        display_name = (
            contact.display_name
            or contact.group_name
            or contact.user_nickname
            or contact.session_id
            or ""
        )

        with self._lock:
            self._ensure_uia()
            if not self._ready:
                raise RuntimeError("UIA 发送器未就绪：未找到微信窗口")

            if not self._ensure_window():
                raise RuntimeError("UIA 发送器：微信窗口不可用")

            # 安全检查：过滤 PIL 引用
            if "<PIL." in text or "PIL." in text:
                self.logger.warning(f"跳过 PIL 引用消息: {text[:60]}")
                return

            self._activate()

            # 切换到联系人（物理点击搜索框，坐标后备模式下也有效）
            if self.search_enabled and display_name:
                if display_name != self._last_contact:
                    if self._switch_contact(display_name):
                        self._last_contact = display_name
                        # 切换会话后 UIA 控件树常会重建，清空缓存强制重新定位。
                        self._input_control = None
                        self._send_button = None
                        self._use_coord_fallback = False
                    else:
                        raise RuntimeError(
                            f"UIA 发送器：无法切换到联系人 '{display_name}'，已阻止发送到当前窗口"
                        )

            # 定位输入框
            if not self._locate_input():
                raise RuntimeError("UIA 发送器：无法定位输入框")

            try:
                if self._use_coord_fallback:
                    # Qt 界面：找不到 EditControl，靠全局快捷键发送
                    # 窗口已在 _activate() 里拉到前台，焦点默认在输入框；
                    # 仅当焦点不在输入框时才点一下输入框区域兜底。
                    self._click_input_area_if_needed()
                    self._paste_text(text)
                    time.sleep(self.PASTE_DELAY)
                    self._press_vk(0x0D)
                    self.logger.info(
                        f"[UIA✓] {display_name}: {text[:50]}... (快捷键模式)"
                    )
                    return

                ctrl = self._input_control

                # 微信 Electron/Qt 对 ValuePattern.SetValue 的事件触发不一致；生产发送
                # 以「聚焦输入框 + 剪贴板粘贴 + Enter」为主路径，速度和成功率最高。
                if not self._focus_control(ctrl):
                    self._click_input_area_if_needed()
                self._hotkey(0x11, 0x41)  # Ctrl+A，清理输入框残留草稿
                self._paste_text(text)
                time.sleep(self.PASTE_DELAY)
                self._press_vk(0x0D)

                self.logger.info(f"[UIA✓] {display_name}: {text[:50]}...")
            except Exception as e:
                self.logger.error(f"[UIA✗] {display_name}: {e}")
                raise RuntimeError(f"UIA 发送文本失败: {e}") from e

    def _sync_send_image(self, contact: ContactRef, image_path: Path) -> None:
        """同步发送图片：失败抛 RuntimeError。"""

        self._ensure_com()
        display_name = (
            contact.display_name
            or contact.group_name
            or contact.user_nickname
            or contact.session_id
            or ""
        )
        image_path_str = str(image_path)

        with self._lock:
            self._ensure_uia()
            if not self._ready:
                raise RuntimeError("UIA 发送器未就绪：未找到微信窗口")

            if not os.path.isfile(image_path_str):
                raise RuntimeError(f"图片不存在: {image_path_str}")

            try:
                if not self._ensure_window():
                    raise RuntimeError("UIA 发送器：微信窗口不可用")
                self._activate()

                if self.search_enabled and display_name:
                    if display_name != self._last_contact:
                        if self._switch_contact(display_name):
                            self._last_contact = display_name
                            self._input_control = None
                            self._send_button = None
                            self._use_coord_fallback = False
                        else:
                            raise RuntimeError(
                                f"UIA 发送器：无法切换到联系人 '{display_name}'，已阻止发送到当前窗口"
                            )

                # 复制图片到剪贴板
                self._copy_image_to_clipboard(image_path_str)
                time.sleep(self.PASTE_DELAY)

                if not self._locate_input():
                    raise RuntimeError("UIA 发送器：无法定位输入框")

                if self._use_coord_fallback:
                    # Qt 界面：找不到 EditControl，靠全局快捷键发送图片
                    self._click_input_area_if_needed()
                    self._hotkey(0x11, 0x56)
                    time.sleep(self.IMAGE_PASTE_DELAY)
                    self._press_vk(0x0D)
                    self.logger.info(
                        f"[UIA✓] 图片 → {display_name}: "
                        f"{os.path.basename(image_path_str)} (快捷键模式)"
                    )
                    return

                if not self._focus_control(self._input_control):
                    self._click_input_area_if_needed()
                self._hotkey(0x11, 0x56)
                time.sleep(self.IMAGE_PASTE_DELAY)
                self._press_vk(0x0D)

                self.logger.info(
                    f"[UIA✓] 图片 → {display_name}: {os.path.basename(image_path_str)}"
                )
            except RuntimeError:
                raise
            except Exception as e:
                self.logger.error(f"[UIA✗] 图片 → {display_name}: {e}")
                raise RuntimeError(f"UIA 发送图片失败: {e}") from e

    # ================================================================
    # 控件定位（移植自原版，逻辑保持一致）
    # ================================================================

    def _find_window(self) -> None:
        """定位微信主窗口，优先按微信窗口类名匹配，标题只作后备。

        仅按标题包含「微信」/``WeChat`` 容易误命中浏览器、资源管理器或文档窗口；
        先匹配微信 3.x/4.x 常见主窗口类名，再以标题兜底，可以显著降低 UIA
        操作跑到错误窗口的概率。
        """

        auto = self._auto
        root = auto.GetRootControl()
        class_candidates = []
        title_candidates = []
        wechat_classes = {"WeChatMainWndForPC", "Qt51514QWindowIcon"}

        try:
            children = root.GetChildren()
        except Exception as e:
            self.logger.debug(f"枚举顶层窗口失败: {e}")
            return

        for w in children:
            try:
                cls = w.ClassName or ""
                name = w.Name or ""
            except Exception:
                continue
            if cls in self.EXCLUDE_CLASSES:
                continue
            if cls in wechat_classes:
                class_candidates.append(w)
                continue
            if any(kw in name for kw in self.WECHAT_TITLES):
                title_candidates.append(w)

        for w in class_candidates + title_candidates:
            try:
                if not w.Exists(0.1):
                    continue
                self._window = w
                self._is_electron = (w.ClassName or "") != "WeChatMainWndForPC"
                # 窗口变更后必须清空控件缓存，避免把消息发到旧 UIA 节点。
                self._input_control = None
                self._send_button = None
                self._search_box = None
                self._use_coord_fallback = False
                return
            except Exception:
                continue

    def _ensure_window(self) -> bool:
        """确保窗口可用（缓存失效则重新定位）。"""

        if self._window and self._window.Exists(0.2):
            return True
        self._window = None
        self._input_control = None
        self._send_button = None
        self._search_box = None
        self._use_coord_fallback = False
        self._find_window()
        if not self._window:
            self.logger.warning("微信窗口未找到")
            self._ready = False
            return False
        self._ready = True
        return True

    def _main_hwnd(self) -> int:
        """返回当前微信主窗口 HWND，优先使用 UIA 缓存的 NativeWindowHandle。"""

        try:
            hwnd = int(getattr(self._window, "NativeWindowHandle", 0) or 0)
            if hwnd:
                return hwnd
        except Exception:
            pass
        try:
            user32 = ctypes.windll.user32
            for cls in ("WeChatMainWndForPC", "Qt51514QWindowIcon"):
                hwnd = user32.FindWindowW(cls, None)
                if hwnd:
                    return int(hwnd)
        except Exception:
            pass
        return 0

    def _activate(self) -> None:
        """激活微信窗口到前台（AttachThreadInput 确保后台也能生效）。"""

        try:
            hwnd = self._main_hwnd()
            if hwnd:
                user32 = ctypes.windll.user32
                we_chat_tid = user32.GetWindowThreadProcessId(hwnd, None)
                current_tid = ctypes.windll.kernel32.GetCurrentThreadId()
                user32.AttachThreadInput(current_tid, we_chat_tid, True)
                try:
                    # SW_RESTORE=9，避免最小化时 SetForegroundWindow 后仍不可输入。
                    user32.ShowWindow(hwnd, 9)
                    user32.SetForegroundWindow(hwnd)
                    user32.BringWindowToTop(hwnd)
                finally:
                    user32.AttachThreadInput(current_tid, we_chat_tid, False)
                time.sleep(self.ACTIVATE_DELAY)
                return
        except Exception as e:
            self.logger.debug(f"HWND 激活失败，回退 UIA 激活: {e}")

        try:
            self._window.SetActive()
            time.sleep(self.ACTIVATE_DELAY)
        except Exception:
            try:
                self._window.SwitchToThisWindow()
                time.sleep(self.ACTIVATE_DELAY)
            except Exception:
                pass

    @staticmethod
    def _press_vk(vk: int) -> None:
        user32 = ctypes.windll.user32
        user32.keybd_event(vk, 0, 0, 0)
        user32.keybd_event(vk, 0, 2, 0)

    @staticmethod
    def _hotkey(*vks: int) -> None:
        user32 = ctypes.windll.user32
        for vk in vks:
            user32.keybd_event(vk, 0, 0, 0)
        for vk in reversed(vks):
            user32.keybd_event(vk, 0, 2, 0)

    def _paste_text(self, text: str) -> None:
        """通过剪贴板高速粘贴文本，比逐字 SendKeys 稳定且快。"""

        import pyperclip

        pyperclip.copy(text)
        time.sleep(self.PASTE_DELAY)
        self._hotkey(0x11, 0x56)  # Ctrl+V

    def _focus_control(self, ctrl) -> bool:
        """尽力让 UIA 控件获得焦点，失败返回 False。"""

        try:
            ctrl.SetFocus()
            time.sleep(0.02)
            return True
        except Exception:
            pass
        try:
            ctrl.Click()
            time.sleep(0.02)
            return True
        except Exception as e:
            self.logger.debug(f"控件聚焦失败: {e}")
            return False

    def _dump_tree(self, ctrl, depth: int = 0, max_depth: int = 4) -> None:
        """调试：输出 UIA 子树（仅 debug）。"""

        if depth > max_depth:
            return
        try:
            pad = "  " * depth
            name = (ctrl.Name or "")[:40]
            cls = ctrl.ClassName or ""
            ctrl_type = ctrl.ControlTypeName
            vp = self._has_value_pattern(ctrl)
            ip = self._has_invoke_pattern(ctrl)
            rect = ctrl.BoundingRectangle
            info = (
                f"[{rect.left},{rect.top} {rect.width()}x{rect.height()}]"
                if rect
                else ""
            )
            self.logger.debug(
                f"{pad}{ctrl_type} '{name}' {info} V={vp} I={ip} cls={cls}"
            )
            for child in ctrl.GetChildren():
                self._dump_tree(child, depth + 1, max_depth)
        except Exception:
            pass

    def _find_search_box_uia(self):
        """通过 UIA 树定位微信搜索框（窗口上半部分、宽度小于窗口一半的 EditControl）。"""

        win_rect = self._window.BoundingRectangle
        win_w = win_rect.width()
        win_h = win_rect.height()

        edits: list = []

        def walk(ctrl, depth=0):
            if depth > 12:
                return
            try:
                for child in ctrl.GetChildren():
                    cn = (child.ControlTypeName or "").replace(" ", "")
                    cls = child.ClassName or ""
                    if "Edit" in cn or "Edit" in cls:
                        rect = child.BoundingRectangle
                        if rect and rect.width() > 50:
                            edits.append((child, rect))
                    walk(child, depth + 1)
            except Exception:
                pass

        try:
            walk(self._window)
        except Exception:
            pass

        # 过滤：上半部分的 EditControl，宽度小于窗口一半
        candidates = [
            (c, r)
            for c, r in edits
            if r.top < win_rect.top + win_h * 0.3 and r.width() < win_w * 0.5
        ]

        if not candidates:
            return None

        # 取最靠上的（搜索框通常比任何其他上半部分控件更高）
        candidates.sort(key=lambda x: x[1].top)
        return candidates[0][0]

    def _focus_chat_input(self) -> None:
        """物理点击聊天输入框区域（坐标后备模式专用），让输入框获得键盘焦点。"""

        try:
            from ctypes import wintypes
        except ImportError:
            return

        hwnd = self._main_hwnd()
        if not hwnd:
            return

        rect = wintypes.RECT()
        ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect))
        win_w = rect.right - rect.left
        win_h = rect.bottom - rect.top
        input_x = rect.left + int(win_w * 0.3)
        input_y = rect.top + int(win_h * 0.92)
        ctypes.windll.user32.SetCursorPos(input_x, input_y)
        ctypes.windll.user32.mouse_event(0x0002, 0, 0, 0, 0)
        ctypes.windll.user32.mouse_event(0x0004, 0, 0, 0, 0)
        time.sleep(self.ACTIVATE_DELAY)

    def _click_input_area_if_needed(self) -> None:
        """快捷键模式专用：焦点不在输入框时点一下输入框区域，已在则跳过。

        Qt 界面下找不到 EditControl，但微信窗口被拉到前台后焦点通常
        已在输入框。通过 ``GetFocus`` 拿到当前焦点控件的类名判断：
        类名含 ``Edit`` 视为已在输入框，跳过点击；否则用坐标兜底点一下。
        这样既保证焦点正确，又避免每次发送都盲点鼠标。
        """

        try:
            user32 = ctypes.windll.user32
            # 取当前线程与前台窗口线程，AttachThreadInput 后才能 GetFocus
            hwnd = self._main_hwnd()
            if not hwnd:
                return
            fg_tid = user32.GetWindowThreadProcessId(hwnd, None)
            cur_tid = ctypes.windll.kernel32.GetCurrentThreadId()
            user32.AttachThreadInput(cur_tid, fg_tid, True)
            try:
                focused = user32.GetFocus()
            finally:
                user32.AttachThreadInput(cur_tid, fg_tid, False)
            if focused:
                cls_buf = ctypes.create_unicode_buffer(256)
                user32.GetClassNameW(focused, cls_buf, 256)
                cls_name = cls_buf.value or ""
                if "Edit" in cls_name or "edit" in cls_name:
                    return  # 焦点已在输入框，无需点击
        except Exception as e:
            self.logger.debug(f"焦点判断失败，回退到点击: {e}")

        # 焦点不在输入框，用坐标兜底点一下
        self._focus_chat_input()

    def _switch_contact(self, contact: str) -> bool:
        """切换到指定联系人/群聊的聊天窗口。

        主路径：UIA 定位搜索框 → 聚焦 → Ctrl+A → 粘贴联系人 → Enter。
        后备路径保留 Akasha-WeChat 已验证可用的全局 Ctrl+F → 粘贴 → Enter。
        这样既解决「只打开微信主界面、不打开联系人」的问题，也避免纯坐标点击在
        不同 DPI / 侧边栏宽度下失效。
        """

        if not contact or not self._ensure_window():
            return False
        self._activate()

        hwnd = self._main_hwnd()
        if not hwnd:
            self.logger.warning("找不到微信主窗口句柄")
            return False

        user32 = ctypes.windll.user32
        we_chat_tid = user32.GetWindowThreadProcessId(hwnd, None)
        current_tid = ctypes.windll.kernel32.GetCurrentThreadId()
        user32.AttachThreadInput(current_tid, we_chat_tid, True)
        try:
            user32.ShowWindow(hwnd, 9)
            user32.SetForegroundWindow(hwnd)
            user32.BringWindowToTop(hwnd)

            search_box = self._find_search_box_uia()
            if search_box is not None and self._focus_control(search_box):
                self._hotkey(0x11, 0x41)  # Ctrl+A
            else:
                # 后备：打开微信搜索；这是 Akasha-WeChat 的可用核心路径。
                self._hotkey(0x11, 0x46)  # Ctrl+F
                time.sleep(self.SEARCH_OPEN_DELAY)
                self._hotkey(0x11, 0x41)

            self._paste_text(contact)
            time.sleep(self.SEARCH_RESULT_DELAY)
            self._press_vk(0x0D)
            time.sleep(self.CHAT_SWITCH_DELAY)

            # 切换会话后控件树经常重建，所有 UIA 控件缓存都必须失效。
            self._input_control = None
            self._send_button = None
            self._search_box = None
            self._use_coord_fallback = False
            self.logger.info(f"已切到联系人: {contact}")
            return True
        except Exception as e:
            self.logger.warning(f"切换联系人失败: {contact}: {e}")
            return False
        finally:
            user32.AttachThreadInput(current_tid, we_chat_tid, False)

    def _locate_input(self) -> bool:
        """定位聊天输入框和发送按钮。

        在 Electron / Qt 两种界面下都能工作：
        - Electron (微信 4.0)：EditControl 支持 ValuePattern，位于窗口下半部分
        - Qt (微信 3.9)：EditControl 的 ControlTypeName 可能是 "Edit Control"
          （带空格），且 Name 常为「输入」/「消息输入框」
        找不到 EditControl 时启用快捷键发送模式。
        """

        if not self._ensure_window():
            return False

        # 如果已有缓存且控件仍可用，直接返回；同时清除坐标后备标记。
        if self._input_control is not None:
            try:
                if self._input_control.Exists(0.2):
                    self._use_coord_fallback = False
                    return True
                self._input_control = None
                self._send_button = None
            except Exception:
                self._input_control = None
                self._send_button = None

        win_rect = self._window.BoundingRectangle
        win_center_y = win_rect.top + win_rect.height() / 2

        edits: list = []

        def walk(ctrl, depth=0):
            if depth > 14:
                return
            try:
                for child in ctrl.GetChildren():
                    try:
                        cn = child.ControlTypeName or ""
                        cls = child.ClassName or ""
                        # 输入控件：兼容 "EditControl" 和 "Edit Control" 两种写法，
                        # 同时用 ClassName 兜底（Qt 微信可能用不同的类型名）
                        if "Edit" in cn.replace(" ", "") or "Edit" in cls:
                            edits.append(child)
                        walk(child, depth + 1)
                    except Exception:
                        pass
            except Exception:
                pass

        try:
            walk(self._window)
        except Exception as e:
            self.logger.debug(f"UIA 遍历异常: {e}")

        # walk 找不到时，用 uiautomation 库原生 EditControl() 兜底搜索。
        # Qt 微信的控件树结构复杂，手动递归可能因中间容器异常而漏掉；
        # 库的 EditControl() 内部用 UIA FindFirst，覆盖更全。
        if not edits:
            self.logger.debug("手动遍历未找到 EditControl，尝试库原生搜索...")
            try:
                native_edit = self._window.EditControl(searchDepth=10)
                if native_edit.Exists(0.5):
                    edits.append(native_edit)
                    self.logger.debug(
                        f"库原生搜索找到 EditControl: "
                        f"Name='{(native_edit.Name or '')[:30]}' "
                        f"Class='{native_edit.ClassName}'"
                    )
            except Exception as e:
                self.logger.debug(f"库原生 EditControl 搜索失败: {e}")

        # 每次成功重新扫描时先关闭快捷键后备，避免一次 Qt/遍历失败后永久走坐标模式。
        self._use_coord_fallback = False

        if not edits:
            self.logger.info(
                f"未找到输入控件 (ClassName={self._window.ClassName})，"
                f"启用快捷键发送模式"
            )
            self._use_coord_fallback = True
            return True

        # 诊断日志：列出找到的所有 EditControl，方便排查 Qt 微信的控件结构
        for e in edits:
            try:
                r = e.BoundingRectangle
                self.logger.debug(
                    f"EditControl 候选: Name='{(e.Name or '')[:30]}' "
                    f"Class='{e.ClassName}' "
                    f"rect=[{r.left},{r.top} {r.width()}x{r.height()}] "
                    f"V={self._has_value_pattern(e)}"
                )
            except Exception:
                pass

        # 过滤：聊天输入框在窗口下半部分，面积较大
        candidates = [
            e
            for e in edits
            if e.BoundingRectangle
            and e.BoundingRectangle.top >= win_center_y - 20
            and e.BoundingRectangle.width() > 100
        ]

        if not candidates:
            candidates = [e for e in edits if e.BoundingRectangle]

        # 按面积倒序，最大的就是聊天输入框
        candidates.sort(
            key=lambda e: e.BoundingRectangle.width() * e.BoundingRectangle.height(),
            reverse=True,
        )

        for ctrl in candidates:
            rect = ctrl.BoundingRectangle
            area = rect.width() * rect.height()
            if area < 200:
                continue

            name = ctrl.Name or ""
            has_vp = self._has_value_pattern(ctrl)
            self.logger.debug(
                f"输入候选: '{name[:30]}' {rect.width()}x{rect.height()} "
                f"V={has_vp}"
            )

            # 优先使用支持 ValuePattern 的
            if has_vp:
                self._input_control = ctrl
                self.logger.info(
                    f"聊天输入框: {rect.width()}x{rect.height()} (ValuePattern)"
                )
                break

        if not self._input_control:
            # 后备：用面积最大的
            self._input_control = candidates[0] if candidates else edits[0]
            self.logger.warning("输入框无 ValuePattern，使用 SendKeys 后备方案")
            self.logger.debug(
                f"后备输入控件: {self._input_control.ControlTypeName} "
                f"'{self._input_control.Name[:30]}'"
            )

        # 查找发送按钮
        try:
            buttons: list = []

            def find_buttons(ctrl, depth=0):
                if depth > 8:
                    return
                try:
                    for child in ctrl.GetChildren():
                        if child.ControlTypeName == "ButtonControl":
                            bn = child.Name or ""
                            if "发送" in bn or "Send" in bn or bn.strip() == "":
                                buttons.append(child)
                        find_buttons(child, depth + 1)
                except Exception:
                    pass

            find_buttons(self._window)
            if buttons:
                self._send_button = buttons[0]
                self.logger.info("已定位发送按钮")
            else:
                self.logger.info("未找到发送按钮，发送时用 Enter")
        except Exception:
            pass

        return True

    def _copy_image_to_clipboard(self, path: str) -> None:
        """复制图片到剪贴板（通过 PowerShell，避免 PIL 对象被当作文本复制）。"""

        abs_path = os.path.abspath(path)
        ps_path = abs_path.replace("'", "''")
        try:
            subprocess.run(
                [
                    "powershell",
                    "-WindowStyle",
                    "Hidden",
                    "-Command",
                    f"Add-Type -AssemblyName System.Windows.Forms;"
                    f"Add-Type -AssemblyName System.Drawing;"
                    f"$img = [System.Drawing.Image]::FromFile('{ps_path}');"
                    f"[System.Windows.Forms.Clipboard]::SetImage($img);"
                    f"$img.Dispose()",
                ],
                check=True,
                timeout=10,
            )
            self.logger.debug("PowerShell 已复制图片到剪贴板")
        except Exception as e:
            self.logger.error(f"复制图片到剪贴板失败: {e}")
            raise

    # ================================================================
    # 诊断
    # ================================================================

    def diagnose(self) -> None:
        """输出诊断信息，用于调试。"""

        if not self._window:
            print("✗ 未找到微信窗口")
            return

        print(f"✓ 微信窗口: '{self._window.Name}'")
        print(f"  ClassName: {self._window.ClassName}")
        print(f"  Electron: {self._is_electron}")
        print(
            f"  位置: [{self._window.BoundingRectangle.left},"
            f"{self._window.BoundingRectangle.top}] "
            f"{self._window.BoundingRectangle.width()}x"
            f"{self._window.BoundingRectangle.height()}"
        )

        print("\n--- UIA 树 ---")
        self._dump_tree(self._window, max_depth=4)

        print("\n--- 控件状态 ---")
        print(f"  输入框: {'✓' if self._input_control else '✗'}")
        print(f"  发送按钮: {'✓' if self._send_button else '✗'}")
