import queue
import sys
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from wx_Listener import (
    ChatRecoveryState,
    UICommand,
    UICommandQueue,
    UICommandTimeout,
    WeChatListener,
)


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
        listener._last_health_check = None
        listener._title_check_at = {}
        listener._message_poll_cursor = 0
        listener._message_failures = {}
        listener._command_active = False
        listener._command_started = None
        listener._recovery_active = False
        listener._recovery_started = None
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

    def test_command_drain_executes_only_one_item_per_tick(self):
        listener = self.make_listener()
        first = UICommand(action="send", args=("one", "text", "1"), timeout=15)
        second = UICommand(action="send", args=("two", "text", "2"), timeout=15)
        commands = Mock()
        commands.get_nowait.side_effect = [first, second]
        listener.commands = commands
        listener._send = Mock(return_value=True)

        listener._drain_commands(limit=20)

        self.assertTrue(first.future.result())
        self.assertFalse(second.future.done())
        commands.get_nowait.assert_called_once_with()
        commands.task_done.assert_called_once_with()

    def test_started_command_timeout_is_bounded_and_not_retry_safe(self):
        commands = UICommandQueue()
        commands._queue.put = Mock(
            side_effect=lambda command, timeout: command.begin())

        with self.assertRaises(UICommandTimeout) as raised:
            commands.submit("send", "target", "text", "hello", timeout=0.01)

        self.assertFalse(raised.exception.retry_safe)
        self.assertIsNotNone(raised.exception.command_future)

    def test_completed_command_timeout_is_not_misclassified_as_wait_timeout(self):
        commands = UICommandQueue()

        def complete_with_timeout(command, timeout):
            command.begin()
            command.future.set_exception(TimeoutError("send operation timed out"))

        commands._queue.put = Mock(side_effect=complete_with_timeout)

        with self.assertRaisesRegex(TimeoutError, "send operation timed out") as raised:
            commands.submit("send", "target", "text", "hello", timeout=0.01)

        self.assertNotIsInstance(raised.exception, UICommandTimeout)

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
        listener._last_health_check = None
        listener._title_check_at = {}
        listener._message_poll_cursor = 0
        listener._message_failures = {}
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
        bad.Close.assert_not_called()
        self.assertIs(listener.wx.listen["Good"], good)
        self.assertIs(listener._chatwnd_cache["Good"], good)
        self.assertEqual(listener._failed_chats["Bad"].next_retry, 100)

        listener._retry_failed_chats()
        self.assertIs(listener.wx.listen["Bad"], rebuilt)
        self.assertIs(listener._chatwnd_cache["Bad"], rebuilt)
        self.assertEqual(listener.listen_chats["Good"], "Good")
        self.assertNotIn("Bad", listener._failed_chats)

    @patch("wx_Listener.time.monotonic", side_effect=[100, 102])
    def test_listener_health_is_throttled_for_five_seconds(self, _monotonic):
        listener = self.make_lifecycle_listener()
        chat = SimpleNamespace()
        listener.listen_chats = {"Good": "Good"}
        listener._desired_chats = {"Good": "Good"}
        listener.wx = SimpleNamespace(listen={"Good": chat})
        listener._chatwnd_is_alive = Mock(return_value=True)
        listener._refresh_chat_title = Mock()

        listener._check_listener_health()
        listener._check_listener_health()

        listener._chatwnd_is_alive.assert_called_once_with(chat)
        listener._refresh_chat_title.assert_called_once_with("Good", chat)

    @patch("wx_Listener.time.monotonic", return_value=100)
    def test_retry_failed_chats_rebuilds_only_one_per_tick(self, _monotonic):
        listener = self.make_lifecycle_listener()
        listener._failed_chats = {
            "One": ChatRecoveryState("One", next_retry=0, disabled=True),
            "Two": ChatRecoveryState("Two", next_retry=0),
        }
        listener._add_listen_chat = Mock(return_value=True)

        listener._retry_failed_chats()

        listener._add_listen_chat.assert_called_once_with("One", max_retries=1)
        self.assertFalse(listener._failed_chats["One"].disabled)
        self.assertFalse(listener._recovery_active)
        self.assertIsNone(listener._recovery_started)

    @patch("wx_Listener.time.monotonic", return_value=100)
    def test_mark_failed_discards_state_without_closing_window(self, _monotonic):
        listener = self.make_lifecycle_listener()
        chat = SimpleNamespace(Close=Mock())
        listener.listen_chats = {"Bad": "Bad"}
        listener._desired_chats = {"Bad": "Bad"}
        listener.wx = SimpleNamespace(listen={"Bad": chat})

        listener._mark_chat_failed("Bad", "invalid")

        chat.Close.assert_not_called()
        self.assertNotIn("Bad", listener.wx.listen)
        self.assertNotIn("Bad", listener.listen_chats)
        self.assertIn("Bad", listener._failed_chats)

    @patch("wx_Listener.time.monotonic", return_value=100)
    def test_title_refresh_ignores_dynamic_unread_suffix(self, _monotonic):
        listener = self.make_lifecycle_listener()
        chat = SimpleNamespace(
            HWND=123, uia_name="Good", who="Good", Rebind=Mock())
        listener.listen_chats = {"Good": "Good"}
        listener._desired_chats = {"Good": "Good"}
        listener.wx = SimpleNamespace(listen={"Good": chat})
        win32gui = SimpleNamespace(GetWindowText=Mock(return_value="Good (3)"))

        with patch.dict(sys.modules, {"win32gui": win32gui}):
            listener._refresh_chat_title("Good", chat)

        chat.Rebind.assert_not_called()
        self.assertIs(listener.wx.listen["Good"], chat)

    @patch("wx_Listener.time.monotonic", side_effect=[100, 110])
    def test_title_refresh_is_throttled_for_thirty_seconds(self, _monotonic):
        listener = self.make_lifecycle_listener()
        chat = SimpleNamespace(HWND=123, uia_name="Good", who="Good", Rebind=Mock())
        listener.listen_chats = {"Good": "Good"}
        listener._desired_chats = {"Good": "Good"}
        listener.wx = SimpleNamespace(listen={"Good": chat})
        win32gui = SimpleNamespace(GetWindowText=Mock(return_value="Good"))

        with patch.dict(sys.modules, {"win32gui": win32gui}):
            listener._refresh_chat_title("Good", chat)
            listener._refresh_chat_title("Good", chat)

        win32gui.GetWindowText.assert_called_once_with(123)

    def test_single_message_error_does_not_rebuild_chat(self):
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

        listener._mark_chat_failed.assert_not_called()
        self.assertEqual(listener._message_failures["Bad"], 1)
        listener._process_message.assert_called_once_with("Good", message)

    def test_three_isolated_message_errors_rebuild_only_failed_chat(self):
        listener = self.make_lifecycle_listener()
        chat = SimpleNamespace(who="Bad")
        listener.listen_chats = {"Bad": "Bad"}
        listener._desired_chats = {"Bad": "Bad"}
        listener.wx = SimpleNamespace(listen={"Bad": chat})

        def fail(_key):
            listener.wx.listen_errors = {"Bad": RuntimeError("UIA timeout")}
            return []

        listener.wx.GetListenMessage = Mock(side_effect=fail)
        for _ in range(3):
            listener._check_new_messages()

        self.assertNotIn("Bad", listener.listen_chats)
        self.assertIn("Bad", listener._failed_chats)

    def test_three_message_poll_failures_rebuild_only_selected_chat(self):
        listener = self.make_lifecycle_listener()
        chat = SimpleNamespace(who="Good")
        listener.chat_types = {"Good": "group"}
        listener.listen_chats = {"Good": "Good"}
        listener._desired_chats = {"Good": "Good"}
        listener.wx = SimpleNamespace(
            listen={"Good": chat},
            listen_errors={},
            GetListenMessage=Mock(side_effect=RuntimeError("stalled")),
        )

        listener._check_new_messages()
        listener._check_new_messages()
        listener._check_new_messages()

        self.assertNotIn("Good", listener.listen_chats)
        self.assertNotIn("Good", listener.wx.listen)
        self.assertIn("Good", listener._failed_chats)

    @patch("wx_Listener.time.monotonic", return_value=100)
    def test_chat_recovery_uses_exponential_backoff(self, _monotonic):
        listener = self.make_lifecycle_listener()

        expected = [101, 102, 104, 108, 116, 130, 130]
        for deadline in expected:
            listener._schedule_chat_retry("Bad", "Bad", RuntimeError("missing"))
            self.assertEqual(listener._failed_chats["Bad"].next_retry, deadline)
        listener._schedule_chat_retry("Bad", "Bad", RuntimeError("missing"))
        self.assertFalse(listener._failed_chats["Bad"].disabled)
        self.assertEqual(listener._failed_chats["Bad"].next_retry, 130)

    def test_main_window_reconnect_resets_all_runtime_chat_state(self):
        listener = self.make_lifecycle_listener()
        listener._desired_chats = {"Bad": "Bad", "Good": "Good"}
        listener._failed_chats = {
            "Bad": ChatRecoveryState("Bad", failures=4, next_retry=999)}
        old_wx = SimpleNamespace(listen={})
        new_wx = SimpleNamespace(listen={})
        listener.wx = old_wx
        listener._wechat_class = Mock(return_value=new_wx)
        listener._add_listen_chat = Mock(return_value=True)

        self.assertTrue(listener._reconnect_wechat())

        self.assertIs(listener.wx, new_wx)
        self.assertEqual(listener.listen_chats, {})
        self.assertEqual(listener._chatwnd_cache, {})
        self.assertEqual(set(listener._failed_chats), {"Bad", "Good"})
        self.assertEqual(
            [call.args[0] for call in listener._add_listen_chat.call_args_list],
            [],
        )


if __name__ == "__main__":
    unittest.main()
