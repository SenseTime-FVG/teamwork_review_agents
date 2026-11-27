"""仓库保存、轻量摘要和并发状态查询的回归测试。"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient

from teamwork_review_agents.config_manager import ConfigManager
from teamwork_review_agents.events import detect_events
from teamwork_review_agents.repository_initialization import (
    RepositoryInitialization, RepositoryInitializationManager,
)
from teamwork_review_agents.state import StateStore
from teamwork_review_agents.webapp import create_app


def _add_run(store, run_id, *, repository_id=None, event_id=None, resource_key="github:demo:7", started=1, status="completed"):
    """写入可控的运行时间和归属，用于兼容旧记录及活动状态验证。"""

    reservation = store.begin_agent_run(
        proposed_run_id=run_id, root_run_id=None, parent_run_id=None,
        idempotency_key=run_id, event_id=event_id, rule_name="review",
        agent_name="reviewer", resource_key=resource_key, prompt="测试",
        repository_id=repository_id, max_attempts=1,
    )
    assert reservation is not None
    with store.connect() as connection:
        connection.execute(
            "UPDATE agent_runs SET started_at=?,status=?,finished_at=? WHERE run_id=?",
            (started, status, started + 1 if status == "completed" else None, run_id),
        )


@pytest.mark.parametrize("source", ["explicit", "event", "resource"])
def test_light_repository_run_queries_preserve_legacy_ownership(tmp_path, snapshot_factory, source):
    """显式字段、关联事件和旧资源键均能恢复仓库归属，不受其他仓库的较新运行影响。"""

    store = StateStore(tmp_path / "state.db")
    store.initialize()
    snapshot = snapshot_factory(repository_id="demo")
    event = detect_events(None, snapshot, emit_initial=True)[0]
    store.save_snapshot_and_events(snapshot, [event])
    _add_run(
        store, "run-demo", repository_id="demo" if source == "explicit" else None,
        event_id=event.id if source == "event" else None,
        resource_key="github:demo:7" if source == "resource" else "shared:resource",
        started=2, status="preparing",
    )
    _add_run(store, "run-other", repository_id="other", resource_key="github:other:7", started=3)
    assert store.latest_repository_run("demo") == {
        "run_id": "run-demo", "status": "preparing", "started_at": 2, "finished_at": None,
    }
    assert store.has_preparing_repository_run("demo")
    assert not store.has_preparing_repository_run("other")
    assert store.latest_repository_run("missing") is None
    assert not store.has_preparing_repository_run("missing")


def test_repository_summary_uses_covering_order_index_and_preserves_ties(tmp_path, monkeypatch):
    """大审计字段不参与摘要读取，既有运行列表也不再全表临时排序。"""

    store = StateStore(tmp_path / "state.db")
    store.initialize()
    for run_id in ("run-a", "run-z"):
        _add_run(store, run_id, repository_id="demo", started=10)
    with store.connect() as connection:
        connection.execute("UPDATE agent_runs SET prompt=?,events=?", ("x" * 1000000, "x" * 1000000))
    queries = []
    original_connect = store.connect

    @contextmanager
    def traced_connect():
        """记录真实业务 SQL，用执行计划验证访问方式，避免不稳定的秒数断言。"""

        with original_connect() as connection:
            connection.set_trace_callback(queries.append)
            yield connection

    monkeypatch.setattr(store, "connect", traced_connect)
    assert store.latest_repository_run("demo")["run_id"] == "run-z"
    assert [row["run_id"] for row in store.list_runs(2, repository_id="demo")] == ["run-z", "run-a"]
    selects = [query for query in queries if query.lstrip().startswith("SELECT agent_runs.run_id")]
    assert len(selects) == 2
    with original_connect() as connection:
        plans = [[row["detail"] for row in connection.execute("EXPLAIN QUERY PLAN " + query)] for query in selects]
    assert any("COVERING INDEX idx_agent_runs_recent_summary" in step for step in plans[0])
    assert all(not any("TEMP B-TREE" in step for step in plan) for plan in plans)
    assert all(any("idx_agent_runs_recent_summary" in step for step in plan) for plan in plans)


def test_repository_activity_index_migrates_without_changing_old_runs(tmp_path):
    """已有数据库补索引不改历史内容，准备状态检查使用小型活动索引。"""

    store = StateStore(tmp_path / "state.db")
    store.initialize()
    _add_run(store, "old-active", repository_id="demo", status="preparing")
    _add_run(store, "new-completed", repository_id="demo", started=10)
    with store.connect() as connection:
        connection.execute("DROP INDEX idx_agent_runs_recent_summary")
        connection.execute("DROP INDEX idx_agent_runs_preparing_repository")
    store.initialize()
    assert store.has_preparing_repository_run("demo")
    assert store.latest_repository_run("demo")["run_id"] == "new-completed"
    with store.connect() as connection:
        plan = [row["detail"] for row in connection.execute(
            "EXPLAIN QUERY PLAN SELECT 1 FROM agent_runs WHERE status='preparing' AND repository_id='demo' LIMIT 1",
        )]
        assert any("idx_agent_runs_preparing_repository" in step for step in plan)
        assert connection.execute("SELECT status FROM agent_runs WHERE run_id='old-active'").fetchone()[0] == "preparing"


@pytest.mark.parametrize("manual", [False, True])
def test_repository_save_keeps_busy_protection_without_full_snapshot(configured_app_factory, monkeypatch, manual):
    """保存不读取完整状态；手动 Git 和较早的 preparing Agent 仍阻止写入。"""

    config = configured_app_factory()
    app = create_app(config.config_path, start_scheduler=False)
    manager = app.state.repository_initialization_manager
    store = app.state.config_manager.store

    async def forbidden(_repository):
        """完整状态故意不可用，保存必须通过独立活动检查完成。"""

        raise AssertionError("保存不能等待目录检查或历史摘要")

    monkeypatch.setattr(manager, "_snapshot", forbidden)
    if manual:
        manager._operations["demo"] = RepositoryInitialization(
            repository_id="demo", workspace=str(config.repositories[0].workspace), operation="update", status="updating",
        )
    else:
        _add_run(store, "old-preparing", repository_id="demo", status="preparing")
        _add_run(store, "new-completed", repository_id="demo", started=10)
    with TestClient(app) as client:
        current = client.get("/api/config").json()
        repository = {**current["document"]["repositories"][0], "display_name": "已修改的名称"}
        body = {"revision": current["revision"], "repository_id": "demo", "repository": repository}
        response = client.put("/api/config/repositories/demo", json=body)
        assert response.status_code == 409 and "Git 操作" in response.json()["detail"]
        assert app.state.config_manager.config.revision == current["revision"]
        if manual:
            manager._operations["demo"].status = "ready"
        else:
            assert store.mark_agent_run_running("old-preparing")
        saved = client.put("/api/config/repositories/demo", json=body)
        assert saved.status_code == 200
        assert saved.json()["document"]["repositories"][0]["display_name"] == "已修改的名称"


async def test_repository_snapshots_share_inflight_work_and_isolate_reader_cancellation(configured_app_factory, monkeypatch):
    """多个读请求只执行一次巡检，取消一个请求不影响其他请求，完成后读取新状态。"""

    config = configured_app_factory()
    manager = RepositoryInitializationManager(ConfigManager(config.config_path))
    entered, release = asyncio.Event(), asyncio.Event()
    calls = 0

    async def slow_snapshot(_repository):
        """用确定的屏障复现慢查询，避免通过等待秒数制造时序。"""

        nonlocal calls
        calls += 1
        entered.set()
        await release.wait()
        return {"version": calls}

    monkeypatch.setattr(manager, "_build_snapshot", slow_snapshot)
    first = asyncio.create_task(manager.get("demo"))
    await entered.wait()
    second = asyncio.create_task(manager.get("demo"))
    await asyncio.sleep(0)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    release.set()
    assert await second == {"version": 1}
    assert await manager.get("demo") == {"version": 2}


async def test_repository_failed_snapshot_can_be_retried(configured_app_factory, monkeypatch):
    """失败任务不能留在共享字典里，让下一次读取永久复用同一次失败。"""

    config = configured_app_factory()
    manager = RepositoryInitializationManager(ConfigManager(config.config_path))
    calls = 0

    async def snapshot(_repository):
        """首次查询失败，后续查询读取最新结果。"""

        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("临时查询失败")
        return {"ready": True}

    monkeypatch.setattr(manager, "_build_snapshot", snapshot)
    with pytest.raises(RuntimeError, match="临时查询失败"):
        await manager.get("demo")
    assert await manager.get("demo") == {"ready": True}
