"""Akasha WeChat WeFlow 适配器共享常量。"""

from __future__ import annotations

# ============ 消息网关标识 ============
# 双工消息网关名称（@MessageGateway name）
GATEWAY_NAME = "wechat_weflow_gateway"
# 适配器声明的平台与协议
PLATFORM = "wechat"
PROTOCOL = "weflow"
# 网关连接作用域
SCOPE = "primary"

# ============ WeFlow 默认连接参数 ============
DEFAULT_WEFLOW_BASE_URL = "http://127.0.0.1:5031"
DEFAULT_WEFLOW_SEND_API = "http://127.0.0.1:5031/api/v1/message"
DEFAULT_REQUEST_TIMEOUT = 30.0

# ============ 桥接默认参数 ============
DEFAULT_BUFFER_SECONDS = 5
DEFAULT_RECONNECT_DELAY_SEC = 10

# ============ Ollama 图片描述默认参数 ============
DEFAULT_OLLAMA_BASE_URL = "http://127.0.0.1:61000"
DEFAULT_OLLAMA_TIMEOUT = 60

# ============ 图片描述（OpenAI 兼容）默认参数 ============
DEFAULT_IMAGE_CAPTION_MODEL = "llava:7b"
DEFAULT_IMAGE_CAPTION_API_BASE = "https://api.moonshot.cn/v1"
DEFAULT_IMAGE_CAPTION_PROMPT = "请用中文简短描述这张图片的内容"

# ============ 去重缓存默认参数 ============
# 内容去重 TTL（防止 AI 回复回流）
DEFAULT_CONTENT_DEDUPE_TTL_SEC = 120
DEFAULT_CONTENT_DEDUPE_MAXSIZE = 10000
# rawid 去重 TTL
DEFAULT_RAWID_DEDUPE_TTL_SEC = 300

# ============ 运行时数据目录与文件名 ============
DATA_DIR = "data"
ID_MAP_FILENAME = "id_contact_map.json"
WECHAT_IMAGES_SUBDIR = "wechat_images"

# ============ WeFlow API 路径 ============
SSE_PUSH_PATH = "/api/v1/push/messages"
MESSAGES_API_PATH = "/api/v1/messages"

# ============ 微信消息类型 ============
# 语音消息 type
WECHAT_VOICE_TYPE = 34
