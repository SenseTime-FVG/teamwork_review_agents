"""模型 Provider、凭据、协议适配与回退语义测试。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient

from teamwork_review_agents.codex_model_runner import CodexModelRunner
from teamwork_review_agents.codex_runner import CodexRunner
from teamwork_review_agents.config import (
    ModelProviderConfig,
    parse_config_data,
)
from teamwork_review_agents.config_manager import ConfigManager
from teamwork_review_agents.executor import AgentExecutor
from teamwork_review_agents.model_provider_client import (
    ExternalModelClient,
    _chat_messages,
)
from teamwork_review_agents.model_provider_credentials import (
    ModelProviderCredentialStore,
)
from teamwork_review_agents.model_provider_runtime import (
    ModelProviderUnavailableError,
    resolve_model_selection,
    resolve_model_snapshot,
)
from teamwork_review_agents.state import StateStore
from teamwork_review_agents.webapp import create_app


def _write_provider_config(tmp_path: Path) -> Path:
    """写入同时包含内置与外部 Provider 的最小配置。"""

    document = {
        "database": {"path": str(tmp_path / "state.db")},
        "runtime": {
            "default_model": {
                "provider": "external-openai",
                "model": "global-model",
            }
        },
        "model_providers": {
            "codex-cli": {
                "display_name": "Codex CLI",
                "driver": "codex_cli",
                "enabled": True,
            },
            "external-openai": {
                "display_name": "External OpenAI",
                "driver": "openai_responses",
                "enabled": True,
                "base_url": "https://models.example.test",
                "default_model": "provider-model",
                "models": ["provider-model", "global-model"],
            },
        },
        "agents": {
            "explicit": {
                "prompt": "测试显式模型。",
                "model_provider": "external-openai",
                "model": "agent-model",
            },
            "inherited": {"prompt": "测试继承模型。"},
        },
    }
    path = tmp_path / "config.yaml"
    path.write_text(
        yaml.safe_dump(document, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    return path


def _provider(driver: str) -> ModelProviderConfig:
    """创建协议适配测试使用的外部 Provider。"""

    return ModelProviderConfig.model_validate(
        {
            "display_name": driver,
            "driver": driver,
            "base_url": "https://models.example.test",
            "default_model": "demo-model",
        }
    )


def _payload(*, tools: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """生成模型客户端统一输入。"""

    return {
        "model": "demo-model",
        "instructions": "请按要求处理。",
        "input": [
            {
                "role": "user",
                "content": [{"type": "input_text", "text": "你好"}],
            }
        ],
        "tools": tools or [],
        "tool_choice": "auto",
    }


def test_legacy_config_adds_builtin_provider_and_agent_reference(tmp_path) -> None:
    """旧配置应自动迁移到内置 Codex CLI，并保持旧 Agent 模型含义。"""

    config = parse_config_data(
        {
            "database": {"path": str(tmp_path / "state.db")},
            "runtime": {"codex": {"model": "gpt-demo"}},
            "agents": {
                "reviewer": {"prompt": "测试。", "model": "gpt-agent"}
            },
        },
        tmp_path / "config.yaml",
    )

    assert config.runtime.default_model.provider == "codex-cli"
    assert config.runtime.default_model.model == "gpt-demo"
    assert config.runtime.codex.model is None
    assert config.model_providers["codex-cli"].driver == "codex_cli"
    assert config.model_providers["codex-cli"].default_model == "gpt-demo"
    assert config.agents["reviewer"].model_provider == "codex-cli"
    assert config.agents["reviewer"].model == "gpt-agent"


def test_deleted_provider_references_fall_back_atomically(tmp_path) -> None:
    """删除 Provider 时应同时回退全局默认和所有 Agent 引用。"""

    manager = ConfigManager(_write_provider_config(tmp_path))
    updated = manager.delete_model_provider(
        expected_revision=manager.config.revision,
        provider_id="external-openai",
    )
    document = manager.document(mask_secrets=False)

    assert "external-openai" not in updated.model_providers
    assert updated.runtime.default_model.provider == "codex-cli"
    assert updated.runtime.default_model.model is None
    assert updated.agents["explicit"].model_provider is None
    assert updated.agents["explicit"].model is None
    assert "model_provider" not in document["agents"]["explicit"]
    assert "model" not in document["agents"]["explicit"]

    with pytest.raises(ValueError, match="不允许删除"):
        manager.delete_model_provider(
            expected_revision=updated.revision,
            provider_id="codex-cli",
        )


def test_disabled_provider_preserves_reference_but_blocks_new_run(tmp_path) -> None:
    """停用不是删除：引用保留，但新运行必须明确失败。"""

    config = parse_config_data(
        yaml.safe_load(_write_provider_config(tmp_path).read_text(encoding="utf-8")),
        tmp_path / "config.yaml",
    )
    config.model_providers["external-openai"].enabled = False

    with pytest.raises(ModelProviderUnavailableError, match="已停用"):
        resolve_model_selection(config, config.agents["explicit"])

    selection = resolve_model_selection(
        config,
        config.agents["explicit"],
        require_enabled=False,
    )
    assert selection.provider_id == "external-openai"
    assert selection.model == "agent-model"


def test_executor_routes_each_agent_to_its_effective_provider(tmp_path) -> None:
    """执行器必须按有效 Provider 选择 Runner，而不是复用全局单例。"""

    config_path = _write_provider_config(tmp_path)
    config = parse_config_data(
        yaml.safe_load(config_path.read_text(encoding="utf-8")),
        config_path,
    )
    store = StateStore(config.database.path)
    store.initialize()
    executor = AgentExecutor(config, store)

    cli_runner = executor._runner_for_provider("codex-cli")
    api_runner = executor._runner_for_provider("external-openai")
    assert isinstance(cli_runner, CodexRunner)
    assert isinstance(api_runner, CodexModelRunner)
    assert api_runner.provider_id == "external-openai"
    assert executor._runner_for_provider("external-openai") is api_runner


def test_model_snapshot_records_provider_and_resolved_inheritance(tmp_path) -> None:
    """每个 Agent 运行快照应固化 Provider、模型和继承说明。"""

    config_path = _write_provider_config(tmp_path)
    config = parse_config_data(
        yaml.safe_load(config_path.read_text(encoding="utf-8")),
        config_path,
    )
    snapshot = resolve_model_snapshot(config, config.agents["inherited"])

    assert snapshot["provider_id"] == "external-openai"
    assert snapshot["provider_driver"] == "openai_responses"
    assert snapshot["model"] == "global-model"
    assert snapshot["model_source"] == "global"
    assert snapshot["resolved_label"] == (
        "继承全局默认（External OpenAI / global-model）"
    )


def test_credential_store_masks_reveals_replaces_and_deletes(tmp_path) -> None:
    """API Key 应独立落盘，列表脱敏且只按需返回明文。"""

    store = ModelProviderCredentialStore(tmp_path / "credentials")
    store.replace("external-openai", "sk-first-secret")
    assert store.configured("external-openai") is True
    assert store.masked("external-openai") == "sk-****cret"
    assert store.reveal("external-openai") == "sk-first-secret"

    store.replace("external-openai", "sk-replaced")
    assert store.reveal("external-openai") == "sk-replaced"
    store.delete("external-openai")
    assert store.configured("external-openai") is False
    with pytest.raises(KeyError):
        store.reveal("external-openai")


def test_chat_history_groups_parallel_calls_before_tool_results() -> None:
    """同一轮多个函数调用应恢复成一条 Chat assistant 消息。"""

    messages = _chat_messages(
        "",
        [
            {
                "type": "function_call",
                "call_id": "call-1",
                "name": "first",
                "arguments": "{}",
            },
            {
                "type": "function_call",
                "call_id": "call-2",
                "name": "second",
                "arguments": "{}",
            },
            {
                "type": "function_call_output",
                "call_id": "call-1",
                "output": "one",
            },
            {
                "type": "function_call_output",
                "call_id": "call-2",
                "output": "two",
            },
        ],
    )

    assert [message["role"] for message in messages] == [
        "assistant",
        "tool",
        "tool",
    ]
    assert [call["id"] for call in messages[0]["tool_calls"]] == [
        "call-1",
        "call-2",
    ]


async def test_openai_responses_protocol_keeps_native_output() -> None:
    """Responses 兼容接口应保留统一 output 与 usage。"""

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/responses"
        assert request.headers["authorization"] == "Bearer secret"
        body = json.loads(request.content)
        assert body["stream"] is False
        return httpx.Response(
            200,
            json={
                "id": "response-1",
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "完成"}],
                    }
                ],
                "usage": {"input_tokens": 2, "output_tokens": 1},
            },
        )

    client = ExternalModelClient(
        _provider("openai_responses"),
        "secret",
        timeout_seconds=10,
        idle_timeout_seconds=10,
        transport=httpx.MockTransport(handler),
    )
    result = await client.create_response(_payload())
    assert result["id"] == "response-1"
    assert result["output"][0]["content"][0]["text"] == "完成"


async def test_openai_chat_protocol_normalizes_tool_call() -> None:
    """Chat Completions 的函数调用应转换为统一工具调用。"""

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["messages"][0]["role"] == "system"
        assert body["tools"][0]["function"]["name"] == "execute_command"
        return httpx.Response(
            200,
            json={
                "id": "chat-1",
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "准备执行",
                            "tool_calls": [
                                {
                                    "id": "call-1",
                                    "type": "function",
                                    "function": {
                                        "name": "execute_command",
                                        "arguments": '{"command":["pwd"]}',
                                    },
                                }
                            ],
                        }
                    }
                ],
                "usage": {"prompt_tokens": 3, "completion_tokens": 2},
            },
        )

    tool = {
        "type": "function",
        "name": "execute_command",
        "description": "执行命令",
        "parameters": {"type": "object"},
    }
    client = ExternalModelClient(
        _provider("openai_chat_completions"),
        "secret",
        timeout_seconds=10,
        idle_timeout_seconds=10,
        transport=httpx.MockTransport(handler),
    )
    result = await client.create_response(_payload(tools=[tool]))
    assert result["output_text"] == "准备执行"
    assert result["output"][1]["type"] == "function_call"
    assert result["output"][1]["call_id"] == "call-1"
    assert result["usage"]["total_tokens"] == 5


@pytest.mark.parametrize(
    ("driver", "response_document", "expected_path", "expected_name"),
    [
        (
            "anthropic_messages",
            {
                "id": "message-1",
                "content": [
                    {"type": "text", "text": "调用工具"},
                    {
                        "type": "tool_use",
                        "id": "tool-1",
                        "name": "execute_command",
                        "input": {"command": ["pwd"]},
                    },
                ],
                "usage": {"input_tokens": 4, "output_tokens": 3},
            },
            "/v1/messages",
            "execute_command",
        ),
        (
            "gemini_generate_content",
            {
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {"text": "调用工具"},
                                {
                                    "functionCall": {
                                        "name": "execute_command",
                                        "args": {"command": ["pwd"]},
                                    }
                                },
                            ]
                        }
                    }
                ],
                "usageMetadata": {
                    "promptTokenCount": 4,
                    "candidatesTokenCount": 3,
                    "totalTokenCount": 7,
                },
            },
            "/v1beta/models/demo-model:generateContent",
            "execute_command",
        ),
    ],
)
async def test_native_protocols_normalize_tool_calls(
    driver: str,
    response_document: dict[str, Any],
    expected_path: str,
    expected_name: str,
) -> None:
    """Anthropic 与 Gemini 工具调用应进入同一 Agent 工具循环。"""

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == expected_path
        return httpx.Response(200, json=response_document)

    tool = {
        "type": "function",
        "name": "execute_command",
        "description": "执行命令",
        "parameters": {"type": "object"},
    }
    client = ExternalModelClient(
        _provider(driver),
        "secret",
        timeout_seconds=10,
        idle_timeout_seconds=10,
        transport=httpx.MockTransport(handler),
    )
    result = await client.create_response(_payload(tools=[tool]))
    call = next(item for item in result["output"] if item["type"] == "function_call")
    assert call["name"] == expected_name
    assert result["usage"]["total_tokens"] == 7


@pytest.mark.parametrize(
    "driver",
    ["openai_chat_completions", "anthropic_messages", "gemini_generate_content"],
)
async def test_connection_round_omits_empty_tool_declarations(driver: str) -> None:
    """无工具连接测试不应发送部分兼容服务会拒绝的空工具数组。"""

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert "tools" not in body
        assert "reasoning_effort" not in body
        if driver == "openai_chat_completions":
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": "hi"}}]},
            )
        if driver == "anthropic_messages":
            return httpx.Response(
                200,
                json={"content": [{"type": "text", "text": "hi"}]},
            )
        return httpx.Response(
            200,
            json={
                "candidates": [{"content": {"parts": [{"text": "hi"}]}}]
            },
        )

    client = ExternalModelClient(
        _provider(driver),
        "secret",
        timeout_seconds=10,
        idle_timeout_seconds=10,
        transport=httpx.MockTransport(handler),
    )
    result = await client.create_response(_payload())
    assert result["output_text"] == "hi"


def test_model_provider_web_api_masks_reveals_and_deletes(tmp_path) -> None:
    """管理 API 应提供掩码列表、小眼睛明文和带引用回退的删除。"""

    config_path = _write_provider_config(tmp_path)
    app = create_app(config_path, start_scheduler=False)
    with TestClient(app) as client:
        snapshot = client.get("/api/model-providers")
        assert snapshot.status_code == 200
        external = next(
            item
            for item in snapshot.json()["providers"]
            if item["id"] == "external-openai"
        )
        assert external["api_key_configured"] is False
        assert external["referenced_agents"] == ["explicit"]
        assert external["is_global_default"] is True

        replaced = client.put(
            "/api/model-providers/external-openai/key",
            json={"api_key": "sk-web-secret"},
        )
        assert replaced.status_code == 200
        external = next(
            item
            for item in replaced.json()["providers"]
            if item["id"] == "external-openai"
        )
        assert external["masked_key"] == "sk-****cret"
        assert "sk-web-secret" not in json.dumps(replaced.json())
        revealed = client.get("/api/model-providers/external-openai/key")
        assert revealed.json() == {"api_key": "sk-web-secret"}
        assert revealed.headers["cache-control"] == "no-store"

        current = client.get("/api/config").json()
        deleted = client.request(
            "DELETE",
            "/api/model-providers/external-openai",
            json={"revision": current["revision"]},
        )
        assert deleted.status_code == 200
        document = deleted.json()["document"]
        assert document["runtime"]["default_model"] == {
            "provider": "codex-cli"
        }
        assert "model_provider" not in document["agents"]["explicit"]
        assert client.get(
            "/api/model-providers/external-openai/key"
        ).status_code == 404
