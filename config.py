"""Akasha WeChat WeFlow 适配器配置模型。"""

from __future__ import annotations

from typing import Literal

from maibot_sdk import Field, PluginConfigBase

from .constants import (
    DEFAULT_BUFFER_SECONDS,
    DEFAULT_CONTENT_DEDUPE_TTL_SEC,
    DEFAULT_IMAGE_CAPTION_API_BASE,
    DEFAULT_IMAGE_CAPTION_MODEL,
    DEFAULT_IMAGE_CAPTION_PROMPT,
    DEFAULT_OLLAMA_BASE_URL,
    DEFAULT_OLLAMA_TIMEOUT,
    DEFAULT_RECONNECT_DELAY_SEC,
    DEFAULT_REQUEST_TIMEOUT,
    DEFAULT_WEFLOW_BASE_URL,
    DEFAULT_WEFLOW_SEND_API,
    WECHAT_IMAGES_SUBDIR,
)


class PluginSection(PluginConfigBase):
    """插件基础配置。"""

    __ui_label__ = "插件"
    __ui_icon__ = "power_settings_new"
    __ui_order__ = 0

    enabled: bool = Field(default=True, description="是否启用适配器")
    config_version: str = Field(default="1.0.0", description="配置版本号")


class WeFlowSection(PluginConfigBase):
    """WeFlow 连接配置。"""

    __ui_label__ = "WeFlow 连接"
    __ui_icon__ = "link"
    __ui_order__ = 1

    base_url: str = Field(default=DEFAULT_WEFLOW_BASE_URL, description="WeFlow API 根地址")
    access_token: str = Field(
        default="",
        description="WeFlow Access Token",
        json_schema_extra={"placeholder": "填入 WeFlow 的 access_token"},
    )
    send_api: str = Field(default=DEFAULT_WEFLOW_SEND_API, description="WeFlow 发送消息 API 地址")
    send_method: Literal["uia", "weflow_api"] = Field(
        default="uia",
        description="发送方式：uia=Windows UI 自动化（推荐，官方 WeFlow 无发送 API）；"
        "weflow_api=WeFlow REST API（需兼容 WeFlow 协议的第三方扩展）",
    )
    request_timeout: float = Field(default=DEFAULT_REQUEST_TIMEOUT, description="HTTP 请求超时（秒）")


class BotSection(PluginConfigBase):
    """机器人配置。"""

    __ui_label__ = "机器人"
    __ui_icon__ = "smart_toy"
    __ui_order__ = 2

    nicknames: list[str] = Field(
        default_factory=list, description="机器人微信昵称列表，群聊 @ 检测与自回复过滤用"
    )
    wxid: str = Field(default="", description="机器人自身 wxid（self_id），自回复过滤用")


class BridgeSection(PluginConfigBase):
    """桥接配置。"""

    __ui_label__ = "桥接设置"
    __ui_icon__ = "settings"
    __ui_order__ = 3

    buffer_seconds: int = Field(
        default=DEFAULT_BUFFER_SECONDS, description="消息缓冲秒数，多条消息合并后推送"
    )
    group_reply_mode: Literal["mention", "all", "batch"] = Field(
        default="mention", description="群聊回复模式：mention=仅@回复，all=全部回复，batch=批处理"
    )
    reconnect_delay_sec: int = Field(
        default=DEFAULT_RECONNECT_DELAY_SEC, description="SSE 断线重连间隔（秒）"
    )
    history_filter_enabled: bool = Field(
        default=True, description="是否丢弃启动前的历史消息"
    )


class ImageCaptionSection(PluginConfigBase):
    """图片描述配置。"""

    __ui_label__ = "图片描述"
    __ui_icon__ = "image"
    __ui_order__ = 4

    provider: Literal["none", "ollama", "openai"] = Field(
        default="none", description="图片描述服务：none=不描述（仅转发图片），ollama，openai"
    )
    model: str = Field(default=DEFAULT_IMAGE_CAPTION_MODEL, description="视觉模型名，如 llava:7b")
    api_key: str = Field(
        default="",
        description="OpenAI 兼容模式 API Key",
        json_schema_extra={"placeholder": "sk-..."},
    )
    api_base: str = Field(
        default=DEFAULT_IMAGE_CAPTION_API_BASE, description="OpenAI 兼容 API 根地址"
    )
    prompt: str = Field(default=DEFAULT_IMAGE_CAPTION_PROMPT, description="视觉模型提示词")
    ollama_base_url: str = Field(
        default=DEFAULT_OLLAMA_BASE_URL, description="Ollama API 根地址"
    )
    ollama_timeout: int = Field(
        default=DEFAULT_OLLAMA_TIMEOUT, description="Ollama 请求超时（秒）"
    )
    download_images: bool = Field(
        default=True, description="是否下载图片并以 image 消息段转发给 MaiBot"
    )
    attachments_dir: str = Field(
        default=WECHAT_IMAGES_SUBDIR, description="图片保存子目录（相对插件 data 目录）"
    )


class ChatSection(PluginConfigBase):
    """聊天名单配置。"""

    __ui_label__ = "聊天名单"
    __ui_icon__ = "forum"
    __ui_order__ = 5

    group_list_type: Literal["whitelist", "blacklist"] = Field(
        default="whitelist", description="群聊名单类型"
    )
    group_list: list[str] = Field(
        default_factory=list, description="群聊名单（群名或群ID）"
    )
    private_list_type: Literal["whitelist", "blacklist"] = Field(
        default="whitelist", description="私聊名单类型"
    )
    private_list: list[str] = Field(default_factory=list, description="私聊名单")
    ban_user_id: list[str] = Field(default_factory=list, description="封禁用户ID列表")


class FiltersSection(PluginConfigBase):
    """过滤配置。"""

    __ui_label__ = "过滤设置"
    __ui_icon__ = "filter_alt"
    __ui_order__ = 6

    ignore_self_message: bool = Field(default=True, description="是否忽略机器人自身消息")
    content_dedupe_ttl_sec: int = Field(
        default=DEFAULT_CONTENT_DEDUPE_TTL_SEC, description="内容去重 TTL（秒），防止 AI 回复回流"
    )
    regex_filter_enabled: bool = Field(default=False, description="是否启用正则过滤")
    regex_filter_mode: Literal["blacklist", "whitelist"] = Field(
        default="blacklist", description="正则过滤模式"
    )
    regex_filter_patterns: list[str] = Field(
        default_factory=list, description="正则过滤模式列表"
    )


class WeChatWeFlowConfig(PluginConfigBase):
    """Akasha WeChat WeFlow 适配器完整配置。"""

    __ui_label__ = "Akasha WeChat WeFlow 适配器"

    plugin: PluginSection = Field(default_factory=PluginSection)
    weflow: WeFlowSection = Field(default_factory=WeFlowSection)
    bot: BotSection = Field(default_factory=BotSection)
    bridge: BridgeSection = Field(default_factory=BridgeSection)
    image_caption: ImageCaptionSection = Field(default_factory=ImageCaptionSection)
    chat: ChatSection = Field(default_factory=ChatSection)
    filters: FiltersSection = Field(default_factory=FiltersSection)
