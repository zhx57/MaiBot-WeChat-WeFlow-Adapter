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
    return value.lower() in ('true', 'yes', '1', 't', 'y')

# 要监听的聊天对象列表
WX_TARGET_CHATS = _parse_list(os.getenv('WX_TARGET_CHATS'), [])

# 是否在未指定目标聊天时监听所有聊天
WX_LISTEN_ALL_IF_EMPTY = _parse_bool(os.getenv('WX_LISTEN_ALL_IF_EMPTY'), False)

# 排除的聊天对象
WX_EXCLUDED_CHATS = _parse_list(
    os.getenv('WX_EXCLUDED_CHATS'), 
    ["文件传输助手", "微信团队", "微信支付"]
)

# MaiBot API 配置
# 新版麦麦用 maim_message 库的纯 WebSocket 服务，默认端口 8000，路径 /ws。
# 端口对应 bot_config.toml 里 [maim_message].ws_server_port（默认 8000）。
MAIBOT_API_URL = os.getenv('MAIBOT_API_URL', 'ws://127.0.0.1:8000/ws')

# 日志配置
LOG_LEVEL = os.getenv('LOG_LEVEL', 'INFO').strip().upper()
LOG_FILE = os.getenv('LOG_FILE', 'wepush.log')
EXIT_LOG_FILE = os.getenv('EXIT_LOG_FILE', 'wemai_exit.log')
LOG_FORMAT = os.getenv('LOG_FORMAT', '%(asctime)s - %(levelname)s - %(message)s')
LOG_DATE_FORMAT = os.getenv('LOG_DATE_FORMAT', '%Y-%m-%d %H:%M:%S')

# 平台标识
PLATFORM_ID = os.getenv('PLATFORM_ID', 'wxauto')
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


MAX_MEDIA_BYTES = _bounded_int('MAX_MEDIA_BYTES', 10 * 1024 * 1024, 1024 * 1024 * 1024)
SEND_QUEUE_SIZE = _bounded_int('SEND_QUEUE_SIZE', 100, 100000)
UI_QUEUE_SIZE = _bounded_int('UI_QUEUE_SIZE', 100, 10000)
ID_MAP_FILE = os.getenv('ID_MAP_FILE', 'wemai_id_map.json')
LOG_MAX_BYTES = _bounded_int('LOG_MAX_BYTES', 10 * 1024 * 1024, 10 * 1024 * 1024 * 1024)
LOG_BACKUP_COUNT = _bounded_int('LOG_BACKUP_COUNT', 5, 100)
EXIT_LOG_MAX_BYTES = _bounded_int('EXIT_LOG_MAX_BYTES', 1024 * 1024, 1024 * 1024 * 1024)
EXIT_LOG_BACKUP_COUNT = _bounded_int('EXIT_LOG_BACKUP_COUNT', 3, 100)

if LOG_LEVEL not in {'DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'}:
    raise ValueError(f"无效 LOG_LEVEL: {LOG_LEVEL!r}")

# 配置信息打印
def print_config_info():
    """打印当前加载的配置信息"""
    logger = logging.getLogger(__name__)
    logger.info("\n=== WeMai 配置信息 ===")
    logger.info(f"\u5fae信监听目标: {WX_TARGET_CHATS}")
    logger.info(f"\u76d1听所有聊天: {WX_LISTEN_ALL_IF_EMPTY}")
    logger.info(f"\u6392除的聊天: {WX_EXCLUDED_CHATS}")
    logger.info(f"MaiBot API URL: {MAIBOT_API_URL}")
    logger.info(f"\u65e5志级别: {LOG_LEVEL}")
    logger.info(f"\u5e73台标识: {PLATFORM_ID}")
    logger.info("==========================\n")

# 日志只由 main.configure_logging() 集中初始化。
