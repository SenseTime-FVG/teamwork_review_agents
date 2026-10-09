"""手动批次复用规则去重，但不伪装扫描或关联被替代事件。"""

from datetime import UTC, datetime, timedelta
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from teamwork_review_agents.config import RuleConfig
from teamwork_review_agents.events import create_manual_activity_event, create_manual_replay_event
from teamwork_review_agents.models import AgentResult, ChangeEvent, ChangeRequestActivity, PreflightResult
from teamwork_review_agents.orchestrator import CycleSummary, Orchestrator, plan_rule_invocations
from teamwork_review_agents.state import StateStore
from teamwork_review_agents.webapp import create_app


def source_event(snapshot_factory, index=0, **overrides):
    """构造时间可比较、可调整分支和仓库的原始事件。"""

    snapshot = snapshot_factory(provider="github-main", **overrides)
    return ChangeEvent(
        id=f"source-{index}", type="change_request.commits_changed",
        provider=snapshot.provider, repository_id=snapshot.repository_id,
        number=snapshot.number, old=snapshot, new=snapshot,
        occurred_at=datetime(2026, 10, 1, tzinfo=UTC) + timedelta(minutes=index),
    )


def review_rule(**overrides):
    """提供只使用模拟执行器的审查规则。"""

    return RuleConfig(**{
        "name": "review", "events": ["change_request.commits_changed"],
        "agents": ["code-reviewer"], **overrides,
    })


def record_execution(monkeypatch, orchestrator):
    """替代真实 Agent，只记录规则与事件的执行组合。"""

    calls = []

    async def execute(**kwargs):
        """返回模拟成功结果，不访问模型、Git 或远端平台。"""

        event = kwargs["event"]
        calls.append((kwargs["rule_name"], event.id))
        run_id = f"run-{len(calls)}"
        return AgentResult(
            run_id=run_id, root_run_id=run_id,
            agent_name=kwargs["agent_name"], status="completed",
        )

    monkeypatch.setattr(orchestrator.executor, "execute", execute)
    return calls


def test_replay_keeps_original_order_time_and_immediate_audit_source(snapshot_factory):
    """连续重放不能把原始事件变新，直接来源审计时间保持真实。"""

    source = source_event(snapshot_factory)
    first = create_manual_replay_event(source, batch_id="manual:first")
    second = create_manual_replay_event(first, batch_id="manual:second")
    assert second.deduplication_time == source.occurred_at
    assert second.source_event_id == first.id
    assert second.source_event_occurred_at == first.occurred_at
    assert len({source.id, first.id, second.id}) == 3
    legacy = ChangeEvent.model_validate(first.model_dump(exclude={"deduplication_occurred_at"}))
    assert create_manual_replay_event(legacy).deduplication_time == source.occurred_at
    activity = ChangeRequestActivity(id="commit", type="committed", occurred_at=source.occurred_at)
    manual = create_manual_activity_event(source.new, activity)
    assert create_manual_replay_event(manual).deduplication_time == activity.occurred_at


@pytest.mark.parametrize("switch", [
    "deduplicate_per_scan", "deduplicate_source_branch_per_scan",
    "deduplicate_target_branch_per_scan",
])
def test_manual_rule_winners_use_source_time_not_selection_order(snapshot_factory, switch):
    """反向选择时仍取原始最新事件；不同点击、仓库和关闭开关不合并。"""

    newer = create_manual_replay_event(source_event(snapshot_factory, 2), batch_id="manual:one")
    older = create_manual_replay_event(source_event(snapshot_factory, 1), batch_id="manual:one")
    independent = create_manual_replay_event(source_event(snapshot_factory, 0))
    other_repository = create_manual_replay_event(
        source_event(snapshot_factory, 3, repository_id="other"), batch_id="manual:one",
    )
    events = [newer, older, independent, other_repository]
    plans = plan_rule_invocations([review_rule(**{switch: True})], events)
    assert {plan.events[0].id for plan in plans} == {newer.id, independent.id, other_repository.id}
    assert len(plan_rule_invocations([review_rule()], events)) == 4
    conditional = review_rule(**{switch: True, "conditions": {"title": "仅旧事件匹配"}})
    older.new.title = "仅旧事件匹配"
    assert plan_rule_invocations([conditional], [newer, older])[0].events == (older,)


def test_combined_manual_keys_are_transitive(snapshot_factory):
    """叠加开关复用扫描的连通分组，而不是要求所有字段同时相等。"""

    events = [
        create_manual_replay_event(source_event(snapshot_factory, 0, number=7, source_branch="a"), batch_id="manual:one"),
        create_manual_replay_event(source_event(snapshot_factory, 1, number=7, source_branch="b"), batch_id="manual:one"),
        create_manual_replay_event(source_event(snapshot_factory, 2, number=8, source_branch="b"), batch_id="manual:one"),
    ]
    plans = plan_rule_invocations([
        review_rule(deduplicate_per_scan=True, deduplicate_source_branch_per_scan=True),
    ], events)
    assert [plan.events for plan in plans] == [(events[-1],)]


def test_strict_enqueue_rolls_back_entire_batch(tmp_path, snapshot_factory):
    """冲突时整批回滚，不能部分入队后错误选择去重赢家。"""

    store = StateStore(tmp_path / "state.db")
    store.initialize()
    existing = create_manual_replay_event(source_event(snapshot_factory, 0), batch_id="manual:one")
    fresh = create_manual_replay_event(source_event(snapshot_factory, 1), batch_id="manual:one")
    store.enqueue_events([existing])
    with pytest.raises(ValueError, match="完整写入"):
        store.enqueue_events([fresh, existing], require_all=True)
    assert store.load_event(fresh.id) is None
    assert store.manual_events_for_batch(existing.batch_id) == [store.load_event(existing.id)]


@pytest.mark.parametrize("endpoint", ["replay", "latest"])
def test_bulk_api_atomic_manual_batch_without_scan_side_effects(
    configured_app_factory, snapshot_factory, monkeypatch, endpoint,
):
    """两种入口均整批入队；混合来源、逐项报错、独立点击与扫描审计不变。"""

    config = configured_app_factory()
    app = create_app(config.config_path, start_scheduler=False)
    with TestClient(app) as client:
        store = app.state.config_manager.store
        source = source_event(snapshot_factory, 0, number=7)
        later = source_event(snapshot_factory, 1, number=8)
        for event in [source, later]:
            store.save_snapshot_and_events(event.new, [event])
            store.finish_event(event.id)
        activity = ChangeRequestActivity(id="commit", type="committed", occurred_at=source.occurred_at)
        store.save_activity_cursor(source.provider, source.repository_id, source.number, {
            "latest_activity_checked": True, "latest_activity": activity.model_dump(mode="json"),
        })
        with store.connect() as connection:
            snapshots_before = [tuple(row) for row in connection.execute("SELECT * FROM snapshots ORDER BY snapshot_key")]
            cursors_before = [tuple(row) for row in connection.execute("SELECT * FROM provider_activity_cursors")]
        dispatch = Mock()
        monkeypatch.setattr(app.state.runtime, "dispatch_events_now", dispatch)
        enqueue = Mock(wraps=store.enqueue_events)
        monkeypatch.setattr(store, "enqueue_events", enqueue)
        if endpoint == "replay":
            url = "/api/events/replay"
            request = {"event_ids": [later.id, source.id, later.id, "missing"]}
        else:
            url = "/api/change-requests/trigger-latest-events"
            request = {"targets": [
                {"repository_id": "demo", "number": number} for number in [8, 7, 8, 999]
            ]}
        response = client.post(url, json=request)
        assert response.status_code == 200
        body = response.json()
        assert (body["requested"], body["created"], body["failed"]) == (4, 2, 2)
        assert [result["status_code"] for result in body["results"]] == [200, 200, 409, 404]
        enqueue.assert_called_once()
        assert enqueue.call_args.kwargs == {"require_all": True}
        assert len(enqueue.call_args.args[0]) == 2
        dispatch.assert_called_once()
        events = [store.load_event(result["event_id"]) for result in body["results"] if result["created"]]
        assert len({event.batch_id for event in events}) == 1
        assert all(event.origin == "manual" for event in events)
        assert [event.deduplication_time for event in events] == [later.occurred_at, source.occurred_at]
        if endpoint == "latest":
            assert [result["source"] for result in body["results"] if result["created"]] == ["system", "platform"]
        next_body = client.post(url, json=request).json()
        next_events = [store.load_event(result["event_id"]) for result in next_body["results"] if result["created"]]
        assert events[0].batch_id != next_events[0].batch_id
        assert not {event.id for event in events} & {event.id for event in next_events}
        with store.connect() as connection:
            assert snapshots_before == [tuple(row) for row in connection.execute("SELECT * FROM snapshots ORDER BY snapshot_key")]
            assert cursors_before == [tuple(row) for row in connection.execute("SELECT * FROM provider_activity_cursors")]
        assert store.load_event(source.id) == source
        assert store.get_event_detail(source.id)["status"] == "completed"


@pytest.mark.parametrize("switch,number", [
    ("deduplicate_per_scan", 7), ("deduplicate_source_branch_per_scan", 8),
    ("deduplicate_target_branch_per_scan", 8),
])
async def test_manual_suppression_has_no_run_or_ci_association(
    configured_app_factory, snapshot_factory, monkeypatch, switch, number,
):
    """被去重事件未触发且不关联获胜运行；单条下一批次仍独立执行。"""

    config = configured_app_factory()
    config.rules = [review_rule(**{switch: True})]
    orchestrator = Orchestrator(config, recover_interrupted=False)
    older = create_manual_replay_event(source_event(snapshot_factory, 0), batch_id="manual:one")
    newer = create_manual_replay_event(source_event(snapshot_factory, 1, number=number), batch_id="manual:one")
    separate = create_manual_replay_event(source_event(snapshot_factory, 0))
    orchestrator.store.enqueue_events([newer, older, separate], require_all=True)
    calls = record_execution(monkeypatch, orchestrator)
    summary = CycleSummary()
    await orchestrator.process_events(summary)
    assert set(calls) == {("review", newer.id), ("review", separate.id)}
    assert summary.agent_runs == 2
    detail = orchestrator.store.get_event_detail(older.id)
    assert detail["status"] == "unmatched"
    assert detail["unmatched_reason"] == "manual_batch_deduplicated"
    assert detail["trigger_count"] == 0
    assert not detail["preflights"]
    assert not detail["agent_runs"]
    assert not detail["dispatches"]


async def test_cross_pr_dedup_is_per_rule_even_after_winner_completed(
    configured_app_factory, snapshot_factory, monkeypatch,
):
    """旧事件可触发其他规则，但不能因另一 PR 已完成而再次触发去重规则。"""

    config = configured_app_factory()
    config.rules = [
        review_rule(deduplicate_target_branch_per_scan=True),
        review_rule(name="other-rule", conditions={"number": 7}),
    ]
    orchestrator = Orchestrator(config, recover_interrupted=False)
    older = create_manual_replay_event(source_event(snapshot_factory, 0), batch_id="manual:one")
    newer = create_manual_replay_event(source_event(snapshot_factory, 1, number=8), batch_id="manual:one")
    orchestrator.store.enqueue_events([newer, older], require_all=True)
    calls = record_execution(monkeypatch, orchestrator)
    summary = CycleSummary()
    await orchestrator._process_resource_events(summary, ("demo", 8))
    await orchestrator.process_events(summary)
    assert calls == [("review", newer.id), ("other-rule", older.id)]
    # 模拟旧事件进入重试，已完成赢家仍必须参与选择。
    orchestrator.store.finish_event(older.id, error="模拟可重试错误")
    await orchestrator.process_events(summary)
    assert ("review", older.id) not in calls


async def test_only_manual_winner_enters_preflight(
    configured_app_factory, snapshot_factory, monkeypatch,
):
    """实际写入 CI 关联时只能包含获胜事件，被替代事件不共享 CI。"""

    config = configured_app_factory()
    config.rules = [review_rule(deduplicate_per_scan=True, run_preflight=True)]
    config.repositories[0].preflight.enabled = True
    orchestrator = Orchestrator(config, recover_interrupted=False)
    events = [
        create_manual_replay_event(source_event(snapshot_factory, index), batch_id="manual:ci")
        for index in range(2)
    ]
    orchestrator.store.enqueue_events(events, require_all=True)
    calls = record_execution(monkeypatch, orchestrator)

    async def ensure_passed(event, *, event_ids):
        """只模拟 CI 结果，沿用真实状态存储创建运行与事件关联。"""

        assert event.id == events[-1].id
        assert event_ids == (event.id,)
        orchestrator.store.begin_preflight_run(
            proposed_run_id="ci-run", idempotency_key="ci-key", event_id=event.id,
            event_ids=event_ids, repository_id=event.repository_id, number=event.number,
            head_sha=event.new.head_sha, config_revision="test", max_attempts=1,
        )
        result = PreflightResult(
            run_id="ci-run", repository_id=event.repository_id, number=event.number,
            head_sha=event.new.head_sha, status="success",
        )
        orchestrator.store.finish_preflight_run(result)
        return result

    monkeypatch.setattr(orchestrator.preflight, "ensure_passed", ensure_passed)
    summary = CycleSummary()
    await orchestrator.process_events(summary)
    assert summary.preflight_runs == 1
    assert summary.preflight_errors == 0
    assert calls == [("review", events[-1].id)]
    assert not orchestrator.store.get_event_detail(events[0].id)["preflights"]
    assert len(orchestrator.store.get_event_detail(events[-1].id)["preflights"]) == 1


async def test_manual_batch_exceeds_resource_page_size(
    configured_app_factory, snapshot_factory, monkeypatch,
):
    """最新事件位于资源查询的第二页时，第一页也不能提前触发较旧事件。"""

    config = configured_app_factory()
    config.rules = [review_rule(deduplicate_per_scan=True)]
    orchestrator = Orchestrator(config, recover_interrupted=False)
    events = [
        create_manual_replay_event(source_event(snapshot_factory, index), batch_id="manual:large").model_copy(
            update={"id": f"manual-{index:03d}"},
        )
        for index in range(101)
    ]
    orchestrator.store.enqueue_events(events, require_all=True)
    calls = record_execution(monkeypatch, orchestrator)
    summary = CycleSummary()
    await orchestrator.process_events(summary)
    assert calls == [("review", events[-1].id)]
    assert summary.processed_events == 101
    assert len(orchestrator.store.manual_events_for_batch("manual:large")) == 101


@pytest.mark.parametrize("active_status", ["pending", "processing", "triggered", "failed"])
def test_retention_keeps_manual_dedup_evidence_until_batch_finishes(
    tmp_path, snapshot_factory, active_status,
):
    """历史清理不能先删掉获胜事件，导致仍在排队或重试的旧事件重新触发。"""

    store = StateStore(tmp_path / "state.db")
    store.initialize()
    source = source_event(snapshot_factory).model_copy(update={"type": "change_request.target_commits_changed"})
    winner = create_manual_replay_event(source, batch_id="manual:retention")
    older = create_manual_replay_event(source, batch_id="manual:retention")
    store.enqueue_events([winner, older], require_all=True)
    store.finish_event(winner.id)
    store.finish_event(older.id, status=active_status)
    with store.connect() as connection:
        connection.execute("UPDATE event_inbox SET updated_at = 1")
    assert store.prune_terminal_target_events(2, max_attempts=2) == 0
    store.finish_event(older.id)
    with store.connect() as connection:
        connection.execute("UPDATE event_inbox SET updated_at = 1")
    assert store.prune_terminal_target_events(2, max_attempts=2) == 2


async def test_manual_batch_arrives_during_active_dispatch(
    configured_app_factory, snapshot_factory, monkeypatch,
):
    """调度已在运行时新增批次也执行完整跨 PR 去重，不依赖启动前预结算。"""

    config = configured_app_factory()
    config.rules = [review_rule(deduplicate_target_branch_per_scan=True)]
    orchestrator = Orchestrator(config, recover_interrupted=False)
    initial = create_manual_replay_event(source_event(snapshot_factory, 0, number=9))
    older = create_manual_replay_event(source_event(snapshot_factory, 1), batch_id="manual:later")
    newer = create_manual_replay_event(source_event(snapshot_factory, 2, number=8), batch_id="manual:later")
    orchestrator.store.enqueue_events([initial])
    calls = []

    async def execute(**kwargs):
        """在初始运行内部注入下一批次，模拟 API 并发提交。"""

        event = kwargs["event"]
        calls.append(event.id)
        if event.id == initial.id:
            orchestrator.store.enqueue_events([older, newer], require_all=True)
        return AgentResult(run_id=event.id, root_run_id=event.id, agent_name="code-reviewer", status="completed")

    monkeypatch.setattr(orchestrator.executor, "execute", execute)
    await orchestrator.process_events(CycleSummary())
    assert calls == [initial.id, newer.id]
