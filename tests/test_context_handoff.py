"""验证摘要能参考完整任务、分片不丢依据，以及完整窗口的 95% 触发边界。"""

from __future__ import annotations

import copy
import json

import pytest

from teamwork_review_agents.config import ContextCompactionConfig
from teamwork_review_agents.context_compaction import (
    ConversationContext,
    ContextCompactionError,
    SUMMARY_INSTRUCTIONS,
    SUMMARY_PREFIX,
    SUMMARY_REQUEST,
    estimate_tokens,
)


def _round(index: int, size: int) -> list[dict]:
    """完整回合携带唯一标记，方便核对分片后的材料覆盖。"""

    return [
        {"type": "function_call", "call_id": f"call-{index}", "name": "execute_command", "arguments": "{}"},
        {"type": "function_call_output", "call_id": f"call-{index}", "output": f"结果-{index}:" + "x" * size},
    ]


def _source(payload: dict) -> dict:
    """摘要输入是只读快照，最后一条消息才是本次交接请求。"""

    assert payload["input"][-1]["content"][0]["text"] == SUMMARY_REQUEST
    assert payload["tools"] == [] and payload["tool_choice"] == "none"
    assert "text" not in payload
    return json.loads(payload["input"][0]["content"][0]["text"])


async def test_full_snapshot_includes_recent_rounds_and_task_without_changing_live_context():
    """一次能装下时，摘要看到原始目标、系统/工具定义和最近结果，替换时只替换旧历史。"""

    fixed = [{"role": "user", "content": "增量更新文档，范围 before..after，只提交文档，不执行 PR 审核"}]
    identity = {"agent_name": "incremental-doc-updater", "repository_id": "repo", "run_id": "one-run"}
    fields = {
        "instructions": "系统权限边界与 Skill 全文",
        "tools": [{"type": "function", "name": "execute_command"}],
        "text": {"format": {"type": "json_schema", "schema": {
            "type": "object", "properties": {"encrypted_content": {"type": "string"}},
        }}},
    }
    original_fields = copy.deepcopy(fields)
    context = ConversationContext(fixed, ContextCompactionConfig(), runtime_identity=identity)
    rounds = [_round(0, 9000), _round(1, 1500), _round(2, 1500)]
    rounds[0].insert(0, {"type": "reasoning", "summary": [], "encrypted_content": "不能当作明文资料"})
    for item in rounds:
        context.append_round(item)
    requests = []

    async def summarize(payload):
        requests.append(payload)
        source = _source(payload)
        assert source["reference_context"] == {
            "runtime_identity": identity, "original_task": fixed, "request_fields": fields,
        }
        material = json.loads(source["history_fragment"])
        expected = copy.deepcopy(rounds)
        expected[0][0].pop("encrypted_content")
        assert material["completed_rounds"] + material["recent_rounds_reference"] == expected
        # 格式定义中的同名字段必须保留，只有历史中的不透明推理值需要排除。
        assert "不能当作明文资料" not in json.dumps(payload, ensure_ascii=False)
        return "已核对固定基线；下一步仅更新受影响文档。"

    result = await context.ensure_budget(model="test", request_fields=fields, window=32000, summarize=summarize, force=True)
    assert len(requests) == 1 and result["summary_context_mode"] == "full"
    assert result["summary_reference_preserved"] is True
    assert fields == original_fields and context.fixed_messages == fixed
    assert context.rounds == rounds[-2:]
    assert SUMMARY_REQUEST not in json.dumps(context.history(), ensure_ascii=False)
    assert context.history()[1]["content"][0]["text"].startswith(SUMMARY_PREFIX)


async def test_chunked_snapshot_preserves_fixed_reference_and_all_history_material():
    """超限分片仍在每一片携带任务和系统依据，最近回合也必须完整纳入参考资料。"""

    fixed = [{"role": "user", "content": "仅维护文档；已确定 before..after 基线，不审查后续提交"}]
    fields = {"instructions": "权限边界" * 100, "tools": [{"name": "execute_command"}]}
    context = ConversationContext(fixed, ContextCompactionConfig(keep_recent_rounds=1, reserved_output_tokens=512))
    context.summary = "先前摘要：已确认基线，尚未推送。"
    original_summary = context.summary
    rounds = [_round(0, 9000), _round(1, 9000), _round(2, 1000)]
    for item in rounds:
        context.append_round(item)
    fragments = []

    async def summarize(payload):
        assert estimate_tokens(payload) + 256 <= 12000 - 512
        source = _source(payload)
        assert source["reference_context"]["original_task"] == fixed
        assert source["reference_context"]["request_fields"] == fields
        fragments.append(source["history_fragment"])
        return "已完成基线检查；保留未提交 README 修改；下一步验证文档并提交。"

    result = await context.ensure_budget(model="test", request_fields=fields, window=12000, summarize=summarize)
    assert result["summary_context_mode"] == "chunked" and len(fragments) > 1
    material = json.loads("".join(fragments))
    assert material["previous_summary"] == original_summary
    assert material["completed_rounds"] + material["recent_rounds_reference"] == rounds
    assert material["recent_rounds_reference"] == rounds[-1:]
    assert context.rounds == rounds[-1:] and context.fixed_messages == fixed


async def test_repeated_document_handoffs_keep_original_identity_and_side_effect_evidence():
    """模拟现场多次压缩：即使旧摘要角色错误，每轮仍提供正确文档任务及修改证据。"""

    task = "负责增量文档更新；固定 before..after；只修改文档，验证后提交并推送，不开展只读代码评审"
    fixed = [{"role": "user", "content": task}]
    identity = {"agent_name": "incremental-doc-updater", "run_id": "same-run"}
    context = ConversationContext(fixed, ContextCompactionConfig(), runtime_identity=identity)
    # 程序持有独立身份快照，不允许调用方之后修改原字典影响交接。
    identity["agent_name"] = "错误身份"
    context.summary = "错误旧摘要：正在只读评审 PR，待审查目标分支后续提交。"
    requests = []

    async def summarize(payload):
        source = _source(payload)
        assert source["reference_context"]["original_task"] == fixed
        assert source["reference_context"]["runtime_identity"]["agent_name"] == "incremental-doc-updater"
        assert "旧摘要与原始任务冲突时纠正旧摘要" in payload["instructions"]
        assert "不从工具操作或旧摘要猜测角色" in payload["instructions"]
        requests.append(source)
        # 模拟摘要按明确任务依据和执行证据交接，不借助真实模型宣称语义保证。
        return "已验证 before..after；README.md 已修改未提交；补丁失败后替换成功；待验证并提交推送，基线未变无需重查。"

    for cycle in range(3):
        for offset in range(3):
            item = _round(cycle * 3 + offset, 2500)
            item[-1]["output"] += "README.md 已修改；git diff --check 通过；commit 未执行；push 未执行"
            context.append_round(item)
        result = await context.ensure_budget(model="test", request_fields={"instructions": "保留权限边界"},
                                             window=24000, summarize=summarize, force=True)
        assert result["summary_reference_preserved"] and context.fixed_messages == fixed
        assert context.history()[1]["content"][0]["text"].startswith(SUMMARY_PREFIX)
        assert "这是同一次任务的继续" in context.history()[1]["content"][0]["text"]
        assert "README.md 已修改未提交" in context.summary
        assert "正在只读评审" not in context.summary
    assert len(requests) == 3
    assert all("README.md 已修改" in source["history_fragment"] for source in requests)


async def test_reference_too_large_fails_without_cutting_task_or_replacing_history():
    """正常请求固定内容可容纳，但加上交接指令已放不下时，不擅自删除原始任务依据。"""

    context = ConversationContext([{"role": "user", "content": "原始文档任务"}], ContextCompactionConfig(reserved_output_tokens=512))
    context.append_round(_round(0, 1000))
    before = context.history()

    async def summarize(payload):
        pytest.fail("参考资料超限时不能发送请求")

    with pytest.raises(ContextCompactionError) as caught:
        await context.ensure_budget(model="test", request_fields={"instructions": "x" * 6500},
                                    window=8192, summarize=summarize, force=True)
    assert caught.value.error_code == "context_summary_reference_too_large"
    assert caught.value.diagnostics["summary_reference_preserved"] is True
    assert context.history() == before and context.compaction_count == 0


@pytest.mark.parametrize("ratio,reserved,threshold", [(0.95, 512, 19000), (0.8, 512, 16000), (0.95, 3000, 17000)])
@pytest.mark.parametrize("offset", [-1, 0, 1])
async def test_automatic_threshold_uses_full_window_and_output_safety_cap(ratio, reserved, threshold, offset):
    """覆盖阈值前、恰到阈值、超阈值；不是对扣除输出后的预算再乘比例。"""

    settings = ContextCompactionConfig(trigger_ratio=ratio, reserved_output_tokens=reserved)
    context = ConversationContext([{"role": "user", "content": "文档更新任务"}], settings)
    fields = {"instructions": "系统边界"}
    context.append_round(_round(0, 0))
    base_cost = estimate_tokens({**fields, "input": context.history()}) + 256
    context.rounds[0][-1]["output"] += "x" * (threshold + offset - base_cost)
    assert estimate_tokens({**fields, "input": context.history()}) + 256 == threshold + offset
    calls = []

    async def summarize(payload):
        calls.append(payload)
        assert estimate_tokens(payload) + 256 <= 20000 - reserved
        return "已读证据，下一步更新文档。"

    result = await context.ensure_budget(model="test", request_fields=fields, window=20000, summarize=summarize)
    if offset < 0:
        assert result is None and calls == []
    else:
        assert result["trigger_threshold"] == threshold and result["trigger_ratio"] == ratio
        assert result["after_estimated_tokens"] <= 20000 - reserved


def test_default_trigger_ratio_and_resume_guidance():
    """95% 为新默认值，恢复提示区分继续任务与从头重跑。"""

    assert ContextCompactionConfig().trigger_ratio == 0.95
    assert "不重做已完成检查" in SUMMARY_PREFIX
    assert "提交、推送等已发生操作不得重复执行" in SUMMARY_PREFIX
    assert "失败操作及原因" in SUMMARY_INSTRUCTIONS
    assert "已完成检查及结论/重查条件" in SUMMARY_INSTRUCTIONS
