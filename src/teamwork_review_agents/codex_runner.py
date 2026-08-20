"""Codex CLI JSONL 进程运行器。"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import sys
import time
from contextlib import suppress
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping

from .agent_home import (
    TemporaryAgentHome,
    TemporaryCodexHome,
    cleanup_stale_agent_homes_once,
)
from .config import AgentConfig, AppConfig, RepositoryConfig
from .codex_settings import (
    agent_network_overrides,
    agent_overrides,
    codex_home,
    read_user_mcp_servers,
    runtime_overrides,
    validate_codex_version,
)
from .environment import SecretRedactor
from .mcp_bridge import ManagedMcpBroker, McpBridgeChannel
from .models import AgentResult, InvocationContext
from .managed_sandbox import (
    ManagedSandboxInspection,
    inspect_managed_sandbox,
    wrap_managed_sandbox_command,
)
from .process_control import process_group_options, terminate_process
from .skill_files import SkillProjection
from .subprocess_utils import (
    WINDOWS_REQUIRED_ENVIRONMENT_NAMES,
    remove_environment_names,
    resolve_executable,
    selected_environment,
)


LogCallback = Callable[[str, str, str | dict[str, Any]], Awaitable[None]]
CancelCheck = Callable[[], Awaitable[bool]]

_CODEX_STREAM_LIMIT_BYTES = 16 * 1024 * 1024
_PROCESS_TERMINATE_GRACE_SECONDS = 5.0
_PROCESS_KILL_GRACE_SECONDS = 1.0
_STREAM_DRAIN_TIMEOUT_SECONDS = 5.0
_STREAM_CANCEL_TIMEOUT_SECONDS = 1.0
_STREAM_ERROR_DETAIL_LIMIT = 500
_TEAMWORK_MCP_SERVER_NAME = "teamwork_agent_gateway"
_TEAMWORK_MCP_TOOL_NAMESPACE = f"mcp__{_TEAMWORK_MCP_SERVER_NAME}"
_TEAMWORK_MCP_DIRECT_TOOL_OVERRIDE = (
    "features.code_mode.direct_only_tool_namespaces="
    f'["{_TEAMWORK_MCP_TOOL_NAMESPACE}"]'
)

BASE_ENVIRONMENT_NAMES = {
    "PATH",
    "HOME",
    "USER",
    "LOGNAME",
    "SHELL",
    "LANG",
    "LC_ALL",
    "TMPDIR",
    "CODEX_HOME",
    "CODEX_API_KEY",
    "OPENAI_API_KEY",
    "GH_CONFIG_DIR",
    "GLAB_CONFIG_DIR",
    "GIT_CONFIG_GLOBAL",
    "SSH_AUTH_SOCK",
    "SSH_AGENT_PID",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
} | WINDOWS_REQUIRED_ENVIRONMENT_NAMES


def encode_invocation_context(context: InvocationContext) -> str:
    """将 MCP 调用上下文编码为适合环境变量传递的文本。"""

    payload = context.model_dump_json(
        exclude={
            "event": {
                "old": {"raw"},
                "new": {"raw"},
                "current": {"raw"},
            }
        }
    )
    return base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii")


def decode_invocation_context(value: str) -> InvocationContext:
    """从环境变量恢复 MCP 调用上下文。"""

    payload = base64.urlsafe_b64decode(value.encode("ascii")).decode("utf-8")
    return InvocationContext.model_validate_json(payload)


def _toml_string(value: str) -> str:
    """使用 JSON 字符串语法生成兼容 TOML 的转义值。"""

    return json.dumps(value, ensure_ascii=False)


def _skills_config_override(
    skill_files: Mapping[str, Path],
    enabled_skill_ids: list[str],
) -> str:
    """生成 Codex `skills.config` 的 TOML 内联表数组。"""

    enabled = set(enabled_skill_ids)
    entries = [
        (
            "{ path = "
            f"{_toml_string(str(path))}, enabled = "
            f"{'true' if skill_id in enabled else 'false'}"
            " }"
        )
        for skill_id, path in sorted(skill_files.items())
    ]
    return f"skills.config=[{', '.join(entries)}]"


def _add_git_excludes_file(environment: dict[str, str], path: Path) -> None:
    """用 Git 的进程级配置隐藏临时 Skill 投影，不修改仓库配置。"""

    try:
        count = int(environment.get("GIT_CONFIG_COUNT", "0"))
    except ValueError:
        count = 0
    environment["GIT_CONFIG_COUNT"] = str(count + 1)
    environment[f"GIT_CONFIG_KEY_{count}"] = "core.excludesFile"
    environment[f"GIT_CONFIG_VALUE_{count}"] = str(path)


def _toml_key_segment(value: str) -> str:
    """为 TOML 点号键安全编码一个 MCP Server 名称。"""

    if value and all(
        character.isascii() and (character.isalnum() or character in "_-")
        for character in value
    ):
        return value
    return _toml_string(value)


class CodexRunner:
    """启动 `codex exec` 并提取最终消息、线程和用量。"""

    def __init__(self, config: AppConfig) -> None:
        self.config = config
        cleanup_stale_agent_homes_once()

    def build_command(
        self,
        agent: AgentConfig,
        repository: RepositoryConfig,
        context: InvocationContext,
        skill_files: Mapping[str, Path] | None = None,
        *,
        managed_sandbox: bool | None = None,
        environment: Mapping[str, str] | None = None,
        mcp_bridge: McpBridgeChannel | None = None,
        codex_runtime_directory: Path | None = None,
    ) -> list[str]:
        """构造显式叠加 Teamwork 默认和 Agent 覆盖的 Codex 命令。"""

        server_name = _TEAMWORK_MCP_SERVER_NAME
        use_managed_sandbox = (
            self.config.runtime.managed_sandbox.enabled
            and agent.sandbox != "danger-full-access"
            if managed_sandbox is None
            else managed_sandbox
        )
        active_environment = environment if environment is not None else os.environ
        codex_binary = resolve_executable(
            self.config.runtime.codex_binary,
            active_environment,
        )
        command = [
            codex_binary,
            "exec",
            "--json",
            "--ephemeral",
        ]
        if use_managed_sandbox:
            command.append("--dangerously-bypass-approvals-and-sandbox")
        else:
            command.extend(["--sandbox", agent.sandbox])
        command.extend(["--cd", str(repository.workspace)])
        if agent.model:
            command.extend(["--model", agent.model])
        if agent.skip_git_repo_check:
            command.append("--skip-git-repo-check")
        if agent.output_schema:
            command.extend(["--output-schema", str(agent.output_schema)])

        # 托管模式即使尚未拿到通道，也绝不能退回会读取配置和数据库的完整 MCP。
        use_mcp_bridge = use_managed_sandbox
        mcp_args = [
            "-m",
            (
                "teamwork_review_agents.mcp_proxy"
                if use_mcp_bridge
                else "teamwork_review_agents.mcp_server"
            ),
        ]
        enabled_tools = ["invoke_agent"]
        if agent.managed_comment:
            enabled_tools.append("publish_comment")
        overrides = [
            *runtime_overrides(self.config.runtime.codex),
            *agent_overrides(agent),
            *([] if use_managed_sandbox else agent_network_overrides(agent)),
            f"mcp_servers.{server_name}.command={_toml_string(sys.executable)}",
            f"mcp_servers.{server_name}.args={json.dumps(mcp_args)}",
            f"mcp_servers.{server_name}.required=true",
            f"mcp_servers.{server_name}.enabled=true",
            (
                f"mcp_servers.{server_name}.startup_timeout_sec="
                f"{self.config.runtime.mcp_startup_timeout_seconds}"
            ),
            (
                f"mcp_servers.{server_name}.tool_timeout_sec="
                f"{self.config.runtime.mcp_tool_timeout_seconds}"
            ),
            (
                f"mcp_servers.{server_name}.enabled_tools="
                f"{json.dumps(enabled_tools)}"
            ),
            f"mcp_servers.{server_name}.default_tools_approval_mode=\"approve\"",
        ]
        if use_mcp_bridge and mcp_bridge is not None:
            for name, value in mcp_bridge.environment_overrides().items():
                overrides.append(
                    f"mcp_servers.{server_name}.env.{name}={_toml_string(value)}"
                )
        elif not use_mcp_bridge:
            context_value = encode_invocation_context(context)
            overrides.extend(
                [
                    (
                        f"mcp_servers.{server_name}.env.TEAMWORK_CONFIG_PATH="
                        f"{_toml_string(str(self.config.config_path))}"
                    ),
                    (
                        f"mcp_servers.{server_name}.env.TEAMWORK_INVOCATION_CONTEXT="
                        f"{_toml_string(context_value)}"
                    ),
                ]
            )
        if not self.config.runtime.inherit_user_mcp_servers:
            allowed = set(self.config.runtime.allowed_user_mcp_servers)
            user_servers, _ = read_user_mcp_servers(
                codex_home(self.config.runtime.codex_home),
            )
            disabled_overrides = [
                f"mcp_servers.{_toml_key_segment(name)}.enabled=false"
                for name in user_servers
                if name != server_name and name not in allowed
            ]
            overrides = [*disabled_overrides, *overrides]
        if skill_files:
            overrides.append(_skills_config_override(skill_files, agent.skills))
        for override in overrides:
            command.extend(["--config", override])
        command.extend(agent.extra_codex_args)
        # Teamwork 的直接 MCP 工具和项目指令隔离必须覆盖全部自定义参数。
        command.extend(
            [
                "--config",
                _TEAMWORK_MCP_DIRECT_TOOL_OVERRIDE,
                "--config",
                "project_doc_max_bytes=0",
            ]
        )
        command.append("-")
        if use_managed_sandbox:
            return wrap_managed_sandbox_command(
                codex_binary=codex_binary,
                workspace=repository.workspace,
                agent=agent,
                inner_command=command,
                environment=active_environment,
                ipc_directory=mcp_bridge.directory if mcp_bridge is not None else None,
                codex_runtime_directory=codex_runtime_directory,
            )
        return command

    def child_environment(
        self,
        agent_environment: dict[str, str] | None = None,
        *,
        temporary_home: TemporaryAgentHome | None = None,
        temporary_codex_home: TemporaryCodexHome | None = None,
    ) -> dict[str, str]:
        """只继承运行必需变量，再叠加 Agent 明确声明的环境。"""

        environment = selected_environment(BASE_ENVIRONMENT_NAMES)
        environment.update(agent_environment or {})
        if self.config.runtime.codex_home is not None:
            environment["CODEX_HOME"] = str(self.config.runtime.codex_home)
        if temporary_home is not None:
            temporary_home.apply_environment(
                environment,
                codex_home=codex_home(self.config.runtime.codex_home),
            )
        if temporary_codex_home is not None:
            temporary_codex_home.apply_environment(environment)
        # 即使 Agent 环境显式同名，也不能重新注入扫描器的 Provider 凭据。
        remove_environment_names(
            environment,
            (provider.token_env for provider in self.config.providers.values()),
        )
        environment["PYTHONUNBUFFERED"] = "1"
        return environment

    async def run(
        self,
        *,
        run_id: str,
        root_run_id: str,
        parent_run_id: str | None,
        agent_name: str,
        agent: AgentConfig,
        repository: RepositoryConfig,
        context: InvocationContext,
        prompt: str,
        process_environment: dict[str, str] | None = None,
        redactor: SecretRedactor | None = None,
        log_callback: LogCallback | None = None,
        cancel_check: CancelCheck | None = None,
    ) -> AgentResult:
        """准备 Skill 与临时 HOME，并保证 Codex 退出后立即清理。"""

        temporary_home: TemporaryAgentHome | None = None
        temporary_codex_home: TemporaryCodexHome | None = None
        projection: SkillProjection | None = None
        mcp_broker: ManagedMcpBroker | None = None
        managed_requested = (
            self.config.runtime.managed_sandbox.enabled
            and agent.sandbox != "danger-full-access"
        )
        try:
            if agent.home_mode == "temporary":
                temporary_home = TemporaryAgentHome.create(run_id)
            if managed_requested:
                temporary_codex_home = TemporaryCodexHome.create(
                    run_id,
                    source_home=codex_home(self.config.runtime.codex_home),
                )
            projection = SkillProjection(
                repository.workspace,
                {
                    skill_id: skill.path
                    for skill_id, skill in self.config.skills.items()
                },
                self.config.revision,
            ).prepare()
            managed_inspection: ManagedSandboxInspection | None = None
            mcp_bridge_error: str | None = None
            if managed_requested:
                managed_inspection = await asyncio.to_thread(
                    inspect_managed_sandbox,
                    self.config.runtime.codex_binary,
                    self.config.runtime.codex_home,
                )
                if managed_inspection.available:
                    try:
                        mcp_broker = await ManagedMcpBroker.start(
                            run_id=run_id,
                            config_path=self.config.config_path,
                            encoded_context=encode_invocation_context(context),
                            base_environment=self.child_environment(
                                process_environment,
                                temporary_home=temporary_home,
                                temporary_codex_home=temporary_codex_home,
                            ),
                            response_timeout_seconds=max(
                                1.0,
                                float(
                                    self.config.runtime.mcp_tool_timeout_seconds
                                )
                                - 1.0,
                            ),
                        )
                    except Exception as exc:
                        mcp_bridge_error = (
                            "Teamwork 托管沙盒的 MCP Bridge 启动失败："
                            f"{type(exc).__name__}: {exc}"
                        )
            return await self._run_with_projection(
                run_id=run_id,
                root_run_id=root_run_id,
                parent_run_id=parent_run_id,
                agent_name=agent_name,
                agent=agent,
                repository=repository,
                context=context,
                prompt=prompt,
                process_environment=process_environment,
                redactor=redactor,
                log_callback=log_callback,
                cancel_check=cancel_check,
                skill_files=projection.skill_files,
                git_excludes_file=projection.marker if self.config.skills else None,
                temporary_home=temporary_home,
                temporary_codex_home=temporary_codex_home,
                managed_inspection=managed_inspection,
                mcp_bridge=mcp_broker.channel if mcp_broker is not None else None,
                mcp_bridge_error=mcp_bridge_error,
            )
        finally:
            try:
                if mcp_broker is not None:
                    await mcp_broker.close()
            finally:
                try:
                    if projection is not None:
                        projection.cleanup()
                finally:
                    try:
                        if temporary_codex_home is not None:
                            codex_path = str(temporary_codex_home.path)
                            cleanup_error = temporary_codex_home.cleanup()
                            if log_callback is not None:
                                event_type = (
                                    "run.codex_home_cleanup_failed"
                                    if cleanup_error
                                    else "run.codex_home_cleaned"
                                )
                                payload: dict[str, Any] = {
                                    "mode": "temporary",
                                    "path": codex_path,
                                    "cleaned": cleanup_error is None,
                                }
                                if cleanup_error:
                                    payload["error"] = cleanup_error
                                try:
                                    await log_callback("system", event_type, payload)
                                except Exception:
                                    # 清理已经完成尝试，日志失败不能覆盖任务结果。
                                    pass
                    finally:
                        if temporary_home is not None:
                            home_path = str(temporary_home.path)
                            cleanup_error = temporary_home.cleanup()
                            if log_callback is not None:
                                event_type = (
                                    "run.home_cleanup_failed"
                                    if cleanup_error
                                    else "run.home_cleaned"
                                )
                                payload = {
                                    "mode": "temporary",
                                    "path": home_path,
                                    "cleaned": cleanup_error is None,
                                }
                                if cleanup_error:
                                    payload["error"] = cleanup_error
                                try:
                                    await log_callback("system", event_type, payload)
                                except Exception:
                                    # 清理已经完成尝试，日志失败不能覆盖任务结果。
                                    pass

    async def _run_with_projection(
        self,
        *,
        run_id: str,
        root_run_id: str,
        parent_run_id: str | None,
        agent_name: str,
        agent: AgentConfig,
        repository: RepositoryConfig,
        context: InvocationContext,
        prompt: str,
        process_environment: dict[str, str] | None = None,
        redactor: SecretRedactor | None = None,
        log_callback: LogCallback | None = None,
        cancel_check: CancelCheck | None = None,
        skill_files: Mapping[str, Path],
        git_excludes_file: Path | None,
        temporary_home: TemporaryAgentHome | None,
        temporary_codex_home: TemporaryCodexHome | None,
        managed_inspection: ManagedSandboxInspection | None,
        mcp_bridge: McpBridgeChannel | None,
        mcp_bridge_error: str | None,
    ) -> AgentResult:
        """流式执行 Codex CLI；超时后终止整个进程组。"""

        active_redactor = redactor or SecretRedactor(())

        async def emit(
            stream: str,
            event_type: str,
            payload: str | dict[str, Any],
        ) -> None:
            """日志持久化失败不应中断 Codex 主任务。"""

            if log_callback is None:
                return
            try:
                await log_callback(stream, event_type, payload)
            except Exception:
                return

        child_environment = self.child_environment(
            process_environment,
            temporary_home=temporary_home,
            temporary_codex_home=temporary_codex_home,
        )
        if temporary_home is not None:
            await emit(
                "system",
                "run.home_prepared",
                {
                    "mode": "temporary",
                    "path": str(temporary_home.path),
                    "bridges": list(temporary_home.bridges),
                },
            )
        if temporary_codex_home is not None:
            await emit(
                "system",
                "run.codex_home_prepared",
                {
                    "mode": "temporary",
                    "path": str(temporary_codex_home.path),
                    "bridges": list(temporary_codex_home.bridges),
                },
            )
        if git_excludes_file is not None:
            _add_git_excludes_file(child_environment, git_excludes_file)
        version_error = await asyncio.to_thread(
            validate_codex_version,
            self.config.runtime.codex_binary,
            self.config.runtime.expected_codex_version,
            self.config.runtime.codex_home,
        )
        if version_error:
            await emit("system", "run.version_mismatch", version_error)
            return AgentResult(
                run_id=run_id,
                root_run_id=root_run_id,
                parent_run_id=parent_run_id,
                agent_name=agent_name,
                status="failed",
                error=version_error,
            )
        managed_requested = (
            self.config.runtime.managed_sandbox.enabled
            and agent.sandbox != "danger-full-access"
        )
        use_managed_sandbox = False
        if managed_requested:
            inspection = managed_inspection or await asyncio.to_thread(
                inspect_managed_sandbox,
                self.config.runtime.codex_binary,
                self.config.runtime.codex_home,
            )
            unavailable_error = inspection.error
            if inspection.available and mcp_bridge is None:
                unavailable_error = (
                    mcp_bridge_error
                    or "Teamwork 托管沙盒的 MCP Bridge 未能准备完成"
                )
            if inspection.available and mcp_bridge is not None:
                use_managed_sandbox = True
                await emit(
                    "system",
                    "run.sandbox_prepared",
                    {
                        "mode": agent.sandbox,
                        "managed": True,
                        "platform": inspection.platform,
                        "backend": inspection.backend,
                        "network_mode": (
                            "disabled"
                            if not agent.network_access
                            else "allowlist"
                            if agent.network_domains
                            else "full"
                        ),
                        "network_domain_count": len(agent.network_domains),
                        "mcp_bridge": "file-channel",
                    },
                )
            elif self.config.runtime.managed_sandbox.fail_closed:
                error = unavailable_error or "Teamwork 外层沙盒能力不可用"
                await emit(
                    "system",
                    "run.sandbox_unavailable",
                    {
                        "platform": inspection.platform,
                        "backend": inspection.backend,
                        "error": error,
                        "fail_closed": True,
                    },
                )
                return AgentResult(
                    run_id=run_id,
                    root_run_id=root_run_id,
                    parent_run_id=parent_run_id,
                    agent_name=agent_name,
                    status="failed",
                    error=error,
                )
            else:
                await emit(
                    "system",
                    "run.sandbox_fallback",
                    {
                        "mode": agent.sandbox,
                        "managed": False,
                        "platform": inspection.platform,
                        "backend": inspection.backend,
                        "error": unavailable_error,
                        "fallback": "codex_internal_sandbox",
                    },
                )
        elif agent.sandbox == "danger-full-access":
            await emit(
                "system",
                "run.sandbox_prepared",
                {
                    "mode": agent.sandbox,
                    "managed": False,
                    "backend": None,
                    "network_mode": "full",
                    "network_domain_count": 0,
                },
            )
        command = self.build_command(
            agent,
            repository,
            context,
            skill_files,
            managed_sandbox=use_managed_sandbox,
            environment=child_environment,
            mcp_bridge=mcp_bridge if use_managed_sandbox else None,
            codex_runtime_directory=(
                temporary_codex_home.path
                if use_managed_sandbox and temporary_codex_home is not None
                else None
            ),
        )
        if cancel_check is not None and await cancel_check():
            error = "运行在 Codex CLI 启动前被管理员取消"
            await emit("system", "run.cancelled", error)
            return AgentResult(
                run_id=run_id,
                root_run_id=root_run_id,
                parent_run_id=parent_run_id,
                agent_name=agent_name,
                status="cancelled",
                error=error,
            )
        process = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=repository.workspace,
            env=child_environment,
            limit=_CODEX_STREAM_LIMIT_BYTES,
            **process_group_options(),
        )
        assert process.stdin is not None
        assert process.stdout is not None
        assert process.stderr is not None
        try:
            process.stdin.write(prompt.encode("utf-8"))
            await process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            # 子进程若在读取 Prompt 前退出，仍继续收集 stdout/stderr 和退出码。
            pass
        finally:
            process.stdin.close()
            with suppress(BrokenPipeError, ConnectionResetError):
                await process.stdin.wait_closed()

        events: list[dict[str, Any]] = []
        final_message = ""
        thread_id: str | None = None
        usage: dict[str, Any] = {}
        stream_error: str | None = None
        started_at = time.monotonic()
        last_progress_at = started_at
        stream_failure_event = asyncio.Event()

        async def record_stream_failure(stream: str, error: Exception) -> None:
            """记录安全的流读取错误，并通知主循环尽快终止子进程。"""

            nonlocal stream_error
            detail = active_redactor.text(str(error)).strip()
            if len(detail) > _STREAM_ERROR_DETAIL_LIMIT:
                detail = f"{detail[:_STREAM_ERROR_DETAIL_LIMIT]}…"
            message = f"读取 Codex {stream} 失败：{type(error).__name__}"
            if detail:
                message = f"{message}: {detail}"
            if stream_error is None:
                stream_error = message
            stream_failure_event.set()
            await emit(
                "system",
                "run.stream_failed",
                {
                    "stream": stream,
                    "error_type": type(error).__name__,
                    "message": message,
                },
            )

        async def read_stdout() -> None:
            """逐行解析并持久化 Codex JSONL。"""

            nonlocal final_message, thread_id, usage, stream_error, last_progress_at
            try:
                while raw_line := await process.stdout.readline():
                    # 只有 stdout / JSONL 代表 Agent 有实际语义进展；重复诊断 stderr 不续期。
                    last_progress_at = time.monotonic()
                    text_line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
                    try:
                        parsed = json.loads(text_line)
                    except json.JSONDecodeError:
                        redacted_text = active_redactor.text(text_line)
                        await emit("stdout", "text", redacted_text)
                        continue
                    if not isinstance(parsed, dict):
                        continue
                    event = active_redactor.data(parsed)
                    events.append(event)
                    event_type = str(event.get("type") or "jsonl")
                    await emit("stdout", event_type, event)
                    if event_type == "thread.started":
                        thread_id = str(event.get("thread_id") or "") or None
                    if event_type == "item.completed":
                        item = event.get("item") or {}
                        if isinstance(item, dict) and item.get("type") == "agent_message":
                            final_message = str(item.get("text") or "")
                    if event_type == "turn.completed" and isinstance(
                        event.get("usage"), dict
                    ):
                        usage = event["usage"]
                    if event_type in {"turn.failed", "error"}:
                        stream_error = json.dumps(event, ensure_ascii=False)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                await record_stream_failure("stdout", error)

        async def read_stderr() -> None:
            """逐行保存 Codex 进度与诊断输出。"""

            try:
                while raw_line := await process.stderr.readline():
                    text_line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
                    await emit("stderr", "text", active_redactor.text(text_line))
            except asyncio.CancelledError:
                raise
            except Exception as error:
                await record_stream_failure("stderr", error)

        stdout_task = asyncio.create_task(read_stdout())
        stderr_task = asyncio.create_task(read_stderr())
        wait_task = asyncio.create_task(process.wait())
        stop_reason: str | None = None
        idle_timeout = (
            agent.idle_timeout_seconds
            or self.config.runtime.agent_idle_timeout_seconds
        )

        async def terminate_process_group() -> None:
            """按平台先请求停止、再强制结束当前 Codex 及其子进程。"""

            with suppress(ProcessLookupError, PermissionError):
                terminate_process(process.pid, force=False, tree=True)
            try:
                await asyncio.wait_for(
                    asyncio.shield(wait_task),
                    timeout=_PROCESS_TERMINATE_GRACE_SECONDS,
                )
            except TimeoutError:
                # asyncio 的 wait() 可能等待继承管道的后代关闭；已有退出码就不再误判主进程存活。
                if process.returncode is None:
                    with suppress(ProcessLookupError, PermissionError):
                        terminate_process(process.pid, force=True, tree=True)
                    try:
                        await asyncio.wait_for(
                            asyncio.shield(wait_task),
                            timeout=_PROCESS_KILL_GRACE_SECONDS,
                        )
                    except TimeoutError:
                        pass
            if not wait_task.done():
                wait_task.cancel()
                await asyncio.gather(wait_task, return_exceptions=True)
            if process.returncode is None:
                await emit(
                    "system",
                    "run.process_termination_incomplete",
                    "Codex 进程组已收到强制终止信号，但主进程退出状态仍不可用",
                )

        while not wait_task.done():
            try:
                await asyncio.wait_for(asyncio.shield(wait_task), timeout=0.5)
                break
            except TimeoutError:
                pass
            now = time.monotonic()
            if stream_failure_event.is_set():
                stop_reason = "stream_error"
            if cancel_check is not None:
                try:
                    if await cancel_check():
                        stop_reason = "cancelled"
                except Exception:
                    # 取消检查短暂失败不能误杀正常运行，下一轮会继续检查。
                    pass
            if stop_reason is None and now - started_at >= agent.timeout_seconds:
                stop_reason = "total_timeout"
            if stop_reason is None and now - last_progress_at >= idle_timeout:
                stop_reason = "idle_timeout"
            if stop_reason is not None:
                await terminate_process_group()
                break

        stream_tasks = {stdout_task, stderr_task}
        _, pending_stream_tasks = await asyncio.wait(
            stream_tasks,
            timeout=_STREAM_DRAIN_TIMEOUT_SECONDS,
        )
        if pending_stream_tasks:
            for task in pending_stream_tasks:
                task.cancel()
            _, still_pending = await asyncio.wait(
                pending_stream_tasks,
                timeout=_STREAM_CANCEL_TIMEOUT_SECONDS,
            )
            message = (
                "Codex 子进程结束后，stdout/stderr 未在宽限期内关闭；"
                "已停止等待剩余输出"
            )
            if stream_error is None:
                stream_error = message
            await emit(
                "system",
                "run.stream_drain_timed_out",
                {
                    "message": message,
                    "pending_streams": len(pending_stream_tasks),
                    "still_pending_streams": len(still_pending),
                },
            )

        # 所有已结束任务都必须取出异常，避免 asyncio 产生未读取异常警告。
        for task in stream_tasks:
            if not task.done() or task.cancelled():
                continue
            error = task.exception()
            if error is not None:
                await record_stream_failure("unknown", error)

        if stop_reason == "cancelled":
            error = "运行已由管理员取消"
            await emit("system", "run.cancelled", error)
            return AgentResult(
                run_id=run_id,
                root_run_id=root_run_id,
                parent_run_id=parent_run_id,
                agent_name=agent_name,
                status="cancelled",
                events=events[-self.config.runtime.max_jsonl_events :],
                error=error,
            )
        if stop_reason == "total_timeout":
            error = f"Codex CLI 运行超过 {agent.timeout_seconds} 秒"
            await emit("system", "run.timed_out", error)
            return AgentResult(
                run_id=run_id,
                root_run_id=root_run_id,
                parent_run_id=parent_run_id,
                agent_name=agent_name,
                status="timed_out",
                events=events[-self.config.runtime.max_jsonl_events :],
                error=error,
            )
        if stop_reason == "idle_timeout":
            error = f"Codex CLI 连续 {idle_timeout} 秒没有 stdout / JSONL 进展"
            await emit("system", "run.idle_timed_out", error)
            return AgentResult(
                run_id=run_id,
                root_run_id=root_run_id,
                parent_run_id=parent_run_id,
                agent_name=agent_name,
                status="timed_out",
                events=events[-self.config.runtime.max_jsonl_events :],
                error=error,
            )

        max_events = self.config.runtime.max_jsonl_events
        if len(events) > max_events:
            events = events[-max_events:]
        if process.returncode != 0:
            error = stream_error or f"Codex CLI 退出码：{process.returncode}"
            status = "failed"
        else:
            error = stream_error
            status = "failed" if stream_error else "completed"
        return AgentResult(
            run_id=run_id,
            root_run_id=root_run_id,
            parent_run_id=parent_run_id,
            agent_name=agent_name,
            status=status,
            final_message=final_message,
            thread_id=thread_id,
            usage=usage,
            events=events,
            error=error,
        )
