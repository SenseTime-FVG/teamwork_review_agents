"""Teamwork 内嵌 Agent 使用的本地函数工具。"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
import sys
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlsplit

from .config import AgentConfig, AppConfig, RepositoryConfig
from .managed_comments import ManagedCommentService
from .managed_sandbox import wrap_managed_sandbox_command
from .models import InvocationContext
from .process_control import process_group_options, terminate_process
from .state import StateStore
from .subprocess_utils import ProcessLaunch, resolve_executable
from .sandbox_environment import sandbox_executable_environment
from .codex_executable import resolve_codex_executable
from .sandbox_git import SandboxGitError, classify_git_failure, current_sandbox_git


CancelCheck = Callable[[], Awaitable[bool]]
ProgressCallback = Callable[[], None]
InvokeAgentStartedCallback = Callable[[dict[str, Any]], Awaitable[None]]
InvokeAgentCallback = Callable[
    [
        InvocationContext,
        str,
        str,
        dict[str, Any] | None,
        InvokeAgentStartedCallback | None,
    ],
    Awaitable[dict[str, Any]],
]

_TOOL_OUTPUT_LIMIT_BYTES = 1_000_000
_COMMAND_LIMIT_BYTES = 256 * 1024
_PATCH_LIMIT_BYTES = 4 * 1024 * 1024
_PROCESS_TERMINATE_GRACE_SECONDS = 3.0
_PROCESS_KILL_GRACE_SECONDS = 1.0


def teamwork_function_tools(
    *,
    allow_sub_agents: bool,
    allow_publish_comment: bool = False,
) -> list[dict[str, Any]]:
    """生成 Codex Responses 接受的 Teamwork 函数工具定义。"""

    tools: list[dict[str, Any]] = [
        {
            "type": "function",
            "name": "execute_command",
            "description": (
                "在当前 Agent 工作区中执行一条 shell 命令。"
                "workdir 必须是工作区内的相对目录；输出会被限制长度。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "需要执行的 shell 命令。",
                    },
                    "workdir": {
                        "type": "string",
                        "description": "相对当前工作区的目录，默认是工作区根目录。",
                    },
                    "timeout_seconds": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 1800,
                        "description": "本条命令超时，默认 600 秒。",
                    },
                },
                "required": ["command"],
                "additionalProperties": False,
            },
        },
        {
            "type": "function",
            "name": "apply_patch",
            "description": (
                "把标准 unified diff 应用到当前工作区。"
                "只允许工作区相对路径，不允许修改 .git。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "patch": {
                        "type": "string",
                        "description": "包含 diff --git、---、+++ 和 hunk 的统一补丁。",
                    }
                },
                "required": ["patch"],
                "additionalProperties": False,
            },
        },
    ]
    if allow_sub_agents:
        tools.append(
            {
                "type": "function",
                "name": "invoke_agent",
                "description": (
                    "调用当前 Agent 白名单中的 sub-agent，并等待结构化结果。"
                    "只在任务确实需要委托时使用。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "agent_name": {
                            "type": "string",
                            "description": "配置中允许调用的 sub-agent 名称。",
                        },
                        "task": {
                            "type": "string",
                            "description": "边界清晰、可独立完成的委托任务。",
                        },
                        "extra_context": {
                            "type": "object",
                            "description": "可选的结构化补充上下文。",
                            "additionalProperties": True,
                        },
                    },
                    "required": ["agent_name", "task"],
                    "additionalProperties": False,
                },
            }
        )
    if allow_publish_comment:
        tools.append(
            {
                "type": "function",
                "name": "publish_comment",
                "description": (
                    "发布或更新当前 Agent 在本次 PR/MR 源版本代次的托管顶层评论。"
                    "目标分支变化会更新当前评论，源分支变化会创建下一代评论。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "body": {
                            "type": "string",
                            "description": "需要发布的完整 Markdown 顶层评论正文。",
                        }
                    },
                    "required": ["body"],
                    "additionalProperties": False,
                },
            }
        )
    return tools


class ModelToolExecutor:
    """校验并执行 Teamwork 自有函数工具。"""

    def __init__(
        self,
        *,
        config: AppConfig,
        agent: AgentConfig,
        repository: RepositoryConfig,
        context: InvocationContext,
        environment: dict[str, str],
        managed_sandbox: bool,
        cancel_check: CancelCheck | None,
        progress_callback: ProgressCallback,
        invoke_agent_callback: InvokeAgentCallback | None,
        codex_runtime_directory: Path | None = None,
    ) -> None:
        self.config = config
        self.agent = agent
        self.repository = repository
        self.context = context
        self.environment = environment
        self.managed_sandbox = managed_sandbox
        self.cancel_check = cancel_check
        self.progress_callback = progress_callback
        self.invoke_agent_callback = invoke_agent_callback
        self.codex_runtime_directory = codex_runtime_directory

    async def execute(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        invoke_agent_started_callback: InvokeAgentStartedCallback | None = None,
    ) -> dict[str, Any]:
        """按工具名分派调用并返回可序列化结果。"""

        if name == "execute_command":
            return await self._execute_command(arguments)
        if name == "apply_patch":
            return await self._apply_patch(arguments)
        if name == "invoke_agent":
            return await self._invoke_agent(
                arguments,
                started_callback=invoke_agent_started_callback,
            )
        if name == "publish_comment":
            return await self._publish_comment(arguments)
        raise ValueError(f"未知 Teamwork 工具：{name}")

    async def _publish_comment(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """把顶层评论交给 Teamwork 托管服务发布或更新。"""

        body = arguments.get("body")
        if not isinstance(body, str):
            raise ValueError("publish_comment.body 必须是字符串")
        store = StateStore(self.config.database.path)
        await asyncio.to_thread(store.initialize)
        return await ManagedCommentService(
            self.config,
            store,
        ).publish_agent_comment(self.context, body)

    async def _execute_command(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """在受约束工作目录内执行跨平台 shell。"""

        command = arguments.get("command")
        if not isinstance(command, str) or not command.strip():
            raise ValueError("execute_command.command 必须是非空字符串")
        if len(command.encode("utf-8")) > _COMMAND_LIMIT_BYTES:
            raise ValueError("execute_command.command 不能超过 256 KiB")
        workdir_value = arguments.get("workdir", ".")
        if not isinstance(workdir_value, str):
            raise ValueError("execute_command.workdir 必须是字符串")
        workdir = _resolve_workdir(self.repository.workspace, workdir_value)
        timeout_value = arguments.get("timeout_seconds", 600)
        if not isinstance(timeout_value, int) or isinstance(timeout_value, bool):
            raise ValueError("execute_command.timeout_seconds 必须是整数")
        timeout_seconds = max(
            1,
            min(
                timeout_value,
                1800,
                self.config.runtime.mcp_tool_timeout_seconds,
            ),
        )
        inner_command = shell_command(
            command,
            workdir=workdir if self.managed_sandbox else None,
            environment=self.environment,
        )
        process_command = self._wrap(inner_command)
        result = await self._run_process(
            process_command,
            cwd=workdir,
            timeout_seconds=timeout_seconds,
        )
        if self.managed_sandbox and current_sandbox_git() is not None and result["exit_code"] != 0:
            failure = classify_git_failure(f"{result['stderr']}\n{result['stdout']}")
            if failure is not None:
                raise failure
        return {
            "exit_code": result["exit_code"],
            "stdout": result["stdout"],
            "stderr": result["stderr"],
            "timed_out": result["timed_out"],
            "truncated": result["truncated"],
            "workdir": str(workdir.relative_to(self.repository.workspace.resolve()) or "."),
        }

    async def check_git_https(self) -> dict[str, Any]:
        """在真实工具沙盒内只读探测 HTTPS；不触碰远端引用，不输出任何凭据。"""

        git_context = current_sandbox_git()
        if not self.managed_sandbox or git_context is None:
            return {"status": "skipped", "reason": "当前不是 Windows 托管沙盒"}
        if not self.agent.network_access:
            return {"status": "skipped", "reason": "Agent 禁止联网，未执行 HTTPS 探测"}
        if not (self.repository.workspace / ".git").exists():
            return {"status": "skipped", "reason": "当前不是 Git 工作区"}
        timeout = min(30, self.config.runtime.mcp_tool_timeout_seconds)

        async def probe(command: list[str], stage: str) -> dict[str, Any]:
            """沿用有界输出、进程树取消和相同权限档案，绝不回退到宿主执行。"""

            try:
                process_task = asyncio.create_task(self._run_process(
                    self._wrap(command), cwd=self.repository.workspace, timeout_seconds=timeout,
                ))
                try:
                    # 完整 CLI 模式尚未启动自身看门狗，自检也必须响应管理员取消。
                    while not process_task.done():
                        await asyncio.wait({process_task}, timeout=0.25)
                        if self.cancel_check is not None and await self.cancel_check():
                            raise asyncio.CancelledError
                    result = await process_task
                finally:
                    if not process_task.done():
                        process_task.cancel()
                    await asyncio.gather(process_task, return_exceptions=True)
            except OSError as exc:
                raise SandboxGitError(
                    f"Git HTTPS 自检无法启动（{stage}）：{exc}",
                    error_code="sandbox_git_probe_launch_failed",
                ) from exc
            if result["timed_out"]:
                raise SandboxGitError(
                    f"Git HTTPS 自检超时（{stage}，{timeout} 秒）",
                    error_code="sandbox_git_probe_timeout", retryable=True,
                )
            return result

        git = resolve_executable("git", self.environment)
        remote = await probe([git, "remote", "get-url", "origin"], "读取远端")
        if remote["exit_code"] != 0:
            output = f"{remote['stderr']}\n{remote['stdout']}"
            if "no such remote" in output.lower():
                return {"status": "skipped", "reason": "当前工作区没有 origin"}
            raise classify_git_failure(output) or SandboxGitError(
                f"无法读取 Git origin：{output[-1200:]}", error_code="sandbox_git_remote_probe_failed",
                retryable=True,
            )
        try:
            remote_url = urlsplit(remote["stdout"].strip())
            is_https = remote_url.scheme.lower() == "https" and remote_url.hostname
        except ValueError:
            is_https = False
        if not is_https:
            return {"status": "skipped", "reason": "origin 不是 HTTPS 远端"}
        if git_context.probe_command is not None:
            helper = await probe(git_context.probe_command, "askpass helper")
            if helper["exit_code"] != 0 or helper["stdout"].strip() != "teamwork-askpass-ready":
                # 不回显 helper stdout，避免错误 helper 意外打印 Token。
                raise SandboxGitError(
                    f"沙盒内 askpass helper 自检失败：{helper['stderr'][-1200:]}",
                    error_code="sandbox_git_askpass_unavailable",
                )
        result = await probe([git, "ls-remote", "--exit-code", "origin", "HEAD"], "HTTPS 远端")
        if result["exit_code"] != 0:
            output = f"{result['stderr']}\n{result['stdout']}"
            raise classify_git_failure(output) or SandboxGitError(
                f"Git HTTPS 远端探测失败：{output[-1200:]}",
                error_code="sandbox_git_remote_probe_failed", retryable=True,
            )
        match = re.search(r"^([0-9a-fA-F]{40}|[0-9a-fA-F]{64})\s+HEAD\s*$", result["stdout"], re.MULTILINE)
        if match is None:
            raise SandboxGitError("Git HTTPS 探测未返回有效 HEAD SHA", error_code="sandbox_git_invalid_remote_head")
        return {"status": "ready", "ssl_backend": "openssl", "git_binary": git,
                "remote": "origin", "host": remote_url.hostname, "sha": match.group(1)}

    async def _apply_patch(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """先校验路径和补丁，再通过 git apply 原子落盘。"""

        patch = arguments.get("patch")
        if not isinstance(patch, str) or not patch.strip():
            raise ValueError("apply_patch.patch 必须是非空 unified diff")
        if len(patch.encode("utf-8")) > _PATCH_LIMIT_BYTES:
            raise ValueError("apply_patch.patch 不能超过 4 MiB")
        paths = validate_unified_diff(patch)
        git = resolve_executable("git", self.environment)
        check_command = self._wrap(
            [git, "-C", str(self.repository.workspace), "apply", "--check", "--whitespace=nowarn", "-"]
        )
        checked = await self._run_process(
            check_command,
            cwd=self.repository.workspace,
            timeout_seconds=min(120, self.config.runtime.mcp_tool_timeout_seconds),
            input_text=patch,
        )
        if checked["exit_code"] != 0 or checked["timed_out"]:
            return {
                "applied": False,
                "paths": paths,
                "check": checked,
            }
        apply_command = self._wrap(
            [git, "-C", str(self.repository.workspace), "apply", "--whitespace=nowarn", "-"]
        )
        applied = await self._run_process(
            apply_command,
            cwd=self.repository.workspace,
            timeout_seconds=min(120, self.config.runtime.mcp_tool_timeout_seconds),
            input_text=patch,
        )
        return {
            "applied": applied["exit_code"] == 0 and not applied["timed_out"],
            "paths": paths,
            "exit_code": applied["exit_code"],
            "stderr": applied["stderr"],
            "timed_out": applied["timed_out"],
        }

    async def _invoke_agent(
        self,
        arguments: dict[str, Any],
        *,
        started_callback: InvokeAgentStartedCallback | None,
    ) -> dict[str, Any]:
        """直接进入 Teamwork 执行器，不经过 MCP 或外部 API。"""

        if self.invoke_agent_callback is None:
            raise PermissionError("当前内嵌运行器没有启用 sub-agent 调度")
        agent_name = arguments.get("agent_name")
        task = arguments.get("task")
        extra_context = arguments.get("extra_context")
        if not isinstance(agent_name, str) or not agent_name.strip():
            raise ValueError("invoke_agent.agent_name 必须是非空字符串")
        if not isinstance(task, str) or not task.strip():
            raise ValueError("invoke_agent.task 必须是非空字符串")
        if extra_context is not None and not isinstance(extra_context, dict):
            raise ValueError("invoke_agent.extra_context 必须是对象")
        self.progress_callback()
        result = await self.invoke_agent_callback(
            self.context,
            agent_name.strip(),
            task.strip(),
            extra_context,
            started_callback,
        )
        self.progress_callback()
        return result

    def _wrap(self, inner_command: list[str]) -> ProcessLaunch:
        """受限 Agent 的每个本地进程都必须进入 Teamwork 托管沙盒。"""

        if not self.managed_sandbox:
            return ProcessLaunch(inner_command, dict(self.environment))
        return wrap_managed_sandbox_command(
            codex_binary=resolve_codex_executable(
                self.config.runtime.codex_binary,
                sandbox_executable_environment(self.environment),
            ),
            workspace=self.repository.workspace,
            agent=self.agent,
            inner_command=inner_command,
            environment=self.environment,
            codex_runtime_directory=self.codex_runtime_directory,
            codex_home=self.config.runtime.codex_home,
        )

    async def _run_process(
        self,
        launch: ProcessLaunch,
        *,
        cwd: Path,
        timeout_seconds: int,
        input_text: str | None = None,
    ) -> dict[str, Any]:
        """流式排空输出，取消时终止完整进程树。"""

        if self.cancel_check is not None and await self.cancel_check():
            raise asyncio.CancelledError
        command = launch.command
        resolved_command = [
            resolve_executable(command[0], launch.environment),
            *command[1:],
        ]
        process = await asyncio.create_subprocess_exec(
            *resolved_command,
            stdin=asyncio.subprocess.PIPE if input_text is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            env=launch.environment,
            **process_group_options(),
        )
        assert process.stdout is not None
        assert process.stderr is not None
        stdout = bytearray()
        stderr = bytearray()
        truncated = False

        async def drain(stream: asyncio.StreamReader, target: bytearray) -> None:
            """持续读取管道，保留总计不超过上限的前部内容。"""

            nonlocal truncated
            while chunk := await stream.read(64 * 1024):
                self.progress_callback()
                remaining = _TOOL_OUTPUT_LIMIT_BYTES - len(stdout) - len(stderr)
                if remaining > 0:
                    target.extend(chunk[:remaining])
                if len(chunk) > max(0, remaining):
                    truncated = True

        stdout_task = asyncio.create_task(drain(process.stdout, stdout))
        stderr_task = asyncio.create_task(drain(process.stderr, stderr))
        timed_out = False
        try:
            if input_text is not None:
                assert process.stdin is not None
                try:
                    process.stdin.write(input_text.encode("utf-8"))
                    await process.stdin.drain()
                except (BrokenPipeError, ConnectionResetError):
                    pass
                finally:
                    process.stdin.close()
                    with suppress(BrokenPipeError, ConnectionResetError):
                        await process.stdin.wait_closed()
            await asyncio.wait_for(process.wait(), timeout=timeout_seconds)
        except TimeoutError:
            timed_out = True
            await _terminate_process(process)
        except asyncio.CancelledError:
            await _terminate_process(process)
            raise
        finally:
            await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
        return {
            "exit_code": process.returncode,
            "stdout": stdout.decode("utf-8", errors="replace"),
            "stderr": stderr.decode("utf-8", errors="replace"),
            "timed_out": timed_out,
            "truncated": truncated,
        }


async def _terminate_process(process: asyncio.subprocess.Process) -> None:
    """先温和终止、再强制结束工具完整进程树。"""

    if process.returncode is not None:
        return
    with suppress(ProcessLookupError, PermissionError):
        terminate_process(process.pid, force=False, tree=True)
    try:
        await asyncio.wait_for(process.wait(), timeout=_PROCESS_TERMINATE_GRACE_SECONDS)
        return
    except TimeoutError:
        pass
    with suppress(ProcessLookupError, PermissionError):
        terminate_process(process.pid, force=True, tree=True)
    with suppress(TimeoutError):
        await asyncio.wait_for(process.wait(), timeout=_PROCESS_KILL_GRACE_SECONDS)


def shell_command(
    command: str,
    *,
    workdir: Path | None = None,
    environment: Mapping[str, str] | None = None,
    platform_name: str | None = None,
    os_name: str | None = None,
) -> list[str]:
    """按宿主平台构造 shell 参数，外层沙盒时显式恢复子目录 cwd。"""

    active_platform = platform_name or sys.platform
    active_os = os_name or os.name
    if active_os == "nt" or active_platform == "win32":
        shell = _first_resolved_executable(
            ("pwsh", "powershell", "powershell.exe"),
            environment,
        )
        script = command
        if workdir is not None:
            escaped = str(workdir).replace("'", "''")
            script = f"Set-Location -LiteralPath '{escaped}'; {command}"
        return [
            shell,
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            script,
        ]
    shell = _first_resolved_executable(("bash", "sh", "/bin/sh"), environment)
    script = command
    if workdir is not None:
        script = f"cd -- {shlex.quote(str(workdir))} && {command}"
    return [shell, "-lc", script]


def _first_resolved_executable(
    candidates: tuple[str, ...],
    environment: Mapping[str, str] | None,
) -> str:
    """从最终子进程 PATH 选择第一个真实存在的 shell。"""

    for candidate in candidates:
        resolved = resolve_executable(candidate, environment)
        if resolved != candidate or Path(resolved).is_file():
            return resolved
    return candidates[-1]


def _resolve_workdir(workspace: Path, value: str) -> Path:
    """把用户相对目录限制在真实工作区内。"""

    relative = Path(value)
    if relative.is_absolute():
        raise ValueError("workdir 必须是当前工作区内的相对路径")
    root = workspace.resolve()
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError("workdir 不能越过当前工作区") from exc
    if not candidate.is_dir():
        raise ValueError(f"workdir 不存在或不是目录：{value}")
    return candidate


def validate_unified_diff(patch: str) -> list[str]:
    """提取补丁目标并拒绝绝对路径、目录穿越和 Git 元数据。"""

    paths: set[str] = set()
    for line in patch.splitlines():
        candidates: list[str] = []
        if line.startswith("diff --git "):
            try:
                parts = shlex.split(line)
            except ValueError as exc:
                raise ValueError("补丁 diff --git 头格式无效") from exc
            if len(parts) != 4:
                raise ValueError("补丁 diff --git 头格式无效")
            candidates.extend(parts[2:4])
        elif line.startswith(("--- ", "+++ ")):
            candidates.append(line[4:].split("\t", 1)[0].strip())
        elif line.startswith(("rename from ", "rename to ", "copy from ", "copy to ")):
            candidates.append(line.split(" ", 2)[2].strip())
        for raw_path in candidates:
            if raw_path == "/dev/null":
                continue
            normalized = raw_path.replace("\\", "/")
            if raw_path.startswith('"') or re_windows_drive_path(normalized):
                raise ValueError(f"补丁包含不安全路径：{raw_path}")
            if normalized.startswith(("a/", "b/")):
                normalized = normalized[2:]
            path = PurePosixPath(normalized)
            if (
                not normalized
                or path.is_absolute()
                or any(part in {"", ".", ".."} for part in path.parts)
                or ".git" in path.parts
            ):
                raise ValueError(f"补丁包含不安全路径：{raw_path}")
            paths.add(path.as_posix())
    if not paths or "@@" not in patch:
        raise ValueError("补丁必须包含文件头和至少一个 hunk")
    return sorted(paths)


def re_windows_drive_path(path: str) -> bool:
    """识别在原生 Windows 上会成为绝对路径的盘符写法。"""

    return len(path) >= 3 and path[0].isalpha() and path[1:3] == ":/"
