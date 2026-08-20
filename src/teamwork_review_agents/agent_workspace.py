"""Agent 工作区准备步骤与仓库级依赖缓存编排。"""

from __future__ import annotations

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


LogCallback = Callable[[str, str, str | dict[str, Any]], Awaitable[None]]
CancelCheck = Callable[[], bool]


@dataclass(frozen=True)
class AgentWorkspacePreparationResult:
    """工作区准备结果以及应继续注入 Agent 的缓存环境。"""

    outcome: StepExecutionOutcome
    cache_environment: dict[str, str]
    cache_root: Path | None


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
    return AgentWorkspacePreparationResult(
        outcome=outcome,
        cache_environment=cache_environment,
        cache_root=cache_root,
    )
