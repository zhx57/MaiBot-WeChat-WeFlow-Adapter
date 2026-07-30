import os
import tempfile
import unittest
from unittest.mock import patch

from weflow_listener import WeFlowListener
from wx_Processor import MessageProcessor


class InboundFileTests(unittest.TestCase):
    def setUp(self):
        self.processor = MessageProcessor.__new__(MessageProcessor)

    def test_file_becomes_lightweight_name_marker(self):
        segment = self.processor._inbound_segment(
            r"C:\sender\temporary-name.tmp",
            "file",
            file_name=r"C:\sender\report.txt",
        )
        self.assertEqual(segment.type, "text")
        self.assertEqual(segment.data, "[文件消息：report.txt]")

    def test_file_marker_does_not_require_local_file(self):
        segment = self.processor._inbound_segment("missing", "file", file_name="large.bin")
        self.assertEqual(segment.type, "text")
        self.assertEqual(segment.data, "[文件消息：large.bin]")

    def test_saved_weflow_file_is_preserved_as_file_segment(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "report.txt")
            with open(path, "wb") as stream:
                stream.write(b"hello")
            with patch("wx_Processor.IMAGE_SAVE_DIR", directory):
                segment = self.processor._inbound_segment(path, "file", "report.txt")
            self.assertEqual(segment.type, "file")
            self.assertEqual(segment.data, os.path.abspath(path))

    def test_weflow_appmsg_file_uses_real_title(self):
        content = (
            "<msg><appmsg><title>唐诗三百首.docx</title><type>6</type>"
            "<appattach><fileext>docx</fileext></appattach></appmsg></msg>"
        )
        self.assertEqual(
            WeFlowListener._file_name_from_message({"content": content}),
            "唐诗三百首.docx",
        )

    def test_weflow_link_is_not_treated_as_file(self):
        content = "<msg><appmsg><title>网页标题</title><type>5</type></appmsg></msg>"
        self.assertEqual(WeFlowListener._file_name_from_message({}, content), "")


if __name__ == "__main__":
    unittest.main()
