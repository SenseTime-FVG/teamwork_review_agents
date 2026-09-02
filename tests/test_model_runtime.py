"""Codex 模型基座与 Teamwork 工具运行时测试。"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

from teamwork_review_agents.codex_model_client import (
    CodexOAuthStore,
    CodexResponsesClient,
    CodexUpstreamError,
)
from teamwork_review_agents.codex_model_runner import (
    CodexModelRunner,
    _instructions,
    _validate_output_schema,
)
from teamwork_review_agents.config import CodexRuntimeConfig, SkillConfig
from teamwork_review_agents.environment import SecretRedactor
from teamwork_review_agents.executor import AgentExecutor
from teamwork_review_agents.model_tools import (
    ModelToolExecutor,
    shell_command,
    validate_unified_diff,
)
from teamwork_review_agents.models import AgentResult, ChangeEvent, InvocationContext
from teamwork_review_agents.state import RunReservation, StateStore


def _jwt(payload: dict[str, Any]) -> str:
    """生成只供本地解析测试使用的未签名 JWT 文本。"""

    def encode(value: dict[str, Any]) -> str:
        raw = json.dumps(value, separators=(",", ":")).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    return f"{encode({'alg': 'none'})}.{encode(payload)}.signature"


def _context(config, snapshot_factory) -> InvocationContext:
    """构造不包含真实凭据的最小运行上下文。"""

    snapshot = snapshot_factory(provider="github-main")
    event = ChangeEvent(
        id="event-model-runtime",
        type="change_request.updated",
        provider="github-main",
        repository_id="demo",
        number=snapshot.number,
        old=None,
        new=snapshot,
    )
    return InvocationContext(
        config_path=str(config.config_path),
        current_agent="code-reviewer",
        run_id="run-parent",
        root_run_id="run-root",
        active_workspace=str(config.repositories[0].workspace),
        event=event,
    )


def test_execution_mode_defaults_to_model_and_rejects_unknown_value() -> None:
    """Codex CLI 默认使用基座模式，枚举之外的值应被拒绝。"""

    assert CodexRuntimeConfig().execution_mode == "model"
    assert CodexRuntimeConfig(execution_mode="model").execution_mode == "model"
    with pytest.raises(ValueError):
        CodexRuntimeConfig(execution_mode="external-api")


def test_agent_executor_selects_embedded_runner(configured_app_factory) -> None:
    """开关只替换运行器，不改变统一执行入口。"""

    config = configured_app_factory()
    config.runtime.codex.execution_mode = "model"
    executor = AgentExecutor(config, StateStore(config.database.path))
    assert isinstance(executor.runner, CodexModelRunner)


@pytest.mark.asyncio
async def test_embedded_sub_agent_reuses_whitelist_and_execution_context(
    configured_app_factory,
    snapshot_factory,
) -> None:
    """invoke_agent 应直接复用统一执行器，而不是请求 MCP 或外部服务。"""

    config = configured_app_factory()
    config.runtime.codex.execution_mode = "model"
    executor = AgentExecutor(config, StateStore(config.database.path))
    context = _context(config, snapshot_factory).model_copy(
        update={"call_chain": ("code-reviewer",)}
    )
    captured: dict[str, Any] = {}
    linked_runs: list[dict[str, Any]] = []

    async def fake_execute(**arguments):
        captured.update(arguments)
        await arguments["run_started_callback"](
            RunReservation(
                run_id="run-child",
                root_run_id=context.root_run_id,
                parent_run_id=context.run_id,
                attempts=1,
            )
        )
        return AgentResult(
            run_id="run-child",
            root_run_id=context.root_run_id,
            parent_run_id=context.run_id,
            agent_name="security-reviewer",
            status="completed",
            final_message="安全审查完成",
        )

    setattr(executor, "execute", fake_execute)

    async def record_started(linked_run: dict[str, Any]) -> None:
        """记录父运行应收到的子运行精确关联。"""

        linked_runs.append(linked_run)

    result = await executor._invoke_embedded_agent(
        context,
        "security-reviewer",
        "检查依赖安全",
        {"scope": "dependencies"},
        record_started,
    )

    assert result["status"] == "completed"
    assert result["final_message"] == "安全审查完成"
    assert captured["root_run_id"] == context.root_run_id
    assert captured["parent_run_id"] == context.run_id
    assert captured["depth"] == 1
    assert captured["parent_workspace"] == Path(context.active_workspace)
    assert linked_runs == [
        {
            "run_id": "run-child",
            "root_run_id": context.root_run_id,
            "parent_run_id": context.run_id,
            "agent_name": "security-reviewer",
            "status": "queued",
        }
    ]

    with pytest.raises(PermissionError):
        await executor._invoke_embedded_agent(
            context,
            "unknown-agent",
            "不允许的委托",
            None,
        )


@pytest.mark.asyncio
async def test_model_runner_emits_live_sub_agent_link(
    configured_app_factory,
    snapshot_factory,
    monkeypatch,
) -> None:
    """子运行创建后应立即写入可点击关联，不必等待工具调用完成。"""

    config = configured_app_factory()
    config.runtime.codex.model = "gpt-test"
    config.agents["code-reviewer"] = config.agents["code-reviewer"].model_copy(
        update={"sandbox": "danger-full-access"}
    )
    repository = config.repositories[0]
    response_count = 0

    async def fake_create_response(self, payload, *, event_callback=None):
        nonlocal response_count
        response_count += 1
        if response_count == 1:
            return {
                "id": "resp-invoke-1",
                "output": [
                    {
                        "type": "function_call",
                        "call_id": "call-sub-agent-1",
                        "name": "invoke_agent",
                        "arguments": json.dumps(
                            {
                                "agent_name": "security-reviewer",
                                "task": "检查依赖安全",
                            }
                        ),
                    }
                ],
                "usage": {"input_tokens": 2, "output_tokens": 1},
            }
        return {
            "id": "resp-invoke-2",
            "output": [
                {
                    "id": "msg-invoke-2",
                    "type": "message",
                    "content": [{"type": "output_text", "text": "委托完成"}],
                }
            ],
            "usage": {"input_tokens": 2, "output_tokens": 1},
        }

    monkeypatch.setattr(CodexResponsesClient, "create_response", fake_create_response)
    logs: list[tuple[str, str, Any]] = []

    async def log_callback(stream: str, event_type: str, payload: Any) -> None:
        logs.append((stream, event_type, payload))

    async def invoke_agent_callback(
        context: InvocationContext,
        agent_name: str,
        task: str,
        extra_context: dict[str, Any] | None,
        started_callback,
    ) -> dict[str, Any]:
        assert started_callback is not None
        await started_callback(
            {
                "run_id": "run-child-live",
                "root_run_id": context.root_run_id,
                "parent_run_id": context.run_id,
                "agent_name": agent_name,
                "status": "queued",
            }
        )
        return {
            "status": "completed",
            "run_id": "run-child-live",
            "agent_name": agent_name,
            "final_message": "检查完成",
            "usage": {},
        }

    result = await CodexModelRunner(
        config,
        invoke_agent_callback=invoke_agent_callback,
    ).run(
        run_id="run-parent-live",
        root_run_id="run-parent-live",
        parent_run_id=None,
        agent_name="code-reviewer",
        agent=config.agents["code-reviewer"],
        repository=repository,
        context=_context(config, snapshot_factory).model_copy(
            update={
                "run_id": "run-parent-live",
                "root_run_id": "run-parent-live",
            }
        ),
        prompt="请委托安全检查",
        process_environment={},
        redactor=SecretRedactor(()),
        log_callback=log_callback,
        cancel_check=None,
    )

    assert result.status == "completed"
    tool_logs = [
        (event_type, payload["item"])
        for _, event_type, payload in logs
        if event_type in {"item.started", "item.updated", "item.completed"}
        and isinstance(payload, dict)
        and isinstance(payload.get("item"), dict)
        and payload["item"].get("tool") == "invoke_agent"
    ]
    assert [event_type for event_type, _ in tool_logs] == [
        "item.started",
        "item.updated",
        "item.completed",
    ]
    assert {item["call_id"] for _, item in tool_logs} == {"call-sub-agent-1"}
    assert tool_logs[1][1]["linked_run"]["run_id"] == "run-child-live"
    assert tool_logs[2][1]["result"]["run_id"] == "run-child-live"


@pytest.mark.asyncio
async def test_oauth_store_refreshes_existing_codex_login(tmp_path) -> None:
    """内嵌客户端只复用 Codex auth.json，并可原子刷新过期 token。"""

    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    expired = _jwt({"exp": int(time.time()) - 60})
    (codex_home / "auth.json").write_text(
        json.dumps(
            {
                "auth_mode": "chatgpt",
                "tokens": {
                    "access_token": expired,
                    "refresh_token": "refresh-secret",
                },
            }
        ),
        encoding="utf-8",
    )
    refreshed = _jwt(
        {
            "exp": int(time.time()) + 3600,
            "https://api.openai.com/auth": {"chatgpt_account_id": "account-test"},
        }
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == httpx.URL("https://auth.openai.com/oauth/token")
        assert b"refresh-secret" in await request.aread()
        return httpx.Response(
            200,
            json={
                "access_token": refreshed,
                "refresh_token": "refresh-new",
                "expires_in": 3600,
            },
        )

    store = CodexOAuthStore(
        codex_home,
        transport=httpx.MockTransport(handler),
    )
    credentials = await store.credentials()

    assert credentials.access_token == refreshed
    assert credentials.refresh_token == "refresh-new"
    assert credentials.account_id == "account-test"
    saved = json.loads((codex_home / "auth.json").read_text(encoding="utf-8"))
    assert saved["tokens"]["access_token"] == refreshed


@pytest.mark.asyncio
async def test_responses_client_parses_sse_without_local_api(tmp_path) -> None:
    """请求应直达 Codex 上游，并聚合标准 SSE completed 事件。"""

    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    access = _jwt({"exp": int(time.time()) + 3600})
    (codex_home / "auth.json").write_text(
        json.dumps(
            {
                "auth_mode": "chatgpt",
                "tokens": {
                    "access_token": access,
                    "refresh_token": "refresh-secret",
                },
            }
        ),
        encoding="utf-8",
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == httpx.URL(
            "https://chatgpt.com/backend-api/codex/responses"
        )
        assert request.headers["authorization"] == f"Bearer {access}"
        assert request.url.port is None
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=(
                'data: {"type":"response.created","response":{"id":"resp_1"}}\n\n'
                'data: {"type":"response.output_text.delta","delta":"完成"}\n\n'
                'data: {"type":"response.output_item.done","item":{"id":"msg_1",'
                '"type":"message","content":[{"type":"output_text","text":"完成"}]}}\n\n'
                'data: {"type":"response.completed","response":{"id":"resp_1",'
                '"output":[],"usage":{"input_tokens":3}}}\n\n'
                "data: [DONE]\n\n"
            ),
        )

    transport = httpx.MockTransport(handler)
    client = CodexResponsesClient(
        oauth=CodexOAuthStore(codex_home, transport=transport),
        codex_binary="missing-codex-for-test",
        transport=transport,
    )
    events: list[str] = []
    response = await client.create_response(
        {"model": "gpt-test", "input": []},
        event_callback=lambda event: _append_event(events, event),
    )

    assert response["id"] == "resp_1"
    assert response["output_text"] == "完成"
    assert response["output"][0]["id"] == "msg_1"
    assert events == [
        "response.created",
        "response.output_text.delta",
        "response.output_item.done",
        "response.completed",
    ]


@pytest.mark.asyncio
async def test_responses_client_exposes_nested_sse_failure_details(tmp_path) -> None:
    """response.failed 应保留上游错误字段，而不是只返回通用失败文案。"""

    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    access = _jwt({"exp": int(time.time()) + 3600})
    (codex_home / "auth.json").write_text(
        json.dumps(
            {
                "auth_mode": "chatgpt",
                "tokens": {"access_token": access, "refresh_token": "refresh-secret"},
            }
        ),
        encoding="utf-8",
    )
    requests = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=(
                'data: {"type":"response.failed","response":{"status":"failed",'
                '"error":{"message":"模型暂时不可用","type":"server_error",'
                '"code":"model_unavailable","param":"model",'
                '"request_id":"req-sse-1"}}}\n\n'
                "data: [DONE]\n\n"
            ),
        )

    transport = httpx.MockTransport(handler)
    client = CodexResponsesClient(
        oauth=CodexOAuthStore(codex_home, transport=transport),
        codex_binary="missing-codex-for-test",
        transport=transport,
    )

    with pytest.raises(CodexUpstreamError) as raised:
        await client.create_response({"model": "gpt-test", "input": []})

    message = str(raised.value)
    assert "模型暂时不可用" in message
    assert "类型=server_error" in message
    assert "代码=model_unavailable" in message
    assert "参数=model" in message
    assert "请求 ID=req-sse-1" in message
    assert raised.value.fallbackable is True
    assert requests == 1


@pytest.mark.asyncio
async def test_responses_client_exposes_top_level_sse_error_details(tmp_path) -> None:
    """顶层 error 事件没有嵌套 response 时也应保留消息和代码。"""

    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    access = _jwt({"exp": int(time.time()) + 3600})
    (codex_home / "auth.json").write_text(
        json.dumps(
            {
                "auth_mode": "chatgpt",
                "tokens": {"access_token": access, "refresh_token": "refresh-secret"},
            }
        ),
        encoding="utf-8",
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=(
                'data: {"type":"error","message":"请求参数无效",'
                '"code":"invalid_request","requestId":"req-sse-2"}\n\n'
            ),
        )

    transport = httpx.MockTransport(handler)
    client = CodexResponsesClient(
        oauth=CodexOAuthStore(codex_home, transport=transport),
        codex_binary="missing-codex-for-test",
        transport=transport,
    )

    with pytest.raises(CodexUpstreamError) as raised:
        await client.create_response({"model": "gpt-test", "input": []})

    message = str(raised.value)
    assert "请求参数无效" in message
    assert "代码=invalid_request" in message
    assert "请求 ID=req-sse-2" in message
    assert raised.value.fallbackable is False


@pytest.mark.asyncio
async def test_responses_client_exposes_http_error_and_redacts_sensitive_text(
    tmp_path,
) -> None:
    """非 2xx JSON 错误应展示原因，同时限制长度并脱敏凭据。"""

    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    access = _jwt({"exp": int(time.time()) + 3600})
    (codex_home / "auth.json").write_text(
        json.dumps(
            {
                "auth_mode": "chatgpt",
                "tokens": {"access_token": access, "refresh_token": "refresh-secret"},
            }
        ),
        encoding="utf-8",
    )
    secret = "sk-test-secret-value-123456"

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={
                "error": {
                    "message": f"请求被拒绝 Bearer {secret}",
                    "type": "invalid_request_error",
                    "code": "unsupported_model",
                    "param": "model",
                    "request_id": "req-http-1",
                }
            },
        )

    transport = httpx.MockTransport(handler)
    client = CodexResponsesClient(
        oauth=CodexOAuthStore(codex_home, transport=transport),
        codex_binary="missing-codex-for-test",
        transport=transport,
    )

    with pytest.raises(CodexUpstreamError) as raised:
        await client.create_response({"model": "gpt-test", "input": []})

    message = str(raised.value)
    assert "HTTP 400" in message
    assert "请求被拒绝" in message
    assert "类型=invalid_request_error" in message
    assert "代码=unsupported_model" in message
    assert "请求 ID=req-http-1" in message
    assert secret not in message
    assert "Bearer [已脱敏]" in message
    assert len(message) <= 2000
    assert raised.value.fallbackable is False


async def _append_event(events: list[str], event: dict[str, Any]) -> None:
    """供 SSE 测试使用的异步事件收集器。"""

    events.append(str(event.get("type")))


def test_shell_command_covers_posix_and_native_windows(tmp_path) -> None:
    """三平台共用路径约束，shell 参数按 POSIX 与原生 Windows 分流。"""

    posix = shell_command(
        "pwd",
        workdir=tmp_path,
        platform_name="linux",
        os_name="posix",
    )
    macos = shell_command(
        "pwd",
        workdir=tmp_path,
        platform_name="darwin",
        os_name="posix",
    )
    windows = shell_command(
        "Get-Location",
        workdir=Path("C:/workspace"),
        platform_name="win32",
        os_name="nt",
    )

    assert posix[-2] == "-lc"
    assert "cd --" in posix[-1]
    assert macos[-2:] == posix[-2:]
    assert windows[-2] == "-Command"
    assert "Set-Location -LiteralPath" in windows[-1]


def test_shell_command_uses_final_child_path(tmp_path) -> None:
    """内嵌工具选择 PowerShell 时必须服从最终子进程 PATH。"""

    shell = tmp_path / ("pwsh.cmd" if os.name == "nt" else "pwsh")
    shell.write_text("@echo off\r\n" if os.name == "nt" else "#!/bin/sh\n", encoding="utf-8")
    if os.name != "nt":
        shell.chmod(0o755)

    command = shell_command(
        "Get-Location",
        environment={"PATH": str(tmp_path), "PATHEXT": ".COM;.EXE;.BAT;.CMD"},
        platform_name="win32",
        os_name="nt",
    )

    assert Path(command[0]).samefile(shell)


def test_unified_diff_rejects_escape_and_git_metadata() -> None:
    """apply_patch 不能用路径头越过工作区或修改 .git。"""

    safe = """diff --git a/src/a.py b/src/a.py
--- a/src/a.py
+++ b/src/a.py
@@ -1 +1 @@
-old
+new
"""
    assert validate_unified_diff(safe) == ["src/a.py"]

    for path in ("../outside", ".git/config", "/tmp/outside"):
        unsafe = f"""diff --git a/file b/{path}
--- a/file
+++ b/{path}
@@ -1 +1 @@
-old
+new
"""
        with pytest.raises(ValueError):
            validate_unified_diff(unsafe)


@pytest.mark.asyncio
async def test_apply_patch_tool_updates_only_workspace(
    configured_app_factory,
    snapshot_factory,
) -> None:
    """Teamwork 补丁工具先 check，再在当前工作区应用 unified diff。"""

    config = configured_app_factory()
    repository = config.repositories[0]
    subprocess.run(["git", "init", "-q", str(repository.workspace)], check=True)
    target = repository.workspace / "example.txt"
    target.write_text("old\n", encoding="utf-8")
    agent = config.agents["code-reviewer"].model_copy(
        update={"sandbox": "danger-full-access"}
    )
    executor = ModelToolExecutor(
        config=config,
        agent=agent,
        repository=repository,
        context=_context(config, snapshot_factory),
        environment={"PATH": str(Path("/usr/bin")) + ":/bin"},
        managed_sandbox=False,
        cancel_check=None,
        progress_callback=lambda: None,
        invoke_agent_callback=None,
    )
    result = await executor.execute(
        "apply_patch",
        {
            "patch": """diff --git a/example.txt b/example.txt
--- a/example.txt
+++ b/example.txt
@@ -1 +1 @@
-old
+new
"""
        },
    )

    assert result["applied"] is True
    assert target.read_text(encoding="utf-8") == "new\n"


def test_model_tool_resolves_managed_sandbox_binary_from_child_path(
    configured_app_factory,
    snapshot_factory,
    tmp_path,
) -> None:
    """托管沙盒外层 Codex 必须按最终子进程 PATH 解析。"""

    config = configured_app_factory()
    config.runtime.codex_binary = "codex"
    repository = config.repositories[0]
    executable = tmp_path / ("codex.cmd" if os.name == "nt" else "codex")
    executable.write_text("@echo off\r\n" if os.name == "nt" else "#!/bin/sh\n", encoding="utf-8")
    if os.name != "nt":
        executable.chmod(0o755)
    executor = ModelToolExecutor(
        config=config,
        agent=config.agents["code-reviewer"],
        repository=repository,
        context=_context(config, snapshot_factory),
        environment={"PATH": str(tmp_path)},
        managed_sandbox=True,
        cancel_check=None,
        progress_callback=lambda: None,
        invoke_agent_callback=None,
    )

    command = executor._wrap([sys.executable, "-c", "pass"])

    assert command[0].lower() == str(executable.resolve()).lower()


@pytest.mark.asyncio
async def test_model_runner_executes_tool_loop_and_merges_usage(
    configured_app_factory,
    snapshot_factory,
    monkeypatch,
) -> None:
    """模型函数调用应由 Teamwork 执行并作为 output 回传下一回合。"""

    config = configured_app_factory()
    config.runtime.codex.model = "gpt-test"
    config.agents["code-reviewer"] = config.agents["code-reviewer"].model_copy(
        update={"sandbox": "danger-full-access"}
    )
    repository = config.repositories[0]
    payloads: list[dict[str, Any]] = []

    async def fake_create_response(self, payload, *, event_callback=None):
        payloads.append(payload)
        response_number = len(payloads)
        if event_callback is not None:
            await event_callback(
                {"type": "response.created", "response": {"id": f"resp_{response_number}"}}
            )
        if response_number == 1:
            response = {
                "id": "resp_1",
                "output": [
                    {
                        "id": "msg_1",
                        "type": "message",
                        "content": [
                            {"type": "output_text", "text": "先检查命令"}
                        ],
                    },
                    {
                        "type": "function_call",
                        "call_id": "call_1",
                        "name": "execute_command",
                        "arguments": json.dumps({"command": "printf model-tool"}),
                    }
                ],
                "usage": {"input_tokens": 5, "output_tokens": 2},
            }
        else:
            response = {
                "id": "resp_2",
                "output": [
                    {
                        "id": "msg_2",
                        "type": "message",
                        "content": [{"type": "output_text", "text": "工具验证完成"}],
                    }
                ],
                "usage": {"input_tokens": 3, "output_tokens": 4},
            }
        if event_callback is not None:
            for item in response["output"]:
                await event_callback(
                    {"type": "response.output_item.done", "item": item}
                )
            await event_callback(
                {
                    "type": "response.completed",
                    "response": {
                        "id": response["id"],
                        "status": "completed",
                        "usage": response["usage"],
                    },
                }
            )
        return response

    monkeypatch.setattr(CodexResponsesClient, "create_response", fake_create_response)
    logs: list[tuple[str, str, Any]] = []

    async def log_callback(stream: str, event_type: str, payload: Any) -> None:
        logs.append((stream, event_type, payload))

    result = await CodexModelRunner(config).run(
        run_id="run-model",
        root_run_id="run-model",
        parent_run_id=None,
        agent_name="code-reviewer",
        agent=config.agents["code-reviewer"],
        repository=repository,
        context=_context(config, snapshot_factory),
        prompt="请执行工具测试",
        process_environment={},
        redactor=SecretRedactor(()),
        log_callback=log_callback,
        cancel_check=None,
    )

    assert result.status == "completed"
    assert result.final_message == "工具验证完成"
    assert result.thread_id == "resp_2"
    assert result.usage == {"input_tokens": 8, "output_tokens": 6}
    outputs = [
        item
        for item in payloads[1]["input"]
        if item.get("type") == "function_call_output"
    ]
    assert len(outputs) == 1
    assert "model-tool" in outputs[0]["output"]
    agent_messages = [
        payload["item"]["text"]
        for _, event_type, payload in logs
        if event_type == "item.completed"
        and isinstance(payload, dict)
        and isinstance(payload.get("item"), dict)
        and payload["item"].get("type") == "agent_message"
    ]
    assert agent_messages == ["先检查命令", "工具验证完成"]
    assert sum(event_type == "thread.started" for _, event_type, _ in logs) == 1
    assert all(not event_type.startswith("response.") for _, event_type, _ in logs)
    assert [
        event["type"]
        for event in result.events
        if str(event.get("type", "")).startswith("response.")
    ] == [
        "response.created",
        "response.output_item.done",
        "response.output_item.done",
        "response.completed",
        "response.created",
        "response.output_item.done",
        "response.completed",
    ]


def test_model_tool_environment_uses_empty_run_codex_home(
    configured_app_factory,
    tmp_path,
    monkeypatch,
) -> None:
    """真实 OAuth 目录不能通过 CODEX_HOME 暴露给工具子进程。"""

    config = configured_app_factory()
    monkeypatch.setenv("SystemRoot", "C:/Windows")
    monkeypatch.setenv("ComSpec", "C:/Windows/System32/cmd.exe")
    tool_codex_home = tmp_path / "tool-codex-home"
    tool_codex_home.mkdir()
    environment = CodexModelRunner(config).child_environment(
        {
            "OPENAI_API_KEY": "do-not-forward",
            "CODEX_API_KEY": "do-not-forward",
            "Github_Token": "explicit-provider-token",
        },
        temporary_home=None,
        tool_codex_home=tool_codex_home,
    )

    assert environment["CODEX_HOME"] == str(tool_codex_home)
    assert environment["CODEX_HOME"] != str(config.runtime.codex_home)
    assert "OPENAI_API_KEY" not in environment
    assert "CODEX_API_KEY" not in environment
    assert "GITHUB_TOKEN" not in environment
    assert environment["Github_Token"] == "explicit-provider-token"
    assert environment["SYSTEMROOT"] == "C:/Windows"
    assert environment["COMSPEC"] == "C:/Windows/System32/cmd.exe"


def test_selected_skill_is_injected_into_model_instructions(
    configured_app_factory,
    tmp_path,
) -> None:
    """模型基座模式应完整注入选中 Skill，而不是依赖 Codex CLI 发现。"""

    config = configured_app_factory()
    skill_file = tmp_path / "skills" / "docs" / "SKILL.md"
    skill_file.parent.mkdir(parents=True)
    skill_file.write_text(
        "---\nname: docs\ndescription: 文档规则\n---\n\n必须先检查文档索引。\n",
        encoding="utf-8",
    )
    config.skills["docs"] = SkillConfig(path=skill_file.parent)
    agent = config.agents["code-reviewer"].model_copy(update={"skills": ["docs"]})
    instructions = _instructions(
        repository=config.repositories[0],
        agent=agent,
        personality=None,
        skill_files={"docs": skill_file},
    )

    assert "已启用 Skill：docs" in instructions
    assert "必须先检查文档索引" in instructions


@pytest.mark.asyncio
async def test_model_runner_watchdog_cancels_pending_model_request(
    configured_app_factory,
    snapshot_factory,
    monkeypatch,
) -> None:
    """持久化取消应中断正在等待的模型 HTTP 回合。"""

    config = configured_app_factory()
    config.runtime.codex.model = "gpt-test"
    config.agents["code-reviewer"] = config.agents["code-reviewer"].model_copy(
        update={"sandbox": "danger-full-access"}
    )
    entered = False

    async def pending_response(self, payload, *, event_callback=None):
        nonlocal entered
        entered = True
        await asyncio.Event().wait()

    monkeypatch.setattr(CodexResponsesClient, "create_response", pending_response)

    async def cancel_check() -> bool:
        return entered

    result = await CodexModelRunner(config).run(
        run_id="run-cancel",
        root_run_id="run-cancel",
        parent_run_id=None,
        agent_name="code-reviewer",
        agent=config.agents["code-reviewer"],
        repository=config.repositories[0],
        context=_context(config, snapshot_factory),
        prompt="等待取消",
        process_environment={},
        redactor=SecretRedactor(()),
        cancel_check=cancel_check,
    )

    assert result.status == "cancelled"


@pytest.mark.asyncio
async def test_restricted_model_runner_fails_when_managed_sandbox_is_disabled(
    configured_app_factory,
    snapshot_factory,
) -> None:
    """模型基座模式没有 Codex 内层沙盒，受限工具不能无沙盒回退。"""

    config = configured_app_factory()
    config.runtime.codex.model = "gpt-test"
    config.runtime.managed_sandbox.enabled = False
    repository = config.repositories[0]
    result = await CodexModelRunner(config).run(
        run_id="run-restricted",
        root_run_id="run-restricted",
        parent_run_id=None,
        agent_name="code-reviewer",
        agent=config.agents["code-reviewer"],
        repository=repository,
        context=_context(config, snapshot_factory),
        prompt="不应调用模型",
        process_environment={},
        redactor=SecretRedactor(()),
    )

    assert result.status == "failed"
    assert "必须启用 Teamwork 外层沙盒" in (result.error or "")


def test_final_output_schema_is_validated_locally() -> None:
    """即使上游忽略格式约束，最终结果也不能绕过本地 Schema。"""

    schema = {
        "type": "object",
        "properties": {"ok": {"type": "boolean"}},
        "required": ["ok"],
        "additionalProperties": False,
    }
    _validate_output_schema('{"ok":true}', schema)
    with pytest.raises(RuntimeError):
        _validate_output_schema('{"ok":"yes"}', schema)
