"""扫描失败和长扫描期间的增量窗口回归测试。"""

from datetime import UTC, datetime, timedelta

import pytest

from teamwork_review_agents.orchestrator import CycleSummary, Orchestrator
from teamwork_review_agents.providers.base import ProviderError


@pytest.mark.parametrize("failure_stage", ["list", "activity"])
async def test_failed_scan_keeps_window_across_restart(
    monkeypatch, configured_app_factory, snapshot_factory, failure_stage,
):
    """失败保留 t1，恢复时覆盖至 t3，部分入库事件不重复，长扫描也不漏变化。"""

    config = configured_app_factory()
    repository = config.repositories[0]
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    t1 = datetime(2026, 11, 5, 8, tzinfo=UTC)
    t2 = t1 + timedelta(minutes=40)
    t3 = t1 + timedelta(hours=1)
    now = t2

    class Clock(datetime):
        """模拟长扫描和恢复时间，避免依赖实际等待。"""

        @classmethod
        def now(cls, tz=None):
            return now

    monkeypatch.setattr("teamwork_review_agents.orchestrator.datetime", Clock)
    orchestrator = Orchestrator(config, recover_interrupted=False)
    state_key = f"repository_scan:{repository.id}"
    original_state = {
        "started_at": t1.isoformat(),
        "completed_at": (t1 + timedelta(minutes=30)).isoformat(),
    }
    orchestrator.store.set_service_state(state_key, original_state)
    original_state = orchestrator.store.get_service_state(state_key)
    snapshots = []
    for number in (7, 8):
        old = snapshot_factory(
            provider=repository.provider, number=number, updated_at=t1,
        )
        orchestrator.store.save_snapshot_and_events(
            old, [], activity_cursor={"latest_activity_checked": True},
        )
        snapshots.append(old.model_copy(update={
            "head_sha": "b" * 40,
            "updated_at": t1 + timedelta(minutes=number),
        }))
    fail = True
    windows = []

    class Provider:
        """按时间筛选，并可在列表或部分 MR 入库后模拟断流。"""

        name = repository.provider

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return None

        async def list_change_requests(self, _repository, *, updated_since=None):
            nonlocal now
            windows.append(updated_since)
            if fail and failure_stage == "list":
                raise ProviderError("incomplete chunked read")
            if not fail:
                # 本次扫描耗时超过重叠窗口，下一轮仍须覆盖执行期间变化。
                now += timedelta(minutes=30)
            return [item for item in snapshots if item.updated_at >= updated_since]

        async def list_change_request_activities(self, _repository, number, **kwargs):
            if fail and failure_stage == "activity" and number == 8:
                raise ProviderError("incomplete chunked read")
            return None

    monkeypatch.setattr(
        "teamwork_review_agents.orchestrator.create_provider", lambda *args, **kwargs: Provider(),
    )
    for _ in range(2):
        failed_summary = CycleSummary()
        await orchestrator.scan(failed_summary)
        assert failed_summary.errors == ["扫描仓库 demo 失败：incomplete chunked read"]
        assert orchestrator.store.get_service_state(state_key) == original_state
        now += timedelta(minutes=5)

    previous_event_ids = {event.id for event in orchestrator.store.pending_events()}
    assert bool(previous_event_ids) == (failure_stage == "activity")
    # 失败期间新建的 MR 必须在重启后的恢复扫描被发现。
    snapshots.append(snapshot_factory(
        provider=repository.provider, number=9, created_at=t3 - timedelta(minutes=1),
        updated_at=t3 - timedelta(minutes=1),
    ))
    orchestrator = Orchestrator(config, recover_interrupted=False)
    fail = False
    now = t3
    recovered_summary = CycleSummary()
    await orchestrator.scan(recovered_summary)

    assert recovered_summary.errors == []
    overlap = timedelta(seconds=max(60, config.scanner.interval_seconds * 2))
    assert windows == [t1 - overlap] * 3
    events = orchestrator.store.pending_events()
    assert previous_event_ids <= {event.id for event in events}
    assert sorted(event.number for event in events if event.type == "change_request.commits_changed") == [7, 8]
    assert [event.number for event in events if event.type == "change_request.opened"] == [9]
    recovered_state = orchestrator.store.get_service_state(state_key)
    assert recovered_state["started_at"] == t3.isoformat()
    assert recovered_state["completed_at"] == (t3 + timedelta(minutes=30)).isoformat()

    # 该变化发生在恢复扫描期间，若使用完成时间回看便会漏掉。
    snapshots[2] = snapshots[2].model_copy(update={
        "head_sha": "c" * 40, "updated_at": t3 + timedelta(minutes=1),
    })
    followup_summary = CycleSummary()
    await orchestrator.scan(followup_summary)
    assert windows[-1] == t3 - overlap
    assert followup_summary.errors == []
    commits = [event.number for event in orchestrator.store.pending_events() if event.type == "change_request.commits_changed"]
    assert sorted(commits) == [7, 8, 9]


def test_legacy_watermark_and_missing_state(configured_app_factory):
    """旧数据只有完成时间时仍可增量扫描，没有水位时保持初次扫描。"""

    orchestrator = Orchestrator(configured_app_factory(), recover_interrupted=False)
    assert orchestrator._updated_since("demo") is None
    completed = datetime(2026, 11, 5, 8, tzinfo=UTC)
    orchestrator.store.set_service_state("repository_scan:demo", {"completed_at": completed.isoformat()})
    overlap = timedelta(seconds=max(60, orchestrator.config.scanner.interval_seconds * 2))
    assert orchestrator._updated_since("demo") == completed - overlap
