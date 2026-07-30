import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from unittest import TestCase

from weflow_listener import WeFlowListener


class WeFlowDispatchConcurrencyTest(TestCase):
    def setUp(self):
        self.listeners = []

    def tearDown(self):
        for listener in self.listeners:
            listener.stop_event.set()
            with listener._dispatch_condition:
                listener._dispatch_condition.notify_all()
            executor, listener._dispatch_executor = listener._dispatch_executor, None
            if executor is not None:
                executor.shutdown(wait=True, cancel_futures=True)

    def _listener(self, workers=8):
        listener = WeFlowListener(
            api_url="http://127.0.0.1:5031",
            api_token="test-token",
            dispatch_workers=workers,
        )
        listener._dispatch_executor = ThreadPoolExecutor(
            max_workers=workers,
            thread_name_prefix="test-weflow-inbound",
        )
        self.listeners.append(listener)
        return listener

    @staticmethod
    def _submit(listener, session_id, rawid):
        listener._submit_sse_frame(
            "message.new",
            [json.dumps({"sessionId": session_id, "rawid": rawid})],
        )

    @staticmethod
    def _wait_idle(listener, timeout=3):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with listener._dispatch_condition:
                if (
                    listener._dispatch_pending == 0
                    and not listener._dispatch_active_sessions
                ):
                    return True
            time.sleep(0.01)
        return False

    def test_different_sessions_reach_configured_eight_way_parallelism(self):
        listener = self._listener(workers=8)
        lock = threading.Lock()
        release = threading.Event()
        eight_started = threading.Event()
        active = 0
        maximum = 0

        def handle(_event, lines):
            nonlocal active, maximum
            json.loads("\n".join(lines))
            with lock:
                active += 1
                maximum = max(maximum, active)
                if active == 8:
                    eight_started.set()
            release.wait(2)
            with lock:
                active -= 1

        listener._dispatch_sse_frame = handle
        for index in range(12):
            self._submit(listener, f"session-{index}", f"message-{index}")

        self.assertTrue(eight_started.wait(2), "8 个不同会话未能并行启动")
        with lock:
            self.assertEqual(8, maximum)
        release.set()
        self.assertTrue(self._wait_idle(listener), "并发压测后队列未清空")

    def test_slow_session_does_not_block_another_session_or_reorder_itself(self):
        listener = self._listener(workers=8)
        release_slow = threading.Event()
        slow_started = threading.Event()
        slow_second_started = threading.Event()
        fast_finished = threading.Event()
        lock = threading.Lock()
        processed = []

        def handle(_event, lines):
            rawid = json.loads("\n".join(lines))["rawid"]
            with lock:
                processed.append(rawid)
            if rawid == "slow-1":
                slow_started.set()
                release_slow.wait(2)
            elif rawid == "slow-2":
                slow_second_started.set()
            elif rawid == "fast-1":
                fast_finished.set()

        listener._dispatch_sse_frame = handle
        self._submit(listener, "slow-session", "slow-1")
        self.assertTrue(slow_started.wait(1))
        self._submit(listener, "slow-session", "slow-2")
        self._submit(listener, "fast-session", "fast-1")

        self.assertTrue(fast_finished.wait(1), "其他会话被慢会话阻塞")
        self.assertFalse(slow_second_started.is_set(), "同会话第二条发生越序")
        release_slow.set()
        self.assertTrue(slow_second_started.wait(1))
        self.assertTrue(self._wait_idle(listener))
        with lock:
            self.assertLess(processed.index("slow-1"), processed.index("slow-2"))

    def test_dispatch_worker_limit_is_validated(self):
        for value in (0, 33):
            with self.subTest(value=value), self.assertRaises(ValueError):
                WeFlowListener(
                    api_url="http://127.0.0.1:5031",
                    api_token="test-token",
                    dispatch_workers=value,
                )

