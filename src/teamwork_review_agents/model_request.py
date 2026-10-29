"""模型请求的有界等待与安全诊断，不重放工具或关闭运行取消。"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import nullcontext
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

import httpx

from .run_control import active_run_control


Diagnostic = Callable[[str, dict[str, Any]], Awaitable[None]]


class ModelRequestTimeout(TimeoutError):
    """只结束当前模型请求，交由调用者进行有限恢复。"""

    def __init__(self, details: dict[str, Any]) -> None:
        self.details = details
        self.error_code = "model_request_timeout"
        kind = {"idle": "连续无响应", "total": "超过请求最长耗时", "transport": "HTTP 连接或读取超时"}[details["timeout_kind"]]
        super().__init__(f"模型请求超时：{kind}（阶段：{details['stage']}，耗时 {details['elapsed_seconds']:.1f} 秒）")


@dataclass
class _RequestState:
    """记录阶段与真实响应进展；状态仅存在于当前请求协程上下文。"""

    emit: Diagnostic
    started_at: float
    last_progress_at: float
    stage: str = "provider_queue"
    event_count: int = 0
    http_status: int | None = None
    upstream_request_id: str | None = None
    idle_scope: asyncio.Timeout | None = None
    idle_limit: float = 0

    def details(self) -> dict[str, Any]:
        """返回不包含正文或认证信息的诊断字段。"""

        now = asyncio.get_running_loop().time()
        return {
            "stage": self.stage, "elapsed_seconds": round(now - self.started_at, 3),
            "idle_seconds": round(now - self.last_progress_at, 3),
            "response_event_count": self.event_count,
            "http_status": self.http_status, "upstream_request_id": self.upstream_request_id,
        }


_active_request: ContextVar[_RequestState | None] = ContextVar("model_request", default=None)


async def record_model_transport(
    stage: str, *, http_status: int | None = None, upstream_request_id: str | None = None,
) -> None:
    """HTTP 客户端只上报阶段和白名单元数据，诊断不续期请求。"""

    state = _active_request.get()
    if state is None:
        return
    if state.idle_scope is not None:
        if stage == "version_probe":
            # CLI 升级等待已有独立有限预算；这段本地等待不能冒充上游静默。
            state.idle_scope.reschedule(None)
        elif state.stage == "version_probe":
            state.last_progress_at = asyncio.get_running_loop().time()
            state.idle_scope.reschedule(state.last_progress_at + state.idle_limit)
    state.stage = stage
    state.http_status = http_status
    state.upstream_request_id = upstream_request_id[:200] if upstream_request_id else None
    await state.emit("model.request_stage", state.details())


async def run_model_request(
    request: Callable[[Callable[[dict[str, Any]], Awaitable[None]]], Awaitable[dict[str, Any]]],
    *, callback: Callable[[dict[str, Any]], Awaitable[None]], emit: Diagnostic,
    idle_seconds: float, total_seconds: float, semaphore: asyncio.Semaphore | None = None,
) -> dict[str, Any]:
    """排队和请求共用有限预算，只有真实响应事件续期无进展计时。"""

    loop = asyncio.get_running_loop()
    state = _RequestState(emit, loop.time(), loop.time())
    token = _active_request.set(state)
    control = active_run_control.get()
    # 请求自身先到期；短暂余量只给清理让出调度机会，不延长请求期限。
    waiting = control.waiting_for_model(total_seconds + 1) if control else nullcontext()
    total = asyncio.timeout(total_seconds)
    idle = asyncio.timeout(idle_seconds)
    state.idle_scope = idle
    state.idle_limit = idle_seconds

    async def receive(event: dict[str, Any]) -> None:
        """真实协议事件计作进展，首次响应只记录元数据。"""

        state.last_progress_at = loop.time()
        state.event_count += 1
        state.stage = "receiving_response"
        idle.reschedule(state.last_progress_at + idle_seconds)
        if state.event_count == 1:
            await emit("model.request_first_event", state.details())
        await callback(event)

    try:
        with waiting:
            try:
                async with total, idle:
                    await emit("model.request_stage", state.details())
                    async with semaphore if semaphore is not None else nullcontext():
                        await record_model_transport("client_preparation")
                        response = await request(receive)
                        # 客户端即使吞掉取消，也不能把已经过期的响应冒充成功。
                        if total.expired() or idle.expired():
                            raise TimeoutError
                        if control:
                            control.raise_if_stopped()
                        await emit("model.request_completed", state.details())
                        return response
            except Exception as exc:
                cause: BaseException | None = exc
                transport_timeout = False
                while cause is not None:
                    transport_timeout = transport_timeout or isinstance(cause, httpx.TimeoutException)
                    cause = cause.__cause__
                if not total.expired() and not idle.expired() and not transport_timeout:
                    raise
                details = {**state.details(), "timeout_kind": "total" if total.expired() else "idle" if idle.expired() else "transport"}
                await emit("model.request_timed_out", details)
                raise ModelRequestTimeout(details) from exc
    finally:
        _active_request.reset(token)
