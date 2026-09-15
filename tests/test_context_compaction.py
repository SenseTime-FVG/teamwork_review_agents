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
    ConversationContext, ContextCompactionError, SUMMARY_INSTRUCTIONS, SUMMARY_PREFIX, SUMMARY_REQUEST,
    estimate_tokens, resolve_context_window, summary_payload,
)
from teamwork_review_agents.codex_model_client import CodexResponsesClient, CodexUpstreamError
from teamwork_review_agents.codex_model_runner import CodexModelRunner, _instructions
from teamwork_review_agents.environment import SecretRedactor
from teamwork_review_agents.model_provider_client import ExternalModelClient, ModelProviderRequestError
from teamwork_review_agents.model_provider_credentials import ModelProviderCredentialStore
from teamwork_review_agents.model_provider_runtime import resolve_model_plan
from teamwork_review_agents.model_tools import ModelToolExecutor, teamwork_function_tools
from teamwork_review_agents.tool_results import ToolResultError, ToolResultStore


def _settings(**overrides):
    """小窗口让测试只使用少量数据即可触发压缩。"""

    return ContextCompactionConfig(**{
        "default_context_window_tokens": 8192, "reserved_output_tokens": 512,
        "summary_target_bytes": 512, "tool_output_inline_bytes": 65536,
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
        source = json.loads(payload["input"][0]["content"][0]["text"])
        assert source["reference_context"]["request_fields"] == original_fields
        assert source["reference_context"]["original_task"] == original_fixed
        assert payload["input"][-1]["content"][0]["text"] == SUMMARY_REQUEST
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
        return "" if failure == "empty" else "x" * (10000 if failure == "large" else 512)

    expected = asyncio.CancelledError if failure == "cancel" else RuntimeError
    with pytest.raises(expected):
        await context.ensure_budget(model="test", request_fields={}, window=8192, summarize=summarize, force=True)
    assert context.history() == before
    assert context.compaction_count == 0


async def test_chinese_summary_above_2048_bytes_is_accepted_when_full_request_fits():
    """回归现场问题：800 汉字摘要不再因 2400 字节超过软目标而被拒绝。"""

    context = _context(ContextCompactionConfig())
    context.append_round(_turn(0, 125000))
    fixed = copy.deepcopy(context.fixed_messages)
    fields = {"instructions": "SYSTEM 原文；Skill 和权限原样保留", "tools": [{"name": "原始工具"}]}
    summary = "摘要" * 400

    async def summarize(payload):
        assert "软目标" in payload["instructions"]
        assert "不得超过" not in payload["instructions"]
        return summary

    result = await context.ensure_budget(model="test", request_fields=fields, window=131072, summarize=summarize)
    assert result["summary_bytes"] == 2400
    assert result["summary_target_bytes"] == 2048
    assert result["summary_above_target"] is True
    assert result["summary_requests"] >= 1 and result["summary_rewrites"] == 0
    assert result["after_estimated_tokens"] < result["before_estimated_tokens"]
    assert result["after_estimated_tokens"] <= result["input_budget"]
    assert context.summary == summary and context.fixed_messages == fixed


async def test_summary_above_soft_goal_keeps_complete_recent_round():
    """软目标和目标压缩比例都不应拒绝已经能装下的完整替代上下文。"""

    context = _context(_settings(target_ratio=0.5))
    for index in range(4):
        context.append_round(_turn(index))

    async def summarize(payload):
        return "摘要" * 400

    result = await context.ensure_budget(model="test", request_fields={}, window=8192, summarize=summarize)
    assert result["after_estimated_tokens"] > result["input_budget"] * 0.5
    assert result["summary_above_target"] is True
    assert context.rounds == [_turn(3)]


@pytest.mark.parametrize("recover", [True, False])
async def test_full_request_overflow_uses_bounded_rewrites(recover):
    """计算固定指令与 JSON 转义的整体开销，只有真实不合预算时才收短。"""

    context = _context()
    context.append_round(_turn(0, 5000))
    original = context.history()
    requests, diagnostics = [], []

    async def summarize(payload):
        requests.append(payload)
        assert estimate_tokens(payload) + 256 <= 32256
        return "已完成验证，尚未推送" if recover and len(requests) > 1 else "\\" * 7000

    async def report(value):
        diagnostics.append(value)

    kwargs = dict(model="test", request_fields={"instructions": "x" * 20000}, window=32768,
                  summarize=summarize, force=True, diagnostic_callback=report)
    if recover:
        result = await context.ensure_budget(**kwargs)
        assert result["summary_rewrites"] == 1 and result["summary_requests"] == 2
        assert result["after_estimated_tokens"] <= 32256
    else:
        with pytest.raises(ContextCompactionError) as caught:
            await context.ensure_budget(**kwargs)
        assert caught.value.error_code == "context_summary_context_overflow"
        assert caught.value.diagnostics["summary_bytes"] == 7000
        assert caught.value.diagnostics["summary_rewrites"] == 2
        assert caught.value.diagnostics["after_estimated_tokens"] > 32256
        assert context.history() == original
        assert len(requests) == 3
    assert all(payload["input"] == requests[0]["input"] for payload in requests)
    assert diagnostics[0]["reason"] == "context_summary_context_overflow"
    assert "进一步收短" in requests[1]["instructions"]


async def test_large_intermediate_draft_is_regenerated_without_truncating_source():
    """中间草稿太大时不发送超限请求；重用完整上一片段，并保留全部后续材料。"""

    context = _context()
    context.append_round(_turn(0, 18000))
    requests = []

    async def summarize(payload):
        requests.append(payload)
        assert estimate_tokens(payload) + 256 <= 7680
        return "z" * 10000 if len(requests) == 1 else "已记录前面的全部关键事实"

    result = await context.ensure_budget(model="test", request_fields={}, window=8192, summarize=summarize)
    assert result["summary_rewrites"] == 1
    assert requests[0]["input"] == requests[1]["input"]
    assert all("z" * 10000 not in json.dumps(payload) for payload in requests[1:])
    # 忽略重复归纳的那次请求，按顺序拼回全部源材料，确认没有为控长丢掉片段。
    sources = [json.loads(payload["input"][0]["content"][0]["text"]) for payload in [requests[0], *requests[2:]]]
    original = json.loads("".join(item["history_fragment"] for item in sources))
    assert original["completed_rounds"] == [_turn(0, 18000)]
    assert context.summary == "已记录前面的全部关键事实"


async def test_existing_summary_can_be_split_for_a_smaller_model():
    """已有摘要超过新模型窗口时，作为完整材料分片处理，不能直接丢弃。"""

    context = _context()
    context.summary = "旧摘要" * 2000
    pieces = []

    async def summarize(payload):
        assert estimate_tokens(payload) + 256 <= 7680
        pieces.append(json.loads(payload["input"][0]["content"][0]["text"])["history_fragment"])
        return "已保留旧摘要的关键事实"

    result = await context.ensure_budget(model="smaller", request_fields={}, window=8192, summarize=summarize)
    assert result["summary_requests"] > 1
    assert json.loads("".join(pieces))["previous_summary"] == "旧摘要" * 2000


@pytest.mark.parametrize("limit", ["rewrites", "requests", "cancel"])
async def test_rewrite_limits_and_cancellation_preserve_active_history(limit):
    """收短同时受次数及总请求限制，取消或耗尽后不提交草稿。"""

    settings = _settings(**({"max_summary_rewrites": 0} if limit == "rewrites" else {"max_compaction_requests": 1} if limit == "requests" else {}))
    context = _context(settings)
    context.append_round(_turn(0, 2600))
    original = context.history()
    requests = []

    async def summarize(payload):
        requests.append(payload)
        if len(requests) > 1:
            raise asyncio.CancelledError()
        return "\\" * 2000

    with pytest.raises(asyncio.CancelledError if limit == "cancel" else ContextCompactionError) as caught:
        await context.ensure_budget(model="test", request_fields={"instructions": "x" * 5000}, window=8192, summarize=summarize, force=True)
    if limit == "requests":
        assert caught.value.error_code == "context_compaction_request_limit"
    assert len(requests) == (2 if limit == "cancel" else 1)
    assert context.history() == original


def test_legacy_summary_setting_is_a_soft_goal_and_serializes_new_name():
    """旧键仍可读取，大软目标不再让有效模型窗口配置被拒绝。"""

    settings = ContextCompactionConfig(max_summary_tokens=1000000)
    assert settings.summary_target_bytes == 1000000
    assert "max_summary_tokens" not in settings.model_dump()
    assert settings.model_dump()["summary_target_bytes"] == 1000000
    assert ContextCompactionConfig(max_summary_tokens=500, summary_target_bytes=1000).summary_target_bytes == 1000


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


def test_tool_pairing():
    """不拆散工具调用与结果的配对。"""

    context = _context()
    with pytest.raises(ContextCompactionError, match="配对"):
        context.append_round(_turn(0)[:1])
    context.append_round(_turn(0))
    assert context.rounds == [_turn(0)]


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
        pytest.fail("只有原始任务而没有历史时，不应生成摘要")

    root = Path(__file__).resolve().parents[1]
    for name in ("general-review.md", "依赖review&增量文档更新 入口.md", "依赖review.md", "增量文档更新.md"):
        prompt = (root / "prompts" / name).read_text(encoding="utf-8")
        context = ConversationContext([{"role": "user", "content": prompt}], ContextCompactionConfig())
        assert await context.ensure_budget(model="test", request_fields=fields, window=131072, summarize=summarize) is None
        assert context.history()[0]["content"] == prompt


@pytest.mark.parametrize("name", ["general-review.md", "依赖review&增量文档更新 入口.md", "依赖review.md", "增量文档更新.md"])
async def test_builtin_prompts_remain_complete_summary_reference(configured_app_factory, name):
    """已有长 Prompt 进入交接请求时也完整保留，不因增加参考资料而误拦截默认窗口。"""

    config = configured_app_factory()
    fields = {
        "instructions": _instructions(repository=config.repositories[0], agent=config.agents["code-reviewer"], personality=None, skill_files={}),
        "tools": teamwork_function_tools(allow_sub_agents=True, allow_publish_comment=True),
    }
    prompt = (Path(__file__).resolve().parents[1] / "prompts" / name).read_text(encoding="utf-8")
    fixed = [{"role": "user", "content": prompt}]
    context = ConversationContext(fixed, ContextCompactionConfig())
    context.append_round(_turn(0, 30000))

    async def summarize(payload):
        assert estimate_tokens(payload) + 256 <= 131072 - 4096
        source = json.loads(payload["input"][0]["content"][0]["text"])
        assert source["reference_context"]["original_task"] == fixed
        assert source["reference_context"]["request_fields"] == fields
        return "已读取本轮证据，原任务继续有效，尚未提交或推送。"

    result = await context.ensure_budget(model="test", request_fields=fields, window=131072, summarize=summarize, force=True)
    assert result["summary_reference_preserved"] is True
    assert context.fixed_messages == fixed


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
            assert "TASK-ORIGINAL" in json.dumps(body)
            reference = json.loads(body["input"][0]["content"][0]["text"])["reference_context"]
            assert reference["runtime_identity"]["agent_name"] == "code-reviewer"
            assert reference["runtime_identity"]["run_id"] == "context-run"
            assert reference["request_fields"]["instructions"].startswith("SYSTEM-ORIGINAL")
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
    assert any(SUMMARY_PREFIX.splitlines()[0] in json.dumps(body, ensure_ascii=False) for body in normal[1:])
    assert outcome.result.usage["input_tokens"] == 2 * (len(normal) + len(summaries))
    assert outcome.snapshots[-1]["context_compactions"][-1]["fixed_content_preserved"] is True


async def test_runner_accepts_long_summary_and_does_not_replay_tool(run_context):
    """真实运行器收到超软目标摘要后继续当前请求，系统和已执行工具不改变。"""

    normal = []

    async def handler(request):
        body = json.loads(request.content)
        if body["instructions"].startswith(SUMMARY_INSTRUCTIONS):
            return _response("摘要" * 400)
        normal.append(body)
        return _tool(0) if len(normal) == 1 else _overflow() if len(normal) == 2 else _response("完成")

    outcome = await run_context(handler, output_size=30000, settings=ContextCompactionConfig())
    assert outcome.result.status == "completed" and outcome.calls == [0]
    assert len(normal) == 3
    assert normal[0]["instructions"] == normal[-1]["instructions"]
    assert normal[0]["input"][0] == normal[-1]["input"][0]
    compacted = next(payload for event, payload in outcome.logs if event == "context.compacted")
    assert compacted["summary_bytes"] == 2400 and compacted["summary_above_target"] is True
    assert compacted["summary_rewrites"] == 0


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
    if kind == "empty":
        assert outcome.result.error_code == "context_summary_empty"
        failure = next(payload for event, payload in outcome.logs if event == "context.compaction_failed")
        assert failure["summary_bytes"] == 0 and failure["input_budget"] == 7680
        assert failure["provider_id"] == "a" and failure["request_round"] > 1


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
    assert SUMMARY_PREFIX.splitlines()[0] in json.dumps(normal[-1], ensure_ascii=False)


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


@pytest.mark.parametrize("window,output_size", [(131072, 70000), (8192, 30000)])
async def test_large_tool_output_keeps_complete_readable_file(run_context, window, output_size):
    """模型在运行时能补读完整结果，结束后临时文件清理，原始日志仍完整。"""

    normal, paths = [], []

    async def handler(request):
        body = json.loads(request.content)
        assert not body["instructions"].startswith(SUMMARY_INSTRUCTIONS)
        normal.append(body)
        if len(normal) == 2:
            output = next(item["output"] for item in body["input"] if item.get("type") == "function_call_output")
            result = json.loads(output)
            path = Path(result["output_file"]["path"])
            paths.append(path)
            assert json.loads(path.read_text(encoding="utf-8")) == {"exit_code": 0, "stdout": "结果" + "x" * output_size}
            assert "execute_command" in result["read_hint"]
            assert result["exit_code"] == 0
        return _tool(0) if len(normal) == 1 else _response("完成")

    outcome = await run_context(handler, output_size=output_size, settings=_settings(default_context_window_tokens=window))
    assert outcome.result.status == "completed" and outcome.calls == [0]
    assert paths and not paths[0].exists()
    assert any(event == "context.tool_output_stored" for event, _ in outcome.logs)
    assert any("x" * output_size in json.dumps(payload) for event, payload in outcome.logs if event == "item.completed")


@pytest.mark.parametrize("output_size", [5628, 30000])
async def test_normal_tool_output_reaches_model_in_full_with_legacy_config(run_context, output_size):
    """截图中的普通输出不再被旧配置的 4096 字节预算截短。"""

    normal = []

    async def handler(request):
        body = json.loads(request.content)
        assert not body["instructions"].startswith(SUMMARY_INSTRUCTIONS)
        normal.append(body)
        return _tool(0) if len(normal) == 1 else _response("完成")

    outcome = await run_context(handler, output_size=output_size, settings=ContextCompactionConfig(tool_output_tokens=4096))
    assert outcome.result.status == "completed"
    output = next(item["output"] for item in normal[-1]["input"] if item.get("type") == "function_call_output")
    assert json.loads(output) == {"exit_code": 0, "stdout": "结果" + "x" * output_size}
    assert not any(event.startswith("context.") for event, _ in outcome.logs)


async def test_multiple_large_outputs_share_round_budget(run_context):
    """同回合多个工具都留下可读引用，不让第一个结果占满小窗口。"""

    normal, paths = [], []

    async def handler(request):
        body = json.loads(request.content)
        if body["instructions"].startswith(SUMMARY_INSTRUCTIONS):
            return _response("工具都已完成，文件证据已经检查。")
        normal.append(body)
        if len(normal) == 1:
            return httpx.Response(200, json={"output": [_turn(0)[0], _turn(1)[0]]})
        outputs = [json.loads(item["output"]) for item in body["input"] if item.get("type") == "function_call_output"]
        assert len(outputs) == 2
        for output in outputs:
            path = Path(output["output_file"]["path"])
            paths.append(path)
            assert len(json.loads(path.read_text(encoding="utf-8"))["stdout"]) == 30002
        return _response("完成")

    outcome = await run_context(handler, output_size=30000, settings=_settings(trigger_ratio=0.95))
    assert outcome.result.status == "completed" and outcome.calls == [0, 1]
    assert len(paths) == 2 and all(not path.exists() for path in paths)


async def test_tool_storage_failure_stops_run_without_replay(run_context, monkeypatch):
    """工具执行后保存失败是不可自动重试错误，不发第二个模型请求或重跑命令。"""

    normal = []

    async def handler(request):
        normal.append(json.loads(request.content))
        return _tool(0)

    def failed(self, *args, **kwargs):
        """模拟已执行工具后磁盘写入失败。"""
        raise ToolResultError("模拟磁盘已满")

    monkeypatch.setattr(ToolResultStore, "prepare", failed)
    outcome = await run_context(handler)
    assert outcome.result.status == "failed" and outcome.result.retryable is False
    assert outcome.result.error_code == "tool_output_storage_failed"
    assert outcome.calls == [0] and len(normal) == 1
    assert any(event == "run.tool_output_failed" for event, _ in outcome.logs)


async def test_cancelled_run_cleans_tool_result_files(run_context):
    """取消不会遗留临时证据文件，也不会重新执行工具。"""

    paths, normal = [], []

    async def handler(request):
        body = json.loads(request.content)
        normal.append(body)
        if len(normal) == 1:
            return _tool(0)
        output = next(item["output"] for item in body["input"] if item.get("type") == "function_call_output")
        paths.append(Path(json.loads(output)["output_file"]["path"]))
        assert paths[0].exists()
        raise asyncio.CancelledError

    outcome = await run_context(handler, output_size=70000, settings=ContextCompactionConfig())
    assert outcome.result.status == "cancelled" and outcome.calls == [0]
    assert paths and not paths[0].exists()


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
    assert "output_file" in json.dumps(calls[-1])


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
