"""推理强度拒绝识别、逐级降级及模型循环兼容性测试。"""

from __future__ import annotations

import asyncio
import json
from functools import partial
from types import SimpleNamespace

import httpx
import pytest

from teamwork_review_agents.codex_model_client import (
    CodexResponsesClient, CodexUpstreamError,
)
from teamwork_review_agents.codex_model_runner import CodexModelRunner
from teamwork_review_agents.config import ModelProviderConfig, ModelSelectionConfig
from teamwork_review_agents.environment import SecretRedactor
from teamwork_review_agents.model_provider_client import ExternalModelClient
from teamwork_review_agents.model_provider_credentials import ModelProviderCredentialStore
from teamwork_review_agents.model_provider_runtime import resolve_model_plan
from teamwork_review_agents.model_tools import ModelToolExecutor
from teamwork_review_agents.reasoning_effort import (
    is_reasoning_effort_rejection, next_reasoning_effort,
)


@pytest.mark.parametrize("current,expected", [
    ("ultra", "max"), ("max", "xhigh"), ("xhigh", "high"),
    ("high", "medium"), ("medium", "low"), ("low", None),
    ("minimal", None), ("custom-effort", None),
])
def test_next_effort_never_includes_minimal(current, expected):
    """兼容链严格递减，历史未知值不猜测其强度。"""

    assert next_reasoning_effort(current) == expected


@pytest.mark.parametrize("fields,status,expected", [
    ({"param": "reasoning.effort", "code": "unsupported_value"}, 400, True),
    ({"param": "reasoning_effort", "code": "invalid_enum_value"}, 422, True),
    ({"param": "reasoning", "code": "unsupported_parameter"}, 400, True),
    ({"message": "Unsupported parameter: reasoning_effort"}, 400, True),
    ({"message": "Unsupported parameter: 'reasoning'"}, 400, True),
    ({"param": "reasoning", "message": "This parameter is not supported"}, 400, True),
    ({"message": "reasoning.effort must be one of high, medium, low"}, None, True),
    ({"param": "reasoning.effort", "message": "不支持此值"}, 400, True),
    ({"param": "tools", "message": "Unsupported effort in tool schema"}, 400, False),
    ({"param": "reasoning.summary", "code": "unsupported_value"}, 400, False),
    ({"param": "reasoning", "code": "unsupported_parameter", "message": "summary unsupported"}, 400, False),
    ({"param": "reasoning", "message": "Unsupported summary while effort is high"}, 400, False),
    ({"message": "Unsupported reasoning.summary while effort is high"}, 400, False),
    ({"message": "Bad request"}, 400, False),
    ({"param": "reasoning.effort", "message": "must be a string"}, 400, False),
    ({"param": "reasoning.effort", "type": "server_error", "message": "not supported"}, None, False),
    *[({"param": "reasoning.effort", "code": "unsupported_value"}, status, False)
      for status in (401, 403, 404, 429, 500, 503)],
])
def test_only_explicit_effort_rejections_are_downgraded(fields, status, expected):
    """非兼容性错误及其他参数错误必须保持原有处理。"""

    assert is_reasoning_effort_rejection(fields, status_code=status) is expected


def _rejection(param="reasoning.effort"):
    """构造含可脱敏测试凭据的上游参数拒绝。"""

    return httpx.Response(400, json={"error": {
        "param": param, "code": "unsupported_value",
        "message": "Unsupported value for effort. Bearer sk-test-secret-value",
        "type": "invalid_request_error",
    }})


def _completed(driver):
    """返回两类外部协议的正常完成响应。"""

    if driver == "openai_chat_completions":
        return httpx.Response(200, json={
            "id": "done", "choices": [{"message": {"role": "assistant", "content": "完成"}}],
        })
    return httpx.Response(200, json={"id": "done", "output": [{
        "type": "message", "content": [{"type": "output_text", "text": "完成"}],
    }]})


@pytest.fixture
def run_effort(configured_app_factory, monkeypatch):
    """以真实协议适配器和本地假传输驱动完整 Agent 循环。"""

    async def run(handler, *, effort="max", driver="openai_responses", fallback=False):
        config = configured_app_factory()
        for name in ("primary", "backup"):
            config.model_providers[name] = ModelProviderConfig(
                display_name=name, driver=driver,
                base_url=f"https://{name}.example.test",
                default_model=f"gpt-{name}",
                model_reasoning_effort=effort if name == "primary" else "xhigh",
            )
        config.runtime.default_model = ModelSelectionConfig(provider="primary")
        config.runtime.default_model_fallbacks = (
            [ModelSelectionConfig(provider="backup")] if fallback else []
        )
        agent = config.agents["code-reviewer"].model_copy(update={"sandbox": "danger-full-access"})
        credentials = ModelProviderCredentialStore(config.database.path.parent / "model-provider-credentials")
        for name in ("primary", "backup"):
            credentials.replace(name, "test-key")
        monkeypatch.setattr(
            "teamwork_review_agents.codex_model_runner.ExternalModelClient",
            partial(ExternalModelClient, transport=httpx.MockTransport(handler)),
        )
        snapshots, logs = [], []

        async def snapshot_callback(snapshot):
            snapshots.append(snapshot)

        async def log_callback(stream, event_type, payload):
            logs.append((event_type, payload))

        result = await CodexModelRunner(config, provider_id="primary").run(
            run_id="effort-run", root_run_id="effort-run", parent_run_id=None,
            agent_name="code-reviewer", agent=agent, repository=config.repositories[0],
            context=None, prompt="测试推理强度", process_environment={},
            redactor=SecretRedactor(()), model_plan=resolve_model_plan(config, agent).selections,
            model_snapshot_callback=snapshot_callback, log_callback=log_callback,
        )
        return SimpleNamespace(result=result, snapshots=snapshots, logs=logs, config=config)

    return run


@pytest.mark.parametrize("driver", ["openai_responses", "openai_chat_completions"])
@pytest.mark.parametrize("accepted", ["max", "xhigh", "high", "medium", "low", None])
async def test_effort_chain_until_accepted(run_effort, driver, accepted):
    """首次成功即停止降级，最低失败后省略字段且保留真实原因。"""

    efforts = []

    async def handler(request):
        body = json.loads(request.content)
        effort = body.get("reasoning", {}).get("effort") if driver == "openai_responses" else body.get("reasoning_effort")
        efforts.append(effort)
        if effort != accepted:
            return _rejection("reasoning.effort" if driver == "openai_responses" else "reasoning_effort")
        if effort is None:
            assert "reasoning" not in body
            assert "reasoning_effort" not in body
        return _completed(driver)

    outcome = await run_effort(handler, driver=driver)
    chain = ["max", "xhigh", "high", "medium", "low", None]
    assert efforts == chain[:chain.index(accepted) + 1]
    assert outcome.result.status == "completed"
    snapshot = outcome.snapshots[-1]
    assert snapshot["reasoning_effort"] == accepted
    assert snapshot["configured_reasoning_effort"] == "max"
    assert snapshot["fallback_used"] is False
    assert len(snapshot["reasoning_downgrades"]) == len(efforts) - 1
    assert outcome.config.model_providers["primary"].model_reasoning_effort == "max"
    assert "sk-test-secret-value" not in json.dumps(outcome.logs)
    assert "sk-test-secret-value" not in json.dumps(outcome.snapshots)
    if accepted != "max":
        assert snapshot["reasoning_effort_source"] == "compatibility_downgrade"
        assert "Unsupported value" in snapshot["reasoning_downgrades"][0]["reason"]


@pytest.mark.parametrize("after_removal", [400, 503])
async def test_no_effort_failure_obeys_existing_fallback(run_effort, after_removal):
    """无参数请求只尝试一次；仅原有可回退错误进入新模型自身配置。"""

    calls = []

    async def handler(request):
        effort = json.loads(request.content).get("reasoning", {}).get("effort")
        calls.append((request.url.host, effort))
        if request.url.host == "backup.example.test":
            return _completed("openai_responses")
        if effort is not None:
            return _rejection()
        return httpx.Response(after_removal, json={"error": {"message": "仍然失败"}})

    outcome = await run_effort(handler, fallback=True)
    assert calls[:6] == [("primary.example.test", value) for value in ("max", "xhigh", "high", "medium", "low", None)]
    if after_removal == 400:
        assert len(calls) == 6
        assert outcome.result.status == "failed"
        assert outcome.snapshots[-1]["fallback_used"] is False
    else:
        assert calls[6:] == [("backup.example.test", "xhigh")]
        assert outcome.result.status == "completed"
        assert outcome.snapshots[-1]["configured_reasoning_effort"] == "xhigh"
        assert outcome.snapshots[-1]["reasoning_effort"] == "xhigh"
        assert outcome.snapshots[-1]["fallback_used"] is True


@pytest.mark.parametrize("status,param", [(400, "tools"), (400, "reasoning.summary"), (401, "reasoning.effort"), (429, "reasoning.effort"), (500, "reasoning.effort")])
async def test_non_effort_errors_do_not_retry_effort(run_effort, status, param):
    """普通参数错误、鉴权、限流和服务错误均不得重试低档 effort。"""

    calls = []

    async def handler(request):
        calls.append(json.loads(request.content)["reasoning"]["effort"])
        return httpx.Response(status, json={"error": {"param": param, "code": "unsupported_value"}})

    outcome = await run_effort(handler)
    assert calls == ["max"]
    assert outcome.result.status == "failed"
    assert outcome.snapshots[-1]["reasoning_downgrades"] == []


@pytest.mark.parametrize("accepted", ["high", None])
async def test_downgrade_reuses_history_and_does_not_repeat_tools(run_effort, monkeypatch, accepted):
    """第二轮才降级也必须保留工具结果，后续回合继续使用成功档位。"""

    calls, tool_calls = [], []

    async def execute(self, name, arguments, **kwargs):
        tool_calls.append(arguments["index"])
        return {"ok": True}

    monkeypatch.setattr(ModelToolExecutor, "execute", execute)

    async def handler(request):
        body = json.loads(request.content)
        calls.append(body)
        effort = body.get("reasoning", {}).get("effort")
        completed_tools = [item for item in body["input"] if item.get("type") == "function_call_output"]
        if completed_tools and effort != accepted:
            return _rejection()
        if len(completed_tools) == 2:
            return _completed("openai_responses")
        index = len(completed_tools)
        return httpx.Response(200, json={"output": [{
            "type": "function_call", "call_id": f"call-{index}", "name": "exec_command",
            "arguments": json.dumps({"index": index}),
        }]})

    outcome = await run_effort(handler)
    assert outcome.result.status == "completed"
    expected = ["max", "max", "xhigh", "high", "high"] if accepted else ["max", "max", "xhigh", "high", "medium", "low", None, None]
    assert [body.get("reasoning", {}).get("effort") for body in calls] == expected
    assert all(body["input"] == calls[1]["input"] for body in calls[1:-1])
    assert tool_calls == [0, 1]


async def test_cancellation_after_downgrade_is_not_retried(run_effort):
    """已降级的请求被取消后不得继续尝试更低档位。"""

    calls = []

    async def handler(request):
        calls.append(json.loads(request.content)["reasoning"]["effort"])
        if len(calls) == 1:
            return _rejection()
        raise asyncio.CancelledError()

    outcome = await run_effort(handler)
    assert calls == ["max", "xhigh"]
    assert outcome.result.status == "cancelled"


@pytest.mark.parametrize("effort", [None, "low"])
async def test_rejection_after_omission_does_not_loop(run_effort, effort):
    """即使省略参数后仍收到同样的拒绝，也不得反复重试无参数请求。"""

    calls = []

    async def handler(request):
        calls.append(json.loads(request.content).get("reasoning", {}).get("effort"))
        return _rejection()

    outcome = await run_effort(handler, effort=effort)
    assert calls == ([None] if effort is None else ["low", None])
    assert outcome.result.status == "failed"


@pytest.mark.parametrize("mode", ["http", "sse", "json"])
async def test_codex_client_marks_effort_rejection(mode, monkeypatch):
    """内置模型客户端的 HTTP、SSE 和兼容 JSON 错误采用相同判定。"""

    class OAuth:
        """避免测试依赖宿主登录状态。"""

        async def credentials(self):
            return object()

    monkeypatch.setattr("teamwork_review_agents.codex_model_client._codex_headers", lambda *args, **kwargs: {})
    error = {"error": {"code": "unsupported_value", "param": "reasoning.effort", "message": "Unsupported value"}}

    async def handler(request):
        if mode == "http":
            return httpx.Response(400, json=error)
        if mode == "json":
            return httpx.Response(200, json=error)
        event = {"type": "response.failed", "response": {"status": "failed", **error}}
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=f"data: {json.dumps(event)}\n\n")

    client = CodexResponsesClient(
        oauth=OAuth(), codex_binary="unused-test-codex",
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(CodexUpstreamError) as raised:
        await client.create_response({"model": "gpt-test"})
    assert raised.value.reasoning_effort_rejected is True
