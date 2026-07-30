import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from wx_Processor import MessageProcessor


class LocalFileDeliveryTest(unittest.TestCase):
    def test_file_url_is_limited_to_attachments_directory(self):
        with tempfile.TemporaryDirectory() as root_text:
            root = Path(root_text)
            allowed = root / "attachments" / "task"
            allowed.mkdir(parents=True)
            source = allowed / "result.txt"
            source.write_text("ok", encoding="utf-8")
            outside = root / "outside.txt"
            outside.write_text("no", encoding="utf-8")
            with patch("wx_Processor.LOCAL_FILE_DELIVERY_ROOT", str(root)):
                self.assertEqual(
                    MessageProcessor._normalize_file({"url": source.as_uri()}),
                    str(source),
                )
                with self.assertRaisesRegex(ValueError, "超出可信交付目录"):
                    MessageProcessor._normalize_file({"url": outside.as_uri()})

    def test_receipt_is_written_atomically(self):
        with tempfile.TemporaryDirectory() as root_text:
            root = Path(root_text)
            delivery_id = f"codex-{'c' * 32}-001"
            with patch("wx_Processor.LOCAL_FILE_DELIVERY_ROOT", str(root)):
                MessageProcessor._write_local_file_receipt(
                    delivery_id,
                    success=True,
                )
            receipt = root / "receipts" / f"{delivery_id}.json"
            payload = json.loads(receipt.read_text(encoding="utf-8"))
            self.assertTrue(payload["success"])
            self.assertEqual(payload["delivery_id"], delivery_id)


if __name__ == "__main__":
    unittest.main()
