"""规则操作符测试。"""

from datetime import UTC, datetime

from teamwork_review_agents.config import RuleConfig
from teamwork_review_agents.events import detect_events
from teamwork_review_agents.orchestrator import plan_rule_invocations
from teamwork_review_agents.rules import rule_matches


def test_matches_new_snapshot_conditions(snapshot_factory) -> None:
    old = snapshot_factory()
    new = snapshot_factory(
        approvals=2,
        pipeline_status="success",
        merge_status="can_be_merged",
        labels=("backend", "security"),
    )
    event = next(
        item for item in detect_events(old, new) if item.type == "change_request.updated"
    )
    rule = RuleConfig(
        name="merge-ready",
        events=["change_request.updated"],
        agents=["merge-manager"],
        conditions={
            "state": "opened",
            "approvals__gte": 2,
            "pipeline_status__in": ["success", "skipped"],
            "labels__contains": "security",
            "approvals__changed": True,
        },
    )
    assert rule_matches(rule, event)

def test_supports_old_and_new_paths(snapshot_factory) -> None:
    old = snapshot_factory(draft=True)
    new = snapshot_factory(draft=False)
    event = next(
        item for item in detect_events(old, new) if item.type == "change_request.draft_changed"
    )
    rule = RuleConfig(
        name="ready",
        events=["change_request.draft_changed"],
        agents=["reviewer"],
        conditions={"old.draft": True, "new.draft": False},
    )
    assert rule_matches(rule, event)


def test_rule_can_deduplicate_matching_events_per_scan(snapshot_factory) -> None:
    """规则去重开启时，同批次只保留时间最新的目标事件。"""

    old = snapshot_factory()
    new = snapshot_factory(state="closed", updated_at="2026-08-17T08:05:00Z")
    events = detect_events(old, new)
    selected = ["change_request.closed", "change_request.updated"]

    separate_rule = RuleConfig(
        name="separate",
        events=selected,
        agents=["reviewer"],
    )
    deduplicated_rule = RuleConfig(
        name="deduplicated",
        events=selected,
        agents=["reviewer"],
        deduplicate_per_scan=True,
    )

    separate = plan_rule_invocations([separate_rule], events)
    deduplicated = plan_rule_invocations([deduplicated_rule], events)
    assert len(separate) == 2
    assert [item.actions for item in separate] == [
        ("change_request.closed",),
        ("change_request.updated",),
    ]
    assert len(deduplicated) == 1
    assert deduplicated[0].actions == (deduplicated[0].events[0].type,)
    assert deduplicated[0].events[0] is max(
        events,
        key=lambda event: (event.occurred_at, event.id),
    )


def test_rule_can_deduplicate_different_change_requests_by_source_branch(
    snapshot_factory,
) -> None:
    """源分支去重应跨 MR / PR 选择最新匹配事件。"""

    old_one = snapshot_factory(number=1, source_branch="feature/shared", head_sha="a" * 40)
    old_two = snapshot_factory(number=2, source_branch="feature/shared", head_sha="c" * 40)
    event_one = next(
        event
        for event in detect_events(
            old_one,
            old_one.model_copy(
                update={
                    "head_sha": "b" * 40,
                    "updated_at": datetime(2026, 8, 17, 8, 1, tzinfo=UTC),
                }
            ),
            batch_id="scan-one",
        )
        if event.type == "change_request.commits_changed"
    )
    event_two = next(
        event
        for event in detect_events(
            old_two,
            old_two.model_copy(
                update={
                    "head_sha": "d" * 40,
                    "updated_at": datetime(2026, 8, 17, 8, 2, tzinfo=UTC),
                }
            ),
            batch_id="scan-one",
        )
        if event.type == "change_request.commits_changed"
    )
    rule = RuleConfig(
        name="source-dedup",
        events=["change_request.commits_changed"],
        agents=["reviewer"],
        deduplicate_source_branch_per_scan=True,
    )

    invocations = plan_rule_invocations([rule], [event_one, event_two])

    assert len(invocations) == 1
    assert invocations[0].events == (event_two,)


def test_rule_can_deduplicate_by_any_enabled_branch_dimension(snapshot_factory) -> None:
    """混合去重开关按任一相同键形成连通组。"""

    def commit_event(
        number: int,
        source: str,
        target: str,
        head: str,
        next_head: str,
        updated: datetime,
    ):
        old = snapshot_factory(
            number=number,
            source_branch=source,
            target_branch=target,
            head_sha=head,
        )
        current = old.model_copy(
            update={
                "head_sha": next_head,
                "updated_at": updated,
            }
        )
        return next(
            event
            for event in detect_events(old, current, batch_id="scan-one")
            if event.type == "change_request.commits_changed"
        )

    first = commit_event(
        1,
        "feature/one",
        "main",
        "a" * 40,
        "b" * 40,
        datetime(2026, 8, 17, 8, 1, tzinfo=UTC),
    )
    second = commit_event(
        2,
        "feature/two",
        "main",
        "c" * 40,
        "d" * 40,
        datetime(2026, 8, 17, 8, 2, tzinfo=UTC),
    )
    third = commit_event(
        3,
        "feature/two",
        "release",
        "d" * 40,
        "e" * 40,
        datetime(2026, 8, 17, 8, 3, tzinfo=UTC),
    )
    rule = RuleConfig(
        name="mixed-dedup",
        events=["change_request.commits_changed"],
        agents=["reviewer"],
        deduplicate_source_branch_per_scan=True,
        deduplicate_target_branch_per_scan=True,
    )

    invocations = plan_rule_invocations([rule], [first, second, third])

    assert len(invocations) == 1
    assert invocations[0].events == (third,)


def test_rule_workspace_inheritance_defaults_to_disabled() -> None:
    """规则默认隔离 sub-agent 工作区，只有显式开启才共享。"""

    isolated = RuleConfig(
        name="isolated",
        events=["change_request.updated"],
        agents=["reviewer"],
    )
    shared = RuleConfig(
        name="shared",
        events=["change_request.updated"],
        agents=["reviewer"],
        inherit_workspace=True,
    )
    assert isolated.inherit_workspace is False
    assert shared.inherit_workspace is True
