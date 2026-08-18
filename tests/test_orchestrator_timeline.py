"""Timeline 活动与扫描编排集成测试。"""

from datetime import UTC, datetime, timedelta

from teamwork_review_agents.config import RuleConfig
from teamwork_review_agents.events import detect_target_branch_event
from teamwork_review_agents.models import (
    AgentResult,
    ChangeRequestActivity,
    ChangeRequestActivityBatch,
)
from teamwork_review_agents.orchestrator import CycleSummary, Orchestrator
from teamwork_review_agents.state import (
    CANCEL_SOURCE_ADMINISTRATOR,
    CANCEL_SOURCE_SERVICE_SHUTDOWN,
)


async def test_scan_detects_target_head_for_pr_outside_updated_candidates(
    monkeypatch,
    configured_app_factory,
    snapshot_factory,
) -> None:
    """目标分支变化必须覆盖未进入 Provider 更新时间候选的打开 PR。"""

    config = configured_app_factory()
    repository = config.repositories[0]
    old = snapshot_factory(
        provider=repository.provider,
        repository_id=repository.id,
        target_head_sha="a" * 40,
    )
    orchestrator = Orchestrator(config, recover_interrupted=False)
    orchestrator.store.save_snapshot_and_events(old, [])
    branch_calls: list[str] = []

    class FakeProvider:
        """模拟 PR 本身未更新、目标分支已经推进的 Provider。"""

        name = repository.provider

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

        async def list_change_requests(self, *_: object, **__: object):
            return []

        async def list_change_request_activities(self, *_: object, **__: object):
            return None

        async def get_branch_head(
            self,
            _repository,
            branch: str,
        ) -> str:
            branch_calls.append(branch)
            return "b" * 40

    monkeypatch.setattr(
        "teamwork_review_agents.orchestrator.create_provider",
        lambda *_args, **_kwargs: FakeProvider(),
    )
    summary = CycleSummary()
    await orchestrator.scan(summary)

    assert branch_calls == ["main"]
    assert summary.new_events == 1
    assert [event.type for event in orchestrator.store.pending_events()] == [
        "change_request.target_commits_changed"
    ]
    saved = orchestrator.store.load_snapshot(old.key)
    assert saved is not None
    assert saved.target_head_sha == "b" * 40

    repeated_summary = CycleSummary()
    await orchestrator.scan(repeated_summary)
    assert repeated_summary.new_events == 0
    assert len(orchestrator.store.pending_events()) == 1


async def test_first_target_head_only_builds_baseline(
    monkeypatch,
    configured_app_factory,
    snapshot_factory,
) -> None:
    """旧快照没有目标 Head 时，升级后的首次扫描不能补发历史变化。"""

    config = configured_app_factory()
    repository = config.repositories[0]
    old = snapshot_factory(
        provider=repository.provider,
        repository_id=repository.id,
    )
    orchestrator = Orchestrator(config, recover_interrupted=False)
    orchestrator.store.save_snapshot_and_events(old, [])

    class FakeProvider:
        """只提供目标分支基线。"""

        name = repository.provider

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

        async def list_change_requests(self, *_: object, **__: object):
            return []

        async def list_change_request_activities(self, *_: object, **__: object):
            return None

        async def get_branch_head(self, *_: object) -> str:
            return "b" * 40

    monkeypatch.setattr(
        "teamwork_review_agents.orchestrator.create_provider",
        lambda *_args, **_kwargs: FakeProvider(),
    )
    summary = CycleSummary()
    await orchestrator.scan(summary)

    assert summary.new_events == 0
    assert orchestrator.store.pending_events() == []
    saved = orchestrator.store.load_snapshot(old.key)
    assert saved is not None
    assert saved.target_head_sha == "b" * 40


async def test_unmatched_target_event_is_removed(
    configured_app_factory,
    snapshot_factory,
) -> None:
    """没有规则匹配的目标变化事件处理后不进入长期历史。"""

    config = configured_app_factory()
    orchestrator = Orchestrator(config, recover_interrupted=False)
    old = snapshot_factory(target_head_sha="a" * 40)
    current = snapshot_factory(target_head_sha="b" * 40)
    event = detect_target_branch_event(
        old,
        current,
        batch_id="scan-one",
        occurred_at=current.updated_at,
    )[0]
    orchestrator.store.save_snapshot_and_events(current, [event])

    summary = CycleSummary()
    await orchestrator.process_events(summary)

    assert summary.processed_events == 1
    assert orchestrator.store.list_events(None) == []


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
        activity_cursor={
            "page": 1,
            "item_id": "before-close",
            "latest_activity_checked": True,
        },
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
            assert cursor == {
                "page": 1,
                "item_id": "before-close",
                "latest_activity_checked": True,
            }
            latest = ChangeRequestActivity(
                id="reopened-1",
                type="reopened",
                occurred_at="2026-08-17T08:02:00Z",
            )
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
                latest_activity=latest,
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
    cursor = orchestrator.store.load_activity_cursor(
        repository.provider,
        repository.id,
        current.number,
    )
    assert cursor is not None
    assert cursor["page"] == 1
    assert cursor["item_id"] == "reopened-1"
    assert cursor["latest_activity_checked"] is True
    assert cursor["latest_activity"]["type"] == "reopened"


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
                latest_activity=ChangeRequestActivity(
                    id="baseline-merged",
                    type="merged",
                    occurred_at="2026-08-16T08:00:00Z",
                ),
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
    cursor = orchestrator.store.load_activity_cursor(
        repository.provider,
        repository.id,
        snapshot.number,
    )
    assert cursor is not None
    assert cursor["page"] == 4
    assert cursor["item_id"] == "baseline-last"
    assert cursor["latest_activity_checked"] is True
    assert cursor["latest_activity"]["type"] == "merged"
    assert orchestrator.store.pending_events() == []
    latest_event = orchestrator.store.list_snapshots()[0]["latest_event"]
    assert latest_event["event_type"] == "change_request.merged"
    assert latest_event["provider_event_id"] == "baseline-merged"


async def test_first_scan_replays_one_cycle_then_uses_saved_cursor(
    monkeypatch,
    configured_app_factory,
    snapshot_factory,
) -> None:
    """首次扫描回看一个周期，下一轮不得重复同一 Timeline 活动。"""

    config = configured_app_factory()
    config.scanner.emit_initial_events = True
    repository = config.repositories[0]
    now = datetime.now(UTC)
    current = snapshot_factory(
        provider=repository.provider,
        repository_id=repository.id,
        created_at=now - timedelta(minutes=4),
        updated_at=now - timedelta(minutes=1),
    )
    orchestrator = Orchestrator(config, recover_interrupted=False)
    seen_since: list[datetime] = []

    class FakeProvider:
        """首次返回窗口活动，后续根据已保存游标返回空增量。"""

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
            since: datetime | None = None,
        ) -> ChangeRequestActivityBatch:
            if cursor is not None:
                assert cursor["page"] == 1
                assert cursor["item_id"] == "first-reopen"
                assert cursor["latest_activity_checked"] is True
                assert cursor["latest_activity"]["id"] == "first-reopen"
                assert since is None
                return ChangeRequestActivityBatch(
                    cursor={"page": 1, "item_id": "first-reopen"},
                    latest_activity=ChangeRequestActivity.model_validate(
                        cursor["latest_activity"]
                    ),
                )
            assert since is not None
            seen_since.append(since)
            return ChangeRequestActivityBatch(
                activities=(
                    ChangeRequestActivity(
                        id="first-close",
                        type="closed",
                        occurred_at=now - timedelta(minutes=3),
                    ),
                    ChangeRequestActivity(
                        id="first-reopen",
                        type="reopened",
                        occurred_at=now - timedelta(minutes=2),
                    ),
                ),
                latest_activity=ChangeRequestActivity(
                    id="first-reopen",
                    type="reopened",
                    occurred_at=now - timedelta(minutes=2),
                ),
                cursor={"page": 1, "item_id": "first-reopen"},
            )

    monkeypatch.setattr(
        "teamwork_review_agents.orchestrator.create_provider",
        lambda *_args, **_kwargs: FakeProvider(),
    )
    first_summary = CycleSummary()
    await orchestrator.scan(first_summary)
    second_summary = CycleSummary()
    await orchestrator.scan(second_summary)

    assert len(seen_since) == 1
    assert now - timedelta(minutes=6) < seen_since[0] < now - timedelta(minutes=4)
    assert first_summary.new_events == 7
    assert second_summary.new_events == 0
    assert [item.type for item in orchestrator.store.pending_events()] == [
        "change_request.discovered",
        "change_request.opened",
        "change_request.updated",
        "change_request.closed",
        "change_request.updated",
        "change_request.reopened",
        "change_request.updated",
    ]


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


async def test_service_shutdown_requeues_event_and_resumes_same_run(
    monkeypatch,
    configured_app_factory,
    snapshot_factory,
) -> None:
    """服务停止中断后应复用原运行，并且不消耗事件或 Agent 重试次数。"""

    from teamwork_review_agents.events import detect_events

    config = configured_app_factory()
    config.runtime.event_retry_count = 0
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
    original_run_id = "service-interrupted-run"

    async def fake_interrupted_execute(**kwargs):
        """模拟服务停止时排队运行被持久化取消。"""

        reservation = orchestrator.store.begin_agent_run(
            proposed_run_id=original_run_id,
            root_run_id=None,
            parent_run_id=None,
            idempotency_key=kwargs["idempotency_key"],
            event_id=kwargs["event"].id,
            rule_name=kwargs["rule_name"],
            agent_name=kwargs["agent_name"],
            resource_key=kwargs["event"].resource_key,
            prompt="",
            max_attempts=1,
        )
        assert reservation is not None
        orchestrator.store.request_cancel_run(
            reservation.run_id,
            source=CANCEL_SOURCE_SERVICE_SHUTDOWN,
        )
        return AgentResult(
            run_id=reservation.run_id,
            root_run_id=reservation.root_run_id,
            agent_name=kwargs["agent_name"],
            status="cancelled",
            error="服务停止时中断运行",
        )

    monkeypatch.setattr(orchestrator.executor, "execute", fake_interrupted_execute)
    first_summary = CycleSummary()
    await orchestrator.process_events(first_summary)

    first_records = {
        item["event_type"]: item for item in orchestrator.store.list_events()
    }
    assert first_records["change_request.closed"]["status"] == "pending"
    assert first_records["change_request.closed"]["attempts"] == 0
    assert first_summary.processed_events == 1
    assert first_summary.errors == []

    async def fake_resumed_execute(**kwargs):
        """模拟新服务使用同一幂等记录完成恢复执行。"""

        reservation = orchestrator.store.begin_agent_run(
            proposed_run_id="unused-run-id",
            root_run_id=None,
            parent_run_id=None,
            idempotency_key=kwargs["idempotency_key"],
            event_id=kwargs["event"].id,
            rule_name=kwargs["rule_name"],
            agent_name=kwargs["agent_name"],
            resource_key=kwargs["event"].resource_key,
            prompt="",
            max_attempts=1,
        )
        assert reservation is not None
        assert reservation.run_id == original_run_id
        assert reservation.attempts == 1
        result = AgentResult(
            run_id=reservation.run_id,
            root_run_id=reservation.root_run_id,
            agent_name=kwargs["agent_name"],
            status="completed",
        )
        orchestrator.store.finish_agent_run(result)
        return result

    monkeypatch.setattr(orchestrator.executor, "execute", fake_resumed_execute)
    second_summary = CycleSummary()
    await orchestrator.process_events(second_summary)

    second_records = {
        item["event_type"]: item for item in orchestrator.store.list_events()
    }
    assert second_records["change_request.closed"]["status"] == "completed"
    assert second_records["change_request.closed"]["attempts"] == 1
    assert second_summary.errors == []
    run = orchestrator.store.get_run(original_run_id)
    assert run is not None
    assert run["status"] == "completed"
    assert run["attempts"] == 1


async def test_administrator_cancel_keeps_event_terminal(
    monkeypatch,
    configured_app_factory,
    snapshot_factory,
) -> None:
    """管理员主动取消后，事件应保持已取消终态且不再自动领取。"""

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

    async def fake_cancelled_execute(**kwargs):
        """模拟管理员在运行排队阶段主动取消。"""

        reservation = orchestrator.store.begin_agent_run(
            proposed_run_id="administrator-cancelled-run",
            root_run_id=None,
            parent_run_id=None,
            idempotency_key=kwargs["idempotency_key"],
            event_id=kwargs["event"].id,
            rule_name=kwargs["rule_name"],
            agent_name=kwargs["agent_name"],
            resource_key=kwargs["event"].resource_key,
            prompt="",
            max_attempts=3,
        )
        assert reservation is not None
        orchestrator.store.request_cancel_run(
            reservation.run_id,
            source=CANCEL_SOURCE_ADMINISTRATOR,
        )
        return AgentResult(
            run_id=reservation.run_id,
            root_run_id=reservation.root_run_id,
            agent_name=kwargs["agent_name"],
            status="cancelled",
            error="运行已由管理员取消",
        )

    monkeypatch.setattr(orchestrator.executor, "execute", fake_cancelled_execute)
    summary = CycleSummary()
    await orchestrator.process_events(summary)

    records = {
        item["event_type"]: item for item in orchestrator.store.list_events()
    }
    assert records["change_request.closed"]["status"] == "cancelled"
    assert records["change_request.closed"]["error"] == "关联 Agent 运行已由管理员取消"
    assert records["change_request.updated"]["status"] == "unmatched"
    assert orchestrator.store.pending_events() == []
    assert summary.errors == []
