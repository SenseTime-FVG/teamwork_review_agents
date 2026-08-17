"""Timeline 活动与扫描编排集成测试。"""

from teamwork_review_agents.config import RuleConfig
from teamwork_review_agents.models import (
    AgentResult,
    ChangeRequestActivity,
    ChangeRequestActivityBatch,
)
from teamwork_review_agents.orchestrator import CycleSummary, Orchestrator


async def test_scan_recovers_transient_state_changes_from_timeline(
    monkeypatch,
    configured_app_factory,
    snapshot_factory,
) -> None:
    """前后快照同为打开时，扫描器仍应持久化 Timeline 中间动作。"""

    config = configured_app_factory()
    repository = config.repositories[0]
    old = snapshot_factory(
        provider=repository.provider,
        repository_id=repository.id,
        updated_at="2026-08-17T08:00:00Z",
    )
    current = snapshot_factory(
        provider=repository.provider,
        repository_id=repository.id,
        updated_at="2026-08-17T08:05:00Z",
    )
    orchestrator = Orchestrator(config, recover_interrupted=False)
    orchestrator.store.save_snapshot_and_events(
        old,
        [],
        activity_cursor={"page": 1, "item_id": "before-close"},
    )

    class FakeProvider:
        """只返回本测试需要的快照和活动。"""

        name = repository.provider

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

        async def list_change_requests(self, *_: object, **__: object):
            return [current]

        async def list_change_request_activities(
            self,
            *_: object,
            cursor: dict[str, object] | None = None,
        ) -> ChangeRequestActivityBatch:
            assert cursor == {"page": 1, "item_id": "before-close"}
            return ChangeRequestActivityBatch(
                activities=(
                    ChangeRequestActivity(
                        id="closed-1",
                        type="closed",
                        occurred_at="2026-08-17T08:01:00Z",
                    ),
                    ChangeRequestActivity(
                        id="reopened-1",
                        type="reopened",
                        occurred_at="2026-08-17T08:02:00Z",
                    ),
                ),
                cursor={"page": 1, "item_id": "reopened-1"},
            )

    monkeypatch.setattr(
        "teamwork_review_agents.orchestrator.create_provider",
        lambda *_args, **_kwargs: FakeProvider(),
    )
    summary = CycleSummary()
    await orchestrator.scan(summary)

    assert summary.new_events == 4
    assert [item.type for item in orchestrator.store.pending_events()] == [
        "change_request.closed",
        "change_request.updated",
        "change_request.reopened",
        "change_request.updated",
    ]
    assert orchestrator.store.load_activity_cursor(
        repository.provider,
        repository.id,
        current.number,
    ) == {"page": 1, "item_id": "reopened-1"}


async def test_scan_initializes_existing_snapshot_before_candidate_filtering(
    monkeypatch,
    configured_app_factory,
    snapshot_factory,
) -> None:
    """升级后即使没有候选更新，也应先为已有快照建立 Timeline 基线。"""

    config = configured_app_factory()
    repository = config.repositories[0]
    snapshot = snapshot_factory(
        provider=repository.provider,
        repository_id=repository.id,
    )
    orchestrator = Orchestrator(config, recover_interrupted=False)
    orchestrator.store.save_snapshot_and_events(snapshot, [])

    class FakeProvider:
        """模拟没有候选更新但支持活动流的 Provider。"""

        name = repository.provider

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

        async def list_change_requests(self, *_: object, **__: object):
            return []

        async def list_change_request_activities(
            self,
            *_: object,
            cursor: dict[str, object] | None = None,
        ) -> ChangeRequestActivityBatch:
            assert cursor is None
            return ChangeRequestActivityBatch(
                cursor={"page": 4, "item_id": "baseline-last"},
                baseline=True,
            )

    monkeypatch.setattr(
        "teamwork_review_agents.orchestrator.create_provider",
        lambda *_args, **_kwargs: FakeProvider(),
    )
    summary = CycleSummary()
    await orchestrator.scan(summary)

    assert summary.snapshots == 0
    assert summary.new_events == 0
    assert orchestrator.store.load_activity_cursor(
        repository.provider,
        repository.id,
        snapshot.number,
    ) == {"page": 4, "item_id": "baseline-last"}


async def test_process_events_marks_events_without_matching_rules_as_unmatched(
    configured_app_factory,
    snapshot_factory,
) -> None:
    """未选择 updated 等事件时，消费后应显示未触发而不是处理中。"""

    from teamwork_review_agents.events import detect_events

    config = configured_app_factory()
    old = snapshot_factory(provider="github-main", repository_id="demo")
    current = snapshot_factory(
        provider="github-main",
        repository_id="demo",
        state="closed",
        updated_at="2026-08-17T08:05:00Z",
    )
    events = detect_events(old, current)
    orchestrator = Orchestrator(config, recover_interrupted=False)
    orchestrator.store.save_snapshot_and_events(current, events)

    summary = CycleSummary()
    await orchestrator.process_events(summary)

    assert summary.processed_events == 2
    assert summary.agent_runs == 0
    records = orchestrator.store.list_events()
    assert {item["status"] for item in records} == {"unmatched"}
    assert {item["trigger_count"] for item in records} == {0}


async def test_deduplicated_run_is_linked_to_every_matching_event(
    monkeypatch,
    configured_app_factory,
    snapshot_factory,
) -> None:
    """单轮去重产生的一次运行应同时计入所有被合并事件。"""

    from teamwork_review_agents.events import detect_events

    config = configured_app_factory()
    config.rules = [
        RuleConfig(
            name="close-review",
            events=["change_request.closed", "change_request.updated"],
            agents=["code-reviewer"],
            deduplicate_per_scan=True,
        )
    ]
    old = snapshot_factory(provider="github-main", repository_id="demo")
    current = snapshot_factory(
        provider="github-main",
        repository_id="demo",
        state="closed",
        updated_at="2026-08-17T08:05:00Z",
    )
    events = detect_events(old, current)
    orchestrator = Orchestrator(config, recover_interrupted=False)
    orchestrator.store.save_snapshot_and_events(current, events)

    async def fake_execute(**kwargs):
        """跳过真实 Codex，仅验证编排产生的调度关联。"""

        return AgentResult(
            run_id="deduplicated-run",
            root_run_id="deduplicated-run",
            agent_name=kwargs["agent_name"],
            status="completed",
        )

    monkeypatch.setattr(orchestrator.executor, "execute", fake_execute)
    summary = CycleSummary()
    await orchestrator.process_events(summary)

    assert summary.agent_runs == 1
    records = orchestrator.store.list_events()
    assert {item["status"] for item in records} == {"completed"}
    assert {item["trigger_count"] for item in records} == {1}


async def test_failed_agent_marks_matching_event_as_failed(
    monkeypatch,
    configured_app_factory,
    snapshot_factory,
) -> None:
    """Agent 返回失败结果时，匹配事件也应进入处理失败状态。"""

    from teamwork_review_agents.events import detect_events

    config = configured_app_factory()
    config.rules = [
        RuleConfig(
            name="close-review",
            events=["change_request.closed"],
            agents=["code-reviewer"],
        )
    ]
    old = snapshot_factory(provider="github-main", repository_id="demo")
    current = snapshot_factory(
        provider="github-main",
        repository_id="demo",
        state="closed",
        updated_at="2026-08-17T08:05:00Z",
    )
    events = detect_events(old, current)
    orchestrator = Orchestrator(config, recover_interrupted=False)
    orchestrator.store.save_snapshot_and_events(current, events)

    async def fake_execute(**kwargs):
        """返回失败结果以验证事件终态。"""

        return AgentResult(
            run_id="failed-run",
            root_run_id="failed-run",
            agent_name=kwargs["agent_name"],
            status="failed",
            error="审核失败",
        )

    monkeypatch.setattr(orchestrator.executor, "execute", fake_execute)
    summary = CycleSummary()
    await orchestrator.process_events(summary)

    records = {
        item["event_type"]: item for item in orchestrator.store.list_events()
    }
    assert records["change_request.closed"]["status"] == "failed"
    assert records["change_request.closed"]["error"] == "审核失败"
    assert records["change_request.updated"]["status"] == "unmatched"
