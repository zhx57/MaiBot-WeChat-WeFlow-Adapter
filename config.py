"""
WePush 配置模块
从.env文件加载配置，支持环境变量覆盖
"""

import os
import logging
import json
from typing import List, Optional
try:
    from dotenv import load_dotenv
except ImportError:
    def load_dotenv(*_args, **_kwargs):
        return False

# 加载.env文件
load_dotenv()

# 微信监听配置
def _parse_list(value: Optional[str], default: List[str] = None) -> List[str]:
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

# 要监听的聊天对象列表
WX_TARGET_CHATS = _parse_list(os.getenv('WX_TARGET_CHATS'), [])

# 是否在未指定目标聊天时监听所有聊天
WX_LISTEN_ALL_IF_EMPTY = _parse_bool(os.getenv('WX_LISTEN_ALL_IF_EMPTY'), False)

# 排除的聊天对象（黑名单）
WX_EXCLUDED_CHATS = _parse_list(
    os.getenv('WX_EXCLUDED_CHATS'),
    ["文件传输助手", "微信团队", "微信支付"]
)

# 黑名单模式：启用后忽略 WX_TARGET_CHATS 白名单，监听除黑名单外的所有聊天。
# 仅在 WeFlow 推送启用时生效（UI 路径无法枚举全部聊天）。
WX_BLACKLIST_MODE = _parse_bool(os.getenv('WX_BLACKLIST_MODE'), False)

# WeFlow HTTP API 配置。主动推送启用时 Token 为必填项，并在服务启动时校验。
WEFLOW_API_URL = os.getenv('WEFLOW_API_URL', 'http://127.0.0.1:5031').strip().rstrip('/')
WEFLOW_API_TOKEN = os.getenv('WEFLOW_API_TOKEN', '').strip()
WEFLOW_PUSH_ENABLED = _parse_bool(os.getenv('WEFLOW_PUSH_ENABLED'), True)

# 按需打开窗口：启用后不在启动时一次性打开全部目标窗口，
# 仅在发送消息时按需打开，打开后保持检测。
# 默认跟随 WEFLOW_PUSH_ENABLED：WeFlow 接收时按需打开，UI 接收时仍全量打开。
WX_OPEN_WINDOWS_ON_DEMAND = _parse_bool(
    os.getenv('WX_OPEN_WINDOWS_ON_DEMAND'), WEFLOW_PUSH_ENABLED
)

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
LOG_FILE = os.getenv('LOG_FILE', 'wepush.log')
EXIT_LOG_FILE = os.getenv('EXIT_LOG_FILE', 'wemai_exit.log')
LOG_FORMAT = os.getenv('LOG_FORMAT', '%(asctime)s - %(levelname)s - %(message)s')
LOG_DATE_FORMAT = os.getenv('LOG_DATE_FORMAT', '%Y-%m-%d %H:%M:%S')

# 平台标识
PLATFORM_ID = os.getenv('PLATFORM_ID', 'wx4py')
WX_BOT_NICKNAME = os.getenv('WX_BOT_NICKNAME', '').strip()
IMAGE_AUTO_DOWNLOAD = _parse_bool(os.getenv('IMAGE_AUTO_DOWNLOAD'), True)
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


# wx4py 连接与消息监听调度参数
WX4PY_AUTO_CONNECT = _parse_bool(os.getenv('WX4PY_AUTO_CONNECT'), True)
WX4PY_TICK = _positive_float('WX4PY_TICK', 0.05)
WX4PY_BATCH_SIZE = _bounded_int('WX4PY_BATCH_SIZE', 8, 10000)
WX4PY_TAIL_SIZE = _bounded_int('WX4PY_TAIL_SIZE', 8, 10000)
WX4PY_UI_LOOKUP_TIMEOUT = _positive_float('WX4PY_UI_LOOKUP_TIMEOUT', 0.08)
WX4PY_UI_POLL_INTERVAL = _positive_float('WX4PY_UI_POLL_INTERVAL', 0.05)
WX4PY_SEARCH_TIMEOUT = _positive_float('WX4PY_SEARCH_TIMEOUT', 4.0)
WX4PY_SUBWINDOW_TIMEOUT = _positive_float('WX4PY_SUBWINDOW_TIMEOUT', 8.0)
WX4PY_WINDOW_CHECK_INTERVAL = _positive_float(
    'WX4PY_WINDOW_CHECK_INTERVAL', 5.0
)

# 0 表示等待所有初始目标处理完成，不按累计耗时误杀 UI worker。
UI_WORKER_STARTUP_TIMEOUT_SECONDS = _nonnegative_float(
    'UI_WORKER_STARTUP_TIMEOUT_SECONDS', 0.0
)


IMAGE_SAVE_DIR = os.path.abspath(os.path.expanduser(
    os.getenv('IMAGE_SAVE_DIR', os.path.join(os.getcwd(), 'wxauto文件'))
))
IMAGE_SAVE_TIMEOUT_SECONDS = _bounded_int('IMAGE_SAVE_TIMEOUT_SECONDS', 10, 120)
MAX_MEDIA_BYTES = _bounded_int('MAX_MEDIA_BYTES', 10 * 1024 * 1024, 1024 * 1024 * 1024)
MEDIA_DOWNLOAD_TIMEOUT_SECONDS = _bounded_int('MEDIA_DOWNLOAD_TIMEOUT_SECONDS', 15, 120)
MEDIA_DOWNLOAD_MAX_REDIRECTS = _bounded_int('MEDIA_DOWNLOAD_MAX_REDIRECTS', 3, 10)
SEND_QUEUE_SIZE = _bounded_int('SEND_QUEUE_SIZE', 100, 100000)
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
LOG_MAX_BYTES = _bounded_int('LOG_MAX_BYTES', 10 * 1024 * 1024, 10 * 1024 * 1024 * 1024)
LOG_BACKUP_COUNT = _bounded_int('LOG_BACKUP_COUNT', 5, 100)
EXIT_LOG_MAX_BYTES = _bounded_int('EXIT_LOG_MAX_BYTES', 1024 * 1024, 1024 * 1024 * 1024)
EXIT_LOG_BACKUP_COUNT = _bounded_int('EXIT_LOG_BACKUP_COUNT', 3, 100)

if LOG_LEVEL not in {'DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'}:
    raise ValueError(f"无效 LOG_LEVEL: {LOG_LEVEL!r}")
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
    logger.info(f"\u5fae信监听目标: {WX_TARGET_CHATS}")
    logger.info(f"\u76d1听所有聊天: {WX_LISTEN_ALL_IF_EMPTY}")
    logger.info(f"\u6392除的聊天: {WX_EXCLUDED_CHATS}")
    logger.info(f"\u9ed1名单模式: {WX_BLACKLIST_MODE}")
    logger.info(f"\u6309需打开窗口: {WX_OPEN_WINDOWS_ON_DEMAND}")
    logger.info(
        "WeFlow: push_enabled=%s api_url=%s token_configured=%s",
        WEFLOW_PUSH_ENABLED,
        WEFLOW_API_URL,
        bool(WEFLOW_API_TOKEN),
    )
    logger.info(f"MaiBot API URL: {MAIBOT_API_URL}")
    logger.info(f"MaiBot 数据目录: {MAIBOT_DATA_DIR or '<未配置>'}")
    logger.info(f"\u65e5志级别: {LOG_LEVEL}")
    logger.info(f"\u5e73台标识: {PLATFORM_ID}")
    logger.info(
        "wx4py: auto_connect=%s tick=%s batch_size=%s tail_size=%s "
        "lookup_timeout=%ss poll_interval=%ss search_timeout=%ss "
        "subwindow_timeout=%ss window_check_interval=%ss ui_startup_timeout=%ss",
        WX4PY_AUTO_CONNECT,
        WX4PY_TICK,
        WX4PY_BATCH_SIZE,
        WX4PY_TAIL_SIZE,
        WX4PY_UI_LOOKUP_TIMEOUT,
        WX4PY_UI_POLL_INTERVAL,
        WX4PY_SEARCH_TIMEOUT,
        WX4PY_SUBWINDOW_TIMEOUT,
        WX4PY_WINDOW_CHECK_INTERVAL,
        UI_WORKER_STARTUP_TIMEOUT_SECONDS,
    )
    logger.info(
        "UI worker watchdog: idle=%ss busy=%ss auto_restart=%s",
        UI_WORKER_IDLE_TIMEOUT_SECONDS,
        UI_WORKER_BUSY_TIMEOUT_SECONDS,
        UI_WORKER_AUTO_RESTART,
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
