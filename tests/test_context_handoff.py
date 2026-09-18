"""验证完整原对话 fork、交接消息、隔离与 90% 默认触发边界。"""

from __future__ import annotations

import copy
import json

import pytest

from teamwork_review_agents.config import ContextCompactionConfig
from teamwork_review_agents.context_compaction import (
    ConversationContext,
    ContextCompactionError,
    SUMMARY_PREFIX,
    SUMMARY_REQUEST,
    estimate_tokens,
    summary_payload,
)


def _round(index: int, size: int) -> list[dict]:
    """使用真实消息结构保留工具调用与结果的唯一标识。"""

    return [
        {"role": "assistant", "content": [{"type": "output_text", "text": f"准备执行-{index}"}]},
        {"type": "function_call", "call_id": f"call-{index}", "name": "execute_command", "arguments": "{}"},
        {"type": "function_call_output", "call_id": f"call-{index}", "output": f"结果-{index}:" + "x" * size},
    ]


async def test_fork_preserves_entire_prefix_settings_and_recent_rounds():
    """摘要看到原消息和最近回合，只有 fork 末尾多一条请求；成功后仅替换旧历史。"""

    fixed = [{"role": "user", "content": "原始任务；必须遵守用户权限边界"}]
    fields = {
        "model": "test", "instructions": "SYSTEM 权限和 Skill 全文",
        "tools": [{"type": "function", "name": "execute_command"}],
        "tool_choice": "auto", "parallel_tool_calls": False,
        "reasoning": {"effort": "high", "summary": "auto"},
        "include": ["reasoning.encrypted_content"], "service_tier": "priority",
        "text": {"verbosity": "low", "format": {"type": "json_schema", "schema": {"type": "object"}}},
    }
    original_fields = copy.deepcopy(fields)
    context = ConversationContext(fixed, ContextCompactionConfig())
    rounds = [_round(0, 9000), _round(1, 1500), _round(2, 1500)]
    rounds[0].insert(0, {"type": "reasoning", "summary": [], "encrypted_content": "opaque-test-value"})
    for item in rounds:
        context.append_round(item)
    original = context.history()
    requests = []

    async def summarize(payload):
        requests.append(payload)
        assert payload["input"][:-1] == original
        assert payload["input"][-1]["content"][0]["text"].startswith(SUMMARY_REQUEST)
        for key in ("instructions", "tools", "model", "reasoning", "include", "service_tier"):
            assert payload[key] == fields[key]
        assert payload["text"] == {"verbosity": "low"}
        assert payload["tool_choice"] == "none"
        assert context.history() == original
        assert payload["input"][1]["encrypted_content"] == "opaque-test-value"
        return "已完成检查；下一步验证剩余修改，证据路径 evidence.json。"

    result = await context.ensure_budget(model="test", request_fields=fields, window=32000, summarize=summarize, force=True)
    assert len(requests) == 1 and result["summary_context_mode"] == "fork"
    assert result["summary_material_complete"] and result["fixed_content_preserved"]
    assert fields == original_fields and context.fixed_messages == fixed
    assert context.rounds == rounds[-2:]
    assert SUMMARY_REQUEST not in json.dumps(context.history(), ensure_ascii=False)
    assert context.history()[1]["content"][0]["text"].startswith(SUMMARY_PREFIX)


@pytest.mark.parametrize("abort", [False, True])
async def test_fork_mutation_does_not_leak_back_to_parent(abort):
    """客户端修改副本嵌套内容也不能影响父对话、工具定义和输出 Schema。"""

    fields = {"instructions": "SYSTEM", "tools": [{"name": "execute_command"}],
              "text": {"format": {"type": "json_object"}}}
    context = ConversationContext([{"role": "user", "content": [{"type": "input_text", "text": "原始任务"}]}],
                                  ContextCompactionConfig(keep_recent_rounds=1, summary_target_bytes=512))
    for index in range(3):
        context.append_round(_round(index, 2200))
    before, original_fields = context.history(), copy.deepcopy(fields)

    async def summarize(payload):
        assert payload["input"][:-1] == before
        payload["input"][0]["content"][0]["text"] = "被修改的副本"
        payload["input"][-2]["output"] = "被修改的工具结果"
        payload["tools"][0]["name"] = "被修改的工具"
        if abort:
            raise RuntimeError("副本请求失败")
        return "已记录结果，下一步继续完成原始任务。"

    if abort:
        with pytest.raises(RuntimeError, match="副本请求失败"):
            await context.ensure_budget(model="test", request_fields=fields, window=24000, summarize=summarize, force=True)
        assert context.history() == before and context.compaction_count == 0
    else:
        await context.ensure_budget(model="test", request_fields=fields, window=24000, summarize=summarize, force=True)
        assert context.fixed_messages == before[:1]
        assert context.rounds == [_round(2, 2200)]
    assert fields == original_fields


@pytest.mark.parametrize("task", ["核对数据并生成报告", "更新文档并验证链接", "实现功能并运行测试"])
async def test_repeated_handoffs_fork_current_history_without_role_flattening(task):
    """不同类型任务共用同一交接请求，多次压缩仍保留原任务及工具消息身份。"""

    fixed = [{"role": "user", "content": task}]
    context = ConversationContext(fixed, ContextCompactionConfig())
    context.summary = "先前已完成准备，尚未执行下一步。"
    fields = {"instructions": "系统约束保持不变"}
    requests = []

    for cycle in range(3):
        for offset in range(3):
            context.append_round(_round(cycle * 3 + offset, 2500))
        original = context.history()

        async def summarize(payload):
            assert payload["input"][:-1] == original
            assert payload["instructions"] == fields["instructions"]
            assert payload["input"][-1]["role"] == "user"
            assert payload["input"][1]["role"] == "assistant"
            requests.append(payload)
            return f"已完成第 {cycle} 组操作，尚未完成最终验证。"

        result = await context.ensure_budget(model="test", request_fields=fields, window=32000, summarize=summarize, force=True)
        assert result["summary_context_mode"] == "fork"
        assert context.fixed_messages == fixed
        assert context.compaction_count == cycle + 1
    assert len(requests) == 3


async def test_fork_too_large_retains_original_instructions_and_history():
    """原对话可能可发送，但追加交接已超限时不截断、不切片、不调用摘要模型。"""

    context = ConversationContext([{"role": "user", "content": "原始任务"}],
                                  ContextCompactionConfig(reserved_output_tokens=512))
    context.append_round(_round(0, 1000))
    before = context.history()

    async def summarize(payload):
        pytest.fail("完整副本超限时不能发送请求")

    with pytest.raises(ContextCompactionError) as caught:
        await context.ensure_budget(model="test", request_fields={"instructions": "x" * 6000},
                                    window=8192, summarize=summarize, force=True)
    assert caught.value.error_code == "context_summary_request_too_large"
    assert caught.value.diagnostics["summary_requests"] == 0
    assert context.history() == before and context.compaction_count == 0


@pytest.mark.parametrize("ratio,reserved", [(0.90, 512), (0.95, 512), (0.8, 512), (0.90, 15000)])
@pytest.mark.parametrize("offset", [-1, 0, 1])
async def test_threshold_reserves_output_and_appended_request(ratio, reserved, offset):
    """90% 与显式比例使用完整窗口，安全上限同时预留输出和追加交接提示。"""

    window = 100000
    settings = ContextCompactionConfig(trigger_ratio=ratio, reserved_output_tokens=reserved)
    context = ConversationContext([{"role": "user", "content": "当前任务"}], settings)
    fields = {"model": "test", "instructions": "系统边界"}
    context.append_round(_round(0, 0))
    base = estimate_tokens({**fields, "input": context.history()}) + 256
    fork = summary_payload("test", context.history(), fields, settings.summary_target_bytes, shortening=True)
    headroom = max(0, estimate_tokens(fork) + 256 - base)
    threshold = min(int(window * ratio), window - reserved - headroom)
    context.rounds[0][-1]["output"] += "x" * (threshold + offset - base)
    calls = []

    async def summarize(payload):
        calls.append(payload)
        assert estimate_tokens(payload) + 256 <= window - reserved
        return "已完成部分工作，下一步继续验证。"

    result = await context.ensure_budget(model="test", request_fields=fields, window=window, summarize=summarize)
    if offset < 0:
        assert result is None and calls == []
    else:
        assert result["trigger_threshold"] == threshold
        assert result["summary_prompt_headroom"] == headroom
        assert result["trigger_ratio"] == ratio
        assert len(calls) == 1


def test_default_ratio_and_generic_handoff_prompt():
    """新默认 90%，显式 95% 不覆盖；交接请求不绑定任何具体 Agent。"""

    assert ContextCompactionConfig().trigger_ratio == 0.90
    assert ContextCompactionConfig(trigger_ratio=0.95).trigger_ratio == 0.95
    assert "不调用工具" in SUMMARY_REQUEST and "最新要求" in SUMMARY_REQUEST
    assert "当前进展" in SUMMARY_REQUEST and "未完成" in SUMMARY_REQUEST
    assert "工具结果文件路径" in SUMMARY_REQUEST
    assert "不重做已完成检查" in SUMMARY_PREFIX
    assert "提交、推送等已发生操作不得重复执行" in SUMMARY_PREFIX
