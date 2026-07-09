"""WeFlow SSE 异步客户端（aiohttp），自动重连。

对应 spec「WeFlow SSE 消息接收」：
- 建立 ``{base_url}/api/v1/push/messages?access_token={token}`` 长连接
- 带 ``Accept: text/event-stream`` / ``Cache-Control: no-cache`` 头
- 逐行解析 ``data: {json}`` 并回调 ``on_message``
- 401 记录 error 日志，不上报 ready
- 断线按 ``reconnect_delay_sec`` 自动重连
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Awaitable, Callable, Optional

import aiohttp

from .constants import HEALTH_PATH, SSE_PUSH_PATH


class WeFlowSSEClient:
    """WeFlow SSE 长连接客户端，自动重连。"""

    def __init__(
        self,
        base_url: str,
        access_token: str,
        request_timeout: float,
        reconnect_delay_sec: int,
        on_message: Callable[[dict[str, Any]], Awaitable[None]],
        on_connected: Callable[[], Awaitable[None]],
        on_disconnected: Callable[[str], Awaitable[None]],
        logger: logging.Logger,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._access_token = access_token
        self._request_timeout = request_timeout
        self._reconnect_delay_sec = reconnect_delay_sec
        self._on_message = on_message
        self._on_connected = on_connected
        self._on_disconnected = on_disconnected
        self._logger = logger

        self._session: Optional[aiohttp.ClientSession] = None
        self._response: Optional[aiohttp.ClientResponse] = None
        self._task: Optional[asyncio.Task[None]] = None
        self._stop_requested = False
        # 是否已通过 on_connected 上报过 ready，用于区分「初次 401」与「断线后重连失败」
        self._was_connected = False

    # ---------- URL 与 session 管理 ----------

    @property
    def _stream_url(self) -> str:
        return f"{self._base_url}{SSE_PUSH_PATH}?access_token={self._access_token}"

    @property
    def _probe_url(self) -> str:
        # 用健康检查端点探活：免 access_token，避免 /api/v1/messages 因缺 talker 参数返回 400。
        # access_token 有效性由 SSE 连接（_stream_once）的 401 处理覆盖。
        return f"{self._base_url}{HEALTH_PATH}"

    def _ensure_session(self) -> aiohttp.ClientSession:
        """惰性创建/复用 ClientSession。"""

        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=self._request_timeout)
            self._session = aiohttp.ClientSession(timeout=timeout)
        return self._session

    # ---------- SSE 行解析 ----------

    @staticmethod
    def _parse_data_line(line: bytes) -> Optional[dict[str, Any]]:
        """解析单行 SSE ``data:`` 负载。非 data 行或解析失败返回 None。"""

        stripped = line.strip()
        if not stripped:
            return None
        if not stripped.startswith(b"data:"):
            # SSE 其他行类型（event:/id:/retry:/注释 ``:``）忽略
            return None
        payload = stripped[5:].lstrip()
        if not payload:
            return None
        try:
            data = json.loads(payload.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None
        if not isinstance(data, dict):
            return None
        return data

    # ---------- 对外接口 ----------

    async def probe(self) -> bool:
        """GET 健康检查探活。

        用 ``/api/v1/health``（免 token）确认 WeFlow 在线。
        ``access_token`` 有效性由后续 SSE 连接的 401 处理覆盖。
        """

        try:
            session = self._ensure_session()
            async with session.get(self._probe_url) as resp:
                if resp.status == 200:
                    return True
                self._logger.error("WeFlow 探活失败：HTTP %s", resp.status)
                return False
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self._logger.error("WeFlow 探活异常：%s", e)
            return False

    async def start(self) -> None:
        """启动后台 SSE 循环。幂等（已运行则跳过）。"""

        if self._task is not None and not self._task.done():
            return
        self._stop_requested = False
        self._task = asyncio.create_task(self._run_loop())

    async def stop(self) -> None:
        """停止 SSE：置 stop 标志、关闭响应与 session、取消任务。"""

        self._stop_requested = True

        # 关闭当前响应以中断流读取
        if self._response is not None and not self._response.closed:
            try:
                self._response.close()
            except Exception:
                pass
        self._response = None

        # 关闭 session
        if self._session is not None and not self._session.closed:
            try:
                await self._session.close()
            except Exception:
                pass
        self._session = None

        # 取消任务并等待退出
        if self._task is not None and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            except Exception as e:
                self._logger.debug("SSE 任务退出异常：%s", e)
        self._task = None
        self._was_connected = False

    # ---------- 内部循环 ----------

    async def _run_loop(self) -> None:
        """主循环：探活→连接→流式接收→断线重连。"""

        while not self._stop_requested:
            ok = await self.probe()
            if self._stop_requested:
                break
            if not ok:
                # 探活失败（含 401）：不上报 ready，等待后重连
                if not self._stop_requested:
                    await asyncio.sleep(self._reconnect_delay_sec)
                continue

            stream_ended_normally = False
            try:
                stream_ended_normally = await self._stream_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                if not self._stop_requested:
                    self._logger.warning("WeFlow SSE 异常：%s", e)
                    if self._was_connected:
                        self._was_connected = False
                        await self._safe_callback(
                            self._on_disconnected, str(e), label="on_disconnected"
                        )
            else:
                # 流正常结束（EOF）：若曾连接，上报断开
                if stream_ended_normally and self._was_connected and not self._stop_requested:
                    self._was_connected = False
                    await self._safe_callback(
                        self._on_disconnected, "SSE 流结束", label="on_disconnected"
                    )
            finally:
                # 清理当前响应引用
                if self._response is not None and not self._response.closed:
                    try:
                        self._response.close()
                    except Exception:
                        pass
                self._response = None

            # 重连前等待（stop 时不等）
            if not self._stop_requested:
                await asyncio.sleep(self._reconnect_delay_sec)

    async def _stream_once(self) -> bool:
        """单次 SSE 流接收。返回 True 表示流正常结束（EOF），False 表示未建立流。"""

        session = self._ensure_session()
        # SSE 长连接不设总超时
        stream_timeout = aiohttp.ClientTimeout(total=None)
        headers = {"Accept": "text/event-stream", "Cache-Control": "no-cache"}

        resp = await session.get(self._stream_url, headers=headers, timeout=stream_timeout)
        self._response = resp
        try:
            if resp.status == 401:
                self._logger.error("WeFlow access_token 无效（HTTP 401）")
                return False
            if resp.status != 200:
                self._logger.error("WeFlow SSE 连接失败：HTTP %s", resp.status)
                return False

            # 上报已连接
            self._was_connected = True
            await self._safe_callback(self._on_connected, label="on_connected")

            # 逐行解析：aiohttp StreamReader 的 __aiter__ 按 readline() 切行，每行含末尾换行
            async for line in resp.content:
                if self._stop_requested:
                    break
                parsed = self._parse_data_line(line)
                if parsed is None:
                    continue
                await self._safe_callback(self._on_message, parsed, label="on_message")
            return True
        finally:
            if not resp.closed:
                try:
                    resp.close()
                except Exception:
                    pass
            if self._response is resp:
                self._response = None

    async def _safe_callback(
        self,
        fn: Callable[..., Awaitable[None]],
        *args: Any,
        label: str = "",
    ) -> None:
        """安全调用回调，异常不污染主循环。"""

        try:
            await fn(*args)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self._logger.debug("回调 %s 异常：%s", label or repr(fn), e)
