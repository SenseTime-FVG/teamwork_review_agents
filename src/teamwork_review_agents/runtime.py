"""后台扫描循环、热加载和服务状态管理。"""

from __future__ import annotations

import asyncio
import logging
import shutil
import time
from collections.abc import Awaitable, Callable
from contextlib import contextmanager
from typing import Any

from .config import ScheduledRuleConfig
from .curl_runtime import CurlRuntimeManager
from .repository_migration import has_pending_repository_migration
from .config_manager import ConfigManager
from .orchestrator import CycleSummary, Orchestrator
from .scheduler import next_scheduled_at, schedule_signature, schedule_summary
from .workspace_cleanup import WorkspaceCleanupManager


LOGGER = logging.getLogger(__name__)
WORKER_LABELS = {"scanner": "仓库扫描", "dispatcher": "Agent 调度", "scheduler": "定时调度"}
RETRY_INITIAL_SECONDS = 5.0
RETRY_MAX_SECONDS = 60.0


class BackgroundRuntime:
    """在 FastAPI 生命周期内持续运行扫描和 Agent 调度。"""

    def __init__(self, manager: ConfigManager) -> None:
        self.manager = manager
        self.curl_runtime = CurlRuntimeManager(lambda: manager.config)
        self.store = manager.store
        self.store.recover_interrupted_work()
        self.workspace_cleanup = WorkspaceCleanupManager(lambda: manager.config, self.store)
        self.workspace_cleanup.maintenance_check = lambda: self.repository_migrating
        self._stop_event = asyncio.Event()
        self._scan_wake_event = asyncio.Event()
        self._dispatch_event = asyncio.Event()
        self._schedule_wake_event = asyncio.Event()
        self._scan_task: asyncio.Task[None] | None = None
        self._dispatch_task: asyncio.Task[None] | None = None
        self._schedule_task: asyncio.Task[None] | None = None
        self._worker_failures: dict[str, dict[str, Any]] = {}
        self.status_error: str | None = None
        self._status_read_error: str | None = None
        self._scheduled_occurrence_tasks: set[asyncio.Task[None]] = set()
        self._schedule_deadlines: dict[str, tuple[str, float]] = {}
        self._orchestrator = Orchestrator(
            manager.config,
            recover_interrupted=False,
        )
        self._active_orchestrators: set[Orchestrator] = set()
        self._revision = manager.config.revision
        self.repository_migrating = False
        self._repository_ready = asyncio.Event()
        self._repository_ready.set()
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
        self.last_schedule_started_at: float | None = None
        self.last_schedule_finished_at: float | None = None
        self.last_schedule_summary: dict[str, Any] | None = None
        self.last_schedule_error: str | None = None

    async def start(self) -> None:
        """启动扫描、Agent 调度与独立维护循环。"""

        self.curl_runtime.start()
        self.workspace_cleanup.start()
        if self._scan_task is None or self._scan_task.done():
            self._scan_task = asyncio.create_task(
                self._supervise_loop("scanner", self._scan_loop),
                name="teamwork-background-scanner",
            )
        if self._dispatch_task is None or self._dispatch_task.done():
            self._dispatch_task = asyncio.create_task(
                self._supervise_loop("dispatcher", self._dispatch_loop),
                name="teamwork-background-dispatcher",
            )
        if self._schedule_task is None or self._schedule_task.done():
            self._schedule_task = asyncio.create_task(
                self._supervise_loop("scheduler", self._schedule_loop),
                name="teamwork-background-scheduler",
            )

    async def stop(self) -> None:
        """停止新工作、取消活动 Agent，并等待全部子进程安全收尾。"""

        self._stop_event.set()
        await self.workspace_cleanup.close()
        await self.curl_runtime.close()
        self._scan_wake_event.set()
        self._dispatch_event.set()
        self._schedule_wake_event.set()
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
            for task in (self._scan_task, self._dispatch_task, self._schedule_task)
            if task is not None
        )
        tasks = (*tasks, *tuple(self._scheduled_occurrence_tasks))
        if tasks:
            # 已异常退出的任务不能阻断其余任务与子进程的关闭。
            await asyncio.gather(*tasks, return_exceptions=True)
        self._scan_task = None
        self._dispatch_task = None
        self._schedule_task = None
        self._scheduled_occurrence_tasks.clear()

    def _failure_message(self, exc: BaseException) -> str:
        """保留异常类型及 SQLite 诊断，剩余空间仅代表记录异常时的状态。"""

        message = f"{type(exc).__name__}: {exc}"
        code = getattr(exc, "sqlite_errorcode", None)
        if code is not None:
            directory = self.store.path.parent
            details = [
                f"SQLite {getattr(exc, 'sqlite_errorname', 'UNKNOWN')}（{code}）",
                f"数据库目录：{directory}",
                f"目录存在：{directory.is_dir()}",
            ]
            try:
                details.append(f"记录时剩余空间：{shutil.disk_usage(directory).free / 1024**3:.2f} GiB")
            except OSError:
                details.append("记录时无法读取剩余空间")
            message += "；" + "；".join(details)
        return message

    def _record_worker_failure(self, worker: str, exc: BaseException, retry_at: float | None) -> None:
        """先在内存记录故障，数据库不可用时页面和日志仍能看到错误。"""

        previous = self._worker_failures.get(worker, {})
        message = self._failure_message(exc)
        self._worker_failures[worker] = {
            "error": message,
            "failed_at": time.time(),
            "retry_at": retry_at,
            "consecutive_failures": previous.get("consecutive_failures", 0) + 1,
        }
        if worker == "scanner":
            self.running_cycle = False
            self.last_error = message
        elif worker == "dispatcher":
            self.dispatching_events = False
            self.last_dispatch_error = message
        else:
            self.last_schedule_error = message
        LOGGER.error("%s后台任务异常：%s", WORKER_LABELS[worker], message, exc_info=exc)

    async def _supervise_loop(self, worker: str, run: Callable[[], Awaitable[None]]) -> None:
        """监督长期循环，以有上限的退避恢复异常退出，停止请求不触发重启。"""

        while not self._stop_event.is_set():
            if worker in self._worker_failures:
                self._worker_failures[worker]["retry_at"] = None
            try:
                await run()
                if self._stop_event.is_set():
                    return
                raise RuntimeError("后台循环意外退出")
            except asyncio.CancelledError as exc:
                if not self._stop_event.is_set():
                    self._record_worker_failure(worker, exc, None)
                raise
            except Exception as exc:
                failures = self._worker_failures.get(worker, {}).get("consecutive_failures", 0)
                delay = min(RETRY_MAX_SECONDS, RETRY_INITIAL_SECONDS * 2 ** min(failures, 8))
                self._record_worker_failure(worker, exc, time.time() + delay)
                if worker == "dispatcher":
                    # 已领取事件仍由持久化幂等与重试次数约束，恢复时重新检查队列。
                    self._dispatch_event.set()
                self._persist_status_safely()
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=delay)
                except TimeoutError:
                    pass

    def _background_task_status(self) -> dict[str, dict[str, Any]]:
        """将实际任务存活状态与最近故障一起交给 Dashboard。"""

        result = {}
        for worker, task in (
            ("scanner", self._scan_task),
            ("dispatcher", self._dispatch_task),
            ("scheduler", self._schedule_task),
        ):
            failure = dict(self._worker_failures.get(worker, {}))
            state = "not_started" if task is None else "running"
            if task is not None and task.done():
                state = "stopped"
                if not failure.get("error"):
                    exc = None if task.cancelled() else task.exception()
                    failure["error"] = str(exc) if exc else "后台任务已停止"
            elif failure:
                state = "retrying" if failure.get("retry_at") is not None else "recovering"
            result[worker] = {"state": state, **failure}
        return result

    def _clear_worker_failure(self, worker: str) -> None:
        """只有实际工作恢复成功才清除故障，保留独立定时周期的执行错误。"""

        failure = self._worker_failures.pop(worker, None)
        if worker == "scheduler" and failure and self.last_schedule_error == failure["error"]:
            self.last_schedule_error = None

    def notify_config_changed(self) -> None:
        """唤醒后台，让下一周期应用最新配置。"""

        self._scan_wake_event.set()
        self._schedule_wake_event.set()
        self.workspace_cleanup.notify_config_changed()

    def scan_now(self) -> None:
        """请求尽快执行一次扫描，不与正在运行的周期重叠。"""

        self._scan_wake_event.set()

    @contextmanager
    def repository_maintenance(self):
        """空闲时封闭后台入口，直到配置和目录迁移完成。"""

        if (
            self.repository_migrating
            or self.workspace_cleanup.running
            or self.running_cycle
            or self.dispatching_events
            or self._active_orchestrators
            or any(not task.done() for task in self._scheduled_occurrence_tasks)
        ):
            raise ValueError("后台正在扫描、执行任务或清理工作区，请等待当前工作完成后再迁移仓库")
        self.repository_migrating = True
        self._repository_ready.clear()
        try:
            yield
        finally:
            # 恢复分发前更换编排器，避免旧配置继续写入旧仓库身份。
            if has_pending_repository_migration(self.manager.path):
                self.last_error = "仓库迁移尚未恢复，后台已暂停，请重启服务完成恢复"
            else:
                self._reload_config()
                self.repository_migrating = False
                self._repository_ready.set()
                self.notify_config_changed()
                self._dispatch_event.set()

    def dispatch_events_now(self) -> None:
        """请求尽快只处理待处理事件，不额外访问 Provider。"""

        self._dispatch_event.set()

    def pause(self) -> None:
        """暂停新扫描，不中断正在运行的 Agent。"""

        self.paused = True
        self._schedule_deadlines.clear()
        self._schedule_wake_event.set()
        self._persist_status()

    def resume(self) -> None:
        """恢复扫描并立即唤醒后台。"""

        self.paused = False
        self._scan_wake_event.set()
        self._schedule_wake_event.set()
        self._persist_status()

    def _reload_config(self) -> None:
        """应用手工编辑或 UI 保存后的新配置。"""

        self.manager.reload_if_changed()
        config = self.manager.config
        if config.revision != self._revision:
            self.curl_runtime.start()
            self._orchestrator = Orchestrator(
                config,
                recover_interrupted=False,
            )
            self._revision = config.revision
            self.workspace_cleanup.notify_config_changed()

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
                "last_schedule_started_at": self.last_schedule_started_at,
                "last_schedule_finished_at": self.last_schedule_finished_at,
                "last_schedule_summary": self.last_schedule_summary,
                "last_schedule_error": self.last_schedule_error,
                "status_error": self.status_error,
                "background_tasks": self._background_task_status(),
            },
        )

    def _persist_status_safely(self) -> None:
        """收尾和监督路径不能因保存状态失败再次退出或覆盖原始异常。"""

        try:
            self._persist_status()
        except Exception as exc:
            message = self._failure_message(exc)
            if message != self.status_error:
                LOGGER.error("后台状态保存失败：%s", message, exc_info=exc)
            self.status_error = message
        else:
            self.status_error = None

    async def snapshot(self, repository_id: str | None = None) -> dict[str, Any]:
        """组合全局服务状态与可选仓库范围的统计，筛选不改变调度范围。"""

        stats = {"runs": {}, "events": {}, "change_requests": {}}
        repository_scan = None
        read_error = None
        try:
            stats = await asyncio.to_thread(self.store.dashboard_stats, repository_id)
            if repository_id:
                repository_scan = await asyncio.to_thread(
                    self.store.get_service_state,
                    Orchestrator._scan_state_key(repository_id),
                )
        except Exception as exc:
            # 健康信息来自内存；数据库损坏时也应返回告警，而非只返回 HTTP 500。
            read_error = self._failure_message(exc)
            if read_error != self._status_read_error:
                LOGGER.error("概览统计读取失败：%s", read_error, exc_info=exc)
        self._status_read_error = read_error
        background_tasks = self._background_task_status()
        return {
            "repository_id": repository_id or None,
            "repository_last_scan_completed_at": (repository_scan or {}).get("completed_at"),
            "paused": self.paused,
            "running_cycle": self.running_cycle and background_tasks["scanner"]["state"] != "stopped",
            "dispatching_events": self.dispatching_events and background_tasks["dispatcher"]["state"] != "stopped",
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
            "last_schedule_started_at": self.last_schedule_started_at,
            "last_schedule_finished_at": self.last_schedule_finished_at,
            "last_schedule_summary": self.last_schedule_summary,
            "last_schedule_error": self.last_schedule_error,
            "status_error": self.status_error or read_error,
            "stats_available": read_error is None,
            "background_tasks": background_tasks,
            "scheduled_rules": [
                {
                    "name": rule.name,
                    "enabled": rule.enabled,
                    "summary": schedule_summary(rule),
                    "timezone": rule.schedule.timezone,
                    "next_scheduled_at": (
                        self._schedule_deadlines.get(rule.name, ("", None))[1]
                        if rule.enabled
                        else None
                    ),
                }
                for rule in self.manager.config.scheduled_rules
            ],
            "stats": stats,
        }

    async def _run_scan_cycle(self) -> None:
        """执行一次扫描并持久化事件，不等待 Agent 执行。"""

        await self._repository_ready.wait()
        self.running_cycle = True
        self.last_started_at = time.time()
        try:
            self._persist_status()
            summary = CycleSummary()
            orchestrator = self._orchestrator
            self._active_orchestrators.add(orchestrator)
            try:
                await orchestrator.scan(summary)
            finally:
                self._active_orchestrators.discard(orchestrator)
            self.last_summary = summary.to_dict()
            self.last_error = "; ".join(summary.errors) if summary.errors else None
            if not summary.errors:
                self._clear_worker_failure("scanner")
        except Exception as exc:
            self.last_error = self._failure_message(exc)
            self.last_summary = None
            raise
        finally:
            self.running_cycle = False
            self.last_finished_at = time.time()
            self._persist_status_safely()
            # 即使单个仓库失败，其他仓库本轮已产生的事件仍需及时调度。
            self._dispatch_event.set()

    async def _run_dispatch_cycle(self) -> None:
        """独立领取并执行持久化事件，不占用扫描循环。"""

        await self.curl_runtime.wait()
        if self._stop_event.is_set():
            return
        await self._repository_ready.wait()
        self.dispatching_events = True
        self.last_dispatch_started_at = time.time()
        try:
            self._persist_status()
            summary = CycleSummary()
            orchestrator = self._orchestrator
            self._active_orchestrators.add(orchestrator)
            try:
                await orchestrator.process_events(summary)
            finally:
                self._active_orchestrators.discard(orchestrator)
            self.last_dispatch_summary = summary.to_dict()
            self.last_dispatch_error = "; ".join(summary.errors) if summary.errors else None
        except Exception as exc:
            self.last_dispatch_error = self._failure_message(exc)
            self.last_dispatch_summary = None
            raise
        finally:
            self.dispatching_events = False
            self.last_dispatch_finished_at = time.time()
            self._persist_status_safely()
        cutoff = time.time() - self.manager.config.web.log_retention_days * 86400
        await asyncio.to_thread(self.store.prune_run_logs, cutoff)
        await asyncio.to_thread(
            self.store.prune_terminal_target_events,
            cutoff,
            max_attempts=self.manager.config.runtime.event_retry_count + 1,
        )
        if not self.last_dispatch_error:
            self._clear_worker_failure("dispatcher")

    async def _scan_loop(self) -> None:
        """按固定间隔扫描 Provider，不等待事件执行循环。"""

        next_scan_at = 0.0
        while not self._stop_event.is_set():
            await self._repository_ready.wait()
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

    async def _run_scheduled_occurrence(
        self,
        orchestrator: Orchestrator,
        rule: ScheduledRuleConfig,
        scheduled_at: float,
        signature: str,
    ) -> None:
        """执行一个已到期周期，不阻塞后续周期的创建。"""

        await self.curl_runtime.wait()
        if self._stop_event.is_set():
            await orchestrator.request_shutdown()
            return
        self.last_schedule_started_at = time.time()
        self._active_orchestrators.add(orchestrator)
        try:
            self._persist_status()
            summary = await orchestrator.run_scheduled_rule(
                rule,
                scheduled_at=scheduled_at,
                schedule_signature=signature,
            )
            self.last_schedule_summary = summary.to_dict()
            self.last_schedule_error = "; ".join(summary.errors) if summary.errors else None
        except Exception as exc:
            self.last_schedule_error = self._failure_message(exc)
            self.last_schedule_summary = None
            LOGGER.error("定时周期执行失败：%s", self.last_schedule_error, exc_info=exc)
        finally:
            self._active_orchestrators.discard(orchestrator)
            self.last_schedule_finished_at = time.time()
            self._persist_status_safely()

    def _track_scheduled_occurrence(self, task: asyncio.Task[None]) -> None:
        """保存并自动清理仍在执行的定时周期任务引用。"""

        self._scheduled_occurrence_tasks.add(task)
        task.add_done_callback(self._scheduled_occurrence_tasks.discard)

    async def _schedule_loop(self) -> None:
        """计算固定间隔与 Cron 到期点，并为每个周期独立创建任务。"""

        while not self._stop_event.is_set():
            await self._repository_ready.wait()
            self._reload_config()
            if self._stop_event.is_set():
                break
            if self.paused:
                self._schedule_deadlines.clear()
                try:
                    await asyncio.wait_for(
                        self._schedule_wake_event.wait(),
                        timeout=self.manager.config.web.config_poll_seconds,
                    )
                except TimeoutError:
                    pass
                self._schedule_wake_event.clear()
                continue

            rules = {
                rule.name: rule
                for rule in self.manager.config.scheduled_rules
                if rule.enabled
            }
            now = time.time()
            for name in tuple(self._schedule_deadlines):
                if name not in rules:
                    self._schedule_deadlines.pop(name, None)
            for name, rule in rules.items():
                signature = schedule_signature(rule)
                current = self._schedule_deadlines.get(name)
                if current is None or current[0] != signature:
                    self._schedule_deadlines[name] = (
                        signature,
                        next_scheduled_at(rule, now),
                    )

            for name, rule in rules.items():
                signature, deadline = self._schedule_deadlines[name]
                while deadline <= now and not self._stop_event.is_set():
                    # 每个周期持有独立编排器与配置快照，避免上一周期或热加载
                    # 改变下一周期的执行上下文；并发额度和写锁仍由 SQLite 统一约束。
                    orchestrator = Orchestrator(
                        self.manager.config,
                        recover_interrupted=False,
                    )
                    task = asyncio.create_task(
                        self._run_scheduled_occurrence(
                            orchestrator,
                            rule,
                            deadline,
                            signature,
                        ),
                        name=f"scheduled-occurrence-{name}",
                    )
                    self._track_scheduled_occurrence(task)
                    deadline = next_scheduled_at(rule, deadline)
                    self._schedule_deadlines[name] = (signature, deadline)

            deadlines = [item[1] for item in self._schedule_deadlines.values()]
            self._clear_worker_failure("scheduler")
            timeout = self.manager.config.web.config_poll_seconds
            if deadlines:
                timeout = min(timeout, max(0.05, min(deadlines) - time.time()))
            try:
                await asyncio.wait_for(
                    self._schedule_wake_event.wait(),
                    timeout=timeout,
                )
            except TimeoutError:
                pass
            self._schedule_wake_event.clear()
