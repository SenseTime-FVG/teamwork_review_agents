"""SQLite 幂等与资源租约测试。"""

import sqlite3

import pytest

from teamwork_review_agents.events import create_manual_activity_event, detect_events
from teamwork_review_agents.models import (
    AgentResult,
    ChangeRequestActivity,
    PreflightResult,
)
from teamwork_review_agents.state import StateStore


def test_connection_context_closes_on_success_and_failure(tmp_path) -> None:
    """状态存储连接在正常返回和异常回滚后都必须立即关闭。"""

    store = StateStore(tmp_path / "state.db")
    store.initialize()

    with store.connect() as successful_connection:
        successful_connection.execute("SELECT 1").fetchone()
    with pytest.raises(sqlite3.ProgrammingError):
        successful_connection.execute("SELECT 1")

    with pytest.raises(RuntimeError):
        with store.connect() as failed_connection:
            failed_connection.execute("SELECT 1").fetchone()
            raise RuntimeError("模拟事务失败")
    with pytest.raises(sqlite3.ProgrammingError):
        failed_connection.execute("SELECT 1")


def test_snapshot_and_events_are_idempotent(tmp_path, snapshot_factory) -> None:
    store = StateStore(tmp_path / "state.db")
    store.initialize()
    old = snapshot_factory()
    new = snapshot_factory(head_sha="b" * 40)
    store.save_snapshot_and_events(old, [])
    events = detect_events(old, new)
    assert store.save_snapshot_and_events(new, events) == 2
    assert store.save_snapshot_and_events(new, events) == 0
    assert len(store.pending_events()) == 2
    assert store.load_snapshot(new.key) == new


def test_lists_snapshot_stats_and_enqueues_discovered_event(
    tmp_path,
    snapshot_factory,
) -> None:
    """快照应独立展示，管理员补发事件不能改写快照且必须幂等。"""

    store = StateStore(tmp_path / "state.db")
    store.initialize()
    snapshot = snapshot_factory(repository_id="demo", number=9)
    store.save_snapshot_and_events(snapshot, [])

    items = store.list_snapshots()
    assert len(items) == 1
    assert items[0]["number"] == 9
    assert items[0]["discovered_event_emitted"] is False
    assert store.dashboard_stats()["change_requests"] == {"total": 1, "opened": 1}

    event = detect_events(None, snapshot, emit_initial=True)[0]
    assert store.enqueue_events([event]) == 1
    assert store.enqueue_events([event]) == 0
    assert store.has_event_type("demo", 9, "change_request.discovered")
    assert store.load_snapshot(snapshot.key) == snapshot
    assert store.list_snapshots()[0]["discovered_event_emitted"] is True


def test_latest_activity_reference_and_manual_events_are_independent(
    tmp_path,
    snapshot_factory,
) -> None:
    """最新活动只作参考，同一活动可多次生成不同的手动事件。"""

    store = StateStore(tmp_path / "state.db")
    store.initialize()
    snapshot = snapshot_factory(repository_id="demo", number=9, state="merged")
    activity = ChangeRequestActivity(
        id="timeline-merged",
        type="merged",
        occurred_at="2026-08-17T08:00:00Z",
    )
    store.save_snapshot_and_events(
        snapshot,
        [],
        activity_cursor={
            "page": 3,
            "item_id": activity.id,
            "latest_activity_checked": True,
            "latest_activity": activity.model_dump(mode="json"),
        },
    )

    assert store.pending_events() == []
    latest = store.list_snapshots()[0]["latest_event"]
    assert latest["event_type"] == "change_request.merged"
    assert latest["provider_event_id"] == activity.id

    first = create_manual_activity_event(snapshot, activity)
    second = create_manual_activity_event(snapshot, activity)
    assert first.id != second.id
    assert first.batch_id != second.batch_id
    assert store.enqueue_events([first, second]) == 2
    assert len(store.pending_events()) == 2
    records = store.list_events()
    assert {item["origin"] for item in records} == {"manual"}
    assert {item["source_activity_id"] for item in records} == {activity.id}
    assert {item["source_activity_type"] for item in records} == {"merged"}


def test_overview_lists_filter_sort_and_apply_optional_limits(
    tmp_path,
    snapshot_factory,
) -> None:
    """概览查询应先过滤，再按业务时间倒序并支持全部记录。"""

    store = StateStore(tmp_path / "state.db")
    store.initialize()
    newest = snapshot_factory(
        repository_id="first",
        number=1,
        state="opened",
        updated_at="2026-08-18T10:00:00Z",
    )
    older = snapshot_factory(
        repository_id="second",
        number=2,
        state="closed",
        updated_at="2026-08-18T09:00:00Z",
    )
    newest_event = detect_events(None, newest, emit_initial=True)[0]
    older_event = detect_events(None, older, emit_initial=True)[0]

    # 故意让业务时间较旧的记录后入库，验证排序不依赖扫描或入库时间。
    store.save_snapshot_and_events(newest, [newest_event])
    store.save_snapshot_and_events(older, [older_event])
    assert store.claim_event(older_event.id, 2)
    store.record_event_dispatches([older_event.id], [])

    assert [item["snapshot_key"] for item in store.list_snapshots(None)] == [
        newest.key,
        older.key,
    ]
    assert [item["snapshot_key"] for item in store.list_snapshots(1)] == [
        newest.key,
    ]
    assert [
        item["snapshot_key"]
        for item in store.list_snapshots(None, repository_id="second")
    ] == [older.key]
    assert [
        item["snapshot_key"]
        for item in store.list_snapshots(None, status="opened")
    ] == [newest.key]

    assert [item["event_id"] for item in store.list_events(None)] == [
        newest_event.id,
        older_event.id,
    ]
    assert store.list_events(1)[0]["event_id"] == newest_event.id
    assert store.list_events(None, repository_id="second")[0]["event_id"] == older_event.id
    assert store.list_events(None, status="unmatched")[0]["event_id"] == older_event.id
    assert store.list_events(None, status="pending")[0]["event_id"] == newest_event.id
    assert store.list_events(None)[0]["occurred_at"].startswith("2026-08-18T10:00:00")


def test_event_claim_respects_attempt_limit(tmp_path, snapshot_factory) -> None:
    store = StateStore(tmp_path / "state.db")
    store.initialize()
    event = detect_events(None, snapshot_factory(), emit_initial=True)[0]
    store.save_snapshot_and_events(event.new, [event])
    assert store.claim_event(event.id, 2)
    store.finish_event(event.id, error="第一次失败")
    assert store.claim_event(event.id, 2)
    store.finish_event(event.id, error="第二次失败")
    assert not store.claim_event(event.id, 2)


def test_resource_lock_is_reentrant_and_exclusive(tmp_path) -> None:
    store = StateStore(tmp_path / "state.db")
    store.initialize()
    keys = ["workspace:/tmp/demo", "change_request:demo:7"]
    assert store.acquire_locks(keys, "root-a", 60)
    assert store.acquire_locks(keys, "root-a", 60)
    assert not store.acquire_locks(keys, "root-b", 60)
    store.release_locks(keys, "root-a")
    assert not store.acquire_locks(keys, "root-b", 60)
    store.release_locks(keys, "root-a")
    assert store.acquire_locks(keys, "root-b", 60)


def test_recovery_requeues_processing_event(tmp_path, snapshot_factory) -> None:
    store = StateStore(tmp_path / "state.db")
    store.initialize()
    event = detect_events(None, snapshot_factory(), emit_initial=True)[0]
    store.save_snapshot_and_events(event.new, [event])
    assert store.claim_event(event.id, 2)
    store.recover_interrupted_work()
    assert [item.id for item in store.pending_events()] == [event.id]


def test_recovery_requeues_triggered_event_and_fails_queued_agent(
    tmp_path,
    snapshot_factory,
) -> None:
    """异常退出后，已触发事件和排队 Agent 应恢复为可重试状态。"""

    store = StateStore(tmp_path / "state.db")
    store.initialize()
    event = detect_events(None, snapshot_factory(), emit_initial=True)[0]
    store.save_snapshot_and_events(event.new, [event])
    assert store.claim_event(event.id, 2)
    store.record_event_dispatches(
        [event.id],
        [(event.id, "recovery-key", "review", "reviewer")],
    )
    reservation = store.begin_agent_run(
        proposed_run_id="queued-before-recovery",
        root_run_id=None,
        parent_run_id=None,
        idempotency_key="recovery-key",
        event_id=event.id,
        rule_name="review",
        agent_name="reviewer",
        resource_key=event.resource_key,
        prompt="",
        max_attempts=2,
    )
    assert reservation is not None

    store.recover_interrupted_work()

    assert [item.id for item in store.pending_events()] == [event.id]
    assert store.agent_run_status("recovery-key") == "failed"


def test_event_dispatch_and_agent_progress_are_tracked_separately(
    tmp_path,
    snapshot_factory,
) -> None:
    """未匹配事件应立即标记未触发，匹配事件独立聚合 Agent 进度。"""

    store = StateStore(tmp_path / "state.db")
    store.initialize()
    old = snapshot_factory()
    new = snapshot_factory(state="closed", updated_at="2026-08-17T08:05:00Z")
    events = detect_events(old, new)
    closed = next(item for item in events if item.type == "change_request.closed")
    updated = next(item for item in events if item.type == "change_request.updated")
    store.save_snapshot_and_events(new, events)
    assert store.claim_event(closed.id, 2)
    assert store.claim_event(updated.id, 2)

    store.record_event_dispatches(
        [closed.id, updated.id],
        [(closed.id, "dispatch-key", "close-review", "reviewer")],
    )
    records = {item["event_type"]: item for item in store.list_events()}
    assert records["change_request.updated"]["status"] == "unmatched"
    assert records["change_request.updated"]["trigger_count"] == 0
    assert records["change_request.closed"]["status"] == "triggered"
    assert records["change_request.closed"]["trigger_count"] == 1
    assert records["change_request.closed"]["agent_queued_count"] == 1

    reservation = store.begin_agent_run(
        proposed_run_id="run-close-review",
        root_run_id=None,
        parent_run_id=None,
        idempotency_key="dispatch-key",
        event_id=closed.id,
        rule_name="close-review",
        agent_name="reviewer",
        resource_key=closed.resource_key,
        prompt="",
        max_attempts=1,
    )
    assert reservation is not None
    assert store.agent_run_status("dispatch-key") == "queued"
    assert store.mark_agent_run_preparing(reservation.run_id)
    records = {item["event_type"]: item for item in store.list_events()}
    assert records["change_request.closed"]["agent_preparing_count"] == 1
    store.mark_agent_run_running(reservation.run_id)
    records = {item["event_type"]: item for item in store.list_events()}
    assert records["change_request.closed"]["agent_running_count"] == 1

    store.finish_agent_run(
        AgentResult(
            run_id=reservation.run_id,
            root_run_id=reservation.root_run_id,
            agent_name="reviewer",
            status="completed",
        )
    )
    store.finish_event(closed.id)
    records = {item["event_type"]: item for item in store.list_events()}
    assert records["change_request.closed"]["status"] == "completed"
    assert records["change_request.closed"]["agent_completed_count"] == 1
    summary = store.list_runs()[0]
    detail = store.get_run(reservation.run_id)
    assert summary["repository_id"] == new.repository_id
    assert summary["change_request_number"] == new.number
    assert summary["change_request_title"] == new.title
    assert summary["change_request_url"] == new.web_url
    assert detail is not None
    assert detail["change_request_title"] == new.title


def test_agent_run_exposes_workspace_cleanup_status(tmp_path) -> None:
    """运行列表与详情都应返回实际工作区和清理原因。"""

    store = StateStore(tmp_path / "state.db")
    store.initialize()
    reservation = store.begin_agent_run(
        proposed_run_id="run-workspace",
        root_run_id=None,
        parent_run_id=None,
        idempotency_key="workspace-key",
        event_id=None,
        rule_name="review",
        agent_name="reviewer",
        resource_key="github:demo:7",
        prompt="",
        max_attempts=1,
    )
    assert reservation is not None
    store.update_agent_run_workspace(
        reservation.run_id,
        path="/tmp/worktrees/run-workspace",
        status="retained",
        reason="工作区存在未提交文件",
    )

    summary = store.list_runs()[0]
    detail = store.get_run(reservation.run_id)
    assert summary["workspace_status"] == "retained"
    assert summary["workspace_path"] == "/tmp/worktrees/run-workspace"
    assert detail is not None
    assert detail["workspace_reason"] == "工作区存在未提交文件"


def test_cancel_request_persists_and_cascades_to_descendants(tmp_path) -> None:
    """取消根运行时，排队后代应立即取消，执行中运行应收到持久化请求。"""

    store = StateStore(tmp_path / "state.db")
    store.initialize()
    root = store.begin_agent_run(
        proposed_run_id="run-root",
        root_run_id=None,
        parent_run_id=None,
        idempotency_key="cancel-root",
        event_id=None,
        rule_name="review",
        agent_name="reviewer",
        resource_key="github:demo:7",
        prompt="",
        max_attempts=1,
    )
    child = store.begin_agent_run(
        proposed_run_id="run-child",
        root_run_id="run-root",
        parent_run_id="run-root",
        idempotency_key="cancel-child",
        event_id=None,
        rule_name=None,
        agent_name="helper",
        resource_key="github:demo:7",
        prompt="",
        max_attempts=1,
    )
    assert root is not None
    assert child is not None
    assert store.mark_agent_run_running(root.run_id)

    cancelled = store.request_cancel_run(root.run_id)

    assert set(cancelled or []) == {root.run_id, child.run_id}
    assert store.agent_run_cancel_requested(root.run_id)
    assert store.agent_run_cancel_requested(child.run_id)
    assert store.get_run(root.run_id)["status"] == "running"
    child_detail = store.get_run(child.run_id)
    assert child_detail["status"] == "cancelled"
    assert child_detail["finished_at"] is not None
    assert not store.mark_agent_run_running(child.run_id)


def test_cancel_request_marks_preparing_run_for_cooperative_stop(tmp_path) -> None:
    """准备工作区的运行应保留状态，并向 Git 工作线程发出取消请求。"""

    store = StateStore(tmp_path / "state.db")
    store.initialize()
    reservation = store.begin_agent_run(
        proposed_run_id="run-preparing",
        root_run_id=None,
        parent_run_id=None,
        idempotency_key="cancel-preparing",
        event_id=None,
        rule_name="review",
        agent_name="reviewer",
        resource_key="github:demo:7",
        prompt="",
        max_attempts=1,
    )
    assert reservation is not None
    assert store.mark_agent_run_preparing(reservation.run_id)

    assert store.request_cancel_run(reservation.run_id) == [reservation.run_id]
    detail = store.get_run(reservation.run_id)
    assert detail is not None
    assert detail["status"] == "preparing"
    assert detail["cancel_requested"] == 1
    assert not store.mark_agent_run_running(reservation.run_id)


def test_activity_cursor_is_saved_with_snapshot_and_events(
    tmp_path,
    snapshot_factory,
) -> None:
    """活动游标应与快照和事件一起保存，并可覆盖到下一位置。"""

    store = StateStore(tmp_path / "state.db")
    store.initialize()
    snapshot = snapshot_factory(provider="github-main", repository_id="demo")
    event = detect_events(None, snapshot, emit_initial=True)[0]

    store.save_snapshot_and_events(
        snapshot,
        [event],
        activity_cursor={"page": 2, "item_id": "timeline-20"},
    )
    assert store.load_activity_cursor("github-main", "demo", 7) == {
        "page": 2,
        "item_id": "timeline-20",
    }

    store.save_snapshot_and_events(
        snapshot,
        [event],
        activity_cursor={"page": 3, "item_id": "timeline-21"},
    )
    assert store.load_activity_cursor("github-main", "demo", 7) == {
        "page": 3,
        "item_id": "timeline-21",
    }
    assert len(store.pending_events()) == 1


def test_preflight_runs_are_idempotent_per_head_and_config_revision(tmp_path) -> None:
    """同一 SHA 和配置版本只能产生一次终态 CI，配置变化应创建新运行。"""

    store = StateStore(tmp_path / "state.db")
    store.initialize()
    reservation = store.begin_preflight_run(
        proposed_run_id="preflight-1",
        idempotency_key="demo:7:sha-a:revision-a",
        event_id="event-1",
        repository_id="demo",
        number=7,
        head_sha="a" * 40,
        config_revision="revision-a",
        max_attempts=2,
    )
    assert reservation is not None
    assert reservation.attempts == 1
    assert store.begin_preflight_run(
        proposed_run_id="duplicate",
        idempotency_key="demo:7:sha-a:revision-a",
        event_id="event-1",
        repository_id="demo",
        number=7,
        head_sha="a" * 40,
        config_revision="revision-a",
        max_attempts=2,
    ) is None

    store.finish_preflight_run(
        PreflightResult(
            run_id=reservation.run_id,
            repository_id="demo",
            number=7,
            head_sha="a" * 40,
            status="success",
            output="4 tests passed",
        )
    )
    saved = store.load_preflight_result("demo:7:sha-a:revision-a")
    assert saved is not None
    assert saved.status == "success"
    assert saved.output == "4 tests passed"
    assert store.begin_preflight_run(
        proposed_run_id="after-success",
        idempotency_key="demo:7:sha-a:revision-a",
        event_id="event-1",
        repository_id="demo",
        number=7,
        head_sha="a" * 40,
        config_revision="revision-a",
        max_attempts=2,
    ) is None

    changed = store.begin_preflight_run(
        proposed_run_id="preflight-2",
        idempotency_key="demo:7:sha-a:revision-b",
        event_id="event-1",
        repository_id="demo",
        number=7,
        head_sha="a" * 40,
        config_revision="revision-b",
        max_attempts=2,
    )
    assert changed is not None
    assert changed.run_id == "preflight-2"


def test_recovery_marks_running_preflight_as_retryable_error(tmp_path) -> None:
    """服务异常退出后的 CI 应转成 error，并允许按次数限制复用运行记录。"""

    store = StateStore(tmp_path / "state.db")
    store.initialize()
    reservation = store.begin_preflight_run(
        proposed_run_id="preflight-1",
        idempotency_key="demo:7:sha-a:revision-a",
        event_id="event-1",
        repository_id="demo",
        number=7,
        head_sha="a" * 40,
        config_revision="revision-a",
        max_attempts=2,
    )
    assert reservation is not None

    store.recover_interrupted_work()

    recovered = store.load_preflight_result("demo:7:sha-a:revision-a")
    assert recovered is not None
    assert recovered.status == "error"
    retried = store.begin_preflight_run(
        proposed_run_id="unused-new-id",
        idempotency_key="demo:7:sha-a:revision-a",
        event_id="event-1",
        repository_id="demo",
        number=7,
        head_sha="a" * 40,
        config_revision="revision-a",
        max_attempts=2,
    )
    assert retried is not None
    assert retried.run_id == "preflight-1"
    assert retried.attempts == 2
