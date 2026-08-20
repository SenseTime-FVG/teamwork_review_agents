"""Agent 工作区准备步骤与仓库级依赖缓存编排。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

from .config import AgentConfig, AppConfig, RepositoryConfig
from .environment import SecretRedactor
from .filesystem import temporary_directory
from .managed_sandbox import inspect_managed_sandbox, wrap_managed_sandbox_command
from .preflight import (
    PreflightStepUpdate,
    StepExecutionOutcome,
    build_preflight_environment,
    execute_preflight_steps,
)
from .preflight_cache import (
    build_repository_cache_environment,
    repository_cache_root,
)
from .subprocess_utils import resolve_executable
from .workspace_snapshot import (
    WorkspaceSnapshotCancelled,
    WorkspaceSnapshotError,
    create_workspace_snapshot,
    invalidate_workspace_snapshot,
    restore_workspace_snapshot,
    workspace_snapshot_fingerprint,
)


LogCallback = Callable[[str, str, str | dict[str, Any]], Awaitable[None]]
CancelCheck = Callable[[], bool]


@dataclass(frozen=True)
class AgentWorkspacePreparationResult:
    """工作区准备结果以及应继续注入 Agent 的缓存环境。"""

    outcome: StepExecutionOutcome
    cache_environment: dict[str, str]
    cache_root: Path | None
    snapshot_status: str = "disabled"
    snapshot_fingerprint: str | None = None
    snapshot_metadata: dict[str, Any] | None = None


def agent_repository_cache_environment(
    config: AppConfig,
    repository: RepositoryConfig,
) -> tuple[Path | None, dict[str, str]]:
    """按仓库配置创建缓存环境；关闭时不触碰文件系统。"""

    if not repository.agent_workspace.cache_enabled:
        return None, {}
    root = repository_cache_root(config, repository)
    return root, build_repository_cache_environment(root)


async def prepare_agent_workspace(
    *,
    config: AppConfig,
    repository: RepositoryConfig,
    agent: AgentConfig,
    process_environment: dict[str, str],
    redactor: SecretRedactor,
    log_callback: LogCallback,
    cancel_check: CancelCheck,
    inherited_workspace: bool = False,
) -> AgentWorkspacePreparationResult:
    """在模型启动前通过外层沙盒执行仓库声明的准备步骤。"""

    settings = repository.agent_workspace
    cache_root, cache_environment = agent_repository_cache_environment(
        config,
        repository,
    )
    steps = settings.prepare_steps
    if not steps and cache_root is None:
        return AgentWorkspacePreparationResult(
            outcome=StepExecutionOutcome(status="success"),
            cache_environment=cache_environment,
            cache_root=cache_root,
        )

    if inherited_workspace and steps:
        await log_callback(
            "system",
            "workspace.prepare.inherited",
            {
                "steps": len(steps),
                "reason": "当前 sub-agent 继承父 Agent 已准备的同一工作区",
            },
        )
        return AgentWorkspacePreparationResult(
            outcome=StepExecutionOutcome(status="success"),
            cache_environment=cache_environment,
            cache_root=cache_root,
            snapshot_status="inherited",
        )

    snapshot_fingerprint: str | None = None
    preparation_signature: str | None = None
    if steps and cache_root is not None:
        try:
            fingerprint_environment = build_preflight_environment(
                cache_environment=cache_environment,
            )
            fingerprint_environment.update(process_environment)
            snapshot_fingerprint, preparation_signature = await asyncio.to_thread(
                workspace_snapshot_fingerprint,
                repository,
                fingerprint_environment,
            )
            await log_callback(
                "system",
                "workspace.snapshot.lookup",
                {"fingerprint": snapshot_fingerprint},
            )
            metadata = await asyncio.to_thread(
                restore_workspace_snapshot,
                config,
                repository,
                snapshot_fingerprint,
                cancel_check=cancel_check,
            )
            if metadata is not None:
                await log_callback(
                    "system",
                    "workspace.snapshot.restored",
                    {
                        "fingerprint": snapshot_fingerprint,
                        "size_bytes": metadata.get("size_bytes"),
                        "artifact_count": metadata.get("artifact_count"),
                    },
                )
                return AgentWorkspacePreparationResult(
                    outcome=StepExecutionOutcome(status="success"),
                    cache_environment=cache_environment,
                    cache_root=cache_root,
                    snapshot_status="restored",
                    snapshot_fingerprint=snapshot_fingerprint,
                    snapshot_metadata=metadata,
                )
            await log_callback(
                "system",
                "workspace.snapshot.missed",
                {"fingerprint": snapshot_fingerprint},
            )
        except WorkspaceSnapshotCancelled as exc:
            await log_callback(
                "system",
                "workspace.snapshot.cancelled",
                {
                    "fingerprint": snapshot_fingerprint,
                    "error": redactor.text(str(exc)),
                },
            )
            return AgentWorkspacePreparationResult(
                outcome=StepExecutionOutcome(
                    status="cancelled",
                    error="Agent 工作区准备已由管理员取消",
                ),
                cache_environment=cache_environment,
                cache_root=cache_root,
                snapshot_status="cancelled",
                snapshot_fingerprint=snapshot_fingerprint,
            )
        except (OSError, WorkspaceSnapshotError) as exc:
            await log_callback(
                "stderr",
                "workspace.snapshot.restore_failed",
                {
                    "fingerprint": snapshot_fingerprint,
                    "error": redactor.text(str(exc)),
                    "fallback": "execute_prepare_steps",
                },
            )
            if snapshot_fingerprint is not None:
                try:
                    await asyncio.to_thread(
                        invalidate_workspace_snapshot,
                        config,
                        repository,
                        snapshot_fingerprint,
                    )
                except OSError as invalidate_error:
                    # 清理失败不应阻断实际准备步骤，后续仍可覆盖该快照。
                    await log_callback(
                        "stderr",
                        "workspace.snapshot.invalidate_failed",
                        {
                            "fingerprint": snapshot_fingerprint,
                            "error": redactor.text(str(invalidate_error)),
                            "agent_continues": True,
                        },
                    )

    restricted = agent.sandbox != "danger-full-access"
    if restricted:
        if not config.runtime.managed_sandbox.enabled:
            outcome = StepExecutionOutcome(
                status="error",
                error=(
                    "Agent 工作区准备或仓库级缓存必须使用 Teamwork 外层沙盒，"
                    "但运行时已关闭该能力"
                ),
            )
            return AgentWorkspacePreparationResult(
                outcome=outcome,
                cache_environment=cache_environment,
                cache_root=cache_root,
            )
        inspection = inspect_managed_sandbox(
            config.runtime.codex_binary,
            config.runtime.codex_home,
        )
        if not inspection.available:
            outcome = StepExecutionOutcome(
                status="error",
                error=(
                    "Agent 工作区准备或仓库级缓存无法启用 Teamwork 外层沙盒："
                    f"{inspection.error or '当前平台能力不可用'}"
                ),
            )
            return AgentWorkspacePreparationResult(
                outcome=outcome,
                cache_environment=cache_environment,
                cache_root=cache_root,
            )

    if not steps:
        if cache_root is not None:
            await log_callback(
                "system",
                "workspace.prepare.completed",
                {
                    "steps": 0,
                    "cache_enabled": True,
                    "cache_path": str(cache_root.resolve()),
                },
            )
        return AgentWorkspacePreparationResult(
            outcome=StepExecutionOutcome(status="success"),
            cache_environment=cache_environment,
            cache_root=cache_root,
        )

    await log_callback(
        "system",
        "workspace.prepare.started",
        {
            "steps": len(steps),
            "cache_enabled": cache_root is not None,
            "cache_path": str(cache_root.resolve()) if cache_root else None,
        },
    )

    with temporary_directory(prefix="teamwork-agent-prepare-home-") as home:
        environment = build_preflight_environment(
            home=home,
            cache_environment=cache_environment,
        )
        environment.update(process_environment)
        # HOME 与缓存路径属于运行时安全边界，仓库或 Agent 环境不能覆盖。
        locked_environment = build_preflight_environment(
            home=home,
            cache_environment=cache_environment,
        )
        for name in {
            "HOME",
            "USERPROFILE",
            "APPDATA",
            "LOCALAPPDATA",
            "TEMP",
            "TMP",
            *cache_environment.keys(),
        }:
            if name in locked_environment:
                environment[name] = locked_environment[name]
        if config.runtime.codex_home is not None:
            environment["CODEX_HOME"] = str(
                config.runtime.codex_home.expanduser().resolve()
            )

        preparation_agent = agent
        if restricted:
            # 安装依赖需要写当前工作区，但不会扩大正式 Agent 的文件权限。
            preparation_agent = agent.model_copy(
                update={"sandbox": "workspace-write"},
            )

        def wrap_command(command: list[str], step_cwd: Path) -> list[str]:
            """为每个步骤按其工作目录生成同一套原生沙盒边界。"""

            if not restricted:
                return command
            return wrap_managed_sandbox_command(
                codex_binary=resolve_executable(
                    config.runtime.codex_binary,
                    environment,
                ),
                workspace=step_cwd,
                agent=preparation_agent,
                inner_command=command,
                environment=environment,
            )

        async def on_step_update(update: PreflightStepUpdate) -> None:
            """把结构化步骤状态映射到当前 Agent 运行时间线。"""

            step = steps[update.step_index]
            suffix = (
                "started" if update.status == "running" else "completed"
            )
            await log_callback(
                "system",
                f"workspace.prepare.step_{suffix}",
                redactor.data(
                    {
                        "step_index": update.step_index,
                        "name": step.name,
                        "cwd": step.cwd,
                        "command": step.command,
                        "status": update.status,
                        "timeout_seconds": update.timeout_seconds,
                        "exit_code": update.exit_code,
                        "error": update.error,
                    }
                ),
            )

        async def on_output(output: str) -> None:
            """逐段写入脱敏后的准备输出，便于页面实时查看。"""

            await log_callback(
                "stdout",
                "workspace.prepare.output",
                redactor.text(output),
            )

        outcome = await execute_preflight_steps(
            settings,
            cwd=repository.workspace,
            environment=environment,
            on_step_update=on_step_update,
            on_output=on_output,
            cancel_check=cancel_check,
            command_wrapper=wrap_command,
            operation_name="Agent 工作区准备",
            cancellation_message="Agent 工作区准备已由管理员取消",
        )

    terminal_event = (
        "workspace.prepare.completed"
        if outcome.status == "success"
        else "workspace.prepare.failed"
    )
    await log_callback(
        "system",
        terminal_event,
        redactor.data(
            {
                "status": outcome.status,
                "failed_step": outcome.failed_step,
                "exit_code": outcome.exit_code,
                "error": outcome.error,
                "cache_path": str(cache_root.resolve()) if cache_root else None,
            }
        ),
    )
    snapshot_metadata: dict[str, Any] | None = None
    snapshot_status = "disabled" if cache_root is None else "not_created"
    if (
        outcome.status == "success"
        and snapshot_fingerprint is not None
        and preparation_signature is not None
    ):
        try:
            snapshot_metadata = await asyncio.to_thread(
                create_workspace_snapshot,
                config,
                repository,
                snapshot_fingerprint,
                preparation_signature,
                cancel_check=cancel_check,
            )
            snapshot_status = "created" if snapshot_metadata is not None else "empty"
            await log_callback(
                "system",
                "workspace.snapshot.created",
                {
                    "fingerprint": snapshot_fingerprint,
                    "status": snapshot_status,
                    "size_bytes": (
                        snapshot_metadata.get("size_bytes")
                        if snapshot_metadata is not None
                        else 0
                    ),
                    "artifact_count": (
                        snapshot_metadata.get("artifact_count")
                        if snapshot_metadata is not None
                        else 0
                    ),
                },
            )
        except WorkspaceSnapshotCancelled as exc:
            snapshot_status = "cancelled"
            outcome = StepExecutionOutcome(
                status="cancelled",
                error="Agent 工作区准备已由管理员取消",
            )
            await log_callback(
                "system",
                "workspace.snapshot.cancelled",
                {
                    "fingerprint": snapshot_fingerprint,
                    "error": redactor.text(str(exc)),
                },
            )
        except (OSError, WorkspaceSnapshotError) as exc:
            snapshot_status = "create_failed"
            await log_callback(
                "stderr",
                "workspace.snapshot.create_failed",
                {
                    "fingerprint": snapshot_fingerprint,
                    "error": redactor.text(str(exc)),
                    "agent_continues": True,
                },
            )
    return AgentWorkspacePreparationResult(
        outcome=outcome,
        cache_environment=cache_environment,
        cache_root=cache_root,
        snapshot_status=snapshot_status,
        snapshot_fingerprint=snapshot_fingerprint,
        snapshot_metadata=snapshot_metadata,
    )
