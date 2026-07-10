import argparse
import asyncio
import logging
from logging.handlers import RotatingFileHandler
import signal
import sys
import threading
import time

from config import (
    LOG_BACKUP_COUNT,
    LOG_DATE_FORMAT,
    LOG_FILE,
    LOG_FORMAT,
    LOG_LEVEL,
    LOG_MAX_BYTES,
    WX_LISTEN_ALL_IF_EMPTY,
    WX_TARGET_CHATS,
    _parse_list,
)
from wx_Listener import (
    UICommandQueue,
    WeChatListener,
    create_message_processor,
    message_callback,
    set_global_processor,
)

logger = logging.getLogger(__name__)
stop_event = threading.Event()


def configure_logging():
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(getattr(logging, LOG_LEVEL))
    formatter = logging.Formatter(LOG_FORMAT, datefmt=LOG_DATE_FORMAT)
    stream = logging.StreamHandler()
    stream.setFormatter(formatter)
    rotating = RotatingFileHandler(LOG_FILE, maxBytes=LOG_MAX_BYTES,
                                   backupCount=LOG_BACKUP_COUNT, encoding="utf-8")
    rotating.setFormatter(formatter)
    root.addHandler(stream)
    root.addHandler(rotating)


def run_ui_worker(target_chats, commands, inbound_enabled, state):
    """Create and use all COM/UIA objects in this one dedicated thread."""
    pythoncom = None
    uia = None
    listener = None
    try:
        try:
            import pythoncom as _pythoncom
            pythoncom = _pythoncom
            pythoncom.CoInitialize()
        except ImportError as exc:
            raise RuntimeError("Windows UI 线程需要 pywin32/pythoncom") from exc
        from wxauto import uiautomation as _uia
        uia = _uia
        uia.InitializeUIAutomationInCurrentThread()
        listener = WeChatListener(
            target_chats=target_chats if inbound_enabled else [],
            callback=message_callback if inbound_enabled else None,
            command_queue=commands,
            stop_event=stop_event,
            heartbeat=lambda: state.__setitem__("heartbeat", time.monotonic()),
        )
        state["listener"] = listener
        state["ready"].set()
        listener.start_listening()
    except BaseException as exc:
        state["error"] = exc
        state["ready"].set()
        logger.exception("UI worker 异常")
    finally:
        if listener:
            try:
                listener.close()
            except Exception:
                logger.exception("清理微信监听资源失败")
        if uia:
            try:
                uia.UninitializeUIAutomationInCurrentThread()
            except Exception:
                logger.exception("释放 UIAutomation 失败")
        if pythoncom:
            pythoncom.CoUninitialize()
        stop_event.set()


def _handle_signal(signum, _frame):
    logger.info("收到信号 %s，开始停止", signum)
    stop_event.set()


async def main(args):
    stop_event.clear()
    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)
    inbound = not args.maibot_to_wx
    outbound = not args.wx_to_maibot
    targets = _parse_list(args.target_chats, WX_TARGET_CHATS) if args.target_chats else WX_TARGET_CHATS
    commands = UICommandQueue()
    processor = create_message_processor(
        ui_submit=commands.submit, inbound_enabled=inbound, outbound_enabled=outbound
    )
    for target in targets:
        if isinstance(target, dict) and target.get("type") in {"private", "group"}:
            processor.register_target(target["name"], target["type"])
    set_global_processor(processor)
    ui_thread = None
    state = {"ready": threading.Event(), "error": None, "listener": None,
             "heartbeat": time.monotonic()}
    try:
        processor.start(timeout=20)
        logger.info("Router WebSocket 已连接")
        ui_thread = threading.Thread(
            target=run_ui_worker,
            args=(targets, commands, inbound, state),
            name="wechat-ui",
            daemon=True,
        )
        ui_thread.start()
        if not await asyncio.to_thread(state["ready"].wait, 30):
            raise TimeoutError("微信 UI worker 启动超时")
        if state["error"]:
            raise RuntimeError("微信 UI worker 启动失败") from state["error"]
        logger.info("服务已就绪 mode=%s chats=%s listen_all_if_empty=%s",
                    "双向" if inbound and outbound else ("微信到MaiBot" if inbound else "MaiBot到微信"),
                    targets, WX_LISTEN_ALL_IF_EMPTY)
        while not stop_event.is_set():
            if not ui_thread.is_alive():
                stop_event.set()
                if state["error"]:
                    raise RuntimeError("UI worker 异常结束") from state["error"]
                break
            if processor._thread and not processor._thread.is_alive():
                stop_event.set()
                raise RuntimeError("Router worker 异常结束") from processor.startup_error
            if time.monotonic() - state["heartbeat"] > 30:
                stop_event.set()
                raise RuntimeError("UI worker heartbeat 超过 30 秒未更新")
            if not processor.router.check_connection(processor.platform):
                logger.warning("Router 当前未连接，后台健康检查将负责失败退出")
            await asyncio.sleep(0.5)
    finally:
        stop_event.set()
        processor.stop(timeout=15)
        if ui_thread:
            ui_thread.join(timeout=10)
            if ui_thread.is_alive():
                logger.error("UI worker 在 10 秒内未退出；daemon 线程将随进程结束")
        set_global_processor(None)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="WeMai - 微信消息转发服务")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--all", action="store_true", help="启动双向转发（默认）")
    group.add_argument("--wx-to-maibot", action="store_true", help="仅微信到 MaiBot")
    group.add_argument("--maibot-to-wx", action="store_true", help="仅 MaiBot 到微信")
    parser.add_argument("--target-chats", help="聊天名称，逗号分隔；类型配置请使用环境配置")
    return parser.parse_args(argv)


if __name__ == "__main__":
    configure_logging()
    try:
        asyncio.run(main(parse_args()))
    except KeyboardInterrupt:
        pass
    except Exception:
        logger.exception("程序异常退出")
        sys.exit(1)
