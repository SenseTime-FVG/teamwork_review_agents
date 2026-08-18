"""快照变化检测测试。"""

from datetime import UTC, datetime

from teamwork_review_agents.events import (
    detect_activity_events,
    detect_events,
    detect_first_seen_events,
    detect_target_branch_event,
)
from teamwork_review_agents.models import ChangeRequestActivity


def test_first_snapshot_builds_baseline_by_default(snapshot_factory) -> None:
    snapshot = snapshot_factory()
    assert detect_events(None, snapshot) == []
    events = detect_events(None, snapshot, emit_initial=True)
    assert [event.type for event in events] == ["change_request.discovered"]


def test_detects_semantic_field_changes(snapshot_factory) -> None:
    old = snapshot_factory()
    new = snapshot_factory(
        head_sha="b" * 40,
        approvals=2,
        pipeline_status="success",
        merge_status="can_be_merged",
    )
    events = detect_events(old, new)
    types = {event.type for event in events}
    assert types == {
        "change_request.commits_changed",
        "change_request.approvals_changed",
        "change_request.pipeline_changed",
        "change_request.merge_status_changed",
        "change_request.updated",
    }
    assert len({event.id for event in events}) == len(events)
    assert [event.id for event in events] == [event.id for event in detect_events(old, new)]


def test_detects_merged_state(snapshot_factory) -> None:
    old = snapshot_factory()
    new = snapshot_factory(state="merged")
    types = {event.type for event in detect_events(old, new)}
    assert "change_request.merged" in types
    assert "change_request.updated" in types


def test_ignores_only_updated_timestamp(snapshot_factory) -> None:
    old = snapshot_factory()
    new = snapshot_factory(updated_at="2026-08-17T09:00:00Z")
    assert detect_events(old, new) == []


def test_target_branch_change_is_one_batch_scoped_event(snapshot_factory) -> None:
    """目标分支 Head 变化应独立成事件，并允许未来同值往返再次触发。"""

    old = snapshot_factory(target_head_sha="a" * 40)
    new = snapshot_factory(target_head_sha="b" * 40)
    occurred_at = datetime(2026, 8, 17, 9, tzinfo=UTC)

    events = detect_target_branch_event(
        old,
        new,
        batch_id="scan-one",
        occurred_at=occurred_at,
    )
    repeated = detect_target_branch_event(
        old,
        new,
        batch_id="scan-one",
        occurred_at=occurred_at,
    )
    later = detect_target_branch_event(
        old,
        new,
        batch_id="scan-two",
        occurred_at=occurred_at,
    )

    assert [event.type for event in events] == [
        "change_request.target_commits_changed"
    ]
    assert events[0].changed_fields == ("target_head_sha",)
    assert events[0].occurred_at == occurred_at
    assert repeated[0].id == events[0].id
    assert later[0].id != events[0].id
    assert detect_events(old, new) == []


def test_target_branch_change_requires_existing_open_baseline(snapshot_factory) -> None:
    """首次建立目标 Head 基线及已结束 PR 都不应补发目标变化事件。"""

    current = snapshot_factory(target_head_sha="b" * 40)
    assert (
        detect_target_branch_event(
            None,
            current,
            batch_id="scan-one",
            occurred_at=current.updated_at,
        )
        == []
    )
    empty_baseline = snapshot_factory(target_head_sha="")
    assert (
        detect_target_branch_event(
            empty_baseline,
            current,
            batch_id="scan-one",
            occurred_at=current.updated_at,
        )
        == []
    )
    merged = current.model_copy(update={"state": "merged"})
    assert (
        detect_target_branch_event(
            snapshot_factory(target_head_sha="a" * 40),
            merged,
            batch_id="scan-one",
            occurred_at=current.updated_at,
        )
        == []
    )


def test_repeated_state_transition_uses_occurrence_time_in_event_id(
    snapshot_factory,
) -> None:
    """先后发生的同名状态转换必须是不同事件，重复检测同一快照仍需幂等。"""

    opened_first = snapshot_factory(updated_at="2026-08-17T08:00:00Z")
    closed_first = snapshot_factory(
        state="closed",
        updated_at="2026-08-17T08:05:00Z",
    )
    reopened = snapshot_factory(updated_at="2026-08-17T08:06:00Z")
    closed_second = snapshot_factory(
        state="closed",
        updated_at="2026-08-17T08:07:00Z",
    )

    first_events = detect_events(opened_first, closed_first)
    repeated_events = detect_events(opened_first, closed_first)
    second_events = detect_events(reopened, closed_second)
    first_closed = next(item for item in first_events if item.type == "change_request.closed")
    second_closed = next(item for item in second_events if item.type == "change_request.closed")

    assert [item.id for item in first_events] == [item.id for item in repeated_events]
    assert first_closed.id != second_closed.id
    assert len({item.batch_id for item in first_events}) == 1


def test_timeline_recovers_close_and_reopen_between_snapshots(snapshot_factory) -> None:
    """最终状态同为打开时，Timeline 仍应恢复中间关闭与重新打开。"""

    old = snapshot_factory(updated_at="2026-08-17T08:00:00Z")
    current = snapshot_factory(updated_at="2026-08-17T08:05:00Z")
    activities = (
        ChangeRequestActivity(
            id="timeline-closed",
            type="closed",
            occurred_at="2026-08-17T08:01:00Z",
        ),
        ChangeRequestActivity(
            id="timeline-reopened",
            type="reopened",
            occurred_at="2026-08-17T08:02:00Z",
        ),
    )

    events = detect_activity_events(old, current, activities, batch_id="scan-1")
    repeated = detect_activity_events(old, current, activities, batch_id="scan-2")

    assert [item.type for item in events] == [
        "change_request.closed",
        "change_request.updated",
        "change_request.reopened",
        "change_request.updated",
    ]
    assert events[0].old is not None and events[0].old.state == "opened"
    assert events[0].new.state == "closed"
    assert events[2].old is not None and events[2].old.state == "closed"
    assert events[2].new.state == "opened"
    assert all(item.current_snapshot == current for item in events)
    assert [item.id for item in events] == [item.id for item in repeated]


def test_timeline_commits_and_snapshot_only_fields_are_combined(snapshot_factory) -> None:
    """Timeline 提交与快照中的审批变化应同时产生且不重复。"""

    old = snapshot_factory(updated_at="2026-08-17T08:00:00Z")
    current = snapshot_factory(
        head_sha="c" * 40,
        approvals=1,
        updated_at="2026-08-17T08:05:00Z",
    )
    activities = (
        ChangeRequestActivity(
            id="commit-b",
            type="committed",
            occurred_at="2026-08-17T08:01:00Z",
            data={"sha": "b" * 40},
        ),
        ChangeRequestActivity(
            id="commit-c",
            type="committed",
            occurred_at="2026-08-17T08:02:00Z",
            data={"sha": "c" * 40},
        ),
    )

    events = detect_activity_events(old, current, activities)

    assert [item.type for item in events] == [
        "change_request.commits_changed",
        "change_request.updated",
        "change_request.commits_changed",
        "change_request.updated",
        "change_request.approvals_changed",
        "change_request.updated",
    ]
    assert events[-2].changed_fields == ("approvals",)


def test_first_seen_recent_pr_emits_opened_and_window_activities(
    snapshot_factory,
) -> None:
    """首次扫描应输出窗口内的新建、关闭与重开，而不是只建基线。"""

    current = snapshot_factory(
        created_at="2026-08-17T08:01:00Z",
        updated_at="2026-08-17T08:04:00Z",
    )
    activities = (
        ChangeRequestActivity(
            id="first-close",
            type="closed",
            occurred_at="2026-08-17T08:02:00Z",
        ),
        ChangeRequestActivity(
            id="first-reopen",
            type="reopened",
            occurred_at="2026-08-17T08:03:00Z",
        ),
    )

    events = detect_first_seen_events(
        current,
        activities,
        event_window_start=datetime(2026, 8, 17, 8, tzinfo=UTC),
        emit_discovered=True,
        batch_id="first-scan",
    )

    assert [event.type for event in events] == [
        "change_request.discovered",
        "change_request.opened",
        "change_request.updated",
        "change_request.closed",
        "change_request.updated",
        "change_request.reopened",
        "change_request.updated",
    ]
    assert events[3].old is not None and events[3].old.state == "opened"
    assert events[5].old is not None and events[5].old.state == "closed"
    assert all(event.current_snapshot == current for event in events[1:])


def test_first_seen_historical_pr_only_replays_recent_activities(
    snapshot_factory,
) -> None:
    """历史 PR 不应补发 opened，但窗口内的真实动作仍需完整输出。"""

    current = snapshot_factory(
        created_at="2026-08-16T08:00:00Z",
        updated_at="2026-08-17T08:04:00Z",
    )
    activities = (
        ChangeRequestActivity(
            id="old-outside-window",
            type="closed",
            occurred_at="2026-08-17T07:59:00Z",
        ),
        ChangeRequestActivity(
            id="recent-close",
            type="closed",
            occurred_at="2026-08-17T08:02:00Z",
        ),
        ChangeRequestActivity(
            id="recent-reopen",
            type="reopened",
            occurred_at="2026-08-17T08:03:00Z",
        ),
    )

    events = detect_first_seen_events(
        current,
        activities,
        event_window_start=datetime(2026, 8, 17, 8, tzinfo=UTC),
    )

    assert [event.type for event in events] == [
        "change_request.closed",
        "change_request.updated",
        "change_request.reopened",
        "change_request.updated",
    ]


def test_created_at_upgrade_does_not_create_snapshot_change(snapshot_factory) -> None:
    """旧快照缺少创建时间时，补齐字段不应产生一次性 updated。"""

    old = snapshot_factory(created_at=None)
    current = snapshot_factory(created_at="2026-08-17T07:00:00Z")

    assert detect_events(old, current) == []
