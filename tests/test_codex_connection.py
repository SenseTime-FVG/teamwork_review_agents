"""Codex 运行时连接测试。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from teamwork_review_agents.codex_connection import (
    CodexConnectionTestError,
    CodexConnectionTestTimeout,
    _cli_connection_command,
    _parse_cli_output,
    test_codex_connection as run_connection_test,
)
from teamwork_review_agents.codex_model_client import CodexResponsesClient
from teamwork_review_agents.webapp import create_app


@pytest.mark.asyncio
async def test_model_connection_uses_saved_model_without_tools(
    configured_app_factory,
    monkeypatch,
) -> None:
    """模型基座连接测试应复用保存模型并显式禁止全部工具。"""

    config = configured_app_factory()
    config.runtime.codex.execution_mode = "model"
    config.runtime.codex.model = "gpt-connection-test"
    config.runtime.codex.model_reasoning_effort = "low"
    captured: dict[str, object] = {}

    async def create_response(self, payload, **_kwargs):
        captured.update(payload)
        return {"output_text": "hi"}

    monkeypatch.setattr(CodexResponsesClient, "create_response", create_response)

    result = await run_connection_test(config)

    assert result["success"] is True
    assert result["mode"] == "model"
    assert result["model"] == "gpt-connection-test"
    assert result["reply"] == "hi"
    assert captured["tools"] == []
    assert captured["tool_choice"] == "none"
    assert captured["reasoning"] == {"effort": "low"}
    assert captured["input"] == [
        {
            "role": "user",
            "content": [{"type": "input_text", "text": "只回复 hi"}],
        }
    ]


def test_cli_connection_command_isolates_repository_mcp_and_search(
    configured_app_factory,
) -> None:
    """CLI 连接测试必须使用空工作区并覆盖用户 MCP、Skill 和搜索配置。"""

    config = configured_app_factory()
    config.runtime.codex.model = "gpt-cli-test"
    home = Path(config.runtime.codex_home)
    (home / "config.toml").write_text(
        '[mcp_servers."browser.tools"]\ncommand = "browser"\n',
        encoding="utf-8",
    )
    workspace = home / "empty-workspace"
    command = _cli_connection_command(config, "codex", workspace)

    assert command[:5] == ["codex", "exec", "--json", "--ephemeral", "--sandbox"]
    assert command[command.index("--cd") + 1] == str(workspace)
    assert "--skip-git-repo-check" in command
    assert 'model="gpt-cli-test"' in command
    assert 'mcp_servers."browser.tools".enabled=false' in command
    assert "skills.config=[]" in command
    assert "project_doc_max_bytes=0" in command
    assert 'web_search="disabled"' in command
    assert command[-1] == "-"


def test_cli_output_extracts_reply_and_detects_tool_use() -> None:
    """CLI JSONL 解析应保留最终回复并识别任何工具执行。"""

    stdout = "\n".join(
        [
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {"type": "command_execution", "command": "pwd"},
                }
            ),
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": "hi"},
                }
            ),
        ]
    )

    reply, tool_types, stream_error = _parse_cli_output(stdout)

    assert reply == "hi"
    assert tool_types == {"command_execution"}
    assert stream_error is None


@pytest.mark.asyncio
async def test_connection_timeout_is_normalized(
    configured_app_factory,
    monkeypatch,
) -> None:
    """底层超时应转换为稳定的连接测试超时错误。"""

    config = configured_app_factory()
    config.runtime.codex.execution_mode = "model"

    async def fail(_config):
        raise asyncio.TimeoutError

    monkeypatch.setattr(
        "teamwork_review_agents.codex_connection._test_model_connection",
        fail,
    )

    with pytest.raises(CodexConnectionTestTimeout, match="超过 30 秒"):
        await run_connection_test(config)


def test_connection_test_api_uses_current_saved_config(
    configured_app_factory,
    monkeypatch,
) -> None:
    """管理 API 应把当前已加载配置交给连接测试服务并返回结果。"""

    config = configured_app_factory()
    captured = {}

    async def run(saved_config):
        captured["config_path"] = saved_config.config_path
        return {
            "success": True,
            "mode": "cli",
            "model": "gpt-api-test",
            "reply": "hi",
            "elapsed_seconds": 0.12,
        }

    monkeypatch.setattr("teamwork_review_agents.webapp.test_codex_connection", run)
    app = create_app(config.config_path, start_scheduler=False)

    with TestClient(app) as client:
        response = client.post("/api/codex/connection-test")

    assert response.status_code == 200
    assert response.json()["reply"] == "hi"
    assert captured["config_path"] == config.config_path


@pytest.mark.parametrize(
    ("error", "status_code"),
    [
        (CodexConnectionTestError("认证失败"), 502),
        (CodexConnectionTestTimeout("超过 30 秒"), 504),
    ],
)
def test_connection_test_api_maps_safe_failures(
    configured_app_factory,
    monkeypatch,
    error,
    status_code,
) -> None:
    """连接失败与超时应映射为不同的管理 API 状态。"""

    config = configured_app_factory()

    async def fail(_saved_config):
        raise error

    monkeypatch.setattr("teamwork_review_agents.webapp.test_codex_connection", fail)
    app = create_app(config.config_path, start_scheduler=False)

    with TestClient(app) as client:
        response = client.post("/api/codex/connection-test")

    assert response.status_code == status_code
    assert response.json()["detail"] == str(error)
