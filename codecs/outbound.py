"""出站消息编解码：MessageDict → 发送动作。

- ``resolve_contact``：从 MessageDict 反查 ``ContactRef``（UIA 用 display_name，
  WeFlow API 用 session_id）。
- ``iter_send_actions``：遍历 ``raw_message`` Seg 列表，产出 ``(kind, payload)``
  供 sender 依次发送。

Seg ``data`` 字段类型重载：text 的 data 是 str，at 的 data 是 dict，
image 的 data 通常为空串而二进制在 ``binary_data_base64``。务必按 ``type`` 区分。
"""

from __future__ import annotations

import logging
from typing import Any, Iterator, Optional

from ..id_mapping import ContactMapper, ContactRef


def resolve_contact(
    message: dict[str, Any],
    contact_mapper: ContactMapper,
    logger: logging.Logger,
) -> Optional[ContactRef]:
    """从 MessageDict 反查 ContactRef。

    优先 ``message_info.group_info.group_id``；其次 ``message_info.user_info.user_id``；
    也兼容 ``additional_config.platform_io_target_group_id`` /
    ``platform_io_target_user_id``。找不到返回 None（logger.warning）。
    """

    message_info = message.get("message_info") or {}
    group_info = message_info.get("group_info") or {}
    user_info = message_info.get("user_info") or {}

    # 候选 ID 列表：(来源标签, entity_id)，按优先级追加
    candidates: list[tuple[str, str]] = []

    group_id = group_info.get("group_id")
    if group_id:
        candidates.append(("group_id", str(group_id)))

    user_id = user_info.get("user_id")
    if user_id:
        candidates.append(("user_id", str(user_id)))

    additional_config = (message.get("additional_config") or {}) | (
        message_info.get("additional_config") or {}
    )
    target_group_id = additional_config.get("platform_io_target_group_id")
    if target_group_id:
        candidates.append(("platform_io_target_group_id", str(target_group_id)))
    target_user_id = additional_config.get("platform_io_target_user_id")
    if target_user_id:
        candidates.append(("platform_io_target_user_id", str(target_user_id)))

    if not candidates:
        logger.warning("出站消息缺少可识别的目标 ID（group_id/user_id/平台目标）")
        return None

    for label, entity_id in candidates:
        ref = contact_mapper.resolve(entity_id)
        if ref is not None:
            return ref
        logger.warning(f"出站消息 {label}={entity_id} 无法反查 ContactRef")

    return None


def iter_send_actions(
    raw_message: list[dict[str, Any]],
) -> Iterator[tuple[str, Any]]:
    """遍历 raw_message Seg 列表，产出 (kind, payload)。

    - ``{"type":"text","data":"..."}`` → ``("text", str(data))``
    - ``{"type":"image",...}`` / ``{"type":"emoji",...}``（含 ``binary_data_base64``）
      → ``("image", binary_data_base64_str)``；二进制字段缺失则跳过
    - ``{"type":"face",...}`` → ``("text", "[表情]")``
    - ``at`` / ``reply`` / ``record`` / ``video`` / 未知类型 → 跳过

    text 段的 data 可能是 str；若不是 str 则 ``str(data)``。
    """

    for seg in raw_message or []:
        if not isinstance(seg, dict):
            continue
        seg_type = seg.get("type")
        if seg_type == "text":
            data = seg.get("data", "")
            if not isinstance(data, str):
                data = str(data)
            yield ("text", data)
        elif seg_type in ("image", "emoji"):
            b64 = seg.get("binary_data_base64")
            if not b64:
                continue
            yield ("image", b64)
        elif seg_type == "face":
            yield ("text", "[表情]")
        # at / reply / record / video / 未知类型：跳过
