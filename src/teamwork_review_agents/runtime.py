"""后台扫描循环、热加载和服务状态管理。"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from .config_manager import ConfigManager
from .orchestrator import CycleSummary, Orchestrator


class BackgroundRuntime:
    """在 FastAPI 生命周期内持续运行扫描和 Agent 调度。"""

    def __init__(self, manager: ConfigManager) -> None:
        self.manager = manager
        self.store = manager.store
        self.store.recover_interrupted_work()
        self._stop_event = asyncio.Event()
        self._scan_wake_event = asyncio.Event()
        self._dispatch_event = asyncio.Event()
        self._scan_task: asyncio.Task[None] | None = None
        self._dispatch_task: asyncio.Task[None] | None = None
        self._orchestrator = Orchestrator(
            manager.config,
            recover_interrupted=False,
        )
        self._active_orchestrators: set[Orchestrator] = set()
        self._revision = manager.config.revision
        self.paused = False
        self.running_cycle = False
        self.dispatching_events = False
        self.last_started_at: float | None = None
        self.last_finished_at: float | None = None
        self.last_summary: dict[str, Any] | None = None
        self.last_error: str | None = None
        self.last_dispatch_started_at: float | None = None
        self.last_dispatch_finished_at: float | None = None
        self.last_dispatch_summary: dict[str, Any] | None = None
        self.last_dispatch_error: str | None = None

    async def start(self) -> None:
        """启动扫描与事件执行两个后台循环。"""

        if self._scan_task is None:
            self._scan_task = asyncio.create_task(
                self._scan_loop(),
                name="teamwork-background-scanner",
            )
        if self._dispatch_task is None:
            self._dispatch_task = asyncio.create_task(
                self._dispatch_loop(),
                name="teamwork-background-dispatcher",
            )

    async def stop(self) -> None:
        """停止新工作、取消活动 Agent，并等待全部子进程安全收尾。"""

        self._stop_event.set()
        self._scan_wake_event.set()
        self._dispatch_event.set()
        orchestrators = {
            self._orchestrator,
            *self._active_orchestrators,
        }
        if orchestrators:
            await asyncio.gather(
                *(orchestrator.request_shutdown() for orchestrator in orchestrators)
            )
        tasks = tuple(
            task
            for task in (self._scan_task, self._dispatch_task)
            if task is not None
        )
        if tasks:
            await asyncio.gather(*tasks)
        self._scan_task = None
        self._dispatch_task = None

    def notify_config_changed(self) -> None:
        """唤醒后台，让下一周期应用最新配置。"""

        self._scan_wake_event.set()

    def scan_now(self) -> None:
        """请求尽快执行一次扫描，不与正在运行的周期重叠。"""

        self._scan_wake_event.set()

    def dispatch_events_now(self) -> None:
        """请求尽快只处理待处理事件，不额外访问 Provider。"""

        self._dispatch_event.set()

    def pause(self) -> None:
        """暂停新扫描，不中断正在运行的 Agent。"""

        self.paused = True
        self._persist_status()

    def resume(self) -> None:
        """恢复扫描并立即唤醒后台。"""

        self.paused = False
        self._scan_wake_event.set()
        self._persist_status()

    def _reload_config(self) -> None:
        """应用手工编辑或 UI 保存后的新配置。"""

        self.manager.reload_if_changed()
        config = self.manager.config
        if config.revision != self._revision:
            self._orchestrator = Orchestrator(
                config,
                recover_interrupted=False,
            )
            self._revision = config.revision

    def _persist_status(self) -> None:
        """保存可在重启后查看的最近服务状态。"""

        self.store.set_service_state(
            "runtime",
            {
                "paused": self.paused,
                "running_cycle": self.running_cycle,
                "dispatching_events": self.dispatching_events,
                "config_revision": self._revision,
                "config_error": self.manager.last_error,
                "last_started_at": self.last_started_at,
                "last_finished_at": self.last_finished_at,
                "last_summary": self.last_summary,
                "last_error": self.last_error,
                "last_dispatch_started_at": self.last_dispatch_started_at,
                "last_dispatch_finished_at": self.last_dispatch_finished_at,
                "last_dispatch_summary": self.last_dispatch_summary,
                "last_dispatch_error": self.last_dispatch_error,
            },
        )

    async def snapshot(self) -> dict[str, Any]:
        """组合实时服务状态和数据库统计。"""

        stats = await asyncio.to_thread(self.store.dashboard_stats)
        return {
            "paused": self.paused,
            "running_cycle": self.running_cycle,
            "dispatching_events": self.dispatching_events,
            "config_revision": self._revision,
            "config_error": self.manager.last_error,
            "last_started_at": self.last_started_at,
            "last_finished_at": self.last_finished_at,
            "last_summary": self.last_summary,
            "last_error": self.last_error,
            "last_dispatch_started_at": self.last_dispatch_started_at,
            "last_dispatch_finished_at": self.last_dispatch_finished_at,
            "last_dispatch_summary": self.last_dispatch_summary,
            "last_dispatch_error": self.last_dispatch_error,
            "stats": stats,
        }

    async def _run_scan_cycle(self) -> None:
        """执行一次扫描并持久化事件，不等待 Agent 执行。"""

        self.running_cycle = True
        self.last_started_at = time.time()
        self.last_error = None
        self._persist_status()
        try:
            summary = CycleSummary()
            orchestrator = self._orchestrator
            self._active_orchestrators.add(orchestrator)
            try:
                await orchestrator.scan(summary)
            finally:
                self._active_orchestrators.discard(orchestrator)
            self.last_summary = summary.to_dict()
            if summary.errors:
                self.last_error = "; ".join(summary.errors)
        except Exception as exc:
            self.last_error = str(exc)
            self.last_summary = None
        finally:
            self.running_cycle = False
            self.last_finished_at = time.time()
            self._persist_status()
            # 即使单个仓库失败，其他仓库本轮已产生的事件仍需及时调度。
            self._dispatch_event.set()

    async def _run_dispatch_cycle(self) -> None:
        """独立领取并执行持久化事件，不占用扫描循环。"""

        self.dispatching_events = True
        self.last_dispatch_started_at = time.time()
        self.last_dispatch_error = None
        self._persist_status()
        try:
            summary = CycleSummary()
            orchestrator = self._orchestrator
            self._active_orchestrators.add(orchestrator)
            try:
                await orchestrator.process_events(summary)
            finally:
                self._active_orchestrators.discard(orchestrator)
            self.last_dispatch_summary = summary.to_dict()
            if summary.errors:
                self.last_dispatch_error = "; ".join(summary.errors)
        except Exception as exc:
            self.last_dispatch_error = str(exc)
            self.last_dispatch_summary = None
        finally:
            self.dispatching_events = False
            self.last_dispatch_finished_at = time.time()
            self._persist_status()
        cutoff = time.time() - self.manager.config.web.log_retention_days * 86400
        await asyncio.to_thread(self.store.prune_run_logs, cutoff)
        await asyncio.to_thread(
            self.store.prune_terminal_target_events,
            cutoff,
            max_attempts=self.manager.config.runtime.event_retry_count + 1,
        )

    async def _scan_loop(self) -> None:
        """按固定间隔扫描 Provider，不等待事件执行循环。"""

        next_scan_at = 0.0
        while not self._stop_event.is_set():
            self._reload_config()
            if self._stop_event.is_set():
                break
            if self.paused:
                self._persist_status()
                try:
                    await asyncio.wait_for(
                        self._scan_wake_event.wait(),
                        timeout=self.manager.config.web.config_poll_seconds,
                    )
                except TimeoutError:
                    pass
                self._scan_wake_event.clear()
                continue
            now = time.monotonic()
            if now < next_scan_at:
                timeout = min(
                    self.manager.config.web.config_poll_seconds,
                    max(0.0, next_scan_at - now),
                )
                try:
                    await asyncio.wait_for(
                        self._scan_wake_event.wait(),
                        timeout=timeout,
                    )
                except TimeoutError:
                    pass
                if self._scan_wake_event.is_set():
                    # 配置保存和手动扫描都要求尽快执行下一周期。
                    next_scan_at = 0.0
                self._scan_wake_event.clear()
                continue
            await self._run_scan_cycle()
            next_scan_at = time.monotonic() + self.manager.config.scanner.interval_seconds

    async def _dispatch_loop(self) -> None:
        """收到持久化事件通知后串行执行调度批次。"""

        while not self._stop_event.is_set():
            await self._dispatch_event.wait()
            self._dispatch_event.clear()
            if self._stop_event.is_set():
                break
            await self._run_dispatch_cycle()
