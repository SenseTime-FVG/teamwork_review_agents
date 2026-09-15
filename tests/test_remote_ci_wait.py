"""使用隔离数据库与平台替身验证 CI 等待，不访问真实仓库或启动真实 Agent。"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from types import SimpleNamespace

import pytest

from teamwork_review_agents.config import EnvironmentVariable
from teamwork_review_agents.codex_runner import CodexRunner
from teamwork_review_agents.events import detect_events
from teamwork_review_agents.models import AgentResult, InvocationContext
from teamwork_review_agents.remote_ci import RemoteCIWaiter, ci_snapshot, validate_ci_arguments
from teamwork_review_agents.run_waits import RunWaits, mcp_wait_timeout
from teamwork_review_agents.state import StateStore
from teamwork_review_agents.codex_model_runner import CodexModelRunner
from teamwork_review_agents.environment import SecretRedactor
from teamwork_review_agents.model_provider_runtime import resolve_model_plan
from teamwork_review_agents.model_tools import ModelToolExecutor


SHA = "a" * 40


def reserve(store, *, run_id="root", parent=None):
    """建立真实运行占位，测试不通过绕开运行校验来制造等待。"""

    result = store.begin_agent_run(
        proposed_run_id=run_id, root_run_id="root", parent_run_id=parent,
        idempotency_key=run_id, event_id=None, rule_name=None,
        agent_name="code-reviewer", resource_key="demo:1", prompt="回归测试", max_attempts=3,
    )
    assert result is not None
    store.mark_agent_run_running(run_id)
    return result


@pytest.fixture
def ci_case(configured_app_factory, snapshot_factory):
    """配置短等待上限，但保留生产数据模型和 Token 解析优先级。"""

    config = configured_app_factory()
    config.runtime = config.runtime.model_copy(update={"remote_ci_wait_timeout_seconds": 0.2})
    store = StateStore(config.database.path)
    store.initialize()
    reserve(store)
    event = detect_events(None, snapshot_factory(repository_id="demo", provider="github-main"), emit_initial=True)[0]
    context = InvocationContext(config_path=str(config.config_path), current_agent="code-reviewer", run_id="root", root_run_id="root", event=event)
    return config, store, context


class Platform:
    """记录只读请求，允许逐次返回确定的平台状态。"""

    def __init__(self, kind="github", *, state="success", pages=1, total=1, sha=SHA):
        self.config = SimpleNamespace(kind=kind)
        self.state = state
        self.pages = pages
        self.total = total
        self.sha = sha
        self.calls = []
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        self.closed = True

    async def get_json(self, path, **kwargs):
        """只允许生产逻辑声明的固定 GET 端点。"""

        self.calls.append((path, kwargs))
        if self.state == "exception":
            raise RuntimeError("fake-provider-secret")
        if "/pulls/" in path:
            return {"state": "open", "head": {"sha": self.sha}}
        if "/merge_requests/" in path:
            return {"state": "opened", "sha": self.sha, "head_pipeline": {
                "sha": self.sha, "status": self.state,
            }}
        if path.endswith("/check-runs"):
            page = kwargs["params"]["page"]
            count = 100 if page < self.pages else self.total
            state = self.state if page == self.pages else "success"
            return {"total_count": 100 * (self.pages - 1) + self.total, "check_runs": [
                {"head_sha": SHA, "status": "in_progress" if state == "pending" else "completed", "conclusion": state}
                for _ in range(count)
            ]}
        if path.endswith("/status"):
            return {"total_count": 0, "state": "pending", "sha": SHA}
        raise AssertionError(path)


@pytest.mark.parametrize("kind,state,expected", [
    ("github", "success", "success"), ("github", "failure", "failure"),
    ("github", "pending", "pending"), ("github", "unknown", "pending"),
    ("gitlab", "success", "success"), ("gitlab", "failed", "failure"),
    ("gitlab", "manual", "pending"), ("gitlab", "skipped", "pending"),
])
async def test_ci_platform_states(ci_case, kind, state, expected):
    """仅已确认成功才能通过，未知和手动等待不能跳过门禁。"""

    config, _, _ = ci_case
    result = await ci_snapshot(Platform(kind, state=state), config.repositories[0], 123, SHA)
    assert result["status"] == expected


async def test_pagination_and_sha_guard(ci_case):
    """第二页的失败、空检查和源 SHA 漂移均不能被第一项成功掩盖。"""

    config, _, _ = ci_case
    repo = config.repositories[0]
    platform = Platform(pages=2, state="failure")
    assert (await ci_snapshot(platform, repo, 123, SHA))["status"] == "failure"
    assert len([path for path, _ in platform.calls if path.endswith("check-runs")]) == 2
    assert (await ci_snapshot(Platform(total=0), repo, 123, SHA))["status"] == "pending"
    changed = Platform(sha="b" * 40)
    assert (await ci_snapshot(changed, repo, 123, SHA))["status"] == "head_changed"
    assert len(changed.calls) == 1


async def test_old_gitlab_pipeline_cannot_pass(ci_case):
    """MR 已更新但 head_pipeline 尚未刷新时不复用旧成功。"""

    config, _, _ = ci_case
    platform = Platform("gitlab")

    async def get_json(*args, **kwargs):
        return {"state": "opened", "sha": SHA, "head_pipeline": {"sha": "b" * 40, "status": "success"}}

    platform.get_json = get_json
    assert (await ci_snapshot(platform, config.repositories[0], 123, SHA))["status"] == "pending"


@pytest.mark.parametrize("number,sha", [(True, SHA), (0, SHA), (1, "main"), (1, "https://other.example"), ("1", SHA)])
def test_ci_rejects_invalid_arguments(number, sha):
    """调用方不能通过参数注入路径、URL 或命令。"""

    with pytest.raises(ValueError):
        validate_ci_arguments(number, sha)


@pytest.mark.parametrize("state", ["pending", "exception"])
async def test_fixed_deadline_and_safe_timeout(ci_case, monkeypatch, state):
    """有效的排队状态和查询异常都不延长期限，错误正文不泄漏。"""

    config, store, context = ci_case
    platform = Platform(state=state)
    monkeypatch.setattr("teamwork_review_agents.remote_ci.create_provider", lambda *args, **kwargs: platform)
    result = await RemoteCIWaiter(config, store, poll_seconds=0.02).wait(context, 123, SHA)
    assert result["status"] == "timed_out"
    assert platform.closed
    waits = RunWaits(store)
    record = waits.list("root")[0]
    assert record["deadline"] - record["started_at"] == pytest.approx(0.2)
    assert waits.state("root") == (False, True)
    assert "fake-provider-secret" not in json.dumps(store.list_run_logs("root"))
    first_deadline = record["deadline"]
    assert (await RemoteCIWaiter(config, store).wait(context, 123, SHA))["status"] == "timed_out"
    assert waits.list("root")[0]["deadline"] == first_deadline


async def test_repository_token_and_timeout_override(ci_case, monkeypatch):
    """GitHub 认证仍使用仓库 Token，等待上限允许仓库覆盖全局。"""

    config, store, context = ci_case
    repo = config.repositories[0]
    repo.environment["GITHUB_TOKEN"] = EnvironmentVariable(value="repo-fixture-secret", secret=True)
    repo.remote_ci_wait_timeout_seconds = 600
    config.environment.global_variables["GITHUB_TOKEN"] = EnvironmentVariable(value="global-fixture-secret", secret=True)
    platform = Platform()

    def factory(name, provider, scanner, *, token):
        assert token == "repo-fixture-secret"
        assert name == "github-main"
        return platform

    monkeypatch.setattr("teamwork_review_agents.remote_ci.create_provider", factory)
    assert (await RemoteCIWaiter(config, store).wait(context, 123, SHA))["status"] == "success"
    record = RunWaits(store).list("root")[0]
    assert record["deadline"] - record["started_at"] == 600
    assert record["status"] == "finished"
    assert "secret" not in json.dumps(record)
    assert mcp_wait_timeout(config) > 600


async def test_cancellation_and_stopped_run(ci_case, monkeypatch):
    """取消仍可中断等待，停止后的运行不能重新登记等待。"""

    config, store, context = ci_case
    monkeypatch.setattr("teamwork_review_agents.remote_ci.create_provider", lambda *args, **kwargs: Platform(state="pending"))

    async def cancelled():
        return True

    with pytest.raises(asyncio.CancelledError):
        await RemoteCIWaiter(config, store).wait(context, 123, SHA, cancel_check=cancelled)
    assert RunWaits(store).state("root") == (False, False)
    store.request_cancel_run("root")
    with pytest.raises(RuntimeError, match="运行已停止"):
        RunWaits(store).begin("root", "ci", f"123:{SHA}", 50)


@pytest.mark.parametrize("kind", ["child", "ci"])
async def test_cli_root_ignores_total_and_respects_registered_wait(ci_case, tmp_path, kind):
    """真实本地替身 CLI 等待超过原总时限与 idle 后仍能完成。"""

    config, store, context = ci_case
    fake = tmp_path / "fake-codex"
    fake.write_text(f"#!{sys.executable}\nimport sys,time,json\nsys.stdin.read()\ntime.sleep(1.3)\nprint(json.dumps({{'type':'turn.completed'}}),flush=True)\n", encoding="utf-8")
    fake.chmod(0o755)
    config.runtime.codex_binary = str(fake)
    config.runtime.codex.execution_mode = "cli"
    config.runtime.managed_sandbox.enabled = False
    agent = config.agents["code-reviewer"].model_copy(update={"timeout_seconds": 0.4, "idle_timeout_seconds": 0.4})
    RunWaits(store).begin("root", kind, "test", 3)
    result = await CodexRunner(config).run(run_id="root", root_run_id="root", parent_run_id=None,
                                         agent_name="code-reviewer", agent=agent, repository=config.repositories[0],
                                         context=context, prompt="仅运行测试替身")
    assert result.status == "completed"


@pytest.mark.parametrize("ci_timeout", [True, False])
async def test_cli_ci_timeout_or_child_total_stops_without_retry(ci_case, tmp_path, ci_timeout):
    """CI 到期和子任务总时限都终止真实替身进程，且不重试。"""

    config, store, context = ci_case
    fake = tmp_path / "fake-codex-timeout"
    fake.write_text(f"#!{sys.executable}\nimport sys,time\nsys.stdin.read()\ntime.sleep(30)\n", encoding="utf-8")
    fake.chmod(0o755)
    config.runtime.codex_binary = str(fake)
    config.runtime.codex.execution_mode = "cli"
    config.runtime.managed_sandbox.enabled = False
    agent = config.agents["code-reviewer"].model_copy(update={"timeout_seconds": 0.4, "idle_timeout_seconds": 5})
    if ci_timeout:
        RunWaits(store).begin("root", "ci", "test", 0.3)
    result = await CodexRunner(config).run(run_id="root", root_run_id="root", parent_run_id=None if ci_timeout else "ancestor",
                                         agent_name="code-reviewer", agent=agent, repository=config.repositories[0],
                                         context=context, prompt="仅运行测试替身")
    assert result.status == "timed_out"
    assert result.error_code == ("remote_ci_timeout" if ci_timeout else "agent_total_timeout")
    assert result.retryable is False
    store.finish_agent_run(result)
    assert store.begin_agent_run(proposed_run_id="new", root_run_id=None, parent_run_id=None,
                                 idempotency_key="root", event_id=None, rule_name=None, agent_name="code-reviewer",
                                 resource_key="demo:1", prompt="不可重放", max_attempts=3) is None


@pytest.mark.parametrize("succeeds", [False, True])
async def test_model_ci_wait_does_not_trigger_idle_or_continue_after_timeout(ci_case, monkeypatch, succeeds):
    """真实模型看门狗等待超过 idle；到期即使循环吞取消，也不能返回成功或执行下一步。"""

    config, store, context = ci_case
    config.runtime.remote_ci_wait_timeout_seconds = 0.6
    config.runtime.codex.model = "gpt-test"
    runner = CodexModelRunner(config)
    agent = config.agents["code-reviewer"].model_copy(update={"timeout_seconds": 0.1, "idle_timeout_seconds": 0.1})
    platform = Platform(state="success" if succeeds else "pending")
    original_get = platform.get_json

    async def delayed_get(*args, **kwargs):
        """只延迟首个请求，模拟低于平台超时但长于模型 idle 的正常查询。"""

        if not platform.calls:
            await asyncio.sleep(0.4)
        return await original_get(*args, **kwargs)

    platform.get_json = delayed_get
    monkeypatch.setattr("teamwork_review_agents.remote_ci.create_provider", lambda *args, **kwargs: platform)
    continued = []

    async def loop(**kwargs):
        """模拟一个等待工具后还有发布动作的模型回合。"""

        tool = ModelToolExecutor(config=config, agent=agent, repository=config.repositories[0], context=context,
                                 environment={}, managed_sandbox=False, cancel_check=None,
                                 progress_callback=kwargs["progress"], invoke_agent_callback=None)
        try:
            result = await tool.execute("wait_for_ci", {"number": 123, "expected_head_sha": SHA})
            assert result["status"] == "success"
            continued.append("after-ci")
        except asyncio.CancelledError:
            pass
        return AgentResult(run_id="root", root_run_id="root", agent_name="code-reviewer", status="completed")

    runner._agent_loop = loop

    async def emit(*args):
        """测试不输出模型日志到终端。"""

    result = await runner._run_guarded(
        run_id="root", root_run_id="root", parent_run_id=None, agent_name="code-reviewer",
        agent=agent, repository=config.repositories[0], context=context, prompt="回归测试", environment={},
        codex_runtime_directory=config.runtime.codex_home, skill_files={}, managed_sandbox=False,
        redactor=SecretRedactor(()), emit=emit, cancel_check=None, cancel_source_check=None,
        model_plan=resolve_model_plan(config, agent).selections, model_snapshot_callback=None,
    )
    assert result.status == ("completed" if succeeds else "timed_out")
    assert continued == (["after-ci"] if succeeds else [])
    if not succeeds:
        assert result.error_code == "remote_ci_timeout"
        assert result.retryable is False


def test_ci_defaults_and_round_trip(configured_app_factory):
    """旧配置默认 30 分钟，仓库覆盖可序列化且不改变本地 CI 限制。"""

    config = configured_app_factory()
    assert config.runtime.remote_ci_wait_timeout_seconds == 1800
    assert config.repositories[0].remote_ci_wait_timeout_seconds is None
    config.repositories[0].remote_ci_wait_timeout_seconds = 3600
    from teamwork_review_agents.config import AppConfig

    restored = AppConfig.model_validate({**config.model_dump(), "config_path": config.config_path, "revision": config.revision})
    assert restored.repositories[0].remote_ci_wait_timeout_seconds == 3600


def test_cli_child_ci_timeout_is_visible_to_root(ci_case):
    """子任务等待 CI 超时后，外层 CLI 不能接着清理该 PR。"""

    _, store, _ = ci_case
    reserve(store, run_id="child", parent="root")
    waits = RunWaits(store)
    waits.begin("child", "ci", "123", 30)
    waits.update("child", "ci", "123", "timed_out")
    assert waits.state("root")[1] is True


@pytest.mark.parametrize("ci_timeout", [False, True])
def test_broker_shutdown_preserves_ci_timeout_source(ci_case, monkeypatch, ci_timeout):
    """超时收尾不是管理员取消，普通代理中断仍保留原有取消行为。"""

    from teamwork_review_agents.codex_runner import encode_invocation_context
    from teamwork_review_agents.mcp_bridge import _request_current_run_cancellation

    config, store, context = ci_case
    monkeypatch.setenv("TEAMWORK_CONFIG_PATH", str(config.config_path))
    monkeypatch.setenv("TEAMWORK_INVOCATION_CONTEXT", encode_invocation_context(context))
    if ci_timeout:
        waits = RunWaits(store)
        waits.begin("root", "ci", "123", 30)
        waits.update("root", "ci", "123", "timed_out")
    assert _request_current_run_cancellation() is ci_timeout
    assert store.agent_run_cancel_source("root") == (None if ci_timeout else "administrator")
    assert store.agent_run_cancel_requested("root") is (not ci_timeout)


async def test_broker_releases_hanging_ci_request_on_timeout(ci_case, monkeypatch):
    """CI 到期后不回到模型；Broker 停止时取消挂起工具并退出，不伪造人工取消。"""

    from teamwork_review_agents import mcp_bridge
    from teamwork_review_agents.codex_runner import encode_invocation_context

    config, store, context = ci_case
    monkeypatch.setenv("TEAMWORK_CONFIG_PATH", str(config.config_path))
    monkeypatch.setenv("TEAMWORK_INVOCATION_CONTEXT", encode_invocation_context(context))
    channel = mcp_bridge.McpBridgeChannel.create("root", response_timeout_seconds=3)
    monkeypatch.setattr(mcp_bridge, "channel_from_environment", lambda: channel)
    request_id = "00000000-0000-0000-0000-000000000001"
    mcp_bridge._atomic_write_json(channel.requests_directory / f"{request_id}.request.json", {})
    released = asyncio.Event()

    async def hanging_request(*args):
        """模拟已落库超时、仍等待外部看门狗停止的 MCP 工具。"""

        waits = RunWaits(store)
        waits.begin("root", "ci", "123", 30)
        waits.update("root", "ci", "123", "timed_out")
        mcp_bridge._atomic_write_json(channel.stop_path, {})
        try:
            await asyncio.Event().wait()
        finally:
            released.set()

    monkeypatch.setattr(mcp_bridge, "_handle_request", hanging_request)
    try:
        await asyncio.wait_for(mcp_bridge.run_broker(), timeout=3)
        assert released.is_set()
        assert store.agent_run_cancel_source("root") is None
        assert RunWaits(store).state("root")[1]
    finally:
        channel.cleanup()


async def test_cli_child_ci_timeout_stops_embedded_parent_chain(ci_case):
    """混用 CLI 子任务与内嵌父任务时，CI 超时不能作为普通工具错误返回模型。"""

    from teamwork_review_agents.executor import AgentExecutionError
    from teamwork_review_agents.run_control import RunControl, active_run_control

    config, _, context = ci_case
    root = RunControl("ancestor")
    control = RunControl("root", parent=root)

    async def invoke(*args):
        """模拟完整 CLI 子运行已由执行器判定为不可重试 CI 超时。"""

        raise AgentExecutionError("等待远端 CI 超时", error_code="remote_ci_timeout", retryable=False)

    tool = ModelToolExecutor(config=config, agent=config.agents["code-reviewer"], repository=config.repositories[0],
                             context=context, environment={}, managed_sandbox=False, cancel_check=None,
                             progress_callback=lambda: None, invoke_agent_callback=invoke)
    token = active_run_control.set(control)
    try:
        with pytest.raises(asyncio.CancelledError):
            await tool.execute("invoke_agent", {"agent_name": "security-reviewer", "task": "CI 回归"})
        assert control.stop.error_code == "remote_ci_timeout"
        assert root.stop.error_code == "remote_ci_timeout"
        assert control.waiting_children == 0
    finally:
        active_run_control.reset(token)
