"""快照变化检测测试。"""

from teamwork_review_agents.events import detect_events


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
