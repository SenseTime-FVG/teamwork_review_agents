"""Codex CLI JSONL 进程运行器。"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import signal
import sys
from contextlib import suppress
from pathlib import Path
from typing import Any, Awaitable, Callable

from .config import AgentConfig, AppConfig, RepositoryConfig
from .environment import SecretRedactor
from .models import AgentResult, InvocationContext


LogCallback = Callable[[str, str, str | dict[str, Any]], Awaitable[None]]

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

    payload = context.model_dump_json(exclude={"event": {"old": {"raw"}, "new": {"raw"}}})
    return base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii")


def decode_invocation_context(value: str) -> InvocationContext:
    """从环境变量恢复 MCP 调用上下文。"""

    payload = base64.urlsafe_b64decode(value.encode("ascii")).decode("utf-8")
    return InvocationContext.model_validate_json(payload)


def _toml_string(value: str) -> str:
    """使用 JSON 字符串语法生成兼容 TOML 的转义值。"""

    return json.dumps(value, ensure_ascii=False)


class CodexRunner:
    """启动 `codex exec` 并提取最终消息、线程和用量。"""

    def __init__(self, config: AppConfig) -> None:
        self.config = config

    def build_command(
        self,
        agent: AgentConfig,
        repository: RepositoryConfig,
        context: InvocationContext,
    ) -> list[str]:
        """构造不加载用户工具配置的可审计 Codex 命令。"""

        server_name = "teamwork_agent_gateway"
        command = [
            self.config.runtime.codex_binary,
            "exec",
            "--json",
            "--ephemeral",
            "--ignore-user-config",
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
            f"mcp_servers.{server_name}.command={_toml_string(sys.executable)}",
            f"mcp_servers.{server_name}.args={json.dumps(mcp_args)}",
            f"mcp_servers.{server_name}.required=true",
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
        for provider in self.config.providers.values():
            environment.pop(provider.token_env, None)
        environment.update(agent_environment or {})
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
    ) -> AgentResult:
        """流式执行 Codex CLI；超时后终止整个进程组。"""

        command = self.build_command(agent, repository, context)
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

        process = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=repository.workspace,
            env=self.child_environment(process_environment),
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

        async def read_stdout() -> None:
            """逐行解析并持久化 Codex JSONL。"""

            nonlocal final_message, thread_id, usage, stream_error
            while raw_line := await process.stdout.readline():
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
        timed_out = False
        try:
            await asyncio.wait_for(process.wait(), timeout=agent.timeout_seconds)
        except TimeoutError:
            timed_out = True
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGTERM)
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
            except TimeoutError:
                with suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                await process.wait()
        await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)

        if timed_out:
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
