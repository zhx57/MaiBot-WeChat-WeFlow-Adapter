import contextlib
import json
import sqlite3
import sys
import tempfile
import threading
from collections import deque
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from weflow_listener import WeFlowListener  # noqa: E402
from wx_Processor import MessageProcessor  # noqa: E402


class QuoteInboundTest(TestCase):
    @staticmethod
    def _listener(callback):
        listener = WeFlowListener.__new__(WeFlowListener)
        listener._seen = set()
        listener._seen_order = deque()
        listener._targets = []
        listener.outgoing_registry = None
        listener._recency_recorder = None
        listener._known_message_checker = None
        listener.callback = callback
        return listener

    def test_private_text_attaches_quote_from_history(self):
        events = []
        listener = self._listener(events.append)
        history_row = {
            "serverId": "quoted-2",
            "content": "这是一条引用消息，引用内容为：你好",
            "senderUsername": "wxid_sender",
            "quote": {
                "accountName": "元宝原宝",
                "content": "你好",
                "platformMessageId": "quoted-1",
                "sender": "wxid_sender",
                "type": 0,
            },
            "replyToMessageId": "quoted-1",
        }
        listener._media_for_message = lambda *args, **kwargs: history_row
        payload = {
            "event": "message.new",
            "rawid": "quoted-2",
            "sessionId": "wxid_sender",
            "sessionType": "other",
            "sourceName": "元宝原宝",
            "content": "这是一条引用消息，引用内容为：你好",
            "timestamp": 1785300862,
        }

        listener._dispatch_sse_frame("message.new", [json.dumps(payload)])

        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event.content, payload["content"])
        self.assertEqual(event.raw["quote"]["content"], "你好")
        self.assertEqual(event.raw["replyToMessageId"], "quoted-1")

    def test_history_page_marks_outgoing_quote_target(self):
        messages = [
            {
                "serverId": "current",
                "replyToMessageId": "target",
                "quote": {"platformMessageId": "target", "content": "你好"},
            },
            {
                "serverId": "target",
                "createTime": 1785300860,
                "isSend": 1,
                "content": "你好",
            },
        ]

        WeFlowListener._enrich_quote_targets(messages)

        self.assertEqual(messages[0]["quote"]["targetCreateTime"], 1785300860)
        self.assertEqual(messages[0]["quote"]["targetIsSend"], 1)

    def test_quote_lookup_failure_does_not_drop_text(self):
        events = []
        listener = self._listener(events.append)

        def fail(_self, *args, **kwargs):
            raise OSError("history unavailable")

        listener._media_for_message = fail
        payload = {
            "event": "message.new",
            "rawid": "plain-1",
            "sessionId": "wxid_sender",
            "sessionType": "other",
            "sourceName": "元宝原宝",
            "content": "普通消息",
            "timestamp": 1785300862,
        }

        listener._dispatch_sse_frame("message.new", [json.dumps(payload)])

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].content, "普通消息")
        self.assertNotIn("quote", events[0].raw)

    def test_processor_builds_native_maibot_reply_segment(self):
        processor = MessageProcessor.__new__(MessageProcessor)
        processor.platform = "wx"
        processor._remember = lambda *args, **kwargs: None
        processor._schedule_avatar_cache = lambda *args, **kwargs: None
        data = {
            "chat_type": "private",
            "chat_id": "wxid_sender",
            "sender_id": "wxid_sender",
            "sender": "元宝原宝",
            "user_nickname": "元宝原宝",
            "content": "这是正文",
            "type": "text",
            "timestamp": 1785300862,
            "event": "message.new",
            "rawid": "quoted-2",
            "replyToMessageId": "quoted-1",
            "quote": {
                "accountName": "元宝原宝",
                "content": "你好",
                "platformMessageId": "quoted-1",
            },
        }

        message = processor._build_message("元宝原宝", data)

        segments = message.message_segment.data
        self.assertEqual([segment.type for segment in segments], ["reply", "text"])
        self.assertEqual(
            segments[0].data,
            processor._weflow_message_id("quoted-1"),
        )
        self.assertEqual(segments[1].data, "这是正文")

    @staticmethod
    def _create_maibot_db(directory, *, group=False):
        database = Path(directory) / "MaiBot.db"
        with contextlib.closing(sqlite3.connect(database)) as db:
            db.execute(
                "CREATE TABLE chat_sessions (session_id TEXT, platform TEXT, "
                "user_id TEXT, group_id TEXT, last_active_timestamp DATETIME)"
            )
            db.execute(
                "CREATE TABLE mai_messages (message_id TEXT, timestamp DATETIME, "
                "platform TEXT, user_id TEXT, group_id TEXT, session_id TEXT, "
                "processed_plain_text TEXT)"
            )
            if group:
                db.execute(
                    "INSERT INTO mai_messages VALUES(?,?,?,?,?,?,?)",
                    (
                        "send_api_group",
                        "2026-07-29 13:34:20.200000",
                        "wx",
                        "菲比",
                        "group-id",
                        "group-session",
                        "群聊回复",
                    ),
                )
            else:
                db.execute(
                    "INSERT INTO chat_sessions VALUES(?,?,?,?,?)",
                    ("private-session", "wx", "user-id", None, "2026-07-29 13:35:00"),
                )
                db.execute(
                    "INSERT INTO mai_messages VALUES(?,?,?,?,?,?,?)",
                    (
                        "send_api_private",
                        "2026-07-29 13:34:20.200000",
                        "wx",
                        "菲比",
                        None,
                        "private-session",
                        "第一行\n第二行",
                    ),
                )
            db.commit()
        return database

    def test_private_self_quote_resolves_original_send_api_id(self):
        processor = MessageProcessor.__new__(MessageProcessor)
        processor.platform = "wx"
        target_time = 1785303260
        with tempfile.TemporaryDirectory() as directory:
            self._create_maibot_db(directory)
            with patch("wx_Processor.MAIBOT_DATA_DIR", directory):
                result = processor._resolve_outgoing_reply_id(
                    {
                        "content": "第一行 第二行",
                        "targetCreateTime": target_time,
                        "targetIsSend": 1,
                    },
                    user_id="user-id",
                )
        self.assertEqual(result, "send_api_private")

    def test_group_self_quote_is_scoped_by_group_id(self):
        processor = MessageProcessor.__new__(MessageProcessor)
        processor.platform = "wx"
        target_time = 1785303260
        with tempfile.TemporaryDirectory() as directory:
            self._create_maibot_db(directory, group=True)
            with patch("wx_Processor.MAIBOT_DATA_DIR", directory):
                result = processor._resolve_outgoing_reply_id(
                    {
                        "content": "群聊回复",
                        "targetCreateTime": target_time,
                        "targetIsSend": 1,
                    },
                    user_id="member-id",
                    group_id="group-id",
                )
        self.assertEqual(result, "send_api_group")

    def test_history_response_is_reused_for_same_group_burst(self):
        listener = WeFlowListener.__new__(WeFlowListener)
        listener._history_cache = {}
        listener._history_cache_lock = threading.RLock()
        calls = []

        def request(_path, query=None):
            calls.append(dict(query or {}))
            return {
                "success": True,
                "messages": [
                    {
                        "serverId": "burst-1",
                        "senderUsername": "wxid_1",
                        "content": "第一条",
                    },
                    {
                        "serverId": "burst-2",
                        "senderUsername": "wxid_2",
                        "content": "第二条",
                    },
                ],
            }

        listener._request_json = request

        first = listener._media_for_message(
            "room@chatroom", "burst-1", include_media=False
        )
        second = listener._media_for_message(
            "room@chatroom", "burst-2", include_media=False
        )

        self.assertEqual(first["senderUsername"], "wxid_1")
        self.assertEqual(second["senderUsername"], "wxid_2")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["limit"], 10)

    def test_plain_history_expands_only_when_fast_window_misses(self):
        listener = WeFlowListener.__new__(WeFlowListener)
        listener._history_cache = {}
        listener._history_cache_lock = threading.RLock()
        calls = []

        def request(_path, query=None):
            query = dict(query or {})
            calls.append(query)
            if query["limit"] == 10:
                return {
                    "success": True,
                    "messages": [
                        {
                            "serverId": "newer-message",
                            "senderUsername": "wxid_newer",
                        }
                    ],
                }
            return {
                "success": True,
                "messages": [
                    {
                        "serverId": "older-target",
                        "senderUsername": "wxid_target",
                        "quote": {
                            "serverId": "quoted-message",
                            "content": "你好",
                        },
                    }
                ],
            }

        listener._request_json = request

        result = listener._media_for_message(
            "room@chatroom", "older-target", include_media=False
        )

        self.assertEqual(result["senderUsername"], "wxid_target")
        self.assertEqual(result["quote"]["content"], "你好")
        self.assertEqual([call["limit"] for call in calls], [10, 100])
        self.assertTrue(all("media" not in call for call in calls))

    def test_media_history_keeps_single_full_query(self):
        listener = WeFlowListener.__new__(WeFlowListener)
        listener._history_cache = {}
        listener._history_cache_lock = threading.RLock()
        calls = []

        def request(_path, query=None):
            calls.append(dict(query or {}))
            return {
                "success": True,
                "messages": [
                    {
                        "serverId": "voice-1",
                        "senderUsername": "wxid_voice",
                        "mediaUrl": "http://127.0.0.1/media/voice-1.silk",
                    }
                ],
            }

        listener._request_json = request

        result = listener._media_for_message(
            "room@chatroom", "voice-1", include_media=True
        )

        self.assertEqual(result["serverId"], "voice-1")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["limit"], 100)
        self.assertEqual(calls[0]["media"], 1)

    def test_plain_group_text_does_not_enter_media_export_path(self):
        payload = {"content": "普通群消息", "sessionType": "group"}
        self.assertTrue(
            WeFlowListener._is_plain_text_message(payload, "普通群消息")
        )
        self.assertFalse(
            WeFlowListener._is_plain_text_message(payload, "[图片]")
        )

    def test_session_mailbox_preserves_order_and_schedules_once(self):
        class CapturingExecutor:
            def __init__(self):
                self.callbacks = []

            def submit(self, callback, *args):
                self.callbacks.append((callback, args))
                return callback

        listener = WeFlowListener.__new__(WeFlowListener)
        listener._dispatch_executor = CapturingExecutor()
        listener._dispatch_lock = threading.RLock()
        listener._dispatch_condition = threading.Condition(listener._dispatch_lock)
        listener._dispatch_queues = {}
        listener._dispatch_active_sessions = set()
        listener._dispatch_pending = 0
        listener.stop_event = threading.Event()
        processed = []
        listener._dispatch_sse_frame = lambda _event, lines: processed.append(
            json.loads("\n".join(lines))["rawid"]
        )
        listener._submit_sse_frame(
            "message.new",
            [json.dumps({"sessionId": "room@chatroom", "rawid": "1"})],
        )
        listener._submit_sse_frame(
            "message.new",
            [json.dumps({"sessionId": "room@chatroom", "rawid": "2"})],
        )

        self.assertEqual(len(listener._dispatch_executor.callbacks), 1)
        callback, args = listener._dispatch_executor.callbacks.pop(0)
        callback(*args)
        self.assertEqual(processed, ["1", "2"])
        self.assertEqual(listener._dispatch_pending, 0)

    def test_busy_session_yields_to_other_sessions(self):
        class CapturingExecutor:
            def __init__(self):
                self.callbacks = []

            def submit(self, callback, *args):
                self.callbacks.append((callback, args))

        listener = WeFlowListener.__new__(WeFlowListener)
        listener._dispatch_executor = CapturingExecutor()
        listener._dispatch_lock = threading.RLock()
        listener._dispatch_condition = threading.Condition(listener._dispatch_lock)
        listener._dispatch_queues = {}
        listener._dispatch_active_sessions = set()
        listener._dispatch_pending = 0
        listener.stop_event = threading.Event()
        processed = []
        listener._dispatch_sse_frame = lambda _event, lines: processed.append(
            json.loads("\n".join(lines))["rawid"]
        )

        for index in range(10):
            listener._submit_sse_frame(
                "message.new",
                [json.dumps({"sessionId": "busy", "rawid": f"a{index}"})],
            )
        listener._submit_sse_frame(
            "message.new",
            [json.dumps({"sessionId": "quiet", "rawid": "b0"})],
        )

        callback, args = listener._dispatch_executor.callbacks.pop(0)
        callback(*args)
        callback, args = listener._dispatch_executor.callbacks.pop(0)
        callback(*args)
        self.assertEqual(processed[:9], [f"a{i}" for i in range(8)] + ["b0"])

    def test_persisted_replay_is_skipped_before_history_lookup(self):
        events = []
        listener = self._listener(events.append)
        listener._known_message_checker = lambda rawid, event: (
            rawid == "known-1" and event == "message.new"
        )
        listener._media_for_message = lambda *args, **kwargs: self.fail(
            "已处理重放消息不应查询历史"
        )
        payload = {
            "event": "message.new",
            "rawid": "known-1",
            "sessionId": "wxid_sender",
            "sessionType": "other",
            "sourceName": "元宝原宝",
            "content": "旧消息",
            "timestamp": 1785300862,
        }

        listener._dispatch_sse_frame("message.new", [json.dumps(payload)])

        self.assertEqual(events, [])
        self.assertIn(("message.new", "known-1"), listener._seen)


if __name__ == "__main__":
    import unittest

    unittest.main()
