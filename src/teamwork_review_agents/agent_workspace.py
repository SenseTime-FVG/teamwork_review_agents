"""Agent 工作区准备步骤与仓库级依赖缓存编排。"""

from __future__ import annotations

import asyncio
import json
import time
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterator

from .config import (
    AgentConfig,
    AgentWorkspacePrepareStepConfig,
    AppConfig,
    RepositoryConfig,
)
from .environment import SecretRedactor
from .filesystem import temporary_directory
from .managed_sandbox import inspect_managed_sandbox, wrap_managed_sandbox_command
from .sandbox_environment import (
    sandbox_executable_environment,
    windows_environment_separation,
)
from .subprocess_utils import ProcessLaunch, remove_environment_names
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
from .codex_executable import resolve_codex_executable
from .workspace_snapshot import (
    WorkspaceSnapshotCancelled,
    WorkspaceSnapshotError,
    create_workspace_snapshot,
    invalidate_workspace_snapshot,
    restore_workspace_snapshot,
    workspace_snapshot_fingerprint,
)
from .workspace_python import (
    PYTHON_PROBE,
    apply_workspace_python_environment,
    check_python_script_paths,
    python_probe_metadata,
    workspace_python_environment,
    workspace_python_paths,
    workspace_python_runtime_hint,
)


LogCallback = Callable[[str, str, str | dict[str, Any]], Awaitable[None]]
CancelCheck = Callable[[], bool]


@dataclass(frozen=True)
class AgentWorkspacePreparationResult:
    """工作区准备结果及应继续注入 Agent 的缓存和 Python 环境。"""

    outcome: StepExecutionOutcome
    cache_environment: dict[str, str]
    cache_root: Path | None
    snapshot_status: str = "disabled"
    snapshot_fingerprint: str | None = None
    snapshot_metadata: dict[str, Any] | None = None
    execution_environment: dict[str, str] | None = None
    runtime_hint: str = ""
    error_code: str | None = None
    retryable: bool = True


@contextmanager
def _preparation_process_context(
    config: AppConfig,
    agent: AgentConfig,
    process_environment: dict[str, str],
    cache_environment: dict[str, str],
) -> Iterator[tuple[dict[str, str], Callable[[list[str], Path], ProcessLaunch]]]:
    """准备与启动校验共用临时 HOME、凭据过滤和原生沙盒边界。"""

    restricted = agent.sandbox != "danger-full-access"
    with temporary_directory(prefix="teamwork-agent-prepare-home-") as home:
        environment = build_preflight_environment(
            home=home, cache_environment=cache_environment
        )
        environment.update(process_environment)
        locked = build_preflight_environment(
            home=home, cache_environment=cache_environment
        )
        for name in {
            "HOME",
            "USERPROFILE",
            "APPDATA",
            "LOCALAPPDATA",
            "TEMP",
            "TMP",
            *cache_environment,
        }:
            if name in locked:
                remove_environment_names(environment, {name})
                environment[name] = locked[name]
        preparation_codex_home = None
        if restricted and windows_environment_separation():
            # 校验进程与安装进程都不能借临时 HOME 获得宿主 Codex 登录信息。
            preparation_codex_home = home / "codex-home"
            preparation_codex_home.mkdir(mode=0o700)
            remove_environment_names(environment, {"CODEX_HOME"})
            environment["CODEX_HOME"] = str(preparation_codex_home.resolve())
        elif config.runtime.codex_home is not None:
            environment["CODEX_HOME"] = str(
                config.runtime.codex_home.expanduser().resolve()
            )
        preparation_agent = (
            agent.model_copy(update={"sandbox": "workspace-write"})
            if restricted
            else agent
        )

        def wrap_command(command: list[str], step_cwd: Path) -> ProcessLaunch:
            """按步骤实际目录构造启动对象，不放宽正式 Agent 权限。"""

            if not restricted:
                return ProcessLaunch(command, dict(environment))
            return wrap_managed_sandbox_command(
                codex_binary=resolve_codex_executable(
                    config.runtime.codex_binary,
                    sandbox_executable_environment(environment),
                ),
                workspace=step_cwd,
                agent=preparation_agent,
                inner_command=command,
                environment=environment,
                codex_runtime_directory=preparation_codex_home,
                codex_home=config.runtime.codex_home,
            )

        yield environment, wrap_command


def _preparation_sandbox_error(config: AppConfig, agent: AgentConfig) -> str | None:
    """缓存命中后的真实校验同样必须满足已有沙盒要求。"""

    if agent.sandbox == "danger-full-access":
        return None
    if not config.runtime.managed_sandbox.enabled:
        return "Agent 工作区准备或仓库级缓存必须使用 Teamwork 外层沙盒，但运行时已关闭该能力"
    inspection = inspect_managed_sandbox(
        config.runtime.codex_binary, config.runtime.codex_home
    )
    if not inspection.available:
        return f"Agent 工作区准备或仓库级缓存无法启用 Teamwork 外层沙盒：{inspection.error or '当前平台能力不可用'}"
    return None


def agent_repository_cache_environment(
    config: AppConfig,
    repository: RepositoryConfig,
) -> tuple[Path | None, dict[str, str]]:
    """按仓库配置创建缓存环境；关闭时不触碰文件系统。"""

    if not repository.agent_workspace.cache_enabled:
        return None, {}
    root = repository_cache_root(config, repository)
    return root, build_repository_cache_environment(root)


async def _prepare_workspace_artifacts(
    *,
    config: AppConfig,
    repository: RepositoryConfig,
    agent: AgentConfig,
    process_environment: dict[str, str],
    redactor: SecretRedactor,
    log_callback: LogCallback,
    cancel_check: CancelCheck,
    inherited_workspace: bool = False,
    restore_snapshot: bool = True,
    deadline: float | None = None,
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
            metadata = (
                await asyncio.to_thread(
                    restore_workspace_snapshot,
                    config,
                    repository,
                    snapshot_fingerprint,
                    cancel_check=cancel_check,
                )
                if restore_snapshot
                else None
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

    sandbox_error = _preparation_sandbox_error(config, agent)
    if sandbox_error:
        return AgentWorkspacePreparationResult(
            outcome=StepExecutionOutcome(status="error", error=sandbox_error),
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

    with _preparation_process_context(
        config, agent, process_environment, cache_environment
    ) as (environment, wrap_command):

        async def on_step_update(update: PreflightStepUpdate) -> None:
            """把结构化步骤状态映射到当前 Agent 运行时间线。"""

            step = steps[update.step_index]
            suffix = "started" if update.status == "running" else "completed"
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
            settings.model_copy(
                update={"timeout_seconds": max(0.001, deadline - time.monotonic())}
            )
            if deadline
            else settings,
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


async def _validate_workspace_python(
    *,
    config: AppConfig,
    repository: RepositoryConfig,
    agent: AgentConfig,
    process_environment: dict[str, str],
    result: AgentWorkspacePreparationResult,
    redactor: SecretRedactor,
    log_callback: LogCallback,
    cancel_check: CancelCheck,
    deadline: float,
) -> AgentWorkspacePreparationResult:
    """使用实际虚拟环境解释器，在同一沙盒边界内校验环境与显式模块。"""

    source = result.snapshot_status
    await log_callback(
        "system",
        "workspace.python.check_started",
        {
            "source": source,
            "venv": repository.agent_workspace.python_venv,
            "checked_modules": repository.agent_workspace.python_check_modules,
        },
    )
    metadata: dict[str, object] | None = None
    overrides: dict[str, str] | None = None
    try:
        if cancel_check():
            outcome = StepExecutionOutcome(
                status="cancelled", error="Agent 工作区准备已取消"
            )
        elif time.monotonic() >= deadline:
            outcome = StepExecutionOutcome(
                status="timed_out", error="Agent 工作区准备总运行时间超时"
            )
        else:
            sandbox_error = _preparation_sandbox_error(config, agent)
            if sandbox_error:
                raise ValueError(sandbox_error)
            venv, scripts, python = workspace_python_paths(
                repository.workspace, repository.agent_workspace.python_venv
            )
            check_python_script_paths(scripts)
            with _preparation_process_context(
                config, agent, process_environment, result.cache_environment
            ) as (environment, wrap_command):
                overrides = workspace_python_environment(
                    venv, scripts, python, environment
                )
                apply_workspace_python_environment(environment, overrides)
                settings = repository.agent_workspace.model_copy(
                    update={
                        "timeout_seconds": max(0.001, deadline - time.monotonic()),
                        "prepare_steps": [
                            AgentWorkspacePrepareStepConfig(
                                name="校验工作区 Python 环境",
                                command=[
                                    str(python),
                                    "-I",
                                    "-c",
                                    PYTHON_PROBE,
                                    str(venv),
                                    json.dumps(
                                        repository.agent_workspace.python_check_modules
                                    ),
                                ],
                            )
                        ],
                    }
                )

                def wrap_probe(command: list[str], cwd: Path) -> ProcessLaunch:
                    """通用程序解析会解引用链接，此处必须保留虚拟环境的 Python 入口。"""

                    return wrap_command([str(python), *command[1:]], cwd)

                outcome = await execute_preflight_steps(
                    settings,
                    cwd=repository.workspace,
                    environment=environment,
                    cancel_check=cancel_check,
                    command_wrapper=wrap_probe,
                    operation_name="工作区 Python 环境校验",
                    cancellation_message="Agent 工作区准备已取消",
                )
            if outcome.status == "success":
                metadata = python_probe_metadata(outcome.output)
    except (OSError, ValueError) as exc:
        outcome = StepExecutionOutcome(status="error", error=str(exc))
    if outcome.status == "success" and metadata is not None:
        await log_callback(
            "system",
            "workspace.python.ready",
            redactor.data({**metadata, "source": source}),
        )
        return replace(
            result,
            execution_environment=overrides,
            runtime_hint=workspace_python_runtime_hint(metadata),
        )
    if outcome.status == "failure":
        # 导入校验失败是环境准备失败，不是已经执行过的代码测试失败。
        outcome = replace(outcome, status="error")
    detail = outcome.error or "Python 环境校验失败"
    if outcome.output:
        # 错误摘要保留具体缺失模块，但不把完整大型输出复制进运行终态。
        detail += "\n" + outcome.output[-4000:]
    outcome = replace(outcome, error=f"环境未就绪，相关测试未执行：{detail}")
    await log_callback(
        "stderr",
        "workspace.python.failed",
        redactor.data(
            {
                "source": source,
                "status": outcome.status,
                "error": outcome.error,
                "exit_code": outcome.exit_code,
                "output": outcome.output,
                "error_code": "workspace_python_environment_unavailable",
            }
        ),
    )
    return replace(
        result,
        outcome=outcome,
        error_code="workspace_python_environment_unavailable",
        retryable=False,
    )


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
    """编排准备、环境校验与一次快照恢复，禁止静默降级到宿主 Python。"""

    deadline = time.monotonic() + repository.agent_workspace.timeout_seconds

    def should_stop() -> bool:
        """安装、解包、校验与重建共用一个总期限。"""

        return cancel_check() or time.monotonic() >= deadline

    async def prepare(*, restore: bool) -> AgentWorkspacePreparationResult:
        """恢复失效时只重跑一次声明的准备步骤，不额外猜测安装命令。"""

        prepared = await _prepare_workspace_artifacts(
            config=config,
            repository=repository,
            agent=agent,
            process_environment=process_environment,
            redactor=redactor,
            log_callback=log_callback,
            cancel_check=should_stop,
            inherited_workspace=inherited_workspace,
            restore_snapshot=restore,
            deadline=deadline,
        )
        if (
            prepared.outcome.status == "cancelled"
            and not cancel_check()
            and time.monotonic() >= deadline
        ):
            prepared = replace(
                prepared,
                outcome=replace(
                    prepared.outcome,
                    status="timed_out",
                    error="Agent 工作区准备总运行时间超时",
                ),
            )
        if prepared.outcome.status != "success":
            return replace(
                prepared, error_code="workspace_environment_preparation_failed"
            )
        if repository.agent_workspace.python_venv is None:
            return prepared
        return await _validate_workspace_python(
            config=config,
            repository=repository,
            agent=agent,
            process_environment=process_environment,
            result=prepared,
            redactor=redactor,
            log_callback=log_callback,
            cancel_check=cancel_check,
            deadline=deadline,
        )

    result = await prepare(restore=True)
    if (
        result.error_code != "workspace_python_environment_unavailable"
        or result.outcome.status in {"cancelled", "timed_out"}
    ):
        return result
    if result.snapshot_fingerprint is not None:
        try:
            await asyncio.to_thread(
                invalidate_workspace_snapshot,
                config,
                repository,
                result.snapshot_fingerprint,
            )
        except OSError as exc:
            await log_callback(
                "stderr",
                "workspace.snapshot.invalidate_failed",
                {"error": redactor.text(str(exc))},
            )
    if (
        result.snapshot_status == "restored"
        and not inherited_workspace
        and repository.agent_workspace.prepare_steps
        and not should_stop()
    ):
        await log_callback(
            "system",
            "workspace.python.rebuilding",
            {
                "reason": "恢复的 Python 环境不可用，重新执行一次准备步骤",
                "max_attempts": 1,
            },
        )
        rebuilt = await prepare(restore=False)
        if rebuilt.outcome.status != "success":
            # 一次恢复额度已经耗尽，不能让事件重试从头重复同样的修复流程。
            rebuilt = replace(rebuilt, retryable=False)
        if (
            rebuilt.error_code == "workspace_python_environment_unavailable"
            and rebuilt.snapshot_fingerprint is not None
        ):
            try:
                await asyncio.to_thread(
                    invalidate_workspace_snapshot,
                    config,
                    repository,
                    rebuilt.snapshot_fingerprint,
                )
            except OSError as exc:
                await log_callback(
                    "stderr",
                    "workspace.snapshot.invalidate_failed",
                    {"error": redactor.text(str(exc))},
                )
        return rebuilt
    return result
