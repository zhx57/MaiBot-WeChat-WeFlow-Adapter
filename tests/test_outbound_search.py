import unittest
from types import SimpleNamespace
from unittest.mock import patch

import config_webui
import main as wemai_main
import wx_Listener
import wx_Processor


def _item(name, group):
    return wx_Listener._SearchUIItem(name=name, ctrl=object(), group=group)


class _FakeControl(SimpleNamespace):
    def Exists(self, **_kwargs):
        return True


class _FakeResultList:
    def __init__(self):
        self.wheels = 0

    def Exists(self, **_kwargs):
        return True

    def WheelDown(self, waitTime=0):
        assert waitTime == 0
        self.wheels += 1

    def GetChildren(self):
        header = _FakeControl(
            Name=wx_Listener._SEARCH_GROUP_FREQUENT,
            ClassName="mmui::XTableCell",
            AutomationId="",
            ControlTypeName="TextControl",
        )
        row = _FakeControl(
            Name="元宝原宝",
            ClassName="SearchContentCellView",
            AutomationId="search_item_contact_0",
            ControlTypeName="ListItemControl",
        )
        return [header, row]


class OutboundSearchTests(unittest.TestCase):
    def test_chat_history_section_is_not_a_duplicate_group_result(self):
        controls = [
            _FakeControl(
                Name=wx_Listener._SEARCH_GROUP_FREQUENT,
                ClassName="mmui::XTableCell",
                AutomationId="",
                ControlTypeName="TextControl",
            ),
            _FakeControl(
                Name="测试群",
                ClassName="SearchContentCellView",
                AutomationId="search_item_测试群",
                ControlTypeName="ListItemControl",
            ),
            _FakeControl(
                Name=wx_Listener._SEARCH_GROUP_HISTORY,
                ClassName="SearchContentCellView",
                AutomationId="",
                ControlTypeName="ListItemControl",
            ),
            _FakeControl(
                Name="测试群",
                ClassName="SearchContentCellView",
                AutomationId="",
                ControlTypeName="ListItemControl",
            ),
        ]

        results = wx_Listener._parse_search_result_controls(controls)
        actual = wx_Listener.WeChatListener._find_exact_search_result(
            results,
            "测试群",
            "group",
            allow_contains=False,
        )

        self.assertIsNotNone(actual)
        self.assertEqual(actual.name, "测试群")
        self.assertNotIn(wx_Listener._SEARCH_GROUP_HISTORY, results)

    def test_private_chat_history_section_is_also_ignored(self):
        controls = [
            _FakeControl(
                Name=wx_Listener._SEARCH_GROUP_CONTACTS,
                ClassName="mmui::XTableCell",
                AutomationId="",
                ControlTypeName="TextControl",
            ),
            _FakeControl(
                Name="元宝原宝",
                ClassName="SearchContentCellView",
                AutomationId="search_item_元宝原宝",
                ControlTypeName="ListItemControl",
            ),
            _FakeControl(
                Name=wx_Listener._SEARCH_GROUP_HISTORY,
                ClassName="SearchContentCellView",
                AutomationId="",
                ControlTypeName="ListItemControl",
            ),
            _FakeControl(
                Name="元宝原宝",
                ClassName="SearchContentCellView",
                AutomationId="",
                ControlTypeName="ListItemControl",
            ),
        ]

        results = wx_Listener._parse_search_result_controls(controls)
        actual = wx_Listener.WeChatListener._find_exact_search_result(
            results,
            "元宝原宝",
            "contact",
            allow_contains=False,
        )

        self.assertIsNotNone(actual)
        self.assertEqual(actual.name, "元宝原宝")

    def test_two_real_same_name_groups_remain_ambiguous(self):
        results = {
            wx_Listener._SEARCH_GROUP_CHATS: [
                _item("测试群", wx_Listener._SEARCH_GROUP_CHATS),
                _item("测试群", wx_Listener._SEARCH_GROUP_CHATS),
            ]
        }

        actual = wx_Listener.WeChatListener._find_exact_search_result(
            results,
            "测试群",
            "group",
            allow_contains=False,
        )

        self.assertIsNone(actual)

    def test_private_chat_accepts_unique_frequent_exact_match(self):
        expected = _item("元宝原宝", wx_Listener._SEARCH_GROUP_FREQUENT)
        results = {wx_Listener._SEARCH_GROUP_FREQUENT: [expected]}

        actual = wx_Listener.WeChatListener._find_exact_search_result(
            results,
            "元宝原宝",
            "contact",
            allow_contains=False,
        )

        self.assertIs(actual, expected)

    def test_group_chat_accepts_unique_frequent_exact_match(self):
        expected = _item("测试群", wx_Listener._SEARCH_GROUP_FREQUENT)
        results = {wx_Listener._SEARCH_GROUP_FREQUENT: [expected]}

        actual = wx_Listener.WeChatListener._find_exact_search_result(
            results,
            "测试群",
            "group",
            allow_contains=False,
        )

        self.assertIs(actual, expected)

    def test_typed_group_wins_before_frequent_result(self):
        contact = _item("元宝原宝", wx_Listener._SEARCH_GROUP_CONTACTS)
        frequent = _item("元宝原宝", wx_Listener._SEARCH_GROUP_FREQUENT)
        results = {
            wx_Listener._SEARCH_GROUP_FREQUENT: [frequent],
            wx_Listener._SEARCH_GROUP_CONTACTS: [contact],
        }

        actual = wx_Listener.WeChatListener._find_exact_search_result(
            results,
            "元宝原宝",
            "contact",
            allow_contains=False,
        )

        self.assertIs(actual, contact)

    def test_scroll_reveals_virtualized_exact_result(self):
        result_list = _FakeResultList()

        def accepts(results):
            return (
                wx_Listener.WeChatListener._find_exact_search_result(
                    results,
                    "元宝原宝",
                    "contact",
                    allow_contains=False,
                )
                is not None
            )

        with patch.object(
            wx_Listener,
            "_find_search_result_list",
            return_value=result_list,
        ):
            results = wx_Listener._scroll_search_results_for_match(
                object(),
                "元宝原宝",
                result_list,
                accepts,
                duration=0.01,
            )

        self.assertTrue(accepts(results))
        self.assertEqual(result_list.wheels, 1)

    def test_search_failure_cooldown_is_twenty_seconds(self):
        self.assertEqual(wx_Listener._SEARCH_FAIL_COOLDOWN_SECONDS, 20.0)

    def test_session_open_uses_physical_double_click_first(self):
        class Control:
            def __init__(self):
                self.uia_calls = 0

            def GetParentControl(self):
                return object()

            def DoubleClick(self, **_kwargs):
                self.uia_calls += 1

        control = Control()
        with (
            patch.object(wx_Listener, "_roll_control_into_view"),
            patch.object(
                wx_Listener,
                "_physical_double_click_control",
                return_value=True,
            ) as physical,
        ):
            result = wx_Listener._double_click_control(control)

        self.assertTrue(result)
        physical.assert_called_once_with(control)
        self.assertEqual(control.uia_calls, 0)

    def test_session_open_falls_back_to_uia_double_click(self):
        class Control:
            def __init__(self):
                self.uia_calls = 0

            def GetParentControl(self):
                return object()

            def DoubleClick(self, **_kwargs):
                self.uia_calls += 1

        control = Control()
        with (
            patch.object(wx_Listener, "_roll_control_into_view"),
            patch.object(
                wx_Listener,
                "_physical_double_click_control",
                return_value=False,
            ),
        ):
            result = wx_Listener._double_click_control(control)

        self.assertTrue(result)
        self.assertEqual(control.uia_calls, 1)

    def test_window_open_error_does_not_mean_chat_not_found(self):
        self.assertFalse(
            issubclass(
                wx_Listener._SubwindowOpenError,
                wx_Listener.ChatNotFoundError,
            )
        )

    def test_weflow_mode_skips_unused_uia_message_baseline(self):
        with patch.object(
            wx_Listener,
            "_read_visible_items",
            side_effect=AssertionError("WeFlow 模式不应读取 UIA 消息快照"),
        ):
            baseline = wx_Listener._initial_session_baseline(
                object(),
                callback=None,
            )

        self.assertEqual(baseline, [])

    def test_uia_inbound_mode_keeps_message_baseline(self):
        with patch.object(
            wx_Listener,
            "_read_visible_items",
            return_value=["visible-message"],
        ) as reader:
            baseline = wx_Listener._initial_session_baseline(
                "message-list",
                callback=lambda _event: None,
            )

        self.assertEqual(baseline, ["visible-message"])
        reader.assert_called_once_with("message-list")

    def test_router_disables_websocket_transport_compression(self):
        self.assertIs(
            wx_Processor._maim_api.WebSocketClient,
            wx_Processor._UncompressedMaimWebSocketClient,
        )

    def test_host_log_mode_falls_back_to_standard_format(self):
        with patch.object(wemai_main, "LOG_FORMAT", "json"):
            formatter = wemai_main._log_formatter()

        self.assertEqual(formatter._fmt, wemai_main.DEFAULT_LOG_FORMAT)

    def test_core_webui_has_responsive_control_and_log_pages(self):
        self.assertIn('<nav class="sidebar"', config_webui.HTML)
        self.assertIn('id="page-config"', config_webui.HTML)
        self.assertIn('id="page-logs"', config_webui.HTML)
        self.assertNotIn("OPTIONAL_CONTACTS_ENABLED", config_webui.HTML)
        self.assertNotIn("OPTIONAL_GROUPS_ENABLED", config_webui.HTML)
        self.assertNotIn("position:sticky;bottom:0", config_webui.HTML)
        self.assertIn("@media(max-width:780px)", config_webui.HTML)


if __name__ == "__main__":
    unittest.main()
