"""聊天名称的 Unicode 安全标准化与比较工具。"""

import unicodedata

_IGNORABLE_UIA_CHARACTERS = {"\u200b", "\u2060", "\ufeff"}


def normalize_chat_name(value):
    """把配置、WeFlow 和 UIA 返回的名称转换为可稳定比较的键。

    微信控件文本偶尔混入变体选择符、零宽字符和控制字符；这些字符不可见，
    却会导致字典命中和精确搜索失败，因此只在比较键中移除，显示名称保持原样。
    """
    if value is None:
        return ""
    value = unicodedata.normalize("NFC", str(value))
    normalized = "".join(
        char for char in value
        if not ("\ufe00" <= char <= "\ufe0f")
        and char not in _IGNORABLE_UIA_CHARACTERS
        and unicodedata.category(char) != "Cc"
    )
    return normalized.strip()


def chat_names_equal(left, right):
    """比较两个聊天名称，忽略微信 UI 注入的不可见字符。"""
    return normalize_chat_name(left) == normalize_chat_name(right)
