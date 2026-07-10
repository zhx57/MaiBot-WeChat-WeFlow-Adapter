import sys
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from wx_Listener import WeChatListener


class ChatWndCacheTest(unittest.TestCase):
    def make_listener(self, chat):
        listener = WeChatListener.__new__(WeChatListener)
        listener._chatwnd_cache = {"target": chat}
        return listener

    def test_valid_cached_hwnd_returns_without_window_search(self):
        chat = SimpleNamespace(HWND=123, uia_name="Target")
        listener = self.make_listener(chat)
        win32gui = SimpleNamespace(IsWindow=Mock(return_value=True), FindWindow=Mock())

        with patch.dict(sys.modules, {"win32gui": win32gui}):
            self.assertIs(listener._ensure_chatwnd("target"), chat)

        win32gui.IsWindow.assert_called_once_with(123)
        win32gui.FindWindow.assert_not_called()

    def test_chat_without_hwnd_is_invalid_without_find_window(self):
        chat = SimpleNamespace(uia_name="Target")
        win32gui = SimpleNamespace(
            IsWindow=Mock(return_value=True), FindWindow=Mock(return_value=456))

        with patch.dict(sys.modules, {"win32gui": win32gui}):
            self.assertFalse(WeChatListener._chatwnd_is_alive(chat))

        win32gui.FindWindow.assert_not_called()
        win32gui.IsWindow.assert_not_called()

    def test_rebuild_prefers_native_window_lookup_over_root_scan(self):
        window = SimpleNamespace(ClassName="ChatWnd", Name="Target")
        root = SimpleNamespace(GetChildren=Mock())
        uia = SimpleNamespace(
            ControlFromHandle=Mock(return_value=window),
            GetRootControl=Mock(return_value=root),
        )
        win32gui = SimpleNamespace(
            FindWindow=Mock(return_value=456), IsWindow=Mock(return_value=True))

        with patch.dict(sys.modules, {"win32gui": win32gui}):
            self.assertEqual(WeChatListener._find_chat_windows("Target", uia), [window])

        uia.ControlFromHandle.assert_called_once_with(456)
        uia.GetRootControl.assert_not_called()


if __name__ == "__main__":
    unittest.main()
