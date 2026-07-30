"""WeMai 核心进程入口与运行生命周期管理。

启动顺序为：配置日志 -> 启动可选扩展宿主 -> 启动 MaiBot Router ->
启动微信 UI 工作线程 -> 建立 WeFlow SSE 入站连接。关闭时按相反顺序回收，
避免消息仍在进入时先销毁投递端或微信窗口。

本模块只负责组件编排和健康检查；消息格式转换在 ``wx_Processor.py``，
WeFlow 协议解析在 ``weflow_listener.py``，微信 UI 操作在 ``wx_Listener.py``。
"""

import argparse
import asyncio
import contextlib
import logging
import os
import signal
import sys
import threading
import time
import traceback
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler

from config import (
    EXIT_LOG_BACKUP_COUNT,
    EXIT_LOG_FILE,
    EXIT_LOG_MAX_BYTES,
    IMAGE_AUTO_DOWNLOAD,
    LOG_BACKUP_COUNT,
    LOG_DATE_FORMAT,
    LOG_FILE,
    LOG_FORMAT,
    LOG_LEVEL,
    LOG_MAX_BYTES,
    OPTIONAL_CONTACTS_ENABLED,
    OPTIONAL_GROUPS_ENABLED,
    OPTIONAL_HOST_PORT,
    UI_WORKER_AUTO_RESTART,
    UI_WORKER_BUSY_TIMEOUT_SECONDS,
    UI_WORKER_IDLE_TIMEOUT_SECONDS,
    UI_WORKER_STARTUP_TIMEOUT_SECONDS,
    WEFLOW_API_TOKEN,
    WEFLOW_API_URL,
    print_config_info,
)
from optional_host_manager import OptionalFeatureHost
from outgoing_registry import OutgoingMessageRegistry
from voice_host_backend import OutboundUIGate, WeMaiVoiceBackend
from weflow_listener import WeFlowListener
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
_runtime = {"started": time.monotonic(), "state": None, "processor": None,
            "ui_thread": None, "shutdown_reason": None}
DEFAULT_LOG_FORMAT = '%(asctime)s.%(msecs)03d - %(levelname)s - %(message)s'


class UIWorkerStalledError(RuntimeError):
    """UI 工作线程仍存活，但心跳长时间没有推进。"""


def _log_formatter():
    """构造本进程日志格式，不继承宿主可能设置的 JSON 日志模式。"""
    try:
        return logging.Formatter(LOG_FORMAT, datefmt=LOG_DATE_FORMAT)
    except (TypeError, ValueError):
        return logging.Formatter(DEFAULT_LOG_FORMAT, datefmt=LOG_DATE_FORMAT)


def configure_logging():
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(getattr(logging, LOG_LEVEL))
    formatter = _log_formatter()
    stream = logging.StreamHandler()
    stream.setFormatter(formatter)
    rotating = RotatingFileHandler(LOG_FILE, maxBytes=LOG_MAX_BYTES,
                                   backupCount=LOG_BACKUP_COUNT, encoding="utf-8")
    rotating.setFormatter(formatter)
    root.addHandler(stream)
    root.addHandler(rotating)
    # 核心模块已经记录连接状态，隐藏第三方库重复且格式不一致的 INFO 输出。
    for noisy_logger in (
        "comtypes",
        "maim_message",
        "asyncio",
        "PIL",
        "urllib3",
    ):
        logging.getLogger(noisy_logger).setLevel(logging.WARNING)


def configure_exit_logging():
    for handler in exit_logger.handlers:
        with contextlib.suppress(Exception):
            handler.close()
    exit_logger.handlers.clear()
    exit_logger.setLevel(logging.INFO)
    exit_logger.propagate = False
    formatter = _log_formatter()
    handler = RotatingFileHandler(
        EXIT_LOG_FILE, maxBytes=EXIT_LOG_MAX_BYTES,
        backupCount=EXIT_LOG_BACKUP_COUNT, encoding="utf-8", delay=True)
    handler.setFormatter(formatter)
    exit_logger.addHandler(handler)


def _flush_logs():
    seen = set()
    for current_logger in (logging.getLogger(), exit_logger):
        for handler in current_logger.handlers:
            if id(handler) not in seen:
                seen.add(id(handler))
                with contextlib.suppress(Exception):
                    handler.flush()


def log_exit(reason, exc=None):
    """向独立退出日志写入完整的运行状态快照。"""
    now = time.monotonic()
    state = _runtime.get("state") or {}
    processor = _runtime.get("processor")
    ui_thread = _runtime.get("ui_thread")
    listener = state.get("listener")
    voice_backend = state.get("voice_backend")
    optional_host = state.get("optional_host")
    heartbeat = state.get("heartbeat")
    heartbeat_age = now - heartbeat if heartbeat is not None else None
    router_thread = getattr(processor, "_thread", None)
    router_connected = getattr(processor, "_router_connected", None)
    threads = [
        {"name": thread.name, "ident": thread.ident, "alive": thread.is_alive(),
         "daemon": thread.daemon}
        for thread in threading.enumerate()
    ]
    ui_status = listener.status if listener is not None else None
    details = {
        "exit_time": datetime.now(timezone.utc).astimezone().isoformat(),
        "runtime_seconds": round(now - _runtime["started"], 3),
        "reason": reason,
        "threads": threads,
        "ui_thread_alive": ui_thread.is_alive() if ui_thread else None,
        "router_thread_alive": router_thread.is_alive() if router_thread else None,
        "last_heartbeat": state.get("heartbeat_at"),
        "heartbeat_age_seconds": round(heartbeat_age, 3) if heartbeat_age is not None else None,
        "ui_phase": state.get("phase"),
        "ui_thread_stack": state.get("ui_thread_stack"),
        "ui_command_active": ui_status.command_active if ui_status else None,
        "ui_recovery_active": ui_status.recovery_active if ui_status else None,
        "ui_media_active": ui_status.media_active if ui_status else None,
        "ui_reconnecting": ui_status.reconnecting if ui_status else None,
        "ui_command_age_seconds": (
            round(now - ui_status.command_started, 3)
            if ui_status and ui_status.command_started is not None else None),
        "ui_recovery_age_seconds": (
            round(now - ui_status.recovery_started, 3)
            if ui_status and ui_status.recovery_started is not None else None),
        "ui_media_age_seconds": (
            round(now - ui_status.media_started, 3)
            if ui_status and ui_status.media_started is not None else None),
        "listen_chat_count": (
            len(ui_status.listening_target_names) if ui_status else None),
        "wemai_ui_processor_running": (
            ui_status.wemai_ui_processor_running if ui_status else None),
        # 看门狗线程不能跨线程调用 UI 线程持有的自动化对象。
        "wemai_ui_connected": ui_status.connected if ui_status else None,
        "router_connected": router_connected,
        "router_restart_count": getattr(processor, "_router_restart_count", None),
        "router_error": repr(getattr(processor, "startup_error", None)) if processor else None,
        "outbound_backend": "wemai_voice_host",
        "voice_host_running": (
            voice_backend.is_running if voice_backend else None
        ),
        "voice_host_error": (
            repr(voice_backend.last_error) if voice_backend else None
        ),
        "optional_host_running": (
            optional_host.is_running if optional_host else False
        ),
        "optional_host_error": (
            repr(optional_host.last_error) if optional_host else None
        ),
    }
    exc_info = (type(exc), exc, exc.__traceback__) if exc is not None else None
    exit_logger.error("EXIT_DIAGNOSTIC %s", details, exc_info=exc_info)
    _flush_logs()


def _update_heartbeat(state):
    state["heartbeat"] = time.monotonic()
    state["heartbeat_at"] = datetime.now(timezone.utc).astimezone().isoformat()


def _ui_worker_stall_reason(state, now=None):
    heartbeat = state.get("heartbeat")
    if heartbeat is None:
        return None
    now = time.monotonic() if now is None else now
    listener = state.get("listener")
    ui_status = listener.status if listener is not None else None
    command_active = bool(ui_status and ui_status.command_active)
    recovery_active = bool(ui_status and ui_status.recovery_active)
    media_active = bool(ui_status and ui_status.media_active)
    reconnecting = bool(ui_status and ui_status.reconnecting)
    startup_active = state.get("phase") not in {"running", "failed"}
    # 出站发送在独立 wechat-send-* 线程执行；主 UI 循环仍会继续刷新
    # heartbeat，不能用总心跳掩盖后台发送线程卡死。
    if (
        command_active
        and ui_status
        and ui_status.command_started is not None
        and now - ui_status.command_started > UI_WORKER_BUSY_TIMEOUT_SECONDS
    ):
        return (
            f"微信后台发送任务超过 {UI_WORKER_BUSY_TIMEOUT_SECONDS:g} 秒未完成 "
            f"command_age={now - ui_status.command_started:.1f}s"
        )
    busy = (
        command_active
        or recovery_active
        or media_active
        or reconnecting
        or startup_active
    )
    limit = (
        UI_WORKER_BUSY_TIMEOUT_SECONDS
        if busy
        else UI_WORKER_IDLE_TIMEOUT_SECONDS
    )
    age = now - heartbeat
    if age <= limit:
        return None
    return (
        f"微信 UI worker 心跳超过 {limit:g} 秒未更新 "
        f"phase={state.get('phase') or 'unknown'} "
        f"command_active={command_active} "
        f"recovery_active={recovery_active} "
        f"media_active={media_active} reconnecting={reconnecting} "
        f"heartbeat_age={age:.1f}s"
    )


def _thread_stack(thread):
    if thread is None or thread.ident is None:
        return ""
    frame = sys._current_frames().get(thread.ident)
    if frame is None:
        return ""
    return "".join(traceback.format_stack(frame))[-16000:]


def _raise_if_ui_worker_stalled(state, ui_thread, now=None):
    reason = _ui_worker_stall_reason(state, now=now)
    if reason is None:
        return
    state["ui_thread_stack"] = _thread_stack(ui_thread)
    _runtime["shutdown_reason"] = reason
    stop_event.set()
    logger.critical(
        "%s\n微信 UI worker 当前堆栈:\n%s",
        reason,
        state["ui_thread_stack"] or "<unavailable>",
    )
    log_exit(reason)
    raise UIWorkerStalledError(reason)


def _restart_current_process():
    executable = sys.executable
    script = os.path.abspath(sys.argv[0])
    argv = [executable, script, *sys.argv[1:]]
    logger.critical("正在重启 WeMai 进程 argv=%r", argv)
    _flush_logs()
    os.execv(executable, argv)


async def _wait_for_ui_worker_startup(state, ui_thread, timeout, clock=None):
    """Wait for target initialization without imposing a fixed total deadline."""
    clock = clock or time.monotonic
    timeout = max(float(timeout), 0.0)
    started_at = clock()
    deadline = started_at + timeout if timeout else None
    next_progress_log = started_at + 15.0

    while not state["ready"].is_set():
        # The worker can publish ready between the loop condition and this body.
        if state["ready"].is_set():
            break
        if stop_event.is_set():
            return False
        if not ui_thread.is_alive():
            # Let run_ui_worker publish its error/ready state before diagnosing
            # an otherwise unexplained early thread exit.
            await asyncio.sleep(0)
            if state["ready"].is_set():
                break
            raise RuntimeError("微信 UI worker 在启动完成前退出")

        now = clock()
        if deadline is not None and now >= deadline:
            raise TimeoutError(
                f"微信 UI worker 启动超过配置时限 {timeout:g} 秒"
            )
        _raise_if_ui_worker_stalled(state, ui_thread, now=now)

        wait_for = 0.1
        if deadline is not None:
            wait_for = min(wait_for, max(deadline - now, 0.001))
        # Probe the threading.Event without blocking the Router event loop,
        # then yield for the bounded interval when it is still unset.
        if not state["ready"].wait(0):
            await asyncio.sleep(wait_for)

        now = clock()
        if not state["ready"].is_set() and now >= next_progress_log:
            listener = state.get("listener")
            opened = list(
                getattr(listener, "listening_target_names", ()) if listener else ()
            )
            logger.info(
                "微信 UI worker 仍在初始化 phase=%s elapsed=%.1fs "
                "opened_targets=%s（累计启动超时%s）",
                state.get("phase") or "unknown",
                now - started_at,
                opened,
                f"={timeout:g}s" if timeout else "已关闭",
            )
            next_progress_log = now + 15.0
    return True


def run_ui_worker(target_chats, commands, inbound_enabled, state, stop_event=None):
    """创建 WeMai 微信监听器，并把发送命令分派到各会话窗口。

    ``stop_event`` 默认为模块级 ``main.stop_event``，保留旧调用方的行为。
    :func:`main` 显式传入它持有的本地事件，使整个调用链可被独立测试。
    """
    if stop_event is None:
        stop_event = globals()["stop_event"]
    uia = None
    uia_initialized = False
    listener = None
    try:
        state["phase"] = "initializing_uia"
        from wx4py.core import uiautomation as uia

        uia.InitializeUIAutomationInCurrentThread()
        uia_initialized = True
        listener = WeChatListener(
            target_chats=target_chats,
            callback=message_callback if inbound_enabled else None,
            command_queue=commands,
            stop_event=stop_event,
            heartbeat=lambda: _update_heartbeat(state),
            outgoing_registry=state.get("outgoing_registry"),
        )
        state["listener"] = listener
        state["phase"] = "restoring_main_window"
        listener.ensure_main_window_visible()
        state["phase"] = "opening_targets"
        listener.start_listening()
        state["listening_targets"] = list(listener.listening_target_names)
        state["phase"] = "running"
        state["ready"].set()
        while not stop_event.is_set():
            listener.process_commands(limit=1)
            _update_heartbeat(state)
            commands.wait(0.01)
        state["ui_exit_reason"] = "UI worker 正常结束"
    except BaseException as exc:
        state["error"] = exc
        state["phase"] = "failed"
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
        if uia_initialized:
            try:
                uia.UninitializeUIAutomationInCurrentThread()
            except Exception:
                logger.exception("释放 WeMai UIAutomation 失败")
        stop_event.set()


def _handle_signal(signum, _frame):
    logger.info("收到信号 %s，开始停止", signum)
    _runtime["shutdown_reason"] = f"收到信号 {signum}"
    stop_event.set()


def _register_shutdown_signals(handler):
    """注册跨平台停止信号；Windows CTRL_BREAK 对应 SIGBREAK。"""
    handled = [signal.SIGINT, signal.SIGTERM]
    sigbreak = getattr(signal, "SIGBREAK", None)
    if sigbreak is not None:
        handled.append(sigbreak)
    for current_signal in handled:
        signal.signal(current_signal, handler)


def _ui_worker_required(targets, outbound, ui_inbound, backend):
    """只要启用出站，就必须运行 WeMai 独立窗口线程。"""
    return bool(ui_inbound) or bool(outbound)


async def main(args, *, runtime=None, stop_event=None):
    """运行 WeMai 主事件循环。

    ``runtime`` 和 ``stop_event`` 使用仅关键字参数，便于测试注入全新状态；未传入
    时采用模块级默认对象，并把状态同步回模块全局变量以兼容信号处理和退出日志。
    """
    if runtime is None:
        runtime = {"started": time.monotonic(), "state": None, "processor": None,
                   "ui_thread": None, "shutdown_reason": None}
    if stop_event is None:
        stop_event = threading.Event()
    else:
        stop_event.clear()

    # 将本次运行对象单向镜像到模块全局变量，供旧的信号处理和退出日志读取。
    # 测试只应读取该镜像，不应依靠修改全局对象来控制主循环。
    _runtime.clear()
    _runtime.update(runtime)
    globals()["stop_event"] = stop_event

    def _signal_handler(signum, _frame):
        logger.info("收到信号 %s，开始停止", signum)
        _runtime["shutdown_reason"] = f"收到信号 {signum}"
        stop_event.set()

    _register_shutdown_signals(_signal_handler)
    inbound = not args.maibot_to_wx
    outbound = not args.wx_to_maibot
    # WeFlow 自己负责黑白名单，WeMai 始终接收它推送的全部会话。
    targets = []
    commands = UICommandQueue()
    outgoing_registry = OutgoingMessageRegistry()
    weflow_inbound = inbound
    ui_inbound = False
    outbound_ui_gate = OutboundUIGate() if outbound else None
    voice_backend = (
        WeMaiVoiceBackend(
            subwindow_submit=commands.submit,
            outbound_ui_gate=outbound_ui_gate,
        )
        if outbound
        else None
    )
    optional_host = OptionalFeatureHost(
        contacts=OPTIONAL_CONTACTS_ENABLED,
        groups=OPTIONAL_GROUPS_ENABLED,
        port=OPTIONAL_HOST_PORT,
    )
    outbound_submit = (
        voice_backend.submit
        if voice_backend is not None
        else commands.submit
    )
    # 文本、图片、文件由 WeMai UI 发送，语音由 VoiceHost 在同一个 HWND 中发送。
    ui_required = _ui_worker_required(
        targets,
        outbound,
        ui_inbound,
        "wemai_voice_host",
    )
    processor = create_message_processor(
        ui_submit=outbound_submit if outbound else None,
        inbound_enabled=inbound,
        outbound_enabled=outbound,
        voice_outbound_enabled=outbound,
    )
    set_global_processor(processor)
    ui_thread = None
    weflow_listener = None
    state = {"ready": threading.Event(), "error": None, "listener": None,
             "weflow_listener": None,
             "outgoing_registry": outgoing_registry,
             "listening_targets": [],
             "heartbeat": time.monotonic(),
             "heartbeat_at": datetime.now(timezone.utc).astimezone().isoformat(),
             "ui_exit_reason": None, "router_warning_at": 0,
             "phase": "pending"}
    state["voice_backend"] = voice_backend
    state["optional_host"] = optional_host
    _runtime.update({"state": state, "processor": processor, "ui_thread": None})
    failure = None
    try:
        if voice_backend is not None:
            await asyncio.to_thread(voice_backend.start)
        if optional_host.enabled:
            await asyncio.to_thread(optional_host.start)
        processor.start(timeout=20)
        logger.info("Router WebSocket 已连接")
        if ui_required:
            ui_thread = threading.Thread(
                target=run_ui_worker,
                args=(targets, commands, ui_inbound, state, stop_event),
                name="wechat-ui",
                daemon=True,
            )
            _runtime["ui_thread"] = ui_thread
            ui_thread.start()
            if not await _wait_for_ui_worker_startup(
                state,
                ui_thread,
                UI_WORKER_STARTUP_TIMEOUT_SECONDS,
            ):
                return
            if state["error"]:
                raise RuntimeError("微信 UI worker 启动失败") from state["error"]
        else:
            state["phase"] = "running_without_ui"

        if weflow_inbound:
            # 把 WeFlow 真 ID 和最新名称交给窗口层，改名后仍能命中原 HWND。
            def _record_recent_inbound(chat_name, session_id="", chat_type=""):
                listener = state.get("listener")
                recorder = getattr(listener, "record_recent_inbound", None)
                if recorder is not None:
                    recorder(chat_name, session_id, chat_type)

            weflow_listener = WeFlowListener(
                target_chats=[],
                callback=processor.enqueue_inbound_event,
                stop_event=stop_event,
                api_url=WEFLOW_API_URL,
                api_token=WEFLOW_API_TOKEN,
                outgoing_registry=outgoing_registry,
                recency_recorder=_record_recent_inbound,
                known_message_checker=processor.is_inbound_known,
            )
            state["weflow_listener"] = weflow_listener
            await asyncio.to_thread(weflow_listener.start, 20)

        logger.info("服务已就绪 mode=%s inbound_source=%s configured_chats=%s "
                    "listening_chats=%s "
                    "image_auto_download=%s watchdog_idle=%ss "
                    "watchdog_busy=%ss auto_restart=%s",
                    "双向" if inbound and outbound else ("微信到MaiBot" if inbound else "MaiBot到微信"),
                    "weflow" if weflow_inbound else ("ui" if ui_inbound else "disabled"),
                    targets, state["listening_targets"],
                    IMAGE_AUTO_DOWNLOAD,
                    UI_WORKER_IDLE_TIMEOUT_SECONDS,
                    UI_WORKER_BUSY_TIMEOUT_SECONDS,
                    UI_WORKER_AUTO_RESTART)
        while not stop_event.is_set():
            if ui_thread is not None and not ui_thread.is_alive():
                stop_event.set()
                if state["error"]:
                    error = RuntimeError("UI worker 异常结束")
                    log_exit("主循环检测到 UI worker 异常结束", state["error"])
                    raise error from state["error"]
                log_exit(state["ui_exit_reason"] or "UI worker 未知原因结束")
                break
            if weflow_listener is not None and not weflow_listener.is_running:
                stop_event.set()
                error = weflow_listener.startup_error
                log_exit("WeFlow SSE worker 异常结束", error)
                raise RuntimeError("WeFlow SSE worker 异常结束") from error
            if voice_backend is not None and not voice_backend.is_running:
                stop_event.set()
                error = voice_backend.last_error
                log_exit("WeMai 语音宿主异常结束", error)
                raise RuntimeError("WeMai 语音宿主异常结束") from error
            if optional_host.enabled and not optional_host.is_running:
                stop_event.set()
                error = optional_host.last_error
                log_exit("OCR 高级扩展异常结束", error)
                raise RuntimeError("OCR 高级扩展异常结束") from error
            if processor._thread and not processor._thread.is_alive():
                stop_event.set()
                log_exit("主循环检测到 Router worker 异常结束", processor.startup_error)
                raise RuntimeError("Router worker 异常结束") from processor.startup_error
            if ui_thread is not None:
                _raise_if_ui_worker_stalled(state, ui_thread)
                listener = state["listener"]
                ui_processor = getattr(listener, "processor", None)
                if ui_processor is not None and not ui_processor.is_running:
                    stop_event.set()
                    reason = "WeMai 消息监听处理器已停止运行"
                    log_exit(reason)
                    raise RuntimeError(reason)
            router_connected = bool(
                getattr(processor, "_router_connected", False)
            )
            if (not router_connected
                    and time.monotonic() - state["router_warning_at"] >= 30):
                state["router_warning_at"] = time.monotonic()
                logger.warning("Router 当前未连接，后台将持续尝试恢复")
            # 50 毫秒只检查状态和心跳，不执行微信 UI 操作。
            await asyncio.sleep(0.05)
        if state["error"] and not _runtime.get("shutdown_reason"):
            log_exit("停止事件由 UI worker 异常触发", state["error"])
            raise RuntimeError("UI worker 异常结束") from state["error"]
    except BaseException as exc:
        failure = exc
        raise
    finally:
        stop_event.set()
        stalled = isinstance(failure, UIWorkerStalledError)
        if weflow_listener is not None:
            weflow_listener.close(timeout=2 if stalled else 10)
        processor.stop(timeout=2 if stalled else 15)
        if voice_backend is not None:
            voice_backend.close(timeout=2 if stalled else 10)
        if optional_host.enabled:
            optional_host.close(timeout=2 if stalled else 10)
        if ui_thread:
            join_timeout = 1 if stalled else 10
            ui_thread.join(timeout=join_timeout)
            if ui_thread.is_alive():
                logger.error(
                    "UI worker 在 %s 秒内未退出；daemon 线程将随进程结束",
                    join_timeout,
                )
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
    return parser.parse_args(argv)


def cli_main():
    configure_logging()
    configure_exit_logging()
    print_config_info()
    exit_code = 0
    try:
        asyncio.run(main(parse_args()))
    except KeyboardInterrupt:
        log_exit("KeyboardInterrupt")
    except UIWorkerStalledError as exc:
        logger.critical("检测到微信 UI worker 卡死: %s", exc)
        if UI_WORKER_AUTO_RESTART:
            try:
                _restart_current_process()
            except Exception:
                logger.exception("自动重启 WeMai 进程失败")
        exit_code = 1
    except Exception as exc:
        logger.exception("程序异常退出")
        log_exit("程序异常退出", exc)
        exit_code = 1
    finally:
        _flush_logs()
        logging.shutdown()
    return exit_code


if __name__ == "__main__":
    sys.exit(cli_main())
