"""SQLite 幂等与资源租约测试。"""

from teamwork_review_agents.events import detect_events
from teamwork_review_agents.state import StateStore


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
