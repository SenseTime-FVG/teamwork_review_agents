"""依赖缓存清理只操作测试临时目录，覆盖时间、并发、归属与兼容行为。"""

import asyncio
import json
from pathlib import Path
import sys
import threading
import time

import pytest
from pydantic import ValidationError

from teamwork_review_agents.config import RuntimeConfig
from teamwork_review_agents.dependency_cache import RepositoryCacheUsage, USAGE_FILE, _last_used
from teamwork_review_agents.locks import LockCancelledError, LockTimeoutError
from teamwork_review_agents.preflight_cache import repository_cache_root
from teamwork_review_agents.state import StateStore
from teamwork_review_agents.workspace_cleanup import WorkspaceCleanupManager


@pytest.fixture
def cache_world(configured_app_factory):
    """构造独立配置、状态库和受管缓存，绝不读取真实 data。"""

    config = configured_app_factory()
    store = StateStore(config.database.path)
    store.initialize()
    root = repository_cache_root(config, config.repositories[0])
    root.mkdir(parents=True)
    (root / "dependency.txt").write_text("可重新生成的缓存" * 100, encoding="utf-8")
    manager = WorkspaceCleanupManager(lambda: config, store)
    return config, store, root, manager


def mark(root, *, days=8, **overrides):
    """生成测试所需的旧使用记录。"""

    value = {"version": 1, "cache_name": root.name, "last_used_at": time.time() - days * 86400}
    value.update(overrides)
    (root / USAGE_FILE).write_text(json.dumps(value), encoding="utf-8")


async def run(manager):
    """显式运行单批，避免调度测试依赖真实时钟或后台进程。"""

    await manager.run_once(time.time())
    return manager.state["last_run"]


@pytest.mark.parametrize("current_repository", [True, False])
async def test_expired_cache_removed_without_touching_history(cache_world, current_repository):
    """当前仓库与旧身份缓存均按闲置期清理，原有工作区统计及数据库不被删除。"""

    config, store, root, manager = cache_world
    mark(root)
    if not current_repository:
        config.repositories[0].project = "other/repository"
    report = await run(manager)
    assert not root.exists()
    assert report["removed"] == 0
    assert report["dependency_cache"]["removed"] == 1
    assert report["dependency_cache"]["reclaimed_bytes"] > 0
    assert store.path.exists()
    assert store.get_service_state("workspace_cleanup")["last_run"]["dependency_cache"]["removed"] == 1
    assert (root.parent / ".usage-locks" / f"{root.name}.lock").exists()


@pytest.mark.parametrize("days", [0, 1, 6, -1])
async def test_recent_or_future_cache_is_preserved(cache_world, days):
    """未过期或未来时间均不能被清理；定时检查本身不刷新最近使用时间。"""

    _, _, root, manager = cache_world
    mark(root, days=days)
    before = (root / USAGE_FILE).read_bytes()
    report = await run(manager)
    assert root.exists()
    assert (root / USAGE_FILE).read_bytes() == before
    assert report["dependency_cache"]["skipped"] == 1


async def test_legacy_cache_receives_full_grace_period(cache_world):
    """旧缓存首次发现只登记，包文件再旧也不能直接删掉。"""

    _, _, root, manager = cache_world
    started = time.time()
    report = await run(manager)
    assert root.exists()
    assert _last_used(root) >= started
    assert "首次登记" in report["dependency_cache"]["details"][0]["reason"]
    assert (await run(manager))["dependency_cache"]["removed"] == 0


@pytest.mark.parametrize("override", [
    {"last_used_at": True}, {"last_used_at": "1"}, {"last_used_at": 0},
    {"last_used_at": float("nan")}, {"cache_name": "unknown"}, {"version": True},
])
async def test_invalid_usage_metadata_is_not_expiry_evidence(cache_world, override):
    """类型异常、非有限时间和错误身份不得作为过期依据。"""

    _, _, root, manager = cache_world
    mark(root, **override)
    report = await run(manager)
    assert root.exists()
    assert report["dependency_cache"]["skipped"] == 1


async def test_shared_users_run_in_parallel_and_block_cleanup(cache_world):
    """并发使用不串行化，但独占清理不能在任何一个使用者退出前开始。"""

    _, _, root, manager = cache_world
    async with RepositoryCacheUsage(root), RepositoryCacheUsage(root, timeout_seconds=0):
        mark(root)
        with pytest.raises(LockTimeoutError):
            async with RepositoryCacheUsage(root, exclusive=True, timeout_seconds=0):
                pytest.fail("独占锁不应取得")
        report = await run(manager)
        assert root.exists()
        assert report["dependency_cache"]["skipped"] == 1
    assert time.time() - _last_used(root) < 2
    assert (await run(manager))["dependency_cache"]["removed"] == 0


async def test_usage_updates_on_error_exit_and_wait_is_cancelable(cache_world):
    """失败收尾也更新最后使用时间，等待中的任务取消不会占用锁。"""

    _, _, root, _ = cache_world
    with pytest.raises(RuntimeError):
        async with RepositoryCacheUsage(root):
            mark(root)
            raise RuntimeError("测试失败")
    assert time.time() - _last_used(root) < 2
    async with RepositoryCacheUsage(root, exclusive=True):
        with pytest.raises(LockCancelledError):
            async with RepositoryCacheUsage(root, cancel_check=lambda: True):
                pytest.fail("取消后不得进入")
    async with RepositoryCacheUsage(root, exclusive=True, timeout_seconds=0):
        pass


async def test_cross_process_user_protects_cache_until_process_exit(cache_world):
    """真实替身进程持锁时不能删除；进程退出后锁自动释放。"""

    _, _, root, manager = cache_world
    script = (
        "import asyncio,sys\nfrom pathlib import Path\n"
        "from teamwork_review_agents.dependency_cache import RepositoryCacheUsage\n"
        "async def main():\n"
        "    async with RepositoryCacheUsage(Path(sys.argv[1])):\n"
        "        print('ready', flush=True)\n"
        "        await asyncio.sleep(30)\n"
        "asyncio.run(main())\n"
    )
    process = await asyncio.create_subprocess_exec(sys.executable, "-c", script, str(root), stdout=asyncio.subprocess.PIPE)
    try:
        assert await asyncio.wait_for(process.stdout.readline(), 10) == b"ready\n"
        mark(root)
        assert (await run(manager))["dependency_cache"]["removed"] == 0
    finally:
        process.terminate()
        await asyncio.wait_for(process.wait(), 10)
    assert (await run(manager))["dependency_cache"]["removed"] == 1


@pytest.mark.parametrize("table", ["agent_runs", "preflight_runs"])
@pytest.mark.parametrize("status", ["queued", "preparing", "running"])
async def test_legacy_active_records_without_file_locks_protect_cache(cache_world, table, status):
    """旧运行即使尚未接入文件锁，也不能被定时清理误删缓存。"""

    _, store, root, manager = cache_world
    mark(root)
    with store.connect() as db:
        if table == "agent_runs":
            db.execute("INSERT INTO agent_runs (run_id,root_run_id,idempotency_key,agent_name,resource_key,repository_id,status,prompt,started_at) VALUES ('run','run','key','test','test','demo',?,'',?)", (status, time.time()))
        else:
            db.execute("INSERT INTO preflight_runs (run_id,idempotency_key,repository_id,head_sha,config_revision,status,started_at,cache_path) VALUES ('run','key','demo','sha','rev',?,?,?)", (status, time.time(), str(root)))
    assert (await run(manager))["dependency_cache"]["removed"] == 0
    assert root.exists()


@pytest.mark.parametrize("kind", ["root", "marker", "parent", "lock"])
async def test_path_links_are_never_followed(cache_world, tmp_path, kind):
    """缓存入口、标记、父目录及锁入口链接不能越界修改外部目录。"""

    _, _, root, manager = cache_world
    mark(root)
    external = tmp_path / "external"
    external.mkdir()
    valuable = external / "valuable.txt"
    valuable.write_text("必须保留", encoding="utf-8")
    if kind == "root":
        root.rename(root.with_name("preserved"))
        root.symlink_to(external, target_is_directory=True)
    elif kind == "parent":
        root.parent.rename(root.parent.with_name("preserved"))
        root.parent.symlink_to(external, target_is_directory=True)
    elif kind == "marker":
        (root / USAGE_FILE).unlink()
        (root / USAGE_FILE).symlink_to(valuable)
    else:
        lock_root = root.parent / ".usage-locks"
        lock_root.mkdir()
        (lock_root / f"{root.name}.lock").symlink_to(valuable)
    await run(manager)
    assert valuable.read_text(encoding="utf-8") == "必须保留"
    assert external.exists()


async def test_mid_scan_config_change_preserves_cache(cache_world, monkeypatch):
    """耗时统计后再次检查保留期，防止缩短/延长配置时沿用旧删除决定。"""

    config, _, root, manager = cache_world
    mark(root)
    def change_config(_):
        """在测量阶段修改真实配置，模拟页面同时保存。"""
        config.runtime.dependency_cache_retention_days = 30
        return 1
    monkeypatch.setattr("teamwork_review_agents.workspace_cleanup._directory_bytes", change_config)
    assert (await run(manager))["dependency_cache"]["removed"] == 0
    assert root.exists()


async def test_cancelled_cleanup_keeps_exclusive_lock_until_file_thread_finishes(cache_world, monkeypatch):
    """删除线程未结束时，即使协程取消也不允许新任务取得使用锁。"""

    _, _, root, manager = cache_world
    mark(root)
    entered, release = threading.Event(), threading.Event()
    def slow_measure(_):
        """只模拟文件耗时，测试结束前必须释放线程。"""
        entered.set()
        assert release.wait(10)
        return 1
    monkeypatch.setattr("teamwork_review_agents.workspace_cleanup._directory_bytes", slow_measure)
    task = asyncio.create_task(run(manager))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        with pytest.raises(LockTimeoutError):
            async with RepositoryCacheUsage(root, timeout_seconds=0):
                pytest.fail("文件线程未退出前不应释放独占锁")
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert root.exists()
    assert manager.state["last_run"]["status"] == "interrupted"


async def test_replaced_directory_is_preserved(cache_world, monkeypatch):
    """即使替换目录复制了相同标记，也不能删除检查后新出现的内容。"""

    _, _, root, manager = cache_world
    mark(root)
    def replace(_):
        """在测量期间替换入口，模拟外部目录迁移。"""
        root.rename(root.with_name("preserved"))
        root.mkdir()
        mark(root)
        (root / "valuable.txt").write_text("必须保留", encoding="utf-8")
        return 1
    monkeypatch.setattr("teamwork_review_agents.workspace_cleanup._directory_bytes", replace)
    report = await run(manager)
    assert (root / "valuable.txt").exists()
    assert report["dependency_cache"]["removed"] == 0


async def test_disabled_schedule_does_not_inspect_cache(cache_world):
    """停用共用计划后，缓存清理也必须停用。"""

    config, _, root, manager = cache_world
    mark(root)
    config.runtime.workspace_cleanup.enabled = False
    report = await run(manager)
    assert root.exists()
    assert report["status"] == "skipped"


def test_cache_retention_is_independent_and_positive():
    """缓存保留期独立于工作区配置，并拒绝零与负数。"""

    assert RuntimeConfig(worktree_retention_days=30).dependency_cache_retention_days == 7
    for value in (0, -1):
        with pytest.raises(ValidationError):
            RuntimeConfig(dependency_cache_retention_days=value)
