"""自动 CI 磁盘不足的暂停、重启恢复与误识别边界。"""

from __future__ import annotations

import asyncio
import errno
import time
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from teamwork_review_agents import disk_space
from teamwork_review_agents.models import PreflightResult
from teamwork_review_agents.orchestrator import CycleSummary, Orchestrator
from teamwork_review_agents.preflight import StepExecutionOutcome, preflight_idempotency_key
from teamwork_review_agents.runtime import BackgroundRuntime

from test_preflight_orchestration import FakeAgentExecutor, enqueue_discovered, preflight_config


@pytest.fixture
def disk_ci(tmp_path, snapshot_factory, monkeypatch):
    """保留真实 SQLite 和编排，仅替换磁盘、Git、命令及平台网络。"""

    config = preflight_config(tmp_path)
    config.runtime.event_retry_count = 0
    runner = Orchestrator(config)
    event = enqueue_discovered(runner, snapshot_factory)
    agents = FakeAgentExecutor()
    runner.executor = agents
    control = SimpleNamespace(free=1024**3, error=True, checkouts=0, outcome=None, output="")
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    monkeypatch.setattr(disk_space.shutil, "disk_usage", lambda path: SimpleNamespace(free=control.free))

    @contextmanager
    def worktree(*args, **kwargs):
        """磁盘异常发生在 checkout，尚未进入测试。"""

        control.checkouts += 1
        if control.error:
            raise OSError(errno.ENOSPC, "No space left on device")
        yield tmp_path

    async def steps(*args, **kwargs):
        """可模拟错误位于输出前部，末尾已被截断的情况。"""

        if control.output:
            await kwargs["on_output"](control.output)
        return control.outcome or StepExecutionOutcome(status="success")

    monkeypatch.setattr("teamwork_review_agents.preflight.temporary_change_request_worktree", worktree)
    monkeypatch.setattr("teamwork_review_agents.preflight.execute_preflight_steps", steps)
    monkeypatch.setattr("teamwork_review_agents.preflight.PreflightExecutor._set_remote_status", AsyncMock(return_value=None))
    monkeypatch.setattr("teamwork_review_agents.preflight.PreflightExecutor._sync_failure_comment_safely", AsyncMock())
    monkeypatch.setattr("teamwork_review_agents.preflight.PreflightExecutor._queue_log_delivery", AsyncMock())
    return runner, event, agents, control


def expire_wait(runner, run_id):
    """推进持久化检查时间，不依赖真实一分钟等待。"""

    detail = runner.store.get_preflight_run(run_id)
    runner.store.update_disk_wait(run_id, {**detail["disk_wait"], "next_check_at": 0})
    runner._next_disk_check = 0


async def test_disk_wait_survives_restart_and_resumes_without_retry_budget(disk_ci):
    """即使普通重试次数为零，资源恢复也会重跑，并且等待期间不会重启命令。"""

    runner, event, agents, control = disk_ci
    await runner.process_events(CycleSummary())
    detail = runner.store.get_event_detail(event.id)
    run_id = detail["preflight_run_id"]
    assert detail["status"] == "waiting_for_space"
    assert detail["attempts"] == 0
    assert detail["disk_wait"]["volumes"][0]["free_bytes"] == 1024**3
    assert runner.store.get_preflight_run(run_id)["attempts"] == 0
    assert runner.store.get_preflight_diagnosis(run_id) is None
    assert not agents.calls
    await runner.process_events(CycleSummary())
    assert control.checkouts == 1

    restarted = Orchestrator(runner.config)
    restarted.executor = agents
    assert restarted.store.get_event_detail(event.id)["status"] == "waiting_for_space"
    expire_wait(restarted, run_id)
    await restarted.process_events(CycleSummary())
    assert control.checkouts == 1
    assert restarted.store.get_preflight_run(run_id)["disk_wait"]["next_check_at"] > time.time()

    control.free = 6 * 1024**3
    control.error = False
    expire_wait(restarted, run_id)
    await restarted.process_events(CycleSummary())
    assert control.checkouts == 2
    assert agents.calls == ["reviewer"]
    assert restarted.store.get_event_detail(event.id)["status"] == "completed"
    assert restarted.store.get_preflight_run(run_id)["status"] == "success"
    assert restarted.store.disk_wait_delay() is None


@pytest.mark.parametrize("change", ["head", "closed", "disabled"])
async def test_disk_recovery_does_not_replay_obsolete_request(disk_ci, change):
    """等待期间更新、关闭或停用的请求不会恢复旧审核。"""

    runner, event, agents, control = disk_ci
    await runner.process_events(CycleSummary())
    run_id = runner.store.get_event_detail(event.id)["preflight_run_id"]
    if change == "disabled":
        runner.config.repositories[0].enabled = False
    else:
        snapshot = event.current_snapshot.model_copy(update={"head_sha": "c" * 40} if change == "head" else {"state": "closed"})
        runner.store.save_snapshot_and_events(snapshot, [])
    control.free = 6 * 1024**3
    expire_wait(runner, run_id)
    await runner.process_events(CycleSummary())
    assert runner.store.get_event_detail(event.id)["status"] == "unmatched"
    assert runner.store.get_preflight_run(run_id)["status"] == "superseded"
    assert control.checkouts == 1 and not agents.calls


@pytest.mark.parametrize("free,expected", [(1024**3, "waiting_for_space"), (6 * 1024**3, "completed")])
async def test_subprocess_disk_text_requires_real_low_space(disk_ci, free, expected):
    """预期异常文本不能让正常失败进入无限资源重试；真实低空间不依赖日志尾部。"""

    runner, event, agents, control = disk_ci
    control.error = False
    control.free = free
    control.output = "error: ENOSPC: no space left on device\n" + "tail\n" * 200
    control.outcome = StepExecutionOutcome(status="failure", failed_step="test", exit_code=1, output="tail only")
    await runner.process_events(CycleSummary())
    assert runner.store.get_event_detail(event.id)["status"] == expected
    assert not agents.calls


async def test_ordinary_assertion_failure_is_not_disk_wait(disk_ci):
    """主机空间低也不改变没有 ENOSPC 证据的测试失败。"""

    runner, event, agents, control = disk_ci
    control.error = False
    control.outcome = StepExecutionOutcome(status="failure", exit_code=1, output="AssertionError")
    await runner.process_events(CycleSummary())
    detail = runner.store.get_event_detail(event.id)
    assert detail["status"] == "completed" and detail["preflight_status"] == "failure"
    assert runner.store.disk_wait_delay() is None


async def test_duplicate_event_reuses_wait_without_consuming_attempts(disk_ci):
    """同一源提交又收到事件时共享等待，不再次执行或反复扣减 CI 计数。"""

    runner, event, agents, control = disk_ci
    await runner.process_events(CycleSummary())
    newer = event.model_copy(update={"id": "another-event", "batch_id": "another-batch"})
    runner.store.save_snapshot_and_events(event.current_snapshot, [newer])
    await runner.process_events(CycleSummary())
    for item in (event, newer):
        detail = runner.store.get_event_detail(item.id)
        assert detail["status"] == "waiting_for_space" and detail["attempts"] == 0
    assert control.checkouts == 1
    assert runner.store.list_preflight_runs()[0]["attempts"] == 0


async def test_wait_log_write_failure_does_not_erase_persisted_wait(disk_ci, monkeypatch):
    """资源状态落库之后，日志磁盘再次写满也不能触发普通重试覆盖状态。"""

    runner, event, agents, control = disk_ci
    original = runner.preflight._append_log

    async def append(*args, **kwargs):
        """只阻断挂起后的追加日志。"""
        if kwargs.get("event_type") == "disk_wait":
            raise OSError(errno.ENOSPC, "No space left on device")
        return await original(*args, **kwargs)

    monkeypatch.setattr(runner.preflight, "_append_log", append)
    await runner.process_events(CycleSummary())
    assert runner.store.get_event_detail(event.id)["status"] == "waiting_for_space"
    assert control.checkouts == 1


async def test_disk_wait_filters_and_details_are_exposed_by_api(disk_ci, tmp_path):
    """状态筛选允许等待值，事件和 CI 详情返回可直接渲染的空间信息。"""

    from fastapi.testclient import TestClient
    from teamwork_review_agents.webapp import create_app
    from test_environment_and_web import write_config

    runner, event, agents, control = disk_ci
    await runner.process_events(CycleSummary())
    app = create_app(write_config(tmp_path), start_scheduler=False)
    with TestClient(app) as client:
        events = client.get("/api/events?status=waiting_for_space").json()
        assert events[0]["event_id"] == event.id
        assert events[0]["disk_wait"]["required_bytes"] == disk_space.DISK_RECOVERY_BYTES
        runs = client.get("/api/preflight-runs?status_group=waiting").json()
        assert len(runs) == 1 and runs[0]["status"] == "waiting_for_space"
        detail = client.get(f"/api/preflight-runs/{runs[0]['run_id']}").json()
        assert detail["disk_wait"]["volumes"][0]["free_bytes"] == control.free


async def test_legacy_exhausted_disk_error_is_recovered(disk_ci):
    """升级后能接管仍打开且源提交一致的旧资源错误，无需管理员逐条触发。"""

    runner, event, agents, control = disk_ci
    runner.store.claim_event(event.id, 1)
    reservation = runner.store.begin_preflight_run(
        proposed_run_id="legacy", idempotency_key=preflight_idempotency_key(runner.config, event),
        event_id=event.id, repository_id=event.repository_id, number=event.number,
        head_sha=event.current_snapshot.head_sha, config_revision=runner.config.revision, max_attempts=1,
    )
    result = PreflightResult(run_id=reservation.run_id, repository_id=event.repository_id,
                            number=event.number, head_sha=event.current_snapshot.head_sha,
                            status="error", error="fatal: No space left on device")
    runner.store.finish_preflight_run(result)
    runner.store.finish_event(event.id, error=result.error)
    await runner.process_events(CycleSummary())
    assert control.checkouts == 0
    assert runner.store.get_event_detail(event.id)["status"] == "waiting_for_space"
    assert runner.store.get_event_detail(event.id)["attempts"] == 0


def test_disk_probe_failure_never_means_recovery(tmp_path, monkeypatch):
    """目录尚未创建时检查父目录；查询失败保留错误并继续等待。"""

    def fail(path):
        """模拟磁盘不可读。"""
        raise PermissionError("无法读取磁盘")

    monkeypatch.setattr(disk_space.shutil, "disk_usage", fail)
    wait = disk_space.probe_disk_space([tmp_path / "missing" / "checkout"])
    assert not wait.recovered
    assert wait.volumes[0].path == str(tmp_path)
    assert wait.volumes[0].free_bytes is None
    assert wait.volumes[0].error


async def test_dispatch_loop_checks_disk_without_new_events():
    """没有扫描通知时，后台仍能在持久化截止时间唤醒。"""

    runtime = BackgroundRuntime.__new__(BackgroundRuntime)
    runtime._stop_event = asyncio.Event()
    runtime._dispatch_event = asyncio.Event()
    runtime.store = SimpleNamespace(disk_wait_delay=lambda: 0.01)

    async def dispatch():
        """一次资源检查完成后结束测试后台。"""
        runtime._stop_event.set()

    runtime._run_dispatch_cycle = AsyncMock(side_effect=dispatch)
    await asyncio.wait_for(runtime._dispatch_loop(), timeout=1)
    runtime._run_dispatch_cycle.assert_awaited_once()
