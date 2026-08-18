"""基于 SQLite 租约的异步资源锁。"""

from __future__ import annotations

import asyncio
import time
from contextlib import suppress
from types import TracebackType
from typing import Callable

from .state import StateStore


class LockTimeoutError(TimeoutError):
    """在限定时间内无法取得全部写资源。"""


class LockCancelledError(RuntimeError):
    """表示资源锁等待被调用方主动取消。"""


class ResourceLease:
    """申请多个资源锁，并在 Agent 运行期间定时续租。"""

    def __init__(
        self,
        store: StateStore,
        resource_keys: list[str],
        owner: str,
        *,
        ttl_seconds: int,
        timeout_seconds: int,
        cancel_check: Callable[[], bool] | None = None,
    ) -> None:
        self.store = store
        self.resource_keys = sorted(set(resource_keys))
        self.owner = owner
        self.ttl_seconds = ttl_seconds
        self.timeout_seconds = timeout_seconds
        self.cancel_check = cancel_check
        self.lost = False
        self._heartbeat_task: asyncio.Task[None] | None = None

    async def __aenter__(self) -> "ResourceLease":
        if not self.resource_keys:
            return self
        deadline = time.monotonic() + self.timeout_seconds
        while True:
            if self.cancel_check is not None and self.cancel_check():
                raise LockCancelledError("等待资源锁的操作已取消")
            acquired = await asyncio.to_thread(
                self.store.acquire_locks,
                self.resource_keys,
                self.owner,
                self.ttl_seconds,
            )
            if acquired:
                self._heartbeat_task = asyncio.create_task(self._heartbeat())
                return self
            if time.monotonic() >= deadline:
                raise LockTimeoutError(
                    f"等待写资源锁超时：{', '.join(self.resource_keys)}"
                )
            await asyncio.sleep(0.5)

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._heartbeat_task
        if self.resource_keys:
            await asyncio.to_thread(
                self.store.release_locks,
                self.resource_keys,
                self.owner,
            )

    async def _heartbeat(self) -> None:
        """按租期三分之一的频率续租。"""

        interval = max(1.0, self.ttl_seconds / 3)
        while True:
            await asyncio.sleep(interval)
            renewed = await asyncio.to_thread(
                self.store.renew_locks,
                self.resource_keys,
                self.owner,
                self.ttl_seconds,
            )
            if not renewed:
                self.lost = True
                return
