"""在创建 Agent 工作区之前检查实际需要的执行器能力。"""

from __future__ import annotations

from typing import Mapping

from .codex_executable import CodexExecutable, CodexRuntimeError, locate_codex_executable
from .codex_settings import inspect_codex_binary
from .config import AgentConfig, AppConfig, RepositoryConfig
from .managed_sandbox import inspect_managed_sandbox


def check_runtime_readiness(
    config: AppConfig,
    agent: AgentConfig,
    repository: RepositoryConfig,
    environment: Mapping[str, str],
    *,
    cli_execution: bool,
) -> CodexExecutable | None:
    """按执行模式和工作区准备要求检查，不给纯外部模型增加无关依赖。"""

    restricted = agent.sandbox != "danger-full-access"
    preparation = bool(
        repository.agent_workspace.prepare_steps or repository.agent_workspace.cache_enabled
    )
    sandbox_required = restricted and (not cli_execution or preparation)
    managed = config.runtime.managed_sandbox
    if sandbox_required and not managed.enabled:
        raise CodexRuntimeError(
            "当前 Agent 的模型工具或工作区准备要求启用 Teamwork 外层沙盒",
            error_code="sandbox_configuration_invalid",
        )
    sandbox_requested = restricted and managed.enabled
    if not cli_execution and not sandbox_requested:
        return None
    resolution = locate_codex_executable(config.runtime.codex_binary, environment)
    if config.runtime.expected_codex_version:
        version = inspect_codex_binary(resolution.resolved_path, config.runtime.codex_home)
        if version["error"]:
            raise CodexRuntimeError(
                f"无法检查 Codex CLI 版本：{version['error']}",
                error_code="codex_version_probe_failed", retryable=True,
                details=resolution.as_dict(),
            )
        if version["version"] != config.runtime.expected_codex_version:
            raise CodexRuntimeError(
                f"Codex CLI 版本不匹配：期望 {config.runtime.expected_codex_version}，"
                f"实际 {version['version'] or '无法识别'}",
                error_code="codex_version_mismatch", details=resolution.as_dict(),
            )
    if sandbox_requested:
        inspection = inspect_managed_sandbox(
            resolution.resolved_path, config.runtime.codex_home, environment=environment,
        )
        if not inspection.available and (sandbox_required or managed.fail_closed):
            raise CodexRuntimeError(
                inspection.error or "Teamwork 外层沙盒能力不可用",
                error_code=inspection.error_code or "sandbox_probe_failed",
                retryable=inspection.retryable,
                details={**inspection.as_dict(), **resolution.as_dict()},
            )
    return resolution
