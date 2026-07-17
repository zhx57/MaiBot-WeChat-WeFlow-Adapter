import unittest
import sys
import types
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, call, patch

from wx_Listener import (
    WeChatListener,
    _find_chat_input,
    _find_message_list,
    _find_search_box,
    _find_search_result_list,
    _find_send_button,
    _find_session_item,
    _find_session_list,
    _find_window_by_title,
    _clear_direct_search,
    _parse_search_result_controls,
    _roll_control_into_view,
    _search_with_wxauto40_controls,
    _send_files_via_input,
    _send_text_via_input,
    _submit_direct_search_with_enter,
    _SendOutcomeUnknown,
)


def _existing(**attrs):
    attrs.setdefault("Exists", Mock(return_value=True))
    return SimpleNamespace(**attrs)


class WxAuto40ControlPathTest(unittest.TestCase):
    def _build_tree(self):
        session_list = _existing(Name="\u4f1a\u8bdd")
        session_container = _existing(
            ListControl=Mock(return_value=session_list)
        )
        search_edit = _existing()
        search_field = _existing(EditControl=Mock(return_value=search_edit))
        panel = _existing()
        panel.GroupControl = Mock(
            side_effect=lambda **criteria: (
                session_container
                if criteria.get("ClassName") == "mmui::ChatSessionList"
                else search_field
            )
        )

        message_list = _existing()
        message_view = _existing(ListControl=Mock(return_value=message_list))
        chat_input = _existing()
        send_button = _existing()
        chat_box = _existing(ClassName="mmui::XSplitterView")
        chat_box.GroupControl = Mock(return_value=message_view)
        chat_box.EditControl = Mock(return_value=chat_input)
        chat_box.ButtonControl = Mock(return_value=send_button)
        chat_page = _existing(CustomControl=Mock(return_value=chat_box))

        result_list = _existing()
        popover = _existing(ListControl=Mock(return_value=result_list))
        root = _existing(ClassName="mmui::MainWindow")
        root.GroupControl = Mock(
            side_effect=lambda **criteria: (
                panel
                if criteria.get("ClassName") == "mmui::ChatMasterView"
                else chat_page
            )
        )
        root.WindowControl = Mock(return_value=popover)
        return root, session_list, search_edit, result_list, message_list, chat_input, send_button

    def test_native_wechat_40_component_hierarchy_is_used(self):
        (
            root,
            session_list,
            search_edit,
            result_list,
            message_list,
            chat_input,
            send_button,
        ) = self._build_tree()

        self.assertIs(_find_session_list(root), session_list)
        self.assertIs(_find_search_box(root), search_edit)
        self.assertIs(_find_search_result_list(root), result_list)
        self.assertIs(_find_message_list(root), message_list)
        self.assertIs(_find_chat_input(root), chat_input)
        self.assertIs(_find_send_button(root), send_button)

        root.GroupControl.assert_any_call(ClassName="mmui::ChatMasterView")
        root.GroupControl.assert_any_call(ClassName="mmui::ChatMessagePage")

    def test_session_rows_can_be_qt_data_items(self):
        target = _existing(
            ControlTypeName="DataItemControl",
            ClassName="mmui::ChatSessionItemView",
            Name="\u9879\u76ee\u7fa4\n\u6628\u5929 10:00\n\u6700\u540e\u4e00\u6761\u6d88\u606f",
            IsSelected=True,
            GetChildren=lambda: [],
        )
        session_list = _existing(GetChildren=lambda: [target])

        with patch("wx_Listener._find_session_list", return_value=session_list):
            self.assertIs(_find_session_item(object(), "\u9879\u76ee\u7fa4"), target)

    def test_scroll_item_pattern_is_used_before_clicking(self):
        pattern = SimpleNamespace(ScrollIntoView=Mock(return_value=True))
        control = SimpleNamespace(GetScrollItemPattern=Mock(return_value=pattern))

        self.assertTrue(_roll_control_into_view(object(), control))
        pattern.ScrollIntoView.assert_called_once_with(waitTime=0)

    def test_search_results_follow_popover_groups(self):
        header = _existing(
            Name="\u7fa4\u804a",
            ClassName="mmui::XTableCell",
            AutomationId="",
            ControlTypeName="DataItemControl",
        )
        result = _existing(
            Name="\u9879\u76ee\u7fa4",
            ClassName="mmui::SearchContentCellView",
            AutomationId="search_item_1",
            ControlTypeName="DataItemControl",
        )

        grouped = _parse_search_result_controls([header, result])

        self.assertEqual(list(grouped), ["\u7fa4\u804a"])
        self.assertEqual(grouped["\u7fa4\u804a"][0].name, "\u9879\u76ee\u7fa4")
        self.assertIs(grouped["\u7fa4\u804a"][0].ctrl, result)

    def test_operational_search_can_require_an_exact_result(self):
        contained = SimpleNamespace(
            name="target notifications",
            ctrl=object(),
            group="\u7fa4\u804a",
        )

        self.assertIsNone(
            WeChatListener._find_exact_search_result(
                {"\u7fa4\u804a": [contained]},
                "target",
                "group",
                allow_contains=False,
            )
        )

    def test_direct_search_pastes_literal_keyword(self):
        search_box = _existing(Click=Mock(), SendKeys=Mock())

        with patch("wx_Listener._find_search_box", return_value=search_box):
            with patch(
                "wx_Listener._find_search_result_list",
                return_value=None,
            ):
                with patch(
                    "wx_Listener._set_clipboard_text",
                    return_value=True,
                ) as clipboard:
                    grouped = _search_with_wxauto40_controls(
                        object(),
                        "target{Enter}",
                        timeout=0.1,
                    )

        clipboard.assert_called_once_with("target{Enter}")
        # Search box path now submits with Enter and returns empty dict.
        self.assertEqual(grouped, {})
        send_keys_calls = [c.args[0] for c in search_box.SendKeys.call_args_list]
        self.assertIn("{Ctrl}a", send_keys_calls)
        self.assertIn("{DELETE}", send_keys_calls)
        self.assertIn("{Ctrl}v", send_keys_calls)
        self.assertIn("{Enter}", send_keys_calls)

    def test_missing_search_box_uses_ctrl_f_and_enter_without_result_tree(self):
        key_calls = []

        with patch("wx_Listener._find_search_box", return_value=None), patch(
            "wx_Listener._find_search_result_list",
            return_value=None,
        ), patch(
            "wx_Listener._activate_native_window",
            return_value=True,
        ) as activate, patch(
            "wx_Listener._send_global_uia_keys",
            side_effect=key_calls.append,
        ), patch(
            "wx_Listener._set_clipboard_text",
            return_value=True,
        ) as clipboard, patch(
            "wx_Listener._sleep_during_search"
        ):
            grouped = _search_with_wxauto40_controls(
                object(),
                "target",
                main_hwnd=100,
            )

        self.assertEqual(grouped, {})
        activate.assert_called_once_with(100)
        clipboard.assert_called_once_with("target")
        self.assertEqual(key_calls, ["{Ctrl}f", "{Ctrl}v", "{Enter}"])

    def test_ctrl_f_fallback_keeps_exposed_result_parsing_path(self):
        result = _existing(
            Name="target",
            ClassName="mmui::SearchContentCellView",
            AutomationId="search_item_1",
            ControlTypeName="DataItemControl",
        )
        result_list = _existing(GetChildren=Mock(return_value=[result]))
        key_calls = []

        with patch("wx_Listener._find_search_box", return_value=None), patch(
            "wx_Listener._find_search_result_list",
            return_value=result_list,
        ), patch(
            "wx_Listener._activate_native_window",
            return_value=True,
        ), patch(
            "wx_Listener._send_global_uia_keys",
            side_effect=key_calls.append,
        ), patch(
            "wx_Listener._set_clipboard_text",
            return_value=True,
        ), patch(
            "wx_Listener._sleep_during_search"
        ):
            grouped = _search_with_wxauto40_controls(
                object(),
                "target",
                main_hwnd=100,
            )

        self.assertEqual(grouped["\u672a\u77e5"][0].name, "target")
        self.assertEqual(key_calls, ["{Ctrl}f", "{Ctrl}v"])
        result_list.GetChildren.assert_called_once_with()

    def test_empty_direct_results_return_to_main_session_matching(self):
        listener = object.__new__(WeChatListener)
        listener.stop_event = None
        listener.wx = SimpleNamespace(
            window=SimpleNamespace(
                hwnd=100,
                uia=SimpleNamespace(root=object()),
            ),
            chat_window=SimpleNamespace(search=Mock()),
        )
        listener._target_type_for = Mock(return_value=None)

        with patch(
            "wx_Listener._search_with_wxauto40_controls",
            return_value={},
        ), patch("wx_Listener._clear_direct_search") as clear:
            actual_name = (
                listener._open_main_chat_from_exact_search_with_foreground(
                    "target"
                )
            )

        self.assertEqual(actual_name, "target")
        clear.assert_called_once_with(listener.wx.window.uia.root)
        listener.wx.chat_window.search.assert_not_called()

    def test_direct_search_returns_as_soon_as_exact_result_appears(self):
        search_box = _existing(Click=Mock(), SendKeys=Mock())

        with patch("wx_Listener._find_search_box", return_value=search_box), patch(
            "wx_Listener._find_search_result_list",
            return_value=None,
        ), patch(
            "wx_Listener._set_clipboard_text",
            return_value=True,
        ), patch.multiple(
            "wx_Listener",
            _SEARCH_INPUT_STEP_DELAY=0,
            _SEARCH_INITIAL_SETTLE_DELAY=0,
            _SEARCH_RESULT_STABLE_TIME=0,
            _UI_POLL_INTERVAL=0,
        ):
            grouped = _search_with_wxauto40_controls(
                object(),
                "target",
                timeout=0.1,
                accept_results=lambda groups: any(
                    item.name == "target"
                    for items in groups.values()
                    for item in items
                ),
            )

        # Search box path now submits with Enter and returns empty dict.
        self.assertEqual(grouped, {})
        send_keys_calls = [c.args[0] for c in search_box.SendKeys.call_args_list]
        self.assertIn("{Enter}", send_keys_calls)

    def test_direct_search_returns_first_frame_before_popover_disappears(self):
        search_box = _existing(Click=Mock(), SendKeys=Mock())

        with patch("wx_Listener._find_search_box", return_value=search_box), patch(
            "wx_Listener._find_search_result_list",
            return_value=None,
        ), patch(
            "wx_Listener._set_clipboard_text",
            return_value=True,
        ), patch.multiple(
            "wx_Listener",
            _SEARCH_INPUT_STEP_DELAY=0,
            _SEARCH_INITIAL_SETTLE_DELAY=0,
            _SEARCH_RESULT_STABLE_TIME=1,
            _UI_POLL_INTERVAL=0.005,
        ):
            grouped = _search_with_wxauto40_controls(
                object(),
                "target",
                timeout=0.015,
            )

        # Search box path now submits with Enter and returns empty dict.
        self.assertEqual(grouped, {})
        send_keys_calls = [c.args[0] for c in search_box.SendKeys.call_args_list]
        self.assertIn("{Enter}", send_keys_calls)

    def test_disappeared_popover_can_submit_confirmed_query_with_enter(self):
        value_pattern = SimpleNamespace(Value="target")
        search_box = _existing(
            SetFocus=Mock(),
            SendKeys=Mock(),
            GetValuePattern=Mock(return_value=value_pattern),
        )

        with patch("wx_Listener._find_search_result_list", return_value=None), patch(
            "wx_Listener._find_search_box",
            return_value=search_box,
        ):
            self.assertTrue(
                _submit_direct_search_with_enter(object(), "target")
            )

        search_box.SetFocus.assert_called_once_with(waitTime=0)
        search_box.SendKeys.assert_called_once_with("{Enter}", waitTime=0)

    def test_disappeared_popover_does_not_submit_a_different_query(self):
        search_box = _existing(
            SetFocus=Mock(),
            SendKeys=Mock(),
            GetValuePattern=Mock(
                return_value=SimpleNamespace(Value="another target")
            ),
        )

        with patch("wx_Listener._find_search_result_list", return_value=None), patch(
            "wx_Listener._find_search_box",
            return_value=search_box,
        ):
            self.assertFalse(
                _submit_direct_search_with_enter(object(), "target")
            )

        search_box.SetFocus.assert_not_called()
        search_box.SendKeys.assert_not_called()

    def test_exact_search_holds_foreground_lock_for_the_operation(self):
        listener = object.__new__(WeChatListener)
        listener._foreground_ui_lock = MagicMock()

        with patch.object(
            listener,
            "_open_main_chat_from_exact_search_with_foreground",
            return_value="target",
        ) as operation:
            self.assertEqual(
                listener._open_main_chat_from_exact_search("target"),
                "target",
            )

        listener._foreground_ui_lock.__enter__.assert_called_once_with()
        listener._foreground_ui_lock.__exit__.assert_called_once()
        operation.assert_called_once_with(
            "target",
            allow_legacy_fallback=True,
        )

    def test_direct_search_cleanup_only_sends_escape_for_open_popover(self):
        search_box = _existing(SendKeys=Mock())

        with patch("wx_Listener._find_search_result_list", return_value=None), patch(
            "wx_Listener._find_search_box",
            return_value=search_box,
        ):
            _clear_direct_search(object())

        search_box.SendKeys.assert_not_called()

        with patch(
            "wx_Listener._find_search_result_list",
            return_value=_existing(),
        ), patch(
            "wx_Listener._find_search_box",
            return_value=search_box,
        ):
            _clear_direct_search(object())

        search_box.SendKeys.assert_called_once_with("{Esc}", waitTime=0)

    def test_search_click_rejects_row_outside_current_popover(self):
        class Rect:
            def __init__(self, left, top, right, bottom):
                self.left = left
                self.top = top
                self.right = right
                self.bottom = bottom

        scroll_pattern = SimpleNamespace(ScrollIntoView=Mock(return_value=True))
        result_list = _existing(BoundingRectangle=Rect(0, 0, 200, 200))
        result = _existing(
            BoundingRectangle=Rect(0, 300, 200, 340),
            GetScrollItemPattern=Mock(return_value=scroll_pattern),
            Click=Mock(),
        )

        self.assertFalse(
            WeChatListener._click_search_result(result, result_list)
        )
        result.Click.assert_not_called()

    def test_immediate_search_click_skips_scroll_wait(self):
        result = _existing(
            GetParentControl=Mock(),
            Click=Mock(),
        )

        self.assertTrue(
            WeChatListener._click_search_result(result, immediate=True)
        )

        result.GetParentControl.assert_not_called()
        result.Click.assert_called_once_with(
            waitTime=0,
            simulateMove=False,
        )


class WxAuto40SendTest(unittest.TestCase):
    def test_text_send_uses_enter_and_waits_for_empty_input(self):
        class Edit:
            HasKeyboardFocus = True

            def __init__(self):
                self.value = "old draft"
                self.keys = []

            def GetValuePattern(self):
                return SimpleNamespace(Value=self.value)

            def SendKeys(self, keys, **_kwargs):
                self.keys.append(keys)
                if keys == "{DELETE}":
                    self.value = ""
                elif keys == "{Ctrl}v":
                    self.value = "new message"
                elif keys == "{Enter}":
                    self.value = ""

        edit = Edit()

        with patch("wx_Listener._set_clipboard_text", return_value=True):
            with patch("wx_Listener._find_send_button") as find_button:
                self.assertTrue(_send_text_via_input(
                    edit,
                    "new message",
                    before_submit=lambda: edit.keys.append("{reserve}"),
                ))

        find_button.assert_not_called()
        self.assertEqual(edit.value, "")
        self.assertEqual(
            edit.keys[-3:],
            ["{Ctrl}v", "{reserve}", "{Enter}"],
        )

    def test_text_send_marks_enter_exception_as_outcome_unknown(self):
        class Edit:
            HasKeyboardFocus = True

            def __init__(self):
                self.value = ""

            def GetValuePattern(self):
                return SimpleNamespace(Value=self.value)

            def SendKeys(self, keys, **_kwargs):
                if keys == "{DELETE}":
                    self.value = ""
                elif keys == "{Ctrl}v":
                    self.value = "message"
                elif keys == "{Enter}":
                    raise RuntimeError("send failed")

        edit = Edit()

        with patch("wx_Listener._set_clipboard_text", return_value=True):
            with self.assertRaises(_SendOutcomeUnknown) as raised:
                _send_text_via_input(edit, "message")

        self.assertFalse(raised.exception.retry_safe)

    def test_file_send_uses_enter_on_the_same_chat_input(self):
        class Edit:
            HasKeyboardFocus = True

            def __init__(self):
                self.value = ""
                self.keys = []

            def GetValuePattern(self):
                return SimpleNamespace(Value=self.value)

            def SendKeys(self, keys, **_kwargs):
                self.keys.append(keys)
                if keys == "{DELETE}":
                    self.value = ""
                elif keys == "{Ctrl}v":
                    self.value = "\ufffc"
                elif keys == "{Enter}":
                    self.value = ""

        edit = Edit()
        clipboard = types.ModuleType("wx4py.utils.clipboard_utils")
        clipboard.set_files_to_clipboard = Mock(return_value=True)
        utils = types.ModuleType("wx4py.utils")
        utils.clipboard_utils = clipboard
        wx4py = types.ModuleType("wx4py")
        wx4py.utils = utils

        with patch.dict(
            sys.modules,
            {
                "wx4py": wx4py,
                "wx4py.utils": utils,
                "wx4py.utils.clipboard_utils": clipboard,
            },
        ):
            self.assertTrue(_send_files_via_input(edit, ["a.txt", "b.txt"]))

        self.assertEqual(edit.keys[-2:], ["{Ctrl}v", "{Enter}"])
        self.assertEqual(len(clipboard.set_files_to_clipboard.call_args.args[0]), 2)


class WxAuto40WindowSelectionTest(unittest.TestCase):
    def test_subwindow_lookup_stays_in_main_wechat_process(self):
        windows = [
            (201, "target", "Qt51514QWindowIcon"),
            (301, "target", "Qt51514QWindowIcon"),
        ]

        with patch("wx_Listener._list_wechat_windows", return_value=windows):
            with patch(
                "wx_Listener._get_window_process_id",
                side_effect=lambda hwnd: {100: 10, 201: 10, 301: 30}[hwnd],
            ):
                with patch(
                    "wx_Listener._get_uia_window_class",
                    return_value="mmui::FramelessMainWindow",
                ):
                    self.assertEqual(
                        _find_window_by_title("target", exclude_hwnd=100),
                        201,
                    )


if __name__ == "__main__":
    unittest.main()
