import json
import os
import tempfile
import threading
import unittest
from unittest.mock import patch

from weflow_listener import WeFlowListener
from outgoing_registry import OutgoingMessageRegistry


PNG = b"\x89PNG\r\n\x1a\n" + b"weflow-test-image"
GIF = b"GIF89a" + b"weflow-test-animation"


class _Headers(dict):
    def get_content_type(self):
        return self.get("Content-Type", "application/octet-stream").split(";", 1)[0]


class _Response:
    def __init__(self, body=b"", *, lines=None, content_type="application/json"):
        self._body = body
        self._lines = list(lines or [])
        self.headers = _Headers({"Content-Type": content_type})
        self.closed = False

    def __iter__(self):
        return iter(self._lines)

    def read(self, size=-1):
        if not self._body:
            return b""
        if size < 0:
            result, self._body = self._body, b""
        else:
            result, self._body = self._body[:size], self._body[size:]
        return result

    def close(self):
        self.closed = True


class _Opener:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    def open(self, request, timeout=None):
        self.requests.append((request, timeout))
        if not self.responses:
            raise AssertionError("unexpected HTTP request")
        return self.responses.pop(0)


def _payload(**overrides):
    result = {
        "event": "message.new",
        "sessionId": "team@chatroom",
        "sessionType": "group",
        "rawid": "123456789",
        "avatarUrl": "https://example.com/avatar.jpg",
        "sourceName": "李四",
        "groupName": "项目群",
        "content": "你好",
        "timestamp": 1760000123,
    }
    result.update(overrides)
    return result


class WeFlowListenerTests(unittest.TestCase):
    def _listener(self, targets, callback=None, opener=None, registry=None):
        return WeFlowListener(
            target_chats=targets,
            callback=callback,
            stop_event=threading.Event(),
            api_url="http://127.0.0.1:5031",
            api_token="secret-token",
            opener=opener or _Opener(),
            outgoing_registry=registry,
        )

    def test_token_is_required(self):
        with self.assertRaisesRegex(ValueError, "WEFLOW_API_TOKEN"):
            WeFlowListener(api_token="", target_chats=[])

    def test_emoji_marker_accepts_known_labels_descriptions_and_xml(self):
        markers = (
            "[表情]",
            "[动画表情]",
            "[Sticker]",
            "[Emoji]",
            "【动态表情包：开心】",
            "[表情 微笑]",
            "[动画表情] 开心",
            "表情：开心",
            "<msg><emoji md5=\"abc\" /></msg>",
            "<msg type=\"sticker\" />",
        )
        for marker in markers:
            with self.subTest(marker=marker):
                self.assertTrue(WeFlowListener._is_emoji_marker(marker))

        self.assertFalse(WeFlowListener._is_emoji_marker("一起讨论 emoji 格式"))
        self.assertFalse(WeFlowListener._is_emoji_marker("[图片]"))

    def test_voice_marker_accepts_labels_descriptions_xml_and_empty_content(self):
        markers = (
            "[语音]",
            "[语音消息]",
            "[Voice]",
            "[Voice Message]",
            "[语音] 5\"",
            "语音消息 3秒",
            "<msg><voicemsg voicelength=\"3000\" /></msg>",
            "<msg type=\"audio\" />",
            "",
        )
        for marker in markers:
            with self.subTest(marker=marker):
                self.assertTrue(WeFlowListener._is_voice_marker(marker))

        self.assertFalse(WeFlowListener._is_voice_marker("一起讨论 voice 格式"))
        self.assertFalse(WeFlowListener._is_voice_marker("[图片]"))

    def test_voice_media_accepts_type_fields_nested_media_and_urls(self):
        messages = (
            {"mediaType": "voice"},
            {"mediaType": "audio"},
            {"messageType": "Voice Message"},
            {"media": {"content_type": "audio_message"}},
            {"mediaUrl": "/api/v1/media/voice/sample.silk"},
            {"mediaUrl": "/api/v1/media/audio.m4a"},
        )
        for message in messages:
            with self.subTest(message=message):
                self.assertTrue(WeFlowListener._is_voice_media(message))

        self.assertFalse(WeFlowListener._is_voice_media({"mediaType": "image"}))

    def test_run_forever_reconnects_after_disconnect(self):
        listener = self._listener(["项目群"])
        attempts = []

        def consume():
            attempts.append(len(attempts) + 1)
            if len(attempts) == 1:
                raise ConnectionError("disconnected")
            listener.stop_event.set()

        with patch.object(listener, "_consume_once", side_effect=consume):
            with patch.object(listener.stop_event, "wait", return_value=False):
                listener.run_forever()

        self.assertEqual(attempts, [1, 2])
        self.assertFalse(listener.running)

    def test_sse_parser_uses_query_token_and_bearer_header(self):
        received = []
        body = json.dumps(_payload(), ensure_ascii=False).encode("utf-8")
        response = _Response(
            lines=[b"event: message.new\n", b"data: " + body + b"\n", b"\n"],
            content_type="text/event-stream; charset=utf-8",
        )
        opener = _Opener(response)
        listener = self._listener(["项目群"], opener=opener)
        listener.callback = lambda event: (received.append(event), listener.stop_event.set())

        with patch.object(listener, "_media_for_message", return_value=None):
            listener._consume_once()

        self.assertEqual(len(received), 1)
        event = received[0]
        self.assertEqual(event.group, "项目群")
        self.assertEqual(event.chat_type, "group")
        self.assertEqual(event.sender, "李四")
        self.assertEqual(event.avatar_url, "https://example.com/avatar.jpg")
        request = opener.requests[0][0]
        self.assertIn("access_token=secret-token", request.full_url)
        self.assertEqual(request.get_header("Authorization"), "Bearer secret-token")
        self.assertEqual(request.get_header("Accept"), "text/event-stream")

    def test_filters_revoke_unknown_targets_and_duplicates(self):
        received = []
        listener = self._listener(["项目群"], callback=received.append)
        with patch.object(listener, "_media_for_message", return_value=None):
            listener._dispatch_sse_frame(
                "message.revoke",
                [json.dumps(_payload(event="message.revoke"))],
            )
            listener._dispatch_sse_frame(
                "message.new",
                [json.dumps(_payload(groupName="其他群", rawid="2"))],
            )
            frame = json.dumps(_payload())
            listener._dispatch_sse_frame("message.new", [frame])
            listener._dispatch_sse_frame("message.new", [frame])

        self.assertEqual(len(received), 1)

    def test_outgoing_reservation_filters_immediate_sse_echo_once(self):
        received = []
        registry = OutgoingMessageRegistry()
        registry.reserve("项目群", "机器人回复")
        listener = self._listener(
            ["项目群"],
            callback=received.append,
            registry=registry,
        )
        first = _payload(rawid="outgoing-1", content="机器人回复")
        second = _payload(rawid="incoming-1", content="机器人回复")

        with patch.object(listener, "_media_for_message", return_value=None):
            listener._dispatch_sse_frame("message.new", [json.dumps(first)])
            listener._dispatch_sse_frame("message.new", [json.dumps(second)])

        self.assertEqual([event.raw["rawid"] for event in received], ["incoming-1"])

    def test_explicit_outgoing_flag_is_filtered_without_media_lookup(self):
        received = []
        listener = self._listener(["项目群"], callback=received.append)

        with patch.object(listener, "_media_for_message") as media_lookup:
            listener._dispatch_sse_frame(
                "message.new",
                [json.dumps(_payload(rawid="outgoing-flag", isSend=1))],
            )

        self.assertEqual(received, [])
        media_lookup.assert_not_called()

    def test_matches_private_display_name_and_session_id(self):
        received = []
        listener = self._listener(
            [
                {"name": "张三", "type": "private"},
                {"name": "other@chatroom", "type": "group"},
            ],
            callback=received.append,
        )
        private = _payload(
            sessionId="wxid_zhangsan",
            sessionType="other",
            rawid="private-1",
            sourceName="张三",
        )
        private.pop("groupName")
        group = _payload(
            sessionId="other@chatroom",
            groupName="显示群名",
            rawid="group-1",
        )
        with patch.object(listener, "_media_for_message", return_value=None):
            listener._dispatch_sse_frame("message.new", [json.dumps(private)])
            listener._dispatch_sse_frame("message.new", [json.dumps(group)])

        self.assertEqual([event.chat_type for event in received], ["private", "group"])
        self.assertEqual(received[0].sender, "张三")
        self.assertEqual(received[1].group, "显示群名")

    def test_history_and_media_requests_are_authenticated(self):
        history = {
            "success": True,
            "messages": [{
                "serverId": "123456789",
                "localType": 3,
                "mediaType": "image",
                "mediaFileName": "photo.png",
                "mediaUrl": "/api/v1/media/team@chatroom/images/photo.png",
            }],
        }
        opener = _Opener(
            _Response(json.dumps(history).encode("utf-8")),
            _Response(PNG, content_type="image/png"),
        )
        listener = self._listener(["项目群"], opener=opener)

        with tempfile.TemporaryDirectory() as directory:
            with patch("weflow_listener.IMAGE_SAVE_DIR", directory):
                message = listener._media_for_message("team@chatroom", "123456789")
                path, message_type = listener._download_media(message, "123456789")
                try:
                    self.assertEqual(message_type, "image")
                    with open(path, "rb") as stream:
                        self.assertEqual(stream.read(), PNG)
                finally:
                    os.unlink(path)

        history_request = opener.requests[0][0]
        self.assertIn("talker=team%40chatroom", history_request.full_url)
        self.assertIn("limit=100", history_request.full_url)
        self.assertIn("media=1", history_request.full_url)
        for request, _timeout in opener.requests:
            self.assertEqual(
                request.get_header("Authorization"),
                "Bearer secret-token",
            )

    def test_history_matches_sse_rawid_to_api_server_id(self):
        target = {
            "serverId": "123456789",
            "localType": 47,
            "mediaType": "emoji",
            "mediaUrl": "/api/v1/media/emoji/smile.gif",
        }
        history = {
            "success": True,
            "messages": [
                {"serverId": "other-message", "localType": 3},
                target,
            ],
        }
        opener = _Opener(_Response(json.dumps(history).encode("utf-8")))
        listener = self._listener(["项目群"], opener=opener)

        with self.assertLogs("weflow_listener", level="DEBUG") as logs:
            message = listener._media_for_message(
                "team@chatroom",
                "123456789",
            )

        self.assertEqual(message, target)
        self.assertNotIn("rawid", message)
        log_output = "\n".join(logs.output)
        self.assertIn("rawid=123456789", log_output)
        self.assertIn("serverId", log_output)
        self.assertIn("localType", log_output)

    def test_voice_history_matches_server_id_and_returns_media_url(self):
        voice = {
            "serverId": "voice-server-id",
            "localType": 34,
            "mediaType": "voice",
            "mediaFileName": "voice.wav",
            "mediaUrl": "/api/v1/media/voice/voice.wav",
        }
        history = {"success": True, "messages": [voice]}
        opener = _Opener(_Response(json.dumps(history).encode("utf-8")))
        listener = self._listener(["项目群"], opener=opener)

        message = listener._voice_for_message(
            "team@chatroom",
            "voice-server-id",
        )

        self.assertEqual(message, voice)
        self.assertEqual(message["mediaUrl"], "/api/v1/media/voice/voice.wav")
        self.assertNotIn("rawid", message)
        self.assertEqual(len(opener.requests), 1)
        self.assertIn("media=1", opener.requests[0][0].full_url)

    def test_history_media_type_promotes_unrecognized_text_to_emoji(self):
        history = {
            "success": True,
            "messages": [{
                "serverId": "sticker-1",
                "localType": 47,
                "content": "[微笑]",
                "mediaType": "sticker",
                "mediaFileName": "smile.gif",
                "mediaUrl": "/api/v1/media/stickers/smile.gif",
            }],
        }
        opener = _Opener(
            _Response(json.dumps(history).encode("utf-8")),
            _Response(GIF, content_type="image/gif"),
        )
        received = []
        listener = self._listener(["项目群"], callback=received.append, opener=opener)

        with tempfile.TemporaryDirectory() as directory:
            with patch("weflow_listener.IMAGE_SAVE_DIR", directory):
                listener._dispatch_sse_frame(
                    "message.new",
                    [json.dumps(_payload(rawid="sticker-1", content="[微笑]"))],
                )
                self.assertEqual(received[0].message_type, "emoji")
                self.assertTrue(received[0].content.endswith(".gif"))
                with open(received[0].content, "rb") as stream:
                    self.assertEqual(stream.read(), GIF)

    def test_empty_emoji_content_retries_until_history_is_synchronized(self):
        missing = {
            "success": True,
            "messages": [{
                "serverId": "late-sticker",
                "localType": 47,
                "mediaType": "animated_emoji",
            }],
        }
        synchronized = {
            "success": True,
            "messages": [{
                "serverId": "late-sticker",
                "localType": 47,
                "mediaType": "animated_emoji",
                "mediaUrl": "/api/v1/media/emoji/late.gif",
            }],
        }
        opener = _Opener(
            _Response(json.dumps(missing).encode("utf-8")),
            _Response(json.dumps(synchronized).encode("utf-8")),
            _Response(GIF, content_type="image/gif"),
        )
        received = []
        listener = self._listener(["项目群"], callback=received.append, opener=opener)

        with tempfile.TemporaryDirectory() as directory:
            with (
                patch("weflow_listener.IMAGE_SAVE_DIR", directory),
                patch.object(listener.stop_event, "wait", return_value=False) as wait,
            ):
                listener._dispatch_sse_frame(
                    "message.new",
                    [json.dumps(_payload(rawid="late-sticker", content=""))],
                )
                self.assertEqual(received[0].message_type, "emoji")
                wait.assert_called_once()

        self.assertEqual(len(opener.requests), 3)
        for request, _timeout in opener.requests[:2]:
            self.assertIn("media=1", request.full_url)

    def test_sse_media_type_and_url_identify_emoji_without_history_lookup(self):
        opener = _Opener(_Response(GIF, content_type="image/gif"))
        received = []
        listener = self._listener(["项目群"], callback=received.append, opener=opener)

        with tempfile.TemporaryDirectory() as directory:
            with patch("weflow_listener.IMAGE_SAVE_DIR", directory):
                listener._dispatch_sse_frame(
                    "message.new",
                    [json.dumps(_payload(
                        rawid="direct-sticker",
                        content="smile",
                        mediaType="emoji",
                        mediaUrl="/api/v1/media/emoji/direct.gif",
                    ))],
                )
                self.assertEqual(received[0].message_type, "emoji")
                with open(received[0].content, "rb") as stream:
                    self.assertEqual(stream.read(), GIF)

        self.assertEqual(len(opener.requests), 1)

    def test_sse_audio_media_type_identifies_voice_without_marker_or_history(self):
        opener = _Opener(
            _Response(b"direct-audio", content_type="application/octet-stream")
        )
        received = []
        listener = self._listener(["项目群"], callback=received.append, opener=opener)

        with tempfile.TemporaryDirectory() as directory:
            with patch("weflow_listener.IMAGE_SAVE_DIR", directory):
                listener._dispatch_sse_frame(
                    "message.new",
                    [json.dumps(_payload(
                        rawid="direct-audio",
                        content="WeFlow audio payload",
                        mediaType="audio",
                        mediaUrl="/api/v1/media/audio/direct.silk",
                    ))],
                )
                self.assertEqual(received[0].message_type, "voice")
                with open(received[0].content, "rb") as stream:
                    self.assertEqual(stream.read(), b"direct-audio")

        self.assertEqual(len(opener.requests), 1)

    def test_history_media_type_promotes_unrecognized_text_to_voice(self):
        history = {
            "success": True,
            "messages": [{
                "serverId": "history-audio",
                "localType": 34,
                "content": "unknown media placeholder",
                "mediaType": "audio",
                "mediaFileName": "history.silk",
                "mediaUrl": "/api/v1/media/audio/history.silk",
            }],
        }
        opener = _Opener(
            _Response(json.dumps(history).encode("utf-8")),
            _Response(b"history-audio", content_type="application/octet-stream"),
        )
        received = []
        listener = self._listener(["项目群"], callback=received.append, opener=opener)

        with tempfile.TemporaryDirectory() as directory:
            with patch("weflow_listener.IMAGE_SAVE_DIR", directory):
                listener._dispatch_sse_frame(
                    "message.new",
                    [json.dumps(_payload(
                        rawid="history-audio",
                        content="unknown media placeholder",
                    ))],
                )
                self.assertEqual(received[0].message_type, "voice")
                with open(received[0].content, "rb") as stream:
                    self.assertEqual(stream.read(), b"history-audio")

        self.assertIn("media=1", opener.requests[0][0].full_url)

    def test_history_voice_type_without_url_uses_voice_retry(self):
        missing = {
            "success": True,
            "messages": [{
                "serverId": "late-history-audio",
                "localType": 34,
                "mediaType": "audio",
            }],
        }
        synchronized = {
            "success": True,
            "messages": [{
                "serverId": "late-history-audio",
                "localType": 34,
                "mediaType": "audio",
                "mediaFileName": "late-history.silk",
                "mediaUrl": "/api/v1/media/audio/late-history.silk",
            }],
        }
        opener = _Opener(
            _Response(json.dumps(missing).encode("utf-8")),
            _Response(json.dumps(missing).encode("utf-8")),
            _Response(json.dumps(synchronized).encode("utf-8")),
            _Response(b"late-history-audio", content_type="application/octet-stream"),
        )
        received = []
        listener = self._listener(["项目群"], callback=received.append, opener=opener)

        with tempfile.TemporaryDirectory() as directory:
            with (
                patch("weflow_listener.IMAGE_SAVE_DIR", directory),
                patch.object(listener.stop_event, "wait", return_value=False) as wait,
            ):
                listener._dispatch_sse_frame(
                    "message.new",
                    [json.dumps(_payload(
                        rawid="late-history-audio",
                        content="unknown delayed media placeholder",
                    ))],
                )
                self.assertEqual(received[0].message_type, "voice")
                wait.assert_called_once()

        self.assertEqual(len(opener.requests), 4)
        self.assertIn("media=1", opener.requests[0][0].full_url)
        self.assertIn("media=1", opener.requests[1][0].full_url)

    def test_empty_voice_content_retries_until_history_is_synchronized(self):
        missing = {
            "success": True,
            "messages": [{
                "serverId": "late-voice",
                "localType": 34,
                "mediaType": "voice",
            }],
        }
        synchronized = {
            "success": True,
            "messages": [{
                "serverId": "late-voice",
                "localType": 34,
                "mediaType": "voice",
                "mediaFileName": "late.silk",
                "mediaUrl": "/api/v1/media/voice/late.silk",
            }],
        }
        opener = _Opener(
            _Response(json.dumps(missing).encode("utf-8")),
            _Response(json.dumps(synchronized).encode("utf-8")),
            _Response(b"late-voice", content_type="application/octet-stream"),
        )
        received = []
        listener = self._listener(["项目群"], callback=received.append, opener=opener)

        with tempfile.TemporaryDirectory() as directory:
            with (
                patch("weflow_listener.IMAGE_SAVE_DIR", directory),
                patch.object(listener.stop_event, "wait", return_value=False) as wait,
            ):
                listener._dispatch_sse_frame(
                    "message.new",
                    [json.dumps(_payload(rawid="late-voice", content=""))],
                )
                self.assertEqual(received[0].message_type, "voice")
                with open(received[0].content, "rb") as stream:
                    self.assertEqual(stream.read(), b"late-voice")
                wait.assert_called_once()

        self.assertEqual(len(opener.requests), 3)
        for request, _timeout in opener.requests[:2]:
            self.assertIn("media=1", request.full_url)

    def test_voice_marker_exports_matching_server_id_with_media_parameter(self):
        history = {
            "success": True,
            "messages": [{
                "serverId": "voice-1",
                "localType": 34,
                "content": "[语音]",
                "mediaType": "voice",
                "mediaFileName": "voice.silk",
                "mediaUrl": "/api/v1/media/voice.silk",
            }],
        }
        opener = _Opener(
            _Response(json.dumps(history).encode("utf-8")),
            _Response(b"voice-bytes", content_type="application/octet-stream"),
        )
        received = []
        listener = self._listener(["项目群"], callback=received.append, opener=opener)

        with tempfile.TemporaryDirectory() as directory:
            with patch("weflow_listener.IMAGE_SAVE_DIR", directory):
                listener._dispatch_sse_frame(
                    "message.new",
                    [json.dumps(_payload(rawid="voice-1", content="[语音]"))],
                )
                self.assertEqual(received[0].message_type, "voice")
                with open(received[0].content, "rb") as stream:
                    self.assertEqual(stream.read(), b"voice-bytes")

        history_request = opener.requests[0][0]
        self.assertIn("talker=team%40chatroom", history_request.full_url)
        self.assertIn("limit=100", history_request.full_url)
        self.assertIn("media=1", history_request.full_url)
        for request, _timeout in opener.requests:
            self.assertEqual(
                request.get_header("Authorization"),
                "Bearer secret-token",
            )

    def test_unavailable_voice_export_silently_preserves_message(self):
        history = {"success": True, "messages": []}
        opener = _Opener(_Response(json.dumps(history).encode("utf-8")))
        received = []
        listener = self._listener(["项目群"], callback=received.append, opener=opener)

        listener._dispatch_sse_frame(
            "message.new",
            [json.dumps(_payload(rawid="voice-missing", content="[语音]"))],
        )

        self.assertEqual(len(received), 1)
        self.assertEqual(received[0].message_type, "text")
        self.assertEqual(received[0].content, "[语音]")


if __name__ == "__main__":
    unittest.main()
