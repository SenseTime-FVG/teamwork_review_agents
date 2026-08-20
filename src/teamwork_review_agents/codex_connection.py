"""Codex 运行时真实连接测试。"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time
from contextlib import suppress
from pathlib import Path
from typing import Any

from .codex_model_client import CodexOAuthStore, CodexResponsesClient
from .codex_settings import (
    codex_home,
    read_user_inherited_settings,
    read_user_mcp_servers,
    read_user_model,
    runtime_overrides,
)
from .config import AppConfig
from .codex_runner import CodexRunner
from .environment import SecretRedactor
from .process_control import process_group_options, terminate_process
from .subprocess_utils import resolve_executable


CONNECTION_TEST_TIMEOUT_SECONDS = 30.0
_MAX_REPLY_CHARACTERS = 200
_MAX_ERROR_CHARACTERS = 500
_PROCESS_TERMINATE_GRACE_SECONDS = 2.0
_PROCESS_KILL_GRACE_SECONDS = 1.0
_TOOL_ITEM_TYPES = {
    "command_execution",
    "file_change",
    "mcp_tool_call",
    "web_search",
    "dynamic_tool_call",
    "collaboration_tool_call",
}


class CodexConnectionTestError(RuntimeError):
    """表示当前 Codex 运行链路没有完成最小模型回合。"""


class CodexConnectionTestTimeout(CodexConnectionTestError):
    """表示连接测试超过固定总时限。"""


async def test_codex_connection(config: AppConfig) -> dict[str, Any]:
    """按当前已保存执行模式完成一次不使用工具的最小模型回合。"""

    started_at = time.monotonic()
    mode = config.runtime.codex.execution_mode
    try:
        if mode == "model":
            model, reply = await _test_model_connection(config)
        else:
            model, reply = await _test_cli_connection(config)
    except CodexConnectionTestTimeout:
        raise
    except asyncio.TimeoutError as exc:
        raise CodexConnectionTestTimeout("Codex 连接测试超过 30 秒") from exc
    except CodexConnectionTestError:
        raise
    except Exception as exc:
        detail = _safe_error(config, str(exc) or type(exc).__name__)
        raise CodexConnectionTestError(f"Codex 连接测试失败：{detail}") from exc

    elapsed = round(time.monotonic() - started_at, 3)
    return {
        "success": True,
        "mode": mode,
        "model": model,
        "reply": _safe_reply(config, reply),
        "elapsed_seconds": elapsed,
    }


async def _test_model_connection(config: AppConfig) -> tuple[str, str]:
    """通过内嵌 OAuth Responses 客户端验证模型基座链路。"""

    model, reasoning, fast_mode, verbosity = _model_settings(config)
    client = CodexResponsesClient(
        oauth=CodexOAuthStore(codex_home(config.runtime.codex_home)),
        codex_binary=config.runtime.codex_binary,
        timeout_seconds=CONNECTION_TEST_TIMEOUT_SECONDS,
        idle_timeout_seconds=CONNECTION_TEST_TIMEOUT_SECONDS,
    )
    payload: dict[str, Any] = {
        "model": model,
        "instructions": "这是连接测试。不要调用任何工具或访问外部资源，只执行用户的简短回复要求。",
        "input": [
            {
                "role": "user",
                "content": [{"type": "input_text", "text": "只回复 hi"}],
            }
        ],
        "tools": [],
        "tool_choice": "none",
        "parallel_tool_calls": False,
        "stream": True,
        "store": False,
        "reasoning": {"effort": reasoning},
    }
    if fast_mode:
        payload["service_tier"] = "priority"
    if verbosity:
        payload["text"] = {"verbosity": verbosity}

    try:
        async with asyncio.timeout(CONNECTION_TEST_TIMEOUT_SECONDS):
            response = await client.create_response(payload)
    except asyncio.TimeoutError as exc:
        raise CodexConnectionTestTimeout("Codex 模型连接测试超过 30 秒") from exc
    reply = _response_text(response).strip()
    if not reply:
        raise CodexConnectionTestError("Codex 模型连接测试没有返回文本")
    return model, reply


async def _test_cli_connection(config: AppConfig) -> tuple[str | None, str]:
    """在空临时目录中通过当前 Codex CLI 验证真实命令链路。"""

    environment = CodexRunner(config).child_environment()
    binary = resolve_executable(config.runtime.codex_binary, environment)
    with tempfile.TemporaryDirectory(prefix="teamwork-codex-connection-") as directory:
        command = _cli_connection_command(config, binary, Path(directory))
        process = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=directory,
            env=environment,
            **process_group_options(),
        )
        communication = asyncio.create_task(
            process.communicate(
                "这是连接测试。不要读取文件、运行命令或调用任何工具。只回复 hi。".encode(
                    "utf-8"
                )
            )
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                asyncio.shield(communication),
                timeout=CONNECTION_TEST_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError as exc:
            await _terminate_process(process)
            communication.cancel()
            await asyncio.gather(communication, return_exceptions=True)
            raise CodexConnectionTestTimeout("Codex CLI 连接测试超过 30 秒") from exc
        except asyncio.CancelledError:
            await _terminate_process(process)
            communication.cancel()
            await asyncio.gather(communication, return_exceptions=True)
            raise

    stdout_text = stdout.decode("utf-8", errors="replace")
    stderr_text = stderr.decode("utf-8", errors="replace")
    reply, tool_types, stream_error = _parse_cli_output(stdout_text)
    if process.returncode != 0:
        detail = stderr_text.strip() or stream_error or f"退出码 {process.returncode}"
        raise CodexConnectionTestError(
            f"Codex CLI 连接测试失败：{_safe_error(config, detail)}"
        )
    if tool_types:
        kinds = "、".join(sorted(tool_types))
        raise CodexConnectionTestError(f"Codex CLI 连接测试意外调用了工具：{kinds}")
    if not reply.strip():
        detail = stream_error or stderr_text.strip()
        suffix = f"：{_safe_error(config, detail)}" if detail else ""
        raise CodexConnectionTestError(f"Codex CLI 连接测试没有返回文本{suffix}")
    model, _, _ = read_user_model(codex_home(config.runtime.codex_home))
    return config.runtime.codex.model or model, reply.strip()


def _cli_connection_command(
    config: AppConfig,
    binary: str,
    workspace: Path,
) -> list[str]:
    """构造不读取仓库内容、用户 MCP 或联网搜索的临时 CLI 命令。"""

    command = [
        binary,
        "exec",
        "--json",
        "--ephemeral",
        "--sandbox",
        "read-only",
        "--cd",
        str(workspace),
        "--skip-git-repo-check",
    ]
    overrides = list(runtime_overrides(config.runtime.codex))
    user_servers, _ = read_user_mcp_servers(codex_home(config.runtime.codex_home))
    overrides.extend(
        f"mcp_servers.{_toml_key_segment(server)}.enabled=false"
        for server in user_servers
    )
    overrides.extend(
        [
            "skills.config=[]",
            "project_doc_max_bytes=0",
            'web_search="disabled"',
        ]
    )
    for override in overrides:
        command.extend(["--config", override])
    command.append("-")
    return command


def _model_settings(config: AppConfig) -> tuple[str, str, bool, str | None]:
    """按模型基座 Agent 的既有顺序解析后台模型默认值。"""

    home = codex_home(config.runtime.codex_home)
    user_model, _, _ = read_user_model(home)
    model = config.runtime.codex.model or user_model
    if not model:
        raise CodexConnectionTestError(
            "模型基座模式需要在运行时配置或 Codex config.toml 中配置模型"
        )
    inherited, _ = read_user_inherited_settings(home)

    def inherited_value(name: str) -> str | None:
        item = inherited.get(name)
        value = item.get("value") if isinstance(item, dict) else None
        return value if isinstance(value, str) and value else None

    reasoning = (
        config.runtime.codex.model_reasoning_effort
        or inherited_value("model_reasoning_effort")
        or "medium"
    )
    fast_setting = config.runtime.codex.fast_mode
    if fast_setting == "inherit":
        fast_setting = inherited_value("fast_mode") or "standard"
    verbosity = config.runtime.codex.model_verbosity or inherited_value(
        "model_verbosity"
    )
    return model, reasoning, fast_setting == "fast", verbosity


def _parse_cli_output(stdout: str) -> tuple[str, set[str], str | None]:
    """从 Codex JSONL 中提取最终消息、工具类型和安全错误候选。"""

    reply = ""
    tool_types: set[str] = set()
    stream_error: str | None = None
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        event_type = str(event.get("type") or "")
        item = event.get("item")
        if isinstance(item, dict):
            item_type = str(item.get("type") or "")
            if item_type in _TOOL_ITEM_TYPES or "tool_call" in item_type:
                tool_types.add(item_type)
            if event_type == "item.completed" and item_type == "agent_message":
                reply = str(item.get("text") or "")
        if event_type in {"turn.failed", "error"}:
            stream_error = json.dumps(event, ensure_ascii=False)
    return reply, tool_types, stream_error


def _response_text(response: dict[str, Any]) -> str:
    """从 completed response 中提取最终文本。"""

    direct = response.get("output_text")
    if isinstance(direct, str) and direct:
        return direct
    parts: list[str] = []
    output = response.get("output")
    if not isinstance(output, list):
        return ""
    for item in output:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        content = item.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") in {"output_text", "text"} and isinstance(
                block.get("text"), str
            ):
                parts.append(block["text"])
    return "".join(parts)


async def _terminate_process(process: asyncio.subprocess.Process) -> None:
    """连接测试结束等待超时后终止 Codex 完整进程树。"""

    with suppress(ProcessLookupError, PermissionError):
        terminate_process(process.pid, force=False, tree=True)
    try:
        await asyncio.wait_for(
            process.wait(),
            timeout=_PROCESS_TERMINATE_GRACE_SECONDS,
        )
        return
    except asyncio.TimeoutError:
        pass
    with suppress(ProcessLookupError, PermissionError):
        terminate_process(process.pid, force=True, tree=True)
    with suppress(asyncio.TimeoutError):
        await asyncio.wait_for(
            process.wait(),
            timeout=_PROCESS_KILL_GRACE_SECONDS,
        )


def _toml_key_segment(value: str) -> str:
    """把 MCP Server 名称编码为安全的 TOML 点号键段。"""

    if value and all(
        character.isascii() and (character.isalnum() or character in "_-")
        for character in value
    ):
        return value
    return json.dumps(value, ensure_ascii=False)


def _secret_redactor(config: AppConfig) -> SecretRedactor:
    """收集连接测试错误中需要隐藏的宿主凭据。"""

    names = {
        "CODEX_API_KEY",
        "OPENAI_API_KEY",
        *(provider.token_env for provider in config.providers.values()),
    }
    return SecretRedactor(
        tuple(
            value
            for name in names
            if (value := os.environ.get(name))
        )
    )


def _safe_error(config: AppConfig, value: str) -> str:
    """生成单行、脱敏且有长度上限的诊断摘要。"""

    text = " ".join(_secret_redactor(config).text(value).split())
    if not text:
        text = "未知错误"
    return text[:_MAX_ERROR_CHARACTERS] + (
        "…" if len(text) > _MAX_ERROR_CHARACTERS else ""
    )


def _safe_reply(config: AppConfig, value: str) -> str:
    """限制连接测试回复长度并隐藏意外回显的凭据。"""

    text = _secret_redactor(config).text(value).strip()
    return text[:_MAX_REPLY_CHARACTERS] + (
        "…" if len(text) > _MAX_REPLY_CHARACTERS else ""
    )
