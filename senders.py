"""消息发送器抽象基类与工厂。

根据 ``config.weflow.send_method`` 选择 ``UiaSender``（Windows UI 自动化）
或 ``WeFlowApiSender``（WeFlow REST API）。为避免非 Windows 环境或缺依赖时
模块加载失败，具体发送器在 ``create_sender`` 内延迟导入。
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from pathlib import Path

from .id_mapping import ContactRef


class BaseSender(ABC):
    """消息发送器抽象基类。

    所有方法为 ``async``；UIA 实现内部用 ``asyncio.to_thread`` 包装同步逻辑。
    """

    def __init__(self, logger: logging.Logger) -> None:
        self.logger = logger

    @abstractmethod
    async def send_text(self, contact: ContactRef, text: str) -> None:
        ...

    @abstractmethod
    async def send_image(self, contact: ContactRef, image_path: Path) -> None:
        ...

    @abstractmethod
    async def close(self) -> None:
        ...


def create_sender(config, logger: logging.Logger) -> BaseSender:
    """根据 ``config.weflow.send_method`` 返回对应发送器。

    延迟 import 具体实现，避免非 Windows / 缺依赖时本模块 import 失败。
    """

    method = config.weflow.send_method
    if method == "weflow_api":
        from .weflow_api_sender import WeFlowApiSender

        return WeFlowApiSender(
            send_api=config.weflow.send_api,
            access_token=config.weflow.access_token,
            request_timeout=config.weflow.request_timeout,
            logger=logger,
        )
    # 默认 uia
    from .uia_sender import UiaSender

    return UiaSender(logger=logger)
