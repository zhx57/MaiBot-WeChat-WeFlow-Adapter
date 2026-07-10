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

    def test_cached_chat_without_hwnd_uses_single_find_window(self):
        chat = SimpleNamespace(uia_name="Target")
        listener = self.make_listener(chat)
        win32gui = SimpleNamespace(
            IsWindow=Mock(return_value=True), FindWindow=Mock(return_value=456))

        with patch.dict(sys.modules, {"win32gui": win32gui}):
            self.assertIs(listener._ensure_chatwnd("target"), chat)

        win32gui.FindWindow.assert_called_once_with("ChatWnd", "Target")
        win32gui.IsWindow.assert_called_once_with(456)
        self.assertEqual(chat.HWND, 456)


if __name__ == "__main__":
    unittest.main()
