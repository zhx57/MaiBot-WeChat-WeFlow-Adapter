"""WeMai 配置模块，从 .env 加载配置并支持环境变量覆盖。"""

import ipaddress
import json
import logging
import os
from typing import Optional

# WeMai 统一管理日志；阻止底层 wx4py 在工作目录额外创建 wx4py.log。
os.environ["WECHAT_LOG_FILE"] = os.devnull
os.environ["WECHAT_SEND_AUDIT_LOG_FILE"] = os.devnull
try:
    from dotenv import load_dotenv
except ImportError:
    def load_dotenv(*_args, **_kwargs):
        return False

# 加载.env文件
load_dotenv()

# 微信监听配置
def _parse_list(value: Optional[str], default: list[str] = None) -> list[str]:
    """解析逗号分隔的字符串为列表"""
    if not value:
        return default or []
    if value.lstrip().startswith('['):
        parsed = json.loads(value)
        if not isinstance(parsed, list):
            raise ValueError('聊天列表 JSON 必须是数组')
        result = []
        seen = set()
        for item in parsed:
            if isinstance(item, str):
                key = item.strip()
                normalized = key
            elif isinstance(item, dict):
                key = str(item.get('name', '')).strip()
                normalized = {'name': key, 'type': item.get('type')}
            else:
                raise ValueError('聊天列表项必须是字符串或对象')
            if key and key not in seen:
                seen.add(key)
                result.append(normalized)
        return result
    return list(dict.fromkeys(item.strip() for item in value.split(',') if item.strip()))

def _parse_bool(value: Optional[str], default: bool = False) -> bool:
    """解析字符串为布尔值"""
    if not value:
        return default
    normalized = value.strip().lower()
    if normalized in ('true', 'yes', '1', 't', 'y', 'on'):
        return True
    if normalized in ('false', 'no', '0', 'f', 'n', 'off'):
        return False
    raise ValueError(f"布尔配置值无效: {value!r}")

# WeFlow 自己负责黑白名单，WeMai 不再维护第二份聊天过滤配置。
WX_TARGET_CHATS = []

# WeFlow HTTP API 配置。Token 在服务启动时校验。
WEFLOW_API_URL = os.getenv('WEFLOW_API_URL', 'http://127.0.0.1:5031').strip().rstrip('/')
WEFLOW_API_TOKEN = os.getenv('WEFLOW_API_TOKEN', '').strip()
WEFLOW_PUSH_ENABLED = True

# 独立窗口只在第一次发送时打开，此后一直复用绑定的 HWND。
WX_OPEN_WINDOWS_ON_DEMAND = True

# MaiBot API 配置
# 新版麦麦用 maim_message 库的纯 WebSocket 服务，默认端口 8000，路径 /ws。
# 端口对应 bot_config.toml 里 [maim_message].ws_server_port（默认 8000）。
MAIBOT_API_URL = os.getenv('MAIBOT_API_URL', 'ws://127.0.0.1:8000/ws')
_maibot_data_dir = os.getenv('MAIBOT_DATA_DIR', '').strip()
MAIBOT_DATA_DIR = (
    os.path.abspath(os.path.expanduser(_maibot_data_dir))
    if _maibot_data_dir
    else ''
)

# 日志配置
LOG_LEVEL = os.getenv('LOG_LEVEL', 'INFO').strip().upper()
LOG_FILE = os.getenv('LOG_FILE', 'wemai.log')
EXIT_LOG_FILE = os.getenv('EXIT_LOG_FILE', 'wemai_exit.log')
LOG_FORMAT = os.getenv(
    'LOG_FORMAT',
    '%(asctime)s.%(msecs)03d - %(levelname)s - %(message)s',
)
LOG_DATE_FORMAT = os.getenv('LOG_DATE_FORMAT', '%Y-%m-%d %H:%M:%S')

# WeMai 与 MaiBot 约定的固定平台标识。不能随 UI 引擎名称变化，否则会导致
# 同一微信用户、群聊和聊天流被 MaiBot 识别成另一套身份。
PLATFORM_ID = 'wx'
WEMAI_BOT_NICKNAME = os.getenv(
    'WEMAI_BOT_NICKNAME', os.getenv('WX_BOT_NICKNAME', '')
).strip()
# 是否下载入站媒体；图片识别由 MaiBot 负责。
IMAGE_AUTO_DOWNLOAD = _parse_bool(os.getenv('IMAGE_AUTO_DOWNLOAD'), True)
# 图片和表情包独立转发，关闭后普通图片只发送文字占位。
IMAGE_RECOGNITION_ENABLED = _parse_bool(os.getenv('IMAGE_RECOGNITION_ENABLED'), True)


def _bounded_int(name: str, default: int, maximum: int) -> int:
    raw = os.getenv(name, str(default))
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} 必须是整数，当前值: {raw!r}") from exc
    if not 1 <= value <= maximum:
        raise ValueError(f"{name} 必须在 1..{maximum} 范围内，当前值: {value}")
    return value


def _positive_float(name: str, default: float) -> float:
    raw = os.getenv(name, str(default))
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} 必须是数字，当前值: {raw!r}") from exc
    if value <= 0:
        raise ValueError(f"{name} 必须大于 0，当前值: {value}")
    return value


def _nonnegative_float(name: str, default: float) -> float:
    raw = os.getenv(name, str(default))
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} 必须是数字，当前值: {raw!r}") from exc
    if value < 0:
        raise ValueError(f"{name} 必须大于或等于 0，当前值: {value}")
    return value


def _retention_days(name: str, default: int, maximum: int) -> int:
    """读取保留天数，0 表示永不清理。"""
    raw = os.getenv(name, str(default))
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} 必须是整数，当前值: {raw!r}") from exc
    if value < 0 or value > maximum:
        raise ValueError(
            f"{name} 必须在 0..{maximum} 范围内（0 表示不删除），当前值: {value}"
        )
    return value


# WeMai 微信 UI 连接与消息监听调度参数
WEMAI_TICK = _positive_float('WEMAI_TICK', 0.05)
WEMAI_BATCH_SIZE = _bounded_int('WEMAI_BATCH_SIZE', 8, 10000)
WEMAI_TAIL_SIZE = _bounded_int('WEMAI_TAIL_SIZE', 8, 10000)
WEMAI_UI_LOOKUP_TIMEOUT = _positive_float('WEMAI_UI_LOOKUP_TIMEOUT', 0.04)
WEMAI_UI_POLL_INTERVAL = _positive_float('WEMAI_UI_POLL_INTERVAL', 0.05)
# 搜索只用于首次打开或 HWND 失效恢复；结果出现后立即返回，3 秒是轮询上限。
WEMAI_SEARCH_TIMEOUT = _positive_float('WEMAI_SEARCH_TIMEOUT', 3.0)
WEMAI_SUBWINDOW_TIMEOUT = _positive_float('WEMAI_SUBWINDOW_TIMEOUT', 8.0)
WEMAI_WINDOW_CHECK_INTERVAL = _positive_float(
    'WEMAI_WINDOW_CHECK_INTERVAL', 5.0
)
# 微信更新弹窗会遮挡主窗口和独立聊天窗口。更新通常数周才出现一次，
# 默认每 3 天做一次轻量顶层窗口检查；确认后等待 5 秒再点击忽略。
WEMAI_AUTO_DISMISS_UPDATE = _parse_bool(
    os.getenv('WEMAI_AUTO_DISMISS_UPDATE'), True
)
WEMAI_UPDATE_DIALOG_SCAN_INTERVAL = _positive_float(
    'WEMAI_UPDATE_DIALOG_SCAN_INTERVAL', 3 * 24 * 60 * 60
)
WEMAI_UPDATE_DISMISS_DELAY = _nonnegative_float(
    'WEMAI_UPDATE_DISMISS_DELAY', 5.0
)
# 微信 4.1.9.30 已打开的独立窗口标题不会随改名刷新，真 ID 绑定优先。
WEMAI_SUBWINDOW_TITLE_REFRESHES = False

# VoiceHost 校验 60 秒语音上限；65 秒仅用于宿主失去响应时的故障保护。
VOICE_COMMAND_TIMEOUT_SECONDS = 65.0

# WebUI 默认只允许本机访问。填写 0.0.0.0 或局域网 IP 时必须设置访问密码。
WEBUI_HOST = os.getenv('WEBUI_HOST', '127.0.0.1').strip()
WEBUI_PORT = _bounded_int('WEBUI_PORT', 8888, 65535)
WEBUI_PASSWORD = os.getenv('WEBUI_PASSWORD', '')

# 高级管理扩展默认关闭；全部关闭时不会启动 WeChatAuto.SDK 或加载 OCR。
OPTIONAL_CONTACTS_ENABLED = _parse_bool(
    os.getenv('OPTIONAL_CONTACTS_ENABLED'), False
)
OPTIONAL_GROUPS_ENABLED = _parse_bool(
    os.getenv('OPTIONAL_GROUPS_ENABLED'), False
)
OPTIONAL_FEATURES_ENABLED = any((
    OPTIONAL_CONTACTS_ENABLED,
    OPTIONAL_GROUPS_ENABLED,
))
OPTIONAL_HOST_PORT = _bounded_int('OPTIONAL_HOST_PORT', 5088, 65535)

# 0 表示等待所有初始目标处理完成，不按累计耗时误杀 UI worker。
UI_WORKER_STARTUP_TIMEOUT_SECONDS = _nonnegative_float(
    'UI_WORKER_STARTUP_TIMEOUT_SECONDS', 0.0
)


IMAGE_SAVE_DIR = os.path.abspath(os.path.expanduser(
    os.getenv('IMAGE_SAVE_DIR', os.path.join(os.getcwd(), 'WeMai媒体'))
))
IMAGE_SAVE_TIMEOUT_SECONDS = _bounded_int('IMAGE_SAVE_TIMEOUT_SECONDS', 10, 120)
# 图片、表情和语音进入消息对象或语音宿主，保持较小的内存上限。
MAX_MEDIA_BYTES = _bounded_int('MAX_MEDIA_BYTES', 15 * 1024 * 1024, 50 * 1024 * 1024)
# 普通微信文件允许使用微信的 200 MiB 自动下载上限；下载过程采用磁盘流。
FILE_MAX_BYTES = _bounded_int(
    'FILE_MAX_BYTES', 200 * 1024 * 1024, 200 * 1024 * 1024
)
# 本机可信插件可通过 file:// 元数据请求发送文件，但仅允许访问该根目录
# 下的 attachments 子目录。留空时完全禁用，避免任意插件读取本机文件。
_local_file_delivery_root = os.getenv('LOCAL_FILE_DELIVERY_ROOT', '').strip()
LOCAL_FILE_DELIVERY_ROOT = (
    os.path.abspath(os.path.expanduser(_local_file_delivery_root))
    if _local_file_delivery_root
    else ''
)
MEDIA_DOWNLOAD_TIMEOUT_SECONDS = _bounded_int('MEDIA_DOWNLOAD_TIMEOUT_SECONDS', 15, 120)
MEDIA_DOWNLOAD_MAX_REDIRECTS = _bounded_int('MEDIA_DOWNLOAD_MAX_REDIRECTS', 3, 10)
SEND_QUEUE_SIZE = _bounded_int('SEND_QUEUE_SIZE', 100, 100000)
# WeFlow SSE 接收线程只负责读流；解析线程按 sessionId 串行化同一会话，
# 不同会话最多并发 8 路。上限 32 避免错误配置创建过多线程。
WEFLOW_DISPATCH_WORKERS = _bounded_int(
    'WEFLOW_DISPATCH_WORKERS', 8, 32
)
# 微信入站最终投递到 MaiBot 时，同一会话严格串行，不同会话并发。
# 默认 8 足以消除单循环排队，同时避免瞬时突发压垮 MaiBot WebSocket。
INBOUND_ROUTER_CONCURRENCY = _bounded_int(
    'INBOUND_ROUTER_CONCURRENCY', 8, 32
)
UI_QUEUE_SIZE = _bounded_int('UI_QUEUE_SIZE', 100, 10000)
UI_WORKER_IDLE_TIMEOUT_SECONDS = _positive_float(
    'UI_WORKER_IDLE_TIMEOUT_SECONDS', 30.0
)
UI_WORKER_BUSY_TIMEOUT_SECONDS = max(
    _positive_float('UI_WORKER_BUSY_TIMEOUT_SECONDS', 60.0),
    IMAGE_SAVE_TIMEOUT_SECONDS + 15.0,
)
UI_WORKER_AUTO_RESTART = _parse_bool(
    os.getenv('UI_WORKER_AUTO_RESTART'), True
)
ID_MAP_FILE = os.getenv('ID_MAP_FILE', 'wemai_id_map.json')
LOG_MAX_BYTES = _bounded_int('LOG_MAX_BYTES', 10 * 1024 * 1024, 200 * 1024 * 1024)
LOG_BACKUP_COUNT = _bounded_int('LOG_BACKUP_COUNT', 5, 100)
EXIT_LOG_MAX_BYTES = _bounded_int('EXIT_LOG_MAX_BYTES', 1024 * 1024, 200 * 1024 * 1024)
EXIT_LOG_BACKUP_COUNT = _bounded_int('EXIT_LOG_BACKUP_COUNT', 3, 100)

# Long-running housekeeping. Defaults are tuned for a ~1 TB disk where
# plenty of headroom is available: weflow temp files are GC'd after
# 30 days (they are internals; useful for forensic replay within a
# month, wasted beyond that), and user-visible 微信图片_* previews
# are kept for 6 months by default. Either value may be set to 0 to
# disable the corresponding sweep entirely. Upper bound is 3650
# (10 years) for explicit long-retention deployments.
IMAGE_TEMP_PREFIX = "weflow_"
IMAGE_PERSISTED_PREFIX = "微信图片_"
IMAGE_TEMP_RETENTION_DAYS = _retention_days('IMAGE_TEMP_RETENTION_DAYS', 30, 3650)
IMAGE_PERSISTED_RETENTION_DAYS = _retention_days('IMAGE_PERSISTED_RETENTION_DAYS', 180, 3650)
IMAGE_TEMP_RETENTION_SECONDS = IMAGE_TEMP_RETENTION_DAYS * 86400
IMAGE_PERSISTED_RETENTION_SECONDS = IMAGE_PERSISTED_RETENTION_DAYS * 86400
IMAGE_GC_INTERVAL_SECONDS = _positive_float('IMAGE_GC_INTERVAL_SECONDS', 3600.0)
ID_MAP_MAX_ROWS = _bounded_int('ID_MAP_MAX_ROWS', 100000, 1000000)
ID_MAP_GC_INTERVAL_SECONDS = _positive_float('ID_MAP_GC_INTERVAL_SECONDS', 300.0)

if LOG_LEVEL not in {'DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'}:
    raise ValueError(f"无效 LOG_LEVEL: {LOG_LEVEL!r}")
try:
    _webui_address = ipaddress.ip_address(WEBUI_HOST)
except ValueError as exc:
    if WEBUI_HOST.lower() != 'localhost':
        raise ValueError(
            "WEBUI_HOST 必须是 IP 地址或 localhost，"
            f"当前值: {WEBUI_HOST!r}"
        ) from exc
    _webui_address = ipaddress.ip_address('127.0.0.1')
if not (_webui_address.is_loopback or _webui_address.is_unspecified) and not WEBUI_PASSWORD:
    raise ValueError("WebUI 监听非本机地址时必须设置 WEBUI_PASSWORD")
if _webui_address.is_unspecified and not WEBUI_PASSWORD:
    raise ValueError("WebUI 监听 0.0.0.0 时必须设置 WEBUI_PASSWORD")
if OPTIONAL_HOST_PORT == WEBUI_PORT:
    raise ValueError("OPTIONAL_HOST_PORT 不能与 WEBUI_PORT 相同")
if UI_WORKER_BUSY_TIMEOUT_SECONDS < UI_WORKER_IDLE_TIMEOUT_SECONDS:
    raise ValueError(
        "UI_WORKER_BUSY_TIMEOUT_SECONDS 不能小于 "
        "UI_WORKER_IDLE_TIMEOUT_SECONDS"
    )

# 配置信息打印
def print_config_info():
    """打印当前加载的配置信息"""
    logger = logging.getLogger(__name__)
    logger.info("\n=== WeMai 配置信息 ===")
    logger.info("微信会话: 由 WeFlow 黑白名单决定，首次发送时按需打开独立窗口")
    logger.info(
        "WeFlow: push_enabled=%s api_url=%s token_configured=%s",
        WEFLOW_PUSH_ENABLED,
        WEFLOW_API_URL,
        bool(WEFLOW_API_TOKEN),
    )
    logger.info(f"MaiBot API URL: {MAIBOT_API_URL}")
    logger.info(f"MaiBot 数据目录: {MAIBOT_DATA_DIR or '<未配置>'}")
    logger.info(
        "WeFlow 入站解析: 同会话串行，不同会话并发上限=%d",
        WEFLOW_DISPATCH_WORKERS,
    )
    logger.info(
        "MaiBot 入站投递: 同会话串行，不同会话并发上限=%d",
        INBOUND_ROUTER_CONCURRENCY,
    )
    logger.info(f"日志级别: {LOG_LEVEL}")
    logger.info(f"平台标识: {PLATFORM_ID}")
    logger.info(
        "WebUI: http://%s:%s（%s）",
        WEBUI_HOST,
        WEBUI_PORT,
        "需要密码" if WEBUI_PASSWORD else "仅本机访问",
    )
    logger.info(
        "OCR 高级扩展: 联系人=%s 群管理=%s",
        OPTIONAL_CONTACTS_ENABLED,
        OPTIONAL_GROUPS_ENABLED,
    )
    logger.info(
        "WeMai UI: tick=%s batch_size=%s tail_size=%s "
        "lookup_timeout=%ss poll_interval=%ss search_timeout=%ss "
        "subwindow_timeout=%ss window_check_interval=%ss "
        "title_refreshes=%s ui_startup_timeout=%ss",
        WEMAI_TICK,
        WEMAI_BATCH_SIZE,
        WEMAI_TAIL_SIZE,
        WEMAI_UI_LOOKUP_TIMEOUT,
        WEMAI_UI_POLL_INTERVAL,
        WEMAI_SEARCH_TIMEOUT,
        WEMAI_SUBWINDOW_TIMEOUT,
        WEMAI_WINDOW_CHECK_INTERVAL,
        WEMAI_SUBWINDOW_TITLE_REFRESHES,
        UI_WORKER_STARTUP_TIMEOUT_SECONDS,
    )
    logger.info(
        "UI worker watchdog: idle=%ss busy=%ss auto_restart=%s",
        UI_WORKER_IDLE_TIMEOUT_SECONDS,
        UI_WORKER_BUSY_TIMEOUT_SECONDS,
        UI_WORKER_AUTO_RESTART,
    )
    logger.info(
        "微信更新弹窗: auto_dismiss=%s delay=%ss scan_interval=%ss",
        WEMAI_AUTO_DISMISS_UPDATE,
        WEMAI_UPDATE_DISMISS_DELAY,
        WEMAI_UPDATE_DIALOG_SCAN_INTERVAL,
    )
    logger.info(
        "图片处理: auto_download=%s recognition=%s save_dir=%s timeout=%ss",
        IMAGE_AUTO_DOWNLOAD,
        IMAGE_RECOGNITION_ENABLED,
        IMAGE_SAVE_DIR,
        IMAGE_SAVE_TIMEOUT_SECONDS,
    )
    logger.info("==========================\n")

# 日志只由 main.configure_logging() 集中初始化。
