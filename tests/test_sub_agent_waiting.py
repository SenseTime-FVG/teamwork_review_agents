"""父子运行等待与取消回归；模型、工具及工作区检查均使用本地替身。"""

from __future__ import annotations

import asyncio
import json
import time

import pytest

from teamwork_review_agents.codex_model_client import CodexResponsesClient
from teamwork_review_agents.codex_model_runner import CodexModelRunner
from teamwork_review_agents.environment import SecretRedactor
from teamwork_review_agents.events import detect_events
from teamwork_review_agents.executor import AgentExecutionError, AgentExecutor
from teamwork_review_agents.model_provider_runtime import resolve_model_plan
from teamwork_review_agents.model_tools import ModelToolExecutor
from teamwork_review_agents.models import AgentResult, InvocationContext
from teamwork_review_agents.run_control import RunControl, RunStop, active_run_control
from teamwork_review_agents.state import StateStore


class WaitingHarness:
    """复用真实看门狗和 invoke_agent 等待边界，不启动模型、命令或持久化任务。"""

    def __init__(self, config, event):
        self.config = config
        self.event = event
        self.runner = CodexModelRunner(config)
        self.logs = []
        self.results = {}
        self.controls = {}

    async def run(self, name, *, idle=0.4, total=5, cancel_check=None, source=None):
        """测试只缩短计时，不改变被验证的看门狗逻辑。"""

        agent = self.config.agents["code-reviewer"].model_copy(
            update={"idle_timeout_seconds": idle, "timeout_seconds": total}
        )

        async def emit(stream, event_type, payload):
            self.logs.append((name, event_type, payload))

        async def cancel_source():
            return source

        result = await self.runner._run_guarded(
            run_id=name, root_run_id="parent", parent_run_id=None if name == "parent" else "parent",
            agent_name=name, agent=agent, repository=self.config.repositories[0],
            context=self.context(name), prompt="本地回归", environment={},
            codex_runtime_directory=self.config.runtime.codex_home, skill_files={},
            managed_sandbox=False, redactor=SecretRedactor(()), emit=emit,
            cancel_check=cancel_check, cancel_source_check=cancel_source,
            model_plan=resolve_model_plan(self.config, agent).selections,
            model_snapshot_callback=None,
        )
        self.results[name] = result
        return result

    def context(self, name):
        """生成仅供模拟委托使用的最小上下文。"""

        return InvocationContext(
            config_path=str(self.config.config_path), current_agent="code-reviewer",
            run_id=name, root_run_id="parent", event=self.event,
            active_workspace=str(self.config.repositories[0].workspace),
        )

    async def invoke(self, name, **options):
        """进入真实工具等待边界，并使用当前运行自己的进展回调。"""

        control = active_run_control.get()
        assert control is not None
        self.controls[control.run_id] = control

        async def callback(context, agent_name, task, extra_context, started_callback):
            result = await self.run(name, **options)
            if result.status != "completed":
                raise AgentExecutionError(result.error, error_code=result.error_code)
            return {"status": result.status}

        tool = ModelToolExecutor(
            config=self.config, agent=self.config.agents["code-reviewer"],
            repository=self.config.repositories[0], context=self.context(control.run_id),
            environment={}, managed_sandbox=False, cancel_check=None,
            progress_callback=control.progress, invoke_agent_callback=callback,
        )
        return await tool.execute("invoke_agent", {"agent_name": name, "task": "测试委托"})

    @staticmethod
    def completed(name):
        """生成无副作用的成功结果。"""

        return AgentResult(run_id=name, root_run_id="parent", agent_name=name, status="completed")


@pytest.fixture
def waiting_harness(configured_app_factory, snapshot_factory):
    """隔离配置与日志，不接触用户的运行数据库。"""

    config = configured_app_factory()
    config.runtime.codex.model = "gpt-test"
    event = detect_events(None, snapshot_factory(provider="github-main"), emit_initial=True)[0]
    return WaitingHarness(config, event)


@pytest.mark.asyncio
@pytest.mark.parametrize("nested", [False, True])
async def test_active_child_does_not_timeout_idle_parent(waiting_harness, nested):
    """子任务超过父 idle 仍可完成，嵌套等待也不传播子任务进展。"""

    h = waiting_harness

    async def loop(**kwargs):
        name = kwargs["run_id"]
        if name == "parent":
            await h.invoke("child")
        elif name == "child" and nested:
            await h.invoke("grandchild")
        else:
            baseline = {name: c.last_progress_at for name, c in h.controls.items()}
            deadline = time.monotonic() + 1
            while time.monotonic() < deadline:
                kwargs["progress"]()
                assert all(h.controls[name].last_progress_at == stamp for name, stamp in baseline.items())
                await asyncio.sleep(0.03)
        return h.completed(name)

    h.runner._agent_loop = loop
    result = await h.run("parent")
    assert result.status == "completed"
    assert all(r.status == "completed" for r in h.results.values())
    assert all(c.waiting_children == 0 for c in h.controls.values())
    assert h.logs == []
    assert active_run_control.get() is None


@pytest.mark.asyncio
@pytest.mark.parametrize("child_limit", ["idle", "total"])
async def test_child_owns_its_timeout(waiting_harness, child_limit):
    """子任务自身超时返回准确原因，等待它的父任务没有被误判取消。"""

    h = waiting_harness

    async def loop(**kwargs):
        if kwargs["run_id"] == "parent":
            with pytest.raises(AgentExecutionError) as caught:
                await h.invoke("child", idle=0.4, total=0.4 if child_limit == "total" else 5)
            assert caught.value.error_code == f"agent_{child_limit}_timeout"
            return h.completed("parent")
        while True:
            if child_limit == "total":
                kwargs["progress"]()
            await asyncio.sleep(0.03)

    h.runner._agent_loop = loop
    assert (await h.run("parent")).status == "completed"
    assert h.results["child"].status == "timed_out"
    assert "管理员" not in h.results["child"].error


@pytest.mark.asyncio
@pytest.mark.parametrize("child_fails", [False, True])
async def test_parent_idle_restarts_after_wait(waiting_harness, child_fails):
    """等待成功或异常结束都重新计时，但不能永久关闭父任务的 idle 检查。"""

    h = waiting_harness
    returned_at = None

    async def loop(**kwargs):
        nonlocal returned_at
        if kwargs["run_id"] == "child":
            deadline = time.monotonic() + 0.8
            while time.monotonic() < deadline:
                kwargs["progress"]()
                await asyncio.sleep(0.03)
            if child_fails:
                raise RuntimeError("模拟子任务失败")
            return h.completed("child")
        try:
            await h.invoke("child")
        except AgentExecutionError:
            assert child_fails
        returned_at = time.monotonic()
        await asyncio.Event().wait()

    h.runner._agent_loop = loop
    result = await h.run("parent")
    assert result.error_code == "agent_idle_timeout"
    assert returned_at is not None
    assert time.monotonic() - returned_at >= 0.4


@pytest.mark.asyncio
@pytest.mark.parametrize("cause", ["total", "administrator", "service_shutdown", "external"])
async def test_parent_stop_survives_child_return(waiting_harness, cause):
    """即使子调用吞掉中断并返回，父任务也不能以完成结束。"""

    h = waiting_harness
    entered = asyncio.Event()

    async def cancelled():
        return entered.is_set() and cause in {"administrator", "service_shutdown"}

    async def loop(**kwargs):
        if kwargs["run_id"] == "parent":
            try:
                await h.invoke("child")
            except AgentExecutionError:
                pass
            return h.completed("parent")
        entered.set()
        while True:
            kwargs["progress"]()
            await asyncio.sleep(0.03)

    h.runner._agent_loop = loop
    task = asyncio.create_task(h.run(
        "parent", total=0.4 if cause == "total" else 5,
        cancel_check=cancelled, source=None if cause in {"total", "external"} else cause,
    ))
    await asyncio.wait_for(entered.wait(), timeout=3)
    if cause == "external":
        task.cancel()
    result = await asyncio.wait_for(task, timeout=4)
    assert result.status == ("timed_out" if cause == "total" else "cancelled")
    expected = {
        "total": "agent_total_timeout", "administrator": "administrator_cancelled",
        "service_shutdown": "service_shutdown", "external": "run_interrupted",
    }
    assert result.error_code == expected[cause]
    assert h.results["child"].status == "cancelled"
    if cause == "total":
        assert h.results["child"].error_code == "parent_run_timeout"
    if cause != "administrator":
        assert "由管理员取消" not in h.results["child"].error


@pytest.mark.asyncio
@pytest.mark.parametrize("swallowed_result", ["success", "exception", "external"])
async def test_real_model_loop_does_not_continue_after_swallowed_cancel(waiting_harness, monkeypatch, swallowed_result):
    """使用真实模型循环，验证吞中断的委托不能导致第二次模型请求或执行命令。"""

    h = waiting_harness
    requests = []
    entered = asyncio.Event()

    async def response(self, payload, **kwargs):
        requests.append(payload)
        assert len(requests) == 1
        return {"output": [{
            "type": "function_call", "call_id": "delegate", "name": "invoke_agent",
            "arguments": json.dumps({"agent_name": "child", "task": "本地测试"}),
        }]}

    async def invoke(*args):
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            if swallowed_result == "exception":
                raise RuntimeError("模拟转换后的普通错误")
            return {"status": "completed"}

    monkeypatch.setattr(CodexResponsesClient, "create_response", response)
    h.runner.invoke_agent_callback = invoke
    task = asyncio.create_task(h.run("parent", total=5 if swallowed_result == "external" else 0.4))
    await asyncio.wait_for(entered.wait(), timeout=3)
    if swallowed_result == "external":
        task.cancel()
    result = await asyncio.wait_for(task, timeout=4)
    assert result.error_code == ("run_interrupted" if swallowed_result == "external" else "agent_total_timeout")
    assert len(requests) == 1
    assert not any(event == "turn.completed" for _, event, _ in h.logs)


@pytest.mark.asyncio
async def test_waiting_state_isolated_between_concurrent_roots(waiting_harness):
    """同一个 Runner 并发执行时，等待状态不能屏蔽其他根任务的 idle。"""

    h = waiting_harness

    async def loop(**kwargs):
        if kwargs["run_id"] == "parent":
            await h.invoke("child")
            return h.completed("parent")
        if kwargs["run_id"] == "other":
            await asyncio.Event().wait()
        deadline = time.monotonic() + 0.8
        while time.monotonic() < deadline:
            kwargs["progress"]()
            await asyncio.sleep(0.03)
        return h.completed("child")

    h.runner._agent_loop = loop
    parent, other = await asyncio.gather(h.run("parent"), h.run("other"))
    assert parent.status == "completed"
    assert other.error_code == "agent_idle_timeout"


@pytest.fixture
def executor_harness(configured_app_factory, snapshot_factory, monkeypatch):
    """保留真实执行器和数据库，模拟已有工作区及运行环境检查。"""

    import teamwork_review_agents.executor as module

    config = configured_app_factory()
    config.runtime.codex.model = "gpt-test"
    for agent in config.agents.values():
        agent.sandbox = "danger-full-access"
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setattr(module, "check_runtime_readiness", lambda *args, **kwargs: None)
    monkeypatch.setattr(module, "validate_run_workspace", lambda source, target, **kwargs: target)
    monkeypatch.setattr(module, "run_workspace_kind", lambda path: "clone")
    store = StateStore(config.database.path)
    store.initialize()
    executor = AgentExecutor(config, store)
    event = detect_events(None, snapshot_factory(provider="github-main"), emit_initial=True)[0]

    async def execute():
        return await executor.execute(
            agent_name="code-reviewer", event=event, task="本地测试任务",
            inherit_workspace=True, parent_workspace=config.repositories[0].workspace,
            idempotency_key="waiting-test",
        )

    return executor, store, execute


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["administrator", "service_shutdown"])
async def test_cancel_log_has_one_terminal_and_correct_source(executor_harness, monkeypatch, source):
    """取消来源以持久化记录为准，运行器和执行器不重复写取消终态。"""

    executor, store, execute = executor_harness

    async def run(self, **kwargs):
        store.request_cancel_run(kwargs["run_id"], source=source)
        assert await kwargs["cancel_source_check"]() == source
        await kwargs["log_callback"]("system", "run.cancelled", "运行已由管理员取消")
        return AgentResult(
            run_id=kwargs["run_id"], root_run_id=kwargs["root_run_id"],
            agent_name=kwargs["agent_name"], status="cancelled", error="运行已由管理员取消",
        )

    monkeypatch.setattr(CodexModelRunner, "run", run)
    with pytest.raises(AgentExecutionError) as caught:
        await execute()
    assert caught.value.error_code == ("administrator_cancelled" if source == "administrator" else "service_shutdown")
    detail = store.get_run(store.list_runs()[0]["run_id"])
    assert detail["cancel_source"] == source
    if source == "service_shutdown":
        assert detail["error"] == "服务停止时中断运行"
    logs = store.list_run_logs(detail["run_id"])
    assert sum(log["event_type"] == "run.cancelled" for log in logs) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["queued", "preparing"])
async def test_interrupted_pre_model_run_is_persisted(executor_harness, monkeypatch, phase):
    """父任务总超时打断排队或准备时，子记录必须终结，取消仍向上抛出。"""

    import teamwork_review_agents.executor as module

    executor, store, execute = executor_harness
    control = RunControl("parent-running")

    async def interrupt(*args, **kwargs):
        control.stop = RunStop("timed_out", "agent_total_timeout", "Agent 超过总运行时限")
        raise asyncio.CancelledError

    if phase == "queued":
        monkeypatch.setattr(executor, "_wait_for_run_capacity", interrupt)
    else:
        monkeypatch.setattr(module, "prepare_agent_workspace", interrupt)
    token = active_run_control.set(control)
    try:
        with pytest.raises(asyncio.CancelledError):
            await execute()
    finally:
        active_run_control.reset(token)
    detail = store.get_run(store.list_runs()[0]["run_id"])
    assert detail["status"] == "cancelled"
    assert detail["error_code"] == "parent_run_timeout"
    assert detail["cancel_requested"] == 0
    assert detail["cancel_source"] is None
    assert detail["finished_at"] is not None
    assert sum(log["event_type"] == "run.cancelled" for log in store.list_run_logs(detail["run_id"])) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("parent_times_out", [False, True])
async def test_full_executor_nested_wait_and_terminal_state(executor_harness, monkeypatch, parent_times_out):
    """贯通真实执行器、嵌套工具与运行器，确认数据库终态和取消日志。"""

    executor, store, execute = executor_harness
    config = executor.config
    config.agents["code-reviewer"] = config.agents["code-reviewer"].model_copy(update={
        "idle_timeout_seconds": 0.4,
        "timeout_seconds": 0.6 if parent_times_out else 6,
    })
    config.agents["security-reviewer"] = config.agents["security-reviewer"].model_copy(update={
        "idle_timeout_seconds": 0.4, "timeout_seconds": 6,
    })

    async def loop(self, **kwargs):
        if kwargs["agent_name"] == "code-reviewer":
            tool = ModelToolExecutor(
                config=config, agent=kwargs["agent"], repository=kwargs["repository"],
                context=kwargs["context"], environment={}, managed_sandbox=False,
                cancel_check=kwargs["cancel_check"], progress_callback=kwargs["progress"],
                invoke_agent_callback=self.invoke_agent_callback,
            )
            try:
                await tool.execute("invoke_agent", {"agent_name": "security-reviewer", "task": "子任务测试"})
            except AgentExecutionError:
                assert parent_times_out
            # 真实模型循环也检查这一边界，不能把子任务错误吞掉后继续执行。
            active_run_control.get().raise_if_stopped()
        else:
            deadline = time.monotonic() + 1.2
            while time.monotonic() < deadline:
                kwargs["progress"]()
                await asyncio.sleep(0.03)
        return AgentResult(
            run_id=kwargs["run_id"], root_run_id=kwargs["root_run_id"],
            parent_run_id=kwargs["parent_run_id"], agent_name=kwargs["agent_name"], status="completed",
        )

    monkeypatch.setattr(CodexModelRunner, "_agent_loop", loop)
    if parent_times_out:
        with pytest.raises(AgentExecutionError) as caught:
            await execute()
        assert caught.value.error_code == "agent_total_timeout"
    else:
        assert (await execute()).status == "completed"
    runs = {row["agent_name"]: store.get_run(row["run_id"]) for row in store.list_runs()}
    assert len(runs) == 2
    parent, child = runs["code-reviewer"], runs["security-reviewer"]
    assert child["parent_run_id"] == parent["run_id"]
    assert parent["status"] == ("timed_out" if parent_times_out else "completed")
    assert child["status"] == ("cancelled" if parent_times_out else "completed")
    if parent_times_out:
        assert child["error_code"] == "parent_run_timeout"
        assert child["cancel_source"] is None
        assert sum(log["event_type"] == "run.cancelled" for log in store.list_run_logs(child["run_id"])) == 1
