"""扫描、事件规则和 Agent 执行的顶层编排。"""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict, dataclass, field
from typing import Any

from .config import AppConfig
from .events import detect_events
from .executor import AgentExecutor
from .models import ChangeEvent, stable_hash
from .providers import ProviderError, create_provider
from .rules import matching_rules
from .state import StateStore


@dataclass
class CycleSummary:
    """一次扫描和处理周期的统计结果。"""

    repositories: int = 0
    snapshots: int = 0
    new_events: int = 0
    processed_events: int = 0
    agent_runs: int = 0
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class Orchestrator:
    """单服务编排器；跨进程一致性由 SQLite 负责。"""

    def __init__(self, config: AppConfig, *, recover_interrupted: bool = True) -> None:
        self.config = config
        self.store = StateStore(config.database.path)
        self.store.initialize()
        if recover_interrupted:
            self.store.recover_interrupted_work()
        self.executor = AgentExecutor(config, self.store)
        self.agent_semaphore = asyncio.Semaphore(config.runtime.max_concurrent_agents)

    async def scan(self, summary: CycleSummary) -> None:
        """按 Provider 分组扫描所有启用仓库。"""

        enabled = [repository for repository in self.config.repositories if repository.enabled]
        for provider_name, provider_config in self.config.providers.items():
            repositories = [
                repository for repository in enabled if repository.provider == provider_name
            ]
            if not repositories:
                continue
            try:
                async with create_provider(
                    provider_name,
                    provider_config,
                    self.config.scanner,
                ) as provider:
                    for repository in repositories:
                        summary.repositories += 1
                        try:
                            snapshots = await provider.list_change_requests(repository)
                            for snapshot in snapshots:
                                old = await asyncio.to_thread(
                                    self.store.load_snapshot,
                                    snapshot.key,
                                )
                                events = detect_events(
                                    old,
                                    snapshot,
                                    emit_initial=self.config.scanner.emit_initial_events,
                                )
                                inserted = await asyncio.to_thread(
                                    self.store.save_snapshot_and_events,
                                    snapshot,
                                    events,
                                )
                                summary.snapshots += 1
                                summary.new_events += inserted
                        except Exception as exc:
                            summary.errors.append(f"扫描仓库 {repository.id} 失败：{exc}")
            except ProviderError as exc:
                summary.errors.append(str(exc))

    async def _run_agent(
        self,
        event: ChangeEvent,
        rule_name: str,
        agent_name: str,
    ) -> bool:
        """在全局并发额度内执行一条规则产生的 Agent 任务。"""

        async with self.agent_semaphore:
            result = await self.executor.execute(
                agent_name=agent_name,
                event=event,
                rule_name=rule_name,
                idempotency_key=stable_hash(event.id, rule_name, agent_name),
            )
        return result is not None

    async def process_events(self, summary: CycleSummary) -> None:
        """领取事件、匹配规则并等待所有目标 Agent 完成。"""

        events = await asyncio.to_thread(self.store.pending_events)
        max_attempts = self.config.runtime.event_retry_count + 1
        for event in events:
            claimed = await asyncio.to_thread(
                self.store.claim_event,
                event.id,
                max_attempts,
            )
            if not claimed:
                continue
            repository = self.config.repository_map().get(event.repository_id)
            if repository is None or not repository.enabled:
                await asyncio.to_thread(self.store.finish_event, event.id)
                summary.processed_events += 1
                continue
            error: str | None = None
            try:
                rules = matching_rules(self.config.rules, event)
                tasks = [
                    asyncio.create_task(self._run_agent(event, rule.name, agent_name))
                    for rule in rules
                    for agent_name in rule.agents
                ]
                if tasks:
                    results = await asyncio.gather(*tasks, return_exceptions=True)
                    summary.agent_runs += sum(result is True for result in results)
                    failures = [
                        str(result)
                        for result in results
                        if isinstance(result, BaseException)
                    ]
                    if failures:
                        error = "; ".join(failures)
            except Exception as exc:
                error = str(exc)
            if error:
                summary.errors.append(f"处理事件 {event.id} 失败：{error}")
            await asyncio.to_thread(self.store.finish_event, event.id, error=error)
            summary.processed_events += 1

    async def run_once(self, *, dry_run: bool = False) -> CycleSummary:
        """执行一次完整扫描；dry-run 时保留事件但不启动 Agent。"""

        summary = CycleSummary()
        await self.scan(summary)
        if not dry_run:
            await self.process_events(summary)
        return summary

    async def serve(self, stop_event: asyncio.Event) -> None:
        """持续运行扫描周期，收到停止信号后退出。"""

        while not stop_event.is_set():
            summary = await self.run_once()
            print(json.dumps(summary.to_dict(), ensure_ascii=False), flush=True)
            try:
                await asyncio.wait_for(
                    stop_event.wait(),
                    timeout=self.config.scanner.interval_seconds,
                )
            except TimeoutError:
                continue
