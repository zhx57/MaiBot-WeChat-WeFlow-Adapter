"""修复微信数据库文本中不完整的 UTF-16 代理字符。"""

from __future__ import annotations

import re
from typing import Any

_SURROGATE_PATTERN = re.compile("[\ud800-\udfff]")


def sanitize_unicode_text(value: str) -> tuple[str, int]:
    """保留正常 Unicode，并把不完整代理字符替换为标准替代字符。"""
    matches = _SURROGATE_PATTERN.findall(value)
    if not matches:
        return value, 0
    repaired = value.encode("utf-16-le", "surrogatepass").decode(
        "utf-16-le", "replace"
    )
    return repaired, len(matches)


def sanitize_unicode(value: Any) -> tuple[Any, int]:
    """递归清洗 JSON 风格数据，返回清洗结果和处理的代理单元数。"""
    if isinstance(value, str):
        return sanitize_unicode_text(value)
    if isinstance(value, dict):
        result = {}
        total = 0
        for key, item in value.items():
            clean_key, key_count = sanitize_unicode(key)
            clean_item, item_count = sanitize_unicode(item)
            result[clean_key] = clean_item
            total += key_count + item_count
        return result, total
    if isinstance(value, list):
        result = []
        total = 0
        for item in value:
            clean_item, count = sanitize_unicode(item)
            result.append(clean_item)
            total += count
        return result, total
    if isinstance(value, tuple):
        result = []
        total = 0
        for item in value:
            clean_item, count = sanitize_unicode(item)
            result.append(clean_item)
            total += count
        return tuple(result), total
    return value, 0
