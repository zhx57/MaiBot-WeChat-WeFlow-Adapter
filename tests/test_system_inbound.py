import json
import sys
import threading
import time
from collections import deque
from pathlib import Path
from unittest import TestCase


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from weflow_listener import WeFlowListener  # noqa: E402
from wx_Processor import (  # noqa: E402
    _WEFLOW_SYSTEM_SENDER_ID,
    _WEFLOW_SYSTEM_SENDER_NAME,
    _WEFLOW_SYSTEM_UPSTREAM_IDS,
)


PAT_XML = (
    '<msg><appmsg><title>"王森小米弟" 拍了拍 "菲比"</title>'
    '<type>62</type><patinfo>'
    '<fromusername>wxid_ev4txoa8f2mm22</fromusername>'
    '<chatusername>58327292769@chatroom</chatusername>'
    '<pattedusername>wxid_vt620cuy5hio22</pattedusername>'
    '</patinfo></appmsg></msg>'
)


class SystemInboundTest(TestCase):
    @staticmethod
    def _listener(callback):
        listener = WeFlowListener.__new__(WeFlowListener)
        listener._seen = set()
        listener._seen_order = deque()
        listener._targets = []
        listener.outgoing_registry = None
        listener._recency_recorder = None
        listener.callback = callback
        listener.stop_event = threading.Event()
        listener._group_members_cache = {}
        listener._group_members_cache_lock = threading.RLock()
        listener._group_members_refreshing = set()
        listener._metadata_executor = None
        return listener

    def test_pat_identity_parser_recovers_actor(self):
        self.assertEqual(
            WeFlowListener._pat_identity({"rawContent": PAT_XML}),
            ("wxid_ev4txoa8f2mm22", "王森小米弟"),
        )

    def test_pat_event_uses_real_actor_identity(self):
        events = []
        listener = self._listener(events.append)
        history_row = {
            "serverId": "8753182590806660213",
            "localType": 266287972401,
            "senderUsername": None,
            "content": PAT_XML,
            "rawContent": PAT_XML,
        }
        listener._media_for_message = lambda *args, **kwargs: history_row
        listener._group_member_metadata = lambda *args, **kwargs: {}
        payload = {
            "event": "message.new",
            "rawid": "8753182590806660213",
            "sessionId": "58327292769@chatroom",
            "sessionType": "group",
            "groupName": "天天爆单🌈",
            "sourceName": "未知发送者",
            "avatarUrl": "https://example.invalid/system-avatar.png",
            "content": '"王森小米弟" 拍了拍 "菲比"',
            "timestamp": 1785303943,
        }

        listener._dispatch_sse_frame("message.new", [json.dumps(payload)])

        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event.sender_wxid, "wxid_ev4txoa8f2mm22")
        self.assertEqual(event.sender, "王森小米弟")
        self.assertEqual(event.group_card_name, "王森小米弟")
        self.assertEqual(event.avatar_url, "")
        self.assertEqual(event.raw["weflowSystemEvent"], "pat")
        self.assertEqual(event.raw["systemActorWxid"], "wxid_ev4txoa8f2mm22")

    def test_group_member_metadata_supplies_distinct_name_and_avatar(self):
        events = []
        listener = self._listener(events.append)
        listener._media_for_message = lambda *args, **kwargs: {
            "serverId": "member-1",
            "senderUsername": "wxid_member_1",
        }
        listener._group_member_metadata = lambda *args, **kwargs: {
            "nickname": "微信昵称",
            "groupNickname": "群昵称",
            "displayName": "群昵称",
            "avatarUrl": "https://wx.qlogo.cn/member-1.jpg",
        }
        payload = {
            "event": "message.new",
            "rawid": "member-1",
            "sessionId": "room-1@chatroom",
            "sessionType": "group",
            "groupName": "测试群",
            "sourceName": "旧名称",
            "avatarUrl": "https://example.invalid/wrong-group-avatar.jpg",
            "content": "你好",
            "timestamp": 1785303943,
        }

        listener._dispatch_sse_frame("message.new", [json.dumps(payload)])

        event = events[0]
        self.assertEqual(event.sender_wxid, "wxid_member_1")
        self.assertEqual(event.sender, "群昵称")
        self.assertEqual(event.user_nickname, "微信昵称")
        self.assertEqual(event.group_card_name, "群昵称")
        self.assertEqual(event.avatar_url, "https://wx.qlogo.cn/member-1.jpg")
        self.assertEqual(
            event.group_avatar_url,
            "https://example.invalid/wrong-group-avatar.jpg",
        )

    def test_group_member_name_resolves_arbitrary_real_id_format(self):
        listener = self._listener(lambda _event: None)
        listener._group_members_cache = {
            "room@chatroom": (
                time.monotonic() + 60,
                {
                    "custom-early-wechat-id": {
                        "displayName": "瘦瘦的胖子",
                        "nickname": "瘦瘦的胖子",
                        "remark": "",
                        "groupNickname": "",
                        "avatarUrl": "https://example.invalid/member.jpg",
                    }
                },
            )
        }

        member_id, member = listener._group_member_identity(
            "room@chatroom", "瘦瘦的胖子"
        )

        self.assertEqual(member_id, "custom-early-wechat-id")
        self.assertEqual(member["nickname"], "瘦瘦的胖子")

    def test_reported_member_message_is_not_reclassified_as_system(self):
        events = []
        listener = self._listener(events.append)
        listener._group_members_cache = {
            "51716150326@chatroom": (
                time.monotonic() + 60,
                {
                    "wxid_mnsje0vtknfg21": {
                        "displayName": "瘦瘦的胖子",
                        "nickname": "瘦瘦的胖子",
                        "remark": "",
                        "groupNickname": "",
                        "avatarUrl": "https://example.invalid/member.jpg",
                    }
                },
            )
        }
        history_calls = []

        def history(*args, **kwargs):
            history_calls.append((args, kwargs))
            return None

        listener._media_for_message = history
        payload = {
            "event": "message.new",
            "rawid": "1153646331179562288",
            "sessionId": "51716150326@chatroom",
            "sessionType": "group",
            "sourceName": "瘦瘦的胖子",
            "groupName": "一起学AI👾(禁广)",
            "avatarUrl": "https://example.invalid/group.jpg",
            "content": "才一天",
            "timestamp": 1785326940,
        }

        listener._dispatch_sse_frame("message.new", [json.dumps(payload)])

        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event.sender_wxid, "wxid_mnsje0vtknfg21")
        self.assertEqual(event.sender, "瘦瘦的胖子")
        self.assertNotIn("weflowSystemEvent", event.raw)
        self.assertEqual(event.group_avatar_url, "https://example.invalid/group.jpg")
        # 引用/身份共享一次 10→100 历史查询，不再在未命中后重复查询。
        self.assertEqual(len(history_calls), 1)

    def test_revoke_is_deduplicated_separately_and_not_delivered(self):
        events = []
        listener = self._listener(events.append)
        payload = {
            "event": "message.revoke",
            "rawid": "same-server-id",
            "sessionId": "room@chatroom",
            "sessionType": "group",
            "sourceName": "某成员",
            "groupName": "测试群",
            "content": "某成员撤回了一条消息",
            "timestamp": 1785303943,
        }

        listener._dispatch_sse_frame("message.revoke", [json.dumps(payload)])

        self.assertEqual(events, [])
        self.assertIn(("message.revoke", "same-server-id"), listener._seen)

    def test_unrelated_system_xml_does_not_forge_actor(self):
        row = {"content": "<sysmsg><text>修改群名</text></sysmsg>"}
        self.assertEqual(WeFlowListener._pat_identity(row), ("", ""))

    def test_generic_system_identity_is_named_and_separate_from_old_cache(self):
        self.assertEqual(_WEFLOW_SYSTEM_SENDER_NAME, "微信系统消息")
        self.assertNotEqual(_WEFLOW_SYSTEM_SENDER_ID, "weixin")
        self.assertIn("weixin", _WEFLOW_SYSTEM_UPSTREAM_IDS)

    def test_generic_system_message_has_no_content_prefix(self):
        from wx_Processor import MessageProcessor

        processor = MessageProcessor.__new__(MessageProcessor)
        processor.platform = "wx"
        processor._remember = lambda *args, **kwargs: None
        processor._schedule_avatar_cache = lambda *args, **kwargs: None
        message = processor._build_message(
            "测试群",
            {
                "chat_type": "group",
                "chat_id": "room@chatroom",
                "sender": "未知发送者",
                "weflow_system_event": "generic",
                "content": '"元宝原宝"修改群名为“测试群”',
                "type": "text",
                "timestamp": 1785300862,
            },
        )

        self.assertEqual(
            message.message_segment.data[0].data,
            '"元宝原宝"修改群名为“测试群”',
        )
        self.assertEqual(
            message.message_info.user_info.user_nickname,
            _WEFLOW_SYSTEM_SENDER_NAME,
        )

    def test_missing_group_sender_id_is_member_fallback_not_system(self):
        from wx_Processor import MessageProcessor

        processor = MessageProcessor.__new__(MessageProcessor)
        processor.platform = "wx"
        processor._remember = lambda *args, **kwargs: None
        processor._schedule_avatar_cache = lambda *args, **kwargs: None
        message = processor._build_message(
            "一起学AI👾(禁广)",
            {
                "chat_type": "group",
                "chat_id": "51716150326@chatroom",
                "sender": "瘦瘦的胖子",
                "user_nickname": "瘦瘦的胖子",
                "content": "才一天",
                "type": "text",
                "timestamp": 1785300862,
            },
        )

        self.assertEqual(
            message.message_info.user_info.user_nickname,
            "瘦瘦的胖子",
        )
        self.assertNotEqual(
            message.message_info.user_info.user_id,
            processor._stable_id("user", _WEFLOW_SYSTEM_SENDER_ID),
        )


if __name__ == "__main__":
    import unittest

    unittest.main()
