"""规则操作符测试。"""

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
    """规则去重开启时，同批次多个动作只规划一次目标 Agent。"""

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
    assert deduplicated[0].actions == (
        "change_request.closed",
        "change_request.updated",
    )


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
