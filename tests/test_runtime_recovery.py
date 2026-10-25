"""后台数据库故障、任务恢复和健康信息降级的回归测试。"""

import asyncio
import sqlite3
import time

import pytest
from fastapi.testclient import TestClient

from teamwork_review_agents.runtime import BackgroundRuntime
from teamwork_review_agents.state import StateStore
from teamwork_review_agents.webapp import create_app
from test_environment_and_web import write_config


def database_error() -> sqlite3.OperationalError:
    """模拟生产环境中捕获的 SQLite 文件打开错误。"""

    error = sqlite3.OperationalError("unable to open database file")
    error.sqlite_errorcode = sqlite3.SQLITE_CANTOPEN
    error.sqlite_errorname = "SQLITE_CANTOPEN"
    return error


@pytest.fixture
def runtime(configured_app_factory):
    """使用独立临时数据库，避免访问真实平台与运行数据。"""

    config = configured_app_factory()

    class Manager:
        """仅提供后台循环所需的配置管理接口。"""

        def __init__(self):
            self.config = config
            self.store = StateStore(config.database.path)
            self.store.initialize()
            self.last_error = None

        def reload_if_changed(self):
            """测试配置保持不变。"""

    return BackgroundRuntime(Manager())


async def test_scanner_recovers_from_status_write_failure_without_stale_running_flag(runtime, monkeypatch):
    """启动和收尾写库失败后仍保持监督，重扫成功前不提前解除告警。"""

    monkeypatch.setattr("teamwork_review_agents.runtime.RETRY_INITIAL_SECONDS", 0.05)
    write_failed = asyncio.Event()
    scan_started = asyncio.Event()
    release_scan = asyncio.Event()
    scan_completed = asyncio.Event()
    original_write = runtime.store.set_service_state
    writes = 0

    def flaky_write(key, payload):
        """连续阻断启动与收尾写入，模拟一次短暂数据库故障。"""

        nonlocal writes
        if key == "runtime":
            writes += 1
            if writes <= 2:
                write_failed.set()
                raise database_error()
        return original_write(key, payload)

    class Orchestrator:
        """阻塞恢复后的扫描，以便检查恢复中仍保留的故障。"""

        async def scan(self, summary):
            scan_started.set()
            await release_scan.wait()
            summary.snapshots = 1
            scan_completed.set()

    runtime._orchestrator = Orchestrator()
    monkeypatch.setattr(runtime.store, "set_service_state", flaky_write)
    runtime._scan_task = asyncio.create_task(runtime._supervise_loop("scanner", runtime._scan_loop))
    try:
        await asyncio.wait_for(write_failed.wait(), 1)
        failed = await runtime.snapshot()
        assert not failed["running_cycle"]
        assert failed["last_finished_at"] is not None
        assert "SQLITE_CANTOPEN（14）" in failed["last_error"]
        assert "记录时剩余空间" in failed["last_error"]
        assert failed["background_tasks"]["scanner"]["state"] == "retrying"
        assert failed["background_tasks"]["scanner"]["retry_at"] is not None
        assert not runtime._scan_task.done()

        await asyncio.wait_for(scan_started.wait(), 1)
        recovering = await runtime.snapshot()
        assert recovering["running_cycle"]
        assert recovering["last_error"]
        assert recovering["background_tasks"]["scanner"]["state"] == "recovering"

        release_scan.set()
        await asyncio.wait_for(scan_completed.wait(), 1)
        recovered = await runtime.snapshot()
        assert not recovered["running_cycle"]
        assert recovered["last_error"] is None
        assert recovered["last_summary"]["snapshots"] == 1
        assert recovered["background_tasks"]["scanner"] == {"state": "running"}
    finally:
        runtime._stop_event.set()
        runtime._scan_wake_event.set()
        release_scan.set()
        await asyncio.wait_for(runtime._scan_task, 1)


async def test_persistent_database_failure_retries_with_delay_and_remains_observable(runtime, monkeypatch):
    """数据库持续不可用时不忙循环，状态接口仍可返回内存故障且停止及时。"""

    monkeypatch.setattr("teamwork_review_agents.runtime.RETRY_INITIAL_SECONDS", 0.03)
    monkeypatch.setattr("teamwork_review_agents.runtime.RETRY_MAX_SECONDS", 0.05)
    failed = asyncio.Event()
    attempts = 0

    def unavailable(*args, **kwargs):
        """模拟所有数据库读写均失败。"""

        raise database_error()

    async def broken_loop():
        """在重载或扫描入口抛出数据库异常。"""

        nonlocal attempts
        attempts += 1
        failed.set()
        raise database_error()

    monkeypatch.setattr(runtime.store, "set_service_state", unavailable)
    monkeypatch.setattr(runtime.store, "dashboard_stats", unavailable)
    runtime._scan_task = asyncio.create_task(runtime._supervise_loop("scanner", broken_loop))
    await asyncio.wait_for(failed.wait(), 1)
    snapshot = await runtime.snapshot("repo")
    assert snapshot["stats_available"] is False
    assert "SQLITE_CANTOPEN" in snapshot["status_error"]
    assert snapshot["background_tasks"]["scanner"]["error"]
    await asyncio.sleep(0.09)
    assert 2 <= attempts <= 3
    assert snapshot["stats"] == {"runs": {}, "events": {}, "change_requests": {}}
    before_stop = attempts
    runtime._stop_event.set()
    await asyncio.wait_for(runtime._scan_task, 0.2)
    assert attempts == before_stop


async def test_dispatcher_rechecks_queue_after_unexpected_exit(runtime, monkeypatch):
    """调度异常退出会退避恢复并唤醒队列，不要求新扫描产生通知。"""

    monkeypatch.setattr("teamwork_review_agents.runtime.RETRY_INITIAL_SECONDS", 0.01)
    completed = asyncio.Event()
    calls = 0

    async def dispatch_cycle():
        """第一轮失败，恢复后由监督循环重新检查队列。"""

        nonlocal calls
        calls += 1
        if calls == 1:
            raise database_error()
        runtime._clear_worker_failure("dispatcher")
        completed.set()

    monkeypatch.setattr(runtime, "_run_dispatch_cycle", dispatch_cycle)
    runtime._dispatch_task = asyncio.create_task(runtime._supervise_loop("dispatcher", runtime._dispatch_loop))
    runtime.dispatch_events_now()
    try:
        await asyncio.wait_for(completed.wait(), 1)
        assert calls == 2
        assert runtime._worker_failures == {}
    finally:
        runtime._stop_event.set()
        runtime._dispatch_event.set()
        await asyncio.wait_for(runtime._dispatch_task, 1)


async def test_task_that_returns_unexpectedly_is_restarted_and_reported(runtime, monkeypatch):
    """后台函数直接返回也属于异常退出，不允许静默停止扫描。"""

    monkeypatch.setattr("teamwork_review_agents.runtime.RETRY_INITIAL_SECONDS", 0.03)
    returned = asyncio.Event()

    async def returns_immediately():
        """模拟后台循环无异常地提前结束。"""

        returned.set()

    task = asyncio.create_task(runtime._supervise_loop("scanner", returns_immediately))
    try:
        await asyncio.wait_for(returned.wait(), 1)
        assert "后台循环意外退出" in runtime.last_error
        assert runtime._worker_failures["scanner"]["retry_at"] > time.time()
        assert not task.done()
    finally:
        runtime._stop_event.set()
        await asyncio.wait_for(task, 1)


async def test_snapshot_detects_a_dead_task_even_if_running_flag_is_stale(runtime):
    """任务实际退出时直接报告停止，不能被旧的运行中布尔值掩盖。"""

    async def completed():
        """构造已经退出的后台任务。"""

    runtime._scan_task = asyncio.create_task(completed())
    await runtime._scan_task
    runtime.running_cycle = True
    snapshot = await runtime.snapshot()
    assert snapshot["running_cycle"] is False
    assert snapshot["background_tasks"]["scanner"]["state"] == "stopped"
    assert snapshot["background_tasks"]["scanner"]["error"] == "后台任务已停止"


async def test_scheduled_occurrence_releases_active_reference_after_startup_write_error(runtime, monkeypatch):
    """定时周期启动写库失败仍会收尾，不能永久阻塞仓库维护或关闭服务。"""

    class Orchestrator:
        """初始状态保存失败时不应进入实际定时业务。"""

        async def run_scheduled_rule(self, *args, **kwargs):
            raise AssertionError("数据库故障后不应执行业务")

    def unavailable(*args):
        """让启动和收尾均遇到相同的数据库错误。"""

        raise database_error()

    monkeypatch.setattr(runtime.store, "set_service_state", unavailable)
    await runtime._run_scheduled_occurrence(Orchestrator(), None, time.time(), "test")
    assert runtime._active_orchestrators == set()
    assert runtime.last_schedule_finished_at is not None
    assert "SQLITE_CANTOPEN" in runtime.last_schedule_error


def test_storage_diagnostics_unavailable_does_not_mask_database_error(runtime, monkeypatch):
    """目录或磁盘状态无法读取时，仍保留真正触发故障的 SQLite 错误。"""

    def unavailable(*args):
        """模拟数据库目录此时已经不可达。"""

        raise FileNotFoundError("目录不可用")

    monkeypatch.setattr("teamwork_review_agents.runtime.shutil.disk_usage", unavailable)
    message = runtime._failure_message(database_error())
    assert "SQLITE_CANTOPEN（14）" in message
    assert "记录时无法读取剩余空间" in message


def test_status_api_returns_health_when_database_is_unavailable(tmp_path, monkeypatch):
    """即使统计库打不开，Dashboard 仍能取得错误，避免只收到 HTTP 500。"""

    app = create_app(write_config(tmp_path), start_scheduler=False)
    with TestClient(app) as client:
        def unavailable(*args):
            """阻断统计读取，不影响应用初始化。"""

            raise database_error()

        monkeypatch.setattr(app.state.runtime.store, "dashboard_stats", unavailable)
        response = client.get("/api/status")
        assert response.status_code == 200
        assert response.json()["stats_available"] is False
        assert "SQLITE_CANTOPEN" in response.json()["status_error"]
