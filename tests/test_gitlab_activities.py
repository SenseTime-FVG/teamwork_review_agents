"""GitLab 结构化活动、降级和手动重放回归。"""

from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from teamwork_review_agents.config import ProviderConfig, RepositoryConfig, ScannerConfig
from teamwork_review_agents.events import create_manual_activity_event, detect_activity_events, detect_events
from teamwork_review_agents.models import ChangeRequestActivity
from teamwork_review_agents.orchestrator import CycleSummary, Orchestrator
from teamwork_review_agents.providers.gitlab import GitLabProvider
from teamwork_review_agents.providers import ProviderError
from teamwork_review_agents.webapp import create_app


@pytest.fixture
async def activity_server():
    """模拟仅提供结构化端点的 GitLab，任何评论读取都会立即失败。"""

    data = {
        "detail": {"created_at": "2026-09-01T00:00:00Z", "state": "opened", "sha": "a"},
        "resource_state_events": [],
        "resource_label_events": [],
        "versions": [{"id": 1, "created_at": "2026-09-01T00:00:00Z", "head_commit_sha": "a"}],
        "failure": None,
    }
    requests = []

    def respond(request):
        """支持分页、权限错误和故障恢复，不返回任何真实数据。"""

        path = request.url.path
        requests.append(path)
        assert "/notes" not in path and "/discussions" not in path
        suffix = path.rsplit("/", 1)[-1]
        if suffix == data["failure"]:
            return httpx.Response(403, json={"message": "secret-must-not-leak"})
        if suffix == "7":
            return httpx.Response(200, json=data["detail"])
        assert suffix in data
        items = data[suffix]
        page = int(request.url.params.get("page", "1"))
        size = int(request.url.params.get("per_page", "100"))
        start = (page - 1) * size
        next_page = str(page + 1) if start + size < len(items) else ""
        return httpx.Response(200, json=items[start:start + size], headers={"x-next-page": next_page})

    provider = GitLabProvider(
        "gitlab-main", ProviderConfig(kind="gitlab", base_url="https://gitlab.test/api/v4", token_env="TEST_TOKEN"),
        ScannerConfig(), token="test-token",
    )
    # 禁用环境代理，保证 CI 无论宿主配置如何都不会请求真实平台。
    await provider.client.aclose()
    provider.client = httpx.AsyncClient(
        base_url="https://gitlab.test/api/v4/", transport=httpx.MockTransport(respond), trust_env=False,
    )
    repository = RepositoryConfig(id="demo", provider="gitlab-main", project="group/sub/repo", workspace=Path("/tmp/demo"))
    yield provider, repository, data, requests
    await provider.close()


async def test_gitlab_first_baseline_and_incremental_dedup(activity_server, snapshot_factory):
    """升级首次建基线不回放历史，后续标签、提交和流水线各检测一次。"""

    provider, repo, data, requests = activity_server
    try:
        first = await provider.list_change_request_activities(repo, 7)
        assert first.baseline and not first.activities
        assert first.latest_activity.type == "opened"
        manual = create_manual_activity_event(snapshot_factory(), first.latest_activity)
        assert manual.type == "change_request.opened" and manual.origin == "manual"
        cursor = Orchestrator._activity_cursor(first)
        data["versions"].extend([
            {"id": 2, "created_at": "2026-09-02T00:00:00Z", "head_commit_sha": "a"},
            {"id": 3, "created_at": "2026-09-03T00:00:00Z", "head_commit_sha": "b"},
        ])
        data["detail"]["sha"] = "b"
        data["resource_label_events"] = [{"id": 3, "created_at": "2026-09-03T00:00:00Z", "action": "add", "label": {"name": "review"}}]
        provider.ACTIVITY_PAGE_SIZE = 2
        batch = await provider.list_change_request_activities(repo, 7, cursor=cursor)
        assert not batch.baseline
        assert [item.type for item in batch.activities] == ["committed", "labeled"]
        assert len({item.id for item in batch.activities}) == 2
        old = snapshot_factory(head_sha="a", labels=(), pipeline_status="pending")
        current = snapshot_factory(head_sha="b", labels=("review",), pipeline_status="success")
        events = detect_activity_events(old, current, batch.activities)
        assert [event.type for event in events].count("change_request.commits_changed") == 1
        assert [event.type for event in events].count("change_request.labels_changed") == 1
        assert [event.type for event in events].count("change_request.pipeline_changed") == 1
        repeated = await provider.list_change_request_activities(repo, 7, cursor=Orchestrator._activity_cursor(batch))
        assert not repeated.activities
        assert not detect_activity_events(current, current, batch.activities)
        assert not any("notes" in path for path in requests)
    finally:
        await provider.close()


async def test_gitlab_first_window_and_state_round_trip(activity_server, snapshot_factory):
    """首次只回看窗口内活动；同一时间先关闭再打开仍是两条真实动作。"""

    provider, repo, data, _ = activity_server
    data["resource_state_events"] = [
        {"id": 9, "created_at": "2026-09-01T01:00:00Z", "state": "closed"},
        {"id": 10, "created_at": "2026-09-03T01:00:00Z", "state": "closed"},
        {"id": 11, "created_at": "2026-09-03T01:00:00Z", "state": "opened"},
    ]
    try:
        batch = await provider.list_change_request_activities(repo, 7, since=datetime(2026, 9, 2, tzinfo=UTC))
        assert not batch.baseline
        assert [item.type for item in batch.activities] == ["closed", "reopened"]
        snapshot = snapshot_factory(state="opened")
        events = detect_activity_events(snapshot, snapshot, batch.activities)
        assert [event.type for event in events if event.type != "change_request.updated"] == ["change_request.closed", "change_request.reopened"]
    finally:
        await provider.close()


async def test_gitlab_merged_detail_and_resource_event_are_not_duplicated(activity_server):
    """同一合并由详情和状态流共同提供时只保留一个稳定身份。"""

    provider, repo, data, _ = activity_server
    data["detail"]["merged_at"] = "2026-09-03T01:00:00Z"
    data["resource_state_events"] = [{"id": 10, "created_at": "2026-09-03T01:00:00Z", "state": "merged"}]
    try:
        batch = await provider.list_change_request_activities(repo, 7, since=datetime(2026, 9, 2, tzinfo=UTC))
        assert [item.type for item in batch.activities] == ["merged"]
        assert batch.latest_activity.id == "gitlab:7:state:10"
    finally:
        await provider.close()


async def test_gitlab_failure_preserves_cursor_and_recovery_rebaselines(activity_server):
    """结构化源失败保留原水位并展示错误，恢复时不回放快照已处理的变化。"""

    provider, repo, data, _ = activity_server
    try:
        first = await provider.list_change_request_activities(repo, 7)
        cursor = Orchestrator._activity_cursor(first)
        data["failure"] = "resource_label_events"
        data["versions"].append({"id": 2, "created_at": "2026-09-03T00:00:00Z", "head_commit_sha": "b"})
        data["detail"]["sha"] = "b"
        failed = await provider.list_change_request_activities(repo, 7, cursor=cursor)
        assert failed.baseline and not failed.activities
        assert failed.cursor["gitlab_watermarks"] == cursor["gitlab_watermarks"]
        assert "读取失败" in failed.cursor["activity_error"]
        assert "secret" not in str(failed.cursor)
        data["failure"] = None
        recovered = await provider.list_change_request_activities(repo, 7, cursor=Orchestrator._activity_cursor(failed))
        assert recovered.baseline and not recovered.activities
        assert "activity_error" not in recovered.cursor
        assert recovered.cursor["gitlab_watermarks"]["version"] == 2
    finally:
        await provider.close()


async def test_gitlab_incomplete_pagination_and_bad_timestamp_fail_closed(activity_server):
    """分页被截断或时间缺失时不能悄悄产生不完整增量。"""

    provider, repo, data, _ = activity_server
    data["versions"].append({"id": 2, "created_at": None, "head_commit_sha": "b"})
    try:
        provider.ACTIVITY_PAGE_SIZE = 1
        provider.MAX_ACTIVITY_PAGES = 1
        batch = await provider.list_change_request_activities(repo, 7)
        assert batch.cursor["activity_error"] and not batch.activities
        provider.MAX_ACTIVITY_PAGES = 3
        batch = await provider.list_change_request_activities(repo, 7)
        assert batch.cursor["activity_error"] and not batch.activities
    finally:
        await provider.close()


def test_manual_fallback_preserves_real_event_and_excludes_old_generations(configured_app_factory, snapshot_factory):
    """没有平台活动仍可重放当前版本系统事件，不能捏造事件或选择旧源版本。"""

    config = configured_app_factory()
    app = create_app(config.config_path, start_scheduler=False)
    with TestClient(app) as client:
        manager = app.state.config_manager
        manager.config.providers["github-main"].kind = "gitlab"
        app.state.runtime.dispatch_events_now = lambda: None
        store = manager.store
        old = snapshot_factory(provider="github-main", repository_id="demo", number=7, head_sha="a")
        new = old.model_copy(update={"head_sha": "b"})
        store.save_snapshot_and_events(new, detect_events(old, new))
        record = client.get("/api/change-requests").json()[0]
        assert record["latest_event_supported"] is True
        assert record["latest_event"] is None
        assert record["manual_event"]["source"] == "system"
        assert record["manual_event"]["event_type"] == "change_request.commits_changed"
        response = client.post("/api/change-requests/demo/7/trigger-latest-event")
        assert response.status_code == 200
        assert response.json()["source"] == "system"
        replay = store.load_event(response.json()["event_id"])
        assert replay.old.head_sha == "a" and replay.new.head_sha == "b"
        assert replay.source_event_id == record["manual_event"]["source_event_id"]
        assert client.get("/api/change-requests").json()[0]["manual_event"] == record["manual_event"]
        # 即使源 SHA 后来回到 b，也不能重放上一次同 SHA 的历史世代。
        store.save_snapshot_and_events(new.model_copy(update={"head_sha": "c"}), [])
        store.save_snapshot_and_events(new, [])
        assert client.get("/api/change-requests").json()[0]["manual_event"] is None
        assert client.post("/api/change-requests/demo/7/trigger-latest-event").status_code == 409


def test_activity_error_uses_system_fallback_and_batch_reports_sources(configured_app_factory, snapshot_factory):
    """失败的活动缓存不可伪装为最新结果，单条和批量接口都返回来源。"""

    config = configured_app_factory()
    app = create_app(config.config_path, start_scheduler=False)
    with TestClient(app) as client:
        app.state.runtime.dispatch_events_now = lambda: None
        store = app.state.config_manager.store
        snapshot = snapshot_factory(provider="github-main", repository_id="demo", number=7)
        store.save_snapshot_and_events(snapshot, detect_events(None, snapshot, emit_initial=True))
        activity = ChangeRequestActivity(id="old-merged", type="merged", occurred_at=snapshot.updated_at)
        store.save_activity_cursor(snapshot.provider, "demo", 7, {
            "latest_activity": activity.model_dump(mode="json"), "latest_activity_checked": True,
            "activity_error": "GitLab 结构化活动读取失败",
        })
        record = client.get("/api/change-requests").json()[0]
        assert record["latest_event_error"]
        assert record["manual_event"]["source"] == "system"
        result = client.post("/api/change-requests/trigger-latest-events", json={"targets": [{"repository_id": "demo", "number": 7}]}).json()
        assert result["created"] == 1
        assert result["results"][0]["source"] == "system"
        assert result["results"][0]["event_type"] == "change_request.discovered"


async def test_gitlab_scan_upgrade_restart_and_recovery(
    activity_server, configured_app_factory, snapshot_factory, monkeypatch,
):
    """真实适配器与 SQLite 联合验证升级、重启、降级及恢复不会重复入队。"""

    provider, repo, data, _ = activity_server
    config = configured_app_factory()
    config.repositories = [repo]
    orchestrator = Orchestrator(config, recover_interrupted=False)
    current = snapshot_factory(provider=provider.name, repository_id=repo.id, number=7, head_sha="a", pipeline_status="pending")
    # 已有旧快照而没有活动游标，升级时不能重放关闭再打开等历史。
    orchestrator.store.save_snapshot_and_events(current, [])
    data["resource_state_events"] = [
        {"id": 9, "created_at": "2026-09-01T01:00:00Z", "state": "closed"},
        {"id": 10, "created_at": "2026-09-01T02:00:00Z", "state": "opened"},
    ]

    async def snapshots(*args, **kwargs):
        """仅模拟 MR 列表，活动读取和持久化使用真实实现。"""
        return [current]

    async def target_heads(*args, **kwargs):
        """本用例不涉及目标分支检测。"""
        return {}

    monkeypatch.setattr(provider, "list_change_requests", snapshots)
    monkeypatch.setattr(Orchestrator, "_target_branch_heads", target_heads)
    try:
        await orchestrator._scan_repository(provider, repo, "upgrade", CycleSummary())
        assert orchestrator.store.pending_events() == []
        current = current.model_copy(update={"head_sha": "b", "pipeline_status": "success"})
        data["detail"]["sha"] = "b"
        data["versions"].append({"id": 2, "created_at": "2026-09-03T00:00:00Z", "head_commit_sha": "b"})
        await orchestrator._scan_repository(provider, repo, "new-change", CycleSummary())
        events = orchestrator.store.pending_events()
        assert [event.type for event in events].count("change_request.commits_changed") == 1
        assert [event.type for event in events].count("change_request.pipeline_changed") == 1
        restarted = Orchestrator(config, recover_interrupted=False)
        await restarted._scan_repository(provider, repo, "restart", CycleSummary())
        assert len(restarted.store.pending_events()) == len(events)
        data["failure"] = "resource_label_events"
        current = current.model_copy(update={"head_sha": "c"})
        data["detail"]["sha"] = "c"
        data["versions"].append({"id": 3, "created_at": "2026-09-04T00:00:00Z", "head_commit_sha": "c"})
        await restarted._scan_repository(provider, repo, "degraded", CycleSummary())
        degraded = restarted.store.pending_events()
        assert [event.type for event in degraded].count("change_request.commits_changed") == 2
        assert restarted.store.list_snapshots()[0]["latest_event_error"]
        # 失败源在下一轮仍失败时必须能返回，不能困在补建游标循环。
        await restarted._scan_repository(provider, repo, "still-failed", CycleSummary())
        data["failure"] = None
        await restarted._scan_repository(provider, repo, "recovered", CycleSummary())
        assert len(restarted.store.pending_events()) == len(degraded)
        assert not restarted.store.list_snapshots()[0]["latest_event_error"]
    finally:
        await provider.close()


async def test_gitlab_version_lag_does_not_advance_cursor(activity_server):
    """Head 已变化但 diff version 尚未生成时保留游标，用快照检测。"""

    provider, repo, data, _ = activity_server
    try:
        baseline = await provider.list_change_request_activities(repo, 7)
        data["detail"]["sha"] = "new-head"
        lagged = await provider.list_change_request_activities(repo, 7, cursor=Orchestrator._activity_cursor(baseline))
        assert lagged.baseline and lagged.cursor["activity_error"]
        assert lagged.cursor["gitlab_watermarks"] == baseline.cursor["gitlab_watermarks"]
    finally:
        await provider.close()


async def test_gitlab_snapshot_race_preserves_transaction(
    activity_server, configured_app_factory, snapshot_factory, monkeypatch,
):
    """读取过程中出现新提交时，不能将新活动应用到旧快照并产生反向变化。"""

    provider, repo, data, _ = activity_server
    config = configured_app_factory()
    config.repositories = [repo]
    orchestrator = Orchestrator(config, recover_interrupted=False)
    old = snapshot_factory(provider=provider.name, repository_id=repo.id, number=7, head_sha="a")
    first = await provider.list_change_request_activities(repo, 7)
    original_cursor = Orchestrator._activity_cursor(first)
    orchestrator.store.save_snapshot_and_events(old, [], activity_cursor=original_cursor)
    data["detail"]["sha"] = "c"
    data["versions"].append({"id": 2, "created_at": "2026-09-03T00:00:00Z", "head_commit_sha": "c"})

    async def snapshots(*args, **kwargs):
        """列表先看见 b，随后活动接口已经看见 c。"""
        return [old.model_copy(update={"head_sha": "b"})]

    async def target_heads(*args, **kwargs):
        """固定无目标分支变化。"""
        return {}

    monkeypatch.setattr(provider, "list_change_requests", snapshots)
    monkeypatch.setattr(Orchestrator, "_target_branch_heads", target_heads)
    with pytest.raises(ProviderError, match="读取活动期间发生变化"):
        await orchestrator._scan_repository(provider, repo, "racing", CycleSummary())
    assert orchestrator.store.load_snapshot(old.key).head_sha == "a"
    assert orchestrator.store.pending_events() == []
    assert orchestrator.store.load_activity_cursor(provider.name, repo.id, 7) == original_cursor


def test_manual_fallback_does_not_cross_provider_or_target_head(configured_app_factory, snapshot_factory):
    """候选必须与当前平台和目标分支基线一致，不能复用另一仓库连接的历史。"""

    app = create_app(configured_app_factory().config_path, start_scheduler=False)
    with TestClient(app):
        store = app.state.config_manager.store
        original = snapshot_factory(provider="github-main", repository_id="demo", number=7, head_sha="a", target_head_sha="target-a")
        store.save_snapshot_and_events(original, detect_events(None, original, emit_initial=True))
        assert store.load_latest_detected_event(original) is not None
        assert store.load_latest_detected_event(original.model_copy(update={"provider": "gitlab-main"})) is None
        assert store.load_latest_detected_event(original.model_copy(update={"target_head_sha": "target-b"})) is None
