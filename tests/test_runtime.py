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
    assert runtime.last_summary is not None
    assert runtime.last_summary["processed_events"] == 1
