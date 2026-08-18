"""后台运行循环的手动事件调度测试。"""

import asyncio

from teamwork_review_agents.orchestrator import CycleSummary
from teamwork_review_agents.runtime import BackgroundRuntime
from teamwork_review_agents.state import StateStore


async def test_manual_dispatch_runs_while_paused_without_scanning(
    configured_app_factory,
) -> None:
    """暂停扫描时，手动事件仍应只触发调度而不访问 Provider。"""

    config = configured_app_factory()

    class FakeManager:
        """提供运行循环所需的最小配置管理接口。"""

        def __init__(self) -> None:
            self.config = config
            self.store = StateStore(config.database.path)
            self.store.initialize()
            self.last_error = None

        def reload_if_changed(self) -> None:
            return None

    dispatch_finished = asyncio.Event()
    full_scan_calls = 0

    class FakeOrchestrator:
        """记录运行循环选择了完整扫描还是仅事件调度。"""

        async def request_shutdown(self) -> list[str]:
            return []

        async def process_events(self, summary: CycleSummary) -> None:
            summary.processed_events = 1
            dispatch_finished.set()

        async def run_once(self) -> CycleSummary:
            nonlocal full_scan_calls
            full_scan_calls += 1
            return CycleSummary()

    runtime = BackgroundRuntime(FakeManager())
    runtime._orchestrator = FakeOrchestrator()
    runtime.pause()
    await runtime.start()
    try:
        runtime.dispatch_events_now()
        await asyncio.wait_for(dispatch_finished.wait(), timeout=1)
    finally:
        await runtime.stop()

    assert full_scan_calls == 0
    assert runtime.last_dispatch_summary is not None
    assert runtime.last_dispatch_summary["processed_events"] == 1


async def test_agent_dispatch_does_not_block_the_next_scheduled_scan(
    configured_app_factory,
) -> None:
    """长时间 Agent 调度期间，下一次定时扫描仍应按期执行。"""

    config = configured_app_factory()
    config.scanner.interval_seconds = 1

    class FakeManager:
        """提供双循环测试所需的最小配置管理接口。"""

        def __init__(self) -> None:
            self.config = config
            self.store = StateStore(config.database.path)
            self.store.initialize()
            self.last_error = None

        def reload_if_changed(self) -> None:
            return None

    first_scan_finished = asyncio.Event()
    second_scan_finished = asyncio.Event()
    third_scan_finished = asyncio.Event()
    dispatch_started = asyncio.Event()
    shutdown_requested = asyncio.Event()
    release_dispatch = asyncio.Event()
    scan_calls = 0

    class FakeOrchestrator:
        """用阻塞调度模拟运行一小时的 Agent。"""

        async def request_shutdown(self) -> list[str]:
            shutdown_requested.set()
            release_dispatch.set()
            return []

        async def scan(self, summary: CycleSummary) -> None:
            nonlocal scan_calls
            scan_calls += 1
            summary.snapshots = scan_calls
            if scan_calls == 1:
                first_scan_finished.set()
            if scan_calls == 2:
                second_scan_finished.set()
            if scan_calls == 3:
                third_scan_finished.set()

        async def process_events(self, summary: CycleSummary) -> None:
            dispatch_started.set()
            await release_dispatch.wait()
            summary.agent_runs = 1

    runtime = BackgroundRuntime(FakeManager())
    runtime._orchestrator = FakeOrchestrator()
    await runtime.start()
    try:
        await asyncio.wait_for(first_scan_finished.wait(), timeout=1)
        await asyncio.wait_for(dispatch_started.wait(), timeout=1)
        assert runtime.dispatching_events

        await asyncio.wait_for(second_scan_finished.wait(), timeout=2)

        assert scan_calls == 2
        assert runtime.dispatching_events
        assert not runtime.running_cycle

        runtime.scan_now()
        await asyncio.wait_for(third_scan_finished.wait(), timeout=0.5)
        assert scan_calls == 3
        assert runtime.dispatching_events
    finally:
        await runtime.stop()

    assert shutdown_requested.is_set()
    assert runtime.last_dispatch_summary is not None
    assert runtime.last_dispatch_summary["agent_runs"] == 1
