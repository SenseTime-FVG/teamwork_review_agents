"""交互轮数配置、剩余预算提醒和耗尽后停止自动重试的回归。"""

from __future__ import annotations

import json
from functools import partial
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from pydantic import ValidationError

from teamwork_review_agents.codex_model_runner import CodexModelRunner
from teamwork_review_agents.config import AgentConfig, ContextCompactionConfig, ModelProviderConfig, ModelSelectionConfig, RuleConfig, RuntimeConfig, load_config
from teamwork_review_agents.config_manager import ConfigManager
from teamwork_review_agents.context_compaction import ConversationContext
from teamwork_review_agents.environment import SecretRedactor
from teamwork_review_agents.events import detect_events
from teamwork_review_agents.model_provider_client import ExternalModelClient
from teamwork_review_agents.model_provider_credentials import ModelProviderCredentialStore
from teamwork_review_agents.model_provider_runtime import resolve_model_plan
from teamwork_review_agents.model_tools import ModelToolExecutor
from teamwork_review_agents.orchestrator import CycleSummary, Orchestrator
from teamwork_review_agents.run_control import RunStop, active_run_control
from teamwork_review_agents.state import StateStore


def test_round_limit_defaults_and_configuration_roundtrip(configured_app_factory):
    """旧配置默认 256；全局和单 Agent 保存后均可重新加载并恢复继承。"""

    config = configured_app_factory()
    assert config.runtime.max_tool_rounds == 256
    assert config.agents["code-reviewer"].max_tool_rounds is None
    manager = ConfigManager(config.config_path)
    document = manager.document()
    # 管理接口保留原始 YAML，旧文件未写新字段时，表单及运行时各自展示默认值。
    assert document["runtime"].get("max_tool_rounds", 256) == 256
    document["runtime"]["max_tool_rounds"] = 128
    document["agents"]["code-reviewer"]["max_tool_rounds"] = 64
    manager.save(document)
    loaded = load_config(config.config_path)
    assert loaded.runtime.max_tool_rounds == 128
    assert loaded.agents["code-reviewer"].max_tool_rounds == 64
    agent = manager.document()["agents"]["code-reviewer"]
    agent["max_tool_rounds"] = None
    manager.save_agent(expected_revision=manager.config.revision, original_name="code-reviewer", name="code-reviewer", agent=agent)
    assert load_config(config.config_path).agents["code-reviewer"].max_tool_rounds is None
    assert manager.document()["runtime"]["max_tool_rounds"] == 128
    # UI 清空数字后会省略字段，单 Agent 保存不能把旧覆盖值再合并回来。
    agent["max_tool_rounds"] = 8
    manager.save_agent(expected_revision=manager.config.revision, original_name="code-reviewer", name="code-reviewer", agent=agent)
    agent.pop("max_tool_rounds")
    manager.save_agent(expected_revision=manager.config.revision, original_name="code-reviewer", name="code-reviewer", agent=agent)
    assert load_config(config.config_path).agents["code-reviewer"].max_tool_rounds is None


@pytest.mark.parametrize("value", [0, -1, True, False, "256", 1.5])
@pytest.mark.parametrize("model", [RuntimeConfig, AgentConfig])
def test_round_limit_rejects_non_positive_or_non_integer_values(model, value):
    """轮数必须是严格正整数，不能把布尔值、字符串或小数当成预算。"""

    with pytest.raises(ValidationError) as raised:
        model(max_tool_rounds=value, **({"prompt": "测试"} if model is AgentConfig else {}))
    assert any(error["loc"] == ("max_tool_rounds",) for error in raised.value.errors())


def test_only_agent_limit_can_be_null():
    """Agent 可用空值继承，但全局总要有一个明确的有限预算。"""

    assert AgentConfig(prompt="测试", max_tool_rounds=None).max_tool_rounds is None
    with pytest.raises(ValidationError):
        RuntimeConfig(max_tool_rounds=None)


def _response(index, *, completed=False, tool_count=1):
    """构造计数明确的正常响应，不访问真实模型或执行真实工具。"""

    output = [{"type": "message", "content": [{"type": "output_text", "text": "任务完成"}]}] if completed else [
        {"type": "function_call", "call_id": f"call-{index}-{offset}", "name": "execute_command", "arguments": json.dumps({"index": index, "offset": offset})}
        for offset in range(tool_count)
    ]
    return httpx.Response(200, json={"id": f"response-{index}", "output": output, "usage": {"input_tokens": 2, "output_tokens": 1}})


@pytest.fixture
def round_harness(configured_app_factory, monkeypatch):
    """保留真实运行器与协议客户端，只替换网络、工具和不相关的大型指令。"""

    def create(handler, *, global_limit=None, agent_limit=None, fallback=False, on_tool=None, output_size=0):
        config = configured_app_factory()
        if global_limit is not None:
            config.runtime.max_tool_rounds = global_limit
        credentials = ModelProviderCredentialStore(config.database.path.parent / "model-provider-credentials")
        for provider_id in ("a", "b"):
            config.model_providers[provider_id] = ModelProviderConfig(display_name=provider_id, driver="openai_responses", base_url=f"https://{provider_id}.example.test", default_model=f"gpt-{provider_id}")
            credentials.replace(provider_id, "test-key")
        config.runtime.default_model = ModelSelectionConfig(provider="a", model="gpt-a")
        config.runtime.default_model_fallbacks = [ModelSelectionConfig(provider="b", model="gpt-b")] if fallback else []
        agent = config.agents["code-reviewer"].model_copy(update={"sandbox": "danger-full-access", "max_tool_rounds": agent_limit})
        monkeypatch.setattr("teamwork_review_agents.codex_model_runner.ExternalModelClient", partial(ExternalModelClient, transport=httpx.MockTransport(handler)))
        monkeypatch.setattr("teamwork_review_agents.codex_model_runner._instructions", lambda **kwargs: "原始系统约束：完成验证才能结论通过。")
        monkeypatch.setattr("teamwork_review_agents.codex_model_runner.teamwork_function_tools", lambda **kwargs: [{"type": "function", "name": "execute_command", "parameters": {"type": "object"}}])
        tool_calls = []

        async def execute(self, name, arguments, **kwargs):
            """记录执行次数，允许用回调模拟配置热更新或已确定的取消。"""

            tool_calls.append(arguments)
            if on_tool:
                on_tool(config)
            return {"recorded": arguments, "stdout": "测试工具结果" + "x" * output_size}

        monkeypatch.setattr(ModelToolExecutor, "execute", execute)
        runner = CodexModelRunner(config, provider_id="a")
        run_count = 0

        async def run(*, parent_run_id=None):
            """每次根或子运行都建立自己的预算，记录日志和模型快照。"""

            nonlocal run_count
            run_count += 1
            run_id = f"round-test-{run_count}"
            logs, snapshots = [], []

            async def log(stream, event_type, payload):
                logs.append((event_type, payload))

            async def snapshot(value):
                snapshots.append(value)

            result = await runner.run(run_id=run_id, root_run_id=parent_run_id or run_id, parent_run_id=parent_run_id, agent_name="code-reviewer", agent=agent, repository=config.repositories[0], context=None, prompt="原始任务：完成全部必要检查。", process_environment={}, redactor=SecretRedactor(()), model_plan=resolve_model_plan(config, agent).selections, log_callback=log, model_snapshot_callback=snapshot)
            return SimpleNamespace(result=result, logs=logs, snapshots=snapshots)

        return SimpleNamespace(run=run, config=config, agent=agent, tool_calls=tool_calls)

    return create


async def test_default_256_allows_completion_on_last_round_and_warns_once(round_harness):
    """超过旧 64 轮仍可继续，第 256 轮最终回答算完成；提醒不伪造历史消息。"""

    requests = []

    async def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        return _response(len(requests), completed=len(requests) == 256)

    harness = round_harness(handler)
    outcome = await harness.run()
    assert outcome.result.status == "completed", outcome.result.error
    assert len(requests) == 256 and len(harness.tool_calls) == 255
    warnings = [payload for kind, payload in outcome.logs if kind == "run.tool_round_limit_warning"]
    assert len(warnings) == 1
    assert warnings[0]["request_round"] == 225 and warnings[0]["remaining_rounds"] == 32
    assert all("运行器轮数提醒" not in body["instructions"] for body in requests[:224])
    for index, body in enumerate(requests[224:], 225):
        assert f"含当前请求还剩 {257 - index} 轮" in body["instructions"]
        assert "不要为了收尾而省略验证" in body["instructions"]
        assert body["input"][0]["content"][0]["text"] == "原始任务：完成全部必要检查。"
        assert not any(item.get("role") == "user" for item in body["input"][1:])
    assert outcome.snapshots[-1]["max_tool_rounds"] == 256
    assert outcome.snapshots[-1]["tool_round_limit_source"] == "runtime"
    assert not any(kind == "run.tool_round_limit_reached" for kind, _ in outcome.logs)


@pytest.mark.parametrize("parent_run_id", [None, "parent-root"])
async def test_agent_override_and_reset_to_inheritance_get_independent_budgets(round_harness, parent_run_id):
    """根、子 Agent 均优先使用自身预算，清空覆盖后重新继承全局。"""

    requests = []

    async def handler(request):
        requests.append(json.loads(request.content))
        return _response(len(requests))

    harness = round_harness(handler, global_limit=2, agent_limit=1)
    first = await harness.run(parent_run_id=parent_run_id)
    assert len(requests) == 1
    assert first.snapshots[-1]["max_tool_rounds"] == 1
    assert first.snapshots[-1]["tool_round_limit_source"] == "agent"
    harness.agent.max_tool_rounds = None
    second = await harness.run(parent_run_id=parent_run_id)
    assert len(requests) == 3
    assert second.snapshots[-1]["request_round"] == 2
    assert second.snapshots[-1]["max_tool_rounds"] == 2
    assert second.snapshots[-1]["tool_round_limit_source"] == "runtime"
    assert first.result.root_run_id == (parent_run_id or first.result.run_id)
    assert second.result.retryable is False


async def test_exhaustion_preserves_usage_and_multiple_tools_share_one_round(round_harness):
    """同轮两工具不额外消耗预算，耗尽保留用量、身份和执行日志且不报成功。"""

    requests = []

    async def handler(request):
        requests.append(json.loads(request.content))
        return _response(len(requests), tool_count=2)

    harness = round_harness(handler, global_limit=3)
    outcome = await harness.run()
    result = outcome.result
    assert len(requests) == 3 and len(harness.tool_calls) == 6
    assert result.status == "failed" and result.final_message == ""
    assert result.error_code == "agent_tool_round_limit" and result.retryable is False
    assert "3 轮上限，任务未完成" in result.error
    assert result.thread_id == "response-3"
    assert result.usage == {"input_tokens": 6, "output_tokens": 3}
    assert len([event for event in result.events if event.get("type") == "item.completed" and event.get("item", {}).get("type") == "command_execution"]) == 6
    warning = next(payload for kind, payload in outcome.logs if kind == "run.tool_round_limit_warning")
    assert warning["request_round"] == 1 and warning["remaining_rounds"] == 3
    reached = next(payload for kind, payload in outcome.logs if kind == "run.tool_round_limit_reached")
    assert reached["remaining_rounds"] == 0 and reached["retryable"] is False


async def test_configuration_reload_does_not_change_active_run_budget(round_harness):
    """执行期间热更新只影响新运行，不能延长本轮已确定的预算。"""

    requests = []

    async def handler(request):
        requests.append(request)
        return _response(len(requests))

    def reload_config(config):
        config.runtime.max_tool_rounds = 99

    harness = round_harness(handler, global_limit=2, on_tool=reload_config)
    outcome = await harness.run()
    assert len(requests) == 2 and harness.config.runtime.max_tool_rounds == 99
    assert outcome.snapshots[-1]["max_tool_rounds"] == 2


async def test_fallback_attempts_do_not_reset_or_extra_charge_round_budget(round_harness):
    """第一次 A 额度耗尽回退 B，后续跳过 A，但每次正常循环仍只计一轮。"""

    hosts = []

    async def handler(request):
        hosts.append(request.url.host)
        if request.url.host == "a.example.test":
            return httpx.Response(429, json={"error": {"code": "insufficient_quota", "message": "测试额度耗尽"}})
        return _response(len(hosts))

    harness = round_harness(handler, global_limit=2, fallback=True)
    outcome = await harness.run()
    assert hosts == ["a.example.test", "b.example.test", "b.example.test"]
    assert len(harness.tool_calls) == 2
    assert [payload["request_round"] for kind, payload in outcome.logs if kind == "model.request_started"] == [1, 2]
    assert outcome.result.error_code == "agent_tool_round_limit"
    assert outcome.snapshots[-1]["request_round"] == 2


async def test_compaction_summary_does_not_consume_or_reset_round_budget(round_harness, monkeypatch):
    """真实压缩副本另发摘要请求，成功替换历史也不能把任务计数归零。"""

    normal, summaries = [], []

    async def handler(request):
        body = json.loads(request.content)
        if body["tool_choice"] == "none":
            summaries.append(body)
            return _response(f"summary-{len(summaries)}", completed=True)
        normal.append(body)
        return _response(len(normal))

    original_ensure = ConversationContext.ensure_budget

    async def force_after_tool(self, **kwargs):
        """只触发压缩入口，摘要构建、验收与历史提交仍使用真实实现。"""

        kwargs["force"] = bool(self.rounds)
        return await original_ensure(self, **kwargs)

    monkeypatch.setattr(ConversationContext, "ensure_budget", force_after_tool)
    harness = round_harness(handler, global_limit=4, output_size=1000)
    harness.config.runtime.context_compaction = ContextCompactionConfig(keep_recent_rounds=0)
    outcome = await harness.run()
    assert len(normal) == 4 and len(summaries) == 3
    assert len(harness.tool_calls) == 4
    assert outcome.result.error_code == "agent_tool_round_limit"
    assert outcome.result.usage == {"input_tokens": 14, "output_tokens": 7}
    assert len(outcome.snapshots[-1]["context_compactions"]) == 3
    assert outcome.snapshots[-1]["request_round"] == 4
    assert outcome.snapshots[-1]["max_tool_rounds"] == 4


@pytest.mark.parametrize("status,error_code", [("cancelled", "administrator_cancelled"), ("timed_out", "agent_timeout")])
async def test_stop_on_last_tool_has_priority_over_round_exhaustion(round_harness, status, error_code):
    """最后一个工具返回时已确定的取消或超时原因不能被轮数耗尽覆盖。"""

    async def handler(request):
        return _response(1)

    def stop(config):
        active_run_control.get().stop = RunStop(status, error_code, "测试终止原因")

    outcome = await round_harness(handler, global_limit=1, on_tool=stop).run()
    assert outcome.result.status == status
    assert outcome.result.error_code == error_code
    assert not any(kind == "run.tool_round_limit_reached" for kind, _ in outcome.logs)


async def test_limit_failure_is_persisted_and_excluded_from_automatic_retries(round_harness, snapshot_factory, monkeypatch):
    """真实终态经过状态表和事件调度器后，两层入口都拒绝从头自动重试。"""

    async def handler(request):
        return _response(1)

    harness = round_harness(handler, global_limit=1)
    outcome = await harness.run()
    config = harness.config
    store = StateStore(config.database.path)
    store.initialize()
    snapshot = snapshot_factory(provider="github-main")
    event = detect_events(None, snapshot, emit_initial=True)[0]
    store.save_snapshot_and_events(snapshot, [event])
    arguments = dict(proposed_run_id=outcome.result.run_id, root_run_id=None, parent_run_id=None, idempotency_key="round-limit-test", event_id=event.id, rule_name="test-review", agent_name="code-reviewer", resource_key=event.resource_key, prompt="测试轮数", max_attempts=3)
    assert store.begin_agent_run(**arguments) is not None
    store.finish_agent_run(outcome.result)
    assert store.begin_agent_run(**arguments) is None
    assert store.get_run(outcome.result.run_id)["attempts"] == 1
    assert store.agent_run_failure("round-limit-test")["error_code"] == "agent_tool_round_limit"

    config.rules = [RuleConfig(name="test-review", events=[event.type], agents=["code-reviewer"])]
    orchestrator = Orchestrator(config, recover_interrupted=False)
    execute = AsyncMock(return_value=outcome.result)
    monkeypatch.setattr(orchestrator.executor, "execute", execute)
    for _ in range(3):
        await orchestrator.process_events(CycleSummary())
    execute.assert_awaited_once()
    record = store.get_event_detail(event.id)
    assert record["status"] == "failed" and record["attempts"] == 1
    assert record["error_code"] == "agent_tool_round_limit" and not record["retryable"]
    assert store.pending_events() == []
    assert not store.claim_event(event.id, 3)
