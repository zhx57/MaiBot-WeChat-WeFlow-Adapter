import json
import threading
import unittest
from collections import deque

from weflow_listener import WeFlowListener


NESTED_RECORD = """
<recordinfo>
  <title>嵌套记录</title>
  <datalist count="2">
    <dataitem datatype="1" dataid="nested-text">
      <datadesc>嵌套文本</datadesc>
      <sourcename>张三</sourcename>
      <sourcetime>2026-07-30 10:00:00</sourcetime>
    </dataitem>
    <dataitem datatype="8" dataid="nested-file">
      <datatitle>资料.zip</datatitle>
      <datafmt>zip</datafmt>
      <datasize>2048</datasize>
      <sourcename>李四</sourcename>
    </dataitem>
  </datalist>
</recordinfo>
""".strip()

OUTER_RECORD = f"""
<recordinfo>
  <title>测试聊天记录</title>
  <datalist count="5">
    <dataitem datatype="1" dataid="voice">
      <datadesc>[语音] 55&quot;</datadesc>
      <sourcename>菲比</sourcename>
      <sourcetime>2026-07-30 09:00:00</sourcetime>
    </dataitem>
    <dataitem datatype="2" dataid="image">
      <datadesc>[图片]</datadesc>
      <datasize>2359571</datasize>
      <sourcename>元宝原宝</sourcename>
    </dataitem>
    <dataitem datatype="17" dataid="nested">
      <datatitle>嵌套记录</datatitle>
      <sourcename>元宝原宝</sourcename>
      <recordxml>{NESTED_RECORD}</recordxml>
    </dataitem>
    <dataitem datatype="22" dataid="finder">
      <datatitle>视频标题</datatitle>
      <datadesc>视频描述</datadesc>
      <sourcename>王五</sourcename>
      <finderFeed><id>finder-1</id></finderFeed>
    </dataitem>
    <dataitem datatype="5" dataid="link">
      <datatitle>链接标题</datatitle>
      <datadesc>链接描述</datadesc>
      <streamweburl>https://example.com/path?a=1&amp;b=2</streamweburl>
      <sourcename>赵六</sourcename>
    </dataitem>
  </datalist>
</recordinfo>
""".strip()

APPMSG = f"""
<msg><appmsg><title>测试聊天记录</title><type>19</type>
<recorditem><![CDATA[{OUTER_RECORD}]]></recorditem></appmsg></msg>
""".strip()


class ChatRecordInboundTest(unittest.TestCase):
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
        listener.stop_event = threading.Event()
        return listener

    def test_mixed_and_nested_record_is_rendered_readably(self):
        rendered = WeFlowListener._render_chat_record({"rawContent": APPMSG})

        self.assertIn("[合并聊天记录] 测试聊天记录（5条）", rendered)
        self.assertIn("[语音 55秒，原始音频不可提取]", rendered)
        self.assertIn("[图片，2.25MB]", rendered)
        self.assertIn("[嵌套合并聊天记录] 嵌套记录（2条）", rendered)
        self.assertIn("3.1. 张三（2026-07-30 10:00:00）：嵌套文本", rendered)
        self.assertIn("3.2. 李四：[文件：资料.zip，2.0KB]", rendered)
        self.assertIn("4. 王五：[视频号：视频标题] 视频描述", rendered)
        self.assertIn(
            "5. 赵六：[链接：链接标题] 链接描述 "
            "https://example.com/path?a=1&b=2",
            rendered,
        )

    def test_history_lookup_uses_metadata_mode_and_replaces_placeholder(self):
        events = []
        listener = self._listener(events.append)
        calls = []

        def history(*args, **kwargs):
            calls.append((args, kwargs))
            return {
                "serverId": "record-1",
                "senderUsername": "wxid_sender",
                "content": "[聊天记录]",
                "rawContent": APPMSG,
            }

        listener._media_for_message = history
        payload = {
            "event": "message.new",
            "rawid": "record-1",
            "sessionId": "wxid_sender",
            "sessionType": "other",
            "sourceName": "元宝原宝",
            "content": "[聊天记录]",
            "timestamp": 1785399400,
        }

        listener._dispatch_sse_frame("message.new", [json.dumps(payload)])

        self.assertEqual(len(events), 1)
        self.assertTrue(events[0].content.startswith("[合并聊天记录]"))
        self.assertTrue(events[0].raw["weflowChatRecord"])
        self.assertEqual(len(calls), 1)
        self.assertFalse(calls[0][1]["include_media"])
        self.assertFalse(calls[0][1]["strict"])
        self.assertTrue(calls[0][1]["bypass_cache"])
        self.assertEqual(calls[0][1]["attempts"], 3)

    def test_chat_record_bypasses_stale_group_history_cache(self):
        listener = self._listener(None)
        listener._history_cache_store(
            "group@chatroom",
            [{"serverId": "record-1", "rawContent": "[聊天记录]"}],
            False,
        )
        requests = []

        def request_json(_path, query=None):
            requests.append(query)
            return {
                "success": True,
                "messages": [
                    {"serverId": "record-1", "rawContent": APPMSG}
                ],
            }

        listener._request_json = request_json
        row = listener._media_for_message(
            "group@chatroom",
            "record-1",
            attempts=1,
            strict=False,
            include_media=False,
            bypass_cache=True,
        )

        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0]["limit"], 10)
        self.assertIn("<recorditem>", row["rawContent"])

    def test_group_sender_prefix_and_nested_link_fields_are_supported(self):
        record = """
<recordinfo><title>群聊的聊天记录</title><datalist count="2">
  <dataitem datatype="23">
    <datatitle>直播标题</datatitle><datadesc>直播描述</datadesc>
    <sourcename>成员甲</sourcename><finderLive><id>1</id></finderLive>
  </dataitem>
  <dataitem datatype="5">
    <link>https://example.com/article?a=1&amp;b=2</link>
    <sourcename>成员乙</sourcename>
    <weburlitem><title>文章标题</title><desc>文章描述</desc></weburlitem>
  </dataitem>
</datalist></recordinfo>
""".strip()
        outer = (
            "custom_real_id:\n"
            "<?xml version=\"1.0\"?><msg><appmsg><type>19</type>"
            f"<recorditem><![CDATA[{record}]]></recorditem>"
            "</appmsg></msg>"
        )

        rendered = WeFlowListener._render_chat_record({"rawContent": outer})

        self.assertIn("[合并聊天记录] 群聊的聊天记录（2条）", rendered)
        self.assertIn("[视频号直播：直播标题] 直播描述", rendered)
        self.assertIn(
            "[链接：文章标题] 文章描述 https://example.com/article?a=1&b=2",
            rendered,
        )

    def test_malformed_record_falls_back_without_raising(self):
        self.assertIsNone(
            WeFlowListener._render_chat_record({"rawContent": "<msg><broken>"})
        )

    def test_non_record_appmsg_is_not_rendered(self):
        self.assertIsNone(
            WeFlowListener._render_chat_record(
                {"rawContent": "<msg><appmsg><type>5</type></appmsg></msg>"}
            )
        )


if __name__ == "__main__":
    unittest.main()
