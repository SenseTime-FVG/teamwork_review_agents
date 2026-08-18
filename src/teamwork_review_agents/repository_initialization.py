"""管理基础 Git 仓库的初始化、更新、状态查询与取消。"""

from __future__ import annotations

import asyncio
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import RepositoryConfig
from .config_manager import ConfigManager
from .locks import LockCancelledError, LockTimeoutError, ResourceLease
from .workspace import (
    WorkspaceCancelled,
    WorkspaceError,
    initialize_repository_workspace,
    inspect_repository_workspace,
    repository_git_lock_key,
)


ACTIVE_STATUSES = {"waiting", "initializing", "updating"}


@dataclass
class RepositoryInitialization:
    """保存一个只存在于当前服务进程的基础仓库操作。"""

    repository_id: str
    workspace: str
    operation: str
    status: str = "waiting"
    phase: str = "等待仓库锁"
    elapsed_seconds: int = 0
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    error: str | None = None
    cancel_requested: bool = False
    cancel_event: threading.Event = field(default_factory=threading.Event, repr=False)
    task: asyncio.Task[None] | None = field(default=None, repr=False)


def _directory_size(path: Path) -> int:
    """计算基础仓库占用空间，不跟随符号链接。"""

    total = 0
    for root, directories, files in os.walk(path, followlinks=False):
        directories[:] = [
            name
            for name in directories
            if not (Path(root) / name).is_symlink()
        ]
        for name in files:
            candidate = Path(root) / name
            try:
                if not candidate.is_symlink():
                    total += candidate.stat().st_size
            except OSError:
                continue
    return total


class RepositoryInitializationManager:
    """在管理服务进程内编排基础仓库操作。"""

    def __init__(self, config_manager: ConfigManager) -> None:
        self.config_manager = config_manager
        self._operations: dict[str, RepositoryInitialization] = {}
        self._size_cache: dict[str, int] = {}
        self._lock = asyncio.Lock()

    def _repository(self, repository_id: str) -> RepositoryConfig | None:
        return self.config_manager.config.repository_map().get(repository_id)

    async def _inspect(
        self,
        repository: RepositoryConfig,
    ) -> tuple[bool, str | None, int | None]:
        path = repository.workspace.expanduser().resolve()
        try:
            ready, error = await asyncio.to_thread(
                inspect_repository_workspace,
                path,
                timeout_seconds=min(
                    10,
                    self.config_manager.config.runtime.git_timeout_seconds,
                ),
            )
        except WorkspaceError as exc:
            return False, str(exc), None
        size_bytes = None
        if ready:
            key = str(path)
            if key not in self._size_cache:
                self._size_cache[key] = await asyncio.to_thread(
                    _directory_size,
                    path,
                )
            size_bytes = self._size_cache[key]
        return ready, error, size_bytes

    async def _snapshot(
        self,
        repository: RepositoryConfig,
    ) -> dict[str, Any]:
        path = str(repository.workspace.expanduser().resolve())
        operation = self._operations.get(repository.id)
        if operation is not None and operation.workspace != path:
            operation = None
        ready, inspection_error, size_bytes = await self._inspect(repository)
        if operation is None:
            status = "ready" if ready else "invalid" if inspection_error else "uninitialized"
            phase = "基础仓库已就绪" if ready else inspection_error or "尚未初始化"
            return {
                "repository_id": repository.id,
                "workspace": path,
                "enabled": repository.enabled,
                "ready": ready,
                "status": status,
                "operation": None,
                "phase": phase,
                "elapsed_seconds": 0,
                "started_at": None,
                "finished_at": None,
                "size_bytes": size_bytes,
                "error": inspection_error,
                "cancel_requested": False,
            }
        status = operation.status
        phase = operation.phase
        error = operation.error or inspection_error
        if status == "ready" and not ready:
            status = "invalid"
        elapsed_seconds = operation.elapsed_seconds
        if operation.status in ACTIVE_STATUSES:
            elapsed_seconds = max(
                elapsed_seconds,
                int(time.time() - operation.started_at),
            )
        return {
            "repository_id": repository.id,
            "workspace": path,
            "enabled": repository.enabled,
            "ready": ready,
            "status": status,
            "operation": operation.operation,
            "phase": phase,
            "elapsed_seconds": elapsed_seconds,
            "started_at": operation.started_at,
            "finished_at": operation.finished_at,
            "size_bytes": size_bytes,
            "error": error,
            "cancel_requested": operation.cancel_requested,
        }

    async def list(self) -> list[dict[str, Any]]:
        """返回当前已保存仓库的基础目录状态。"""

        repositories = list(self.config_manager.config.repositories)
        return await asyncio.gather(
            *(self._snapshot(repository) for repository in repositories)
        )

    async def get(self, repository_id: str) -> dict[str, Any] | None:
        """返回单个已保存仓库的基础目录状态。"""

        repository = self._repository(repository_id)
        if repository is None:
            return None
        return await self._snapshot(repository)

    async def start(self, repository_id: str) -> dict[str, Any] | None:
        """启动初始化或更新；同仓库已有活动任务时直接复用。"""

        repository = self._repository(repository_id)
        if repository is None:
            return None
        if not repository.enabled:
            raise ValueError("仓库尚未启用，请先保存启用配置")
        path = str(repository.workspace.expanduser().resolve())
        async with self._lock:
            existing = self._operations.get(repository_id)
            if (
                existing is not None
                and existing.workspace == path
                and existing.status in ACTIVE_STATUSES
            ):
                return await self._snapshot(repository)
            ready, _, _ = await self._inspect(repository)
            operation = RepositoryInitialization(
                repository_id=repository.id,
                workspace=path,
                operation="update" if ready else "initialize",
            )
            self._operations[repository.id] = operation
            operation.task = asyncio.create_task(
                self._run(repository, operation),
                name=f"repository-initialize-{repository.id}-{uuid.uuid4()}",
            )
        return await self._snapshot(repository)

    async def _run(
        self,
        repository: RepositoryConfig,
        operation: RepositoryInitialization,
    ) -> None:
        """取得共享仓库锁并执行可取消的 Git 操作。"""

        config = self.config_manager.config
        provider = config.providers[repository.provider]
        lease = ResourceLease(
            self.config_manager.store,
            [repository_git_lock_key(repository)],
            f"repository-initialize:{repository.id}:{uuid.uuid4()}",
            ttl_seconds=config.runtime.lock_ttl_seconds,
            timeout_seconds=config.runtime.lock_timeout_seconds,
            cancel_check=operation.cancel_event.is_set,
        )
        try:
            async with lease:
                if operation.cancel_event.is_set():
                    raise WorkspaceCancelled("基础仓库操作已取消")
                operation.status = (
                    "updating" if operation.operation == "update" else "initializing"
                )
                operation.phase = (
                    "正在更新基础仓库"
                    if operation.operation == "update"
                    else "正在初始化基础仓库"
                )

                def progress(
                    git_operation: str,
                    state: str,
                    _elapsed_seconds: int,
                ) -> None:
                    """保存脱敏 Git 阶段，不接收命令、远端或输出。"""

                    operation.phase = git_operation
                    operation.elapsed_seconds = int(
                        time.time() - operation.started_at
                    )
                    if state == "timed_out":
                        operation.phase = f"{git_operation}超时"
                    elif state == "cancelled":
                        operation.phase = f"{git_operation}已取消"

                _, actual_operation = await asyncio.to_thread(
                    initialize_repository_workspace,
                    provider,
                    repository,
                    timeout_seconds=config.runtime.git_timeout_seconds,
                    cancel_check=operation.cancel_event.is_set,
                    progress_callback=progress,
                )
                operation.operation = actual_operation
                operation.status = "ready"
                operation.phase = "基础仓库已就绪"
                operation.error = None
        except (WorkspaceCancelled, LockCancelledError):
            operation.status = "cancelled"
            operation.phase = "基础仓库操作已取消"
            operation.error = None
        except (WorkspaceError, LockTimeoutError) as exc:
            operation.status = "failed"
            operation.phase = "基础仓库操作失败"
            operation.error = str(exc)
        except Exception:
            operation.status = "failed"
            operation.phase = "基础仓库操作失败"
            operation.error = "基础仓库操作发生未知错误"
        finally:
            operation.elapsed_seconds = max(
                operation.elapsed_seconds,
                int(time.time() - operation.started_at),
            )
            operation.finished_at = time.time()
            self._size_cache.pop(operation.workspace, None)

    async def cancel(self, repository_id: str) -> dict[str, Any] | None:
        """取消指定仓库当前仍在进行的初始化或更新。"""

        repository = self._repository(repository_id)
        if repository is None:
            return None
        operation = self._operations.get(repository_id)
        if operation is not None and operation.status in ACTIVE_STATUSES:
            operation.cancel_requested = True
            operation.cancel_event.set()
        return await self._snapshot(repository)

    async def close(self) -> None:
        """服务退出时取消并等待全部基础仓库任务结束。"""

        tasks = []
        for operation in self._operations.values():
            if operation.status in ACTIVE_STATUSES:
                operation.cancel_requested = True
                operation.cancel_event.set()
            if operation.task is not None and not operation.task.done():
                tasks.append(operation.task)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
