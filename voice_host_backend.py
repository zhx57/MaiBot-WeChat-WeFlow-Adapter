"""WeMai 本地语音宿主与独立窗口的统一出站入口。"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import queue
import subprocess
import threading
import uuid
from pathlib import Path

logger = logging.getLogger(__name__)

_VOICE_MAX_SECONDS = 60.0
_VOICE_COMMAND_TIMEOUT_SECONDS = 65.0


class OutboundUIGate:
    """语音录制独占微信界面，普通独立窗口发送可以并行。"""

    def __init__(self):
        self._condition = threading.Condition()
        self._active_sends = 0
        self._send_waiters = 0
        self._voice_active = False
        self._voice_waiters = 0

    def acquire_send(self):
        with self._condition:
            self._send_waiters += 1
            try:
                while self._voice_active:
                    self._condition.wait()
                self._active_sends += 1
            finally:
                self._send_waiters = max(0, self._send_waiters - 1)

    def release_send(self):
        with self._condition:
            self._active_sends = max(0, self._active_sends - 1)
            if self._active_sends == 0:
                self._condition.notify_all()

    def acquire_voice(self):
        with self._condition:
            self._voice_waiters += 1
            try:
                while self._voice_active or self._active_sends or self._send_waiters:
                    self._condition.wait()
                self._voice_active = True
            finally:
                self._voice_waiters = max(0, self._voice_waiters - 1)

    def release_voice(self):
        with self._condition:
            self._voice_active = False
            self._condition.notify_all()


class VoiceHostError(RuntimeError):
    """VoiceHost 尚未开始操作微信时发生的错误。"""

    retry_safe = True


class VoiceHostDeliveryUncertainError(VoiceHostError):
    """VoiceHost 已接收语音命令，不能自动重发。"""

    retry_safe = False
    cleanup_delay = 5.0


class WeMaiVoiceBackend:
    """自动管理 VoiceHost，并把所有出站统一到独立窗口。"""

    def __init__(
        self,
        *,
        subwindow_submit=None,
        outbound_ui_gate=None,
        executable=None,
        voice_timeout=_VOICE_COMMAND_TIMEOUT_SECONDS,
        max_voice_seconds=_VOICE_MAX_SECONDS,
    ):
        self.subwindow_submit = subwindow_submit
        self.outbound_ui_gate = outbound_ui_gate or OutboundUIGate()
        self.executable = Path(
            executable
            or Path(__file__).resolve().parent / "bin" / "WeMai.VoiceHost.exe"
        )
        self.voice_timeout = float(voice_timeout)
        self.max_voice_seconds = float(max_voice_seconds)

        self._process = None
        self._stdin_lock = threading.Lock()
        self._pending_lock = threading.Lock()
        self._pending = {}
        self._ready = threading.Event()
        self._stdout_thread = None
        self._stderr_thread = None
        self._stopping = threading.Event()
        self.last_error = None

    @property
    def is_running(self):
        process = self._process
        return bool(process is not None and process.poll() is None)

    def start(self, timeout=10):
        if self.is_running:
            return
        if not self.executable.is_file():
            raise VoiceHostError(f"找不到语音宿主：{self.executable}")

        self._ready.clear()
        self._stopping.clear()
        self.last_error = None
        creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            self._process = subprocess.Popen(
                [os.fspath(self.executable)],
                cwd=os.fspath(self.executable.parent),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                creationflags=creation_flags,
            )
        except OSError as exc:
            raise VoiceHostError(f"无法启动语音宿主：{exc}") from exc

        self._stdout_thread = threading.Thread(
            target=self._read_stdout,
            name="wemai-voice-response",
            daemon=True,
        )
        self._stderr_thread = threading.Thread(
            target=self._read_stderr,
            name="wemai-voice-log",
            daemon=True,
        )
        self._stdout_thread.start()
        self._stderr_thread.start()
        if not self._ready.wait(float(timeout)):
            self.close(timeout=2)
            raise VoiceHostError("语音宿主启动超时")
        if not self.is_running:
            raise VoiceHostError("语音宿主启动后立即退出")
        logger.info("WeMai 语音宿主已就绪 pid=%s", self._process.pid)
        try:
            self._request("diagnose", timeout=5.0)
            logger.info("语音设备检查通过：已找到 VB-CABLE")
        except VoiceHostError as exc:
            # 缺少虚拟声卡不影响文本、图片和文件，发送语音时仍会返回明确错误。
            logger.warning("语音设备检查未通过：%s", exc)

    def _read_stdout(self):
        process = self._process
        stream = getattr(process, "stdout", None)
        if stream is None:
            return
        try:
            for line in stream:
                try:
                    response = json.loads(line)
                except (TypeError, ValueError):
                    logger.warning("忽略语音宿主的无效响应：%r", line.rstrip())
                    continue
                if response.get("type") == "ready":
                    self._ready.set()
                    continue
                request_id = str(response.get("id", ""))
                with self._pending_lock:
                    result_queue = self._pending.get(request_id)
                if result_queue is not None:
                    result_queue.put(response)
        finally:
            self._ready.set()
            if not self._stopping.is_set():
                error = VoiceHostError("语音宿主连接已断开")
                self.last_error = error
                self._fail_pending(error)

    @staticmethod
    def _read_log_lines(stream):
        if stream is None:
            return
        for line in stream:
            text = line.rstrip()
            if text:
                logger.warning("VoiceHost: %s", text)

    def _read_stderr(self):
        self._read_log_lines(getattr(self._process, "stderr", None))

    def _fail_pending(self, error):
        with self._pending_lock:
            queues = list(self._pending.values())
        for result_queue in queues:
            with contextlib.suppress(queue.Full):
                result_queue.put_nowait(error)

    def _request(self, action, *, timeout=10, **payload):
        if not self.is_running:
            raise VoiceHostError("语音宿主未运行")
        request_id = uuid.uuid4().hex
        result_queue = queue.Queue(maxsize=1)
        with self._pending_lock:
            self._pending[request_id] = result_queue
        request = {
            "id": request_id,
            "action": action,
            **payload,
        }
        try:
            raw = json.dumps(request, ensure_ascii=False, separators=(",", ":"))
            with self._stdin_lock:
                stream = getattr(self._process, "stdin", None)
                if stream is None:
                    raise VoiceHostError("语音宿主输入流不可用")
                stream.write(raw + "\n")
                stream.flush()
            try:
                response = result_queue.get(timeout=float(timeout))
            except queue.Empty as exc:
                error = VoiceHostDeliveryUncertainError(
                    f"语音宿主命令在 {float(timeout):g} 秒内未完成"
                )
                self.last_error = error
                raise error from exc
            if isinstance(response, BaseException):
                raise response
            if not response.get("success"):
                error_type = (
                    VoiceHostError
                    if response.get("retrySafe") is True
                    else VoiceHostDeliveryUncertainError
                )
                error = error_type(str(response.get("error") or "语音宿主执行失败"))
                self.last_error = error
                raise error
            return response
        finally:
            with self._pending_lock:
                self._pending.pop(request_id, None)

    @staticmethod
    def _parse_window_handle(value):
        if isinstance(value, bool):
            raise VoiceHostError("WeMai UI 返回了无效的独立窗口句柄")
        try:
            handle = int(value)
        except (TypeError, ValueError) as exc:
            raise VoiceHostError("WeMai UI 返回了无效的独立窗口句柄") from exc
        if handle <= 0:
            raise VoiceHostError("WeMai UI 返回了无效的独立窗口句柄")
        return handle

    def submit(self, action, *args, timeout=None):
        if action != "send" or len(args) != 3:
            raise ValueError(f"不支持的微信命令：action={action!r} args={len(args)}")
        receiver, kind, data = args
        if kind != "voice":
            if kind not in {"text", "image", "file"}:
                raise ValueError(f"不支持的发送类型：{kind}")
            if self.subwindow_submit is None:
                raise VoiceHostError("WeMai 独立窗口发送器未配置")
            self.outbound_ui_gate.acquire_send()
            try:
                return self.subwindow_submit(
                    action,
                    receiver,
                    kind,
                    data,
                    timeout=float(timeout or 15),
                )
            finally:
                self.outbound_ui_gate.release_send()

        if self.subwindow_submit is None:
            raise VoiceHostError("WeMai 独立窗口句柄解析器未配置")
        if not self.is_running:
            self.start()
        absolute_path = os.path.abspath(os.fspath(data))
        inspection = self._request(
            "inspect_audio",
            timeout=5.0,
            filePath=absolute_path,
            maxSeconds=self.max_voice_seconds,
        )
        duration = float(inspection.get("durationSeconds", 0))
        logger.debug("语音文件校验通过 path=%s duration=%.2fs", absolute_path, duration)

        # 会话解析与文本、图片、文件共用同一条真 ID -> 独立窗口路径。
        # 只有实际录制阶段需要全局独占：右 Alt 按下前取得，松开并确认后释放。
        raw_handle = self.subwindow_submit(
            "resolve_window_handle",
            receiver,
            timeout=float(timeout or 15),
        )
        window_handle = self._parse_window_handle(raw_handle)
        logger.info("语音独立窗口已解析 target=%s hwnd=%s", receiver, window_handle)

        self.outbound_ui_gate.acquire_voice()
        release_voice_gate = True
        try:
            try:
                response = self._request(
                    "send_voice",
                    timeout=self.voice_timeout,
                    windowHandle=window_handle,
                    filePath=absolute_path,
                    maxSeconds=self.max_voice_seconds,
                )
            except VoiceHostDeliveryUncertainError:
                try:
                    # cancel 的响应只会在录音 finally 完成后返回。
                    self._request("cancel", timeout=3.0)
                except Exception:
                    release_voice_gate = False
                    logger.critical(
                        "无法确认微信录音已经停止；保持全局 UI 独占，"
                        "请通过 WebUI 重启 WeMai",
                        exc_info=True,
                    )
                raise
            logger.info(
                "独立窗口语音已发送 target=%s hwnd=%s duration=%.2fs elapsed=%.2fs "
                "right_alt_down=%s recording_started=%s right_alt_up=%s "
                "recording_stopped=%s",
                receiver,
                window_handle,
                float(response.get("durationSeconds", 0)),
                float(response.get("elapsedSeconds", 0)),
                response.get("rightAltDownInjected") is True,
                response.get("recordingStarted") is True,
                response.get("rightAltReleased") is True,
                response.get("recordingStopped") is True,
            )
            return True
        finally:
            if release_voice_gate:
                self.outbound_ui_gate.release_voice()

    def close(self, timeout=10):
        self._stopping.set()
        process = self._process
        if process is None:
            return
        if process.poll() is None:
            try:
                self._request("cancel", timeout=min(float(timeout), 3.0))
            except Exception:
                logger.debug("取消语音命令失败", exc_info=True)
            try:
                self._request("shutdown", timeout=min(float(timeout), 5.0))
            except Exception:
                logger.debug("关闭语音宿主命令失败", exc_info=True)
        try:
            process.wait(timeout=max(float(timeout), 0.5))
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
        for stream_name in ("stdin", "stdout", "stderr"):
            stream = getattr(process, stream_name, None)
            if stream is not None:
                with contextlib.suppress(OSError):
                    stream.close()
        self._process = None
        logger.info("WeMai 语音宿主已停止")
