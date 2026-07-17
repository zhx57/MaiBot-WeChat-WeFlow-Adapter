import asyncio
import base64
import json
import os
import sqlite3
import sys
import tempfile
import threading
import types
import unittest
from unittest.mock import AsyncMock, Mock, patch

try:
    import maim_message  # noqa: F401
except ImportError:
    maim_message = types.ModuleType("maim_message")
    for name in (
        "BaseMessageInfo", "FormatInfo", "GroupInfo", "MessageBase",
        "ReceiverInfo", "RouteConfig", "Router", "Seg", "SenderInfo",
        "TargetConfig", "UserInfo",
    ):
        setattr(maim_message, name, type(name, (), {}))
    sys.modules["maim_message"] = maim_message

from wx_Processor import MessageProcessor, OutboundDeliveryError
from wx_Processer import MessageProcessor as LegacyMessageProcessor
from wx_Listener import _InboundEvent


PNG = b"\x89PNG\r\n\x1a\n" + b"production-media-test"
PNG_BASE64 = base64.b64encode(PNG).decode("ascii")
GIF = b"GIF89a" + b"production-animated-emoji"


class MessageProcessorMediaTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.processor = MessageProcessor.__new__(MessageProcessor)
        self.processor._queue_outbound = AsyncMock()
        self.processor._cleanup_tasks = set()

    def test_legacy_module_name_exports_same_processor(self):
        self.assertIs(MessageProcessor, LegacyMessageProcessor)

    async def test_router_start_wait_is_cancelled_when_stop_is_requested(self):
        router_task = asyncio.create_task(asyncio.sleep(60))
        self.processor._router_task = router_task
        self.processor._stop_requested = types.SimpleNamespace(is_set=lambda: True)

        with self.assertRaises(asyncio.CancelledError):
            await self.processor._wait_router_ready()

        await asyncio.gather(router_task, return_exceptions=True)
        self.assertTrue(router_task.cancelled())

    def test_router_connection_probe_is_signature_checked(self):
        self.processor.platform = "test"
        self.processor.router = types.SimpleNamespace(
            check_connection=lambda: True,
        )

        with self.assertRaisesRegex(RuntimeError, "签名不兼容"):
            self.processor._router_is_connected()

    async def test_unknown_ui_outcome_is_never_retried(self):
        class UnknownOutcome(RuntimeError):
            retry_safe = False

        self.processor.ui_submit = Mock()
        self.processor._run_blocking = AsyncMock(
            side_effect=UnknownOutcome("possibly sent")
        )

        with self.assertRaises(OutboundDeliveryError) as raised:
            await self.processor._deliver_with_retry("项目群", "text", "一次")

        self.assertFalse(raised.exception.retry_safe)
        self.processor._run_blocking.assert_awaited_once()

    async def _sent_image(self, segment):
        captured = b""

        async def capture(_receiver, kind, path):
            nonlocal captured
            self.assertEqual("image", kind)
            self.assertTrue(os.path.isfile(path))
            with open(path, "rb") as stream:
                captured = stream.read()

        self.processor._queue_outbound.side_effect = capture
        await self.processor._process_segments(segment, "chat")
        path = self.processor._queue_outbound.await_args.args[2]
        self.assertFalse(os.path.exists(path))
        self.assertEqual(PNG, captured)

    async def test_image_base64_is_sent_as_file_and_cleaned_up(self):
        await self._sent_image({"type": "image", "data": PNG_BASE64})

    async def test_unknown_image_outcome_defers_temporary_file_cleanup(self):
        class UnknownOutcome(RuntimeError):
            retry_safe = False
            cleanup_delay = 10.0

        self.processor._queue_outbound.side_effect = OutboundDeliveryError(
            UnknownOutcome("possibly sent")
        )
        self.processor._defer_temporary_cleanup = Mock()

        with self.assertRaises(OutboundDeliveryError):
            await self.processor._send_image_segment("chat", PNG_BASE64)

        path = self.processor._queue_outbound.await_args.args[2]
        try:
            self.assertTrue(os.path.exists(path))
            self.processor._defer_temporary_cleanup.assert_called_once_with(
                path,
                command_future=None,
                delay=10.0,
            )
        finally:
            os.unlink(path)

    async def test_emoji_base64_is_sent_as_image_not_text(self):
        await self._sent_image({"type": "emoji", "data": PNG_BASE64})

    async def test_text_image_data_uri_is_promoted_to_image(self):
        await self._sent_image({
            "type": "text",
            "data": f"DATA:image/png;charset=utf-8;base64,{PNG_BASE64}",
        })

    async def test_short_textual_emoji_remains_text(self):
        await self.processor._process_segments(
            {"type": "emoji", "data": "[微笑]"}, "chat"
        )
        self.processor._queue_outbound.assert_awaited_once_with("chat", "text", "[微笑]")

    async def test_invalid_media_like_emoji_is_never_sent_as_text(self):
        with self.assertRaisesRegex(ValueError, "无效或过长的媒体数据"):
            await self.processor._process_segments(
                {"type": "emoji", "data": "A" * 128}, "chat"
            )
        self.processor._queue_outbound.assert_not_awaited()

    async def test_invalid_seglist_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "必须是数组"):
            await self.processor._process_segments(
                {"type": "seglist", "data": '{"type":"image"}'}, "chat"
            )

    async def test_mixed_segments_preserve_delivery_order(self):
        sent = []

        async def capture(_receiver, kind, data):
            if kind == "image":
                with open(data, "rb") as stream:
                    data = stream.read()
            sent.append((kind, data))

        self.processor._queue_outbound.side_effect = capture
        await self.processor._process_segments({
            "type": "seglist",
            "data": [
                {"type": "text", "data": "before"},
                {"type": "image", "data": PNG_BASE64},
                {"type": "text", "data": "after"},
            ],
        }, "chat")
        self.assertEqual([("text", "before"), ("image", PNG), ("text", "after")], sent)

    async def test_concatenated_image_urls_are_sent_separately(self):
        urls = ["https://images.example/a.png", "https://images.example/b.png"]
        paths = []

        def download(url):
            path, temporary = self.processor._prepare_image(PNG_BASE64)
            self.assertTrue(temporary)
            paths.append((url, path))
            return path, True

        sent = []

        async def capture(_receiver, kind, path):
            sent.append((kind, paths[-1][0]))

        self.processor._download_image = download
        self.processor._queue_outbound.side_effect = capture
        await self.processor._process_segments(
            {"type": "text", "data": "".join(urls)}, "chat"
        )
        self.assertEqual([("image", urls[0]), ("image", urls[1])], sent)
        self.assertTrue(all(not os.path.exists(path) for _, path in paths))

    async def test_markdown_images_are_sent_separately(self):
        urls = ["https://images.example/a", "https://images.example/b"]
        downloaded = []

        def download(url):
            downloaded.append(url)
            return self.processor._prepare_image(PNG_BASE64)

        self.processor._download_image = download
        await self.processor._process_segments(
            {"type": "text", "data": f"![a]({urls[0]})\n![b]({urls[1]})"}, "chat"
        )
        self.assertEqual(urls, downloaded)
        self.assertEqual(2, self.processor._queue_outbound.await_count)

    async def test_text_with_explanation_and_url_remains_text(self):
        text = "参考图片：https://images.example/a.png"
        await self.processor._process_segments({"type": "text", "data": text}, "chat")
        self.processor._queue_outbound.assert_awaited_once_with("chat", "text", text)

    async def test_image_url_list_is_sent_separately(self):
        urls = ["https://images.example/a", "https://images.example/b"]
        downloaded = []

        def download(url):
            downloaded.append(url)
            return self.processor._prepare_image(PNG_BASE64)

        self.processor._download_image = download
        await self.processor._process_segments(
            {"type": "image", "data": [{"url": url} for url in urls]}, "chat"
        )
        self.assertEqual(urls, downloaded)
        self.assertEqual(2, self.processor._queue_outbound.await_count)

    def test_private_image_url_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "非公网地址"):
            self.processor._validate_public_url("http://127.0.0.1/image.png")

    def test_ambiguous_image_source_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "只能指定一种"):
            self.processor._prepare_image({
                "base64": PNG_BASE64,
                "url": "https://images.example/a.png",
            })

    def test_prepare_image_accepts_wrapped_whitespace(self):
        path, temporary = self.processor._prepare_image({
            "data": f"data:image/png;base64,\n{PNG_BASE64}\n",
        })
        try:
            self.assertTrue(temporary)
            with open(path, "rb") as stream:
                self.assertEqual(PNG, stream.read())
        finally:
            os.unlink(path)

    def test_non_base64_data_uri_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "必须使用 base64"):
            self.processor._prepare_image("data:image/svg+xml,%3Csvg%3E")

    def test_saved_inbound_image_becomes_maibot_image_segment(self):
        with tempfile.NamedTemporaryFile(suffix=".png") as stream:
            stream.write(PNG)
            stream.flush()
            with patch(
                "wx_Processor.Seg",
                side_effect=lambda **values: types.SimpleNamespace(**values),
            ):
                segment = self.processor._inbound_segment(stream.name, "image")

        self.assertEqual(segment.type, "image")
        self.assertEqual(base64.b64decode(segment.data), PNG)

    def test_declared_inbound_image_requires_saved_local_file(self):
        with self.assertRaisesRegex(ValueError, "缺少已保存"):
            self.processor._inbound_segment(
                os.path.join(tempfile.gettempdir(), "missing-wemai-image.png"),
                "image",
            )

    def test_saved_animated_gif_becomes_maibot_emoji_segment(self):
        with tempfile.NamedTemporaryFile(suffix=".gif") as stream:
            stream.write(GIF)
            stream.flush()
            with patch(
                "wx_Processor.Seg",
                side_effect=lambda **values: types.SimpleNamespace(**values),
            ):
                segment = self.processor._inbound_segment(stream.name, "emoji")

        self.assertEqual(segment.type, "emoji")
        self.assertEqual(base64.b64decode(segment.data), GIF)

    def test_weflow_inbound_event_preserves_api_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            processor = self._new_storage_processor(
                os.path.join(directory, "inbound.sqlite3")
            )
            event = _InboundEvent(
                group="项目群",
                content="你好",
                timestamp=1760000123,
                raw={
                    "event": "message.new",
                    "rawid": "6116895530414915131",
                    "sessionId": "team@chatroom",
                    "sessionType": "group",
                    "content": "你好",
                },
                sender="李四",
                chat_type="group",
                avatar_url="https://example.com/avatar.jpg",
            )

            result = processor.enqueue_inbound_event(event)

            self.assertTrue(result["success"])
            with sqlite3.connect(processor._db_path) as db:
                payload = json.loads(
                    db.execute("SELECT payload FROM inbound").fetchone()[0]
                )
            self.assertEqual(payload["data"]["rawid"], "6116895530414915131")
            self.assertEqual(payload["data"]["raw_content"], "你好")
            self.assertEqual(
                payload["data"]["avatar_url"],
                "https://example.com/avatar.jpg",
            )

    def test_downloaded_inbound_file_becomes_documented_text_segment(self):
        with tempfile.NamedTemporaryFile(suffix=".txt") as stream:
            stream.write(b"weflow file")
            stream.flush()
            with patch(
                "wx_Processor.Seg",
                side_effect=lambda **values: types.SimpleNamespace(**values),
            ):
                segment = self.processor._inbound_segment(stream.name, "file")

        self.assertEqual(segment.type, "text")
        self.assertEqual(segment.data, f"[文件消息：{os.path.basename(stream.name)}]")

    def test_downloaded_inbound_voice_becomes_documented_text_segment(self):
        with tempfile.NamedTemporaryFile(suffix=".silk") as stream:
            voice = b"weflow voice"
            stream.write(voice)
            stream.flush()
            with patch(
                "wx_Processor.Seg",
                side_effect=lambda **values: types.SimpleNamespace(**values),
            ):
                segment = self.processor._inbound_segment(stream.name, "voice")

        self.assertEqual(segment.type, "voice")
        self.assertEqual(base64.b64decode(segment.data), voice)

    def test_missing_file_and_voice_media_still_use_text_fallbacks(self):
        with patch(
            "wx_Processor.Seg",
            side_effect=lambda **values: types.SimpleNamespace(**values),
        ):
            file_segment = self.processor._inbound_segment("report.pdf", "file")
            voice_segment = self.processor._inbound_segment("[语音]", "voice")

        self.assertEqual((file_segment.type, file_segment.data), (
            "text", "[文件消息：report.pdf]"
        ))
        self.assertEqual((voice_segment.type, voice_segment.data), (
            "text", "[语音消息]"
        ))

    def test_user_info_passes_documented_nickname_fields(self):
        captured = {}

        def user_info(platform, user_id, user_nickname, user_cardname):
            captured.update(locals())
            return captured

        with patch("wx_Processor.UserInfo", user_info):
            result = self.processor._build_user_info(
                platform="test",
                user_id="wxid_user",
                user_nickname="李四",
                user_cardname="群昵称",
            )

        self.assertIs(result, captured)
        self.assertEqual(captured["user_nickname"], "李四")
        self.assertEqual(captured["user_cardname"], "群昵称")

    def test_message_uses_only_documented_fields_and_seglist_wrapper(self):
        calls = {}

        def model(name):
            def construct(**values):
                calls.setdefault(name, []).append(values)
                return types.SimpleNamespace(**values)
            return construct

        processor = MessageProcessor.__new__(MessageProcessor)
        processor.platform = "test"
        processor._remember = Mock()
        processor._schedule_avatar_cache = Mock()
        with (
            patch("wx_Processor.UserInfo", model("UserInfo")),
            patch("wx_Processor.GroupInfo", model("GroupInfo")),
            patch("wx_Processor.Seg", model("Seg")),
            patch("wx_Processor.BaseMessageInfo", model("BaseMessageInfo")),
            patch("wx_Processor.MessageBase", model("MessageBase")),
        ):
            message = processor._build_message("项目群", {
                "chat_type": "group",
                "sender": "李四",
                "type": "text",
                "content": "你好",
                "timestamp": 1760000123.9,
                "avatar_url": "https://example.com/avatar.jpg",
            })

        self.assertEqual(calls["UserInfo"][0], {
            "platform": "test",
            "user_id": processor._stable_id("group", "项目群|李四"),
            "user_nickname": "李四",
            "user_cardname": "李四",
        })
        processor._schedule_avatar_cache.assert_called_once_with(
            "https://example.com/avatar.jpg",
            processor._stable_id("group", "项目群|李四"),
            group_id=processor._stable_id("group", "项目群"),
        )
        self.assertEqual(set(calls["GroupInfo"][0]), {"platform", "group_id", "group_name"})
        self.assertEqual(set(calls["BaseMessageInfo"][0]), {
            "platform", "message_id", "time", "user_info", "group_info"
        })
        self.assertIsInstance(calls["BaseMessageInfo"][0]["time"], int)
        self.assertEqual(calls["BaseMessageInfo"][0]["time"], 1760000123)
        self.assertEqual(calls["Seg"][0], {"type": "text", "data": "你好"})
        self.assertEqual(calls["Seg"][1]["type"], "seglist")
        self.assertEqual(calls["Seg"][1]["data"], [message.message_segment.data[0]])
        self.assertEqual(set(calls["MessageBase"][0]), {
            "message_info", "message_segment", "raw_message"
        })
        self.assertEqual(message.raw_message, "你好")

    def test_group_card_name_is_preferred_and_private_card_is_none(self):
        processor = MessageProcessor.__new__(MessageProcessor)
        processor.platform = "test"
        processor._remember = Mock()
        processor._schedule_avatar_cache = Mock()
        processor._inbound_segment = Mock(
            return_value=types.SimpleNamespace(type="text", data="hello")
        )
        processor._build_user_info = Mock(
            side_effect=lambda **values: types.SimpleNamespace(**values)
        )
        model = lambda **values: types.SimpleNamespace(**values)

        with (
            patch("wx_Processor.GroupInfo", side_effect=model),
            patch("wx_Processor.Seg", side_effect=model),
            patch("wx_Processor.BaseMessageInfo", side_effect=model),
            patch("wx_Processor.MessageBase", side_effect=model),
        ):
            processor._build_message("项目群", {
                "chat_type": "group",
                "sender": "李四",
                "group_card_name": "项目负责人",
                "content": "hello",
                "timestamp": 1,
            })
            processor._build_message("张三", {
                "chat_type": "private",
                "sender": "张三",
                "group_card_name": "不应使用",
                "content": "hello",
                "timestamp": 2,
            })

        group_user, private_user = [
            call.kwargs for call in processor._build_user_info.call_args_list
        ]
        self.assertEqual(group_user["user_nickname"], "李四")
        self.assertEqual(group_user["user_cardname"], "项目负责人")
        self.assertEqual(private_user["user_nickname"], "张三")
        self.assertIsNone(private_user["user_cardname"])

    def test_all_maibot_assertion_fields_are_correct_types(self):
        """Verify every field MaiBot's from_maim_message asserts on is the correct type."""
        calls = {}

        def model(name):
            def construct(**values):
                calls.setdefault(name, []).append(values)
                return types.SimpleNamespace(**values)
            return construct

        processor = MessageProcessor.__new__(MessageProcessor)
        processor.platform = "wx4py"
        processor._remember = Mock()
        processor._schedule_avatar_cache = Mock()
        with (
            patch("wx_Processor.UserInfo", model("UserInfo")),
            patch("wx_Processor.GroupInfo", model("GroupInfo")),
            patch("wx_Processor.Seg", model("Seg")),
            patch("wx_Processor.BaseMessageInfo", model("BaseMessageInfo")),
            patch("wx_Processor.MessageBase", model("MessageBase")),
        ):
            # Group message
            processor._build_message("测试群", {
                "chat_type": "group",
                "sender": "张三",
                "type": "text",
                "content": "hello",
                "timestamp": 1760000123.9,
                "rawid": "evt_001",
            })
            # Private message
            processor._build_message("李四", {
                "chat_type": "private",
                "sender": "李四",
                "type": "text",
                "content": "hi",
                "timestamp": 1760000124.0,
                "rawid": "evt_002",
            })

        # --- Group message assertions ---
        grp_info = calls["GroupInfo"][0]
        # MaiBot: assert isinstance(grp_info.group_id, str)
        self.assertIsInstance(grp_info["group_id"], str)
        self.assertTrue(grp_info["group_id"])
        # MaiBot: assert isinstance(grp_info.group_name, str)
        self.assertIsInstance(grp_info["group_name"], str)
        self.assertTrue(grp_info["group_name"])
        self.assertEqual(grp_info["group_name"], "测试群")

        # --- Common assertions (both messages) ---
        for idx in range(2):
            msg_info = calls["BaseMessageInfo"][idx]
            user_info = calls["UserInfo"][idx]
            # MaiBot: assert isinstance(platform, str)
            self.assertIsInstance(msg_info["platform"], str)
            # MaiBot: assert isinstance(msg_id, str) and assert msg_id
            self.assertIsInstance(msg_info["message_id"], str)
            self.assertTrue(msg_info["message_id"])
            # MaiBot: assert timestamp
            self.assertTrue(msg_info["time"])
            # MaiBot: assert isinstance(usr_info.user_id, str)
            self.assertIsInstance(user_info["user_id"], str)
            self.assertTrue(user_info["user_id"])
            # MaiBot: assert isinstance(usr_info.user_nickname, str)
            self.assertIsInstance(user_info["user_nickname"], str)
            self.assertTrue(user_info["user_nickname"])

        # Private message should have group_info=None
        self.assertIsNone(calls["BaseMessageInfo"][1]["group_info"])

    def test_avatar_cache_writes_normalized_user_and_group_paths(self):
        processor = MessageProcessor.__new__(MessageProcessor)
        processor.platform = "Wx 4/py"
        avatar = b"\x89PNG\r\n\x1a\ncache-avatar"
        processor._download_avatar = Mock(return_value=(avatar, ".png"))

        with tempfile.TemporaryDirectory() as directory:
            with patch("wx_Processor.MAIBOT_DATA_DIR", directory):
                processor._cache_avatar(
                    "https://example.com/avatar.png",
                    "user:123",
                    group_id="group/456",
                )
                cache_dir = os.path.join(directory, "avatar", "wx_4_py")
                with open(os.path.join(cache_dir, "user_123.png"), "rb") as stream:
                    self.assertEqual(stream.read(), avatar)
                with open(
                    os.path.join(cache_dir, "group_group_456.png"), "rb"
                ) as stream:
                    self.assertEqual(stream.read(), avatar)

                processor._cache_avatar(
                    "https://example.com/avatar.png",
                    "user:123",
                    group_id="group/456",
                )

        processor._download_avatar.assert_called_once_with(
            "https://example.com/avatar.png"
        )

    def test_router_uses_documented_legacy_constructor_arguments(self):
        calls = {}

        def target_config(**values):
            calls["target"] = values
            return types.SimpleNamespace(**values)

        def route_config(**values):
            calls["route"] = values
            return types.SimpleNamespace(**values)

        class RouterStub:
            def __init__(self, route):
                self.route = route
                calls["router"] = (route,)

            def register_class_handler(self, handler):
                calls["handler"] = handler

        with (
            patch.object(MessageProcessor, "_init_storage"),
            patch.object(MessageProcessor, "_load_id_map"),
            patch("wx_Processor.TargetConfig", target_config),
            patch("wx_Processor.RouteConfig", route_config),
            patch("wx_Processor.Router", RouterStub),
        ):
            processor = MessageProcessor(platform="test")

        self.assertEqual(calls["target"], {
            "url": __import__("wx_Processor").MAIBOT_API_URL,
            "token": None,
        })
        self.assertEqual(set(calls["route"]), {"route_config"})
        self.assertEqual(calls["router"], (processor.router.route,))
        self.assertEqual(calls["handler"], processor._handle_maibot_response)

    async def test_per_receiver_workers_are_parallel_and_same_receiver_is_ordered(self):
        processor = MessageProcessor.__new__(MessageProcessor)
        processor._send_queue = asyncio.Queue()
        processor._receiver_send_queues = {}
        processor._receiver_send_tasks = {}
        started = []
        release = asyncio.Event()

        async def deliver(receiver, _kind, data):
            started.append((receiver, data))
            if data in {"a1", "b1"}:
                await release.wait()

        processor._deliver_with_retry = deliver
        dispatcher = asyncio.create_task(processor._process_send_queue())
        sends = [
            asyncio.create_task(processor._queue_outbound("A", "text", "a1")),
            asyncio.create_task(processor._queue_outbound("A", "text", "a2")),
            asyncio.create_task(processor._queue_outbound("B", "text", "b1")),
        ]
        try:
            for _ in range(20):
                if len(started) >= 2:
                    break
                await asyncio.sleep(0)
            self.assertCountEqual(started, [("A", "a1"), ("B", "b1")])

            release.set()
            await asyncio.gather(*sends)
            self.assertEqual(
                [data for receiver, data in started if receiver == "A"],
                ["a1", "a2"],
            )
        finally:
            dispatcher.cancel()
            for task in processor._receiver_send_tasks.values():
                task.cancel()
            await asyncio.gather(
                dispatcher,
                *processor._receiver_send_tasks.values(),
                return_exceptions=True,
            )

    @staticmethod
    def _new_storage_processor(db_path):
        processor = MessageProcessor.__new__(MessageProcessor)
        processor.inbound_enabled = True
        processor.platform = "test"
        processor._db_path = db_path
        processor._loop = None
        processor._init_storage()
        return processor

    def test_durable_inbound_queue_does_not_drop_at_outbound_queue_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            processor = self._new_storage_processor(
                os.path.join(directory, "inbound.sqlite3")
            )
            processor._build_message = Mock(
                side_effect=AssertionError("enqueue must not build media")
            )
            base = {
                "chat_type": "group",
                "sender": "小王",
                "type": "text",
                "content": "消息",
            }

            with patch("wx_Processor.SEND_QUEUE_SIZE", 1):
                first = processor.enqueue_message(
                    "项目群", {**base, "timestamp": 1.0}
                )
                second = processor.enqueue_message(
                    "项目群", {**base, "timestamp": 2.0}
                )

            self.assertTrue(first["success"])
            self.assertTrue(second["success"])
            processor._build_message.assert_not_called()
            with sqlite3.connect(processor._db_path) as db:
                self.assertEqual(
                    db.execute(
                        "SELECT COUNT(*) FROM inbound WHERE state='pending'"
                    ).fetchone()[0],
                    2,
                )

    async def test_enqueue_wakes_consumer_without_quarter_second_poll(self):
        with tempfile.TemporaryDirectory() as directory:
            processor = self._new_storage_processor(
                os.path.join(directory, "inbound.sqlite3")
            )
            processor._loop = asyncio.get_running_loop()
            processor._inbound_wakeup = asyncio.Event()

            result = processor.enqueue_message("张总", {
                "chat_type": "private",
                "sender": "张总",
                "type": "text",
                "content": "你好",
                "timestamp": 1.0,
            })
            await asyncio.sleep(0)

            self.assertTrue(result["success"])
            self.assertTrue(processor._inbound_wakeup.is_set())

    async def test_inbound_consumer_drains_multiple_ready_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            processor = self._new_storage_processor(
                os.path.join(directory, "inbound.sqlite3")
            )
            processor._stop_requested = threading.Event()
            processor._inbound_wakeup = asyncio.Event()
            processor._build_message = lambda _chat, data: data["content"]
            sent = []

            async def send(message):
                sent.append(message)
                if len(sent) == 3:
                    processor._stop_requested.set()

            processor.router = types.SimpleNamespace(
                check_connection=lambda _platform: True,
                send_message=send,
            )
            for index in range(3):
                processor.enqueue_message("项目群", {
                    "chat_type": "group",
                    "sender": "小王",
                    "type": "text",
                    "content": f"消息{index}",
                    "timestamp": float(index),
                })

            await asyncio.wait_for(processor._process_inbound_queue(), 0.5)

            self.assertEqual(sent, ["消息0", "消息1", "消息2"])


if __name__ == "__main__":
    unittest.main()
