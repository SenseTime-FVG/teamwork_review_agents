"""管理仓库默认分支的 Agent 工作区手动预热。"""

from __future__ import annotations

import asyncio
import json
import threading
import time
import uuid
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .agent_workspace import prepare_agent_workspace
from .config import AgentConfig, RepositoryConfig
from .config_manager import ConfigManager
from .environment import SecretRedactor, resolve_repository_process_environment
from .locks import LockCancelledError, LockTimeoutError, ResourceLease
from .preflight import build_preflight_environment
from .workspace import (
    GitProgressEvent,
    WorkspaceCancelled,
    repository_git_lock_key,
    temporary_default_branch_worktree,
)
from .workspace_snapshot import inspect_workspace_snapshots


ACTIVE_WARMUP_STATUSES = {"waiting", "preparing"}
MAX_WARMUP_LOG_BYTES = 1_000_000


def _persistent_phase(status: str) -> str:
    """把持久快照状态转换为仓库页提示。"""

    return {
        "disabled": "仓库级缓存未启用",
        "unconfigured": "尚未配置准备步骤",
        "ready": "依赖快照已就绪",
        "outdated": "准备配置或依赖清单已变化，需要重新预热",
        "uninitialized": "尚未创建依赖快照",
    }.get(status, "等待预热")


@dataclass
class AgentWorkspaceWarmup:
    """保存当前服务进程内一次预热操作的状态与有界日志。"""

    repository_id: str
    status: str = "waiting"
    phase: str = "等待仓库锁"
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    error: str | None = None
    cancel_requested: bool = False
    branch: str | None = None
    head_sha: str | None = None
    snapshot_status: str | None = None
    snapshot_fingerprint: str | None = None
    logs: list[dict[str, Any]] = field(default_factory=list, repr=False)
    log_bytes: int = 0
    log_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    cancel_event: threading.Event = field(default_factory=threading.Event, repr=False)
    task: asyncio.Task[None] | None = field(default=None, repr=False)

    def append_log(self, stream: str, event_type: str, payload: Any) -> None:
        """追加脱敏日志，并从最旧记录开始执行字节上限清理。"""

        encoded = json.dumps(payload, ensure_ascii=False, default=str)
        entry = {
            "id": uuid.uuid4().hex,
            "stream": stream,
            "event_type": event_type,
            "payload": payload,
            "created_at": time.time(),
        }
        size = len(encoded.encode("utf-8"))
        with self.log_lock:
            self.logs.append(entry)
            self.log_bytes += size
            while self.logs and self.log_bytes > MAX_WARMUP_LOG_BYTES:
                removed = self.logs.pop(0)
                self.log_bytes -= len(
                    json.dumps(
                        removed["payload"],
                        ensure_ascii=False,
                        default=str,
                    ).encode("utf-8")
                )

    def log_snapshot(self) -> list[dict[str, Any]]:
        """返回当前线程安全的日志副本。"""

        with self.log_lock:
            return [dict(item) for item in self.logs]


class AgentWorkspaceWarmupManager:
    """编排默认分支准备步骤，并复用正常 Agent 的快照实现。"""

    def __init__(self, config_manager: ConfigManager) -> None:
        self.config_manager = config_manager
        self._operations: dict[str, AgentWorkspaceWarmup] = {}
        self._lock = asyncio.Lock()

    def _repository(self, repository_id: str) -> RepositoryConfig | None:
        return self.config_manager.config.repository_map().get(repository_id)

    async def _snapshot(
        self,
        repository: RepositoryConfig,
    ) -> dict[str, Any]:
        """合并持久快照摘要和当前进程内的预热状态。"""

        operation = self._operations.get(repository.id)
        run_id = f"workspace-warmup-status:{repository.id}"
        resolved = resolve_repository_process_environment(
            self.config_manager.config,
            repository,
            run_id,
        )
        fingerprint_environment = build_preflight_environment()
        fingerprint_environment.update(resolved.process_values)
        persistent = await asyncio.to_thread(
            inspect_workspace_snapshots,
            self.config_manager.config,
            repository,
            fingerprint_environment,
        )
        if operation is None:
            return {
                **persistent,
                "repository_id": repository.id,
                "phase": _persistent_phase(str(persistent["status"])),
                "started_at": None,
                "finished_at": None,
                "elapsed_seconds": 0,
                "error": None,
                "cancel_requested": False,
                "branch": None,
                "head_sha": None,
                "logs": [],
            }
        elapsed = int(
            (operation.finished_at or time.time()) - operation.started_at
        )
        operation_status = operation.status
        operation_phase = operation.phase
        current_fingerprint = persistent.get("current_fingerprint")
        snapshot_missing = (
            operation.snapshot_status in {"created", "restored"}
            and persistent.get("status") == "uninitialized"
        )
        configuration_changed = (
            operation.snapshot_fingerprint is not None
            and current_fingerprint != operation.snapshot_fingerprint
        )
        if operation.status == "ready" and (
            persistent.get("status") in {"disabled", "unconfigured", "outdated"}
            or snapshot_missing
            or configuration_changed
        ):
            # 已结束任务不能掩盖后续配置变化或被外部删除的快照。
            operation_status = str(persistent["status"])
            operation_phase = _persistent_phase(operation_status)
        return {
            **persistent,
            "repository_id": repository.id,
            "status": operation_status,
            "phase": operation_phase,
            "started_at": operation.started_at,
            "finished_at": operation.finished_at,
            "elapsed_seconds": max(0, elapsed),
            "error": operation.error,
            "cancel_requested": operation.cancel_requested,
            "branch": operation.branch,
            "head_sha": operation.head_sha,
            "logs": operation.log_snapshot(),
        }

    async def get(self, repository_id: str) -> dict[str, Any] | None:
        """读取单个已保存仓库的预热状态。"""

        repository = self._repository(repository_id)
        if repository is None:
            return None
        return await self._snapshot(repository)

    async def start(self, repository_id: str) -> dict[str, Any] | None:
        """启动默认分支预热；已有活动任务时直接返回当前状态。"""

        repository = self._repository(repository_id)
        if repository is None:
            return None
        if not repository.enabled:
            raise ValueError("仓库尚未启用，请先保存启用配置")
        if not repository.agent_workspace.cache_enabled:
            raise ValueError("请先启用 Agent 仓库级下载缓存")
        if not repository.agent_workspace.prepare_steps:
            raise ValueError("请先配置至少一个模型启动前准备步骤")
        async with self._lock:
            existing = self._operations.get(repository_id)
            if existing is not None and existing.status in ACTIVE_WARMUP_STATUSES:
                return await self._snapshot(repository)
            operation = AgentWorkspaceWarmup(repository_id=repository_id)
            self._operations[repository_id] = operation
            operation.task = asyncio.create_task(
                self._run(repository, operation),
                name=f"agent-workspace-warmup-{repository_id}-{uuid.uuid4()}",
            )
        return await self._snapshot(repository)

    async def cancel(self, repository_id: str) -> dict[str, Any] | None:
        """请求取消当前仓库仍在进行的预热。"""

        repository = self._repository(repository_id)
        if repository is None:
            return None
        operation = self._operations.get(repository_id)
        if operation is not None and operation.status in ACTIVE_WARMUP_STATUSES:
            operation.cancel_requested = True
            operation.phase = "正在取消预热"
            operation.cancel_event.set()
        return await self._snapshot(repository)

    async def _run(
        self,
        repository: RepositoryConfig,
        operation: AgentWorkspaceWarmup,
    ) -> None:
        """锁定基础仓库，在默认分支临时工作区执行准备与快照。"""

        config = self.config_manager.config
        provider = config.providers[repository.provider]
        lease = ResourceLease(
            self.config_manager.store,
            [repository_git_lock_key(repository)],
            f"agent-workspace-warmup:{repository.id}:{uuid.uuid4()}",
            ttl_seconds=config.runtime.lock_ttl_seconds,
            timeout_seconds=config.runtime.lock_timeout_seconds,
            cancel_check=operation.cancel_event.is_set,
        )
        manager = None
        checkout: Path | None = None
        try:
            operation.append_log("system", "workspace.warmup.waiting", "正在等待仓库 Git 资源锁")
            async with lease:
                operation.status = "preparing"
                operation.phase = "正在更新基础仓库并检出默认分支"

                def record_git_progress(event: GitProgressEvent) -> None:
                    """从 Git 工作线程记录不含凭据的结构化阶段。"""

                    operation.append_log(
                        "system",
                        f"workspace.git.{event.state}",
                        event.as_dict(),
                    )

                manager = temporary_default_branch_worktree(
                    provider,
                    repository,
                    timeout_seconds=config.runtime.git_timeout_seconds,
                    initialization_timeout_seconds=(
                        config.runtime.repository_initialization_timeout_seconds
                    ),
                    cancel_check=operation.cancel_event.is_set,
                    progress_callback=record_git_progress,
                )
                checkout, operation.branch, operation.head_sha = await asyncio.to_thread(
                    manager.__enter__
                )
                operation.phase = "正在执行准备步骤或恢复快照"
                checkout_repository = repository.model_copy(
                    update={"workspace": checkout},
                )
                run_id = f"workspace-warmup:{repository.id}:{uuid.uuid4()}"
                resolved = resolve_repository_process_environment(
                    config,
                    checkout_repository,
                    run_id,
                )
                redactor = SecretRedactor(resolved.secret_values)

                async def record_log(
                    stream: str,
                    event_type: str,
                    payload: str | dict[str, Any],
                ) -> None:
                    """保存实时准备日志，所有 Secret 在进入内存前完成脱敏。"""

                    operation.append_log(
                        stream,
                        event_type,
                        redactor.data(payload),
                    )

                result = await prepare_agent_workspace(
                    config=config,
                    repository=checkout_repository,
                    agent=AgentConfig(
                        prompt="仓库工作区预热",
                        sandbox="workspace-write",
                        network_access=True,
                        write_scopes=["workspace"],
                    ),
                    process_environment=resolved.process_values,
                    redactor=redactor,
                    log_callback=record_log,
                    cancel_check=operation.cancel_event.is_set,
                )
                if result.outcome.status == "cancelled":
                    raise WorkspaceCancelled("Agent 工作区预热已取消")
                if result.outcome.status != "success":
                    raise RuntimeError(
                        result.outcome.error
                        or f"准备步骤 {result.outcome.failed_step or 'unknown'} 执行失败"
                    )
                operation.snapshot_status = result.snapshot_status
                operation.snapshot_fingerprint = result.snapshot_fingerprint
                operation.status = "ready"
                operation.phase = (
                    "依赖快照已创建"
                    if result.snapshot_status == "created"
                    else "已有依赖快照可复用"
                    if result.snapshot_status == "restored"
                    else "准备完成，但没有可归档的工作区产物"
                )
        except (WorkspaceCancelled, LockCancelledError):
            operation.status = "cancelled"
            operation.phase = "预热已取消"
            operation.error = "用户取消了 Agent 工作区预热"
        except LockTimeoutError as exc:
            operation.status = "failed"
            operation.phase = "等待仓库锁超时"
            operation.error = str(exc)
        except asyncio.CancelledError:
            operation.cancel_event.set()
            operation.status = "cancelled"
            operation.phase = "服务停止，预热已取消"
            operation.error = operation.phase
        except Exception as exc:
            operation.status = "failed"
            operation.phase = "Agent 工作区预热失败"
            operation.error = str(exc)
            operation.append_log("stderr", "workspace.warmup.failed", str(exc))
        finally:
            if checkout is not None and manager is not None:
                with suppress(Exception):
                    await asyncio.to_thread(manager.__exit__, None, None, None)
            operation.finished_at = time.time()

    async def close(self) -> None:
        """服务退出时取消并等待全部预热后台任务。"""

        tasks: list[asyncio.Task[None]] = []
        for operation in self._operations.values():
            if operation.task is not None and not operation.task.done():
                operation.cancel_event.set()
                operation.cancel_requested = True
                tasks.append(operation.task)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
