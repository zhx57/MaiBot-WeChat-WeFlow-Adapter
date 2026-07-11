import base64
import os
import sys
import types
import unittest
from unittest.mock import AsyncMock

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

from wx_Processer import MessageProcessor


PNG = b"\x89PNG\r\n\x1a\n" + b"production-media-test"
PNG_BASE64 = base64.b64encode(PNG).decode("ascii")


class MessageProcessorMediaTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.processor = MessageProcessor.__new__(MessageProcessor)
        self.processor._queue_outbound = AsyncMock()
        self.processor._cleanup_tasks = set()

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


if __name__ == "__main__":
    unittest.main()
