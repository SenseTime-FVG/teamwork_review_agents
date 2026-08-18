"""Codex CLI JSONL 进程运行器。"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import signal
import sys
import time
from contextlib import suppress
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping

from .config import AgentConfig, AppConfig, RepositoryConfig
from .codex_settings import (
    agent_overrides,
    codex_home,
    read_user_mcp_servers,
    runtime_overrides,
    validate_codex_version,
)
from .environment import SecretRedactor
from .models import AgentResult, InvocationContext
from .skill_files import SkillProjection


LogCallback = Callable[[str, str, str | dict[str, Any]], Awaitable[None]]
CancelCheck = Callable[[], Awaitable[bool]]

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
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
}


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

    def build_command(
        self,
        agent: AgentConfig,
        repository: RepositoryConfig,
        context: InvocationContext,
        skill_files: Mapping[str, Path] | None = None,
    ) -> list[str]:
        """构造显式叠加 Teamwork 默认和 Agent 覆盖的 Codex 命令。"""

        server_name = "teamwork_agent_gateway"
        command = [
            self.config.runtime.codex_binary,
            "exec",
            "--json",
            "--ephemeral",
            "--sandbox",
            agent.sandbox,
            "--cd",
            str(repository.workspace),
        ]
        if agent.model:
            command.extend(["--model", agent.model])
        if agent.skip_git_repo_check:
            command.append("--skip-git-repo-check")
        if agent.output_schema:
            command.extend(["--output-schema", str(agent.output_schema)])

        mcp_args = ["-m", "teamwork_review_agents.mcp_server"]
        context_value = encode_invocation_context(context)
        overrides = [
            *runtime_overrides(self.config.runtime.codex),
            *agent_overrides(agent),
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
            f"mcp_servers.{server_name}.enabled_tools=[\"invoke_agent\"]",
            f"mcp_servers.{server_name}.default_tools_approval_mode=\"approve\"",
            (
                f"mcp_servers.{server_name}.env.TEAMWORK_CONFIG_PATH="
                f"{_toml_string(str(self.config.config_path))}"
            ),
            (
                f"mcp_servers.{server_name}.env.TEAMWORK_INVOCATION_CONTEXT="
                f"{_toml_string(context_value)}"
            ),
        ]
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
        command.append("-")
        return command

    def child_environment(
        self,
        agent_environment: dict[str, str] | None = None,
    ) -> dict[str, str]:
        """只继承运行必需变量，再叠加 Agent 明确声明的环境。"""

        environment = {
            name: value
            for name, value in os.environ.items()
            if name in BASE_ENVIRONMENT_NAMES
        }
        environment.update(agent_environment or {})
        if self.config.runtime.codex_home is not None:
            environment["CODEX_HOME"] = str(self.config.runtime.codex_home)
        # 即使 Agent 环境显式同名，也不能重新注入扫描器的 Provider 凭据。
        for provider in self.config.providers.values():
            environment.pop(provider.token_env, None)
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
        """准备当前工作区的 Skill 投影，并保证 Codex 退出后立即清理。"""

        projection = SkillProjection(
            repository.workspace,
            {
                skill_id: skill.path
                for skill_id, skill in self.config.skills.items()
            },
            self.config.revision,
        ).prepare()
        try:
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
            )
        finally:
            projection.cleanup()

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
    ) -> AgentResult:
        """流式执行 Codex CLI；超时后终止整个进程组。"""

        command = self.build_command(agent, repository, context, skill_files)
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

        child_environment = self.child_environment(process_environment)
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
            start_new_session=True,
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

        async def read_stdout() -> None:
            """逐行解析并持久化 Codex JSONL。"""

            nonlocal final_message, thread_id, usage, stream_error, last_progress_at
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
                if event_type == "turn.completed" and isinstance(event.get("usage"), dict):
                    usage = event["usage"]
                if event_type in {"turn.failed", "error"}:
                    stream_error = json.dumps(event, ensure_ascii=False)

        async def read_stderr() -> None:
            """逐行保存 Codex 进度与诊断输出。"""

            while raw_line := await process.stderr.readline():
                text_line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
                await emit("stderr", "text", active_redactor.text(text_line))

        stdout_task = asyncio.create_task(read_stdout())
        stderr_task = asyncio.create_task(read_stderr())
        wait_task = asyncio.create_task(process.wait())
        stop_reason: str | None = None
        idle_timeout = (
            agent.idle_timeout_seconds
            or self.config.runtime.agent_idle_timeout_seconds
        )

        async def terminate_process_group() -> None:
            """先温和、再强制结束当前 Codex 及同进程组子进程。"""

            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGTERM)
            try:
                await asyncio.wait_for(asyncio.shield(wait_task), timeout=5)
            except TimeoutError:
                with suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                await wait_task

        while not wait_task.done():
            try:
                await asyncio.wait_for(asyncio.shield(wait_task), timeout=0.5)
                break
            except TimeoutError:
                pass
            now = time.monotonic()
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
        await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)

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
