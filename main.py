import argparse
import asyncio
import logging
from logging.handlers import RotatingFileHandler
import signal
import sys
import threading
import time
from datetime import datetime, timezone

from config import (
    EXIT_LOG_BACKUP_COUNT,
    EXIT_LOG_FILE,
    EXIT_LOG_MAX_BYTES,
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
exit_logger = logging.getLogger("wemai.exit")
stop_event = threading.Event()
UI_HEARTBEAT_IDLE_TIMEOUT = 30
UI_HEARTBEAT_BUSY_TIMEOUT = 60
_runtime = {"started": time.monotonic(), "state": None, "processor": None,
            "ui_thread": None, "shutdown_reason": None}


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


def configure_exit_logging():
    exit_logger.handlers.clear()
    exit_logger.setLevel(logging.INFO)
    exit_logger.propagate = False
    formatter = logging.Formatter(LOG_FORMAT, datefmt=LOG_DATE_FORMAT)
    handler = RotatingFileHandler(
        EXIT_LOG_FILE, maxBytes=EXIT_LOG_MAX_BYTES,
        backupCount=EXIT_LOG_BACKUP_COUNT, encoding="utf-8")
    handler.setFormatter(formatter)
    exit_logger.addHandler(handler)


def _flush_logs():
    seen = set()
    for current_logger in (logging.getLogger(), exit_logger):
        for handler in current_logger.handlers:
            if id(handler) not in seen:
                seen.add(id(handler))
                try:
                    handler.flush()
                except Exception:
                    pass


def log_exit(reason, exc=None):
    """Write a self-contained runtime snapshot to the dedicated exit log."""
    now = time.monotonic()
    state = _runtime.get("state") or {}
    processor = _runtime.get("processor")
    ui_thread = _runtime.get("ui_thread")
    listener = state.get("listener")
    heartbeat = state.get("heartbeat")
    heartbeat_age = now - heartbeat if heartbeat is not None else None
    try:
        router_thread = getattr(processor, "_thread", None)
        router_connected = bool(
            processor and processor.router.check_connection(processor.platform))
    except Exception:
        router_thread = getattr(processor, "_thread", None)
        router_connected = "check_failed"
    threads = [
        {"name": thread.name, "ident": thread.ident, "alive": thread.is_alive(),
         "daemon": thread.daemon}
        for thread in threading.enumerate()
    ]
    details = {
        "exit_time": datetime.now(timezone.utc).astimezone().isoformat(),
        "runtime_seconds": round(now - _runtime["started"], 3),
        "reason": reason,
        "threads": threads,
        "ui_thread_alive": ui_thread.is_alive() if ui_thread else None,
        "router_thread_alive": router_thread.is_alive() if router_thread else None,
        "last_heartbeat": state.get("heartbeat_at"),
        "heartbeat_age_seconds": round(heartbeat_age, 3) if heartbeat_age is not None else None,
        "ui_command_active": getattr(listener, "_command_active", None),
        "ui_recovery_active": getattr(listener, "_recovery_active", None),
        "ui_reconnecting": getattr(listener, "_reconnecting", None),
        "ui_command_age_seconds": (
            round(now - listener._command_started, 3)
            if listener and getattr(listener, "_command_started", None) else None),
        "ui_recovery_age_seconds": (
            round(now - listener._recovery_started, 3)
            if listener and getattr(listener, "_recovery_started", None) else None),
        "listen_chat_count": len(getattr(listener, "listen_chats", {})) if listener else None,
        "failed_chat_count": len(getattr(listener, "_failed_chats", {})) if listener else None,
        "router_connected": router_connected,
        "router_restart_count": getattr(processor, "_router_restart_count", None),
        "router_error": repr(getattr(processor, "startup_error", None)) if processor else None,
    }
    exc_info = (type(exc), exc, exc.__traceback__) if exc is not None else None
    exit_logger.error("EXIT_DIAGNOSTIC %s", details, exc_info=exc_info)
    _flush_logs()


def _update_heartbeat(state):
    state["heartbeat"] = time.monotonic()
    state["heartbeat_at"] = datetime.now(timezone.utc).astimezone().isoformat()


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
            heartbeat=lambda: _update_heartbeat(state),
        )
        state["listener"] = listener
        state["ready"].set()
        listener.start_listening()
        state["ui_exit_reason"] = "UI worker 正常结束"
    except BaseException as exc:
        state["error"] = exc
        state["ui_exit_reason"] = f"UI worker 异常: {exc}"
        state["ready"].set()
        logger.exception("UI worker 异常")
        log_exit("UI worker 异常结束", exc)
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
    _runtime["shutdown_reason"] = f"收到信号 {signum}"
    stop_event.set()


async def main(args):
    _runtime["started"] = time.monotonic()
    _runtime["shutdown_reason"] = None
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
             "heartbeat": time.monotonic(),
             "heartbeat_at": datetime.now(timezone.utc).astimezone().isoformat(),
             "ui_exit_reason": None, "router_warning_at": 0}
    _runtime.update({"state": state, "processor": processor, "ui_thread": None})
    failure = None
    try:
        processor.start(timeout=20)
        logger.info("Router WebSocket 已连接")
        ui_thread = threading.Thread(
            target=run_ui_worker,
            args=(targets, commands, inbound, state),
            name="wechat-ui",
            daemon=True,
        )
        _runtime["ui_thread"] = ui_thread
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
                    error = RuntimeError("UI worker 异常结束")
                    log_exit("主循环检测到 UI worker 异常结束", state["error"])
                    raise error from state["error"]
                log_exit(state["ui_exit_reason"] or "UI worker 未知原因结束")
                break
            if processor._thread and not processor._thread.is_alive():
                stop_event.set()
                log_exit("主循环检测到 Router worker 异常结束", processor.startup_error)
                raise RuntimeError("Router worker 异常结束") from processor.startup_error
            heartbeat_age = time.monotonic() - state["heartbeat"]
            listener = state["listener"]
            command_active = bool(listener and listener._command_active)
            recovery_active = bool(listener and listener._recovery_active)
            ui_busy = (command_active or recovery_active
                       or bool(listener and listener._reconnecting))
            heartbeat_limit = (UI_HEARTBEAT_BUSY_TIMEOUT if ui_busy
                               else UI_HEARTBEAT_IDLE_TIMEOUT)
            if heartbeat_age > heartbeat_limit:
                stop_event.set()
                reason = (f"UI worker heartbeat 超过 {heartbeat_limit} 秒未更新 "
                          f"command_active={command_active} "
                          f"recovery_active={recovery_active}")
                log_exit(reason)
                raise RuntimeError(reason)
            try:
                router_connected = processor.router.check_connection(processor.platform)
            except Exception:
                router_connected = False
                logger.debug("主线程查询 Router 连接状态失败", exc_info=True)
            if (not router_connected
                    and time.monotonic() - state["router_warning_at"] >= 30):
                state["router_warning_at"] = time.monotonic()
                logger.warning("Router 当前未连接，后台将持续尝试恢复")
            await asyncio.sleep(0.5)
        if state["error"] and not _runtime.get("shutdown_reason"):
            log_exit("停止事件由 UI worker 异常触发", state["error"])
            raise RuntimeError("UI worker 异常结束") from state["error"]
    except BaseException as exc:
        failure = exc
        raise
    finally:
        stop_event.set()
        processor.stop(timeout=15)
        if ui_thread:
            ui_thread.join(timeout=10)
            if ui_thread.is_alive():
                logger.error("UI worker 在 10 秒内未退出；daemon 线程将随进程结束")
        set_global_processor(None)
        reason = (_runtime.get("shutdown_reason")
                  or (f"main 异常退出: {failure}" if failure else None)
                  or state.get("ui_exit_reason") or "main 正常退出")
        log_exit(reason, failure)


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
    configure_exit_logging()
    try:
        asyncio.run(main(parse_args()))
    except KeyboardInterrupt:
        log_exit("KeyboardInterrupt")
    except Exception as exc:
        logger.exception("程序异常退出")
        log_exit("程序异常退出", exc)
        sys.exit(1)
    finally:
        _flush_logs()
        logging.shutdown()
