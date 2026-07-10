"""Unicode-safe chat name comparison helpers."""

import unicodedata


def normalize_chat_name(value):
    """Return a stable key for chat names reported by config and UIA."""
    if value is None:
        return ""
    value = unicodedata.normalize("NFC", str(value))
    return "".join(
        char for char in value
        if not ("\ufe00" <= char <= "\ufe0f")
        and char not in {"\u200b", "\u200c", "\u200d", "\u2060", "\ufeff"}
        and unicodedata.category(char) not in {"Cc", "Cf"}
    )


def chat_names_equal(left, right):
    return normalize_chat_name(left) == normalize_chat_name(right)
