import queue
import os
import sys
import tempfile
import threading
import types
import unittest
from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

from chat_name_utils import chat_names_equal
from wx_Listener import (
    UICommand,
    WeChatListener,
    _SubwindowOpenedButUnverified,
    _acquire_image_from_preview,
    _appended_visible_items,
    _activate_native_window,
    _click_image_message,
    _copy_image_from_preview,
    _double_click_control,
    _find_actual_main_window,
    _find_chat_input,
    _find_preview_button,
    _find_preview_toolbar,
    _find_save_dialog_controls,
    _find_session_list,
    _find_session_item,
    _find_window_by_title,
    _force_native_window_foreground,
    _group_sender_from_message,
    _list_wechat_windows,
    _message_direction,
    _message_sender,
    _chat_type_from_header,
    _read_visible_items,
    _require_subwindow_message_list,
    _save_image_via_save_as,
    _send_native_message,
    _SendOutcomeUnknown,
)
from outgoing_registry import OutgoingMessageRegistry


class _FakeRegistry:
    def __init__(self):
        self.records = []
        self.ignored = set()

    def record(self, group, content):
        self.records.append((group, content))

    def should_ignore(self, group, content):
        return (group, content) in self.ignored


class _FakeClient:
    instances = []

    def __init__(self, auto_connect=False):
        self.auto_connect = auto_connect
        self.is_connected = True
        self.connect = Mock(return_value=True)
        self.disconnect = Mock()
        self.chat_window = SimpleNamespace(
            search=Mock(return_value={}),
            _clear_search=Mock(),
            open_chat=Mock(side_effect=AssertionError("不得调用模糊匹配的 open_chat")),
            send_to=Mock(side_effect=AssertionError("不得调用 send_to")),
            send_file_to=Mock(side_effect=AssertionError("不得调用 send_file_to")),
        )
        self.process_groups = Mock(
            side_effect=AssertionError("不得调用 process_groups")
        )
        self.outgoing_registry = _FakeRegistry()
        uia = SimpleNamespace(root=object(), bind=Mock())
        self.window = SimpleNamespace(
            hwnd=100,
            uia=uia,
            activate=Mock(return_value=True),
        )
        self.instances.append(self)


@dataclass(frozen=True)
class _VisibleItem:
    name: str
    runtime_id: tuple
    kind: str = "message"
    class_name: str = "mmui::ChatTextItemView"
    control: object = None
    message_type: str = "text"

    @property
    def key(self):
        return self.runtime_id, self.class_name, self.name


class ExactLookupTest(unittest.TestCase):
    def test_group_sender_prefers_explicit_sender_control(self):
        content = SimpleNamespace(
            Name="你好",
            AutomationId="message_text",
            ClassName="mmui::XLabel",
            ControlTypeName="TextControl",
            GetChildren=lambda: [],
        )
        sender = SimpleNamespace(
            Name="小王",
            AutomationId="sender_name_label",
            ClassName="mmui::XLabel",
            ControlTypeName="TextControl",
            GetChildren=lambda: [],
        )
        row = SimpleNamespace(GetChildren=lambda: [content, sender])

        self.assertEqual(
            _message_sender(row, "你好", "group", "项目群", "left"),
            "小王",
        )

    def test_group_sender_falls_back_to_named_avatar_button(self):
        avatar = SimpleNamespace(
            Name="李工",
            AutomationId="avatar",
            ClassName="mmui::AvatarButton",
            ControlTypeName="ButtonControl",
            GetChildren=lambda: [],
        )
        row = SimpleNamespace(GetChildren=lambda: [avatar])

        self.assertEqual(
            _message_sender(row, "进度如何", "group", "项目群", "left"),
            "李工",
        )

    def test_avatar_alone_is_not_group_type_evidence(self):
        avatar = SimpleNamespace(
            Name="张总",
            AutomationId="avatar",
            ClassName="mmui::AvatarButton",
            ControlTypeName="ButtonControl",
            GetChildren=lambda: [],
        )
        row = SimpleNamespace(Name="状态: 正常", GetChildren=lambda: [avatar])

        self.assertEqual(
            _group_sender_from_message(row, "状态: 正常", "left"),
            "",
        )

    def test_private_sender_is_chat_name_and_right_side_is_self(self):
        self.assertEqual(
            _message_sender(None, "你好", "private", "张总", "left"),
            "张总",
        )
        self.assertTrue(
            _message_sender(None, "收到", "group", "项目群", "right")
        )

    def test_group_sender_can_be_parsed_from_qml_row_name(self):
        row = SimpleNamespace(Name="小王: 你好", GetChildren=lambda: [])

        self.assertEqual(
            _message_sender(row, "你好", "group", "项目群", "left"),
            "小王",
        )

    def test_chat_header_member_count_distinguishes_group_and_private(self):
        chat_box = SimpleNamespace(GetParentControl=lambda: object())
        info = object()
        count = object()

        def find_group(_parent, getter, **criteria):
            if getter == "GroupControl":
                return info
            if criteria.get("AutomationId", "").endswith(
                "current_chat_count_label"
            ):
                return count
            return None

        with patch("wx_Listener._find_chat_box", return_value=chat_box):
            with patch("wx_Listener._find_control", side_effect=find_group):
                with patch("wx_Listener._control_exists_now", return_value=True):
                    self.assertEqual(_chat_type_from_header(object()), "group")

        def find_private(_parent, getter, **_criteria):
            return info if getter == "GroupControl" else None

        with patch("wx_Listener._find_chat_box", return_value=chat_box):
            with patch("wx_Listener._find_control", side_effect=find_private):
                self.assertIsNone(_chat_type_from_header(object()))

    def test_chat_header_detects_renamed_group_member_count(self):
        chat_box = SimpleNamespace(GetParentControl=lambda: object())
        count = SimpleNamespace(
            Name="(18)",
            AutomationId="member_total_label",
            ClassName="mmui::XLabel",
            GetChildren=lambda: [],
        )
        info = SimpleNamespace(GetChildren=lambda: [count])

        def find(_parent, getter, **_criteria):
            return info if getter == "GroupControl" else None

        with patch("wx_Listener._find_chat_box", return_value=chat_box):
            with patch("wx_Listener._find_control", side_effect=find):
                self.assertEqual(_chat_type_from_header(object()), "group")

    def test_session_item_requires_full_normalized_name_match(self):
        session_list = object()
        similar = SimpleNamespace(
            ControlTypeName="ListItemControl",
            Name="Caf\u00e9通知",
            ClassName="SessionCell",
        )
        exact = SimpleNamespace(
            ControlTypeName="ListItemControl",
            Name="Cafe\u0301",
            ClassName="SessionCell",
        )

        with patch("wx_Listener._find_session_list", return_value=session_list):
            with patch(
                "wx_Listener._walk_controls",
                return_value=[(similar, 1), (exact, 1)],
            ):
                found = _find_session_item(object(), "Caf\u00e9")

        self.assertIs(found, exact)

    def test_session_item_accepts_unread_suffix_and_prefers_selected_candidate(self):
        session_list = object()
        unread_three = SimpleNamespace(
            ControlTypeName="ListItemControl",
            Name="元宝原宝(3)",
            ClassName="SessionCell",
            IsSelected=False,
        )
        unread_five = SimpleNamespace(
            ControlTypeName="ListItemControl",
            Name="元宝原宝(5)",
            ClassName="SessionCell",
            IsSelected=True,
        )

        with patch("wx_Listener._find_session_list", return_value=session_list):
            with patch(
                "wx_Listener._walk_controls",
                return_value=[(unread_three, 1), (unread_five, 1)],
            ):
                found = _find_session_item(object(), "元宝原宝")

        self.assertIs(found, unread_five)

    def test_session_item_rejects_contains_match_with_excess_metadata(self):
        session_list = object()
        unrelated = SimpleNamespace(
            ControlTypeName="ListItemControl",
            Name="群" + "很长的无关会话名称" * 3,
            ClassName="SessionCell",
        )

        with patch("wx_Listener._find_session_list", return_value=session_list):
            with patch(
                "wx_Listener._walk_controls",
                return_value=[(unrelated, 1)],
            ):
                found = _find_session_item(object(), "群")

        self.assertIsNone(found)

    def test_session_item_matches_exact_descendant_or_bounded_composite_name(self):
        session_list = object()
        similar = SimpleNamespace(
            ControlTypeName="ListItemControl",
            Name="项目群通知 昨天 例会",
            ClassName="SessionCell",
            GetChildren=lambda: [],
        )
        descendant = SimpleNamespace(
            ControlTypeName="TextControl",
            Name="项目群",
            GetChildren=lambda: [],
        )
        exact = SimpleNamespace(
            ControlTypeName="ListItemControl",
            Name="2 条未读，项目群，最后一条消息",
            ClassName="SessionCell",
            GetChildren=lambda: [descendant],
        )

        with patch("wx_Listener._find_session_list", return_value=session_list):
            with patch(
                "wx_Listener._walk_controls",
                return_value=[(similar, 1), (exact, 4)],
            ) as walk:
                found = _find_session_item(object(), "项目群")

        self.assertIs(found, exact)
        walk.assert_called_once_with(session_list, max_depth=6)

    def test_session_list_fallback_walks_deep_enough_for_qt_wrappers(self):
        expected = SimpleNamespace(
            ControlTypeName="ListControl",
            AutomationId="session_list",
            Name="",
        )
        root = SimpleNamespace(
            ListControl=Mock(side_effect=RuntimeError("direct lookup failed"))
        )

        with patch(
            "wx_Listener._walk_controls",
            return_value=[(expected, 7)],
        ) as walk:
            found = _find_session_list(root)

        self.assertIs(found, expected)
        walk.assert_called_once_with(root, max_depth=8)

    def test_window_lookup_requires_full_normalized_title_match(self):
        windows = [
            (201, "项目群通知", "ChatWindow"),
            (202, "Cafe\u0301", "ChatWindow"),
            (100, "Caf\u00e9", "MainWindow"),
        ]

        with patch("wx_Listener._list_wechat_windows", return_value=windows):
            self.assertIsNone(_find_window_by_title("Caf\u00e9"))
            self.assertEqual(_find_window_by_title("项目群"), 201)
            self.assertEqual(
                _find_window_by_title("Caf\u00e9", exclude_hwnd=100),
                202,
            )

    def test_window_lookup_accepts_only_bounded_title_decorations(self):
        windows = [
            (201, "项目群通知", "ChatWindow"),
            (202, "项目群（3）", "ChatWindow"),
        ]

        with patch("wx_Listener._list_wechat_windows", return_value=windows):
            self.assertEqual(_find_window_by_title("项目群"), 202)

    def test_window_lookup_uses_uia_root_name_when_native_title_is_blank(self):
        root = SimpleNamespace(
            ClassName="mmui::FramelessMainWindow",
            Name="项目群",
        )

        with patch(
            "wx_Listener._list_wechat_windows",
            return_value=[(201, "", "Qt673QWindowIcon")],
        ):
            with patch("wx_Listener._get_window_process_id", return_value=10):
                with patch("wx_Listener._control_from_handle", return_value=root):
                    with patch("wx_Listener._find_current_chat_name", return_value=""):
                        self.assertEqual(
                            _find_window_by_title("项目群", exclude_hwnd=100),
                            201,
                        )

    def test_window_lookup_accepts_generic_or_subwindow_but_rejects_main_root(self):
        for root_class in (
            "Qt51514QWindowIcon",
            "mmui::FramelessMainWindow",
        ):
            with self.subTest(root_class=root_class):
                root = SimpleNamespace(ClassName=root_class, Name="项目群")
                with patch(
                    "wx_Listener._list_wechat_windows",
                    return_value=[(201, "", "Qt51514QWindowIcon")],
                ):
                    with patch(
                        "wx_Listener._list_top_level_windows_by_pid",
                        return_value=[],
                    ):
                        with patch(
                            "wx_Listener._get_window_process_id",
                            return_value=10,
                        ):
                            with patch(
                                "wx_Listener._control_from_handle",
                                return_value=root,
                            ):
                                with patch(
                                    "wx_Listener._find_current_chat_name",
                                    return_value="",
                                ):
                                    self.assertEqual(
                                        _find_window_by_title(
                                            "项目群",
                                            exclude_hwnd=100,
                                        ),
                                        201,
                                    )

        root = SimpleNamespace(ClassName="mmui::MainWindow", Name="项目群")
        with patch(
            "wx_Listener._list_wechat_windows",
            return_value=[(201, "项目群", "Qt51514QWindowIcon")],
        ):
            with patch("wx_Listener._get_window_process_id", return_value=10):
                with patch("wx_Listener._control_from_handle", return_value=root):
                    with patch("wx_Listener._find_current_chat_name", return_value="项目群"):
                        self.assertIsNone(
                            _find_window_by_title("项目群", exclude_hwnd=100)
                        )

    def test_window_lookup_merges_same_pid_windows_missed_by_exe_filter(self):
        root = SimpleNamespace(
            ClassName="Qt51514QWindowIcon",
            Name="项目群",
        )
        normally_enumerated = [(100, "微信", "Qt51514QWindowIcon")]
        same_pid_windows = [
            (100, "微信", "Qt51514QWindowIcon"),
            (201, "", "Qt51514QWindowIcon"),
        ]

        with patch(
            "wx_Listener._list_wechat_windows",
            return_value=normally_enumerated,
        ):
            with patch(
                "wx_Listener._list_top_level_windows_by_pid",
                return_value=same_pid_windows,
            ):
                with patch(
                    "wx_Listener._get_window_process_id",
                    return_value=10,
                ):
                    with patch(
                        "wx_Listener._control_from_handle",
                        return_value=root,
                    ):
                        with patch(
                            "wx_Listener._find_current_chat_name",
                            return_value="",
                        ):
                            self.assertEqual(
                                _find_window_by_title(
                                    "项目群",
                                    exclude_hwnd=100,
                                ),
                                201,
                            )

    def test_wait_finds_reused_handle_even_when_snapshot_already_contains_it(self):
        """A hidden Qt HWND may become the chat window without a new handle."""
        root = SimpleNamespace(
            ClassName="Qt51514QWindowIcon",
            Name="项目群",
        )
        windows = [
            (100, "微信", "Qt51514QWindowIcon"),
            (201, "", "Qt51514QWindowIcon"),
        ]

        with patch("wx_Listener._list_wechat_windows", return_value=windows):
            with patch(
                "wx_Listener._list_top_level_windows_by_pid",
                return_value=windows,
            ):
                with patch(
                    "wx_Listener._get_window_process_id",
                    return_value=10,
                ):
                    with patch(
                        "wx_Listener._control_from_handle",
                        return_value=root,
                    ):
                        with patch(
                            "wx_Listener._find_current_chat_name",
                            return_value="",
                        ):
                            self.assertEqual(
                                _find_window_by_title(
                                    "项目群",
                                    exclude_hwnd=100,
                                ),
                                201,
                            )
                            with patch(
                                "wx_Listener._find_message_list",
                                return_value=object(),
                            ):
                                self.assertEqual(
                                    WeChatListener._wait_for_subwindow(
                                        "项目群",
                                        100,
                                        timeout=0.1,
                                        existing_hwnds={100, 201},
                                    ),
                                    201,
                                )

    def test_find_actual_main_window_uses_uia_main_structure(self):
        roots = {
            100: SimpleNamespace(ClassName="mmui::FramelessMainWindow"),
            101: SimpleNamespace(ClassName="mmui::MainWindow"),
        }
        windows = [
            (100, "项目群", "Qt51514QWindowIcon"),
            (101, "微信", "Qt51514QWindowIcon"),
        ]
        with patch("wx_Listener._list_wechat_windows", return_value=windows):
            with patch("wx_Listener._list_top_level_windows_by_pid", return_value=windows):
                with patch("wx_Listener._get_window_process_id", return_value=10):
                    with patch(
                        "wx_Listener._control_from_handle",
                        side_effect=lambda hwnd: roots[hwnd],
                    ):
                        self.assertEqual(_find_actual_main_window(100), 101)

    def test_double_click_does_not_single_click_a_row_first(self):
        parent = object()
        control = SimpleNamespace(
            GetParentControl=Mock(return_value=parent),
            DoubleClick=Mock(),
            Click=Mock(),
        )
        with patch("wx_Listener._roll_control_into_view", return_value=True):
            self.assertTrue(_double_click_control(control))

        control.Click.assert_not_called()
        control.DoubleClick.assert_called_once_with(
            simulateMove=False,
            waitTime=0,
        )

    def test_subwindow_structure_rejects_a_main_window_session_panel(self):
        message_list = object()
        root = SimpleNamespace(ClassName="Qt51514QWindowIcon")

        with patch("wx_Listener._find_message_list", return_value=message_list):
            with patch("wx_Listener._find_session_panel", return_value=object()):
                with self.assertRaisesRegex(RuntimeError, "ChatMasterView"):
                    _require_subwindow_message_list(
                        root,
                        "项目群",
                        hwnd=201,
                    )

    def test_subwindow_structure_rejects_explicit_main_window_class(self):
        root = SimpleNamespace(ClassName="mmui::MainWindow")
        with self.assertRaisesRegex(RuntimeError, "是微信主窗口"):
            _require_subwindow_message_list(root, "项目群", hwnd=201)

    def test_window_lookup_rejects_contains_when_uia_names_another_chat(self):
        root = SimpleNamespace(
            ClassName="mmui::FramelessMainWindow",
            Name="项目群通知",
        )

        with patch(
            "wx_Listener._list_wechat_windows",
            return_value=[(201, "项目群通知", "Qt673QWindowIcon")],
        ):
            with patch("wx_Listener._get_window_process_id", return_value=10):
                with patch("wx_Listener._control_from_handle", return_value=root):
                    with patch(
                        "wx_Listener._find_current_chat_name",
                        return_value="项目群通知",
                    ):
                        self.assertIsNone(
                            _find_window_by_title("项目群", exclude_hwnd=100)
                        )

    def test_visible_item_read_does_not_hide_control_enumeration_failure(self):
        message_list = SimpleNamespace(
            GetChildren=Mock(side_effect=RuntimeError("stale UIA element"))
        )

        with self.assertRaisesRegex(RuntimeError, "stale UIA element"):
            _read_visible_items(message_list)

    def test_visible_image_row_is_classified_from_wxauto40_signature(self):
        row = SimpleNamespace(
            ClassName="mmui::ChatBubbleItemView",
            Name="图片",
            ControlTypeName="ListItemControl",
            AutomationId="message_1",
            GetRuntimeId=lambda: (1, 2, 3),
        )
        items = _read_visible_items(
            SimpleNamespace(GetChildren=lambda: [row])
        )

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].message_type, "image")
        self.assertIs(items[0].control, row)

    def test_image_class_wins_when_wechat_omits_automation_id(self):
        row = SimpleNamespace(
            ClassName="mmui::ChatBubbleItemView",
            Name="图片",
            ControlTypeName="ListItemControl",
            AutomationId="",
            GetRuntimeId=lambda: (4, 5, 6),
        )

        items = _read_visible_items(
            SimpleNamespace(GetChildren=lambda: [row])
        )

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].kind, "message")
        self.assertEqual(items[0].message_type, "image")

    def test_image_row_accepts_new_class_or_row_metadata(self):
        rows = (
            SimpleNamespace(
                ClassName="mmui::ChatImageItemView",
                Name="图片",
                ControlTypeName="ListItemControl",
                AutomationId="message_14",
                GetRuntimeId=lambda: (14,),
            ),
            SimpleNamespace(
                ClassName="mmui::DataItemView",
                Name="",
                ControlTypeName="ListItemControl",
                AutomationId="chat_image_message_15",
                GetRuntimeId=lambda: (15,),
            ),
        )

        items = _read_visible_items(
            SimpleNamespace(GetChildren=lambda: list(rows))
        )

        self.assertEqual([item.message_type for item in items], ["image", "image"])
        self.assertEqual([item.kind for item in items], ["message", "message"])

    def test_image_label_can_be_localized_or_published_by_nested_control(self):
        for label in ("圖片", "Photo", "[Image]"):
            with self.subTest(label=label):
                row = SimpleNamespace(
                    ClassName="mmui::ChatBubbleItemView",
                    Name=label,
                    ControlTypeName="ListItemControl",
                    AutomationId="",
                    GetRuntimeId=lambda: (7, 8, 9),
                )
                item = _read_visible_items(
                    SimpleNamespace(GetChildren=lambda: [row])
                )[0]
                self.assertEqual(item.message_type, "image")

        nested = SimpleNamespace(
            Name="Photo",
            AutomationId="image_message",
            ClassName="mmui::ImageBubble",
            GetChildren=lambda: [],
        )
        row = SimpleNamespace(
            ClassName="mmui::ChatBubbleItemView",
            Name="",
            ControlTypeName="ListItemControl",
            AutomationId="",
            GetRuntimeId=lambda: (10, 11, 12),
            GetChildren=lambda: [nested],
        )
        item = _read_visible_items(
            SimpleNamespace(GetChildren=lambda: [row])
        )[0]
        self.assertEqual(item.name, "[图片]")
        self.assertEqual(item.message_type, "image")

    def test_non_image_bubble_is_not_inferred_from_avatar_image(self):
        avatar = SimpleNamespace(
            Name="",
            AutomationId="sender_avatar_image",
            ClassName="mmui::AvatarImageView",
            GetChildren=lambda: [],
        )
        row = SimpleNamespace(
            ClassName="mmui::ChatBubbleItemView",
            Name="文件\nreport.pdf",
            ControlTypeName="ListItemControl",
            AutomationId="message_13",
            GetRuntimeId=lambda: (13,),
            GetChildren=lambda: [avatar],
        )

        item = _read_visible_items(
            SimpleNamespace(GetChildren=lambda: [row])
        )[0]

        self.assertEqual(item.message_type, "text")

    def test_unnamed_sizable_inset_bubble_is_detected_but_edge_avatar_is_not(self):
        def rect(left, top, right, bottom):
            return SimpleNamespace(
                left=left,
                top=top,
                right=right,
                bottom=bottom,
            )

        avatar = SimpleNamespace(
            Name="",
            AutomationId="avatar",
            ClassName="mmui::AvatarView",
            ControlTypeName="ImageControl",
            BoundingRectangle=rect(5, 5, 45, 45),
            GetChildren=lambda: [],
        )
        image_bubble = SimpleNamespace(
            Name="",
            AutomationId="",
            ClassName="",
            ControlTypeName="ButtonControl",
            BoundingRectangle=rect(85, 5, 225, 145),
            GetChildren=lambda: [],
        )

        def row(children, runtime_id):
            return SimpleNamespace(
                ClassName="mmui::ChatBubbleItemView",
                Name="",
                ControlTypeName="ListItemControl",
                AutomationId="",
                BoundingRectangle=rect(0, 0, 800, 160),
                GetRuntimeId=lambda: runtime_id,
                GetChildren=lambda: children,
            )

        image_items = _read_visible_items(
            SimpleNamespace(GetChildren=lambda: [row([avatar, image_bubble], (14,))])
        )
        avatar_only_items = _read_visible_items(
            SimpleNamespace(GetChildren=lambda: [row([avatar], (15,))])
        )

        self.assertEqual(image_items[0].message_type, "image")
        self.assertEqual(image_items[0].name, "[图片]")
        self.assertEqual(avatar_only_items, [])

    def test_image_direction_uses_wxauto40_screenshot_before_child_buttons(self):
        with tempfile.NamedTemporaryFile(delete=False) as stream:
            screenshot = stream.name
        control = SimpleNamespace(
            MessageDirection="",
            ScreenShot=Mock(return_value=screenshot),
            GetChildren=lambda: [
                SimpleNamespace(
                    ControlTypeName="ButtonControl",
                    Name="right-side-action",
                    GetChildren=lambda: [],
                )
            ],
        )
        try:
            with patch(
                "wx_Listener._direction_from_message_screenshot",
                return_value="left",
            ) as detect:
                self.assertEqual(_message_direction(control), "left")
        finally:
            if os.path.exists(screenshot):
                os.unlink(screenshot)

        detect.assert_called_once_with(screenshot)

    def test_image_double_click_prefers_original_unnamed_bubble_control(self):
        bubble = SimpleNamespace(
            Exists=Mock(return_value=True),
            Name="",
            ControlTypeName="ButtonControl",
            DoubleClick=Mock(),
            Click=Mock(),
        )
        row = SimpleNamespace(
            ButtonControl=Mock(return_value=bubble),
            GetChildren=lambda: [],
            DoubleClick=Mock(),
            Click=Mock(),
        )

        self.assertTrue(_click_image_message(row, "left"))

        row.ButtonControl.assert_called_once_with(Name="")
        bubble.DoubleClick.assert_called_once_with(
            waitTime=0,
            simulateMove=False,
        )
        bubble.Click.assert_not_called()
        row.DoubleClick.assert_not_called()
        row.Click.assert_not_called()

    def test_image_double_click_uses_side_aware_row_fallback(self):
        missing = SimpleNamespace(Exists=Mock(return_value=False))
        row = SimpleNamespace(
            ButtonControl=Mock(return_value=missing),
            GetChildren=lambda: [],
            DoubleClick=Mock(),
            Click=Mock(),
        )

        self.assertTrue(_click_image_message(row, "right"))

        row.DoubleClick.assert_called_once_with(
            waitTime=0,
            x=-102,
            y=30,
            ratioX=1,
            ratioY=0,
            simulateMove=False,
        )
        row.Click.assert_not_called()

    def test_ordered_snapshot_accepts_reused_image_id_at_stable_count(self):
        image = _VisibleItem(
            "图片",
            (1,),
            class_name="mmui::ChatBubbleItemView",
            message_type="image",
        )
        anchor = _VisibleItem("锚点", (2,))

        appended, anchored = _appended_visible_items(
            [image, anchor],
            [anchor, image],
        )

        self.assertTrue(anchored)
        self.assertEqual(appended, [image])

    def test_disjoint_history_snapshot_does_not_replace_bottom_anchor(self):
        previous = [_VisibleItem("最新一", (3,)), _VisibleItem("最新二", (4,))]
        history = [_VisibleItem("历史一", (1,)), _VisibleItem("历史二", (2,))]

        appended, anchored = _appended_visible_items(previous, history)

        self.assertEqual(appended, [])
        self.assertFalse(anchored)

    def test_disjoint_bottom_snapshot_is_a_fast_message_burst(self):
        previous = [_VisibleItem("旧一", (1,)), _VisibleItem("旧二", (2,))]
        current = [_VisibleItem("新一", (3,)), _VisibleItem("新二", (4,))]

        appended, anchored = _appended_visible_items(
            previous,
            current,
            disjoint_is_append=True,
        )

        self.assertEqual(appended, current)
        self.assertTrue(anchored)

    def test_preview_copy_clicks_more_and_copy_then_reads_file(self):
        missing = SimpleNamespace(Exists=lambda **_kwargs: False)
        more = SimpleNamespace(
            Name="更多",
            Exists=lambda **_kwargs: True,
            Click=Mock(),
        )
        toolbar = SimpleNamespace(
            ButtonControl=Mock(
                side_effect=lambda **criteria: (
                    more if criteria.get("Name") == "更多" else missing
                )
            )
        )
        copy_item = SimpleNamespace(Click=Mock())
        with tempfile.TemporaryDirectory() as directory:
            image_path = os.path.join(directory, "wechat.png")
            with open(image_path, "wb") as stream:
                stream.write(b"image")
            with patch(
                "wx_Listener._find_popup_menu_item",
                return_value=copy_item,
            ):
                with patch(
                    "wx_Listener._read_clipboard_file_paths",
                    return_value=[image_path],
                ):
                    with patch("wx_Listener._set_clipboard_text", return_value=True):
                        self.assertEqual(
                            _copy_image_from_preview(toolbar, 10, timeout=1),
                            image_path,
                        )

        more.Click.assert_called_once_with(waitTime=0, simulateMove=False)
        copy_item.Click.assert_called_once_with(waitTime=0, simulateMove=False)

    def test_preview_button_can_live_inside_wxauto40_toolbar_wrapper(self):
        missing = SimpleNamespace(Exists=lambda **_kwargs: False)
        more = SimpleNamespace(
            Name="更多",
            ControlTypeName="ButtonControl",
            Exists=lambda **_kwargs: True,
        )
        wrapper = SimpleNamespace(
            GetChildren=lambda: [],
            ButtonControl=Mock(return_value=more),
        )
        toolbar = SimpleNamespace(
            ButtonControl=Mock(return_value=missing),
            GetChildren=lambda: [wrapper],
        )

        self.assertIs(_find_preview_button(toolbar, ("更多",)), more)

    def test_preview_toolbar_is_detected_by_visible_save_button(self):
        missing = SimpleNamespace(Exists=lambda **_kwargs: False)
        save = SimpleNamespace(
            Name="保存",
            ControlTypeName="ButtonControl",
            Exists=lambda **_kwargs: True,
            GetChildren=lambda: [],
        )
        toolbar = SimpleNamespace(
            ClassName="mmui::MediaActionBar",
            ControlTypeName="ToolBarControl",
            ButtonControl=Mock(
                side_effect=lambda **criteria: (
                    save if criteria.get("Name") == "保存" else missing
                )
            ),
            GetChildren=lambda: [save],
        )
        root = SimpleNamespace(
            ClassName="mmui::ImagePreviewView",
            ControlTypeName="WindowControl",
            GetChildren=lambda: [toolbar],
        )

        self.assertIs(_find_preview_toolbar(root), toolbar)

    def test_image_acquire_uses_fast_clipboard_flow_first(self):
        toolbar = object()
        with patch(
            "wx_Listener._save_image_via_save_as",
        ) as save_as:
            with patch(
                "wx_Listener._copy_image_from_preview",
                return_value=r"C:\\Temp\\wechat-image.jpg",
            ) as copy_image:
                path, temporary = _acquire_image_from_preview(
                    toolbar,
                    process_id=10,
                    timeout=1,
                )

        self.assertEqual(path, r"C:\\Temp\\wechat-image.jpg")
        self.assertFalse(temporary)
        copy_image.assert_called_once()
        save_as.assert_not_called()

    def test_image_acquire_uses_save_dialog_when_clipboard_flow_fails(self):
        toolbar = object()
        with patch(
            "wx_Listener._copy_image_from_preview",
            side_effect=RuntimeError("copy unavailable"),
        ) as copy_image:
            with patch(
                "wx_Listener._save_image_via_save_as",
                return_value=r"C:\\Temp\\wechat-image.png",
            ) as save_as:
                path, temporary = _acquire_image_from_preview(
                    toolbar,
                    process_id=10,
                    timeout=1,
                )

        self.assertEqual(path, r"C:\\Temp\\wechat-image.png")
        self.assertTrue(temporary)
        save_as.assert_called_once()
        copy_image.assert_called_once()

    def test_save_flow_clicks_preview_save_then_completes_dialog(self):
        win32con = types.ModuleType("win32con")
        win32con.WM_SETTEXT = 12
        win32con.BM_CLICK = 245
        with patch.dict(sys.modules, {"win32con": win32con}):
            with patch("wx_Listener._top_level_control", return_value=None):
                with patch("wx_Listener._invoke_preview_action") as invoke:
                    with patch(
                        "wx_Listener._find_save_dialog_controls",
                        return_value=(100, 101, 102),
                    ):
                        with patch("wx_Listener._send_native_message") as send:
                            with patch(
                                "wx_Listener._wait_for_stable_file",
                                return_value=True,
                            ):
                                path = _save_image_via_save_as(
                                    object(),
                                    process_id=10,
                                    timeout=1,
                                )

        self.assertTrue(path.endswith(".jpg"))
        self.assertIn("保存", invoke.call_args.args[2])
        self.assertEqual(send.call_args_list[0].args[:3], (101, 12, 0))
        self.assertEqual(send.call_args_list[0].args[3], path)
        self.assertEqual(send.call_args_list[1].args, (102, 245, 0, 0))

    def test_save_as_dialog_prefers_populated_filename_edit(self):
        win32gui = types.ModuleType("win32gui")
        win32process = types.ModuleType("win32process")
        texts = {
            100: "保存图片",
            201: "",
            202: "微信图片.jpg",
            203: "保存(&S)",
        }
        classes = {
            201: "Edit",
            202: "Edit",
            203: "Button",
        }
        win32gui.GetWindowText = lambda hwnd: texts[hwnd]
        win32gui.GetClassName = lambda hwnd: classes[hwnd]
        win32gui.IsWindowVisible = lambda _hwnd: True
        win32gui.EnumWindows = lambda callback, extra: callback(100, extra)

        def enum_children(_hwnd, callback, extra):
            for child in (201, 202, 203):
                callback(child, extra)

        win32gui.EnumChildWindows = enum_children
        win32process.GetWindowThreadProcessId = lambda _hwnd: (1, 10)

        with patch.dict(
            sys.modules,
            {
                "win32gui": win32gui,
                "win32process": win32process,
            },
        ):
            controls = _find_save_dialog_controls(10, timeout=0.1)

        self.assertEqual(controls, (100, 202, 203))

    def test_save_dialog_messages_abort_when_native_window_is_hung(self):
        win32con = types.ModuleType("win32con")
        win32con.SMTO_ABORTIFHUNG = 2
        win32gui = types.ModuleType("win32gui")
        win32gui.SendMessageTimeout = Mock(return_value=1)
        win32gui.SendMessage = Mock()

        with patch.dict(
            sys.modules,
            {"win32con": win32con, "win32gui": win32gui},
        ):
            self.assertEqual(
                _send_native_message(100, 200, 0, "path", timeout_ms=750),
                1,
            )

        win32gui.SendMessageTimeout.assert_called_once_with(
            100,
            200,
            0,
            "path",
            win32con.SMTO_ABORTIFHUNG,
            750,
        )
        win32gui.SendMessage.assert_not_called()

    def test_chat_input_fallback_accepts_known_lower_edit_class(self):
        class Rect:
            left = 0
            top = 0
            right = 800
            bottom = 600

        class EditRect:
            left = 150
            top = 390
            right = 760
            bottom = 560

        missing = SimpleNamespace(Exists=Mock(return_value=False))
        expected = SimpleNamespace(
            Exists=Mock(return_value=True),
            BoundingRectangle=EditRect(),
        )
        root = SimpleNamespace(
            BoundingRectangle=Rect(),
            EditControl=Mock(
                side_effect=lambda **kwargs: (
                    expected
                    if kwargs.get("ClassName") == "mmui::XTextEdit"
                    else missing
                )
            ),
        )

        self.assertIs(_find_chat_input(root), expected)


class WindowActivationTest(unittest.TestCase):
    def test_activation_does_not_trust_wx4py_without_foreground_ownership(self):
        wx4py_module = types.ModuleType("wx4py")
        core_module = types.ModuleType("wx4py.core")
        win32_module = types.ModuleType("wx4py.core.win32")
        win32_module.bring_window_to_front = Mock(return_value=True)
        wx4py_module.core = core_module
        core_module.win32 = win32_module
        root = SimpleNamespace(SetActive=Mock(return_value=False))

        with patch.dict(sys.modules, {
            "wx4py": wx4py_module,
            "wx4py.core": core_module,
            "wx4py.core.win32": win32_module,
        }):
            with patch("wx_Listener._restore_native_window", return_value=True):
                with patch(
                    "wx_Listener._is_native_window_foreground",
                    return_value=False,
                ):
                    with patch(
                        "wx_Listener._wait_for_native_window_foreground",
                        return_value=False,
                    ):
                        with patch(
                            "wx_Listener._control_from_handle",
                            return_value=root,
                        ):
                            with patch(
                                "wx_Listener._force_native_window_foreground",
                                return_value=True,
                            ) as force:
                                self.assertTrue(_activate_native_window(200))

        win32_module.bring_window_to_front.assert_called_once_with(200)
        root.SetActive.assert_called_once_with(waitTime=0)
        force.assert_called_once_with(200)

    def test_forced_activation_attaches_and_always_detaches_input_threads(self):
        win32api = types.ModuleType("win32api")
        win32api.keybd_event = Mock()
        win32con = types.ModuleType("win32con")
        win32con.VK_MENU = 0x12
        win32con.KEYEVENTF_KEYUP = 0x0002
        win32gui = types.ModuleType("win32gui")
        win32gui.GetForegroundWindow = Mock(return_value=999)
        win32gui.BringWindowToTop = Mock()
        win32gui.SetActiveWindow = Mock()
        win32gui.SetForegroundWindow = Mock()
        win32process = types.ModuleType("win32process")
        win32process.GetWindowThreadProcessId = Mock(
            side_effect=lambda hwnd: (20, 2) if hwnd == 200 else (30, 3)
        )
        win32process.AttachThreadInput = Mock(return_value=True)

        with patch.dict(sys.modules, {
            "win32api": win32api,
            "win32con": win32con,
            "win32gui": win32gui,
            "win32process": win32process,
        }):
            with patch("wx_Listener.threading.get_native_id", return_value=10):
                with patch(
                    "wx_Listener._wait_for_native_window_foreground",
                    return_value=True,
                ):
                    self.assertTrue(_force_native_window_foreground(200))

        attached = [
            item.args
            for item in win32process.AttachThreadInput.call_args_list
            if item.args[2] is True
        ]
        detached = [
            item.args
            for item in win32process.AttachThreadInput.call_args_list
            if item.args[2] is False
        ]
        self.assertEqual(len(attached), 3)
        self.assertEqual(
            detached,
            [(source, target, False) for source, target, _attach in reversed(attached)],
        )
        win32gui.SetForegroundWindow.assert_called_once_with(200)


class WindowEnumerationTest(unittest.TestCase):
    def test_enumeration_uses_process_and_version_agnostic_qt_fallback(self):
        window_data = {
            101: ("项目群", "WeixinDesktopWindow", 11),
            102: ("其他 Qt", "Qt673QWindowIcon", 12),
            103: ("运营群", "Qt673QWindowIcon", 13),
            104: ("辅助窗口", "HelperWindow", 14),
        }
        win32gui = types.ModuleType("win32gui")
        win32process = types.ModuleType("win32process")
        win32gui.GetWindowText = lambda hwnd: window_data[hwnd][0]
        win32gui.GetClassName = lambda hwnd: window_data[hwnd][1]
        win32gui.EnumWindows = lambda callback, extra: [
            callback(hwnd, extra) for hwnd in window_data
        ]
        win32process.GetWindowThreadProcessId = (
            lambda hwnd: (1, window_data[hwnd][2])
        )
        image_paths = {
            11: r"C:\Program Files\Tencent\Weixin.exe",
            12: r"C:\Tools\OtherQt.exe",
            13: "",
            14: r"C:\Program Files\Tencent\WeChatAppEx.exe",
        }

        with patch.dict(
            sys.modules,
            {"win32gui": win32gui, "win32process": win32process},
        ):
            with patch(
                "wx_Listener._get_process_image_name",
                side_effect=lambda pid: image_paths[pid],
            ):
                windows = _list_wechat_windows()

        self.assertEqual(
            windows,
            [
                (101, "项目群", "WeixinDesktopWindow"),
                (103, "运营群", "Qt673QWindowIcon"),
            ],
        )


class Wx4pyListenerTest(unittest.TestCase):
    def setUp(self):
        _FakeClient.instances.clear()
        module = types.ModuleType("wx4py")
        module.WeChatClient = _FakeClient
        self.module_patch = patch.dict(sys.modules, {"wx4py": module})
        self.module_patch.start()

        # 现有测试覆盖全量打开窗口路径；patch 新配置以保持原行为
        self.config_patches = [
            patch("wx_Listener.WX_OPEN_WINDOWS_ON_DEMAND", False),
            patch("wx_Listener.WX_BLACKLIST_MODE", False),
            patch("wx_Listener.WX_EXCLUDED_CHATS", []),
        ]
        for item in self.config_patches:
            item.start()

        self.main_root = None
        self.session_items = {}
        self.session_hwnds = {}
        self.open_windows = {}
        self.window_roots = {}
        self.window_titles = {}
        self.message_items = {}
        self.inputs = {}
        self.closed_windows = []

        self.helper_patches = [
            patch("wx_Listener._find_session_list", side_effect=self._find_session_list),
            patch("wx_Listener._find_session_item", side_effect=self._find_session_item),
            patch("wx_Listener._double_click_control", side_effect=self._double_click),
            patch("wx_Listener._find_window_by_title", side_effect=self._find_window),
            patch("wx_Listener._control_from_handle", side_effect=self._control_from_handle),
            patch("wx_Listener._find_message_list", side_effect=lambda root: root.message_list),
            patch("wx_Listener._read_visible_items", side_effect=lambda msg_list: list(self.message_items[msg_list])),
            patch("wx_Listener._find_chat_input", side_effect=lambda root: self.inputs.get(id(root))),
            patch(
                "wx_Listener._send_text_via_input",
                side_effect=lambda _edit, _content, before_submit=None: (
                    before_submit() if before_submit else None
                ) or True,
            ),
            patch(
                "wx_Listener._send_files_via_input",
                side_effect=lambda _edit, _paths, before_submit=None: (
                    before_submit() if before_submit else None
                ) or True,
            ),
            patch("wx_Listener._get_window_title", side_effect=lambda hwnd: self.window_titles[hwnd]),
            patch("wx_Listener._list_wechat_windows", side_effect=self._list_windows),
            patch("wx_Listener._close_window", side_effect=self._close_window),
            patch("wx_Listener._activate_native_window", return_value=True),
        ]
        self.helpers = [item.start() for item in self.helper_patches]

    def tearDown(self):
        for item in reversed(self.helper_patches):
            item.stop()
        for item in reversed(self.config_patches):
            item.stop()
        self.module_patch.stop()

    def make_listener(self, targets, callback=None, commands=None, registry=None):
        listener = WeChatListener(
            target_chats=targets,
            callback=callback,
            command_queue=commands,
            stop_event=threading.Event(),
            outgoing_registry=registry,
        )
        self.main_root = listener.wx.window.uia.root
        self.inputs[id(self.main_root)] = object()
        return listener

    def add_chat(self, name, baseline=None, hwnd=None):
        hwnd = hwnd or 200 + len(self.session_items)
        item = SimpleNamespace(chat_name=name)
        message_list = object()
        root = SimpleNamespace(message_list=message_list)
        edit = object()
        self.session_items[name] = item
        self.session_hwnds[name] = hwnd
        self.window_roots[hwnd] = root
        self.window_titles[hwnd] = name
        self.message_items[message_list] = list(baseline or [])
        self.inputs[id(root)] = edit
        return hwnd, root, edit

    def add_search_only_chat(self, name, group="群聊", baseline=None):
        hwnd, root, edit = self.add_chat(name, baseline=baseline)
        item = self.session_items.pop(name)
        result_control = Mock()
        result_control.Click.side_effect = lambda **_kwargs: self.session_items.__setitem__(
            name, item
        )
        result = SimpleNamespace(name=name, ctrl=result_control, group=group)
        return hwnd, root, edit, result, result_control

    def _find_session_list(self, root):
        return object() if root is self.main_root else None

    def _find_session_item(self, root, name, **_criteria):
        if root is not self.main_root:
            return None
        for candidate_name, item in self.session_items.items():
            if chat_names_equal(candidate_name, name):
                return item
        return None

    def _double_click(self, item):
        for name, candidate in self.session_items.items():
            if candidate is item:
                self.open_windows[name] = self.session_hwnds[name]
                return True
        return False

    def _find_window(self, title, exclude_hwnd=None):
        hwnd = self.open_windows.get(title)
        return None if hwnd == exclude_hwnd else hwnd

    def _control_from_handle(self, hwnd):
        return self.window_roots[hwnd]

    def _list_windows(self):
        return [
            (hwnd, self.window_titles[hwnd], "ChatWindow")
            for hwnd in self.open_windows.values()
            if hwnd not in self.closed_windows
        ]

    def _close_window(self, hwnd):
        self.closed_windows.append(hwnd)
        for name, candidate in list(self.open_windows.items()):
            if candidate == hwnd:
                self.open_windows.pop(name)

    def test_start_uses_exact_session_list_match_without_search(self):
        listener = self.make_listener([
            {"name": "项目群", "type": "group"},
            {"name": "张总", "type": "private"},
        ], callback=Mock())
        self.add_chat("项目群")
        self.add_chat("张总")

        processor = listener.start_listening()

        self.assertTrue(processor.is_running)
        self.assertEqual(listener.listening_target_names, ["项目群", "张总"])
        self.assertEqual(self.helpers[1].call_args_list, [
            call(self.main_root, "项目群"),
            call(self.main_root, "张总"),
        ])
        self.assertEqual(self.helpers[2].call_count, 2)
        listener.wx.chat_window.search.assert_not_called()
        listener.wx.chat_window.open_chat.assert_not_called()
        listener.wx.process_groups.assert_not_called()

    def test_untyped_session_list_match_does_not_search_for_type(self):
        listener = self.make_listener(["项目群"], callback=Mock())
        self.add_chat("项目群")

        processor = listener.start_listening()

        self.assertTrue(processor.is_running)
        self.assertEqual(listener.listening_target_names, ["项目群"])
        listener.wx.chat_window.search.assert_not_called()

    def test_listen_all_compatibility_flag_does_not_silently_listen_to_nothing(self):
        listener = self.make_listener([], callback=Mock())

        with patch("wx_Listener.WX_LISTEN_ALL_IF_EMPTY", True):
            with self.assertRaisesRegex(RuntimeError, "必须显式配置"):
                listener.start_listening()

        self.assertFalse(listener.running)

    def test_stop_request_interrupts_target_initialization_before_next_window(self):
        listener = self.make_listener(["项目群", "张总"], callback=Mock())
        listener.stop_event.set()

        with patch.object(listener, "_get_or_open_session") as open_session:
            self.assertIsNone(listener.start_listening())

        open_session.assert_not_called()
        self.assertFalse(listener.running)
        self.assertEqual(listener.listening_target_names, [])

    def test_start_accepts_nonstandard_subwindow_root_class_after_structure_check(self):
        listener = self.make_listener(["项目群"], callback=Mock())
        hwnd, root, _edit = self.add_chat("项目群")
        root.ClassName = "Qt51514QWindowIcon"

        listener.start_listening()

        session = listener._sessions["项目群"]
        self.assertEqual(session.hwnd, hwnd)
        self.assertIs(session.root, root)
        self.assertEqual(listener.listening_target_names, ["项目群"])

    def test_missing_session_falls_back_to_exact_search_result(self):
        listener = self.make_listener(
            ["项目群", "历史项目群"], callback=Mock()
        )
        self.add_chat("项目群")
        _hwnd, _root, _edit, exact, exact_control = self.add_search_only_chat(
            "历史项目群"
        )
        similar_control = Mock()
        listener.wx.chat_window.search.return_value = {
            "群聊": [
                SimpleNamespace(
                    name="历史项目群（通知）",
                    ctrl=similar_control,
                    group="群聊",
                ),
                exact,
            ]
        }

        listener.start_listening()

        self.assertEqual(listener.listening_target_names, ["项目群", "历史项目群"])
        listener.wx.chat_window.search.assert_called_once_with("历史项目群")
        exact_control.Click.assert_called_once_with(
            waitTime=0,
            simulateMove=False,
        )
        listener.wx.chat_window._clear_search.assert_called_once_with()
        similar_control.Click.assert_not_called()
        listener.wx.chat_window.open_chat.assert_not_called()
        listener.wx.process_groups.assert_not_called()
        self.assertGreaterEqual(listener.wx.window.activate.call_count, 3)
        self.assertGreaterEqual(self.helpers[13].call_count, 3)
        self.helpers[2].assert_called()

    def test_untyped_contact_search_result_is_clicked_and_type_is_detected(self):
        listener = self.make_listener(["张总"], callback=Mock())
        _hwnd, _root, _edit, exact, exact_control = self.add_search_only_chat(
            "张总", group="联系人"
        )
        listener.wx.chat_window.search.return_value = {"联系人": [exact]}

        listener.start_listening()

        exact_control.Click.assert_called_once_with(
            waitTime=0,
            simulateMove=False,
        )
        self.assertEqual(listener.listening_target_names, ["张总"])
        self.assertEqual(listener.chat_types["张总"], "private")

    def test_search_type_is_cached_for_result_name_and_window_title(self):
        listener = self.make_listener(["wxid_123"], callback=Mock())
        _hwnd, _root, _edit, result, _control = self.add_search_only_chat(
            "张总", group="联系人"
        )
        result.name = "张总 微信号: wxid_123"
        listener.wx.chat_window.search.return_value = {"联系人": [result]}

        listener.start_listening()

        self.assertEqual(listener.chat_types["wxid_123"], "private")
        self.assertEqual(listener.chat_types["张总"], "private")
        self.assertEqual(listener._window_chat_types["张总"], "private")

    def test_explicit_type_wins_over_conflicting_search_category(self):
        listener = self.make_listener(
            [{"name": "项目群", "type": "group"}], callback=Mock()
        )
        self.add_chat("项目群")
        listener._cache_detected_chat_type(
            "项目群", "private", source="search", window_title="项目群"
        )

        self.assertEqual(listener.chat_types["项目群"], "group")
        self.assertEqual(listener._resolved_chat_type("项目群"), "group")

    def test_wechat_id_search_rebinds_to_the_result_display_name(self):
        listener = self.make_listener([
            {"name": "wxid_123", "type": "private"}
        ], callback=Mock())
        _hwnd, _root, _edit, result, result_control = self.add_search_only_chat(
            "张总", group="联系人"
        )
        result.name = "张总 微信号: wxid_123"
        listener.wx.chat_window.search.return_value = {"联系人": [result]}

        listener.start_listening()

        result_control.Click.assert_called_once_with(
            waitTime=0,
            simulateMove=False,
        )
        self.assertEqual(listener._chat_aliases["wxid_123"], "张总")
        self.assertEqual(listener._sessions["wxid_123"].name, "张总")
        self.assertEqual(listener.chat_types["张总"], "private")

    def test_explicit_group_does_not_click_same_named_contact(self):
        result = SimpleNamespace(name="项目群", ctrl=Mock(), group="联系人")

        self.assertIsNone(WeChatListener._find_exact_search_result(
            {"联系人": [result]}, "项目群", "group"
        ))

    def test_search_result_can_use_exact_descendant_name(self):
        name_control = SimpleNamespace(Name="张总", GetChildren=lambda: [])
        result_control = SimpleNamespace(
            Name="微信号 wxid_123",
            GetChildren=lambda: [name_control],
        )
        result = SimpleNamespace(
            name="微信号 wxid_123",
            ctrl=result_control,
            group="联系人",
        )

        self.assertIs(
            WeChatListener._find_exact_search_result(
                {"联系人": [result]}, "张总", None
            ),
            result,
        )

    def test_missing_search_results_are_skipped_after_all_targets_are_tried(self):
        listener = self.make_listener(
            ["项目群", "不存在的群", "运营群"], callback=Mock()
        )
        self.add_chat("项目群")
        self.add_chat("运营群")

        with self.assertLogs("wx_Listener", level="WARNING") as logs:
            processor = listener.start_listening()

        self.assertTrue(processor.is_running)
        self.assertTrue(listener.running)
        self.assertEqual(listener.listening_target_names, ["项目群", "运营群"])
        self.assertEqual(listener._failed_target_names, ["不存在的群"])
        self.assertTrue(any("不存在的群" in line for line in logs.output))
        self.assertEqual(listener.wx.chat_window.search.call_count, 2)

    def test_open_failure_skips_target_and_continues_running(self):
        listener = self.make_listener(
            ["项目群", "损坏会话", "运营群"], callback=Mock()
        )
        self.add_chat("项目群")
        self.add_chat("损坏会话")
        self.add_chat("运营群")
        broken_item = self.session_items["损坏会话"]
        original_double_click = self._double_click

        def double_click(item):
            if item is broken_item:
                raise RuntimeError("模拟窗口打开失败")
            return original_double_click(item)

        self.helpers[2].side_effect = double_click

        with self.assertLogs("wx_Listener", level="WARNING") as logs:
            processor = listener.start_listening()

        self.assertTrue(processor.is_running)
        self.assertTrue(listener.running)
        self.assertEqual(listener.listening_target_names, ["项目群", "运营群"])
        self.assertEqual(listener._failed_target_names, ["损坏会话"])
        self.assertTrue(any("模拟窗口打开失败" in line for line in logs.output))

    def test_failed_target_is_retried_after_startup(self):
        listener = self.make_listener(["稍后出现的群"], callback=Mock())

        with self.assertLogs("wx_Listener", level="WARNING"):
            processor = listener.start_listening()

        self.assertTrue(processor.is_running)
        self.assertEqual(listener.listening_target_names, [])
        self.add_chat("稍后出现的群")
        listener._next_failed_target_retry_at = 0

        listener.process_commands(limit=0)

        self.assertEqual(listener.listening_target_names, ["稍后出现的群"])
        self.assertEqual(listener._failed_target_names, [])

    def test_search_escapes_sendkeys_braces_but_matches_original_name(self):
        listener = self.make_listener(["项目{Enter}群"], callback=Mock())
        _hwnd, _root, _edit, exact, exact_control = self.add_search_only_chat(
            "项目{Enter}群"
        )
        listener.wx.chat_window.search.return_value = {"群聊": [exact]}

        listener.start_listening()

        listener.wx.chat_window.search.assert_called_once_with(
            "项目{{}Enter{}}群"
        )
        exact_control.Click.assert_called_once_with(
            waitTime=0,
            simulateMove=False,
        )

    def test_all_missing_sessions_keep_listener_running_after_exact_search(self):
        listener = self.make_listener(["不存在一", "不存在二"], callback=Mock())

        with self.assertLogs("wx_Listener", level="WARNING"):
            processor = listener.start_listening()

        self.assertTrue(processor.is_running)
        self.assertTrue(listener.running)
        self.assertEqual(listener.listening_target_names, [])
        self.assertEqual(listener._failed_target_names, ["不存在一", "不存在二"])
        self.assertEqual(listener.wx.chat_window.search.call_count, 4)
        listener.wx.chat_window.open_chat.assert_not_called()
        listener.wx.process_groups.assert_not_called()

    def test_search_prefers_exact_match_across_all_relevant_groups(self):
        frequent_contains = SimpleNamespace(name="项目群通知")
        primary_exact = SimpleNamespace(name="项目群")

        self.assertIs(
            WeChatListener._find_exact_search_result(
                {
                    "最常使用": [frequent_contains],
                    "群聊": [primary_exact],
                },
                "项目群",
                "group",
            ),
            primary_exact,
        )

    def test_search_rejects_unclassified_frequent_for_explicit_type(self):
        frequent = SimpleNamespace(name="项目群", group="最常使用")
        primary_contains = SimpleNamespace(name="历史项目群（备注）", group="群聊")

        self.assertIsNone(
            WeChatListener._find_exact_search_result(
                {"最常使用": [frequent]}, "项目群", "group"
            )
        )
        self.assertIs(
            WeChatListener._find_exact_search_result(
                {"最常使用": [frequent]}, "项目群", None
            ),
            frequent,
        )
        self.assertIs(
            WeChatListener._find_exact_search_result(
                {"群聊": [primary_contains]}, "历史项目群", "group"
            ),
            primary_contains,
        )

    def test_search_rejects_ambiguous_exact_and_contains_results(self):
        duplicate_a = SimpleNamespace(name="项目群")
        duplicate_b = SimpleNamespace(name="项目群")
        fuzzy_a = SimpleNamespace(name="项目群通知")
        fuzzy_b = SimpleNamespace(name="历史项目群")

        self.assertIsNone(WeChatListener._find_exact_search_result(
            {"群聊": [duplicate_a, duplicate_b]}, "项目群", "group"
        ))
        self.assertIsNone(WeChatListener._find_exact_search_result(
            {"群聊": [fuzzy_a, fuzzy_b]}, "项目群", "group"
        ))

    def test_search_result_accepts_nfc_equivalent_name(self):
        result = SimpleNamespace(name="Cafe\u0301", ctrl=Mock(), group="联系人")

        self.assertIs(
            WeChatListener._find_exact_search_result(
                {"联系人": [result]}, "Caf\u00e9", "contact"
            ),
            result,
        )

    def test_wait_accepts_window_title_with_extra_information(self):
        listener = self.make_listener(["项目群"])
        hwnd, _root, _edit = self.add_chat("项目群通知")

        with patch("wx_Listener._find_window_by_title", return_value=hwnd):
            self.assertEqual(
                listener._wait_for_subwindow("项目群", 100, timeout=0.1),
                hwnd,
            )

        self.assertEqual(self.closed_windows, [])

    def test_wait_rejects_and_closes_unrelated_window_title(self):
        listener = self.make_listener(["项目群"])
        wrong_hwnd, _root, _edit = self.add_chat("运营群")

        with patch("wx_Listener._find_window_by_title", return_value=wrong_hwnd):
            with self.assertRaisesRegex(RuntimeError, "标题不匹配"):
                listener._wait_for_subwindow("项目群", 100, timeout=0.1)

        self.assertEqual(self.closed_windows, [wrong_hwnd])

    def test_open_does_not_click_again_after_a_new_window_is_observed(self):
        listener = self.make_listener(["项目群"])
        self.add_chat("项目群")

        with patch.object(
            listener,
            "_wait_for_subwindow",
            side_effect=_SubwindowOpenedButUnverified("UIA identity pending"),
        ):
            with self.assertRaisesRegex(RuntimeError, "无法打开标题匹配"):
                listener._open_verified_subwindow("项目群", 100, attempts=2)

        self.helpers[2].assert_called_once_with(self.session_items["项目群"])

    def test_wait_preserves_new_window_while_uia_identity_is_pending(self):
        listener = self.make_listener(["项目群"])
        hwnd, _root, _edit = self.add_chat("项目群")
        self.window_titles[hwnd] = ""

        with patch("wx_Listener._find_window_by_title", return_value=None):
            with patch(
                "wx_Listener._list_new_window_candidates",
                return_value=[(hwnd, "", "FutureQtWindowClass")],
            ):
                with self.assertRaises(_SubwindowOpenedButUnverified):
                    listener._wait_for_subwindow(
                        "项目群",
                        100,
                        timeout=0,
                        existing_hwnds={100},
                    )

        self.assertEqual(self.closed_windows, [])

    def test_wait_for_subwindow_stops_before_another_window_probe(self):
        listener = self.make_listener(["项目群"])
        stop_event = threading.Event()
        stop_event.set()

        with patch("wx_Listener._find_window_by_title") as find_window:
            with self.assertRaisesRegex(RuntimeError, "停止请求"):
                listener._wait_for_subwindow(
                    "项目群",
                    100,
                    timeout=30,
                    stop_event=stop_event,
                )

        find_window.assert_not_called()

    def test_wait_preserves_matching_window_while_message_view_is_pending(self):
        listener = self.make_listener(["项目群"])
        hwnd, _root, _edit = self.add_chat("项目群")
        identity = {
            "native_title": "项目群",
            "root_name": "项目群",
            "current_chat_name": "项目群",
            "ui_class": "mmui::FramelessMainWindow",
            "has_main_structure": False,
        }
        with patch("wx_Listener._find_window_by_title", return_value=None):
            with patch(
                "wx_Listener._list_new_window_candidates",
                return_value=[(hwnd, "项目群", "Qt51514QWindowIcon")],
            ):
                with patch(
                    "wx_Listener.WeChatListener._require_verified_subwindow",
                    side_effect=RuntimeError("MessageView pending"),
                ):
                    with patch(
                        "wx_Listener._match_window_identity",
                        return_value=(303, identity),
                    ):
                        with self.assertRaises(_SubwindowOpenedButUnverified):
                            listener._wait_for_subwindow(
                                "项目群",
                                100,
                                timeout=0,
                                existing_hwnds={100},
                            )

        self.assertEqual(self.closed_windows, [])

    def test_manual_connect_mode_calls_connect(self):
        with patch("wx_Listener.WX4PY_AUTO_CONNECT", False):
            listener = self.make_listener([])

        self.assertFalse(listener.wx.auto_connect)
        listener.wx.connect.assert_called_once_with()

    def test_main_window_handle_change_rebinds_uia_after_tray_restore(self):
        listener = self.make_listener([])

        def activate():
            listener.wx.window.hwnd = 101
            return True

        listener.wx.window.activate.side_effect = activate

        hwnd, root = listener._activate_main_window()

        self.assertEqual(hwnd, 101)
        self.assertIs(root, self.main_root)
        listener.wx.window.uia.bind.assert_called_once_with(101)
        self.helpers[13].assert_called_once_with(101)

    def test_main_window_is_rebound_when_wx4py_selected_a_subwindow(self):
        listener = self.make_listener([])

        with patch(
            "wx_Listener._find_actual_main_window",
            return_value=101,
        ):
            hwnd, root = listener._activate_main_window()

        self.assertEqual(hwnd, 101)
        self.assertIs(root, self.main_root)
        self.assertEqual(listener.wx.window.hwnd, 101)
        listener.wx.window.uia.bind.assert_called_once_with(101)
        self.helpers[13].assert_called_once_with(101)

    def test_main_window_native_activation_runs_after_wx4py_reports_success(self):
        listener = self.make_listener([])

        listener._activate_main_window()

        listener.wx.window.activate.assert_called_once_with()
        self.helpers[13].assert_called_once_with(100)

    def test_main_window_native_activation_recovers_from_wx4py_exception(self):
        listener = self.make_listener([])
        listener.wx.window.activate.side_effect = RuntimeError("foreground denied")

        hwnd, root = listener._activate_main_window()

        self.assertEqual(hwnd, 100)
        self.assertIs(root, self.main_root)
        self.helpers[13].assert_called_once_with(100)

    def test_outbound_only_opens_session_at_start_and_sends_in_subwindow(self):
        listener = self.make_listener([{"name": "张总", "type": "private"}])
        _hwnd, _root, edit = self.add_chat("张总")

        processor = listener.start_listening()
        self.assertTrue(processor.is_running)
        self.assertEqual(self.open_windows["张总"], 200)
        self.assertTrue(listener.send("张总", "text", "收到"))

        self.assertEqual(self.helpers[8].call_args.args, (edit, "收到"))
        self.helpers[13].assert_called_with(200)
        listener.wx.chat_window.send_to.assert_not_called()
        listener.wx.chat_window.open_chat.assert_not_called()

    def test_outbound_only_monitor_reopens_a_closed_target_window(self):
        listener = self.make_listener([{"name": "张总", "type": "private"}])
        self.add_chat("张总")
        listener.start_listening()
        session = listener._sessions["张总"]
        session.next_identity_check_at = 0
        self.open_windows.clear()
        first_title_read = True

        def get_title(hwnd):
            nonlocal first_title_read
            if first_title_read:
                first_title_read = False
                return ""
            return self.window_titles[hwnd]

        with patch("wx_Listener._get_window_title", side_effect=get_title):
            listener.process_commands(limit=0)

        self.assertEqual(self.open_windows["张总"], session.hwnd)

    def test_send_reopens_window_closed_after_session_lookup(self):
        listener = self.make_listener(["项目群"])
        _hwnd, _root, edit = self.add_chat("项目群")
        session = listener._get_or_open_session("项目群")
        self.helpers[2].reset_mock()
        self.helpers[8].reset_mock()
        self.helpers[13].reset_mock()
        first_title_read = True

        def get_title(hwnd):
            nonlocal first_title_read
            if hwnd == session.hwnd and first_title_read:
                first_title_read = False
                title = self.window_titles[hwnd]
                self._close_window(hwnd)
                return title
            if hwnd not in self.open_windows.values():
                return ""
            return self.window_titles[hwnd]

        with patch("wx_Listener._get_window_title", side_effect=get_title):
            with self.assertLogs("wx_Listener", level="WARNING") as logs:
                result = listener.send("项目群", "text", "自动恢复")

        self.assertTrue(result)
        self.assertEqual(self.open_windows["项目群"], session.hwnd)
        self.helpers[2].assert_called_once_with(self.session_items["项目群"])
        self.assertEqual(
            self.helpers[13].call_args_list,
            [call(100), call(session.hwnd)],
        )
        self.helpers[8].assert_called_once_with(
            edit,
            "自动恢复",
            before_submit=unittest.mock.ANY,
        )
        self.assertTrue(any(
            "发送前发现窗口已消失" in line for line in logs.output
        ))

    def test_send_rebinds_when_cached_input_is_missing(self):
        listener = self.make_listener(["项目群"])
        _hwnd, _root, edit = self.add_chat("项目群")
        listener._get_or_open_session("项目群")
        self.helpers[7].side_effect = [None, edit]

        with self.assertLogs("wx_Listener", level="WARNING") as logs:
            result = listener.send("项目群", "text", "重试成功")

        self.assertTrue(result)
        self.assertEqual(self.helpers[8].call_args.args, (edit, "重试成功"))
        self.assertEqual(
            listener.wx.outgoing_registry.records,
            [("项目群", "重试成功")],
        )
        self.assertTrue(any("准备重绑后重试" in line for line in logs.output))

    def test_send_rebinds_after_subwindow_activation_failure(self):
        listener = self.make_listener(["项目群"])
        _hwnd, _root, edit = self.add_chat("项目群")
        listener._get_or_open_session("项目群")
        self.helpers[13].reset_mock()
        self.helpers[13].side_effect = [False, True]

        with self.assertLogs("wx_Listener", level="WARNING"):
            result = listener.send("项目群", "text", "窗口恢复")

        self.assertTrue(result)
        self.assertEqual(self.helpers[8].call_args.args, (edit, "窗口恢复"))
        self.assertEqual(self.helpers[13].call_count, 2)

    def test_unknown_send_outcome_is_not_retried_and_keeps_echo_marker(self):
        registry = OutgoingMessageRegistry()
        listener = self.make_listener(["项目群"], registry=registry)
        self.add_chat("项目群")
        def unknown_after_submit(_edit, _content, before_submit=None):
            before_submit()
            raise _SendOutcomeUnknown("unknown")

        self.helpers[8].side_effect = unknown_after_submit

        with self.assertRaises(_SendOutcomeUnknown) as raised:
            listener.send("项目群", "text", "只发送一次")

        self.assertFalse(raised.exception.retry_safe)
        self.helpers[8].assert_called_once()
        self.assertTrue(registry.should_ignore("项目群", "只发送一次"))

    def test_safe_pre_submit_failure_cancels_echo_marker(self):
        registry = OutgoingMessageRegistry()
        listener = self.make_listener(["项目群"], registry=registry)
        self.add_chat("项目群")
        self.helpers[8].side_effect = lambda *_args, **_kwargs: False

        with self.assertRaisesRegex(RuntimeError, "发送失败"):
            listener.send("项目群", "text", "没有发出")

        self.assertEqual(self.helpers[8].call_count, 2)
        self.assertFalse(registry.should_ignore("项目群", "没有发出"))

    def test_poll_uses_runtime_id_baseline_and_ignores_registered_outgoing(self):
        received = []
        baseline = _VisibleItem("旧消息", (1,))
        listener = self.make_listener(
            ["项目群"], callback=lambda chat, data: received.append((chat, data))
        )
        _hwnd, root, _edit = self.add_chat("项目群", [baseline])
        listener.start_listening()
        session = listener._sessions["项目群"]
        session.next_scan_at = 0
        new_item = _VisibleItem("新消息", (2,))
        outgoing = _VisibleItem("机器人回复", (3,))
        self.message_items[root.message_list] = [baseline, new_item, outgoing]
        listener.wx.outgoing_registry.ignored.add(("项目群", "机器人回复"))

        listener.process_commands(limit=0)
        session.next_scan_at = 0
        listener.process_commands(limit=0)

        self.assertEqual(len(received), 1)
        self.assertEqual(received[0][0], "项目群")
        self.assertEqual(received[0][1]["content"], "新消息")
        self.assertEqual(received[0][1]["chat_type"], "group")

    def test_poll_scans_every_due_session_even_above_legacy_batch_size(self):
        received = []
        names = ["群一", "群二", "群三"]
        listener = self.make_listener(
            [{"name": name, "type": "group"} for name in names],
            callback=lambda chat, data: received.append((chat, data["content"])),
        )
        roots = {}
        for index, name in enumerate(names, 1):
            _hwnd, root, _edit = self.add_chat(
                name, [_VisibleItem("旧", (index, 0))]
            )
            roots[name] = root
        listener.start_listening()
        for index, name in enumerate(names, 1):
            session = listener._sessions[name]
            session.next_scan_at = 0
            self.message_items[roots[name].message_list].append(
                _VisibleItem(f"新{index}", (index, 1))
            )

        with patch("wx_Listener.WX4PY_BATCH_SIZE", 1):
            listener.process_commands(limit=0)

        self.assertEqual(
            received,
            [("群一", "新1"), ("群二", "新2"), ("群三", "新3")],
        )

    def test_poll_never_truncates_a_burst_to_tail_size(self):
        received = []
        baseline = _VisibleItem("旧", (1,))
        listener = self.make_listener(
            [{"name": "项目群", "type": "group"}],
            callback=lambda _chat, data: received.append(data["content"]),
        )
        _hwnd, root, _edit = self.add_chat("项目群", [baseline])
        listener.start_listening()
        session = listener._sessions["项目群"]
        session.next_scan_at = 0
        burst = [_VisibleItem(f"新{i}", (i + 1,)) for i in range(5)]
        self.message_items[root.message_list] = [baseline, *burst]

        with patch("wx_Listener.WX4PY_TAIL_SIZE", 2):
            listener.process_commands(limit=0)

        self.assertEqual(received, [f"新{i}" for i in range(5)])

    def test_poll_recovers_after_transient_empty_message_list(self):
        received = []
        baseline = _VisibleItem("旧", (1,))
        listener = self.make_listener(
            [{"name": "项目群", "type": "group"}],
            callback=lambda _chat, data: received.append(data["content"]),
        )
        _hwnd, root, _edit = self.add_chat("项目群", [baseline])
        listener.start_listening()
        session = listener._sessions["项目群"]
        session.next_scan_at = 0
        self.message_items[root.message_list] = []

        listener.process_commands(limit=0)

        self.assertEqual(session.snapshot, (baseline,))
        self.assertEqual(received, [])
        new_item = _VisibleItem("刷新期间到达", (2,))
        self.message_items[root.message_list] = [baseline, new_item]
        session.next_scan_at = 0
        listener.process_commands(limit=0)
        self.assertEqual(received, ["刷新期间到达"])

    def test_left_side_message_is_not_killed_by_outgoing_text_registry(self):
        received = []
        listener = self.make_listener(
            [{"name": "项目群", "type": "group"}],
            callback=lambda _chat, data: received.append(data["content"]),
        )
        _hwnd, root, _edit = self.add_chat("项目群")
        listener.start_listening()
        session = listener._sessions["项目群"]
        session.next_scan_at = 0
        incoming = _VisibleItem(
            "收到",
            (2,),
            control=SimpleNamespace(MessageDirection="left", GetChildren=lambda: []),
        )
        self.message_items[root.message_list] = [incoming]
        listener.wx.outgoing_registry.ignored.add(("项目群", "收到"))

        listener.process_commands(limit=0)

        self.assertEqual(received, ["收到"])

    def test_image_work_runs_after_concurrent_text_collection(self):
        order = []
        listener = self.make_listener(
            [
                {"name": "图片群", "type": "group"},
                {"name": "文本群", "type": "group"},
            ],
            callback=lambda chat, data: order.append((chat, data["content"])),
        )
        _hwnd1, image_root, _edit1 = self.add_chat("图片群")
        _hwnd2, text_root, _edit2 = self.add_chat("文本群")
        listener.start_listening()
        listener._sessions["图片群"].next_scan_at = 0
        listener._sessions["文本群"].next_scan_at = 0
        self.message_items[image_root.message_list] = [
            _VisibleItem(
                "图片",
                (1,),
                class_name="mmui::ChatBubbleItemView",
                control=SimpleNamespace(MessageDirection="left"),
                message_type="image",
            )
        ]
        self.message_items[text_root.message_list] = [_VisibleItem("及时文本", (2,))]

        def save(*_args):
            order.append(("media", "save"))
            return r"C:\WeMai\received.png"

        with patch.object(listener, "_save_received_image", side_effect=save):
            listener.process_commands(limit=0)

        self.assertEqual(order[0], ("文本群", "及时文本"))
        self.assertEqual(order[1], ("media", "save"))

    def test_failed_callback_row_is_retried_after_snapshot_advances(self):
        calls = []

        def callback(_chat, data):
            calls.append(data["content"])
            if len(calls) == 1:
                raise RuntimeError("sqlite busy")

        listener = self.make_listener(
            [{"name": "项目群", "type": "group"}], callback=callback
        )
        _hwnd, root, _edit = self.add_chat("项目群")
        listener.start_listening()
        session = listener._sessions["项目群"]
        session.next_scan_at = 0
        self.message_items[root.message_list] = [_VisibleItem("不能丢", (2,))]

        with self.assertLogs("wx_Listener", level="ERROR"):
            listener.process_commands(limit=0)
        session.next_scan_at = 0
        listener.process_commands(limit=0)

        self.assertEqual(calls, ["不能丢", "不能丢"])
        self.assertEqual(session.recovered_items, [])

    def test_poll_accepts_reused_runtime_id_when_visible_count_grows(self):
        received = []
        baseline = _VisibleItem("重复消息", (1,))
        listener = self.make_listener(
            ["项目群"], callback=lambda chat, data: received.append((chat, data))
        )
        _hwnd, root, _edit = self.add_chat("项目群", [baseline])
        listener.start_listening()
        session = listener._sessions["项目群"]
        session.next_scan_at = 0
        repeated = _VisibleItem("重复消息", (1,))
        self.message_items[root.message_list] = [baseline, repeated]

        listener.process_commands(limit=0)
        session.next_scan_at = 0
        listener.process_commands(limit=0)

        self.assertEqual(len(received), 1)
        self.assertEqual(received[0][1]["content"], "重复消息")

    def test_poll_accepts_reused_image_id_when_visible_count_is_stable(self):
        received = []
        old_image = _VisibleItem(
            "图片",
            (1,),
            class_name="mmui::ChatBubbleItemView",
            control=SimpleNamespace(MessageDirection="left"),
            message_type="image",
        )
        anchor = _VisibleItem("锚点", (2,))
        listener = self.make_listener(
            ["项目群"], callback=lambda chat, data: received.append((chat, data))
        )
        _hwnd, root, _edit = self.add_chat("项目群", [old_image, anchor])
        listener.start_listening()
        session = listener._sessions["项目群"]
        session.next_scan_at = 0
        repeated_image = _VisibleItem(
            "图片",
            (1,),
            class_name="mmui::ChatBubbleItemView",
            control=SimpleNamespace(MessageDirection="left"),
            message_type="image",
        )
        self.message_items[root.message_list] = [anchor, repeated_image]

        with patch.object(
            listener,
            "_save_received_image",
            return_value=r"C:\WeMai\wxauto文件\reused.png",
        ) as save:
            listener.process_commands(limit=0)

        save.assert_called_once_with(session, repeated_image, "left")
        self.assertEqual(len(received), 1)
        self.assertEqual(received[0][1]["type"], "image")
        self.assertLessEqual(len(session.seen), session.visible_count)

    def test_failed_image_remains_pending_until_retry_fallback(self):
        received = []
        listener = self.make_listener(
            ["项目群"], callback=lambda chat, data: received.append((chat, data))
        )
        _hwnd, root, _edit = self.add_chat("项目群")
        listener.start_listening()
        session = listener._sessions["项目群"]
        image = _VisibleItem(
            "图片",
            (8,),
            class_name="mmui::ChatBubbleItemView",
            control=SimpleNamespace(MessageDirection="left"),
            message_type="image",
        )
        self.message_items[root.message_list] = [image]

        with patch.object(
            listener,
            "_save_received_image",
            side_effect=RuntimeError("preview unavailable"),
        ) as save:
            for _ in range(3):
                session.next_scan_at = 0
                listener.process_commands(limit=0)

        self.assertEqual(save.call_count, 3)
        self.assertEqual(len(received), 1)
        self.assertEqual(received[0][1]["content"], "[图片]")
        self.assertEqual(received[0][1]["type"], "text")
        self.assertEqual(session.media_failures, {})

    def test_received_image_is_saved_and_forwarded_as_image_event(self):
        received = []
        listener = self.make_listener(
            ["项目群"], callback=lambda chat, data: received.append((chat, data))
        )
        _hwnd, root, _edit = self.add_chat("项目群")
        listener.start_listening()
        session = listener._sessions["项目群"]
        session.next_scan_at = 0
        image_control = SimpleNamespace(MessageDirection="left")
        image = _VisibleItem(
            "图片",
            (2,),
            class_name="mmui::ChatBubbleItemView",
            control=image_control,
            message_type="image",
        )
        self.message_items[root.message_list] = [image]

        with patch.object(
            listener,
            "_save_received_image",
            return_value=r"C:\WeMai\wxauto文件\received.png",
        ) as save:
            listener.process_commands(limit=0)

        save.assert_called_once_with(session, image, "left")
        self.assertEqual(received[0][0], "项目群")
        self.assertEqual(received[0][1]["type"], "image")
        self.assertEqual(
            received[0][1]["content"],
            r"C:\WeMai\wxauto文件\received.png",
        )

    def test_image_save_flow_opens_saves_persists_and_closes_preview(self):
        listener = self.make_listener(["项目群"], callback=Mock())
        self.add_chat("项目群")
        listener.start_listening()
        session = listener._sessions["项目群"]
        image_control = SimpleNamespace(MessageDirection="left")
        image = _VisibleItem(
            "图片",
            (22,),
            class_name="mmui::ChatBubbleItemView",
            control=image_control,
            message_type="image",
        )
        preview_root = object()
        toolbar = object()
        saved_path = r"C:\WeMai\wxauto文件\saved.png"
        with tempfile.NamedTemporaryFile(delete=False) as stream:
            source_path = stream.name

        try:
            with patch.object(listener, "_activate_session_window"):
                with patch.object(listener, "_require_verified_subwindow"):
                    with patch(
                        "wx_Listener._get_window_process_id",
                        side_effect=(10, 10),
                    ):
                        with patch(
                            "wx_Listener._list_top_level_windows_by_pid",
                            return_value=[(session.hwnd, "项目群", "ChatWindow")],
                        ):
                            with patch("wx_Listener._roll_control_into_view"):
                                with patch(
                                    "wx_Listener._click_image_message",
                                    return_value=True,
                                ) as double_click:
                                    with patch(
                                        "wx_Listener._wait_for_image_preview",
                                        return_value=(300, preview_root, toolbar),
                                    ) as wait_preview:
                                        with patch(
                                            "wx_Listener._acquire_image_from_preview",
                                            return_value=(source_path, True),
                                        ) as acquire:
                                            with patch(
                                                "wx_Listener._persist_received_image",
                                                return_value=saved_path,
                                            ) as persist:
                                                with patch(
                                                    "wx_Listener._close_preview_window"
                                                ) as close_preview:
                                                    with patch(
                                                        "wx_Listener.os.path.getsize",
                                                        return_value=123,
                                                    ):
                                                        result = listener._save_received_image(
                                                            session,
                                                            image,
                                                            "left",
                                                        )
        finally:
            if os.path.exists(source_path):
                os.unlink(source_path)

        self.assertEqual(result, saved_path)
        double_click.assert_called_once_with(
            image_control,
            "left",
            strategy=0,
        )
        wait_preview.assert_called_once()
        acquire.assert_called_once()
        persist.assert_called_once_with(source_path)
        close_preview.assert_called_once_with(300, preview_root)
        self.assertFalse(os.path.exists(source_path))

    def test_self_sent_image_is_not_forwarded_back_to_maibot(self):
        callback = Mock()
        listener = self.make_listener(["项目群"], callback=callback)
        _hwnd, root, _edit = self.add_chat("项目群")
        listener.start_listening()
        session = listener._sessions["项目群"]
        session.next_scan_at = 0
        image = _VisibleItem(
            "图片",
            (2,),
            class_name="mmui::ChatBubbleItemView",
            control=SimpleNamespace(MessageDirection="right"),
            message_type="image",
        )
        self.message_items[root.message_list] = [image]

        with patch.object(listener, "_save_received_image") as save:
            listener.process_commands(limit=0)

        save.assert_not_called()
        callback.assert_not_called()

    def test_unknown_image_direction_is_not_dropped_by_content_only_registry(self):
        received = []
        listener = self.make_listener(
            ["项目群"], callback=lambda chat, data: received.append((chat, data))
        )
        _hwnd, root, _edit = self.add_chat("项目群")
        listener.start_listening()
        session = listener._sessions["项目群"]
        session.next_scan_at = 0
        image = _VisibleItem(
            "图片",
            (16,),
            class_name="mmui::ChatBubbleItemView",
            control=SimpleNamespace(MessageDirection="", GetChildren=lambda: []),
            message_type="image",
        )
        self.message_items[root.message_list] = [image]
        listener.wx.outgoing_registry.ignored.add(("项目群", "图片"))

        with patch.object(
            listener,
            "_save_received_image",
            return_value=r"C:\WeMai\wxauto文件\unknown-direction.png",
        ) as save:
            listener.process_commands(limit=0)

        save.assert_called_once_with(session, image, "left")
        self.assertEqual(len(received), 1)
        self.assertEqual(received[0][1]["type"], "image")

    def test_poll_title_verification_success_is_silent(self):
        listener = self.make_listener(["项目群"], callback=Mock())
        self.add_chat("项目群")
        listener.start_listening()
        session = listener._sessions["项目群"]
        session.next_scan_at = 0

        with patch("wx_Listener.logger.info") as info:
            with patch("wx_Listener.logger.debug") as debug:
                listener.process_commands(limit=0)

        for log in (info, debug):
            self.assertFalse(any(
                call_args.args
                and "独立窗口标题复核通过" in str(call_args.args[0])
                for call_args in log.call_args_list
            ))

    def test_poll_reopens_a_vanished_independent_window(self):
        listener = self.make_listener(["项目群"], callback=Mock())
        self.add_chat("项目群")
        listener.start_listening()
        session = listener._sessions["项目群"]
        session.next_scan_at = 0
        self.open_windows.clear()
        first_title_read = True

        def get_title(hwnd):
            nonlocal first_title_read
            if first_title_read:
                first_title_read = False
                return ""
            return self.window_titles[hwnd]

        with patch("wx_Listener._get_window_title", side_effect=get_title):
            listener.process_commands(limit=0)

        self.assertEqual(self.open_windows["项目群"], session.hwnd)
        self.assertEqual(session.fail_count, 0)
        self.assertEqual(self.helpers[2].call_count, 2)

    def test_text_and_multiple_files_use_independent_window_input(self):
        listener = self.make_listener(["项目群"])
        _hwnd, _root, edit = self.add_chat("项目群")
        paths = [r"C:\data\a.pdf", r"C:\data\b.pdf"]

        self.assertTrue(listener.send("项目群", "text", "你好"))
        self.assertTrue(listener.send("项目群", "file", paths))

        self.assertEqual(self.helpers[8].call_args.args, (edit, "你好"))
        self.assertEqual(self.helpers[9].call_args.args, (edit, paths))
        self.assertEqual(listener.wx.outgoing_registry.records, [
            ("项目群", "你好"),
            ("项目群", "a.pdf"),
            ("项目群", "b.pdf"),
        ])
        listener.wx.chat_window.send_file_to.assert_not_called()

    def test_multiple_files_keep_one_echo_marker_per_file(self):
        registry = OutgoingMessageRegistry()
        listener = self.make_listener(["项目群"], registry=registry)
        self.add_chat("项目群")

        self.assertTrue(listener.send(
            "项目群",
            "file",
            [r"C:\data\a.pdf", r"C:\data\b.pdf"],
        ))

        self.assertTrue(registry.should_ignore("项目群", "a.pdf"))
        self.assertTrue(registry.should_ignore("项目群", "b.pdf"))
        self.assertFalse(registry.should_ignore("项目群", "a.pdf"))

    def test_message_event_is_converted_to_wemai_format(self):
        received = []
        listener = self.make_listener(
            [{"name": "张总", "type": "private"}, "未标注聊天"],
            callback=lambda chat, data: received.append((chat, data)),
        )

        listener._handle_event(SimpleNamespace(
            group="张总", content="你好", timestamp=123.5
        ))
        listener._handle_event(SimpleNamespace(
            group="未标注聊天", content="群消息", timestamp=124.0
        ))

        self.assertEqual(received[0], ("张总", {
            "chat": "张总",
            "chat_type": "private",
            "sender": "张总",
            "type": "text",
            "content": "你好",
            "timestamp": 123.5,
        }))
        self.assertEqual(received[1][1]["chat_type"], "group")

    def test_command_queue_dispatches_send_on_owner_thread(self):
        command = UICommand(
            action="send", args=("项目群", "text", "hello"), timeout=15
        )
        commands = Mock()
        commands.get_nowait.side_effect = [command, queue.Empty]
        listener = self.make_listener(["项目群"], commands=commands)
        self.add_chat("项目群")

        listener.process_commands()

        self.assertTrue(command.future.result())
        self.helpers[8].assert_called_once()
        commands.task_done.assert_called_once_with()

    def test_command_queue_runs_different_chat_windows_in_parallel(self):
        first = UICommand(
            action="send", args=("项目群", "text", "a"), timeout=15
        )
        second = UICommand(
            action="send", args=("张总", "text", "b"), timeout=15
        )
        commands = Mock()
        commands.get_nowait.side_effect = [first, second]
        listener = self.make_listener(["项目群", "张总"], commands=commands)
        both_started = threading.Event()
        release = threading.Event()
        starts = []
        starts_lock = threading.Lock()

        def send(receiver, _kind, _data):
            with starts_lock:
                starts.append(receiver)
                if len(starts) == 2:
                    both_started.set()
            release.wait(1)
            return True

        try:
            with patch.object(listener, "_send_from_worker", side_effect=send):
                listener.process_commands()
                listener.process_commands()
                self.assertTrue(both_started.wait(1))
                self.assertCountEqual(starts, ["项目群", "张总"])
                release.set()
                self.assertTrue(first.future.result(timeout=1))
                self.assertTrue(second.future.result(timeout=1))
        finally:
            release.set()
            listener.close()

        self.assertEqual(commands.task_done.call_count, 2)

    def test_close_stops_status_and_disconnects_once(self):
        listener = self.make_listener(["项目群"], callback=Mock())
        self.add_chat("项目群")
        processor = listener.start_listening()

        listener.close()
        listener.close()

        self.assertFalse(processor.is_running)
        listener.wx.disconnect.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
