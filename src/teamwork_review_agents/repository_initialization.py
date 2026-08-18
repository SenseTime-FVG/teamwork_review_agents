"""管理基础 Git 仓库的初始化、更新、状态查询与取消。"""

from __future__ import annotations

import asyncio
import json
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
    GitProgressEvent,
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
    commands: dict[str, dict[str, Any]] = field(default_factory=dict, repr=False)
    command_order: list[str] = field(default_factory=list, repr=False)
    command_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    cancel_event: threading.Event = field(default_factory=threading.Event, repr=False)
    task: asyncio.Task[None] | None = field(default=None, repr=False)

    def record_command(self, command: dict[str, Any]) -> None:
        """按命令 ID 更新状态，同时保持首次出现顺序。"""

        command_id = str(command["command_id"])
        with self.command_lock:
            if command_id not in self.commands:
                self.command_order.append(command_id)
            self.commands[command_id] = command

    def command_snapshots(self) -> list[dict[str, Any]]:
        """返回当前线程安全的命令状态副本。"""

        with self.command_lock:
            return [dict(self.commands[item]) for item in self.command_order]


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

    async def _latest_agent_run(
        self,
        repository_id: str,
    ) -> dict[str, Any] | None:
        """返回该仓库最近一次 Agent 运行摘要。"""

        runs = await asyncio.to_thread(
            self.config_manager.store.list_runs,
            1,
            repository_id=repository_id,
        )
        return runs[0] if runs else None

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
        agent_run = await self._latest_agent_run(repository.id)
        ready, inspection_error, size_bytes = await self._inspect(repository)
        operation_started_at = operation.started_at if operation is not None else 0
        agent_started_at = float(agent_run.get("started_at") or 0) if agent_run else 0
        operation_is_active = (
            operation is not None and operation.status in ACTIVE_STATUSES
        )
        detail_source = (
            "manual"
            if operation is not None
            and (operation_is_active or operation_started_at >= agent_started_at)
            else "agent"
            if agent_run is not None
            else None
        )
        detail_run_id = (
            str(agent_run["run_id"])
            if detail_source == "agent" and agent_run is not None
            else None
        )
        if detail_source != "manual":
            status = "ready" if ready else "invalid" if inspection_error else "uninitialized"
            phase = "基础仓库已就绪" if ready else inspection_error or "尚未初始化"
            elapsed_seconds = 0
            if agent_run is not None and agent_run.get("status") == "preparing":
                status = "updating" if ready else "initializing"
                phase = (
                    "Agent 正在准备 Git 工作区"
                    if ready
                    else "Agent 正在初始化基础仓库"
                )
                elapsed_seconds = max(0, int(time.time() - agent_started_at))
            return {
                "repository_id": repository.id,
                "workspace": path,
                "enabled": repository.enabled,
                "ready": ready,
                "status": status,
                "operation": None,
                "phase": phase,
                "elapsed_seconds": elapsed_seconds,
                "started_at": agent_run.get("started_at") if agent_run else None,
                "finished_at": agent_run.get("finished_at") if agent_run else None,
                "size_bytes": size_bytes,
                "error": inspection_error,
                "cancel_requested": False,
                "detail_available": detail_source is not None,
                "detail_source": detail_source,
                "detail_run_id": detail_run_id,
            }
        assert operation is not None
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
            "detail_available": detail_source is not None,
            "detail_source": detail_source,
            "detail_run_id": detail_run_id,
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

    async def detail(self, repository_id: str) -> dict[str, Any] | None:
        """返回最近一次手动或 Agent Git 操作的脱敏命令详情。"""

        repository = self._repository(repository_id)
        if repository is None:
            return None
        operation = self._operations.get(repository_id)
        if operation is not None and operation.workspace != str(
            repository.workspace.expanduser().resolve()
        ):
            operation = None
        agent_run = await self._latest_agent_run(repository_id)
        agent_started_at = float(agent_run.get("started_at") or 0) if agent_run else 0
        if operation is not None and (
            operation.status in ACTIVE_STATUSES
            or operation.started_at >= agent_started_at
        ):
            return {
                "repository_id": repository_id,
                "source": "manual",
                "run_id": None,
                "status": operation.status,
                "phase": operation.phase,
                "started_at": operation.started_at,
                "finished_at": operation.finished_at,
                "commands": operation.command_snapshots(),
            }
        if agent_run is None:
            return {
                "repository_id": repository_id,
                "source": None,
                "run_id": None,
                "status": "uninitialized",
                "phase": "还没有可查看的 Git 操作",
                "started_at": None,
                "finished_at": None,
                "commands": [],
            }
        logs = await asyncio.to_thread(
            self.config_manager.store.list_run_logs,
            str(agent_run["run_id"]),
            limit=2000,
        )
        commands: dict[str, dict[str, Any]] = {}
        order: list[str] = []
        for log in logs:
            if not str(log.get("event_type") or "").startswith("workspace.git."):
                continue
            try:
                payload = json.loads(str(log.get("payload") or "{}"))
            except (TypeError, json.JSONDecodeError):
                continue
            if not isinstance(payload, dict) or not payload.get("command_id"):
                continue
            command_id = str(payload["command_id"])
            if command_id not in commands:
                order.append(command_id)
            commands[command_id] = payload
        return {
            "repository_id": repository_id,
            "source": "agent",
            "run_id": agent_run["run_id"],
            "status": agent_run["status"],
            "phase": "Agent Git 工作区准备",
            "started_at": agent_run["started_at"],
            "finished_at": agent_run["finished_at"],
            "commands": [commands[item] for item in order],
        }

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
            operation.record_command(
                {
                    "command_id": "repository-lock",
                    "operation": "等待仓库锁",
                    "command": "",
                    "state": "waiting",
                    "elapsed_seconds": 0,
                    "timeout_seconds": (
                        self.config_manager.config.runtime.lock_timeout_seconds
                    ),
                    "started_at": operation.started_at,
                    "finished_at": None,
                    "exit_code": None,
                    "error": None,
                }
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
                operation.record_command(
                    {
                        "command_id": "repository-lock",
                        "operation": "等待仓库锁",
                        "command": "",
                        "state": "completed",
                        "elapsed_seconds": int(time.time() - operation.started_at),
                        "timeout_seconds": config.runtime.lock_timeout_seconds,
                        "started_at": operation.started_at,
                        "finished_at": time.time(),
                        "exit_code": None,
                        "error": None,
                    }
                )
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

                def progress(git_event: GitProgressEvent) -> None:
                    """保存脱敏 Git 阶段，不接收命令、远端或输出。"""

                    operation.record_command(git_event.as_dict())
                    operation.phase = git_event.operation
                    operation.elapsed_seconds = int(
                        time.time() - operation.started_at
                    )
                    if git_event.state == "timed_out":
                        operation.phase = f"{git_event.operation}超时"
                    elif git_event.state == "cancelled":
                        operation.phase = f"{git_event.operation}已取消"

                _, actual_operation = await asyncio.to_thread(
                    initialize_repository_workspace,
                    provider,
                    repository,
                    timeout_seconds=config.runtime.git_timeout_seconds,
                    initialization_timeout_seconds=(
                        config.runtime.repository_initialization_timeout_seconds
                    ),
                    cancel_check=operation.cancel_event.is_set,
                    progress_callback=progress,
                )
                operation.operation = actual_operation
                operation.status = "ready"
                operation.phase = "基础仓库已就绪"
                operation.error = None
        except (WorkspaceCancelled, LockCancelledError):
            self._finish_lock_command(
                operation,
                "cancelled",
                "等待仓库锁的操作已取消",
            )
            operation.status = "cancelled"
            operation.phase = "基础仓库操作已取消"
            operation.error = None
        except (WorkspaceError, LockTimeoutError) as exc:
            self._finish_lock_command(operation, "failed", str(exc))
            operation.status = "failed"
            operation.phase = "基础仓库操作失败"
            operation.error = str(exc)
        except Exception:
            self._finish_lock_command(
                operation,
                "failed",
                "基础仓库操作发生未知错误",
            )
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

    def _finish_lock_command(
        self,
        operation: RepositoryInitialization,
        state: str,
        error: str,
    ) -> None:
        """仅在仍等待锁时结束锁步骤，避免覆盖已经完成的记录。"""

        lock_command = next(
            (
                item
                for item in operation.command_snapshots()
                if item["command_id"] == "repository-lock"
            ),
            None,
        )
        if lock_command is None or lock_command["state"] != "waiting":
            return
        operation.record_command(
            {
                **lock_command,
                "state": state,
                "elapsed_seconds": int(time.time() - operation.started_at),
                "finished_at": time.time(),
                "error": error,
            }
        )

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
