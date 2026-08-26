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
    ModelSelectionConfig,
    parse_config_data,
)
from teamwork_review_agents.config_manager import ConfigManager
from teamwork_review_agents.executor import AgentExecutor
from teamwork_review_agents.model_provider_client import (
    ExternalModelClient,
    ModelProviderRequestError,
    _chat_messages,
    discover_model_catalog,
)
from teamwork_review_agents.model_provider_credentials import (
    ModelProviderCredentialStore,
)
from teamwork_review_agents.model_provider_runtime import (
    ModelProviderUnavailableError,
    resolve_model_plan,
    resolve_model_selection,
    resolve_model_snapshot,
    supports_reasoning_effort,
)
from teamwork_review_agents.environment import SecretRedactor
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


def test_legacy_oauth_provider_collapses_into_codex_cli(tmp_path) -> None:
    """历史 OAuth Provider 应并入 Codex CLI，并迁移全部活动引用。"""

    config = parse_config_data(
        {
            "database": {"path": str(tmp_path / "state.db")},
            "runtime": {
                "codex": {"execution_mode": "cli"},
                "default_model": {"provider": "codex-oauth"},
            },
            "model_providers": {
                "codex-cli": {
                    "display_name": "Codex CLI",
                    "driver": "codex_cli",
                    "default_model": "cli-model",
                },
                "codex-oauth": {
                    "display_name": "Codex OAuth 模型基座",
                    "driver": "codex_oauth",
                    "default_model": "oauth-model",
                    "models": ["oauth-model", "oauth-next"],
                    "model_reasoning_effort": "high",
                },
            },
            "agents": {
                "reviewer": {
                    "prompt": "测试。",
                    "model_provider": "codex-oauth",
                }
            },
        },
        tmp_path / "config.yaml",
    )

    assert set(config.model_providers) == {"codex-cli"}
    assert config.runtime.codex.execution_mode == "model"
    assert config.runtime.default_model.provider == "codex-cli"
    assert config.agents["reviewer"].model_provider == "codex-cli"
    assert config.model_providers["codex-cli"].default_model == "oauth-model"
    assert config.model_providers["codex-cli"].models == [
        "oauth-model",
        "oauth-next",
    ]
    assert config.model_providers["codex-cli"].model_reasoning_effort == "high"
    snapshot = resolve_model_snapshot(config, config.agents["reviewer"])
    assert snapshot["provider_id"] == "codex-cli"
    assert snapshot["provider_driver"] == "codex_cli"
    assert snapshot["execution_mode"] == "model"


def test_deleted_provider_references_fall_back_atomically(tmp_path) -> None:
    """删除 Provider 时应同时回退全局默认和所有 Agent 引用。"""

    config_path = _write_provider_config(tmp_path)
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    raw["runtime"]["default_model_fallbacks"] = [
        {"provider": "external-openai", "model": "backup-model"},
    ]
    raw["agents"]["explicit"]["model_fallbacks"] = [
        {"provider": "external-openai", "model": "backup-model"},
        {"provider": "codex-cli", "model": "codex-backup"},
    ]
    config_path.write_text(
        yaml.safe_dump(raw, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    manager = ConfigManager(config_path)
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
    assert updated.runtime.default_model_fallbacks == []
    assert updated.agents["explicit"].model_fallbacks == [
        ModelSelectionConfig(provider="codex-cli", model="codex-backup"),
    ]

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
    assert isinstance(cli_runner, CodexModelRunner)
    assert isinstance(api_runner, CodexModelRunner)
    assert api_runner.provider_id == "external-openai"
    assert executor._runner_for_provider("external-openai") is api_runner

    config.runtime.codex.execution_mode = "cli"
    cli_executor = AgentExecutor(config, store)
    assert isinstance(cli_executor._runner_for_provider("codex-cli"), CodexRunner)


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


def test_model_plan_orders_agent_chain_before_global_chain_and_deduplicates(tmp_path) -> None:
    """Agent 主模型后应先走 Agent 回退，再接全局链并去重。"""

    config_path = _write_provider_config(tmp_path)
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    providers = raw["model_providers"]
    for provider_id in ("agent-fallback", "global-fallback", "global-fallback-2"):
        providers[provider_id] = {
            "display_name": provider_id,
            "driver": "openai_responses",
            "base_url": "https://models.example.test",
            "default_model": f"{provider_id}-model",
        }
    raw["runtime"]["default_model_fallbacks"] = [
        {"provider": "global-fallback", "model": "global-b"},
        {"provider": "global-fallback-2", "model": "global-c"},
    ]
    raw["agents"]["explicit"]["model_fallbacks"] = [
        {"provider": "agent-fallback", "model": "agent-e"},
        {"provider": "external-openai", "model": "global-model"},
    ]
    config_path.write_text(
        yaml.safe_dump(raw, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    config = parse_config_data(raw, config_path)

    plan = resolve_model_plan(config, config.agents["explicit"])

    assert [
        (selection.provider_id, selection.model)
        for selection in plan.selections
    ] == [
        ("external-openai", "agent-model"),
        ("agent-fallback", "agent-e"),
        ("external-openai", "global-model"),
        ("global-fallback", "global-b"),
        ("global-fallback-2", "global-c"),
    ]


def test_model_snapshot_contains_fallback_plan(tmp_path) -> None:
    """模型运行快照应固化完整的去重候选链。"""

    config_path = _write_provider_config(tmp_path)
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    raw["runtime"]["default_model_fallbacks"] = [
        {"provider": "external-openai", "model": "provider-model"},
    ]
    config_path.write_text(
        yaml.safe_dump(raw, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    config = parse_config_data(raw, config_path)
    snapshot = resolve_model_snapshot(config, config.agents["inherited"])

    assert [item["model"] for item in snapshot["fallback_plan"]] == [
        "global-model",
        "provider-model",
    ]
    assert snapshot["fallback_attempts"] == []
    assert snapshot["fallback_used"] is False


def test_reasoning_effort_is_bound_to_gpt_model_nodes(tmp_path) -> None:
    """只有 GPT 模型节点能覆盖推理 effort，其他模型应安全忽略历史值。"""

    config_path = _write_provider_config(tmp_path)
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    raw["model_providers"]["external-openai"]["default_model"] = "gpt-5.6"
    raw["model_providers"]["external-openai"]["models"] = ["gpt-5.6", "deepseek-v4"]
    raw["model_providers"]["external-openai"]["model_reasoning_effort"] = "high"
    raw["runtime"]["default_model"] = {
        "provider": "external-openai",
        "model": "gpt-5.6",
        "reasoning_effort": "low",
    }
    raw["runtime"]["default_model_fallbacks"] = [
        {
            "provider": "external-openai",
            "model": "deepseek-v4",
            "reasoning_effort": "high",
        }
    ]
    raw["agents"]["inherited"]["model_fallbacks"] = [
        {
            "provider": "external-openai",
            "model": "gpt-5.6",
            "reasoning_effort": "medium",
        }
    ]
    config_path.write_text(
        yaml.safe_dump(raw, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    config = parse_config_data(raw, config_path)
    provider = config.model_providers["external-openai"]

    assert supports_reasoning_effort(provider, "gpt-5.6") is True
    assert supports_reasoning_effort(provider, "deepseek-v4") is False
    plan = resolve_model_plan(config, config.agents["inherited"])
    assert plan.selections[0].reasoning_effort == "low"
    assert plan.selections[1].reasoning_effort == "high"

    snapshot = resolve_model_snapshot(config, config.agents["inherited"])
    assert snapshot["reasoning_effort"] == "low"
    assert snapshot["reasoning_effort_source"] == "model_selection"
    assert snapshot["fallback_plan"][1]["reasoning_effort"] is None
    assert snapshot["fallback_plan"][1]["reasoning_effort_source"] == "unsupported"

    non_gpt_agent = config.agents["inherited"].model_copy(
        update={
            "model_provider": "external-openai",
            "model": "deepseek-v4",
            "model_reasoning_effort": "high",
        }
    )
    non_gpt_snapshot = resolve_model_snapshot(config, non_gpt_agent)
    assert non_gpt_snapshot["reasoning_effort"] is None
    assert non_gpt_snapshot["reasoning_effort_source"] == "unsupported"
    _, non_gpt_reasoning, _, _, _ = CodexModelRunner(config)._settings_for_provider(
        non_gpt_agent,
        provider,
        codex_model_base=False,
    )
    assert non_gpt_reasoning is None


@pytest.mark.asyncio
async def test_model_runner_falls_back_without_replaying_tools(
    configured_app_factory,
    snapshot_factory,
    monkeypatch,
) -> None:
    """Provider 限流时应在同一模型循环切换，不能重新执行已完成工具。"""

    config = configured_app_factory()
    config.model_providers["provider-a"] = ModelProviderConfig.model_validate(
        {
            "display_name": "Provider A",
            "driver": "openai_responses",
            "base_url": "https://a.example.test",
            "default_model": "model-a",
        }
    )
    config.model_providers["provider-b"] = ModelProviderConfig.model_validate(
        {
            "display_name": "Provider B",
            "driver": "openai_responses",
            "base_url": "https://b.example.test",
            "default_model": "model-b",
        }
    )
    config.runtime.default_model = ModelSelectionConfig(
        provider="provider-a",
        model="model-a",
    )
    config.runtime.default_model_fallbacks = [
        ModelSelectionConfig(provider="provider-b", model="model-b")
    ]
    agent = config.agents["code-reviewer"].model_copy(
        update={"sandbox": "danger-full-access"}
    )
    credentials = ModelProviderCredentialStore(
        config.database.path.parent / "model-provider-credentials"
    )
    credentials.replace("provider-a", "key-a")
    credentials.replace("provider-b", "key-b")
    calls: list[str] = []

    async def fake_create_response(self, payload, *, event_callback=None):
        calls.append(self.provider.display_name)
        if self.provider.display_name == "Provider A":
            raise ModelProviderRequestError(
                "模型 Provider 请求失败（HTTP 429）",
                status_code=429,
                fallbackable=True,
            )
        return {
            "id": "fallback-response",
            "output": [
                {
                    "id": "fallback-message",
                    "type": "message",
                    "content": [{"type": "output_text", "text": "备用完成"}],
                }
            ],
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }

    monkeypatch.setattr(ExternalModelClient, "create_response", fake_create_response)
    snapshots: list[dict[str, Any]] = []
    async def save_snapshot(snapshot: dict[str, Any]) -> None:
        snapshots.append(snapshot)

    plan = resolve_model_plan(config, agent)
    result = await CodexModelRunner(config, provider_id="provider-a").run(
        run_id="fallback-run",
        root_run_id="fallback-run",
        parent_run_id=None,
        agent_name="code-reviewer",
        agent=agent,
        repository=config.repositories[0],
        context=None,
        prompt="测试回退",
        process_environment={},
        redactor=SecretRedactor(()),
        model_plan=plan.selections,
        model_snapshot_callback=save_snapshot,
    )

    assert result.status == "completed"
    assert result.final_message == "备用完成"
    assert calls == ["Provider A", "Provider B"]
    assert snapshots[-1]["provider_id"] == "provider-b"
    assert snapshots[-1]["fallback_used"] is True


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


@pytest.mark.parametrize(
    ("status_code", "expected_fallbackable"),
    [(400, False), (429, True), (503, True)],
)
async def test_external_http_error_marks_fallbackability(
    status_code: int,
    expected_fallbackable: bool,
) -> None:
    """只有认证、限流和服务不可用等 HTTP 错误才进入模型回退链。"""

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json={"error": {"message": "failed"}})

    client = ExternalModelClient(
        _provider("openai_responses"),
        "secret",
        timeout_seconds=10,
        idle_timeout_seconds=10,
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(ModelProviderRequestError) as raised:
        await client.create_response(_payload())
    assert raised.value.status_code == status_code
    assert raised.value.fallbackable is expected_fallbackable


async def test_external_http_error_exposes_sanitized_upstream_reason() -> None:
    """外部 Provider 的嵌套错误应显示真实原因并隐藏凭据。"""

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            headers={"X-Request-Id": "header-request-id"},
            json={
                "error": {
                    "message": (
                        "Unsupported value: 'minimal' is not supported with the "
                        "'gpt-5.6-terra' model. Bearer sk-test-secret-value"
                    ),
                    "type": "invalid_request_error",
                    "code": "unsupported_value",
                    "param": "reasoning.effort",
                }
            },
        )

    client = ExternalModelClient(
        _provider("openai_responses"),
        "secret",
        timeout_seconds=10,
        idle_timeout_seconds=10,
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(ModelProviderRequestError) as raised:
        await client.create_response(_payload())

    message = str(raised.value)
    assert "HTTP 400" in message
    assert "Unsupported value" in message
    assert "类型=invalid_request_error" in message
    assert "代码=unsupported_value" in message
    assert "参数=reasoning.effort" in message
    assert "请求 ID=header-request-id" in message
    assert "sk-test-secret-value" not in message


async def test_external_http_error_with_plain_text_keeps_bounded_reason() -> None:
    """非 JSON 错误正文也应保留有界脱敏文本。"""

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, text="upstream unavailable\n" * 1000)

    client = ExternalModelClient(
        _provider("openai_responses"),
        "secret",
        timeout_seconds=10,
        idle_timeout_seconds=10,
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(ModelProviderRequestError) as raised:
        await client.create_response(_payload())

    message = str(raised.value)
    assert message.startswith("模型 Provider 请求失败（HTTP 502）：")
    assert len(message) <= 2000


@pytest.mark.parametrize(
    ("driver", "base_url", "expected_path", "response_document"),
    [
        (
            "openai_responses",
            "https://models.example.test/v1",
            "/v1/models",
            {"data": [{"id": "gpt-draft"}, {"id": "gpt-draft"}]},
        ),
        (
            "openai_chat_completions",
            "https://models.example.test/v1",
            "/v1/models",
            {"data": [{"id": "chat-draft"}]},
        ),
        (
            "anthropic_messages",
            "https://models.example.test/v1",
            "/v1/models",
            {"data": [{"id": "claude-draft"}]},
        ),
        (
            "gemini_generate_content",
            "https://models.example.test",
            "/v1beta/models",
            {"models": [{"name": "models/gemini-draft"}]},
        ),
    ],
)
async def test_draft_model_catalog_discovery_uses_normalized_endpoint(
    driver: str,
    base_url: str,
    expected_path: str,
    response_document: dict[str, Any],
) -> None:
    """草稿检测应按协议请求模型目录并去重模型 ID。"""

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == expected_path
        assert (
            request.headers.get("authorization") == "Bearer draft-secret"
            or request.headers.get("x-goog-api-key") == "draft-secret"
            or request.headers.get("x-api-key") == "draft-secret"
        )
        return httpx.Response(200, json=response_document)

    result = await discover_model_catalog(
        driver=driver,
        base_url=base_url,
        api_key="draft-secret",
        transport=httpx.MockTransport(handler),
    )

    assert result in (
        ["gpt-draft"],
        ["chat-draft"],
        ["claude-draft"],
        ["gemini-draft"],
    )


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
        assert [item["id"] for item in snapshot.json()["providers"]] == [
            "codex-cli",
            "external-openai",
        ]
        external = next(
            item
            for item in snapshot.json()["providers"]
            if item["id"] == "external-openai"
        )
        assert external["api_key_configured"] is False
        assert external["referenced_agents"] == ["explicit"]
        assert external["is_global_default"] is True
        assert external["is_global_fallback"] is False

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
        codex_provider = current["document"]["model_providers"]["codex-cli"]
        updated_codex = client.put(
            "/api/model-providers/codex-cli",
            json={
                "revision": current["revision"],
                "provider_id": "codex-cli",
                "provider": codex_provider,
                "codex_runtime": {
                    "codex_binary": "codex-custom",
                    "codex_home": None,
                    "expected_codex_version": "1.2.3",
                    "inherit_user_mcp_servers": False,
                    "allowed_user_mcp_servers": [],
                    "codex": {
                        "execution_mode": "cli",
                        "fast_mode": "inherit",
                        "extra_config": {},
                    },
                },
            },
        )
        assert updated_codex.status_code == 200
        assert updated_codex.json()["document"]["runtime"]["codex"][
            "execution_mode"
        ] == "cli"
        assert updated_codex.json()["document"]["runtime"][
            "codex_binary"
        ] == "codex-custom"
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


def test_model_provider_connection_test_does_not_force_reasoning_effort(
    tmp_path,
    monkeypatch,
) -> None:
    """连接测试不应为 GPT 模型强制发送可能不兼容的 minimal。"""

    config_path = _write_provider_config(tmp_path)
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    raw["model_providers"]["external-openai"]["default_model"] = "gpt-5.6-terra"
    raw["model_providers"]["external-openai"]["model_reasoning_effort"] = "high"
    config_path.write_text(
        yaml.safe_dump(raw, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    captured: dict[str, Any] = {}

    async def fake_create_response(self, payload, *, event_callback=None):
        captured.update(payload)
        return {"output": [], "output_text": "hi"}

    monkeypatch.setattr(ExternalModelClient, "create_response", fake_create_response)
    app = create_app(config_path, start_scheduler=False)
    with TestClient(app) as client:
        key_response = client.put(
            "/api/model-providers/external-openai/key",
            json={"api_key": "sk-connection-test"},
        )
        assert key_response.status_code == 200
        response = client.post("/api/model-providers/external-openai/connection-test")

    assert response.status_code == 200
    assert response.json()["success"] is True
    assert captured["model"] == "gpt-5.6-terra"
    assert "tools" not in captured
    assert "tool_choice" not in captured
    assert "reasoning" not in captured


def test_model_provider_draft_discovery_does_not_save_key_or_provider(
    tmp_path,
    monkeypatch,
) -> None:
    """新建 Provider 检测模型时只使用草稿参数，不提前写配置或凭据。"""

    config_path = _write_provider_config(tmp_path)
    calls: list[dict[str, Any]] = []

    async def fake_discovery(**kwargs):
        calls.append(kwargs)
        return ["draft-model", "draft-backup"]

    monkeypatch.setattr(
        "teamwork_review_agents.webapp.discover_model_catalog",
        fake_discovery,
    )
    original = config_path.read_text(encoding="utf-8")
    app = create_app(config_path, start_scheduler=False)
    with TestClient(app) as client:
        response = client.post(
            "/api/model-providers/discover-models",
            json={
                "driver": "openai_responses",
                "base_url": "https://draft.example.test/v1",
                "request_timeout_seconds": 20,
                "api_key": "sk-draft-secret",
            },
        )

    assert response.status_code == 200
    assert response.json() == {"models": ["draft-model", "draft-backup"]}
    assert calls == [
        {
            "driver": "openai_responses",
            "base_url": "https://draft.example.test/v1",
            "api_key": "sk-draft-secret",
            "timeout_seconds": 20.0,
        }
    ]
    assert config_path.read_text(encoding="utf-8") == original
    assert "sk-draft-secret" not in response.text


def test_model_provider_draft_discovery_can_reuse_saved_key(tmp_path, monkeypatch) -> None:
    """已保存 Provider 未输入新 Key 时，草稿检测应复用受管凭据。"""

    config_path = _write_provider_config(tmp_path)
    captured: dict[str, Any] = {}

    async def fake_discovery(**kwargs):
        captured.update(kwargs)
        return ["saved-key-model"]

    monkeypatch.setattr(
        "teamwork_review_agents.webapp.discover_model_catalog",
        fake_discovery,
    )
    app = create_app(config_path, start_scheduler=False)
    with TestClient(app) as client:
        current = client.get("/api/config").json()
        saved = client.put(
            "/api/model-providers/external-openai/key",
            json={"api_key": "sk-saved-secret"},
        )
        assert saved.status_code == 200
        response = client.post(
            "/api/model-providers/discover-models",
            json={
                "provider_id": "external-openai",
                "driver": "openai_responses",
                "base_url": "https://models.example.test",
                "request_timeout_seconds": 15,
            },
        )

    assert current["revision"]
    assert response.status_code == 200
    assert response.json() == {"models": ["saved-key-model"]}
    assert captured["api_key"] == "sk-saved-secret"
    assert "sk-saved-secret" not in response.text


def test_model_provider_draft_discovery_requires_key_for_new_provider(tmp_path) -> None:
    """新建 Provider 未提供 API Key 时检测应明确拒绝。"""

    app = create_app(_write_provider_config(tmp_path), start_scheduler=False)
    with TestClient(app) as client:
        response = client.post(
            "/api/model-providers/discover-models",
            json={
                "driver": "openai_responses",
                "base_url": "https://models.example.test/v1",
            },
        )

    assert response.status_code == 422
    assert response.json()["detail"] == "请先填写 API Key"
