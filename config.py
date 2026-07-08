"""Akasha WeChat WeFlow 适配器配置模型。"""

from __future__ import annotations

from typing import Literal

from maibot_sdk import Field, PluginConfigBase

from .constants import (
    DEFAULT_BUFFER_SECONDS,
    DEFAULT_CONTENT_DEDUPE_TTL_SEC,
    DEFAULT_IMAGE_CAPTION_PROMPT,
    DEFAULT_IMAGE_CAPTION_TIMEOUT,
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

    enabled: bool = Field(
        default=True,
        description="是否启用这个微信适配器插件",
        json_schema_extra={"label": "启用插件", "hint": "关掉后插件不工作"},
    )
    config_version: str = Field(
        default="1.0.0",
        description="配置文件版本，用于兼容性",
        json_schema_extra={"label": "配置版本号", "hint": "一般不用改"},
    )


class WeFlowSection(PluginConfigBase):
    """WeFlow 连接配置。"""

    __ui_label__ = "WeFlow 连接"
    __ui_icon__ = "link"
    __ui_order__ = 1

    base_url: str = Field(
        default=DEFAULT_WEFLOW_BASE_URL,
        description="WeFlow API 根地址",
        json_schema_extra={
            "label": "WeFlow 服务地址",
            "hint": "WeFlow 软件运行的地址，本机用一般是 http://127.0.0.1:5031",
        },
    )
    access_token: str = Field(
        default="",
        description="WeFlow 访问令牌",
        json_schema_extra={
            "placeholder": "填入 WeFlow 的 access_token",
            "label": "WeFlow 访问令牌",
            "hint": "在 WeFlow 软件的「设置 → API 服务」里复制",
        },
    )
    send_api: str = Field(
        default=DEFAULT_WEFLOW_SEND_API,
        description="WeFlow 发送消息 API 地址",
        json_schema_extra={
            "label": "发送消息接口地址",
            "hint": "用 WeFlow API 发消息时的地址，用 UIA 发送时无需关心",
        },
    )
    send_method: Literal["uia", "weflow_api"] = Field(
        default="uia",
        description="发送方式：uia=Windows UI 自动化；weflow_api=WeFlow REST API",
        json_schema_extra={
            "label": "消息发送方式",
            "hint": "uia=用 Windows 自动化操作微信窗口发消息（推荐）；weflow_api=用 WeFlow 接口发消息（需第三方扩展支持）",
        },
    )
    request_timeout: float = Field(
        default=DEFAULT_REQUEST_TIMEOUT,
        description="HTTP 请求超时（秒）",
        json_schema_extra={
            "label": "请求超时秒数",
            "hint": "和 WeFlow 通信的超时时间",
        },
    )


class BotSection(PluginConfigBase):
    """机器人配置。"""

    __ui_label__ = "微信机器人"
    __ui_icon__ = "smart_toy"
    __ui_order__ = 2

    nicknames: list[str] = Field(
        default_factory=list,
        description="机器人微信昵称列表",
        json_schema_extra={
            "label": "机器人微信昵称",
            "hint": "机器人微信号设置的昵称，用于识别群里@机器人和过滤自己发的消息。可填多个",
        },
    )
    wxid: str = Field(
        default="",
        description="机器人自身 wxid（self_id）",
        json_schema_extra={
            "label": "机器人 wxid",
            "hint": "机器人微信号的唯一ID（wxid_xxx），在 WeFlow 里能看到。用于标识机器人自己",
        },
    )


class BridgeSection(PluginConfigBase):
    """桥接配置。"""

    __ui_label__ = "消息处理"
    __ui_icon__ = "settings"
    __ui_order__ = 3

    buffer_seconds: int = Field(
        default=DEFAULT_BUFFER_SECONDS,
        description="消息缓冲秒数",
        json_schema_extra={
            "label": "消息合并等待秒数",
            "hint": "群里连续发多条消息时，等几秒合并成一条再交给 AI，避免刷屏",
        },
    )
    group_reply_mode: Literal["mention", "all", "batch"] = Field(
        default="mention",
        description="群聊回复模式：mention/all/batch",
        json_schema_extra={
            "label": "群聊回复模式",
            "hint": "mention=只在被@时回复；all=群里每条消息都回复；batch=把群里消息合并成一条处理",
        },
    )
    reconnect_delay_sec: int = Field(
        default=DEFAULT_RECONNECT_DELAY_SEC,
        description="SSE 断线重连间隔（秒）",
        json_schema_extra={
            "label": "断线重连等待秒数",
            "hint": "和 WeFlow 断开后等多久再重连",
        },
    )
    history_filter_enabled: bool = Field(
        default=True,
        description="是否丢弃启动前的历史消息",
        json_schema_extra={
            "label": "忽略启动前的历史消息",
            "hint": "开启后，插件启动前就收到的消息会被丢弃，避免 AI 回复旧消息",
        },
    )


class ImageCaptionSection(PluginConfigBase):
    """图片理解配置。"""

    __ui_label__ = "图片理解"
    __ui_icon__ = "image"
    __ui_order__ = 4

    enabled: bool = Field(
        default=False,
        description="是否启用图片理解。启用后会调用 MaiBot 已配置的视觉模型描述图片内容",
        json_schema_extra={"label": "启用图片理解", "hint": "开启后，机器人能看懂图片。需先在 MaiBot「模型配置」页配置一个支持看图的模型（如 gpt-4o、qwen-vl-plus）"},
    )
    prompt: str = Field(
        default=DEFAULT_IMAGE_CAPTION_PROMPT,
        description="让视觉模型描述图片时使用的提示词",
        json_schema_extra={"label": "图片描述提示词", "hint": "告诉 AI 怎么描述图片，一般不用改"},
    )
    download_images: bool = Field(
        default=True,
        description="是否把图片以原图形式一起转发给 MaiBot",
        json_schema_extra={"label": "下载并转发原图", "hint": "开启后图片会同时以原图发给 AI，AI 看得更清楚但更耗流量"},
    )
    attachments_dir: str = Field(
        default=WECHAT_IMAGES_SUBDIR,
        description="下载的图片保存到哪个子目录（相对插件数据目录）",
        json_schema_extra={"label": "图片缓存目录", "hint": "下载的图片临时存放位置，一般不用改"},
    )
    timeout: int = Field(
        default=DEFAULT_IMAGE_CAPTION_TIMEOUT,
        description="等待视觉模型返回描述的最长时间（秒）",
        json_schema_extra={"label": "描述超时秒数", "hint": "超过这个时间还没描述完就放弃，避免卡住消息处理"},
    )


class ChatSection(PluginConfigBase):
    """聊天名单配置。"""

    __ui_label__ = "聊天名单"
    __ui_icon__ = "forum"
    __ui_order__ = 5

    group_list_type: Literal["whitelist", "blacklist"] = Field(
        default="whitelist",
        description="群聊名单类型：whitelist/blacklist",
        json_schema_extra={
            "label": "群聊名单模式",
            "hint": "whitelist=只响应名单里的群；blacklist=屏蔽名单里的群",
        },
    )
    group_list: list[str] = Field(
        default_factory=list,
        description="群聊名单（群名或群ID）",
        json_schema_extra={
            "label": "群聊名单",
            "hint": "填写群名或群ID，每行一个",
        },
    )
    private_list_type: Literal["whitelist", "blacklist"] = Field(
        default="whitelist",
        description="私聊名单类型",
        json_schema_extra={
            "label": "私聊名单模式",
            "hint": "whitelist=只响应名单里的私聊；blacklist=屏蔽名单里的私聊",
        },
    )
    private_list: list[str] = Field(
        default_factory=list,
        description="私聊名单",
        json_schema_extra={
            "label": "私聊名单",
            "hint": "填写对方昵称或wxid，每行一个",
        },
    )
    ban_user_id: list[str] = Field(
        default_factory=list,
        description="封禁用户ID列表",
        json_schema_extra={
            "label": "封禁用户列表",
            "hint": "这些用户的消息会被完全忽略",
        },
    )


class FiltersSection(PluginConfigBase):
    """过滤配置。"""

    __ui_label__ = "过滤设置"
    __ui_icon__ = "filter_alt"
    __ui_order__ = 6

    ignore_self_message: bool = Field(
        default=True,
        description="是否忽略机器人自身消息",
        json_schema_extra={
            "label": "忽略机器人自己的消息",
            "hint": "开启后机器人自己发的消息不会再次触发回复",
        },
    )
    content_dedupe_ttl_sec: int = Field(
        default=DEFAULT_CONTENT_DEDUPE_TTL_SEC,
        description="内容去重 TTL（秒）",
        json_schema_extra={
            "label": "内容去重时间（秒）",
            "hint": "防止 AI 回复被 WeFlow 回流再次触发。这段时间内相同内容只处理一次",
        },
    )
    regex_filter_enabled: bool = Field(
        default=False,
        description="是否启用正则过滤",
        json_schema_extra={
            "label": "启用正则过滤",
            "hint": "开启后可用正则表达式过滤消息",
        },
    )
    regex_filter_mode: Literal["blacklist", "whitelist"] = Field(
        default="blacklist",
        description="正则过滤模式",
        json_schema_extra={
            "label": "正则过滤模式",
            "hint": "blacklist=匹配的消息被屏蔽；whitelist=只有匹配的消息才处理",
        },
    )
    regex_filter_patterns: list[str] = Field(
        default_factory=list,
        description="正则过滤模式列表",
        json_schema_extra={
            "label": "正则表达式列表",
            "hint": "每行一个正则表达式",
        },
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

    def validate_runtime_config(self, logger) -> bool:
        """启动前校验关键配置，缺失时输出中文告警。返回 True 表示可启动。"""
        if not self.plugin.enabled:
            return True  # 未启用无需校验
        ok = True
        if not self.weflow.access_token:
            logger.warning("⚠️ WeFlow 访问令牌未填写，请在 WeFlow「设置 → API 服务」中获取后填入 [weflow].access_token")
            ok = False
        if not self.bot.wxid:
            logger.warning("⚠️ 机器人 wxid 未填写，请填入 [bot].wxid（机器人微信号的 wxid_xxx）")
            ok = False
        if not self.bot.nicknames:
            logger.warning("⚠️ 机器人微信昵称未填写，建议填入 [bot].nicknames 以便群聊@检测和自回复过滤")
            # 昵称缺失不阻断，仅提示
        if ok:
            logger.info("配置校验通过：WeFlow 令牌、bot.wxid 已配置")
        return ok
