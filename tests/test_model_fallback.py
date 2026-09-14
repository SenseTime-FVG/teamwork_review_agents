"""请求级回退、明确额度耗尽跳过及跨运行隔离回归。"""

from __future__ import annotations

import json
from functools import partial
from types import SimpleNamespace

import httpx
import pytest

from teamwork_review_agents.codex_model_client import CodexResponsesClient, CodexUpstreamError
from teamwork_review_agents.codex_model_runner import CodexModelRunner
from teamwork_review_agents.config import ModelProviderConfig, ModelSelectionConfig
from teamwork_review_agents.environment import SecretRedactor
from teamwork_review_agents.model_provider_client import ExternalModelClient, ModelProviderRequestError
from teamwork_review_agents.model_provider_credentials import ModelProviderCredentialStore
from teamwork_review_agents.model_provider_runtime import resolve_model_plan
from teamwork_review_agents.model_quota import is_quota_exhausted
from teamwork_review_agents.model_tools import ModelToolExecutor


@pytest.mark.parametrize("fields,expected", [
    *[({"code": code}, True) for code in (
        "insufficient_quota", "credit_balance_exhausted",
        "organization_spend_limit_exceeded", "project_spend_limit_exceeded",
        "organization_usage_limit_exceeded", "usage_limit_reached",
        "billing_hard_limit_reached", "insufficient_balance", "quota_exhausted",
    )],
    ({"type": "insufficient_quota"}, True),
    ({"type": "invalid_request_error", "code": "insufficient_quota"}, True),
    ({"code": "rate_limit_exceeded"}, False),
    ({"type": "rate_limit_error", "code": "slow_down"}, False),
    ({"code": "resource_exhausted"}, False),
    ({"code": "context_length_exceeded"}, False),
    ({"message": "Quota exceeded"}, False),
    ({"message": "insufficient_quota"}, False),
    ({"code": "unknown", "request_id": "insufficient_quota"}, False),
    ({}, False),
])
def test_quota_classification_requires_explicit_code_or_type(fields, expected):
    """模糊文本和普通速率限制不得永久跳过本轮候选。"""

    assert is_quota_exhausted(fields) is expected


@pytest.mark.parametrize("client_kind", ["external", "codex_http", "codex_sse", "codex_json"])
@pytest.mark.parametrize("fields,status,expected", [
    ({"type": "invalid_request_error", "code": "insufficient_quota"}, 400, True),
    ({"type": "insufficient_quota"}, 429, True),
    ({"code": "credit_balance_exhausted"}, 429, True),
    ({"code": "usage_limit_reached"}, 429, True),
    ({"code": "insufficient_balance"}, 402, True),
    ({"code": "project_spend_limit_exceeded"}, 503, True),
    ({"type": "rate_limit_error", "code": "rate_limit_exceeded"}, 429, False),
    ({"type": "rate_limit_error", "code": "slow_down"}, 429, False),
    ({"message": "Quota exceeded"}, 429, False),
    ({"message": "payment required"}, 402, False),
])
async def test_clients_preserve_quota_metadata(client_kind, fields, status, expected, monkeypatch):
    """HTTP、嵌套 SSE 及 JSON 同样识别额度，明确耗尽不做客户端内重试。"""

    class OAuth:
        """替代本机 OAuth，测试不能读取真实登录。"""

        async def credentials(self):
            return object()

    monkeypatch.setattr("teamwork_review_agents.codex_model_client._codex_headers", lambda *args, **kwargs: {})
    error = {"type": "error", "error": {"message": "上游拒绝 Bearer sk-test-secret-value", **fields}}
    calls = []

    async def handler(request):
        calls.append(request)
        if client_kind in {"external", "codex_http"}:
            return httpx.Response(status, json=error)
        event = {"type": "response.failed", "response": {"status": "failed", **error}}
        if client_kind == "codex_json":
            return httpx.Response(200, json=event)
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=f"data: {json.dumps(event)}\n\n")

    transport = httpx.MockTransport(handler)
    if client_kind == "external":
        client = ExternalModelClient(
            ModelProviderConfig(display_name="external", driver="openai_responses", base_url="https://a.example.test", default_model="gpt-a"),
            "test-key", timeout_seconds=10, idle_timeout_seconds=10, transport=transport,
        )
    else:
        client = CodexResponsesClient(oauth=OAuth(), codex_binary="unused-test-codex", transport=transport)
    with pytest.raises((CodexUpstreamError, ModelProviderRequestError)) as raised:
        await client.create_response({"model": "gpt-a", "input": []})
    assert raised.value.quota_exhausted is expected
    assert raised.value.fallbackable is True
    assert len(calls) == 1
    assert "sk-test-secret-value" not in str(raised.value)


async def test_external_success_status_cannot_hide_quota_error():
    """兼容服务用 200 封装错误时，仍应传递额度元数据而非成功事件。"""

    async def handler(request):
        return httpx.Response(200, json={
            "type": "response.failed", "response": {
                "status": "failed", "error": {"type": "insufficient_quota"},
            },
        })

    events = []

    async def receive(event):
        events.append(event)

    client = ExternalModelClient(
        ModelProviderConfig(display_name="external", driver="openai_responses", base_url="https://a.example.test", default_model="gpt-a"),
        "test-key", timeout_seconds=10, idle_timeout_seconds=10,
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(ModelProviderRequestError) as raised:
        await client.create_response({"model": "gpt-a"}, event_callback=receive)
    assert raised.value.quota_exhausted is True
    assert raised.value.fallbackable is True
    assert events == []


def _completed():
    """构造无需真实模型的最终响应。"""

    return httpx.Response(200, json={"output": [{
        "type": "message", "content": [{"type": "output_text", "text": "完成"}],
    }]})


def _tool_response(index):
    """构造带唯一标识的工具调用，便于验证没有重放。"""

    return httpx.Response(200, json={"output": [{
        "type": "function_call", "call_id": f"call-{index}", "name": "execute_command",
        "arguments": json.dumps({"index": index}),
    }]})


def _quota_error():
    """构造包含敏感样例文本的明确额度错误。"""

    return httpx.Response(429, json={"error": {
        "code": "insufficient_quota", "message": "额度耗尽 Bearer sk-test-secret-value",
    }})


def _outputs(body):
    """提取前序已经执行完成的工具结果。"""

    return [item for item in body["input"] if item.get("type") == "function_call_output"]


@pytest.fixture
def fallback_harness(configured_app_factory, monkeypatch):
    """以真实外部协议客户端驱动循环，只替换网络与工具执行。"""

    def create(handler, *, chain=(("a", "gpt-a"), ("b", "gpt-b"), ("c", "gpt-c")), effort=None):
        config = configured_app_factory()
        credentials = ModelProviderCredentialStore(config.database.path.parent / "model-provider-credentials")
        for provider_id, model in chain:
            config.model_providers[provider_id] = ModelProviderConfig(
                display_name=provider_id, driver="openai_responses",
                base_url=f"https://{provider_id}.example.test", default_model=model,
                model_reasoning_effort=effort,
            )
            credentials.replace(provider_id, "test-key")
        selections = [ModelSelectionConfig(provider=provider_id, model=model) for provider_id, model in chain]
        config.runtime.default_model = selections[0]
        config.runtime.default_model_fallbacks = selections[1:]
        agent = config.agents["code-reviewer"].model_copy(update={"sandbox": "danger-full-access"})
        plan = resolve_model_plan(config, agent).selections
        monkeypatch.setattr(
            "teamwork_review_agents.codex_model_runner.ExternalModelClient",
            partial(ExternalModelClient, transport=httpx.MockTransport(handler)),
        )
        tool_calls = []

        async def execute(self, name, arguments, **kwargs):
            tool_calls.append(arguments["index"])
            return {"recorded": arguments["index"]}

        monkeypatch.setattr(ModelToolExecutor, "execute", execute)
        runner = CodexModelRunner(config, provider_id=chain[0][0])
        run_count = 0

        async def run():
            nonlocal run_count
            run_count += 1
            run_id = f"fallback-{run_count}"
            snapshots, logs = [], []

            async def snapshot_callback(snapshot):
                snapshots.append(snapshot)

            async def log_callback(stream, event_type, payload):
                logs.append((event_type, payload))

            result = await runner.run(
                run_id=run_id, root_run_id=run_id, parent_run_id=None,
                agent_name="code-reviewer", agent=agent, repository=config.repositories[0],
                context=None, prompt="测试每轮选模", process_environment={}, redactor=SecretRedactor(()),
                model_plan=plan, model_snapshot_callback=snapshot_callback, log_callback=log_callback,
            )
            return SimpleNamespace(result=result, snapshots=snapshots, logs=logs)

        return SimpleNamespace(run=run, config=config, tool_calls=tool_calls)

    return create


@pytest.mark.parametrize("failure", ["429", "503", "timeout"])
async def test_next_request_returns_to_primary_without_replaying_tools(fallback_harness, failure):
    """临时错误只影响当前请求，第二次仍先用 A 而不是从 B 继续。"""

    calls = []

    async def handler(request):
        body = json.loads(request.content)
        calls.append(body)
        if len(calls) == 1:
            if failure == "timeout":
                raise httpx.ReadTimeout("读取超时", request=request)
            return httpx.Response(int(failure), json={"error": {"code": "rate_limit_exceeded" if failure == "429" else "server_error"}})
        if not _outputs(body):
            return _tool_response(0)
        return _completed()

    harness = fallback_harness(handler)
    outcome = await harness.run()
    assert outcome.result.status == "completed"
    assert [body["model"] for body in calls] == ["gpt-a", "gpt-b", "gpt-a"]
    assert calls[0]["input"] == calls[1]["input"]
    assert len(_outputs(calls[2])) == 1
    assert json.loads(_outputs(calls[2])[0]["output"]) == {"recorded": 0}
    assert harness.tool_calls == [0]
    assert outcome.snapshots[-1]["provider_id"] == "a"
    assert outcome.snapshots[-1]["request_round"] == 2
    assert outcome.snapshots[-1]["fallback_used"] is True
    assert outcome.snapshots[-1]["quota_exhausted_models"] == []
    assert harness.config.runtime.default_model.provider == "a"


async def test_quota_skip_is_run_local_and_temporary_backup_failure_does_not_stick(fallback_harness):
    """A 耗尽后 B 暂时失败可去 C，但后续应恢复 B；新运行重新尝试 A。"""

    calls = []

    async def handler(request):
        body = json.loads(request.content)
        calls.append(body)
        if body["model"] == "gpt-a":
            return _quota_error()
        count = len(_outputs(body))
        if count == 1 and body["model"] == "gpt-b":
            return httpx.Response(503, json={"error": {"code": "server_error"}})
        return _tool_response(count) if count < 2 else _completed()

    harness = fallback_harness(handler)
    for _ in range(2):
        start = len(calls)
        outcome = await harness.run()
        assert outcome.result.status == "completed"
        assert [body["model"] for body in calls[start:]] == ["gpt-a", "gpt-b", "gpt-b", "gpt-c", "gpt-b"]
        assert calls[start + 2]["input"] == calls[start + 3]["input"]
        assert len(_outputs(calls[-1])) == 2
        snapshot = outcome.snapshots[-1]
        assert snapshot["quota_exhausted_models"] == [{"provider_id": "a", "model": "gpt-a"}]
        skipped = [item for item in snapshot["fallback_attempts"] if item["status"] == "skipped"]
        assert [item["request_round"] for item in skipped] == [2, 3]
        assert all(item["reason"] == "quota_exhausted" for item in skipped)
        assert len([event for event, payload in outcome.logs if event == "model.quota_exhausted"]) == 1
        assert "sk-test-secret-value" not in json.dumps(outcome.logs)
        assert "sk-test-secret-value" not in json.dumps(outcome.snapshots)
        assert outcome.snapshots[0]["quota_exhausted_models"] == []
    assert harness.tool_calls == [0, 1, 0, 1]


@pytest.mark.parametrize("chain", [
    (("a", "gpt-same"), ("b", "gpt-same")),
    (("a", "gpt-first"), ("a", "gpt-second")),
])
async def test_quota_skip_is_scoped_to_provider_and_model(fallback_harness, chain):
    """同名跨 Provider 与同 Provider 不同模型都不能被连带屏蔽。"""

    calls = []

    async def handler(request):
        body = json.loads(request.content)
        identity = (request.url.host.split(".")[0], body["model"])
        calls.append(identity)
        if identity == chain[0]:
            return _quota_error()
        return _completed() if _outputs(body) else _tool_response(0)

    outcome = await fallback_harness(handler, chain=chain).run()
    assert outcome.result.status == "completed"
    assert calls == [chain[0], chain[1], chain[1]]


async def test_all_quota_exhausted_candidates_stop_once(fallback_harness):
    """全部额度耗尽时有界失败，不重复尝试主模型。"""

    calls = []

    async def handler(request):
        calls.append(json.loads(request.content)["model"])
        return _quota_error()

    harness = fallback_harness(handler)
    outcome = await harness.run()
    assert outcome.result.status == "failed"
    assert calls == ["gpt-a", "gpt-b", "gpt-c"]
    assert len(outcome.snapshots[-1]["quota_exhausted_models"]) == 3
    assert harness.tool_calls == []


async def test_effort_downgrade_survives_switching_away_and_back(fallback_harness):
    """恢复主模型时不能重试它已拒绝的 effort，也不能污染备用模型参数。"""

    calls = []

    async def handler(request):
        body = json.loads(request.content)
        effort = body.get("reasoning", {}).get("effort")
        calls.append((body["model"], effort))
        if body["model"] == "gpt-a":
            if effort is not None:
                return httpx.Response(400, json={"error": {"param": "reasoning.effort", "code": "unsupported_value"}})
            if not _outputs(body):
                return httpx.Response(503, json={"error": {"code": "server_error"}})
            return _completed()
        return _tool_response(0)

    outcome = await fallback_harness(handler, effort="low").run()
    assert outcome.result.status == "completed"
    assert calls == [("gpt-a", "low"), ("gpt-a", None), ("gpt-b", "low"), ("gpt-a", None)]
    assert outcome.snapshots[-1]["configured_reasoning_effort"] == "low"
    assert outcome.snapshots[-1]["reasoning_effort"] is None
    assert outcome.snapshots[-1]["reasoning_effort_source"] == "compatibility_downgrade"
