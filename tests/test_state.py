"""SQLite 幂等与资源租约测试。"""

import sqlite3

import pytest

from teamwork_review_agents.events import (
    create_manual_activity_event,
    detect_events,
    detect_target_branch_event,
)
from teamwork_review_agents.models import (
    AgentResult,
    ChangeRequestActivity,
    PreflightResult,
)
from teamwork_review_agents.state import (
    CANCEL_SOURCE_ADMINISTRATOR,
    CANCEL_SOURCE_SERVICE_SHUTDOWN,
    LEGACY_CANCELLED_RETRY_ERROR,
    StateStore,
)


def test_terminal_target_event_is_lightweight_retained_and_pruned_without_losing_run(
    tmp_path,
    snapshot_factory,
) -> None:
    """目标事件终态先保留再按期限清理，关联运行始终保留完整摘要。"""

    store = StateStore(tmp_path / "state.db")
    store.initialize()
    old = snapshot_factory(
        target_head_sha="a" * 40,
        raw={"large-provider-value": "不应写入临时事件"},
    )
    current = snapshot_factory(
        target_head_sha="b" * 40,
        raw={"large-provider-value": "不应写入临时事件"},
    )
    event = detect_target_branch_event(
        old,
        current,
        batch_id="scan-one",
        occurred_at=current.updated_at,
    )[0]
    store.save_snapshot_and_events(current, [event])

    with store.connect() as connection:
        payload = connection.execute(
            "SELECT payload FROM event_inbox WHERE event_id = ?",
            (event.id,),
        ).fetchone()["payload"]
    assert "large-provider-value" not in payload

    reservation = store.begin_agent_run(
        proposed_run_id="target-run",
        root_run_id=None,
        parent_run_id=None,
        idempotency_key="target-run-key",
        event_id=event.id,
        rule_name="target-review",
        agent_name="reviewer",
        resource_key=event.resource_key,
        prompt="目标分支变化审核",
        max_attempts=2,
    )
    assert reservation is not None
    store.finish_agent_run(
        AgentResult(
            run_id="target-run",
            root_run_id="target-run",
            agent_name="reviewer",
            status="completed",
        )
    )
    store.finish_event(event.id)

    assert store.finalize_terminal_target_event_context(event.id) is True
    retained = store.list_events(None)
    assert len(retained) == 1
    assert retained[0]["event_id"] == event.id
    assert retained[0]["status"] == "completed"
    detail = store.get_run("target-run")
    assert detail is not None
    assert detail["repository_id"] == current.repository_id
    assert detail["change_request_number"] == current.number
    assert detail["change_request_title"] == current.title
    assert detail["change_request_url"] == current.web_url

    with store.connect() as connection:
        connection.execute(
            "UPDATE event_inbox SET updated_at = ? WHERE event_id = ?",
            (1.0, event.id),
        )
    assert store.prune_terminal_target_events(2.0, max_attempts=2) == 1
    assert store.list_events(None) == []

    detail_after_prune = store.get_run("target-run")
    assert detail_after_prune is not None
    assert detail_after_prune["repository_id"] == current.repository_id
    assert detail_after_prune["change_request_number"] == current.number


def test_target_event_retention_prune_only_removes_terminal_statuses(
    tmp_path,
    snapshot_factory,
) -> None:
    """过期清理覆盖全部终态，同时保护仍在处理链路中的目标事件。"""

    store = StateStore(tmp_path / "state.db")
    store.initialize()
    old = snapshot_factory(target_head_sha="a" * 40)
    current = snapshot_factory(target_head_sha="b" * 40)
    active_events = []
    terminal_events = []
    statuses = (
        "pending",
        "processing",
        "triggered",
        "completed",
        "unmatched",
        "failed",
        "cancelled",
    )
    for index, status in enumerate(statuses, start=1):
        event = detect_target_branch_event(
            old,
            current,
            batch_id=f"active-{index}",
            occurred_at=current.updated_at,
        )[0]
        store.save_snapshot_and_events(current, [event])
        with store.connect() as connection:
            connection.execute(
                """
                UPDATE event_inbox
                SET status = ?, attempts = ?, updated_at = ?
                WHERE event_id = ?
                """,
                (status, 2 if status == "failed" else 0, 1.0, event.id),
            )
        if status in {"pending", "processing", "triggered"}:
            active_events.append(event)
        else:
            terminal_events.append(event)

    retryable_failed_event = detect_target_branch_event(
        old,
        current,
        batch_id="retryable-failed",
        occurred_at=current.updated_at,
    )[0]
    store.save_snapshot_and_events(current, [retryable_failed_event])
    with store.connect() as connection:
        connection.execute(
            """
            UPDATE event_inbox
            SET status = 'failed', attempts = 1, updated_at = ?
            WHERE event_id = ?
            """,
            (1.0, retryable_failed_event.id),
        )
    active_events.append(retryable_failed_event)

    assert (
        store.prune_terminal_target_events(2.0, max_attempts=2)
        == len(terminal_events)
    )
    assert {record["event_id"] for record in store.list_events(None)} == {
        event.id for event in active_events
    }


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


def test_agent_run_capacity_combines_global_and_per_agent_limits(tmp_path) -> None:
    """根任务使用全局额度，同名根任务与 sub-agent 共用 Agent 额度。"""

    store = StateStore(tmp_path / "state.db")
    store.initialize()

    def reserve(run_id: str, agent_name: str, parent_run_id: str | None = None) -> None:
        reservation = store.begin_agent_run(
            proposed_run_id=run_id,
            root_run_id="root-one" if parent_run_id else None,
            parent_run_id=parent_run_id,
            idempotency_key=f"key-{run_id}",
            event_id=None,
            rule_name=None,
            agent_name=agent_name,
            resource_key=f"resource-{run_id}",
            prompt="",
            max_attempts=1,
        )
        assert reservation is not None

    reserve("root-one", "reviewer")
    reserve("root-two", "writer")
    reserve("root-three", "publisher")
    reserve("child-one", "security", parent_run_id="root-one")
    reserve("child-two", "security", parent_run_id="root-one")

    assert store.try_acquire_agent_run_capacity(
        "root-one",
        global_limit=1,
        runtime_limit=5,
        agent_limit=None,
        acquire_global=True,
    ) == (True, None)
    assert store.try_acquire_agent_run_capacity(
        "root-two",
        global_limit=1,
        runtime_limit=5,
        agent_limit=None,
        acquire_global=True,
    ) == (False, "global_concurrency")
    assert store.try_acquire_agent_run_capacity(
        "root-three",
        global_limit=5,
        runtime_limit=1,
        agent_limit=None,
        acquire_global=True,
    ) == (False, "runtime_concurrency")

    assert store.try_acquire_agent_run_capacity(
        "child-one",
        global_limit=1,
        runtime_limit=1,
        agent_limit=1,
        acquire_global=False,
    ) == (True, None)
    assert store.try_acquire_agent_run_capacity(
        "child-two",
        global_limit=1,
        runtime_limit=1,
        agent_limit=1,
        acquire_global=False,
    ) == (False, "agent_concurrency")

    queued = {item["run_id"]: item for item in store.list_runs()}
    assert queued["root-two"]["queue_reason"] == "global_concurrency"
    assert queued["root-three"]["queue_reason"] == "runtime_concurrency"
    assert queued["child-two"]["queue_reason"] == "agent_concurrency"

    store.finish_agent_run(
        AgentResult(
            run_id="root-one",
            root_run_id="root-one",
            agent_name="reviewer",
            status="completed",
        )
    )
    assert store.try_acquire_agent_run_capacity(
        "root-two",
        global_limit=1,
        runtime_limit=5,
        agent_limit=None,
        acquire_global=True,
    ) == (True, None)


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
        item["snapshot_key"] for item in store.list_snapshots(1, offset=1)
    ] == [older.key]
    assert store.count_snapshots() == 2
    assert store.count_snapshots(repository_id="second", status="closed") == 1
    assert [
        item["snapshot_key"]
        for item in store.list_snapshots(None, repository_id="second")
    ] == [older.key]
    assert [
        item["snapshot_key"]
        for item in store.list_snapshots(None, status="opened")
    ] == [newest.key]
    assert [
        item["snapshot_key"]
        for item in store.list_snapshots(None, number=2)
    ] == [older.key]

    assert [item["event_id"] for item in store.list_events(None)] == [
        newest_event.id,
        older_event.id,
    ]
    assert store.list_events(1)[0]["event_id"] == newest_event.id
    assert store.list_events(1, offset=1)[0]["event_id"] == older_event.id
    assert store.count_events() == 2
    assert store.count_events(repository_id="second", status="unmatched") == 1
    assert store.list_events(None, repository_id="second")[0]["event_id"] == older_event.id
    assert store.list_events(None, number=2)[0]["event_id"] == older_event.id
    assert store.list_events(None, status="unmatched")[0]["event_id"] == older_event.id
    assert store.list_events(None, status="pending")[0]["event_id"] == newest_event.id
    assert store.list_events(None)[0]["occurred_at"].startswith("2026-08-18T10:00:00")

    detail = store.get_change_request_detail("second", 2)
    assert detail is not None
    assert detail["snapshot_key"] == older.key
    assert [item["event_id"] for item in detail["events"]] == [older_event.id]
    assert store.get_change_request_detail("second", 999) is None


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


def test_service_shutdown_release_does_not_consume_event_attempt(
    tmp_path,
    snapshot_factory,
) -> None:
    """服务停止退回事件时应抵消领取次数，并允许按原次数再次领取。"""

    store = StateStore(tmp_path / "state.db")
    store.initialize()
    event = detect_events(None, snapshot_factory(), emit_initial=True)[0]
    store.save_snapshot_and_events(event.new, [event])
    assert store.claim_event(event.id, 1)
    store.record_event_dispatches(
        [event.id],
        [(event.id, "shutdown-event", "review", "reviewer")],
    )

    assert store.release_event_after_service_shutdown(event.id)
    record = store.list_events(None)[0]
    assert record["status"] == "pending"
    assert record["attempts"] == 0
    assert store.claim_event(event.id, 1)


def test_explicit_cancelled_event_status_is_terminal(
    tmp_path,
    snapshot_factory,
) -> None:
    """带错误说明的管理员取消事件也必须保留 cancelled 终态。"""

    store = StateStore(tmp_path / "state.db")
    store.initialize()
    event = detect_events(None, snapshot_factory(), emit_initial=True)[0]
    store.save_snapshot_and_events(event.new, [event])
    assert store.claim_event(event.id, 2)

    store.finish_event(event.id, status="cancelled", error="管理员取消")

    record = store.list_events(None)[0]
    assert record["status"] == "cancelled"
    assert record["error"] == "管理员取消"
    assert store.pending_events() == []


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

    child = store.begin_agent_run(
        proposed_run_id="run-security-review",
        root_run_id=reservation.root_run_id,
        parent_run_id=reservation.run_id,
        idempotency_key="security-dispatch-key",
        event_id=closed.id,
        rule_name="close-review",
        agent_name="security-reviewer",
        resource_key=closed.resource_key,
        prompt="",
        max_attempts=1,
    )
    assert child is not None
    store.mark_agent_run_running(child.run_id)

    grandchild = store.begin_agent_run(
        proposed_run_id="run-license-review",
        root_run_id=reservation.root_run_id,
        parent_run_id=child.run_id,
        idempotency_key="license-dispatch-key",
        event_id=closed.id,
        rule_name="close-review",
        agent_name="license-reviewer",
        resource_key=closed.resource_key,
        prompt="",
        max_attempts=1,
    )
    assert grandchild is not None
    store.mark_agent_run_running(grandchild.run_id)
    records = {item["event_type"]: item for item in store.list_events()}
    assert records["change_request.closed"]["trigger_count"] == 1
    assert records["change_request.closed"]["sub_agent_count"] == 2
    assert records["change_request.closed"]["agent_running_count"] == 3

    store.finish_agent_run(
        AgentResult(
            run_id=grandchild.run_id,
            root_run_id=grandchild.root_run_id,
            agent_name="license-reviewer",
            status="completed",
        )
    )

    store.finish_agent_run(
        AgentResult(
            run_id=child.run_id,
            root_run_id=child.root_run_id,
            agent_name="security-reviewer",
            status="completed",
        )
    )

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
    assert records["change_request.closed"]["agent_completed_count"] == 3
    summary = store.list_runs()[0]
    detail = store.get_run(reservation.run_id)
    assert summary["repository_id"] == new.repository_id
    assert summary["change_request_number"] == new.number
    assert summary["change_request_title"] == new.title
    assert summary["change_request_url"] == new.web_url
    filtered_runs = store.list_runs(
        statuses=("completed",),
        repository_id=new.repository_id,
        number=new.number,
    )
    assert {item["run_id"] for item in filtered_runs} == {
        reservation.run_id,
        child.run_id,
        grandchild.run_id,
    }
    assert store.list_runs(statuses=()) == []
    assert len(store.list_runs(limit=None)) == 3
    assert detail is not None
    assert detail["change_request_title"] == new.title
    event_detail = store.get_event_detail(closed.id)
    assert event_detail is not None
    assert len(event_detail["dispatches"]) == 1
    assert {
        run["run_id"]: run["parent_run_id"]
        for run in event_detail["agent_runs"]
    } == {
        reservation.run_id: None,
        child.run_id: reservation.run_id,
        grandchild.run_id: child.run_id,
    }


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


def test_service_shutdown_requests_cancel_for_every_active_run(tmp_path) -> None:
    """服务停止应覆盖全部活动根任务和 sub-agent，终态运行保持不变。"""

    store = StateStore(tmp_path / "state.db")
    store.initialize()
    queued = store.begin_agent_run(
        proposed_run_id="run-queued",
        root_run_id=None,
        parent_run_id=None,
        idempotency_key="shutdown-queued",
        event_id=None,
        rule_name="review",
        agent_name="reviewer",
        resource_key="github:demo:8",
        prompt="",
        max_attempts=1,
    )
    running = store.begin_agent_run(
        proposed_run_id="run-running",
        root_run_id=None,
        parent_run_id=None,
        idempotency_key="shutdown-running",
        event_id=None,
        rule_name="review",
        agent_name="reviewer",
        resource_key="github:demo:9",
        prompt="",
        max_attempts=1,
    )
    child = store.begin_agent_run(
        proposed_run_id="run-child",
        root_run_id="run-running",
        parent_run_id="run-running",
        idempotency_key="shutdown-child",
        event_id=None,
        rule_name=None,
        agent_name="helper",
        resource_key="github:demo:9",
        prompt="",
        max_attempts=1,
    )
    assert queued is not None
    assert running is not None
    assert child is not None
    assert store.mark_agent_run_running(running.run_id)
    assert store.mark_agent_run_preparing(child.run_id)

    cancelled = store.request_cancel_active_runs()

    assert set(cancelled) == {queued.run_id, running.run_id, child.run_id}
    assert store.get_run(queued.run_id)["status"] == "cancelled"
    assert store.get_run(running.run_id)["status"] == "running"
    assert store.get_run(child.run_id)["status"] == "preparing"
    assert all(store.agent_run_cancel_requested(run_id) for run_id in cancelled)
    assert all(
        store.agent_run_cancel_source(run_id)
        == CANCEL_SOURCE_SERVICE_SHUTDOWN
        for run_id in cancelled
    )
    assert store.request_cancel_active_runs() == []


def test_service_shutdown_cancelled_run_reuses_attempt_and_run_id(tmp_path) -> None:
    """服务中断的幂等运行应原位恢复，且不增加业务失败尝试次数。"""

    store = StateStore(tmp_path / "state.db")
    store.initialize()
    first = store.begin_agent_run(
        proposed_run_id="service-run",
        root_run_id=None,
        parent_run_id=None,
        idempotency_key="service-retry",
        event_id=None,
        rule_name="review",
        agent_name="reviewer",
        resource_key="github:demo:10",
        prompt="首次执行",
        max_attempts=1,
    )
    assert first is not None
    assert store.request_cancel_run(
        first.run_id,
        source=CANCEL_SOURCE_SERVICE_SHUTDOWN,
    ) == [first.run_id]
    assert store.agent_run_cancel_source(first.run_id) == CANCEL_SOURCE_SERVICE_SHUTDOWN

    resumed = store.begin_agent_run(
        proposed_run_id="unused-new-id",
        root_run_id=None,
        parent_run_id=None,
        idempotency_key="service-retry",
        event_id=None,
        rule_name="review",
        agent_name="reviewer",
        resource_key="github:demo:10",
        prompt="恢复执行",
        max_attempts=1,
    )

    assert resumed is not None
    assert resumed.run_id == first.run_id
    assert resumed.attempts == 1
    detail = store.get_run(first.run_id)
    assert detail is not None
    assert detail["status"] == "queued"
    assert detail["cancel_requested"] == 0
    assert detail["cancel_source"] is None


def test_agent_run_model_snapshot_is_persisted_and_updated_on_retry(tmp_path) -> None:
    """运行详情应固化模型快照，业务失败重试时更新为本次设置。"""

    store = StateStore(tmp_path / "state.db")
    store.initialize()
    first = store.begin_agent_run(
        proposed_run_id="model-run",
        root_run_id=None,
        parent_run_id=None,
        idempotency_key="model-retry",
        event_id=None,
        rule_name="review",
        agent_name="reviewer",
        resource_key="github:demo:12",
        prompt="首次执行",
        max_attempts=2,
        model_snapshot={
            "execution_mode": "cli",
            "model": "gpt-first",
            "model_source": "runtime",
        },
    )
    assert first is not None
    first_detail = store.get_run(first.run_id)
    assert first_detail is not None
    assert first_detail["model_snapshot"]["model"] == "gpt-first"

    store.finish_agent_run(
        AgentResult(
            run_id=first.run_id,
            root_run_id=first.root_run_id,
            agent_name="reviewer",
            status="failed",
            error="首次失败",
        )
    )
    retried = store.begin_agent_run(
        proposed_run_id="unused-model-run",
        root_run_id=None,
        parent_run_id=None,
        idempotency_key="model-retry",
        event_id=None,
        rule_name="review",
        agent_name="reviewer",
        resource_key="github:demo:12",
        prompt="重试执行",
        max_attempts=2,
        model_snapshot={
            "execution_mode": "model",
            "model": "gpt-second",
            "model_source": "agent",
        },
    )

    assert retried is not None
    assert retried.run_id == first.run_id
    retried_detail = store.get_run(first.run_id)
    assert retried_detail is not None
    assert retried_detail["model_snapshot"] == {
        "execution_mode": "model",
        "model": "gpt-second",
        "model_source": "agent",
    }


def test_initialize_adds_model_snapshot_to_existing_agent_runs(tmp_path) -> None:
    """旧数据库缺少模型快照列时，初始化应执行兼容补列。"""

    store = StateStore(tmp_path / "state.db")
    store.initialize()
    with store.connect() as connection:
        connection.execute("ALTER TABLE agent_runs DROP COLUMN model_snapshot")

    store.initialize()

    with store.connect() as connection:
        columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(agent_runs)").fetchall()
        }
    assert "model_snapshot" in columns


def test_manual_cancelled_run_is_not_automatically_reused(tmp_path) -> None:
    """管理员取消保持终态，不能由幂等重试自动恢复。"""

    store = StateStore(tmp_path / "state.db")
    store.initialize()
    first = store.begin_agent_run(
        proposed_run_id="manual-run",
        root_run_id=None,
        parent_run_id=None,
        idempotency_key="manual-cancel",
        event_id=None,
        rule_name="review",
        agent_name="reviewer",
        resource_key="github:demo:11",
        prompt="",
        max_attempts=3,
    )
    assert first is not None
    assert store.request_cancel_run(first.run_id) == [first.run_id]
    assert store.agent_run_cancel_source(first.run_id) == CANCEL_SOURCE_ADMINISTRATOR

    assert store.begin_agent_run(
        proposed_run_id="manual-retry",
        root_run_id=None,
        parent_run_id=None,
        idempotency_key="manual-cancel",
        event_id=None,
        rule_name="review",
        agent_name="reviewer",
        resource_key="github:demo:11",
        prompt="",
        max_attempts=3,
    ) is None


def test_initialize_recovers_narrow_legacy_restart_failure(
    tmp_path,
    snapshot_factory,
) -> None:
    """新增取消来源字段时只恢复旧版重启形成的特征化失败记录。"""

    store = StateStore(tmp_path / "state.db")
    store.initialize()
    event = detect_events(None, snapshot_factory(), emit_initial=True)[0]
    store.save_snapshot_and_events(event.new, [event])
    assert store.claim_event(event.id, 3)
    store.record_event_dispatches(
        [event.id],
        [(event.id, "legacy-cancel", "review", "reviewer")],
    )
    run = store.begin_agent_run(
        proposed_run_id="legacy-run",
        root_run_id=None,
        parent_run_id=None,
        idempotency_key="legacy-cancel",
        event_id=event.id,
        rule_name="review",
        agent_name="reviewer",
        resource_key=event.resource_key,
        prompt="",
        max_attempts=3,
    )
    assert run is not None
    store.request_cancel_run(run.run_id)
    with store.connect() as connection:
        connection.execute(
            """
            UPDATE event_inbox
            SET status = 'failed', attempts = 3, error = ?
            WHERE event_id = ?
            """,
            (LEGACY_CANCELLED_RETRY_ERROR, event.id),
        )
        connection.execute("ALTER TABLE agent_runs DROP COLUMN cancel_source")

    store.initialize()

    record = store.list_events(None)[0]
    assert record["status"] == "pending"
    assert record["attempts"] == 0
    assert store.agent_run_cancel_source(run.run_id) == CANCEL_SOURCE_SERVICE_SHUTDOWN


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
    assert saved.status_published is False
    store.mark_preflight_status_published(saved.run_id)
    published = store.load_preflight_result("demo:7:sha-a:revision-a")
    assert published is not None
    assert published.status_published is True
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


def test_preflight_failure_comment_mapping_has_one_record_per_change_request(
    tmp_path,
) -> None:
    """同一 MR/PR 只能保存一条失败评论映射，并可在成功后删除。"""

    store = StateStore(tmp_path / "state.db")
    store.initialize()
    store.save_preflight_failure_comment(
        repository_id="demo",
        number=7,
        status_context="teamwork/local-ci",
        remote_comment_id="101",
        head_sha="a" * 40,
        content_hash="first",
    )
    first = store.get_preflight_failure_comment("demo", 7)
    assert first is not None
    assert first["remote_comment_id"] == "101"

    store.save_preflight_failure_comment(
        repository_id="demo",
        number=7,
        status_context="teamwork/local-ci",
        remote_comment_id="101",
        head_sha="b" * 40,
        content_hash="second",
    )
    updated = store.get_preflight_failure_comment("demo", 7)
    assert updated is not None
    assert updated["head_sha"] == "b" * 40
    assert updated["content_hash"] == "second"

    store.delete_preflight_failure_comment("demo", 7)
    assert store.get_preflight_failure_comment("demo", 7) is None


def test_manual_preflight_supports_nullable_pr_live_logs_and_cancel(tmp_path) -> None:
    """手动 CI 不绑定事件或编号，并能持久化阶段、日志与取消终态。"""

    store = StateStore(tmp_path / "state.db")
    store.initialize()
    store.create_manual_preflight_run(
        run_id="manual-preflight",
        repository_id="demo",
        config_revision="revision-manual",
    )
    store.initialize_preflight_steps(
        "manual-preflight",
        [{"name": "install", "command": ["uv", "sync"]}],
    )
    store.set_preflight_phase(
        "manual-preflight",
        "running_steps",
        branch="main",
        head_sha="b" * 40,
        cache_path=str(tmp_path / "cache"),
    )
    first_log = store.append_preflight_log(
        "manual-preflight",
        stream="stdout",
        event_type="output",
        payload="downloading dependencies\n",
    )
    second_log = store.append_preflight_log(
        "manual-preflight",
        stream="system",
        event_type="message",
        payload={"phase": "install"},
    )

    assert store.request_cancel_preflight("manual-preflight") is True
    assert store.preflight_cancel_requested("manual-preflight") is True
    store.update_preflight_step(
        "manual-preflight",
        0,
        status="running",
    )
    store.finish_preflight_run(
        PreflightResult(
            run_id="manual-preflight",
            repository_id="demo",
            number=None,
            head_sha="b" * 40,
            status="cancelled",
            error="用户取消了手动 CI",
        )
    )

    detail = store.get_preflight_run("manual-preflight")
    assert detail is not None
    assert detail["event_id"] is None
    assert detail["number"] is None
    assert detail["trigger_source"] == "manual"
    assert detail["branch"] == "main"
    assert detail["phase"] == "finished"
    assert detail["status"] == "cancelled"
    assert detail["steps"][0]["status"] == "cancelled"
    assert detail["linked_events"] == []
    assert [item["id"] for item in store.list_preflight_logs(
        "manual-preflight",
        after_id=first_log,
    )] == [second_log]
    assert store.request_cancel_preflight("manual-preflight") is False


def test_event_list_exposes_linked_preflight_summary(
    tmp_path,
    snapshot_factory,
) -> None:
    """事件列表只暴露关联 CI 摘要，不携带完整命令输出。"""

    store = StateStore(tmp_path / "state.db")
    store.initialize()
    snapshot = snapshot_factory(provider="github-main", repository_id="demo")
    event = detect_events(None, snapshot, emit_initial=True)[0]
    store.save_snapshot_and_events(snapshot, [event])
    assert store.claim_event(event.id, max_attempts=2) is True
    reservation = store.begin_preflight_run(
        proposed_run_id="preflight-event-summary",
        idempotency_key="demo:7:event-summary",
        event_id=event.id,
        repository_id="demo",
        number=event.number,
        head_sha=event.new.head_sha,
        config_revision="revision-a",
        max_attempts=2,
    )
    assert reservation is not None

    running = store.list_events()[0]
    assert running["status"] == "processing"
    assert running["preflight_status"] == "running"
    assert running["preflight_run_id"] == reservation.run_id
    assert running["preflight_reused"] == 0
    assert running["preflight_failed_step"] is None

    store.initialize_preflight_steps(
        reservation.run_id,
        [
            {"name": "format", "command": ["python", "-m", "ruff"], "timeout_seconds": 30},
            {"name": "tests", "command": ["python", "-m", "pytest"], "timeout_seconds": None},
            {"name": "package", "command": ["python", "-m", "build"], "timeout_seconds": 60},
        ],
    )
    store.update_preflight_step(
        reservation.run_id,
        0,
        status="running",
        timeout_seconds=30,
    )
    store.update_preflight_step(
        reservation.run_id,
        0,
        status="success",
        timeout_seconds=30,
        exit_code=0,
    )
    store.update_preflight_step(
        reservation.run_id,
        1,
        status="running",
        timeout_seconds=45,
    )
    store.update_preflight_step(
        reservation.run_id,
        1,
        status="failure",
        timeout_seconds=45,
        exit_code=1,
    )
    store.finish_preflight_run(
        PreflightResult(
            run_id=reservation.run_id,
            repository_id="demo",
            number=event.number,
            head_sha=event.new.head_sha,
            status="failure",
            failed_step="tests",
            exit_code=1,
            output="不应进入事件列表的完整输出",
        )
    )
    failed = store.list_events()[0]
    assert failed["preflight_status"] == "failure"
    assert failed["preflight_exit_code"] == 1
    assert failed["preflight_failed_step"] == "tests"
    assert failed["preflight_error"] is None
    assert failed["preflight_status_published"] == 0
    assert "output" not in failed

    detail = store.get_event_detail(event.id)
    assert detail is not None
    assert detail["dispatches"] == []
    assert "output" not in detail["preflight"]
    assert detail["preflight"]["reused"] == 0
    assert detail["preflights"][0]["run_id"] == reservation.run_id

    summaries = store.list_preflight_runs()
    assert summaries[0]["run_id"] == reservation.run_id
    assert summaries[0]["event_type"] == event.type
    assert summaries[0]["linked_event_count"] == 1
    assert summaries[0]["reused_event_count"] == 0
    assert "output" not in summaries[0]

    preflight_detail = store.get_preflight_run(reservation.run_id)
    assert preflight_detail is not None
    assert preflight_detail["event_type"] == event.type
    assert preflight_detail["output"] == "不应进入事件列表的完整输出"
    assert preflight_detail["linked_events"][0]["event_id"] == event.id
    assert [step["status"] for step in preflight_detail["steps"]] == [
        "success",
        "failure",
        "skipped",
    ]
    assert preflight_detail["steps"][0]["command"] == ["python", "-m", "ruff"]
    assert preflight_detail["steps"][1]["timeout_seconds"] == 45
    assert preflight_detail["steps"][1]["exit_code"] == 1
    assert preflight_detail["steps"][2]["error"] == "前序步骤结束后未执行"

    reused_event = create_manual_activity_event(
        snapshot,
        ChangeRequestActivity(
            id="manual-reuse",
            type="committed",
            occurred_at="2026-08-18T10:00:00Z",
        ),
    )
    store.save_snapshot_and_events(snapshot, [reused_event])
    store.link_events_to_preflight(
        [reused_event.id],
        reservation.run_id,
        reused=True,
    )
    reused_detail = store.get_event_detail(reused_event.id)
    assert reused_detail is not None
    assert reused_detail["preflight"]["run_id"] == reservation.run_id
    assert reused_detail["preflight"]["reused"] == 1
    assert store.list_preflight_runs(number=event.number)[0][
        "linked_event_count"
    ] == 2
    assert store.list_preflight_runs(number=event.number)[0][
        "reused_event_count"
    ] == 1
    assert store.list_preflight_runs(statuses=("failure",))[0][
        "run_id"
    ] == reservation.run_id
    assert store.list_preflight_runs(statuses=()) == []
    assert len(store.list_preflight_runs(limit=None)) == 1
    assert store.list_preflight_runs(number=999) == []
    assert store.get_preflight_run("missing-preflight") is None


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
    store.initialize_preflight_steps(
        reservation.run_id,
        [
            {"name": "active", "command": ["python", "active.py"]},
            {"name": "waiting", "command": ["python", "waiting.py"]},
        ],
    )
    store.update_preflight_step(
        reservation.run_id,
        0,
        status="running",
        timeout_seconds=120,
    )

    store.recover_interrupted_work()

    recovered = store.load_preflight_result("demo:7:sha-a:revision-a")
    assert recovered is not None
    assert recovered.status == "error"
    detail = store.get_preflight_run(reservation.run_id)
    assert detail is not None
    assert [step["status"] for step in detail["steps"]] == ["error", "skipped"]
    assert detail["steps"][0]["error"] == "服务异常退出，CI 步骤未正常结束"
    assert detail["steps"][1]["error"] == "服务异常退出，步骤未执行"
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


def test_initialize_backfills_legacy_event_preflight_links(
    tmp_path,
    snapshot_factory,
) -> None:
    """升级旧数据库时应恢复原事件与 CI 运行的详情关联。"""

    store = StateStore(tmp_path / "state.db")
    store.initialize()
    snapshot = snapshot_factory(provider="github-main", repository_id="demo")
    event = detect_events(None, snapshot, emit_initial=True)[0]
    store.save_snapshot_and_events(snapshot, [event])
    reservation = store.begin_preflight_run(
        proposed_run_id="legacy-preflight",
        idempotency_key="demo:7:legacy-preflight",
        event_id=event.id,
        repository_id="demo",
        number=event.number,
        head_sha=event.new.head_sha,
        config_revision="revision-a",
        max_attempts=2,
    )
    assert reservation is not None
    with store.connect() as connection:
        connection.execute("DROP TABLE event_preflight_links")

    store.initialize()

    detail = store.get_event_detail(event.id)
    assert detail is not None
    assert detail["preflight"]["run_id"] == reservation.run_id
    assert detail["preflight"]["reused"] == 0
