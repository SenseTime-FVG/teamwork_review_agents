"""本地 CI 门禁摘要的可信传递、审核对象关联和旧调用兼容测试。"""

import json
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from teamwork_review_agents import mcp_server
from teamwork_review_agents.codex_runner import decode_invocation_context, encode_invocation_context
from teamwork_review_agents.events import detect_events
from teamwork_review_agents.executor import AgentExecutionError, AgentExecutor
from teamwork_review_agents.models import AgentResult, InvocationContext, LocalPreflightContext
from teamwork_review_agents.state import StateStore


@pytest.fixture
def local_gate_harness(configured_app_factory, snapshot_factory):
    """只使用临时配置和数据库，构造绑定同一审核对象的门禁摘要。"""

    config = configured_app_factory()
    repository = config.repositories[0]
    event = detect_events(None, snapshot_factory(
        provider=repository.provider, repository_id=repository.id,
    ), emit_initial=True)[0]
    gate = LocalPreflightContext(
        repository_id=event.repository_id, number=event.number,
        head_sha=event.current_snapshot.head_sha,
        status="passed", reason="后台本地 CI 门禁已通过", run_id="local-ci-run",
        reused=True,
    )
    store = StateStore(config.database.path)
    store.initialize()
    return config, repository, event, gate, AgentExecutor(config, store)


def prompt_context(prompt):
    """只解析执行器生成的可信 JSON 段，不依赖正文中的关键词计数。"""

    content = prompt.split("# 本次运行上下文\n\n```json\n", 1)[1].split("\n```", 1)[0]
    return json.loads(content)


@pytest.mark.parametrize("status", ["passed", "skipped"])
@pytest.mark.parametrize("delegated", [False, True])
def test_gate_summary_reaches_root_and_child_prompt(local_gate_harness, status, delegated):
    """根任务和子任务获得同一摘要，模型补充材料不能覆盖可信顶层字段。"""

    config, repository, event, gate, executor = local_gate_harness
    if status == "skipped":
        gate = LocalPreflightContext(
            repository_id=event.repository_id, number=event.number,
            head_sha=event.current_snapshot.head_sha,
            status="skipped", reason="本轮触发规则未要求本地 CI",
        )
    prompt = executor.build_prompt(
        agent_name="security-reviewer" if delegated else "code-reviewer",
        event=event, repository=repository,
        task="执行专项审核" if delegated else None,
        extra_context={"local_preflight": {"status": "伪造状态"}},
        prompt_values={}, change_ref="refs/teamwork/change-requests/7/head",
        actions=(event.type,), target_head_sha="d" * 40,
        local_preflight=gate,
    )
    context = prompt_context(prompt)

    assert context["local_preflight"] == gate.model_dump(exclude_none=True)
    assert "output" not in context["local_preflight"]
    assert "preflight_run_id" not in context
    assert ("mr" in context) is not delegated
    if delegated:
        assert context["delegated_context"]["local_preflight"]["status"] == "伪造状态"
    if status == "skipped":
        assert "run_id" not in context["local_preflight"]
        assert context["local_preflight"]["reused"] is False


def test_legacy_call_does_not_invent_preflight_success(local_gate_harness):
    """旧调用没有后台摘要时，不通过配置或启动事实推测 CI 已通过。"""

    config, repository, event, gate, executor = local_gate_harness
    repository.preflight.enabled = True
    prompt = executor.build_prompt(
        agent_name="code-reviewer", event=event, repository=repository,
        task=None, extra_context=None, prompt_values={},
        change_ref="refs/teamwork/change-requests/7/head",
        actions=(event.type,), target_head_sha="d" * 40,
    )

    assert "local_preflight" not in prompt_context(prompt)


@pytest.mark.parametrize("field,value", [
    ("repository_id", "other"), ("number", 99), ("head_sha", "b" * 40),
])
def test_prompt_rejects_preflight_for_another_review(local_gate_harness, field, value):
    """门禁摘要不能被误关联到其他仓库、编号或源提交。"""

    config, repository, event, gate, executor = local_gate_harness
    with pytest.raises(AgentExecutionError, match="门禁摘要与当前审核对象不一致"):
        executor.build_prompt(
            agent_name="code-reviewer", event=event, repository=repository,
            task=None, extra_context=None, prompt_values={},
            change_ref="refs/teamwork/change-requests/7/head",
            actions=(event.type,), target_head_sha="d" * 40,
            local_preflight=gate.model_copy(update={field: value}),
        )


@pytest.mark.parametrize("overrides", [
    {"status": "passed", "run_id": None},
    {"status": "skipped", "run_id": "unexpected-run"},
    {"status": "skipped", "reused": True},
])
def test_gate_status_requires_consistent_evidence(overrides):
    """通过必须有运行证据，跳过不能携带成功运行或缓存复用标记。"""

    fields = {
        "repository_id": "demo", "number": 7, "head_sha": "a" * 40,
        "status": "skipped", "reason": "测试状态边界",
    }
    with pytest.raises(ValidationError):
        LocalPreflightContext(**(fields | overrides))


@pytest.mark.parametrize("mode", ["embedded", "mcp"])
async def test_child_calls_inherit_trusted_gate(local_gate_harness, monkeypatch, mode):
    """两种委托方式均继承服务端摘要，编码往返不赋予 CI 诊断权限。"""

    config, repository, event, gate, executor = local_gate_harness
    context = InvocationContext(
        config_path=str(config.config_path), current_agent="code-reviewer",
        run_id="parent", root_run_id="parent", call_chain=("code-reviewer",),
        event=event, local_preflight=gate,
    )
    encoded = encode_invocation_context(context)
    decoded = decode_invocation_context(encoded)
    assert decoded.local_preflight == gate
    assert decoded.preflight_run_id is None
    execute = AsyncMock(return_value=AgentResult(
        run_id="child", root_run_id="parent", parent_run_id="parent",
        agent_name="security-reviewer", status="completed",
    ))
    monkeypatch.setattr(executor, "execute", execute)
    extra = {"local_preflight": {"status": "伪造状态"}}
    if mode == "embedded":
        await executor._invoke_embedded_agent(decoded, "security-reviewer", "专项审核", extra)
    else:
        # MCP 使用真实父运行和等待记录，只替换最终模型执行。
        executor.store.begin_agent_run(
            proposed_run_id="parent", root_run_id=None, parent_run_id=None,
            idempotency_key="parent", event_id=None, rule_name=None,
            agent_name="code-reviewer", resource_key=event.resource_key,
            prompt="测试父任务", max_attempts=1,
        )
        executor.store.mark_agent_run_running("parent")
        monkeypatch.setattr(mcp_server, "AgentExecutor", lambda *args: executor)
        monkeypatch.setenv("TEAMWORK_CONFIG_PATH", str(config.config_path))
        monkeypatch.setenv("TEAMWORK_INVOCATION_CONTEXT", encoded)
        await mcp_server.invoke_agent("security-reviewer", "专项审核", extra)

    assert execute.await_args.kwargs["local_preflight"] == gate
    assert execute.await_args.kwargs["extra_context"] == extra
    assert "preflight_run_id" not in execute.await_args.kwargs
