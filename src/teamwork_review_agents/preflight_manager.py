"""仓库默认分支手动 CI 的后台调度、取消与实时日志。"""

from __future__ import annotations

import asyncio
import tempfile
import uuid
from contextlib import suppress
from pathlib import Path

from .config import AppConfig, RepositoryConfig
from .config_manager import ConfigManager
from .locks import LockCancelledError, ResourceLease
from .models import PreflightResult
from .preflight import (
    PreflightStepUpdate,
    build_preflight_environment,
    execute_preflight_steps,
)
from .preflight_cache import (
    build_repository_cache_environment,
    repository_cache_root,
)
from .workspace import (
    GitProgressEvent,
    WorkspaceCancelled,
    repository_git_lock_key,
    temporary_default_branch_worktree,
)


class ManualPreflightManager:
    """管理不绑定 MR / PR、不会触发 Agent 的仓库手动 CI。"""

    def __init__(self, config_manager: ConfigManager) -> None:
        self.config_manager = config_manager
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._cancel_events: dict[str, asyncio.Event] = {}
        self._lock = asyncio.Lock()

    async def _append_log(
        self,
        run_id: str,
        payload: str | dict[str, object],
        *,
        stream: str = "system",
        event_type: str = "message",
    ) -> None:
        """持久化手动 CI 日志，避免观察链路故障改变检查结果。"""

        try:
            await asyncio.to_thread(
                self.config_manager.store.append_preflight_log,
                run_id,
                stream=stream,
                event_type=event_type,
                payload=payload,
            )
        except Exception:
            pass

    async def start(self, repository_id: str) -> dict[str, object]:
        """创建手动 CI 记录并立即返回，实际运行由后台任务推进。"""

        config = self.config_manager.config
        repository = config.repository_map().get(repository_id)
        if repository is None:
            raise LookupError("仓库配置不存在")
        if not repository.enabled:
            raise ValueError("仓库未启用，不能执行手动 CI")
        if not repository.preflight.enabled or not repository.preflight.steps:
            raise ValueError("仓库未启用 Preflight 或没有配置 CI 步骤")
        async with self._lock:
            active = await asyncio.to_thread(
                self.config_manager.store.list_preflight_runs,
                None,
                status="running",
                repository_id=repository_id,
            )
            if any(run.get("trigger_source") == "manual" for run in active):
                raise ValueError("该仓库已有手动 CI 正在运行")
            run_id = str(uuid.uuid4())
            await asyncio.to_thread(
                self.config_manager.store.create_manual_preflight_run,
                run_id=run_id,
                repository_id=repository_id,
                config_revision=config.revision,
            )
            cancel_event = asyncio.Event()
            self._cancel_events[run_id] = cancel_event
            task = asyncio.create_task(
                self._run(run_id, config, repository, cancel_event),
                name=f"manual-preflight-{repository_id}-{run_id}",
            )
            self._tasks[run_id] = task
            task.add_done_callback(lambda _: self._forget(run_id))
        return {
            "accepted": True,
            "run_id": run_id,
            "repository_id": repository_id,
            "reason": "手动 CI 已启动，将执行远端默认分支最新提交",
        }

    def _forget(self, run_id: str) -> None:
        """运行结束后移除内存句柄，历史记录仍保留在数据库。"""

        self._tasks.pop(run_id, None)
        self._cancel_events.pop(run_id, None)

    async def cancel(self, run_id: str) -> bool:
        """请求取消一条仍在运行的手动 CI。"""

        accepted = await asyncio.to_thread(
            self.config_manager.store.request_cancel_preflight,
            run_id,
        )
        if not accepted:
            return False
        event = self._cancel_events.get(run_id)
        if event is not None:
            event.set()
        await self._append_log(
            run_id,
            "管理员已请求取消手动 CI\n",
            event_type="cancel_requested",
        )
        return True

    async def _run(
        self,
        run_id: str,
        config: AppConfig,
        repository: RepositoryConfig,
        cancel_event: asyncio.Event,
    ) -> None:
        """取得仓库锁，准备默认分支 worktree，并不限时执行配置步骤。"""

        repository_id = repository.id
        provider = config.providers[repository.provider]
        store = self.config_manager.store
        result = PreflightResult(
            run_id=run_id,
            repository_id=repository_id,
            number=None,
            head_sha="",
            status="error",
            error="手动 CI 未正常开始",
        )
        manager = None
        checkout: Path | None = None
        try:
            await asyncio.to_thread(
                store.initialize_preflight_steps,
                run_id,
                (
                    {
                        "name": step.name,
                        "command": list(step.command),
                        # 手动预热模式忽略步骤限时，因此详情中显示为不限时。
                        "timeout_seconds": None,
                    }
                    for step in repository.preflight.steps
                ),
            )
            await asyncio.to_thread(store.set_preflight_phase, run_id, "waiting_lock")
            await self._append_log(run_id, "正在等待仓库 Git 资源锁\n")
            lease = ResourceLease(
                store,
                [repository_git_lock_key(repository)],
                f"manual-preflight:{repository_id}:{run_id}",
                ttl_seconds=config.runtime.lock_ttl_seconds,
                timeout_seconds=config.runtime.lock_timeout_seconds,
                cancel_check=cancel_event.is_set,
            )
            async with lease:
                await asyncio.to_thread(store.set_preflight_phase, run_id, "preparing")
                await self._append_log(run_id, "正在更新基础仓库并准备默认分支\n")

                def record_git_progress(event: GitProgressEvent) -> None:
                    """从工作线程记录脱敏 Git 阶段，禁止保存认证或命令输出。"""

                    with suppress(Exception):
                        store.append_preflight_log(
                            run_id,
                            stream="system",
                            event_type="git_progress",
                            payload=event.as_dict(),
                        )

                manager = temporary_default_branch_worktree(
                    provider,
                    repository,
                    timeout_seconds=config.runtime.git_timeout_seconds,
                    initialization_timeout_seconds=(
                        config.runtime.repository_initialization_timeout_seconds
                    ),
                    cancel_check=cancel_event.is_set,
                    progress_callback=record_git_progress,
                )
                checkout, branch, head_sha = await asyncio.to_thread(
                    manager.__enter__
                )
                await asyncio.to_thread(
                    store.set_preflight_phase,
                    run_id,
                    "preparing_cache",
                    branch=branch,
                    head_sha=head_sha,
                )
                await self._append_log(
                    run_id,
                    f"已检出默认分支 {branch}：{head_sha[:12]}\n",
                )
                cache_environment: dict[str, str] = {}
                cache_path: str | None = None
                if repository.preflight.cache_enabled:
                    cache_root = repository_cache_root(config, repository)
                    cache_environment = await asyncio.to_thread(
                        build_repository_cache_environment,
                        cache_root,
                    )
                    cache_path = str(cache_root.expanduser().resolve())
                    await self._append_log(
                        run_id,
                        f"正在使用仓库级依赖缓存：{cache_path}\n",
                    )
                await asyncio.to_thread(
                    store.set_preflight_phase,
                    run_id,
                    "running_steps",
                    cache_path=cache_path,
                )

                async def record_step(update: PreflightStepUpdate) -> None:
                    """持久化每个配置步骤的实时状态。"""

                    await asyncio.to_thread(
                        store.update_preflight_step,
                        run_id,
                        update.step_index,
                        status=update.status,
                        timeout_seconds=update.timeout_seconds,
                        exit_code=update.exit_code,
                        error=update.error,
                    )

                async def record_output(chunk: str) -> None:
                    """持久化当前步骤合并后的 stdout / stderr。"""

                    await self._append_log(
                        run_id,
                        chunk,
                        stream="stdout",
                        event_type="output",
                    )

                with tempfile.TemporaryDirectory(
                    prefix="teamwork-manual-preflight-home-"
                ) as home:
                    outcome = await execute_preflight_steps(
                        repository.preflight,
                        cwd=checkout,
                        environment=build_preflight_environment(
                            home=Path(home),
                            cache_environment=cache_environment,
                        ),
                        on_step_update=record_step,
                        on_output=record_output,
                        cancel_check=cancel_event.is_set,
                        unlimited=True,
                    )
                result = PreflightResult(
                    run_id=run_id,
                    repository_id=repository_id,
                    number=None,
                    head_sha=head_sha,
                    status=outcome.status,
                    failed_step=outcome.failed_step,
                    exit_code=outcome.exit_code,
                    output=outcome.output,
                    error=outcome.error,
                )
        except (WorkspaceCancelled, LockCancelledError):
            result = result.model_copy(
                update={"status": "cancelled", "error": "用户取消了手动 CI"}
            )
        except asyncio.CancelledError:
            cancel_event.set()
            result = result.model_copy(
                update={"status": "cancelled", "error": "服务停止，手动 CI 已取消"}
            )
        except Exception as exc:
            result = result.model_copy(update={"status": "error", "error": str(exc)})
            await self._append_log(
                run_id,
                f"手动 CI 基础设施错误：{exc}\n",
                stream="stderr",
                event_type="error",
            )
        finally:
            if checkout is not None and manager is not None:
                with suppress(Exception):
                    await asyncio.to_thread(manager.__exit__, None, None, None)
            await asyncio.to_thread(store.finish_preflight_run, result)
            await self._append_log(
                run_id,
                {
                    "status": result.status,
                    "failed_step": result.failed_step,
                    "exit_code": result.exit_code,
                    "error": result.error,
                },
                event_type="completed",
            )

    async def close(self) -> None:
        """服务退出时取消并等待全部手动 CI 后台任务完成。"""

        tasks = list(self._tasks.items())
        for run_id, _ in tasks:
            event = self._cancel_events.get(run_id)
            if event is not None:
                event.set()
            await asyncio.to_thread(
                self.config_manager.store.request_cancel_preflight,
                run_id,
            )
        if tasks:
            await asyncio.gather(
                *(task for _, task in tasks),
                return_exceptions=True,
            )
