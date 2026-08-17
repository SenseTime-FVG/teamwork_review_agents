"""扫描、事件规则和 Agent 执行的顶层编排。"""

from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from .config import AppConfig, RepositoryConfig, RuleConfig
from .environment import resolve_provider_token
from .events import detect_activity_events, detect_events
from .executor import AgentExecutor
from .models import AgentResult, ChangeEvent, stable_hash
from .providers import BaseProvider, ProviderError, create_provider
from .rules import rule_matches
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


@dataclass(frozen=True)
class RuleInvocation:
    """一条规则在一个扫描批次中计划出的 Agent 调用。"""

    rule: RuleConfig
    agent_name: str
    events: tuple[ChangeEvent, ...]

    @property
    def actions(self) -> tuple[str, ...]:
        """按事件产生顺序返回去重后的动作列表。"""

        return tuple(dict.fromkeys(event.type for event in self.events))

    @property
    def idempotency_key(self) -> str:
        """返回当前规则、Agent 与匹配事件组合的稳定调度键。"""

        event_ids = tuple(event.id for event in self.events)
        return stable_hash(
            event_ids[0] if len(event_ids) == 1 else event_ids,
            self.rule.name,
            self.agent_name,
        )


def plan_rule_invocations(
    rules: list[RuleConfig],
    events: list[ChangeEvent],
) -> list[RuleInvocation]:
    """按规则配置将同批次事件规划为一次或多次 Agent 调用。"""

    invocations: list[RuleInvocation] = []
    for rule in rules:
        matched = [event for event in events if rule_matches(rule, event)]
        if not matched:
            continue
        for agent_name in dict.fromkeys(rule.agents):
            if rule.deduplicate_per_scan:
                invocations.append(RuleInvocation(rule, agent_name, tuple(matched)))
            else:
                invocations.extend(
                    RuleInvocation(rule, agent_name, (event,)) for event in matched
                )
    return invocations


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

    @staticmethod
    def _scan_state_key(repository_id: str) -> str:
        """返回仓库扫描时间水位的持久化键。"""

        return f"repository_scan:{repository_id}"

    def _updated_since(self, repository_id: str) -> datetime | None:
        """读取上次成功扫描时间，并保留两个扫描周期的重叠窗口。"""

        state = self.store.get_service_state(self._scan_state_key(repository_id))
        if not state or not state.get("completed_at"):
            return None
        try:
            completed_at = datetime.fromisoformat(str(state["completed_at"]))
        except ValueError:
            return None
        if completed_at.tzinfo is None:
            completed_at = completed_at.replace(tzinfo=UTC)
        overlap_seconds = max(60, self.config.scanner.interval_seconds * 2)
        return completed_at - timedelta(seconds=overlap_seconds)

    def _mark_scan_completed(self, repository_id: str) -> None:
        """只在仓库完整扫描成功后推进时间水位。"""

        self.store.set_service_state(
            self._scan_state_key(repository_id),
            {"completed_at": datetime.now(UTC).isoformat()},
        )

    async def _initialize_activity_cursors(
        self,
        provider: BaseProvider,
        repository: RepositoryConfig,
    ) -> None:
        """在候选筛选前为已有快照补齐活动基线，避免首次变化被吞掉。"""

        while True:
            snapshots = await asyncio.to_thread(
                self.store.snapshots_without_activity_cursor,
                provider.name,
                repository.id,
                limit=self.config.scanner.max_items_per_repository,
            )
            if not snapshots:
                return
            for snapshot in snapshots:
                activity_batch = await provider.list_change_request_activities(
                    repository,
                    snapshot.number,
                    cursor=None,
                )
                if activity_batch is None:
                    return
                await asyncio.to_thread(
                    self.store.save_activity_cursor,
                    snapshot.provider,
                    snapshot.repository_id,
                    snapshot.number,
                    activity_batch.cursor,
                )
            if len(snapshots) < self.config.scanner.max_items_per_repository:
                return

    async def scan(self, summary: CycleSummary) -> None:
        """按 Provider 分组扫描所有启用仓库。"""

        scan_batch_id = uuid.uuid4().hex
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
                    token=resolve_provider_token(self.config, provider_config),
                ) as provider:
                    for repository in repositories:
                        summary.repositories += 1
                        try:
                            await self._initialize_activity_cursors(
                                provider,
                                repository,
                            )
                            updated_since = await asyncio.to_thread(
                                self._updated_since,
                                repository.id,
                            )
                            snapshots = await provider.list_change_requests(
                                repository,
                                updated_since=updated_since,
                            )
                            for snapshot in snapshots:
                                old = await asyncio.to_thread(
                                    self.store.load_snapshot,
                                    snapshot.key,
                                )
                                activity_cursor = await asyncio.to_thread(
                                    self.store.load_activity_cursor,
                                    snapshot.provider,
                                    snapshot.repository_id,
                                    snapshot.number,
                                )
                                activity_batch = await provider.list_change_request_activities(
                                    repository,
                                    snapshot.number,
                                    cursor=activity_cursor,
                                )
                                if (
                                    old is not None
                                    and activity_batch is not None
                                    and not activity_batch.baseline
                                ):
                                    events = detect_activity_events(
                                        old,
                                        snapshot,
                                        activity_batch.activities,
                                        batch_id=scan_batch_id,
                                    )
                                else:
                                    events = detect_events(
                                        old,
                                        snapshot,
                                        emit_initial=self.config.scanner.emit_initial_events,
                                        batch_id=scan_batch_id,
                                    )
                                inserted = await asyncio.to_thread(
                                    self.store.save_snapshot_and_events,
                                    snapshot,
                                    events,
                                    activity_cursor=(
                                        activity_batch.cursor
                                        if activity_batch is not None
                                        else None
                                    ),
                                )
                                summary.snapshots += 1
                                summary.new_events += inserted
                            await asyncio.to_thread(
                                self._mark_scan_completed,
                                repository.id,
                            )
                        except Exception as exc:
                            summary.errors.append(f"扫描仓库 {repository.id} 失败：{exc}")
            except ProviderError as exc:
                summary.errors.append(str(exc))

    async def _run_agent(
        self,
        invocation: RuleInvocation,
    ) -> AgentResult | None:
        """在全局并发额度内执行一条规则产生的 Agent 任务。"""

        event = invocation.events[0]
        async with self.agent_semaphore:
            return await self.executor.execute(
                agent_name=invocation.agent_name,
                event=event,
                actions=invocation.actions,
                rule_name=invocation.rule.name,
                inherit_workspace=invocation.rule.inherit_workspace,
                idempotency_key=invocation.idempotency_key,
            )

    async def process_events(self, summary: CycleSummary) -> None:
        """领取事件、匹配规则并等待所有目标 Agent 完成。"""

        events = await asyncio.to_thread(self.store.pending_events)
        max_attempts = self.config.runtime.event_retry_count + 1
        batches: dict[tuple[str, int, str], list[ChangeEvent]] = {}
        for event in events:
            batch_key = (
                event.repository_id,
                event.number,
                event.batch_id or event.current_snapshot.updated_at.isoformat(),
            )
            batches.setdefault(batch_key, []).append(event)

        for batch_events in batches.values():
            claimed_events: list[ChangeEvent] = []
            for event in batch_events:
                claimed = await asyncio.to_thread(
                    self.store.claim_event,
                    event.id,
                    max_attempts,
                )
                if claimed:
                    claimed_events.append(event)
            if not claimed_events:
                continue

            repository = self.config.repository_map().get(claimed_events[0].repository_id)
            if repository is None or not repository.enabled:
                for event in claimed_events:
                    await asyncio.to_thread(
                        self.store.finish_event,
                        event.id,
                        status="unmatched",
                    )
                    summary.processed_events += 1
                continue

            errors_by_event: dict[str, list[str]] = {
                event.id: [] for event in claimed_events
            }
            matched_event_ids: set[str] = set()
            try:
                invocations = plan_rule_invocations(self.config.rules, claimed_events)
                dispatches = [
                    (
                        event.id,
                        invocation.idempotency_key,
                        invocation.rule.name,
                        invocation.agent_name,
                    )
                    for invocation in invocations
                    for event in invocation.events
                ]
                matched_event_ids = {item[0] for item in dispatches}
                await asyncio.to_thread(
                    self.store.record_event_dispatches,
                    tuple(event.id for event in claimed_events),
                    dispatches,
                )
                tasks = [asyncio.create_task(self._run_agent(item)) for item in invocations]
                if tasks:
                    results = await asyncio.gather(*tasks, return_exceptions=True)
                    summary.agent_runs += sum(
                        isinstance(result, AgentResult) for result in results
                    )
                    for invocation, result in zip(invocations, results, strict=True):
                        if isinstance(result, BaseException):
                            error = str(result)
                        elif isinstance(result, AgentResult) and result.status != "completed":
                            error = result.error or f"Agent 运行状态为 {result.status}"
                        else:
                            continue
                        for event in invocation.events:
                            errors_by_event[event.id].append(error)
            except Exception as exc:
                for event in claimed_events:
                    errors_by_event[event.id].append(str(exc))

            for event in claimed_events:
                error = "; ".join(errors_by_event[event.id]) or None
                if event.id not in matched_event_ids and error is None:
                    summary.processed_events += 1
                    continue
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
