"""定时清理只操作测试临时目录，覆盖调度、归属、旧格式与活动任务保护。"""

import asyncio
from datetime import datetime
import json
import os
from pathlib import Path
import subprocess
import threading
import time
import uuid

from fastapi.testclient import TestClient
import pytest
from pydantic import ValidationError

from teamwork_review_agents.config import RuntimeConfig, WorkspaceCleanupConfig
from teamwork_review_agents.codex_executable import CodexRuntimeError
from teamwork_review_agents.events import detect_events
from teamwork_review_agents.executor import AgentExecutionError, AgentExecutor
from teamwork_review_agents.locks import ResourceLease
from teamwork_review_agents.models import stable_hash
from teamwork_review_agents.scheduler import next_workspace_cleanup_at
from teamwork_review_agents.state import StateStore
from teamwork_review_agents.webapp import create_app
from teamwork_review_agents.workspace import retained_marker_path, workspace_usage_lock_key
from teamwork_review_agents import workspace_cleanup as cleanup


def git(cwd: Path, *args: str) -> str:
    """临时仓库中的本地 Git 操作，不访问任何远端服务。"""

    return subprocess.run(
        ["git", "-c", "user.name=Cleanup Test", "-c", "user.email=cleanup@example.test",
         "-c", "commit.gpgsign=false", *args], cwd=cwd, check=True,
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    ).stdout.strip()


@pytest.fixture
def cleanup_world(configured_app_factory):
    """构造真实临时 Git 仓库、SQLite 运行记录与外部保留标记。"""

    config = configured_app_factory()
    store = StateStore(config.database.path)
    store.initialize()
    base = config.repositories[0].workspace
    git(base, "init", "-q")
    git(base, "commit", "--allow-empty", "-m", "base")
    bucket = config.database.path.parent / "worktrees" / stable_hash("demo")[:16]
    bucket.mkdir(parents=True)
    manager = cleanup.WorkspaceCleanupManager(lambda: config, store)

    def create(*, days=10, status="failed", shared=False, linked=False, root_run_id=None):
        """默认创建已过期 clone，可按用例模拟旧借用对象库和 linked worktree。"""

        run_id = str(uuid.uuid4())
        target = bucket / run_id
        if linked:
            git(base, "worktree", "add", "--detach", str(target), "HEAD")
        else:
            git(base, "clone", "--shared" if shared else "--no-hardlinks", str(base), str(target))
        (target / "unsaved.txt").write_text("过期但未提交的测试文件", encoding="utf-8")
        ended = time.time() - days * 86400
        retained_marker_path(target).write_text(json.dumps({
            "workspace": str(target), "workspace_kind": "worktree" if linked else "clone",
            "retained_at": ended, "cleanup_at": ended + 7 * 86400,
        }), encoding="utf-8")
        with store.connect() as db:
            db.execute(
                "INSERT INTO agent_runs (run_id, root_run_id, idempotency_key, agent_name, resource_key, "
                "repository_id, status, prompt, started_at, finished_at, workspace_path, workspace_status) "
                "VALUES (?, ?, ?, 'test', 'test', 'demo', ?, '', ?, ?, ?, 'retained')",
                (run_id, root_run_id or run_id, run_id, status, ended - 30, ended, str(target)),
            )
        return target

    return config, store, manager, create


def test_default_and_schedule_validation():
    """默认每天六点，没有用户时区配置；非法时间和非正保留期由配置拒绝。"""

    default = WorkspaceCleanupConfig()
    assert default.enabled and default.kind == "daily"
    assert (default.hour, default.minute) == (6, 0)
    assert "timezone" not in default.model_dump()
    assert RuntimeConfig().worktree_retention_days == 7
    with pytest.raises(ValidationError):
        RuntimeConfig(worktree_retention_days=0)
    for values in ({"minute": 60}, {"hour": 24}, {"weekday": 7}, {"interval_value": 0}, {"interval_unit": "minutes"}):
        with pytest.raises(ValidationError):
            WorkspaceCleanupConfig(**values)


@pytest.mark.parametrize(("values", "after", "expected"), [
    ({}, "2026-09-16 05:00", "2026-09-16 06:00"),
    ({}, "2026-09-16 06:00", "2026-09-17 06:00"),
    ({"kind": "hourly", "minute": 15}, "2026-09-16 06:16", "2026-09-16 07:15"),
    ({"kind": "weekly", "weekday": 0, "hour": 8, "minute": 30}, "2026-09-16 09:00", "2026-09-21 08:30"),
    ({"kind": "interval", "interval_value": 2, "interval_unit": "hours"}, "2026-09-16 23:00", "2026-09-17 01:00"),
    ({"kind": "interval", "interval_value": 2, "interval_unit": "days"}, "2026-09-16 06:00", "2026-09-18 06:00"),
])
def test_local_schedules(values, after, expected):
    """四种模式均使用主机日历，定点触发严格晚于当前时刻。"""

    start = datetime.strptime(after, "%Y-%m-%d %H:%M").timestamp()
    actual = next_workspace_cleanup_at(WorkspaceCleanupConfig(**values), start)
    assert datetime.fromtimestamp(actual).strftime("%Y-%m-%d %H:%M") == expected


@pytest.mark.skipif(not hasattr(time, "tzset"), reason="此用例需 POSIX 动态切换本机时区")
def test_local_calendar_respects_dst(monkeypatch):
    """下一天六点使用下一天的本地偏移，不把固定偏移错用到夏令时。"""

    original = os.environ.get("TZ")
    try:
        monkeypatch.setenv("TZ", "America/New_York")
        time.tzset()
        before = datetime(2026, 3, 7, 6).timestamp()
        due = next_workspace_cleanup_at(WorkspaceCleanupConfig(), before)
        assert datetime.fromtimestamp(due) == datetime(2026, 3, 8, 6)
        assert due - before == 23 * 3600
        gap = next_workspace_cleanup_at(WorkspaceCleanupConfig(hour=2, minute=30), datetime(2026, 3, 8, 0).timestamp())
        assert datetime.fromtimestamp(gap) == datetime(2026, 3, 9, 2, 30)
        repeated = next_workspace_cleanup_at(WorkspaceCleanupConfig(hour=1, minute=30), datetime(2026, 11, 1, 1, 45, fold=0).timestamp())
        assert datetime.fromtimestamp(repeated) == datetime(2026, 11, 2, 1, 30)
        weekly_gap = next_workspace_cleanup_at(WorkspaceCleanupConfig(kind="weekly", weekday=6, hour=2, minute=30), datetime(2026, 3, 4).timestamp())
        assert datetime.fromtimestamp(weekly_gap) == datetime(2026, 3, 15, 2, 30)
    finally:
        if original is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = original
        time.tzset()


def test_plan_persists_future_deadline_and_skips_missed_periods(cleanup_world):
    """重启保留未来计划，过期计划只重排；调整保留期不推迟间隔计划。"""

    config, store, manager, _ = cleanup_world
    config.runtime.workspace_cleanup = WorkspaceCleanupConfig(kind="interval", interval_value=2, interval_unit="hours")
    now = time.time()
    manager.sync_plan(now, startup=True)
    due = manager.state["next_run_at"]
    restored = cleanup.WorkspaceCleanupManager(lambda: config, store)
    restored.sync_plan(now + 10, startup=True)
    assert restored.state["next_run_at"] == due
    config.runtime.worktree_retention_days = 1
    restored.sync_plan(now + 20)
    assert restored.state["next_run_at"] == due
    restored.sync_plan(due + 30, startup=True)
    assert restored.state["next_run_at"] == due + 30 + 7200
    config.runtime.workspace_cleanup.enabled = False
    restored.sync_plan(due + 40)
    assert restored.state["next_run_at"] is None


@pytest.mark.parametrize("repository_state", ["enabled", "disabled", "removed"])
async def test_expired_clone_is_removed_and_recorded(cleanup_world, repository_state):
    """停用或移除仓库也能回收可信 clone，过期未提交测试文件按策略删除。"""

    config, store, manager, create = cleanup_world
    target = create()
    if repository_state == "disabled":
        config.repositories[0].enabled = False
    elif repository_state == "removed":
        config.repositories = []
    await manager.run_once(time.time())
    assert not target.exists()
    assert not retained_marker_path(target).exists()
    assert store.workspace_cleanup_record(target.name)["workspace_status"] == "removed"
    report = manager.snapshot()["last_run"]
    assert report["removed"] == 1 and report["reclaimed_bytes"] > 0
    assert report["started_text"] and report["finished_text"]
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM run_logs WHERE run_id = ? AND event_type = 'workspace.cleanup.removed'", (target.name,)).fetchone()[0] == 1


async def test_legacy_clone_keeps_borrowed_objects_and_base_intact(cleanup_world):
    """借用对象库的旧 clone 可以删除自身，但基础仓库内容和对象保持不变。"""

    config, _, manager, create = cleanup_world
    target = create(shared=True)
    assert (target / ".git/objects/info/alternates").is_file()
    base = config.repositories[0].workspace
    head = git(base, "rev-parse", "HEAD")
    await manager.run_once(time.time())
    assert not target.exists()
    assert git(base, "rev-parse", "HEAD") == head
    git(base, "fsck", "--no-reflogs")


async def test_linked_worktree_cleanup_uses_verified_base(cleanup_world):
    """可信 linked worktree 同步移除 Git 管理记录，不删除基础仓库。"""

    config, _, manager, create = cleanup_world
    target = create(linked=True)
    base = config.repositories[0].workspace
    await manager.run_once(time.time())
    assert not target.exists()
    assert str(target) not in git(base, "worktree", "list", "--porcelain")
    assert base.exists()


@pytest.mark.parametrize("status", ["queued", "preparing", "running"])
async def test_active_runs_are_never_deleted(cleanup_world, status):
    """即便标记已经过期，所有活动状态都必须跳过。"""

    _, _, manager, create = cleanup_world
    target = create(status=status)
    await manager.run_once(time.time())
    assert target.exists()
    assert manager.state["last_run"]["skipped"] == 1


async def test_parent_child_and_shared_directory_are_protected(cleanup_world):
    """活动子任务或另一任务引用目录时，终态父运行也不能被回收。"""

    _, store, manager, create = cleanup_world
    parent = create()
    child = create(status="running", root_run_id=parent.name)
    other = create()
    with store.connect() as db:
        db.execute("UPDATE agent_runs SET workspace_path = ? WHERE run_id = ?", (str(other), child.name))
    await manager.run_once(time.time())
    assert parent.exists() and other.exists()


async def test_latest_retention_applies_to_existing_marker(cleanup_world):
    """不使用标记中旧的到期时间；新保留天数既可延长也可缩短恢复窗口。"""

    config, _, manager, create = cleanup_world
    target = create(days=4)
    await manager.run_once(time.time())
    assert target.exists()
    config.runtime.worktree_retention_days = 3
    await manager.run_once(time.time())
    assert not target.exists()


async def test_busy_workspace_lock_skips_then_can_retry(cleanup_world):
    """工作区使用锁由运行持有时跳过，释放之后才允许清理。"""

    _, store, manager, create = cleanup_world
    target = create()
    async with ResourceLease(store, [workspace_usage_lock_key(target)], "root", ttl_seconds=120, timeout_seconds=0):
        await manager.run_once(time.time())
        assert target.exists()
    await manager.run_once(time.time())
    assert not target.exists()


async def test_active_state_is_rechecked_after_directory_scan(cleanup_world, monkeypatch):
    """目录大小统计后再次检查，状态变化不能落入检查后删除的竞态。"""

    _, store, manager, create = cleanup_world
    target = create()
    def change_status(_target):
        """模拟活动状态在耗时扫描期间改变。"""
        with store.connect() as db:
            db.execute("UPDATE agent_runs SET status='running' WHERE run_id=?", (target.name,))
        return 1
    monkeypatch.setattr(cleanup, "_directory_bytes", change_status)
    await manager.run_once(time.time())
    assert target.exists()


@pytest.mark.parametrize("corruption", ["missing_record", "wrong_path", "wrong_repository", "outside_marker", "bad_marker", "reappeared"])
async def test_untrusted_ownership_never_deletes(cleanup_world, corruption):
    """缺失记录、路径或仓库身份不符、越界标记和损坏元数据均拒绝删除。"""

    config, store, manager, create = cleanup_world
    target = create()
    marker = retained_marker_path(target)
    if corruption == "missing_record":
        with store.connect() as db:
            db.execute("DELETE FROM agent_runs WHERE run_id=?", (target.name,))
    elif corruption in {"wrong_path", "wrong_repository"}:
        with store.connect() as db:
            field = "workspace_path" if corruption == "wrong_path" else "repository_id"
            db.execute(f"UPDATE agent_runs SET {field}='unrelated' WHERE run_id=?", (target.name,))
    elif corruption == "outside_marker":
        data = json.loads(marker.read_text())
        data["workspace"] = str(config.repositories[0].workspace)
        marker.write_text(json.dumps(data))
    elif corruption == "reappeared":
        with store.connect() as db:
            db.execute("UPDATE agent_runs SET workspace_status='removed' WHERE run_id=?", (target.name,))
    else:
        marker.write_text("invalid")
    await manager.run_once(time.time())
    assert target.exists()
    assert manager.state["last_run"]["removed"] == 0


async def test_directory_link_never_deletes_outside(cleanup_world):
    """POSIX 链接或 Windows 联接不能让清理逃逸到外部目录。"""

    config, _, manager, create = cleanup_world
    target = create()
    outside = target.with_name(target.name + "-outside")
    target.rename(outside)
    if os.name == "nt":
        subprocess.run(["cmd", "/c", "mklink", "/J", str(target), str(outside)], check=True, capture_output=True)
    else:
        target.symlink_to(outside, target_is_directory=True)
    await manager.run_once(time.time())
    assert (outside / "unsaved.txt").is_file()
    assert config.repositories[0].workspace.exists()


async def test_deletion_failure_is_visible_and_other_directories_continue(cleanup_world, monkeypatch):
    """单目录权限失败保留现场与原因，不能影响其他工作区回收。"""

    _, _, manager, create = cleanup_world
    denied = create()
    allowed = create()
    original = cleanup.remove_expired_run_workspace
    def remove(source, target):
        """只给一个临时目录注入权限故障。"""
        if target == denied:
            raise PermissionError("模拟权限不足")
        original(source, target)
    monkeypatch.setattr(cleanup, "remove_expired_run_workspace", remove)
    await manager.run_once(time.time())
    report = manager.state["last_run"]
    assert report["status"] == "partial" and report["failed"] == 1 and report["removed"] == 1
    assert denied.exists() and not allowed.exists()


async def test_disabled_and_migration_skip_batch(cleanup_world):
    """停用计划或仓库迁移期间不删除。"""

    config, _, manager, create = cleanup_world
    target = create()
    config.runtime.workspace_cleanup.enabled = False
    await manager.run_once(time.time())
    assert target.exists() and manager.state["last_run"]["status"] == "skipped"
    config.runtime.workspace_cleanup.enabled = True
    manager.maintenance_check = lambda: True
    await manager.run_once(time.time())
    assert target.exists() and manager.state["last_run"]["status"] == "skipped"


async def test_start_does_not_cleanup_and_stop_is_prompt(cleanup_world, monkeypatch):
    """启动只能计算未来计划，停止无需等待明天六点。"""

    _, _, manager, create = cleanup_world
    target = create()
    async def forbidden(_scheduled_at):
        pytest.fail("启动不应立即清理")
    monkeypatch.setattr(manager, "run_once", forbidden)
    manager.start()
    await asyncio.sleep(0.02)
    assert manager.snapshot()["next_run_at"] > time.time()
    await asyncio.wait_for(manager.close(), 1)
    assert target.exists()


def test_status_api_is_read_only_and_configuration_roundtrips(cleanup_world):
    """旧配置省略字段仍采用默认值；读取与保存计划均不能立即触发清理。"""

    config, _, _, create = cleanup_world
    target = create()
    with TestClient(create_app(config.config_path, start_scheduler=False)) as client:
        response = client.get("/api/runtime/workspace-cleanup")
        assert response.status_code == 200
        assert response.json()["next_run_text"]
        assert response.json()["last_run"] is None
        document = client.get("/api/config").json()["document"]
        assert WorkspaceCleanupConfig(**document["runtime"].get("workspace_cleanup", {})).hour == 6
        assert document["runtime"].get("worktree_retention_days", 7) == 7
        schedule = WorkspaceCleanupConfig(kind="weekly", weekday=5, hour=11, minute=20).model_dump()
        document["runtime"].update(workspace_cleanup=schedule, worktree_retention_days=3)
        saved = client.put("/api/config", json={"document": document})
        assert saved.status_code == 200, saved.text
        assert saved.json()["document"]["runtime"]["workspace_cleanup"] == schedule
        assert client.get("/api/config").json()["document"]["runtime"]["worktree_retention_days"] == 3
        due = client.get("/api/runtime/workspace-cleanup").json()["next_run_at"]
        assert datetime.fromtimestamp(due).weekday() == 5
        document["runtime"]["workspace_cleanup"]["minute"] = 60
        assert client.put("/api/config", json={"document": document}).status_code == 422
        assert client.post("/api/runtime/workspace-cleanup").status_code == 405
    assert target.exists()


async def test_same_schedule_is_not_repeated_by_another_manager(cleanup_world):
    """同一周期已经执行后，另一管理器不得用陈旧内存重复执行。"""

    config, store, manager, create = cleanup_world
    second = cleanup.WorkspaceCleanupManager(lambda: config, store)
    due = time.time()
    first = create()
    await manager.run_once(due)
    later = create()
    await second.run_once(due)
    assert not first.exists() and later.exists()


async def test_config_change_during_scan_prevents_deletion(cleanup_world, monkeypatch):
    """扫描目录期间停用清理，真正删除前必须读取最新配置。"""

    config, _, manager, create = cleanup_world
    target = create()
    def disable(_target):
        """模拟管理员在大小统计期间保存停用配置。"""
        config.runtime.workspace_cleanup.enabled = False
        return 1
    monkeypatch.setattr(cleanup, "_directory_bytes", disable)
    await manager.run_once(time.time())
    assert target.exists()
    assert manager.state["last_run"]["skipped"] == 1


async def test_missing_directory_updates_stale_record(cleanup_world):
    """目录此前已移走时只清理旧标记，不能删除移走后的目录。"""

    _, store, manager, create = cleanup_world
    target = create()
    relocated = target.with_name(target.name + "-relocated")
    target.rename(relocated)
    await manager.run_once(time.time())
    assert relocated.exists() and not retained_marker_path(target).exists()
    assert store.workspace_cleanup_record(target.name)["workspace_status"] == "removed"


@pytest.mark.parametrize("link_kind", ["bucket", "git"])
async def test_directory_links_are_visible_and_preserved(cleanup_world, link_kind):
    """仓库分桶和 Git 元数据的链接拒绝访问，跳过原因必须可见。"""

    _, _, manager, create = cleanup_world
    target = create()
    entry = target.parent if link_kind == "bucket" else target / ".git"
    outside = entry.with_name(entry.name + "-outside")
    entry.rename(outside)
    if os.name == "nt":
        subprocess.run(["cmd", "/c", "mklink", "/J", str(entry), str(outside)], check=True, capture_output=True)
    else:
        entry.symlink_to(outside, target_is_directory=True)
    await manager.run_once(time.time())
    assert outside.exists()
    report = manager.state["last_run"]
    assert report["removed"] == 0 and report["skipped"] == 1
    assert report["details"][0]["reason"]


async def test_unknown_directory_and_removed_repository_linked_worktree_are_preserved(cleanup_world):
    """缺少标记的目录和无法确认基础仓库的 linked worktree 不自动删除。"""

    config, _, manager, create = cleanup_world
    target = create(linked=True)
    unknown = target.parent / "unknown-directory"
    unknown.mkdir()
    (unknown / "keep.txt").write_text("不能删除", encoding="utf-8")
    config.repositories = []
    await manager.run_once(time.time())
    assert target.exists() and (unknown / "keep.txt").exists()
    assert manager.state["last_run"]["skipped"] == 1


async def test_scheduler_recovers_from_temporary_store_failure(cleanup_world, monkeypatch):
    """持久化短暂失败后保持循环存活，恢复时仅重新安排未来计划。"""

    _, store, manager, create = cleanup_world
    target = create()
    original = store.set_service_state
    def fail(_key, _value):
        """仅模拟测试数据库暂时不可写。"""
        raise OSError("临时错误")
    monkeypatch.setattr(store, "set_service_state", fail)
    manager.start()
    await asyncio.sleep(0.02)
    assert manager.snapshot()["scheduler_error"]
    assert not manager._task.done()
    monkeypatch.setattr(store, "set_service_state", original)
    manager.notify_config_changed()
    await asyncio.sleep(0.02)
    await manager.close()
    assert not manager.snapshot()["scheduler_error"]
    assert target.exists()


async def test_timer_executes_and_shutdown_waits_for_current_deletion(cleanup_world, monkeypatch):
    """真实后台循环到期清理；退出等待已开始删除收尾，但不继续下个目录。"""

    _, _, manager, create = cleanup_world
    targets = [create(), create()]
    entered = threading.Event()
    release = threading.Event()
    original = cleanup.remove_expired_run_workspace
    def slow_remove(source, target):
        """用事件模拟删除途中停止，不对真实目录注入延时。"""
        entered.set()
        assert release.wait(3)
        original(source, target)
    monkeypatch.setattr(cleanup, "remove_expired_run_workspace", slow_remove)
    manager.start()
    closing = None
    try:
        await asyncio.sleep(0.02)
        manager.state["next_run_at"] = time.time() + 0.1
        manager.notify_config_changed()
        assert await asyncio.to_thread(entered.wait, 2)
        closing = asyncio.create_task(manager.close())
        await asyncio.sleep(0.02)
        assert not closing.done()
    finally:
        release.set()
        await asyncio.wait_for(closing if closing is not None else manager.close(), 3)
    assert sum(target.exists() for target in targets) == 1
    assert manager.state["last_run"]["status"] == "interrupted"


async def test_successful_record_update_is_protected_by_workspace_lease(cleanup_world, monkeypatch):
    """删除和成功状态回写使用同一临界区，重试不能在中间创建新目录。"""

    _, store, manager, create = cleanup_world
    target = create()
    original = store.record_workspace_cleanup
    def record(run_id, path, outcome):
        """模拟另一个运行在清理状态回写时尝试使用此目录。"""
        assert not store.acquire_locks([workspace_usage_lock_key(target)], "retry-run", 120)
        original(run_id, path, outcome)
    monkeypatch.setattr(store, "record_workspace_cleanup", record)
    await manager.run_once(time.time())
    assert not target.exists()
    assert manager.state["last_run"]["removed"] == 1
    assert store.acquire_locks([workspace_usage_lock_key(target)], "retry-run", 120)
    store.release_locks([workspace_usage_lock_key(target)], "retry-run")


@pytest.mark.parametrize("inherited", [False, True])
async def test_executor_acquires_same_usage_lease_before_preparation(cleanup_world, snapshot_factory, monkeypatch, inherited):
    """真实执行入口在创建或继承工作区前就取得清理器使用的同一租约。"""

    config, store, _, create = cleanup_world
    parent = create()
    config.runtime.codex.execution_mode = "cli"
    config.agents["code-reviewer"].sandbox = "danger-full-access"
    snapshot = snapshot_factory(provider="github-main")
    event = detect_events(None, snapshot, emit_initial=True)[0]
    store.save_snapshot_and_events(snapshot, [event])
    executor = AgentExecutor(config, store)
    checked = []
    def verify_lease(*_args, **_kwargs):
        """只核对锁，在任何真实程序或 Git 准备前结束测试运行。"""
        with store.connect() as db:
            record = db.execute("SELECT run_id, root_run_id FROM agent_runs WHERE idempotency_key='lease-test'").fetchone()
            lock = db.execute("SELECT resource_key, owner FROM resource_locks WHERE resource_key LIKE 'workspace_usage:%'").fetchone()
        expected = parent if inherited else executor.run_workspace_path(config.repositories[0], record["run_id"])
        assert lock["resource_key"] == workspace_usage_lock_key(expected)
        assert lock["owner"] == record["root_run_id"]
        checked.append(True)
        raise CodexRuntimeError("测试到此为止", error_code="cleanup_test_stop")
    monkeypatch.setattr("teamwork_review_agents.executor.check_runtime_readiness", verify_lease)
    with pytest.raises(AgentExecutionError) as raised:
        await executor.execute(
            agent_name="code-reviewer", event=event, idempotency_key="lease-test",
            task="仅验证租约" if inherited else None,
            root_run_id=parent.name if inherited else None,
            parent_run_id=parent.name if inherited else None,
            depth=1 if inherited else 0, inherit_workspace=inherited,
            parent_workspace=parent if inherited else None,
        )
    assert raised.value.error_code == "cleanup_test_stop"
    assert checked and parent.exists()
