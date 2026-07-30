import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import wx_Listener


class _FakeControl:
    def __init__(self, name="", control_type="TextControl", children=None):
        self.Name = name
        self.ControlTypeName = control_type
        self.ClassName = ""
        self._children = list(children or [])
        self.clicks = 0

    def GetChildren(self):
        return list(self._children)

    def Click(self, **_kwargs):
        self.clicks += 1


def _update_dialog():
    dismiss = _FakeControl("忽略本次更新", "ButtonControl")
    root = _FakeControl(
        "新版本",
        "WindowControl",
        [
            _FakeControl("微信"),
            dismiss,
            _FakeControl("稍后处理", "ButtonControl"),
            _FakeControl("更新", "ButtonControl"),
        ],
    )
    return root, dismiss


class UpdateDialogTests(unittest.TestCase):
    def test_verified_update_dialog_returns_dismiss_button(self):
        root, dismiss = _update_dialog()

        actual = wx_Listener._find_update_dialog_dismiss_button(root)

        self.assertIs(actual, dismiss)

    def test_same_button_text_without_update_title_is_rejected(self):
        root, _dismiss = _update_dialog()
        root.Name = "设置"

        actual = wx_Listener._find_update_dialog_dismiss_button(root)

        self.assertIsNone(actual)

    def test_native_title_prefilter_accepts_update_title_only(self):
        self.assertTrue(wx_Listener._native_title_may_be_update_dialog("新版本"))
        self.assertTrue(
            wx_Listener._native_title_may_be_update_dialog("微信 - 发现新版本")
        )
        self.assertFalse(wx_Listener._native_title_may_be_update_dialog("测试群"))

    def test_default_scan_interval_is_three_days(self):
        self.assertEqual(wx_Listener.WEMAI_UPDATE_DIALOG_SCAN_INTERVAL, 259200.0)

    def test_listener_waits_five_seconds_before_clicking(self):
        root, dismiss = _update_dialog()
        listener = object.__new__(wx_Listener.WeChatListener)
        listener._next_update_dialog_scan_at = 0.0
        listener._update_dialog_first_seen = {}
        listener._update_dialog_last_click_at = {}
        listener._known_wechat_top_level_hwnds = set()
        listener._foreground_ui_lock = threading.RLock()
        listener.wx = SimpleNamespace(window=SimpleNamespace(hwnd=100))
        listener._touch_heartbeat = lambda: None

        patches = (
            patch.object(wx_Listener, "WEMAI_AUTO_DISMISS_UPDATE", True),
            patch.object(wx_Listener, "WEMAI_UPDATE_DIALOG_SCAN_INTERVAL", 259200.0),
            patch.object(wx_Listener, "WEMAI_UPDATE_DISMISS_DELAY", 5.0),
            patch.object(
                wx_Listener,
                "_list_wechat_windows_for_main",
                return_value=[(200, "新版本", "QtWindow")],
            ),
            patch.object(wx_Listener, "_control_from_handle", return_value=root),
            patch.object(wx_Listener, "_get_window_title", return_value="新版本"),
            patch.object(wx_Listener.time, "monotonic", side_effect=[100.0, 104.9, 105.0]),
        )
        for active_patch in patches:
            active_patch.start()
            self.addCleanup(active_patch.stop)

        listener._check_wechat_update_dialog()
        listener._check_wechat_update_dialog()
        self.assertEqual(dismiss.clicks, 0)

        listener._check_wechat_update_dialog()
        self.assertEqual(dismiss.clicks, 1)


if __name__ == "__main__":
    unittest.main()
