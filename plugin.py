"""Akasha WeChat WeFlow 适配器主插件入口。

将配置模型、ID 映射、过滤器、发送器、图片描述、SSE 传输层、编解码器、桥接核心
与运行时状态组装为 MaiBot 原生插件：声明双工 ``@MessageGateway``，实现三个
生命周期方法（``on_load`` / ``on_unload`` / ``on_config_update``）并暴露
``create_plugin`` 工厂。

实现模式严格对齐 MaiBot 官方 NapCat 适配器。
"""

from __future__ import annotations

import os
import tempfile
from base64 import b64decode
from pathlib import Path
from typing import Any, ClassVar, Dict, Optional

from maibot_sdk import (
    CONFIG_RELOAD_SCOPE_SELF,
    MaiBotPlugin,
    MessageGateway,
    PluginConfigBase,
)

from .bridge import WeChatBridge
from .codecs.outbound import iter_send_actions, resolve_contact
from .config import WeChatWeFlowConfig
from .constants import GATEWAY_NAME, ID_MAP_FILENAME, PLATFORM, PROTOCOL, SCOPE
from .id_mapping import ContactMapper


class WeChatWeFlowAdapterPlugin(MaiBotPlugin):
    """Akasha WeChat WeFlow 适配器插件。

    持有桥接核心 ``WeChatBridge`` 与双向 ID 映射 ``ContactMapper``，通过
    ``@MessageGateway`` 声明双工网关：入站由 bridge 内部 SSE 驱动并经
    ``ctx.gateway.route_message`` 注入 Host；出站由 Host 调用
    ``handle_outbound`` 经 sender 发送到微信。
    """

    config_model: ClassVar[type[PluginConfigBase] | None] = WeChatWeFlowConfig

    def __init__(self) -> None:
        super().__init__()
        self._bridge: Optional[WeChatBridge] = None
        self._contact_mapper: Optional[ContactMapper] = None
        self._data_dir: Optional[Path] = None

    # ================================================================
    # 生命周期
    # ================================================================

    async def on_load(self) -> None:
        """Runner 注入 ctx 后调用：初始化数据目录、映射与桥接。"""

        # 1. 数据目录：使用 ctx.paths.data_dir（Host 管理的持久化目录）
        data_dir = self.ctx.paths.data_dir
        data_dir.mkdir(parents=True, exist_ok=True)
        self._data_dir = data_dir

        # 2. 双向 ID 映射：加载持久化映射，供出站反查发送目标
        contact_mapper = ContactMapper()
        contact_mapper.load(data_dir / ID_MAP_FILENAME)
        self._contact_mapper = contact_mapper

        # 3. 桥接核心：内部创建 sender 并存储在 self._state.sender
        self._bridge = WeChatBridge(
            config=self.config,
            ctx=self.ctx,
            contact_mapper=contact_mapper,
            logger=self.ctx.logger,
            data_dir=data_dir,
        )

        # 4. 配置自校验
        if self.config.plugin.enabled:
            if not self.config.validate_runtime_config(self.ctx.logger):
                self.ctx.logger.error("配置校验未通过，桥接不启动，请按上方告警修正配置后重载插件")
                return

        # 5. 按配置启用
        if self.config.plugin.enabled:
            await self._bridge.start()
        else:
            self.ctx.logger.info("WeChat WeFlow 适配器未启用（plugin.enabled=False）")

    async def on_unload(self) -> None:
        """卸载前调用：停桥接、持久化映射、关发送器、网关置离线。"""

        # 1. 停止桥接（停 SSE、取消缓冲定时器、清去重缓存）
        if self._bridge is not None:
            try:
                await self._bridge.stop()
            except Exception:
                self.ctx.logger.exception("停止桥接时发生异常")

        # 2. 持久化 ID 映射，便于重启后出站反查
        if self._contact_mapper is not None and self._data_dir is not None:
            try:
                self._contact_mapper.save(self._data_dir / ID_MAP_FILENAME)
            except Exception:
                self.ctx.logger.exception("持久化 ID 映射失败")

        # 3. 关闭发送器资源（HTTP 会话 / UIA 句柄等）
        if self._bridge is not None:
            try:
                await self._bridge._state.sender.close()
            except Exception:
                self.ctx.logger.exception("关闭发送器时发生异常")

        # 4. 确保网关标记为离线
        try:
            await self.ctx.gateway.update_state(
                gateway_name=GATEWAY_NAME,
                ready=False,
                platform=PLATFORM,
                account_id=self.config.bot.wxid,
                scope=SCOPE,
                metadata={"protocol": PROTOCOL},
            )
        except Exception:
            self.ctx.logger.exception("上报网关离线状态失败")

    async def on_config_update(
        self, scope: str, config_data: Dict[str, Any], version: str
    ) -> None:
        """配置热重载：仅响应自身作用域更新，显式更新配置后重启桥接。"""

        if scope != CONFIG_RELOAD_SCOPE_SELF:
            return

        # self.config 在 scope="self" 时已由 SDK 自动更新，无需手动重读
        self.ctx.logger.info("WeChat WeFlow 适配器配置已更新: version=%s", version)

        # 重启桥接以应用新配置（安全且正确）
        await self._restart_bridge()

    async def _restart_bridge(self) -> None:
        """重启桥接：停旧桥接 → 持久化映射 → 重建映射与桥接 → 启动。"""

        # 1. 停止旧桥接
        if self._bridge is not None:
            try:
                await self._bridge.stop()
            except Exception:
                self.ctx.logger.exception("重启时停止旧桥接发生异常")
            # 关闭旧发送器资源
            try:
                await self._bridge._state.sender.close()
            except Exception:
                self.ctx.logger.exception("重启时关闭旧发送器发生异常")

        # 2. 持久化旧 contact_mapper（若有），复用已注册的联系人
        if self._contact_mapper is not None and self._data_dir is not None:
            try:
                self._contact_mapper.save(self._data_dir / ID_MAP_FILENAME)
            except Exception:
                self.ctx.logger.exception("重启前持久化 ID 映射失败")

        # 3. 重建 ContactMapper 并加载持久化映射
        if self._data_dir is None:
            # on_load 尚未执行过的兜底（理论上不会发生）
            self._data_dir = self.ctx.paths.data_dir
            self._data_dir.mkdir(parents=True, exist_ok=True)

        contact_mapper = ContactMapper()
        contact_mapper.load(self._data_dir / ID_MAP_FILENAME)
        self._contact_mapper = contact_mapper

        # 4. 重建桥接核心
        self._bridge = WeChatBridge(
            config=self.config,
            ctx=self.ctx,
            contact_mapper=contact_mapper,
            logger=self.ctx.logger,
            data_dir=self._data_dir,
        )

        # 5. 按配置启用
        if self.config.plugin.enabled:
            await self._bridge.start()
        else:
            self.ctx.logger.info(
                "WeChat WeFlow 适配器未启用，已停止桥接（plugin.enabled=False）"
            )

    # ================================================================
    # 出站消息网关
    # ================================================================

    @MessageGateway(
        route_type="duplex",
        name=GATEWAY_NAME,
        platform=PLATFORM,
        protocol=PROTOCOL,
        description="微信 WeFlow 双工消息网关适配器",
    )
    async def handle_outbound(
        self,
        message: Dict[str, Any],
        route: Optional[Dict[str, Any]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """处理 Host 出站消息并发送到微信。

        从 ``message["raw_message"]`` 遍历 Seg，按类型调用 sender：
        - text → ``send_text``
        - image / emoji → base64 解码到临时文件后 ``send_image``，发送后删除
        - face → 转为文本 ``"[表情]"`` 后 ``send_text``
        """

        del route, metadata, kwargs  # 当前未使用

        # 未初始化（on_load 未执行或已卸载）
        if self._bridge is None or self._contact_mapper is None:
            return {"success": False, "error": "适配器尚未初始化", "metadata": {}}

        # 反查发送目标联系人
        sender = self._bridge._state.sender
        contact = resolve_contact(message, self._contact_mapper, self.ctx.logger)
        if contact is None:
            return {
                "success": False,
                "error": "无法解析发送目标联系人",
                "metadata": {},
            }

        try:
            raw_message = message.get("raw_message") or []
            for kind, payload in iter_send_actions(raw_message):
                if kind == "text":
                    await sender.send_text(contact, payload)
                elif kind == "image":
                    # base64 解码到临时文件，发送后删除
                    image_bytes = b64decode(payload)
                    fd, tmp_path = tempfile.mkstemp(
                        suffix=".jpg", prefix="wechat_send_"
                    )
                    try:
                        with os.fdopen(fd, "wb") as f:
                            f.write(image_bytes)
                        await sender.send_image(contact, Path(tmp_path))
                    finally:
                        try:
                            os.unlink(tmp_path)
                        except OSError:
                            pass
            return {"success": True, "external_message_id": None, "metadata": {}}
        except Exception as exc:
            self.ctx.logger.exception("出站消息发送失败")
            return {"success": False, "error": str(exc), "metadata": {}}


def create_plugin() -> WeChatWeFlowAdapterPlugin:
    """创建 WeChat WeFlow 适配器插件实例。"""

    return WeChatWeFlowAdapterPlugin()
