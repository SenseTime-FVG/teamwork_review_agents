"""验证固定指令不变、完整回合压缩、预算及失败原子性。"""

from __future__ import annotations

import asyncio
import copy
import json
from functools import partial
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from pydantic import ValidationError

from teamwork_review_agents.config import ContextCompactionConfig, ModelProviderConfig, ModelSelectionConfig
from teamwork_review_agents.context_compaction import (
    ConversationContext, ContextCompactionError, SUMMARY_INSTRUCTIONS, SUMMARY_PREFIX,
    estimate_tokens, resolve_context_window, summary_payload,
)
from teamwork_review_agents.codex_model_client import CodexResponsesClient, CodexUpstreamError
from teamwork_review_agents.codex_model_runner import CodexModelRunner, _instructions
from teamwork_review_agents.environment import SecretRedactor
from teamwork_review_agents.model_provider_client import ExternalModelClient, ModelProviderRequestError
from teamwork_review_agents.model_provider_credentials import ModelProviderCredentialStore
from teamwork_review_agents.model_provider_runtime import resolve_model_plan
from teamwork_review_agents.model_tools import ModelToolExecutor, teamwork_function_tools


def _settings(**overrides):
    """小窗口让测试只使用少量数据即可触发压缩。"""

    return ContextCompactionConfig(**{
        "default_context_window_tokens": 8192, "reserved_output_tokens": 512,
        "max_summary_tokens": 512, "tool_output_tokens": 4096,
        "trigger_ratio": 0.7, "target_ratio": 0.4, "keep_recent_rounds": 1,
        **overrides,
    })


def _turn(index, size=2000):
    """完整工具回合，保留可验证的身份和输出标记。"""

    return [
        {"type": "function_call", "call_id": f"call-{index}", "name": "execute_command", "arguments": json.dumps({"index": index})},
        {"type": "function_call_output", "call_id": f"call-{index}", "output": f"记录{index}:" + "x" * size},
    ]


def _context(settings=None):
    """固定原始任务与系统边界分别存储，摘要不得替换任何一个。"""

    return ConversationContext([{"role": "user", "content": "原始任务：保留 SHA 与禁止合并约束"}], settings or _settings())


async def test_compaction_preserves_fixed_instructions_and_complete_recent_round():
    """摘要内容只能进入 assistant 历史，系统、工具及原始任务逐字保持。"""

    context = _context(_settings(target_ratio=0.5))
    fields = {"instructions": "SYSTEM 原文\n权限限制\nSKILL 全文", "tools": [{"name": "原始工具"}], "text": {"format": {"type": "json_schema"}}}
    original_fields = copy.deepcopy(fields)
    original_fixed = copy.deepcopy(context.fixed_messages)
    for index in range(4):
        context.append_round(_turn(index))
    requests = []

    async def summarize(payload):
        requests.append(payload)
        assert payload["tools"] == [] and payload["tool_choice"] == "none"
        assert "text" not in payload
        assert "SYSTEM 原文" not in json.dumps(payload, ensure_ascii=False)
        assert "原始任务：保留 SHA" not in json.dumps(payload, ensure_ascii=False)
        return "进展：已完成历史操作；SHA abc123；约束：禁止合并；待办：继续检查。"

    result = await context.ensure_budget(model="test", request_fields=fields, window=8192, summarize=summarize)
    assert result["after_estimated_tokens"] < result["before_estimated_tokens"]
    assert fields == original_fields
    assert context.history()[0] == original_fixed[0]
    assert context.history()[1]["role"] == "assistant"
    assert context.history()[1]["content"][0]["text"].startswith(SUMMARY_PREFIX)
    assert context.rounds == [_turn(3)]
    assert requests


@pytest.mark.parametrize("failure", ["empty", "large", "exception", "cancel", "nonshrinking"])
async def test_failed_compaction_never_replaces_history(failure):
    """空摘要、超预算、异常或取消都保留原始活跃历史。"""

    context = _context()
    context.append_round(_turn(0, 9000 if failure != "nonshrinking" else 1))
    before = copy.deepcopy(context.history())

    async def summarize(payload):
        if failure == "exception":
            raise RuntimeError("模拟上游失败")
        if failure == "cancel":
            raise asyncio.CancelledError()
        return "" if failure == "empty" else "x" * (513 if failure == "large" else 512)

    expected = asyncio.CancelledError if failure == "cancel" else RuntimeError
    with pytest.raises(expected):
        await context.ensure_budget(model="test", request_fields={}, window=8192, summarize=summarize, force=True)
    assert context.history() == before
    assert context.compaction_count == 0


async def test_summary_fragments_fit_budget_and_failure_is_atomic():
    """超大片段被有界分片，后续分片失败不能提交前面的半份摘要。"""

    context = _context()
    context.append_round(_turn(0, 18000))
    original = copy.deepcopy(context.history())
    requests = []

    async def summarize(payload):
        requests.append(payload)
        assert estimate_tokens(payload) + 256 <= 8192 - 512
        if len(requests) == 2:
            source = json.loads(payload["input"][0]["content"][0]["text"])
            assert source["previous_summary"] == "已记录首段事实"
            raise RuntimeError("第二片段失败")
        return "已记录首段事实"

    with pytest.raises(RuntimeError, match="第二片段失败"):
        await context.ensure_budget(model="test", request_fields={}, window=8192, summarize=summarize)
    assert len(requests) == 2
    assert context.history() == original


async def test_safe_short_history_is_kept_when_summary_is_larger():
    """固定指令接近阈值但请求仍安全时，不因短历史摘要膨胀而终止。"""

    context = _context()
    context.append_round(_turn(0, 1))
    before = context.history()

    async def summarize(payload):
        return "x" * 400

    result = await context.ensure_budget(
        model="test", request_fields={"instructions": "x" * 5300},
        window=8192, summarize=summarize,
    )
    assert result is None and context.history() == before
    assert context.compaction_count == 0


async def test_summary_call_limit_and_fixed_content_failure():
    """不能为了压缩继续运行而删掉系统约束，也不能无限调用摘要模型。"""

    context = _context(_settings(max_compaction_requests=1))
    context.append_round(_turn(0, 20000))
    calls = []

    async def summarize(payload):
        calls.append(payload)
        return "已记录"

    with pytest.raises(ContextCompactionError, match="系统指令"):
        await context.ensure_budget(model="test", request_fields={"instructions": "x" * 10000}, window=8192, summarize=summarize)
    assert calls == []
    with pytest.raises(ContextCompactionError, match="本次上限"):
        await context.ensure_budget(model="test", request_fields={}, window=8192, summarize=summarize)
    assert len(calls) == 1
    assert len(context.rounds) == 1


def test_tool_pairing_and_output_budget():
    """不拆散工具配对；送模结果带截短标记和退出码，原始内容不改变。"""

    context = _context()
    with pytest.raises(ContextCompactionError, match="配对"):
        context.append_round(_turn(0)[:1])
    raw = json.dumps({"exit_code": 1, "stdout": "首" + '汉字"\\' * 10000 + "尾"}, ensure_ascii=False)
    bounded = context.bound_tool_output(raw, "call-0")
    assert len(bounded.encode("utf-8")) <= context.settings.tool_output_tokens
    result = json.loads(bounded)
    assert result["truncated_for_context"] is True
    assert result["exit_code"] == 1
    assert "首" in result["head"] and "尾" in result["tail"]
    disabled = _context(_settings(enabled=False))
    assert disabled.bound_tool_output(raw, "call-0") == raw


@pytest.mark.parametrize("overrides", [
    {"target_ratio": 0.8}, {"reserved_output_tokens": 8000},
    {"model_context_windows": {"a": {"small": 1000}}},
])
def test_invalid_compaction_budgets_rejected(overrides):
    """预算配置错误必须在保存时拒绝。"""

    with pytest.raises(ValidationError):
        _settings(**overrides)


def test_model_window_resolution_is_scoped_and_has_safe_fallback(tmp_path):
    """覆盖按 Provider/模型匹配；Codex 缓存不可用时使用明确的默认预算。"""

    settings = _settings(model_context_windows={"a": {"same": 16000}})
    (tmp_path / "models_cache.json").write_text(json.dumps({"models": [{"slug": "same", "context_window": 24000}]}), encoding="utf-8")
    assert resolve_context_window(settings, "a", "same", driver="codex_cli", codex_home=tmp_path) == (16000, "configured")
    assert resolve_context_window(settings, "b", "same", driver="codex_cli", codex_home=tmp_path) == (24000, "codex_model_cache")
    assert resolve_context_window(settings, "b", "same", driver="openai_responses", codex_home=tmp_path) == (8192, "conservative_default")
    (tmp_path / "models_cache.json").write_text("invalid", encoding="utf-8")
    assert resolve_context_window(settings, "b", "same", driver="codex_cli", codex_home=tmp_path)[0] == 8192


async def test_builtin_prompts_fit_default_fixed_budget(configured_app_factory):
    """默认预算不能误拦截项目已有的长任务 Prompt。"""

    config = configured_app_factory()
    agent = config.agents["code-reviewer"]
    fields = {
        "instructions": _instructions(repository=config.repositories[0], agent=agent, personality=None, skill_files={}),
        "tools": teamwork_function_tools(allow_sub_agents=True, allow_publish_comment=True),
    }

    async def summarize(payload):
        pytest.fail("原始任务不得送入摘要")

    root = Path(__file__).resolve().parents[1]
    for name in ("general-review.md", "依赖review&增量文档更新 入口.md", "依赖review.md", "增量文档更新.md"):
        prompt = (root / "prompts" / name).read_text(encoding="utf-8")
        context = ConversationContext([{"role": "user", "content": prompt}], ContextCompactionConfig())
        assert await context.ensure_budget(model="test", request_fields=fields, window=131072, summarize=summarize) is None
        assert context.history()[0]["content"] == prompt


@pytest.fixture
def run_context(configured_app_factory, monkeypatch):
    """通过真实 HTTP 协议适配器验证运行器，禁止网络和真实工具执行。"""

    async def run(handler, *, settings=None, output_size=2400, fallback=False):
        config = configured_app_factory()
        config.runtime.context_compaction = settings or _settings()
        for name in ("a", "b"):
            config.model_providers[name] = ModelProviderConfig(display_name=name, driver="openai_responses", base_url=f"https://{name}.example.test", default_model=f"gpt-{name}")
        config.runtime.default_model = ModelSelectionConfig(provider="a", model="gpt-a")
        config.runtime.default_model_fallbacks = [ModelSelectionConfig(provider="b", model="gpt-b")] if fallback else []
        credentials = ModelProviderCredentialStore(config.database.path.parent / "model-provider-credentials")
        for name in ("a", "b"):
            credentials.replace(name, "test-secret")
        monkeypatch.setattr("teamwork_review_agents.codex_model_runner.ExternalModelClient", partial(ExternalModelClient, transport=httpx.MockTransport(handler)))
        monkeypatch.setattr("teamwork_review_agents.codex_model_runner._instructions", lambda **kwargs: "SYSTEM-ORIGINAL：权限约束\nSKILL-ORIGINAL：审核指令")
        tools = [{"type": "function", "name": "execute_command", "parameters": {"type": "object"}}]
        monkeypatch.setattr("teamwork_review_agents.codex_model_runner.teamwork_function_tools", lambda **kwargs: tools)
        calls, logs, snapshots = [], [], []

        async def execute(self, name, arguments, **kwargs):
            calls.append(arguments["index"])
            return {"exit_code": 0, "stdout": "结果" + "x" * output_size}

        async def log(stream, event_type, payload):
            logs.append((event_type, payload))

        async def snapshot(value):
            snapshots.append(value)

        monkeypatch.setattr(ModelToolExecutor, "execute", execute)
        agent = config.agents["code-reviewer"].model_copy(update={"sandbox": "danger-full-access"})
        result = await CodexModelRunner(config, provider_id="a").run(
            run_id="context-run", root_run_id="context-run", parent_run_id=None,
            agent_name="code-reviewer", agent=agent, repository=config.repositories[0], context=None,
            prompt="TASK-ORIGINAL：审核完成之前不得合并", process_environment={}, redactor=SecretRedactor(()),
            model_plan=resolve_model_plan(config, agent).selections, log_callback=log,
            model_snapshot_callback=snapshot,
        )
        return SimpleNamespace(result=result, calls=calls, logs=logs, snapshots=snapshots, tools=tools)

    return run


def _response(text):
    """规范完成响应，并记录测试用的 Token 用量。"""

    return httpx.Response(200, json={"output": [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": text}]}], "usage": {"input_tokens": 2, "output_tokens": 1}})


def _tool(index):
    """请求一次可计数的模拟工具。"""

    return httpx.Response(200, json={"output": [_turn(index)[0]], "usage": {"input_tokens": 2, "output_tokens": 1}})


def _overflow():
    """模拟上游明确的上下文拒绝。"""

    return httpx.Response(400, json={"error": {"code": "context_length_exceeded", "type": "invalid_request_error", "message": "上下文超限"}})


async def test_runner_compacts_without_changing_system_or_replaying_tools(run_context):
    """多轮运行中系统、Skill、工具、原始任务不变，摘要不会冒充最终回答。"""

    normal, summaries = [], []

    async def handler(request):
        body = json.loads(request.content)
        if body["instructions"].startswith(SUMMARY_INSTRUCTIONS):
            summaries.append(body)
            assert body["tools"] == [] and body["tool_choice"] == "none"
            assert "TASK-ORIGINAL" not in json.dumps(body)
            return _response("进展：已审核部分文件，操作及 SHA abc123 已记录；待办：继续验证。")
        normal.append(body)
        return _tool(len(normal) - 1) if len(normal) <= 4 else _response("任务完成")

    outcome = await run_context(handler)
    assert outcome.result.status == "completed"
    assert outcome.result.final_message == "任务完成"
    assert outcome.calls == [0, 1, 2, 3]
    assert summaries
    assert all(body["instructions"] == normal[0]["instructions"] and body["tools"] == outcome.tools for body in normal)
    assert all(body["input"][0] == normal[0]["input"][0] for body in normal)
    assert any(SUMMARY_PREFIX.rstrip() in json.dumps(body, ensure_ascii=False) for body in normal[1:])
    assert outcome.result.usage["input_tokens"] == 2 * (len(normal) + len(summaries))
    assert outcome.snapshots[-1]["context_compactions"][-1]["fixed_content_preserved"] is True


@pytest.mark.parametrize("again", [False, True])
async def test_context_error_retries_only_current_request_once(run_context, again):
    """超限后压缩一次；仍失败时禁止整轮重跑，已执行工具只执行一次。"""

    normal, summaries = [], []

    async def handler(request):
        body = json.loads(request.content)
        if body["instructions"].startswith(SUMMARY_INSTRUCTIONS):
            summaries.append(body)
            return _response("已完成工具 call-0，剩余验证未完成。")
        normal.append(body)
        if len(normal) == 1:
            return _tool(0)
        return _overflow() if len(normal) == 2 or again else _response("完成")

    outcome = await run_context(handler)
    assert len(normal) == 3 and len(summaries) == 1
    assert outcome.calls == [0]
    assert len(json.dumps(normal[-1]["input"])) < len(json.dumps(normal[-2]["input"]))
    assert outcome.result.status == ("failed" if again else "completed")
    if again:
        assert outcome.result.error_code == "context_length_exceeded"
        assert outcome.result.retryable is False


@pytest.mark.parametrize("kind", ["tool", "empty", "incomplete", "cancel"])
async def test_summary_cannot_execute_tools_or_finish_task(run_context, kind):
    """摘要返回工具或无效内容时拒绝，取消时不重试或继续执行。"""

    normal = []

    async def handler(request):
        body = json.loads(request.content)
        if body["instructions"].startswith(SUMMARY_INSTRUCTIONS):
            if kind == "tool":
                return _tool(999)
            if kind == "cancel":
                raise asyncio.CancelledError()
            if kind == "incomplete":
                return httpx.Response(200, json={"status": "incomplete", "output": [], "output_text": "半份摘要"})
            return _response("")
        normal.append(body)
        return _tool(len(normal) - 1)

    outcome = await run_context(handler)
    assert outcome.result.status == ("cancelled" if kind == "cancel" else "failed")
    assert 999 not in outcome.calls
    assert not outcome.result.final_message
    assert not any(event == "context.compacted" for event, payload in outcome.logs)
    if kind != "cancel":
        assert outcome.result.retryable is False
        assert outcome.result.usage["input_tokens"] >= 4


async def test_switch_to_smaller_model_rechecks_budget_and_keeps_plain_summary(run_context):
    """A 到小窗口 B 前压缩，下一轮恢复 A 时携带通用摘要与完整近期结果。"""

    normal, summaries = [], []

    async def handler(request):
        body = json.loads(request.content)
        if body["instructions"].startswith(SUMMARY_INSTRUCTIONS):
            summaries.append(body)
            return _response("已执行 call-0、call-1；待完成最终审核。")
        normal.append(body)
        if len(normal) <= 2:
            return _tool(len(normal) - 1)
        if len(normal) == 3:
            return httpx.Response(503, json={"error": {"code": "server_error"}})
        return _tool(2) if len(normal) == 4 else _response("完成")

    settings = _settings(model_context_windows={"a": {"gpt-a": 32000}, "b": {"gpt-b": 6500}})
    outcome = await run_context(handler, settings=settings, fallback=True)
    assert outcome.result.status == "completed"
    assert [body["model"] for body in normal] == ["gpt-a", "gpt-a", "gpt-a", "gpt-b", "gpt-a"]
    assert summaries and all(body["model"] == "gpt-b" for body in summaries)
    assert outcome.calls == [0, 1, 2]
    assert SUMMARY_PREFIX.rstrip() in json.dumps(normal[-1], ensure_ascii=False)


@pytest.mark.parametrize("quota", [True, False])
async def test_summary_provider_failure_uses_fallback_without_replaying_tools(run_context, quota):
    """摘要请求同样遵守额度跳过；临时错误在下一轮仍恢复主模型。"""

    normal, summaries = [], []

    async def handler(request):
        body = json.loads(request.content)
        if body["instructions"].startswith(SUMMARY_INSTRUCTIONS):
            summaries.append(body)
            if body["model"] == "gpt-a":
                return httpx.Response(429 if quota else 503, json={"error": {
                    "code": "insufficient_quota" if quota else "server_error",
                }})
            return _response("已执行 call-0 和 call-1，尚未完成最终验证。")
        normal.append(body)
        return _tool(len(normal) - 1) if len(normal) <= 3 else _response("完成")

    outcome = await run_context(handler, fallback=True)
    assert outcome.result.status == "completed"
    assert outcome.calls == [0, 1, 2]
    assert [body["model"] for body in normal] == ["gpt-a", "gpt-a", "gpt-b", "gpt-b" if quota else "gpt-a"]
    assert [body["model"] for body in summaries][:2] == ["gpt-a", "gpt-b"]
    assert outcome.snapshots[-1]["quota_exhausted_models"] == ([{"provider_id": "a", "model": "gpt-a"}] if quota else [])
    failures = [payload for event, payload in outcome.logs if event == "model.attempt_failed"]
    assert failures[0]["phase"] == "compaction"


async def test_tool_output_log_is_not_truncated_with_model_copy(run_context):
    """送模截短不影响原日志，模型看见截短提示而不是虚假的完整输出。"""

    normal = []

    async def handler(request):
        body = json.loads(request.content)
        assert not body["instructions"].startswith(SUMMARY_INSTRUCTIONS)
        normal.append(body)
        return _tool(0) if len(normal) == 1 else _response("完成")

    outcome = await run_context(handler, output_size=30000, settings=_settings(default_context_window_tokens=32000))
    assert outcome.result.status == "completed" and outcome.calls == [0]
    output = next(item["output"] for item in normal[-1]["input"] if item.get("type") == "function_call_output")
    assert json.loads(output)["truncated_for_context"] is True
    assert len(output.encode("utf-8")) <= 4096
    assert any("x" * 30000 in json.dumps(payload) for event, payload in outcome.logs if event == "item.completed")


async def test_disabled_compaction_still_stops_deterministic_context_error(run_context):
    """关闭压缩不修改历史，但不能将确定性超限变成整轮 Agent 重试。"""

    calls = []

    async def handler(request):
        body = json.loads(request.content)
        calls.append(body)
        return _tool(0) if len(calls) == 1 else _overflow()

    outcome = await run_context(handler, settings=_settings(enabled=False), output_size=30000)
    assert outcome.result.error_code == "context_length_exceeded"
    assert outcome.result.retryable is False
    assert outcome.calls == [0] and len(calls) == 2
    assert "x" * 30000 in json.dumps(calls[-1])


@pytest.mark.parametrize("driver", ["openai_chat_completions", "anthropic_messages", "gemini_generate_content"])
@pytest.mark.parametrize("complete", [False, True])
async def test_summary_protocol_adapters_have_no_tools_and_preserve_stop_state(driver, complete):
    """各协议不增加工具或任务 Schema，并保留截断状态供摘要拒绝。"""

    async def handler(request):
        body = json.loads(request.content)
        assert not body.get("tools") and "response_format" not in body
        if driver == "openai_chat_completions":
            assert body["messages"][0]["content"].startswith(SUMMARY_INSTRUCTIONS)
            document = {"choices": [{"message": {"content": "摘要"}, "finish_reason": "stop" if complete else "length"}]}
        elif driver == "anthropic_messages":
            assert body["system"].startswith(SUMMARY_INSTRUCTIONS)
            document = {"content": [{"type": "text", "text": "摘要"}], "stop_reason": "end_turn" if complete else "max_tokens"}
        else:
            assert body["systemInstruction"]["parts"][0]["text"].startswith(SUMMARY_INSTRUCTIONS)
            document = {"candidates": [{"content": {"parts": [{"text": "摘要"}]}, "finishReason": "STOP" if complete else "MAX_TOKENS"}]}
        return httpx.Response(200, json=document)

    provider = ModelProviderConfig(display_name="test", driver=driver, base_url="https://test.example.test", default_model="test")
    client = ExternalModelClient(provider, "key", timeout_seconds=10, idle_timeout_seconds=10, transport=httpx.MockTransport(handler))
    response = await client.create_response(summary_payload("test", "已有摘要", "历史素材", 512))
    assert response["status"] == ("completed" if complete else "incomplete")


@pytest.mark.parametrize("mode", ["external", "external_json", "codex_http", "codex_sse", "codex_json"])
async def test_clients_preserve_context_length_error(mode, monkeypatch):
    """各错误入口统一识别明确超限，不依赖格式化中文文本反向解析。"""

    class OAuth:
        """测试不读取宿主 OAuth。"""

        async def credentials(self):
            return object()

    monkeypatch.setattr("teamwork_review_agents.codex_model_client._codex_headers", lambda *args, **kwargs: {})
    error = {"error": {"code": "context_length_exceeded", "type": "invalid_request_error"}}

    async def handler(request):
        if mode in {"external", "codex_http"}:
            return httpx.Response(400, json=error)
        if mode in {"codex_json", "external_json"}:
            return httpx.Response(200, json=error)
        event = {"type": "response.failed", "response": error}
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=f"data: {json.dumps(event)}\n\n")

    transport = httpx.MockTransport(handler)
    if mode.startswith("external"):
        client = ExternalModelClient(ModelProviderConfig(display_name="a", driver="openai_responses", base_url="https://a.example.test", default_model="gpt-a"), "key", timeout_seconds=10, idle_timeout_seconds=10, transport=transport)
    else:
        client = CodexResponsesClient(oauth=OAuth(), codex_binary="test-codex", transport=transport)
    with pytest.raises((CodexUpstreamError, ModelProviderRequestError)) as raised:
        await client.create_response({"model": "gpt-a"})
    assert raised.value.context_length_exceeded is True
    assert raised.value.quota_exhausted is False
