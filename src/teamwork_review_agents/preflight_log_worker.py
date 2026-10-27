"""独立消费持久化日志附件队列，上传和补链接不占用 CI 门禁流程。"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Callable

from .config import AppConfig
from .environment import SecretRedactor, resolve_provider_token, resolve_repository_process_environment
from .locks import LockTimeoutError, ResourceLease
from .preflight import PreflightExecutor
from .state import StateStore


MAX_DELIVERY_ATTEMPTS = 3


class PreflightLogWorker:
    """限制并发与重试，进程中断后的任务由数据库队列和租约恢复。"""

    def __init__(self, config_getter: Callable[[], AppConfig], store: StateStore) -> None:
        self.config_getter = config_getter
        self.store = store
        self.tasks: dict[tuple[str, int], asyncio.Task[None]] = {}

    @property
    def running(self) -> bool:
        """仓库迁移期间不能改变活动上传任务的仓库身份。"""

        return any(not task.done() for task in self.tasks.values())

    async def deliver(self, job: dict) -> None:
        """已上传附件的链接发布失败时只重发链接，不重新上传文件。"""

        run_id, ci_attempt = job["run_id"], job["ci_attempt"]
        try:
            async with ResourceLease(
                self.store, [f"ci-log-delivery:{run_id}:{ci_attempt}"],
                f"ci-log-delivery:{uuid.uuid4()}", ttl_seconds=60, timeout_seconds=0,
            ):
                current = await asyncio.to_thread(self.store.get_preflight_log_delivery, run_id)
                if not current or current["ci_attempt"] != ci_attempt or current["status"] not in {"queued", "running"}:
                    return
                if current["attempts"] >= MAX_DELIVERY_ATTEMPTS:
                    await asyncio.to_thread(
                        self.store.update_preflight_log_delivery, run_id, ci_attempt,
                        status="failed", attempts=current["attempts"], error="附件发布已达到重试上限",
                    )
                    return
                attempts = current["attempts"] + 1
                await asyncio.to_thread(
                    self.store.update_preflight_log_delivery, run_id, ci_attempt,
                    status="running", attempts=attempts,
                )
                config = self.config_getter()
                try:
                    status = await PreflightExecutor(config, self.store).deliver_log_attachment(run_id, ci_attempt)
                    await asyncio.to_thread(
                        self.store.update_preflight_log_delivery, run_id, ci_attempt,
                        status=status, attempts=attempts,
                        error="日志附件上传失败，详情见上传记录" if status == "failed" else None,
                    )
                except Exception as exc:
                    repository = next(repo for repo in config.repositories if repo.id == job["repository_id"])
                    environment = resolve_repository_process_environment(config, repository, run_id)
                    token = resolve_provider_token(config, config.providers[repository.provider], repository)
                    error = SecretRedactor((*environment.secret_values, token)).text(str(exc) or type(exc).__name__)
                    await asyncio.to_thread(
                        self.store.update_preflight_log_delivery, run_id, ci_attempt,
                        status="queued" if attempts < MAX_DELIVERY_ATTEMPTS else "failed",
                        attempts=attempts, error=error, next_attempt_at=time.time() + 5 * attempts,
                    )
                    await asyncio.to_thread(
                        self.store.append_preflight_log, run_id, stream="stderr",
                        event_type="log_upload_delivery_error", payload=f"日志附件链接发布失败（不影响 CI 结果）：{error}",
                    )
        except LockTimeoutError:
            # 其他进程正在处理同一任务，等待后续队列扫描即可。
            return

    async def run(
        self, stop_event: asyncio.Event, maintenance_check: Callable[[], bool],
        on_healthy: Callable[[], None],
    ) -> None:
        """最多两路上传；关闭服务时取消网络等待，持久化待办留给下次启动。"""

        try:
            while not stop_event.is_set():
                for key, task in list(self.tasks.items()):
                    if task.done():
                        del self.tasks[key]
                        task.result()
                if not maintenance_check():
                    jobs = await asyncio.to_thread(self.store.pending_preflight_log_deliveries)
                    # 查询期间可能进入仓库维护，启动任务前再次核对。
                    if not maintenance_check():
                        for job in jobs:
                            key = (job["run_id"], job["ci_attempt"])
                            if key not in self.tasks and len(self.tasks) < 2:
                                self.tasks[key] = asyncio.create_task(self.deliver(job))
                    on_healthy()
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=0.5)
                except TimeoutError:
                    pass
        finally:
            for task in self.tasks.values():
                task.cancel()
            await asyncio.gather(*self.tasks.values(), return_exceptions=True)
            self.tasks.clear()

    async def drain(self) -> None:
        """单次 CLI 运行退出前消费当前待办，退避中的重试保留到下一周期。"""

        for job in await asyncio.to_thread(self.store.pending_preflight_log_deliveries):
            await self.deliver(job)
