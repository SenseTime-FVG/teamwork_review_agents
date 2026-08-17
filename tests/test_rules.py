"""规则操作符测试。"""

from teamwork_review_agents.config import RuleConfig
from teamwork_review_agents.events import detect_events
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
