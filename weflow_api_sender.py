"""WeFlow REST API 发送器（aiohttp 全异步）。

.. note::
    WeFlow 官方文档（UserDataIsSafeFromUsers）仅提供**读取类** HTTP API
    （messages/sessions/contacts/group-members/media/sns），**未声明发送消息接口**。
    本发送器面向兼容 WeFlow 协议的第三方扩展或未来版本，假设存在
    ``POST {send_api}`` 端点：文本走 JSON body ``{"to","content","type":"text"}``，
    图片走 ``aiohttp.FormData``（multipart/form-data）。
    若使用官方原版 WeFlow，请将 ``send_method`` 配置为 ``"uia"``（默认值）。

POST ``{send_api}`` 发送文本或图片，Header ``Authorization: Bearer {token}``。
非 200 状态码抛 ``RuntimeError``。
"""

from __future__ import annotations

import logging
from pathlib import Path

import aiohttp

from .id_mapping import ContactRef
from .senders import BaseSender

# 官方 WeFlow 不提供发送消息接口。404/405 通常意味着用户在官方原版
# WeFlow 上误选了 weflow_api 发送方式，需改回 uia。
_NOT_SUPPORTED_HINT = (
    "官方原版 WeFlow 不提供发送消息接口（仅读取类 API）。"
    "请将配置 weflow.send_method 改回 \"uia\"（Windows UI 自动化）后再试。"
)


class WeFlowApiSender(BaseSender):
    """基于 WeFlow REST API 的异步消息发送器。"""

    def __init__(
        self,
        send_api: str,
        access_token: str,
        request_timeout: float,
        logger: logging.Logger,
    ) -> None:
        super().__init__(logger)
        self._send_api = send_api
        self._access_token = access_token
        self._timeout = request_timeout
        self._session: aiohttp.ClientSession | None = None

    async def _ensure_session(self) -> aiohttp.ClientSession:
        """懒创建复用的 ClientSession（已关闭则重建）。"""

        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=self._timeout)
            self._session = aiohttp.ClientSession(timeout=timeout)
        return self._session

    @staticmethod
    def _format_error(action: str, status: int, body: str) -> RuntimeError:
        """构造带友好提示的发送失败异常。404/405 附上「改用 uia」指引。"""

        msg = f"WeFlow {action}失败: HTTP {status} {body[:200]}"
        if status in (404, 405):
            msg = f"{msg} | {_NOT_SUPPORTED_HINT}"
        return RuntimeError(msg)

    async def send_text(self, contact: ContactRef, text: str) -> None:
        """发送文本：JSON body ``{"to","content","type":"text"}``。非 200 抛 RuntimeError。"""

        session = await self._ensure_session()
        headers = {"Authorization": f"Bearer {self._access_token}"}
        payload = {"to": contact.session_id, "content": text, "type": "text"}
        try:
            async with session.post(
                self._send_api, json=payload, headers=headers
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    raise self._format_error("发送文本", resp.status, body)
        except aiohttp.ClientError as e:
            raise RuntimeError(f"WeFlow 发送文本请求异常: {e}") from e
        self.logger.info(
            f"[WeFlowSender] 已发送文本至 {contact.session_id}: {text[:50]}"
        )

    async def send_image(self, contact: ContactRef, image_path: Path) -> None:
        """发送图片：multipart ``image`` 文件 + ``to``/``type`` 字段。非 200 抛 RuntimeError。"""

        session = await self._ensure_session()
        headers = {"Authorization": f"Bearer {self._access_token}"}
        form = aiohttp.FormData()
        form.add_field("to", contact.session_id)
        form.add_field("type", "image")
        # 文件需在请求期间保持打开：FormData 持有文件对象引用，上传完成后再关闭。
        with open(image_path, "rb") as f:
            form.add_field(
                "image",
                f,
                filename=image_path.name,
                content_type="application/octet-stream",
            )
            try:
                async with session.post(
                    self._send_api, data=form, headers=headers
                ) as resp:
                    if resp.status != 200:
                        body = await resp.text()
                        raise self._format_error("发送图片", resp.status, body)
            except aiohttp.ClientError as e:
                raise RuntimeError(f"WeFlow 发送图片请求异常: {e}") from e
        self.logger.info(
            f"[WeFlowSender] 已发送图片至 {contact.session_id}: {image_path.name}"
        )

    async def close(self) -> None:
        """关闭 ClientSession。"""

        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None
