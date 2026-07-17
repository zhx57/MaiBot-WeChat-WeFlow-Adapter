"""Unicode-safe chat name comparison helpers."""

import unicodedata


_IGNORABLE_UIA_CHARACTERS = {"\u200b", "\u2060", "\ufeff"}


def normalize_chat_name(value):
    """Return a stable key for chat names reported by config and UIA."""
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
    return normalize_chat_name(left) == normalize_chat_name(right)
