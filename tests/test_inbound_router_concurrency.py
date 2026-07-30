import asyncio
import contextlib
import gc
import json
import sqlite3
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase, TestCase

from wx_Processor import MessageProcessor


class _GateRouter:
    def __init__(self, blocked=(), fail=()):
        self.blocked = set(blocked)
        self.fail = set(fail)
        self.release = asyncio.Event()
        self.started = []
        self.active = 0
        self.max_active = 0

    async def send_message(self, message):
        message_id = message.test_message_id
        self.started.append(message_id)
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            if message_id in self.blocked:
                await self.release.wait()
            return message_id not in self.fail
        finally:
            self.active -= 1


class InboundRouterConcurrencyTest(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.processor = MessageProcessor.__new__(MessageProcessor)
        self.processor.platform = "wx"
        self.processor._db_path = str(Path(self.temporary.name) / "queue.sqlite3")
        self.processor._stop_requested = threading.Event()
        self.processor._inbound_delivery_tasks = {}
        self.processor._inbound_wakeup = asyncio.Event()
        self.processor._router_is_connected = lambda: True
        self.processor._build_message = (
            lambda _chat, _data, message_id: SimpleNamespace(
                test_message_id=message_id
            )
        )

        async def run_direct(callback, *args):
            return callback(*args)

        self.processor._run_blocking = run_direct
        self.processor._delete_delivered_inbound_file = lambda _data: None
        self.processor._init_storage()
        self.dispatcher = None

    async def asyncTearDown(self):
        self.processor._stop_requested.set()
        self.processor._inbound_wakeup.set()
        if self.dispatcher is not None:
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self.dispatcher, timeout=2)
        for task in list(self.processor._inbound_delivery_tasks.values()):
            task.cancel()
        if self.processor._inbound_delivery_tasks:
            await asyncio.gather(
                *self.processor._inbound_delivery_tasks.values(),
                return_exceptions=True,
            )
        self.processor._inbound_delivery_tasks.clear()
        self.dispatcher = None
        gc.collect()
        self.temporary.cleanup()

    def _insert(self, message_id, session_id, created, *, next_try=0):
        data = {
            "chat_type": "private",
            "chat_id": session_id,
            "content": message_id,
            "type": "text",
            "timestamp": created,
            "_wemai_message_id": message_id,
        }
        stored = {
            "_wemai_inbound_event": 1,
            "chat_name": session_id,
            "data": data,
        }
        with contextlib.closing(self.processor._connect()) as db, db:
            db.execute(
                "INSERT INTO inbound("
                "message_id,payload,state,attempts,next_try,created,session_key"
                ") VALUES(?,?,'pending',0,?,?,?)",
                (
                    message_id,
                    json.dumps(stored),
                    next_try,
                    created,
                    f"private:{session_id}",
                ),
            )

    async def _wait_until(self, predicate, timeout=2):
        deadline = asyncio.get_running_loop().time() + timeout
        while not predicate():
            if asyncio.get_running_loop().time() >= deadline:
                self.fail("等待异步投递状态超时")
            await asyncio.sleep(0.01)

    def _state(self, message_id):
        with contextlib.closing(self.processor._connect()) as db:
            return db.execute(
                "SELECT state,attempts FROM inbound WHERE message_id=?",
                (message_id,),
            ).fetchone()

    async def test_same_session_is_serial_and_other_session_overlaps(self):
        self._insert("a1", "session-a", 1)
        self._insert("a2", "session-a", 2)
        self._insert("b1", "session-b", 3)
        router = _GateRouter(blocked={"a1", "b1"})
        self.processor.router = router

        self.dispatcher = asyncio.create_task(
            self.processor._process_inbound_queue()
        )
        await self._wait_until(lambda: len(router.started) == 2)

        self.assertEqual(set(router.started), {"a1", "b1"})
        self.assertNotIn("a2", router.started)
        self.assertEqual(router.max_active, 2)
        router.release.set()
        await self._wait_until(lambda: self._state("a2")[0] == "sent")
        self.assertLess(router.started.index("a1"), router.started.index("a2"))

    async def test_retry_backoff_blocks_only_its_own_session(self):
        self._insert("a1", "session-a", 1, next_try=time.time() + 60)
        self._insert("a2", "session-a", 2)
        self._insert("b1", "session-b", 3)
        router = _GateRouter()
        self.processor.router = router

        self.dispatcher = asyncio.create_task(
            self.processor._process_inbound_queue()
        )
        await self._wait_until(lambda: self._state("b1")[0] == "sent")

        self.assertEqual(router.started, ["b1"])
        self.assertEqual(self._state("a1")[0], "pending")
        self.assertEqual(self._state("a2")[0], "pending")

    async def test_false_router_result_is_retried_without_overtaking(self):
        self._insert("a1", "session-a", 1)
        self._insert("a2", "session-a", 2)
        self._insert("b1", "session-b", 3)
        router = _GateRouter(fail={"a1"})
        self.processor.router = router

        self.dispatcher = asyncio.create_task(
            self.processor._process_inbound_queue()
        )
        await self._wait_until(
            lambda: self._state("a1") == ("pending", 1)
            and self._state("b1")[0] == "sent"
        )

        self.assertNotIn("a2", router.started)
        self.assertEqual(self._state("a2")[0], "pending")


class InboundRouterMigrationTest(TestCase):
    def test_old_sending_rows_are_requeued_and_receive_same_session_key(self):
        with tempfile.TemporaryDirectory() as directory:
            processor = MessageProcessor.__new__(MessageProcessor)
            processor.platform = "wx"
            processor._db_path = str(Path(directory) / "old.sqlite3")
            stored = {
                "_wemai_inbound_event": 1,
                "chat_name": "测试群",
                "data": {
                    "chat_type": "group",
                    "chat_id": "room@chatroom",
                },
            }
            with contextlib.closing(sqlite3.connect(processor._db_path)) as db:
                db.execute(
                    "CREATE TABLE inbound (message_id TEXT PRIMARY KEY, "
                    "payload TEXT NOT NULL, state TEXT NOT NULL, "
                    "attempts INTEGER NOT NULL, next_try REAL NOT NULL, "
                    "created REAL NOT NULL)"
                )
                for index in (1, 2):
                    db.execute(
                        "INSERT INTO inbound VALUES(?,?,'sending',0,0,?)",
                        (f"m{index}", json.dumps(stored), index),
                    )
                db.commit()

            processor._init_storage()

            with contextlib.closing(processor._connect()) as db:
                rows = db.execute(
                    "SELECT state,session_key FROM inbound ORDER BY created"
                ).fetchall()
            self.assertEqual(
                rows,
                [
                    ("pending", "group:room@chatroom"),
                    ("pending", "group:room@chatroom"),
                ],
            )


if __name__ == "__main__":
    import unittest

    unittest.main()
