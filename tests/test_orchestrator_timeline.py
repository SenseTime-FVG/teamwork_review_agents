"""Timeline 活动与扫描编排集成测试。"""

import asyncio

from datetime import UTC, datetime, timedelta

from teamwork_review_agents.config import EnvironmentVariable, RuleConfig
from teamwork_review_agents.events import detect_events, detect_target_branch_event
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


async def test_scan_uses_repository_tokens_and_isolates_repository_errors(
    monkeypatch,
    configured_app_factory,
) -> None:
    """同一 Provider 下的仓库应分别解析 Token，且单仓库失败不阻塞后续扫描。"""

    config = configured_app_factory()
    first = config.repositories[0]
    first.environment["GITHUB_TOKEN"] = EnvironmentVariable(
        from_system="FIRST_REPOSITORY_GITHUB_TOKEN",
    )
    second = first.model_copy(
        update={
            "id": "second",
            "project": "owner/second",
            "workspace": first.workspace.parent / "second",
            "environment": {
                "GITHUB_TOKEN": EnvironmentVariable(
                    from_system="SECOND_REPOSITORY_GITHUB_TOKEN",
                ),
            },
        }
    )
    config.repositories.append(second)
    monkeypatch.setenv("FIRST_REPOSITORY_GITHUB_TOKEN", "first-token")
    monkeypatch.setenv("SECOND_REPOSITORY_GITHUB_TOKEN", "second-token")
    created_tokens: list[str] = []
    scanned: list[tuple[str, str]] = []

    class FakeProvider:
        """记录每个客户端的 Token，并模拟第一个仓库认证失败。"""

        name = "github-main"

        def __init__(self, token: str) -> None:
            self.token = token

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

        async def list_change_requests(self, repository, *, updated_since=None):
            scanned.append((repository.id, self.token))
            if repository.id == "demo":
                raise RuntimeError("认证失败")
            return []

    def fake_create_provider(*_args, token: str, **_kwargs):
        created_tokens.append(token)
        return FakeProvider(token)

    monkeypatch.setattr(
        "teamwork_review_agents.orchestrator.create_provider",
        fake_create_provider,
    )
    orchestrator = Orchestrator(config, recover_interrupted=False)
    summary = CycleSummary()

    await orchestrator.scan(summary)

    assert created_tokens == ["first-token", "second-token"]
    assert scanned == [
        ("demo", "first-token"),
        ("second", "second-token"),
    ]
    assert summary.repositories == 2
    assert summary.errors == ["扫描仓库 demo 失败：认证失败"]


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


async def test_unmatched_target_event_is_retained(
    configured_app_factory,
    snapshot_factory,
) -> None:
    """没有规则匹配的目标变化事件也应保留终态供历史查询。"""

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
    records = orchestrator.store.list_events(None)
    assert len(records) == 1
    assert records[0]["event_id"] == event.id
    assert records[0]["status"] == "unmatched"


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


async def test_unmatched_event_settles_while_same_batch_agent_is_running(
    monkeypatch,
    configured_app_factory,
    snapshot_factory,
) -> None:
    """同批次 Agent 运行时，无匹配事件应立即显示未触发。"""

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
    started = asyncio.Event()
    release = asyncio.Event()

    async def waiting_execute(**kwargs):
        """保持 Agent 运行，以便检查同批次其他事件的中间状态。"""

        started.set()
        await release.wait()
        return AgentResult(
            run_id="waiting-run",
            root_run_id="waiting-run",
            agent_name=kwargs["agent_name"],
            status="completed",
        )

    monkeypatch.setattr(orchestrator.executor, "execute", waiting_execute)
    summary = CycleSummary()
    processing = asyncio.create_task(orchestrator.process_events(summary))

    await asyncio.wait_for(started.wait(), timeout=1)
    try:
        records = {
            item["event_type"]: item for item in orchestrator.store.list_events()
        }
        assert records["change_request.closed"]["status"] == "triggered"
        assert records["change_request.updated"]["status"] == "unmatched"
        assert records["change_request.updated"]["trigger_count"] == 0
    finally:
        release.set()
        await asyncio.wait_for(processing, timeout=1)

    final_records = {
        item["event_type"]: item for item in orchestrator.store.list_events()
    }
    assert final_records["change_request.closed"]["status"] == "completed"
    assert final_records["change_request.updated"]["status"] == "unmatched"
    assert summary.processed_events == 2


async def test_deduplicated_run_only_triggers_latest_matching_event(
    monkeypatch,
    configured_app_factory,
    snapshot_factory,
) -> None:
    """单轮去重只为最新匹配事件创建运行，较早事件保持未触发。"""

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
    records = {item["event_id"]: item for item in orchestrator.store.list_events()}
    representative = max(events, key=lambda event: (event.occurred_at, event.id))
    assert records[representative.id]["status"] == "completed"
    assert records[representative.id]["trigger_count"] == 1
    for event in events:
        if event.id == representative.id:
            continue
        assert records[event.id]["status"] == "unmatched"
        assert records[event.id]["trigger_count"] == 0


async def test_source_branch_dedup_suppresses_older_change_request(
    monkeypatch,
    configured_app_factory,
    snapshot_factory,
) -> None:
    """跨 MR / PR 的源分支去重只让最新事件触发且不建立旧事件关联。"""

    config = configured_app_factory()
    config.rules = [
        RuleConfig(
            name="source-review",
            events=["change_request.commits_changed"],
            agents=["code-reviewer"],
            deduplicate_source_branch_per_scan=True,
        )
    ]
    old_one = snapshot_factory(
        provider="github-main",
        repository_id="demo",
        number=7,
        source_branch="feature/shared",
        head_sha="a" * 40,
    )
    old_two = old_one.model_copy(update={"number": 8, "head_sha": "c" * 40})
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
            batch_id="scan-source",
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
            batch_id="scan-source",
        )
        if event.type == "change_request.commits_changed"
    )
    orchestrator = Orchestrator(config, recover_interrupted=False)
    orchestrator.store.save_snapshot_and_events(old_one, [event_one])
    orchestrator.store.save_snapshot_and_events(old_two, [event_two])

    async def fake_execute(**kwargs):
        """跳过真实 Codex，仅验证代表事件的调度。"""

        return AgentResult(
            run_id="source-deduplicated-run",
            root_run_id="source-deduplicated-run",
            agent_name=kwargs["agent_name"],
            status="completed",
        )

    monkeypatch.setattr(orchestrator.executor, "execute", fake_execute)
    summary = CycleSummary()
    await orchestrator.process_events(summary)

    records = {item["event_id"]: item for item in orchestrator.store.list_events(None)}
    assert summary.agent_runs == 1
    assert records[event_one.id]["status"] == "unmatched"
    assert records[event_one.id]["unmatched_reason"] == "scan_deduplicated"
    assert records[event_one.id]["trigger_count"] == 0
    assert records[event_two.id]["status"] == "completed"
    assert records[event_two.id]["trigger_count"] == 1


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


async def test_different_change_requests_run_concurrently_and_fill_new_capacity(
    monkeypatch,
    configured_app_factory,
    snapshot_factory,
) -> None:
    """长任务运行期间，新入队的其他 PR 应立即使用空闲额度。"""

    config = configured_app_factory()
    config.rules = [
        RuleConfig(
            name="state-review",
            events=["change_request.closed"],
            agents=["code-reviewer"],
        )
    ]
    orchestrator = Orchestrator(config, recover_interrupted=False)
    first_old = snapshot_factory(number=41)
    first_new = snapshot_factory(
        number=41,
        state="closed",
        updated_at="2026-08-17T08:05:00Z",
    )
    first_event = detect_events(first_old, first_new, batch_id="first-batch")[0]
    orchestrator.store.save_snapshot_and_events(first_new, [first_event])

    first_started = asyncio.Event()
    second_started = asyncio.Event()
    release_first = asyncio.Event()

    async def fake_execute(**kwargs):
        """让第一个 PR 持续运行，并记录第二个 PR 是否及时启动。"""

        event = kwargs["event"]
        if event.number == 41:
            first_started.set()
            await release_first.wait()
        else:
            second_started.set()
        return AgentResult(
            run_id=f"run-{event.number}",
            root_run_id=f"run-{event.number}",
            agent_name=kwargs["agent_name"],
            status="completed",
        )

    monkeypatch.setattr(orchestrator.executor, "execute", fake_execute)
    summary = CycleSummary()
    dispatch = asyncio.create_task(orchestrator.process_events(summary))
    await asyncio.wait_for(first_started.wait(), timeout=1)

    second_old = snapshot_factory(number=42)
    second_new = snapshot_factory(
        number=42,
        state="closed",
        updated_at="2026-08-17T08:06:00Z",
    )
    second_event = detect_events(second_old, second_new, batch_id="second-batch")[0]
    orchestrator.store.save_snapshot_and_events(second_new, [second_event])

    await asyncio.wait_for(second_started.wait(), timeout=1)
    release_first.set()
    await asyncio.wait_for(dispatch, timeout=1)

    assert summary.agent_runs == 2
    assert {item["status"] for item in orchestrator.store.list_events()} == {
        "completed"
    }


async def test_batches_for_same_change_request_keep_event_order(
    monkeypatch,
    configured_app_factory,
    snapshot_factory,
) -> None:
    """同一 PR 的后续批次必须等待前序 Agent 完成。"""

    config = configured_app_factory()
    config.rules = [
        RuleConfig(
            name="state-review",
            events=["change_request.closed", "change_request.reopened"],
            agents=["code-reviewer"],
        )
    ]
    orchestrator = Orchestrator(config, recover_interrupted=False)
    opened = snapshot_factory(number=43)
    closed = snapshot_factory(
        number=43,
        state="closed",
        updated_at="2026-08-17T08:05:00Z",
    )
    reopened = snapshot_factory(
        number=43,
        state="opened",
        updated_at="2026-08-17T08:06:00Z",
    )
    closed_event = detect_events(opened, closed, batch_id="close-batch")[0]
    reopened_event = detect_events(closed, reopened, batch_id="reopen-batch")[0]
    orchestrator.store.save_snapshot_and_events(closed, [closed_event])
    orchestrator.store.save_snapshot_and_events(reopened, [reopened_event])

    first_started = asyncio.Event()
    second_started = asyncio.Event()
    release_first = asyncio.Event()
    actions: list[str] = []

    async def fake_execute(**kwargs):
        """阻塞关闭事件，确保重新打开事件不会越过它。"""

        action = kwargs["actions"][0]
        actions.append(action)
        if action == "change_request.closed":
            first_started.set()
            await release_first.wait()
        else:
            second_started.set()
        return AgentResult(
            run_id=f"run-{len(actions)}",
            root_run_id=f"run-{len(actions)}",
            agent_name=kwargs["agent_name"],
            status="completed",
        )

    monkeypatch.setattr(orchestrator.executor, "execute", fake_execute)
    dispatch = asyncio.create_task(orchestrator.process_events(CycleSummary()))
    await asyncio.wait_for(first_started.wait(), timeout=1)
    await asyncio.sleep(0.1)
    assert not second_started.is_set()
    waiting = {
        item["event_type"]: item for item in orchestrator.store.list_events()
    }
    assert waiting["change_request.reopened"]["queue_reason"] == (
        "change_request_order"
    )

    release_first.set()
    await asyncio.wait_for(second_started.wait(), timeout=1)
    await asyncio.wait_for(dispatch, timeout=1)
    assert actions == ["change_request.closed", "change_request.reopened"]


async def test_unmatched_later_batch_settles_while_previous_agent_is_running(
    monkeypatch,
    configured_app_factory,
    snapshot_factory,
) -> None:
    """前序 Agent 运行时，后续批次无匹配事件应立即收敛。"""

    config = configured_app_factory()
    config.rules = [
        RuleConfig(
            name="state-review",
            events=["change_request.closed", "change_request.reopened"],
            agents=["code-reviewer"],
        )
    ]
    orchestrator = Orchestrator(config, recover_interrupted=False)
    opened = snapshot_factory(number=46)
    closed = snapshot_factory(
        number=46,
        state="closed",
        updated_at="2026-08-17T08:05:00Z",
    )
    reopened = snapshot_factory(
        number=46,
        state="opened",
        updated_at="2026-08-17T08:06:00Z",
    )
    closed_event = next(
        event
        for event in detect_events(opened, closed, batch_id="close-batch")
        if event.type == "change_request.closed"
    )
    orchestrator.store.save_snapshot_and_events(closed, [closed_event])

    first_started = asyncio.Event()
    second_started = asyncio.Event()
    release_first = asyncio.Event()

    async def fake_execute(**kwargs):
        """保持前序 Agent 运行，并记录后续匹配事件的启动时机。"""

        action = kwargs["actions"][0]
        if action == "change_request.closed":
            first_started.set()
            await release_first.wait()
        else:
            second_started.set()
        return AgentResult(
            run_id=f"run-{action}",
            root_run_id=f"run-{action}",
            agent_name=kwargs["agent_name"],
            status="completed",
        )

    monkeypatch.setattr(orchestrator.executor, "execute", fake_execute)
    summary = CycleSummary()
    dispatch = asyncio.create_task(orchestrator.process_events(summary))
    await asyncio.wait_for(first_started.wait(), timeout=1)

    later_events = detect_events(closed, reopened, batch_id="reopen-batch")
    orchestrator.store.save_snapshot_and_events(reopened, later_events)

    for _ in range(50):
        records = {
            item["event_type"]: item for item in orchestrator.store.list_events()
        }
        if (
            records["change_request.updated"]["status"] == "unmatched"
            and records["change_request.reopened"]["queue_reason"]
            == "change_request_order"
        ):
            break
        await asyncio.sleep(0.02)
    else:
        raise AssertionError("后续事件没有完成即时收敛与匹配事件排队")

    assert records["change_request.updated"]["queue_reason"] is None
    assert records["change_request.reopened"]["status"] == "pending"
    assert records["change_request.reopened"]["queue_reason"] == (
        "change_request_order"
    )
    assert not second_started.is_set()
    assert summary.processed_events == 1

    release_first.set()
    await asyncio.wait_for(second_started.wait(), timeout=1)
    await asyncio.wait_for(dispatch, timeout=1)

    final_records = {
        item["event_type"]: item for item in orchestrator.store.list_events()
    }
    assert final_records["change_request.closed"]["status"] == "completed"
    assert final_records["change_request.reopened"]["status"] == "completed"
    assert final_records["change_request.updated"]["status"] == "unmatched"
    assert summary.agent_runs == 2
    assert summary.processed_events == 3


async def test_retrying_resource_progresses_while_unrelated_agent_is_running(
    monkeypatch,
    configured_app_factory,
    snapshot_factory,
) -> None:
    """其他 PR 长时间运行时，失败资源仍应独立退避、重试并继续。"""

    config = configured_app_factory()
    config.runtime.event_retry_count = 1
    config.rules = [
        RuleConfig(
            name="state-review",
            events=["change_request.closed", "change_request.reopened"],
            agents=["code-reviewer"],
        )
    ]
    monkeypatch.setattr(
        "teamwork_review_agents.orchestrator.EVENT_RETRY_BACKOFF_SECONDS",
        0.05,
    )
    orchestrator = Orchestrator(config, recover_interrupted=False)

    long_opened = snapshot_factory(number=44)
    long_closed = snapshot_factory(
        number=44,
        state="closed",
        updated_at="2026-08-17T08:05:00Z",
    )
    long_event = detect_events(
        long_opened,
        long_closed,
        batch_id="long-batch",
    )[0]
    retry_opened = snapshot_factory(number=45)
    retry_closed = snapshot_factory(
        number=45,
        state="closed",
        updated_at="2026-08-17T08:06:00Z",
    )
    retry_reopened = snapshot_factory(
        number=45,
        state="opened",
        updated_at="2026-08-17T08:07:00Z",
    )
    failed_event = detect_events(
        retry_opened,
        retry_closed,
        batch_id="retry-close-batch",
    )[0]
    later_event = detect_events(
        retry_closed,
        retry_reopened,
        batch_id="retry-reopen-batch",
    )[0]
    orchestrator.store.save_snapshot_and_events(long_closed, [long_event])
    orchestrator.store.save_snapshot_and_events(retry_closed, [failed_event])
    orchestrator.store.save_snapshot_and_events(retry_reopened, [later_event])

    long_started = asyncio.Event()
    release_long = asyncio.Event()
    later_started = asyncio.Event()
    close_attempts = 0

    async def fake_execute(**kwargs):
        """首次关闭审核失败，重试成功后再执行同 PR 后续批次。"""

        nonlocal close_attempts
        event = kwargs["event"]
        action = kwargs["actions"][0]
        if event.number == 44:
            long_started.set()
            await release_long.wait()
        elif action == "change_request.closed":
            close_attempts += 1
            if close_attempts == 1:
                raise RuntimeError("模拟可重试失败")
        else:
            later_started.set()
        return AgentResult(
            run_id=f"run-{event.number}-{action}-{close_attempts}",
            root_run_id=f"run-{event.number}-{action}-{close_attempts}",
            agent_name=kwargs["agent_name"],
            status="completed",
        )

    monkeypatch.setattr(orchestrator.executor, "execute", fake_execute)
    dispatch = asyncio.create_task(orchestrator.process_events(CycleSummary()))
    await asyncio.wait_for(long_started.wait(), timeout=1)

    for _ in range(50):
        later_record = next(
            item
            for item in orchestrator.store.list_events()
            if item["event_type"] == "change_request.reopened"
        )
        if later_record["queue_reason"] == "event_retry_backoff":
            break
        await asyncio.sleep(0.01)
    else:
        raise AssertionError("后续事件没有显示前序事件重试原因")

    await asyncio.wait_for(later_started.wait(), timeout=1)
    assert not release_long.is_set()
    assert close_attempts == 2

    release_long.set()
    await asyncio.wait_for(dispatch, timeout=1)
    records = {
        (item["number"], item["event_type"]): item
        for item in orchestrator.store.list_events()
    }
    assert records[(45, "change_request.closed")]["status"] == "completed"
    assert records[(45, "change_request.reopened")]["status"] == "completed"


async def test_exhausted_retry_does_not_block_later_batch_for_same_resource(
    monkeypatch,
    configured_app_factory,
    snapshot_factory,
) -> None:
    """前序事件达到重试上限后，同一 PR 的后续批次应立即继续。"""

    config = configured_app_factory()
    config.runtime.event_retry_count = 0
    config.rules = [
        RuleConfig(
            name="state-review",
            events=["change_request.closed", "change_request.reopened"],
            agents=["code-reviewer"],
        )
    ]
    monkeypatch.setattr(
        "teamwork_review_agents.orchestrator.EVENT_RETRY_BACKOFF_SECONDS",
        10.0,
    )
    orchestrator = Orchestrator(config, recover_interrupted=False)
    opened = snapshot_factory(number=46)
    closed = snapshot_factory(
        number=46,
        state="closed",
        updated_at="2026-08-17T08:05:00Z",
    )
    reopened = snapshot_factory(
        number=46,
        state="opened",
        updated_at="2026-08-17T08:06:00Z",
    )
    failed_event = detect_events(opened, closed, batch_id="failed-batch")[0]
    later_event = detect_events(closed, reopened, batch_id="later-batch")[0]
    orchestrator.store.save_snapshot_and_events(closed, [failed_event])
    orchestrator.store.save_snapshot_and_events(reopened, [later_event])

    later_started = asyncio.Event()

    async def fake_execute(**kwargs):
        """让前序关闭事件失败，并观察后续重新打开事件。"""

        action = kwargs["actions"][0]
        if action == "change_request.closed":
            raise RuntimeError("模拟终态失败")
        later_started.set()
        return AgentResult(
            run_id="run-later-batch",
            root_run_id="run-later-batch",
            agent_name=kwargs["agent_name"],
            status="completed",
        )

    monkeypatch.setattr(orchestrator.executor, "execute", fake_execute)
    dispatch = asyncio.create_task(orchestrator.process_events(CycleSummary()))
    await asyncio.wait_for(later_started.wait(), timeout=1)
    await asyncio.wait_for(dispatch, timeout=1)

    records = {
        item["event_type"]: item for item in orchestrator.store.list_events()
    }
    assert records["change_request.closed"]["status"] == "failed"
    assert records["change_request.closed"]["attempts"] == 1
    assert records["change_request.reopened"]["status"] == "completed"
