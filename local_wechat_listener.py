"""监督只读微信数据库 sidecar。

sidecar 输出标准化 JSON 行；本模块把它转换为 WeMai 的 ``_InboundEvent``，
仅在 Python 可靠队列成功落盘后确认消息。
"""

from __future__ import annotations

import json
import logging
import subprocess
import threading
import time
from pathlib import Path

from local_inbound_credentials import load_credentials
from unicode_utils import sanitize_unicode
from wx_Listener import _InboundEvent

logger = logging.getLogger(__name__)


class LocalWechatListener:
    """运行一个本地数据库 sidecar，并在异常时退避重启。"""

    def __init__(
        self,
        *,
        callback,
        stop_event,
        credentials_file,
        node_executable,
        sidecar_script,
        media_dir,
        state_file,
        native_image_addon,
        dispatch_workers=8,
        poll_interval_ms=250,
        media_wait_seconds=120,
        allowlist=(),
        blocklist=(),
        recency_recorder=None,
        account_profile_callback=None,
    ):
        self.callback = callback
        self.stop_event = stop_event
        self.credentials_file = str(credentials_file)
        self.node_executable = str(node_executable)
        self.sidecar_script = str(sidecar_script)
        self.media_dir = str(media_dir)
        self.state_file = str(state_file)
        self.native_image_addon = str(native_image_addon)
        self.dispatch_workers = int(dispatch_workers)
        self.poll_interval_ms = int(poll_interval_ms)
        self.media_wait_seconds = int(media_wait_seconds)
        self.allowlist = list(allowlist or ())
        self.blocklist = list(blocklist or ())
        self.recency_recorder = recency_recorder
        self.account_profile_callback = account_profile_callback
        self.ready_event = threading.Event()
        self.startup_error = None
        self.running = False
        self._thread = None
        self._process = None
        self._process_lock = threading.RLock()
        self._stdin_lock = threading.Lock()

    @property
    def thread(self):
        return self._thread

    @property
    def is_running(self):
        return self.running and self._thread is not None and self._thread.is_alive()

    def start(self, timeout=20):
        if self._thread and self._thread.is_alive():
            return
        self.ready_event.clear()
        self.startup_error = None
        self.running = True
        self._thread = threading.Thread(
            target=self._run_forever,
            name="local-wechat-inbound",
            daemon=True,
        )
        self._thread.start()
        if not self.ready_event.wait(timeout):
            self.close(timeout=5)
            detail = f": {self.startup_error}" if self.startup_error else ""
            raise TimeoutError(f"本地微信数据库入站启动超时{detail}")
        if self.startup_error is not None and not self.is_running:
            raise RuntimeError("本地微信数据库入站启动失败") from self.startup_error

    def close(self, timeout=10):
        self.stop_event.set()
        self.running = False
        with self._process_lock:
            process = self._process
        if process is not None and process.poll() is None:
            self._send_control({"type": "stop"}, process=process, suppress_errors=True)
            try:
                process.wait(timeout=min(float(timeout), 3.0))
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout)

    def _sidecar_config(self):
        credentials = load_credentials(self.credentials_file)
        return {
            "accountDir": credentials["account_dir"],
            "wxid": credentials["wxid"],
            "dbKeyHex": credentials["db_key"],
            "imageXorKey": credentials["image_xor_key"],
            "imageAesKey": credentials["image_aes_key"],
            "mediaDir": str(Path(self.media_dir).resolve()),
            "statePath": str(Path(self.state_file).resolve()),
            "nativeImageAddonPath": str(Path(self.native_image_addon).resolve()),
            "dispatchWorkers": self.dispatch_workers,
            "pollIntervalMs": self.poll_interval_ms,
            "mediaWaitTimeoutMs": self.media_wait_seconds * 1000,
            "allowlist": self.allowlist,
            "blocklist": self.blocklist,
        }

    def _spawn(self):
        creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        process = subprocess.Popen(
            [self.node_executable, self.sidecar_script],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=creation_flags,
        )
        with self._process_lock:
            self._process = process
        threading.Thread(
            target=self._consume_stderr,
            args=(process,),
            name="local-wechat-stderr",
            daemon=True,
        ).start()
        self._send_control(self._sidecar_config(), process=process)
        return process

    def _run_forever(self):
        restart_delay = 1.0
        try:
            while self.running and not self.stop_event.is_set():
                process = None
                became_ready = False
                try:
                    process = self._spawn()
                    became_ready = self._consume_stdout(process)
                    if self.stop_event.is_set() or not self.running:
                        break
                    code = process.poll()
                    raise RuntimeError(f"本地入站 sidecar 异常退出，退出码 {code}")
                except Exception as exc:
                    self.startup_error = exc
                    if self.stop_event.is_set() or not self.running:
                        break
                    logger.warning(
                        "本地微信数据库入站中断，将在 %.1f 秒后重启: %s",
                        restart_delay,
                        exc,
                    )
                    if self.stop_event.wait(restart_delay):
                        break
                    restart_delay = min(30.0, restart_delay * 2)
                finally:
                    if process is not None and process.poll() is None:
                        process.terminate()
                        try:
                            process.wait(timeout=2)
                        except subprocess.TimeoutExpired:
                            process.kill()
                    with self._process_lock:
                        if self._process is process:
                            self._process = None
                if became_ready:
                    restart_delay = 1.0
        finally:
            self.running = False
            self.ready_event.set()

    def _consume_stdout(self, process):
        became_ready = False
        assert process.stdout is not None
        for raw_line in process.stdout:
            if self.stop_event.is_set() or not self.running:
                return became_ready
            line = raw_line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                logger.warning("忽略本地入站 sidecar 的无效 JSON")
                continue
            kind = str(message.get("type") or "")
            if kind == "ready":
                self._handle_account_profile(message.get("accountProfile"))
                became_ready = True
                self.startup_error = None
                self.ready_event.set()
                logger.info(
                    "本地微信数据库入站已就绪 sessions=%s tables=%s baseline=%s "
                    "allowlist=%s blocklist=%s",
                    message.get("sessions"),
                    message.get("messageTables"),
                    message.get("sourceBaseline"),
                    message.get("allowlistEntries", 0),
                    message.get("blocklistEntries", 0),
                )
            elif kind == "account_profile":
                self._handle_account_profile(message.get("accountProfile"))
            elif kind == "event":
                self._handle_event(process, message)
            elif kind == "warning":
                logger.warning("本地入站 sidecar: %s", message.get("error"))
            elif kind == "fatal":
                raise RuntimeError(str(message.get("error") or "本地入站 sidecar 致命错误"))
            elif kind == "heartbeat":
                logger.debug(
                    "本地入站心跳 workers=%s pending_sessions=%s filtered=%s",
                    message.get("activeWorkers"),
                    message.get("pendingSessions"),
                    message.get("filteredEvents", 0),
                )
        return became_ready

    def _handle_account_profile(self, profile):
        if not isinstance(profile, dict):
            return
        callback = self.account_profile_callback
        if callback is None:
            return
        callback(profile)

    def _consume_stderr(self, process):
        if process.stderr is None:
            return
        for line in process.stderr:
            cleaned = line.strip()
            if cleaned:
                logger.warning("本地入站 sidecar stderr: %s", cleaned[:1000])

    def _handle_event(self, process, message):
        received_at = time.time()
        message, repaired_units = sanitize_unicode(message)
        if repaired_units:
            logger.warning(
                "本地微信事件包含不完整 UTF-16 字符，已安全修复 units=%d event_id=%s",
                repaired_units,
                message.get("eventId"),
            )
        event_id = str(message.get("eventId") or "")
        payload = message.get("event")
        if not event_id or not isinstance(payload, dict):
            raise ValueError("本地入站事件缺少 eventId 或 payload")
        try:
            raw = payload.get("raw") if isinstance(payload.get("raw"), dict) else {}
            event = _InboundEvent(
                group=str(payload.get("chatName") or payload.get("sessionId") or ""),
                content=str(payload.get("content") or ""),
                timestamp=float(payload.get("timestamp") or time.time()),
                raw=raw,
                message_type=str(payload.get("messageType") or "text"),
                file_name=str(payload.get("fileName") or ""),
                sender=str(payload.get("sender") or "未知发送者"),
                chat_type=str(payload.get("chatType") or ""),
                avatar_url=str(payload.get("avatarUrl") or ""),
                group_avatar_url=str(payload.get("groupAvatarUrl") or ""),
                session_id=str(payload.get("sessionId") or ""),
                sender_wxid=str(payload.get("senderWxid") or ""),
                user_nickname=str(payload.get("userNickname") or ""),
                group_card_name=str(payload.get("groupCardName") or ""),
            )
            recorder = self.recency_recorder
            if recorder is not None:
                recorder(event.group, event.session_id, event.chat_type)
            result = self.callback(event)
            if isinstance(result, dict) and not result.get("success"):
                raise RuntimeError(result.get("error") or "入站消息持久化失败")
            self._send_control({"type": "ack", "eventId": event_id}, process=process)
            source_timestamp = event.timestamp / 1000 if event.timestamp > 10_000_000_000 else event.timestamp
            timing = message.get("timing") if isinstance(message.get("timing"), dict) else {}
            emitted_at_ms = float(timing.get("emittedAtMs") or received_at * 1000)
            logger.info(
                "收到本地微信消息 chat=%s session=%s chat_type=%s sender=%s "
                "type=%s rawid=%s source_lag=%.3fs "
                "inbound_trigger=%s trigger_wait=%.3fms structure=%.3fms "
                "query=%.3fms content_parse=%.3fms identity=%.3fms "
                "parse_total=%.3fms scan=%.3fms media=%.3fms "
                "dispatch_queue=%.3fms node_python=%.3fms python_callback=%.3fms "
                "targeted=%s database=%s",
                event.group,
                event.session_id,
                event.chat_type,
                event.sender,
                event.message_type,
                raw.get("rawid"),
                max(time.time() - source_timestamp, 0.0),
                timing.get("trigger", "unknown"),
                max(float(timing.get("scanStartedAtMs") or 0) - float(timing.get("requestedAtMs") or 0), 0.0),
                float(timing.get("structureMs") or 0),
                float(timing.get("queryMs") or 0),
                float(timing.get("contentParseMs") or 0),
                float(timing.get("identityMs") or 0),
                float(timing.get("parseMs") or 0),
                float(timing.get("scanMs") or 0),
                float(timing.get("mediaMs") or 0),
                float(timing.get("dispatchQueueMs") or 0),
                max(received_at * 1000 - emitted_at_ms, 0.0),
                max(time.time() - received_at, 0.0) * 1000,
                bool(timing.get("targeted")),
                timing.get("databaseName", ""),
            )
        except Exception as exc:
            self._send_control(
                {"type": "nack", "eventId": event_id, "error": str(exc)[:500]},
                process=process,
                suppress_errors=True,
            )
            raise

    def _send_control(self, value, *, process=None, suppress_errors=False):
        process = process or self._process
        if process is None or process.stdin is None or process.poll() is not None:
            if suppress_errors:
                return
            raise RuntimeError("本地入站 sidecar 不可写")
        try:
            line = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
            with self._stdin_lock:
                process.stdin.write(line + "\n")
                process.stdin.flush()
        except (BrokenPipeError, OSError):
            if not suppress_errors:
                raise
