import io
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from weflow_listener import WeFlowListener
from wx_Processor import MessageProcessor


def _unavailable_avatar_bytes():
    """生成与 WeFlow 占位图相同差异哈希的无损测试图片。"""
    reference = int(
        "080030007200721030240c6218d131806504c00090000e14b2f233420c2c2000",
        16,
    )
    bits = [(reference >> (255 - index)) & 1 for index in range(256)]
    small = Image.new("L", (17, 16))
    index = 0
    for y in range(16):
        values = [128]
        for _x in range(16):
            values.append(values[-1] + (-1 if bits[index] else 1))
            index += 1
        low, high = min(values), max(values)
        scaled = [int((value - low) * 200 / max(1, high - low) + 20) for value in values]
        for x, value in enumerate(scaled):
            small.putpixel((x, y), value)
    image = small.resize((120, 120), Image.Resampling.NEAREST)
    stream = io.BytesIO()
    image.save(stream, format="PNG")
    return stream.getvalue()


class AvatarCacheTest(unittest.TestCase):
    def setUp(self):
        self.processor = MessageProcessor.__new__(MessageProcessor)
        self.processor.platform = "wx"

    def test_group_message_avatar_only_targets_sender(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch("wx_Processor.MAIBOT_DATA_DIR", directory):
                paths = self.processor._avatar_paths("user-1", "group-1")
        self.assertEqual(len(paths), 1)
        self.assertEqual(paths[0][1], "user-1")

    def test_group_avatar_uses_separate_maibot_namespace(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch("wx_Processor.MAIBOT_DATA_DIR", directory):
                paths = self.processor._group_avatar_paths("group-1")
        self.assertEqual(len(paths), 1)
        self.assertEqual(paths[0][1], "group_group-1")

    def test_group_avatar_cache_writes_only_group_target(self):
        with tempfile.TemporaryDirectory() as directory:
            avatar_dir = Path(directory) / "avatar" / "wx"
            self.processor._download_avatar = lambda _url: (b"group-image", ".jpg")
            self.processor._is_weflow_unavailable_avatar = lambda _raw: False
            with patch("wx_Processor.MAIBOT_DATA_DIR", directory):
                changed = self.processor._cache_avatar(
                    "https://example/group.jpg",
                    "",
                    group_id="group-1",
                    target_type="group",
                )
            self.assertTrue(changed)
            self.assertEqual(
                (avatar_dir / "group_group-1.jpg").read_bytes(),
                b"group-image",
            )

    def test_existing_wrong_avatar_is_replaced(self):
        with tempfile.TemporaryDirectory() as directory:
            avatar_dir = Path(directory) / "avatar" / "wx"
            avatar_dir.mkdir(parents=True)
            target = avatar_dir / "user-1.jpg"
            target.write_bytes(b"old-wrong-image")
            self.processor._download_avatar = lambda _url: (b"new-image", ".jpg")
            with patch("wx_Processor.MAIBOT_DATA_DIR", directory):
                changed = self.processor._cache_avatar(
                    "https://wx.qlogo.cn/new.jpg", "user-1", group_id="group-1"
                )
            self.assertTrue(changed)
            self.assertEqual(target.read_bytes(), b"new-image")
            self.assertFalse((avatar_dir / "group_group-1.jpg").exists())

    def test_weflow_unavailable_avatar_is_detected_without_matching_normal_image(self):
        placeholder = _unavailable_avatar_bytes()
        normal = io.BytesIO()
        Image.new("RGB", (120, 120), "black").save(normal, format="PNG")

        self.assertTrue(self.processor._is_weflow_unavailable_avatar(placeholder))
        self.assertFalse(self.processor._is_weflow_unavailable_avatar(normal.getvalue()))

    def test_unavailable_avatar_preserves_good_cache_and_removes_old_placeholder(self):
        placeholder = _unavailable_avatar_bytes()
        with tempfile.TemporaryDirectory() as directory:
            avatar_dir = Path(directory) / "avatar" / "wx"
            avatar_dir.mkdir(parents=True)
            good = avatar_dir / "good-user.jpg"
            stale = avatar_dir / "stale-user.png"
            good.write_bytes(b"existing-good-avatar")
            stale.write_bytes(placeholder)
            self.processor._download_avatar = lambda _url: (placeholder, ".png")
            with patch("wx_Processor.MAIBOT_DATA_DIR", directory):
                good_handled = self.processor._cache_avatar(
                    "https://example/good-placeholder.png", "good-user", group_id="group-1"
                )
                stale_handled = self.processor._cache_avatar(
                    "https://example/stale-placeholder.png", "stale-user", group_id="group-1"
                )

            self.assertTrue(good_handled)
            self.assertTrue(stale_handled)
            self.assertEqual(good.read_bytes(), b"existing-good-avatar")
            self.assertFalse(stale.exists())

    def test_group_member_cache_miss_is_background_only(self):
        class CapturingExecutor:
            def __init__(self):
                self.calls = []

            def submit(self, callback, *args):
                self.calls.append((callback, args))

        listener = WeFlowListener.__new__(WeFlowListener)
        listener._group_members_cache = {}
        listener._group_members_cache_lock = threading.RLock()
        listener._group_members_refreshing = set()
        listener._metadata_executor = CapturingExecutor()
        listener.stop_event = threading.Event()

        first = listener._group_member_metadata(
            "room@chatroom", "wxid_member"
        )
        second = listener._group_member_metadata(
            "room@chatroom", "wxid_member"
        )

        self.assertEqual(first, {})
        self.assertEqual(second, {})
        self.assertEqual(len(listener._metadata_executor.calls), 1)
        callback, args = listener._metadata_executor.calls[0]
        self.assertEqual(callback, listener._refresh_group_members)
        self.assertEqual(args, ("room@chatroom",))

    def test_group_member_cache_hit_never_schedules_refresh(self):
        listener = WeFlowListener.__new__(WeFlowListener)
        listener._group_members_cache = {
            "room@chatroom": (
                float("inf"),
                {
                    "wxid_member": {
                        "nickname": "成员昵称",
                        "avatarUrl": "https://example/avatar.jpg",
                    }
                },
            )
        }
        listener._group_members_cache_lock = threading.RLock()
        listener._group_members_refreshing = set()
        listener._metadata_executor = None
        listener.stop_event = threading.Event()

        result = listener._group_member_metadata(
            "room@chatroom", "wxid_member"
        )

        self.assertEqual(result["nickname"], "成员昵称")
        self.assertEqual(result["avatarUrl"], "https://example/avatar.jpg")


if __name__ == "__main__":
    unittest.main()
