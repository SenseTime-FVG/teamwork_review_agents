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
        self._wake_event = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._orchestrator = Orchestrator(
            manager.config,
            recover_interrupted=False,
        )
        self._revision = manager.config.revision
        self.paused = False
        self.running_cycle = False
        self.last_started_at: float | None = None
        self.last_finished_at: float | None = None
        self.last_summary: dict[str, Any] | None = None
        self.last_error: str | None = None

    async def start(self) -> None:
        """启动唯一后台循环。"""

        if self._task is None:
            self._task = asyncio.create_task(self._loop(), name="teamwork-background-runtime")

    async def stop(self) -> None:
        """请求退出并等待当前扫描周期收尾。"""

        self._stop_event.set()
        self._wake_event.set()
        if self._task:
            await self._task
            self._task = None

    def notify_config_changed(self) -> None:
        """唤醒后台，让下一周期应用最新配置。"""

        self._wake_event.set()

    def scan_now(self) -> None:
        """请求尽快执行一次扫描，不与正在运行的周期重叠。"""

        self._wake_event.set()

    def pause(self) -> None:
        """暂停新扫描，不中断正在运行的 Agent。"""

        self.paused = True
        self._persist_status()

    def resume(self) -> None:
        """恢复扫描并立即唤醒后台。"""

        self.paused = False
        self._wake_event.set()
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
                "config_revision": self._revision,
                "config_error": self.manager.last_error,
                "last_started_at": self.last_started_at,
                "last_finished_at": self.last_finished_at,
                "last_summary": self.last_summary,
                "last_error": self.last_error,
            },
        )

    async def snapshot(self) -> dict[str, Any]:
        """组合实时服务状态和数据库统计。"""

        stats = await asyncio.to_thread(self.store.dashboard_stats)
        return {
            "paused": self.paused,
            "running_cycle": self.running_cycle,
            "config_revision": self._revision,
            "config_error": self.manager.last_error,
            "last_started_at": self.last_started_at,
            "last_finished_at": self.last_finished_at,
            "last_summary": self.last_summary,
            "last_error": self.last_error,
            "stats": stats,
        }

    async def _loop(self) -> None:
        """串行执行周期，并独立按较短间隔检测配置文件变化。"""

        next_scan_at = 0.0
        while not self._stop_event.is_set():
            self._reload_config()
            if self.paused:
                self._persist_status()
                try:
                    await asyncio.wait_for(
                        self._wake_event.wait(),
                        timeout=self.manager.config.web.config_poll_seconds,
                    )
                except TimeoutError:
                    pass
                self._wake_event.clear()
                continue
            now = time.monotonic()
            if now < next_scan_at:
                timeout = min(
                    self.manager.config.web.config_poll_seconds,
                    max(0.0, next_scan_at - now),
                )
                try:
                    await asyncio.wait_for(self._wake_event.wait(), timeout=timeout)
                except TimeoutError:
                    pass
                if self._wake_event.is_set():
                    # 配置保存和手动扫描都要求尽快执行下一周期。
                    next_scan_at = 0.0
                self._wake_event.clear()
                continue
            self.running_cycle = True
            self.last_started_at = time.time()
            self.last_error = None
            self._persist_status()
            try:
                summary: CycleSummary = await self._orchestrator.run_once()
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
            cutoff = time.time() - self.manager.config.web.log_retention_days * 86400
            await asyncio.to_thread(self.store.prune_run_logs, cutoff)
            next_scan_at = time.monotonic() + self.manager.config.scanner.interval_seconds
