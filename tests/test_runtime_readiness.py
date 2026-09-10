"""运行环境错误须在工作区创建前阻断，并在手动重放时重新检查。"""

import subprocess
import sys
from unittest.mock import AsyncMock, Mock

import pytest

from teamwork_review_agents import managed_sandbox
from teamwork_review_agents.codex_executable import CodexRuntimeError
from teamwork_review_agents.config import RuleConfig
from teamwork_review_agents.events import create_manual_replay_event, detect_events
from teamwork_review_agents.executor import AgentExecutionError
from teamwork_review_agents.orchestrator import CycleSummary, Orchestrator
from teamwork_review_agents.models import AgentResult
from teamwork_review_agents.runtime_readiness import check_runtime_readiness


@pytest.mark.parametrize("cli_execution", [False, True])
def test_required_runtime_missing_is_not_retryable(configured_app_factory, tmp_path, cli_execution):
    """CLI 和受限模型工具都必须给出明确的程序缺失错误。"""

    config = configured_app_factory()
    config.runtime.codex_binary = str(tmp_path / "missing-codex.exe")
    with pytest.raises(CodexRuntimeError) as raised:
        check_runtime_readiness(config, config.agents["code-reviewer"], config.repositories[0], {}, cli_execution=cli_execution)
    assert raised.value.error_code == "codex_not_found"
    assert not raised.value.retryable


def test_external_unrestricted_model_does_not_require_codex(configured_app_factory, tmp_path):
    """不使用本地沙盒的外部模型不能因未安装 Codex 被误阻断。"""

    config = configured_app_factory()
    config.runtime.codex_binary = str(tmp_path / "missing-codex.exe")
    agent = config.agents["code-reviewer"].model_copy(update={"sandbox": "danger-full-access"})
    assert check_runtime_readiness(config, agent, config.repositories[0], {}, cli_execution=False) is None


def test_fixed_version_mismatch_stops_retry(configured_app_factory, monkeypatch):
    """固定版本不匹配属于明确配置问题，重复创建工作区不能解决。"""

    config = configured_app_factory()
    config.runtime.codex_binary = sys.executable
    config.runtime.expected_codex_version = "1.2.3"
    monkeypatch.setattr(
        "teamwork_review_agents.runtime_readiness.inspect_codex_binary",
        lambda *args: {"error": None, "version": "1.2.4"},
    )
    with pytest.raises(CodexRuntimeError) as raised:
        check_runtime_readiness(config, config.agents["code-reviewer"], config.repositories[0], {}, cli_execution=True)
    assert raised.value.error_code == "codex_version_mismatch"
    assert not raised.value.retryable


@pytest.mark.parametrize("cli_execution,fail_closed,blocked", [(True, False, False), (True, True, True), (False, False, True)])
def test_readiness_preserves_sandbox_failure_policy(configured_app_factory, monkeypatch, cli_execution, fail_closed, blocked):
    """CLI 保留原生沙盒回退设置，受限模型工具始终要求外层沙盒。"""

    config = configured_app_factory()
    config.runtime.codex_binary = sys.executable
    config.runtime.expected_codex_version = None
    config.runtime.managed_sandbox.fail_closed = fail_closed
    monkeypatch.setattr(
        "teamwork_review_agents.runtime_readiness.inspect_managed_sandbox",
        lambda *args, **kwargs: managed_sandbox.ManagedSandboxInspection(
            available=False, platform="Windows", backend="windows",
            error="缺少沙盒能力", error_code="sandbox_capability_missing", retryable=False,
        ),
    )
    if blocked:
        with pytest.raises(CodexRuntimeError) as raised:
            check_runtime_readiness(config, config.agents["code-reviewer"], config.repositories[0], {}, cli_execution=cli_execution)
        assert not raised.value.retryable
    else:
        assert check_runtime_readiness(config, config.agents["code-reviewer"], config.repositories[0], {}, cli_execution=cli_execution) is not None


@pytest.mark.parametrize("probe_result, retryable", [("missing-capability", False), ("timeout", True)])
def test_sandbox_failures_recover_without_restart(monkeypatch, probe_result, retryable):
    """失败不进入缓存，修复能力或短时超时恢复后立即重新探测。"""

    managed_sandbox._inspect_cached.cache_clear()
    monkeypatch.setattr(managed_sandbox, "_platform_backend", lambda: ("Windows", "windows"))
    calls = []

    def probe(command, **kwargs):
        """先模拟一次探测失败，再返回有效能力。"""

        calls.append(command)
        if len(calls) == 1:
            if probe_result == "timeout":
                raise subprocess.TimeoutExpired(command, 5)
            return subprocess.CompletedProcess(command, 0, "sandbox help", "")
        return subprocess.CompletedProcess(command, 0, "--permission-profile PROFILE", "")

    monkeypatch.setattr(managed_sandbox.subprocess, "run", probe)
    try:
        failed = managed_sandbox.inspect_managed_sandbox(sys.executable)
        assert not failed.available
        assert failed.retryable is retryable
        assert failed.error_code == ("sandbox_probe_failed" if retryable else "sandbox_capability_missing")
        assert managed_sandbox.inspect_managed_sandbox(sys.executable).available
        assert len(calls) == 2
    finally:
        managed_sandbox._inspect_cached.cache_clear()


async def test_missing_codex_stops_before_git_and_manual_replay_rechecks(
    configured_app_factory, snapshot_factory, tmp_path, monkeypatch,
):
    """真实事件与执行器链路只失败一次，修复后的手动事件可再次进入工作区准备。"""

    config = configured_app_factory()
    config.runtime.codex_binary = str(tmp_path / "missing-codex.exe")
    config.runtime.codex.execution_mode = "cli"
    config.runtime.event_retry_count = 2
    config.agents["code-reviewer"].sandbox = "danger-full-access"
    snapshot = snapshot_factory(provider="github-main")
    event = detect_events(None, snapshot, emit_initial=True)[0]
    config.rules = [RuleConfig(name="test-review", events=[event.type], agents=["code-reviewer"])]
    orchestrator = Orchestrator(config, recover_interrupted=False)
    orchestrator.store.save_snapshot_and_events(snapshot, [event])
    prepare = Mock(side_effect=CodexRuntimeError("测试在准备入口停止", error_code="test_stop"))
    monkeypatch.setattr("teamwork_review_agents.executor.prepare_change_request_workspace", prepare)

    await orchestrator.process_events(CycleSummary())
    await orchestrator.process_events(CycleSummary())
    prepare.assert_not_called()
    record = orchestrator.store.get_event_detail(event.id)
    assert record["status"] == "failed"
    assert record["attempts"] == 1
    assert record["error_code"] == "codex_not_found"
    assert not record["retryable"]
    runs = orchestrator.store.list_runs()
    assert len(runs) == 1
    run = orchestrator.store.get_run(runs[0]["run_id"])
    assert run["workspace_status"] == "not-created"
    assert run["error_code"] == "codex_not_found"
    assert not run["retryable"]
    assert orchestrator.store.agent_run_failure(run["idempotency_key"])["retryable"] == 0
    assert orchestrator.store.pending_events() == []
    assert not orchestrator.store.claim_event(event.id, 3)
    with pytest.raises(AgentExecutionError) as raised:
        await orchestrator.executor.execute(
            agent_name="code-reviewer", event=event,
            idempotency_key=run["idempotency_key"], rule_name="test-review",
        )
    assert raised.value.error_code == "codex_not_found"
    assert not raised.value.retryable
    assert orchestrator.store.get_run(run["run_id"])["attempts"] == 1

    # 改用存在的程序，仅验证重新检查和进入准备阶段，不执行外部 Git 或模型。
    config.runtime.codex_binary = sys.executable
    config.runtime.expected_codex_version = None
    replay = create_manual_replay_event(event)
    orchestrator.store.save_snapshot_and_events(snapshot, [replay])
    await orchestrator.process_events(CycleSummary())
    prepare.assert_called_once()
    assert orchestrator.store.get_event_detail(replay.id)["error_code"] == "test_stop"
    assert orchestrator.store.get_event_detail(event.id)["error_code"] == "codex_not_found"


async def test_runtime_failure_does_not_disable_retry_for_other_event_errors(
    configured_app_factory, snapshot_factory, monkeypatch,
):
    """同一事件包含确定性 Agent 错误和暂时性 CI 错误时，仍须保留 CI 重试。"""

    config = configured_app_factory()
    snapshot = snapshot_factory(provider="github-main")
    event = detect_events(None, snapshot, emit_initial=True)[0]
    config.repositories[0].preflight.enabled = True
    config.rules = [
        RuleConfig(name="direct", events=[event.type], agents=["code-reviewer"]),
        RuleConfig(name="with-ci", events=[event.type], agents=["code-reviewer"], run_preflight=True),
    ]
    orchestrator = Orchestrator(config, recover_interrupted=False)
    orchestrator.store.save_snapshot_and_events(snapshot, [event])
    monkeypatch.setattr(orchestrator.preflight, "ensure_passed", AsyncMock(side_effect=RuntimeError("CI 临时错误")))
    monkeypatch.setattr(orchestrator.executor, "execute", AsyncMock(return_value=AgentResult(
        run_id="failed-runtime", root_run_id="failed-runtime", agent_name="code-reviewer",
        status="failed", error="缺少 Codex", error_code="codex_not_found", retryable=False,
    )))
    await orchestrator.process_events(CycleSummary())
    record = orchestrator.store.get_event_detail(event.id)
    assert record["status"] == "failed"
    assert record["retryable"]
    assert "CI 临时错误" in record["error"]
    assert "缺少 Codex" in record["error"]
