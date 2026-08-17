"""快照变化检测测试。"""

from teamwork_review_agents.events import detect_activity_events, detect_events
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
