import unittest

from chat_name_utils import normalize_chat_name


class NormalizeChatNameTest(unittest.TestCase):
    def test_special_chat_names(self):
        cases = {
            "🎮游戏群🎮": "🎮游戏群🎮",
            "测试群\ufe0f": "测试群",
            "A\u200bB": "AB",
            "正常群名": "正常群名",
            "群[1].*": "群[1].*",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(normalize_chat_name(raw), expected)

    def test_nfc_and_control_characters(self):
        self.assertEqual(normalize_chat_name("Cafe\u0301\x00"), "Café")

    def test_preserves_semantic_format_characters_and_trims_outer_space(self):
        self.assertEqual(normalize_chat_name("  正常群名\n"), "正常群名")
        self.assertEqual(normalize_chat_name("👩\u200d💻讨论组"), "👩\u200d💻讨论组")


if __name__ == "__main__":
    unittest.main()
