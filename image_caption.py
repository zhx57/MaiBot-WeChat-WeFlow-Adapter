"""微信图片下载与视觉模型描述（复用 MaiBot Host 已配置的 VLM）。

对应 spec「图片识别与转发」：
- ``download_wechat_image`` —— 从 WeFlow REST API 取图并落盘
- ``caption_image`` —— 调 MaiBot Host 已配置的视觉模型生成描述
- ``image_to_base64`` / ``sha256_of_file`` —— 构造 image Seg 所需的旁路字段
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import time
from pathlib import Path
from typing import Any, Optional

import aiohttp

from .constants import MESSAGES_API_PATH

log = logging.getLogger(__name__)

# Content-Type → 扩展名映射
_CONTENT_TYPE_EXT = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
}

# 描述失败时的占位符
_CAPTION_FALLBACK = "（图片内容无法描述）"


async def download_wechat_image(
    session: aiohttp.ClientSession,
    base_url: str,
    access_token: str,
    session_id: str,
    save_dir: Path,
) -> Optional[Path]:
    """从 WeFlow REST API 获取最新图片并保存到本地。

    GET ``{base_url}{MESSAGES_API_PATH}?access_token={token}&talker={session_id}&media=true&limit=3``，
    解析返回 JSON 列表，找 ``mediaType=='image'`` 的项，拼接 ``mediaUrl + access_token`` 下载，
    按 Content-Type 选扩展名保存到 ``save_dir/wechat_{int(time.time()*1000)}.{ext}``。
    失败返回 None。``save_dir`` 不存在时自动创建。
    """

    base = base_url.rstrip("/")
    url = (
        f"{base}{MESSAGES_API_PATH}"
        f"?access_token={access_token}&talker={session_id}&media=true&limit=3"
    )

    try:
        async with session.get(url) as resp:
            if resp.status != 200:
                log.error("WeFlow 消息API: HTTP %s", resp.status)
                return None
            payload = await resp.json()
    except Exception as e:
        log.error("WeFlow 消息API 请求异常：%s", e)
        return None

    # 防御性解析：可能是 list 或 {"messages":[...]} / {"data":[...]}
    if isinstance(payload, list):
        messages = payload
    elif isinstance(payload, dict):
        messages = payload.get("messages")
        if messages is None:
            messages = payload.get("data", [])
        if not isinstance(messages, list):
            messages = []
    else:
        messages = []

    for msg in messages:
        if not isinstance(msg, dict):
            continue
        if msg.get("mediaType") != "image":
            continue
        media_url = msg.get("mediaUrl")
        if not media_url:
            continue

        sep = "&" if "?" in media_url else "?"
        dl_url = f"{media_url}{sep}access_token={access_token}"

        try:
            async with session.get(dl_url) as img_resp:
                if img_resp.status != 200:
                    log.warning("微信图片下载失败：HTTP %s", img_resp.status)
                    continue
                content_type = img_resp.headers.get("Content-Type", "")
                image_bytes = await img_resp.read()
        except Exception as e:
            log.warning("微信图片下载异常：%s", e)
            continue

        ext = _ext_for_content_type(content_type)
        try:
            save_dir.mkdir(parents=True, exist_ok=True)
            save_path = save_dir / f"wechat_{int(time.time() * 1000)}{ext}"
            save_path.write_bytes(image_bytes)
            log.info("微信图片已保存：%s", save_path)
            return save_path
        except Exception as e:
            log.error("微信图片落盘失败：%s", e)
            return None

    log.warning("消息列表无图片 mediaUrl (talker=%s)", session_id)
    return None


def _ext_for_content_type(content_type: str) -> str:
    """按 Content-Type 选扩展名，默认 .jpg。"""

    ct = (content_type or "").split(";")[0].strip().lower()
    return _CONTENT_TYPE_EXT.get(ct, ".jpg")


async def caption_image(
    session: aiohttp.ClientSession,
    image_path: Path,
    prompt: str,
    timeout: int,
    llm: Optional[Any] = None,
) -> str:
    """对图片生成文字描述（复用 MaiBot Host 已配置的 VLM）。

    通过 ``ctx.llm.generate()`` 调用 Host 在「模型配置」页配置的视觉模型，
    无需在插件内重复填写 provider/model/api_key 等字段。
    需传入 ``llm``（即 ``ctx.llm``）。失败返回占位符 ``（图片内容无法描述）``，
    不抛异常。
    """

    if llm is None:
        log.warning("未传入 ctx.llm，无法描述图片")
        return _CAPTION_FALLBACK

    try:
        img_b64 = image_to_base64(image_path)
    except Exception as e:
        log.warning("图片读取失败（%s）：%s", image_path, e)
        return _CAPTION_FALLBACK

    caption = await _caption_via_maibot(llm, img_b64, prompt, timeout)
    return caption or _CAPTION_FALLBACK


async def _caption_via_maibot(
    llm: Any,
    img_b64: str,
    prompt: str,
    timeout: int,
) -> Optional[str]:
    """通过 MaiBot Host 的 ``ctx.llm.generate()`` 调用已配置视觉模型。

    复用 Host 在「模型配置」页配置的 api_providers / 模型任务，无需在插件重复填写
    api_key/api_base。消息采用 OpenAI vision 兼容格式（``content`` 列表含 ``text``
    与 ``image_url`` 段）。
    """

    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"},
                },
            ],
        }
    ]
    try:
        result = await asyncio.wait_for(
            llm.generate(prompt=messages, model=""),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        log.warning("maibot 图片描述超时（%ss）", timeout)
        return None
    except Exception as e:
        log.warning("maibot 图片描述失败：%s", e)
        return None

    if not isinstance(result, dict) or not result.get("success"):
        log.warning("maibot 图片描述返回失败：%s", result)
        return None
    caption = str(result.get("response", "")).strip()
    if caption:
        used = result.get("model", "") or "host-default"
        log.info("图片描述（maibot/%s）：%s", used, caption[:80])
    return caption or None


def image_to_base64(path: Path) -> str:
    """读文件返回 base64 字符串。"""

    data = Path(path).read_bytes()
    return base64.b64encode(data).decode("utf-8")


def sha256_of_file(path: Path) -> str:
    """返回文件内容的 sha256 十六进制。"""

    h = hashlib.sha256()
    with open(path, "rb") as f:
        # 分块读取，避免大图一次性占用内存
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()
