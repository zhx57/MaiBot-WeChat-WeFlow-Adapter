import queue
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from wx_Listener import ChatRecoveryState, UICommand, WeChatListener


class _WaitingCommands:
    def __init__(self, stop_event):
        self.stop_event = stop_event

    def get_nowait(self):
        raise queue.Empty

    def wait(self, _timeout):
        self.stop_event.set()


class ListenerRecoveryTest(unittest.TestCase):
    def make_listener(self):
        listener = WeChatListener.__new__(WeChatListener)
        listener._owner_thread = threading.get_ident()
        listener.target_specs = []
        listener.stop_event = threading.Event()
        listener.commands = _WaitingCommands(listener.stop_event)
        listener.heartbeat = None
        listener.running = False
        listener.listen_chats = {}
        listener._desired_chats = {}
        listener._chatwnd_cache = {}
        listener._failed_chats = {}
        listener._command_active = False
        listener._command_started = None
        listener._check_listener_health = Mock()
        listener._check_new_messages = Mock()
        listener._retry_failed_chats = Mock()
        listener._reconnect_wechat = Mock(return_value=True)
        return listener

    @patch("wx_Listener.time.sleep")
    @patch("wx_Listener.time.monotonic", side_effect=[5, 10, 15])
    def test_two_probe_failures_do_not_trigger_reconnect(self, _monotonic, _sleep):
        listener = self.make_listener()
        listener._is_wechat_alive = Mock(side_effect=[False, False, True])

        listener.start_listening()

        listener._reconnect_wechat.assert_not_called()

    @patch("wx_Listener.time.sleep")
    @patch("wx_Listener.time.monotonic", side_effect=[5, 10, 15, 20, 25])
    def test_confirmed_loss_retries_reconnect(self, _monotonic, _sleep):
        listener = self.make_listener()
        listener._is_wechat_alive = Mock(side_effect=[False, False, False, True, True])
        listener._reconnect_wechat.side_effect = [False, True]

        listener.start_listening()

        self.assertEqual(listener._reconnect_wechat.call_count, 2)

    def test_command_updates_heartbeat_before_and_after_send(self):
        listener = self.make_listener()
        heartbeats = []
        listener.heartbeat = lambda: heartbeats.append(True)
        command = UICommand(action="send", args=("target", "text", "hello"), timeout=15)
        commands = Mock()
        commands.get_nowait.side_effect = [command, queue.Empty]
        listener.commands = commands
        listener._send = Mock(return_value=True)

        listener._drain_commands(limit=2)

        self.assertTrue(command.future.result())
        self.assertGreaterEqual(len(heartbeats), 2)
        self.assertFalse(listener._command_active)
        self.assertIsNone(listener._command_started)

    def make_lifecycle_listener(self):
        listener = WeChatListener.__new__(WeChatListener)
        listener._owner_thread = threading.get_ident()
        listener.heartbeat = None
        listener.target_specs = []
        listener.chat_types = {"Bad": "group", "Good": "group"}
        listener.listen_chats = {"Bad": "Bad", "Good": "Good"}
        listener._desired_chats = {"Bad": "Bad", "Good": "Good"}
        listener._failed_chats = {}
        listener._chatwnd_cache = {}
        return listener

    @patch("wx_Listener.time.monotonic", return_value=100)
    def test_invalid_chat_is_rebuilt_without_touching_other_chat(self, _monotonic):
        listener = self.make_lifecycle_listener()
        bad = SimpleNamespace(Close=Mock())
        good = SimpleNamespace()
        rebuilt = SimpleNamespace(
            who="Bad", HWND=300, UiaAPI=SimpleNamespace(Exists=Mock(return_value=True)))
        wx = SimpleNamespace(listen={"Bad": bad, "Good": good})
        wx.RemoveListenChat = lambda name: wx.listen.pop(name, None)
        wx.AddListenChat = lambda name, **_kwargs: wx.listen.__setitem__(name, rebuilt)
        listener.wx = wx
        listener._chatwnd_is_alive = Mock(side_effect=lambda chat: chat is not bad)

        listener._check_listener_health()

        self.assertNotIn("Bad", listener.wx.listen)
        self.assertIs(listener.wx.listen["Good"], good)
        self.assertIs(listener._chatwnd_cache["Good"], good)
        self.assertEqual(listener._failed_chats["Bad"].next_retry, 100)

        listener._retry_failed_chats()
        self.assertIs(listener.wx.listen["Bad"], rebuilt)
        self.assertIs(listener._chatwnd_cache["Bad"], rebuilt)
        self.assertEqual(listener.listen_chats["Good"], "Good")
        self.assertNotIn("Bad", listener._failed_chats)

    def test_message_error_marks_only_the_failed_chat(self):
        listener = self.make_lifecycle_listener()
        good_chat = Mock(who="Good")
        message = SimpleNamespace(type="friend", sender="Alice", content="hello")
        listener.wx = SimpleNamespace(
            GetListenMessage=Mock(return_value={good_chat: [message]}),
            listen_errors={"Bad": RuntimeError("closed")},
        )
        listener._mark_chat_failed = Mock()
        listener._process_message = Mock()

        listener._check_new_messages()

        listener._mark_chat_failed.assert_called_once()
        listener._process_message.assert_called_once_with("Good", message)

    @patch("wx_Listener.time.monotonic", return_value=100)
    def test_chat_recovery_uses_exponential_backoff(self, _monotonic):
        listener = self.make_lifecycle_listener()

        expected = [101, 102, 104, 108, 116, 130, 130]
        for deadline in expected:
            listener._schedule_chat_retry("Bad", "Bad", RuntimeError("missing"))
            self.assertEqual(listener._failed_chats["Bad"].next_retry, deadline)
        listener._schedule_chat_retry("Bad", "Bad", RuntimeError("missing"))
        self.assertTrue(listener._failed_chats["Bad"].disabled)

    def test_main_window_reconnect_resets_all_runtime_chat_state(self):
        listener = self.make_lifecycle_listener()
        listener._desired_chats = {"Bad": "Bad", "Good": "Good"}
        listener._failed_chats = {
            "Bad": ChatRecoveryState("Bad", failures=4, next_retry=999)}
        old_wx = SimpleNamespace(listen={})
        new_wx = SimpleNamespace(listen={})
        listener.wx = old_wx
        listener._wechat_class = Mock(return_value=new_wx)
        listener._cleanup_wechat = Mock()
        listener._add_listen_chat = Mock(return_value=True)

        self.assertTrue(listener._reconnect_wechat())

        self.assertIs(listener.wx, new_wx)
        self.assertEqual(listener.listen_chats, {})
        self.assertEqual(listener._chatwnd_cache, {})
        self.assertEqual(listener._failed_chats, {})
        self.assertEqual(
            [call.args[0] for call in listener._add_listen_chat.call_args_list],
            ["Bad", "Good"],
        )


if __name__ == "__main__":
    unittest.main()
