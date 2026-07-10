import queue
import threading
import unittest
from unittest.mock import Mock, patch

from wx_Listener import UICommand, WeChatListener


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
        listener._failed_chats = {}
        listener._last_retry_time = 0
        listener._command_active = False
        listener._command_started = None
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


if __name__ == "__main__":
    unittest.main()
