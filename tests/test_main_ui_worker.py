import sys
import threading
import types
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import main


class MainUIWorkerTest(unittest.TestCase):
    def test_uia_is_initialized_before_listener_and_released_after_close(self):
        events = []
        uia = types.ModuleType("wx4py.core.uiautomation")
        uia.InitializeUIAutomationInCurrentThread = Mock(
            side_effect=lambda: events.append("uia-init")
        )
        uia.UninitializeUIAutomationInCurrentThread = Mock(
            side_effect=lambda: events.append("uia-uninit")
        )
        core = types.ModuleType("wx4py.core")
        core.uiautomation = uia
        wx4py = types.ModuleType("wx4py")
        wx4py.core = core

        class FakeListener:
            def __init__(self, **_kwargs):
                events.append("listener-init")
                self.listening_target_names = []

            def start_listening(self):
                events.append("listener-start")
                events.append(f"ready-during-start={state['ready'].is_set()}")

            def process_commands(self, limit=1):
                events.append(f"process-{limit}")
                main.stop_event.set()

            def close(self):
                events.append("listener-close")

        state = {
            "ready": threading.Event(),
            "error": None,
            "listener": None,
        }
        commands = SimpleNamespace(wait=Mock())
        modules = {
            "wx4py": wx4py,
            "wx4py.core": core,
            "wx4py.core.uiautomation": uia,
        }

        main.stop_event.clear()
        try:
            with patch.dict(sys.modules, modules):
                with patch.object(main, "WeChatListener", FakeListener):
                    main.run_ui_worker([], commands, False, state)
        finally:
            main.stop_event.clear()

        self.assertIsNone(state["error"])
        self.assertTrue(state["ready"].is_set())
        self.assertIn("ready-during-start=False", events)
        self.assertLess(events.index("uia-init"), events.index("listener-init"))
        self.assertLess(events.index("listener-close"), events.index("uia-uninit"))
        uia.InitializeUIAutomationInCurrentThread.assert_called_once_with()
        uia.UninitializeUIAutomationInCurrentThread.assert_called_once_with()


class _StepEvent:
    def __init__(self, ready_after):
        self.ready_after = ready_after
        self.wait_calls = []
        self._is_set = False

    def is_set(self):
        return self._is_set

    def wait(self, timeout=None):
        self.wait_calls.append(timeout)
        if len(self.wait_calls) >= self.ready_after:
            self._is_set = True
        return self._is_set


class MainUIStartupWaitTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        main.stop_event.clear()

    async def asyncTearDown(self):
        main.stop_event.clear()

    async def test_zero_timeout_waits_until_worker_reports_all_targets_ready(self):
        ready = _StepEvent(ready_after=2)
        state = {"ready": ready, "phase": "opening_targets", "listener": None}
        ui_thread = SimpleNamespace(is_alive=Mock(return_value=True))
        clock_values = iter((0.0, 31.0, 62.0, 93.0, 124.0))

        result = await main._wait_for_ui_worker_startup(
            state,
            ui_thread,
            timeout=0,
            clock=lambda: next(clock_values),
        )

        self.assertTrue(result)
        self.assertEqual(len(ready.wait_calls), 2)
        self.assertTrue(all(call_timeout <= 0.1 for call_timeout in ready.wait_calls))

    async def test_explicit_startup_timeout_remains_available(self):
        state = {
            "ready": _StepEvent(ready_after=100),
            "phase": "opening_targets",
            "listener": None,
        }
        ui_thread = SimpleNamespace(is_alive=Mock(return_value=True))

        clock_values = iter((0.0, 1.1))
        with self.assertRaisesRegex(TimeoutError, "配置时限 1 秒"):
            await main._wait_for_ui_worker_startup(
                state,
                ui_thread,
                timeout=1,
                clock=lambda: next(clock_values),
            )

    async def test_zero_cumulative_timeout_still_detects_a_stuck_ui_call(self):
        state = {
            "ready": _StepEvent(ready_after=100),
            "phase": "opening_targets",
            "listener": None,
            "heartbeat": 0.0,
        }
        ui_thread = SimpleNamespace(
            ident=None,
            is_alive=Mock(return_value=True),
        )
        clock_values = iter((0.0, main.UI_WORKER_BUSY_TIMEOUT_SECONDS + 0.1))

        with patch.object(main, "log_exit"):
            with self.assertRaises(main.UIWorkerStalledError):
                await main._wait_for_ui_worker_startup(
                    state,
                    ui_thread,
                    timeout=0,
                    clock=lambda: next(clock_values),
                )

        self.assertTrue(main.stop_event.is_set())


class MainUIWatchdogTest(unittest.TestCase):
    def tearDown(self):
        main.stop_event.clear()
        main._runtime["shutdown_reason"] = None

    def test_idle_and_busy_heartbeat_limits_are_distinct(self):
        listener = SimpleNamespace(
            _command_active=False,
            _recovery_active=False,
            _media_active=False,
            _reconnecting=False,
        )
        state = {
            "heartbeat": 0.0,
            "phase": "running",
            "listener": listener,
        }

        self.assertIsNone(
            main._ui_worker_stall_reason(
                state,
                now=main.UI_WORKER_IDLE_TIMEOUT_SECONDS,
            )
        )
        self.assertIn(
            "心跳超过",
            main._ui_worker_stall_reason(
                state,
                now=main.UI_WORKER_IDLE_TIMEOUT_SECONDS + 0.1,
            ),
        )

        listener._media_active = True
        self.assertIsNone(
            main._ui_worker_stall_reason(
                state,
                now=main.UI_WORKER_BUSY_TIMEOUT_SECONDS,
            )
        )
        self.assertIn(
            "media_active=True",
            main._ui_worker_stall_reason(
                state,
                now=main.UI_WORKER_BUSY_TIMEOUT_SECONDS + 0.1,
            ),
        )

    def test_restart_reexecutes_same_script_and_arguments(self):
        with patch.object(main.sys, "executable", r"C:\Python\python.exe"):
            with patch.object(main.sys, "argv", ["main.py", "--all"]):
                with patch.object(
                    main.os.path,
                    "abspath",
                    return_value=r"C:\WeMai\main.py",
                ):
                    with patch.object(main.os, "execv") as execv:
                        main._restart_current_process()

        execv.assert_called_once_with(
            r"C:\Python\python.exe",
            [r"C:\Python\python.exe", r"C:\WeMai\main.py", "--all"],
        )


if __name__ == "__main__":
    unittest.main()
