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
from .events import (
    detect_activity_events,
    detect_events,
    detect_first_seen_events,
    detect_target_branch_event,
)
from .executor import AgentExecutor
from .models import (
    AgentResult,
    ChangeEvent,
    ChangeRequestActivityBatch,
    ChangeRequestSnapshot,
    stable_hash,
)
from .preflight import PreflightExecutor
from .providers import BaseProvider, ProviderError, create_provider
from .rules import rule_matches
from .state import (
    CANCEL_SOURCE_ADMINISTRATOR,
    CANCEL_SOURCE_SERVICE_SHUTDOWN,
    StateStore,
)


EVENT_RETRY_BACKOFF_SECONDS = 1.0


@dataclass
class CycleSummary:
    """一次扫描和处理周期的统计结果。"""

    repositories: int = 0
    snapshots: int = 0
    new_events: int = 0
    processed_events: int = 0
    preflight_runs: int = 0
    preflight_failures: int = 0
    preflight_errors: int = 0
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
        self.preflight = PreflightExecutor(config, self.store)
        self._shutdown_requested = False

    async def request_shutdown(self) -> list[str]:
        """停止创建新 Agent，并持久化取消当前服务的全部活动运行。"""

        self._shutdown_requested = True
        self.executor.begin_shutdown()
        run_ids = await asyncio.to_thread(self.store.request_cancel_active_runs)
        for run_id in run_ids:
            await asyncio.to_thread(
                self.store.append_run_log,
                run_id,
                stream="system",
                event_type="run.cancel_requested",
                payload={
                    "run_id": run_id,
                    "reason": "服务正在停止",
                    "source": "service_shutdown",
                },
            )
        return run_ids

    @staticmethod
    def _scan_state_key(repository_id: str) -> str:
        """返回仓库扫描时间水位的持久化键。"""

        return f"repository_scan:{repository_id}"

    @staticmethod
    def _activity_cursor(batch: ChangeRequestActivityBatch) -> dict[str, Any]:
        """把最新 Provider 活动与增量位置一起写入持久化游标。"""

        cursor = dict(batch.cursor)
        cursor["latest_activity_checked"] = True
        if batch.latest_activity is not None:
            cursor["latest_activity"] = batch.latest_activity.model_dump(mode="json")
        return cursor

    def _last_scan_completed_at(self, repository_id: str) -> datetime | None:
        """读取仓库上一次成功完成扫描的精确时间。"""

        state = self.store.get_service_state(self._scan_state_key(repository_id))
        if not state or not state.get("completed_at"):
            return None
        try:
            completed_at = datetime.fromisoformat(str(state["completed_at"]))
        except ValueError:
            return None
        if completed_at.tzinfo is None:
            completed_at = completed_at.replace(tzinfo=UTC)
        return completed_at

    def _updated_since(self, repository_id: str) -> datetime | None:
        """读取上次成功扫描时间，并保留两个扫描周期的重叠窗口。"""

        completed_at = self._last_scan_completed_at(repository_id)
        if completed_at is None:
            return None
        overlap_seconds = max(60, self.config.scanner.interval_seconds * 2)
        return completed_at - timedelta(seconds=overlap_seconds)

    def _last_successful_scan_started_at(self, repository_id: str) -> datetime | None:
        """读取上一次成功轮次的开始时间，旧数据回退到完成时间。"""

        state = self.store.get_service_state(self._scan_state_key(repository_id))
        if not state:
            return None
        value = state.get("started_at") or state.get("completed_at")
        if not value:
            return None
        try:
            started_at = datetime.fromisoformat(str(value))
        except ValueError:
            return None
        if started_at.tzinfo is None:
            started_at = started_at.replace(tzinfo=UTC)
        return started_at

    def _mark_scan_completed(
        self,
        repository_id: str,
        scan_started_at: datetime,
    ) -> None:
        """只在仓库完整扫描成功后推进时间水位。"""

        self.store.set_service_state(
            self._scan_state_key(repository_id),
            {
                "started_at": scan_started_at.isoformat(),
                "completed_at": datetime.now(UTC).isoformat(),
            },
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
                    self._activity_cursor(activity_batch),
                )
            if len(snapshots) < self.config.scanner.max_items_per_repository:
                return

    @staticmethod
    async def _target_branch_heads(
        provider: BaseProvider,
        repository: RepositoryConfig,
        snapshots: list[ChangeRequestSnapshot],
    ) -> dict[str, str]:
        """一次扫描只读取每个打开目标分支的一份真实 Head。"""

        resolver = getattr(provider, "get_branch_head", None)
        if not callable(resolver):
            # 兼容不实现新接口的测试替身与外部 Provider。
            return {}
        branches = sorted(
            {
                snapshot.target_branch
                for snapshot in snapshots
                if snapshot.state == "opened" and snapshot.target_branch
            }
        )
        heads = await asyncio.gather(
            *(resolver(repository, branch) for branch in branches)
        )
        return dict(zip(branches, heads, strict=True))

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
                            scan_started_at = datetime.now(UTC)
                            last_scan_started_at = await asyncio.to_thread(
                                self._last_successful_scan_started_at,
                                repository.id,
                            )
                            event_window_start = last_scan_started_at or (
                                scan_started_at
                                - timedelta(seconds=self.config.scanner.interval_seconds)
                            )
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
                            stored_snapshots = await asyncio.to_thread(
                                self.store.repository_snapshots,
                                provider.name,
                                repository.id,
                            )
                            stored_by_key = {
                                item.key: item for item in stored_snapshots
                            }
                            target_heads = await self._target_branch_heads(
                                provider,
                                repository,
                                [*stored_snapshots, *snapshots],
                            )
                            normalized_snapshots: list[ChangeRequestSnapshot] = []
                            for snapshot in snapshots:
                                old = stored_by_key.get(snapshot.key)
                                target_head_sha = target_heads.get(
                                    snapshot.target_branch
                                )
                                if target_head_sha:
                                    snapshot = snapshot.model_copy(
                                        update={"target_head_sha": target_head_sha}
                                    )
                                elif old is not None and old.target_head_sha:
                                    snapshot = snapshot.model_copy(
                                        update={
                                            "target_head_sha": old.target_head_sha
                                        }
                                    )
                                normalized_snapshots.append(snapshot)
                            snapshots = normalized_snapshots
                            candidate_keys = {item.key for item in snapshots}
                            for snapshot in snapshots:
                                old = stored_by_key.get(snapshot.key)
                                activity_cursor = await asyncio.to_thread(
                                    self.store.load_activity_cursor,
                                    snapshot.provider,
                                    snapshot.repository_id,
                                    snapshot.number,
                                )
                                if old is None:
                                    activity_batch = (
                                        await provider.list_change_request_activities(
                                            repository,
                                            snapshot.number,
                                            cursor=activity_cursor,
                                            since=event_window_start,
                                        )
                                    )
                                else:
                                    activity_batch = (
                                        await provider.list_change_request_activities(
                                            repository,
                                            snapshot.number,
                                            cursor=activity_cursor,
                                        )
                                    )
                                if old is None:
                                    events = detect_first_seen_events(
                                        snapshot,
                                        (
                                            activity_batch.activities
                                            if activity_batch is not None
                                            and not activity_batch.baseline
                                            else ()
                                        ),
                                        event_window_start=event_window_start,
                                        emit_discovered=(
                                            self.config.scanner.emit_initial_events
                                            or repository.preflight.enabled
                                        ),
                                        batch_id=scan_batch_id,
                                    )
                                elif (
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
                                events.extend(
                                    detect_target_branch_event(
                                        old,
                                        snapshot,
                                        batch_id=scan_batch_id,
                                        occurred_at=scan_started_at,
                                    )
                                )
                                inserted = await asyncio.to_thread(
                                    self.store.save_snapshot_and_events,
                                    snapshot,
                                    events,
                                    activity_cursor=(
                                        self._activity_cursor(activity_batch)
                                        if activity_batch is not None
                                        else None
                                    ),
                                )
                                summary.snapshots += 1
                                summary.new_events += inserted
                            for old in stored_snapshots:
                                if old.state != "opened" or old.key in candidate_keys:
                                    continue
                                target_head_sha = target_heads.get(old.target_branch)
                                if not target_head_sha or target_head_sha == old.target_head_sha:
                                    continue
                                current = old.model_copy(
                                    update={"target_head_sha": target_head_sha}
                                )
                                events = detect_target_branch_event(
                                    old,
                                    current,
                                    batch_id=scan_batch_id,
                                    occurred_at=scan_started_at,
                                )
                                inserted = await asyncio.to_thread(
                                    self.store.save_snapshot_and_events,
                                    current,
                                    events,
                                )
                                summary.snapshots += 1
                                summary.new_events += inserted
                            await asyncio.to_thread(
                                self._mark_scan_completed,
                                repository.id,
                                scan_started_at,
                            )
                        except Exception as exc:
                            summary.errors.append(f"扫描仓库 {repository.id} 失败：{exc}")
            except ProviderError as exc:
                summary.errors.append(str(exc))

    async def _run_agent(
        self,
        invocation: RuleInvocation,
    ) -> AgentResult | None:
        """执行一条规则产生的 Agent 任务，额度由执行器持久化申请。"""

        event = invocation.events[0]
        return await self.executor.execute(
            agent_name=invocation.agent_name,
            event=event,
            actions=invocation.actions,
            rule_name=invocation.rule.name,
            inherit_workspace=invocation.rule.inherit_workspace,
            idempotency_key=invocation.idempotency_key,
        )

    async def _process_resource_events(
        self,
        summary: CycleSummary,
        resource: tuple[str, int],
    ) -> str | None:
        """按批次顺序处理单个 PR / MR 当前已经入队的事件。"""

        events = await asyncio.to_thread(
            self.store.pending_events_for_resource,
            resource[0],
            resource[1],
            max_attempts=self.config.runtime.event_retry_count + 1,
        )
        max_attempts = self.config.runtime.event_retry_count + 1
        batches: dict[tuple[str, int, str], list[ChangeEvent]] = {}
        for event in events:
            batch_key = (
                event.repository_id,
                event.number,
                event.batch_id or event.current_snapshot.updated_at.isoformat(),
            )
            batches.setdefault(batch_key, []).append(event)

        ordered_batches = list(batches.values())
        if len(ordered_batches) > 1:
            await asyncio.to_thread(
                self.store.set_event_queue_reason,
                (
                    event.id
                    for batch_events in ordered_batches[1:]
                    for event in batch_events
                ),
                "change_request_order",
            )

        for batch_events in ordered_batches:
            service_interrupted = False
            retry_deferred = False
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
                    await asyncio.to_thread(
                        self.store.cleanup_terminal_transient_event,
                        event.id,
                    )
                    summary.processed_events += 1
                continue

            errors_by_event: dict[str, list[str]] = {
                event.id: [] for event in claimed_events
            }
            service_interrupted_event_ids: set[str] = set()
            administrator_cancelled_event_ids: set[str] = set()
            matched_event_ids: set[str] = set()
            task_items: list[
                tuple[RuleInvocation, asyncio.Task[AgentResult | None]]
            ] = []
            try:
                invocations = plan_rule_invocations(self.config.rules, claimed_events)
                direct_invocations = [
                    invocation
                    for invocation in invocations
                    if not invocation.rule.run_preflight
                ]
                preflight_invocations = [
                    invocation
                    for invocation in invocations
                    if invocation.rule.run_preflight
                ]
                representative = claimed_events[0]
                direct_dispatches = [
                    (
                        event.id,
                        invocation.idempotency_key,
                        invocation.rule.name,
                        invocation.agent_name,
                    )
                    for invocation in direct_invocations
                    for event in invocation.events
                ]
                direct_event_ids = tuple(
                    dict.fromkeys(item[0] for item in direct_dispatches)
                )
                if direct_event_ids:
                    matched_event_ids.update(direct_event_ids)
                    await asyncio.to_thread(
                        self.store.record_event_dispatches,
                        direct_event_ids,
                        direct_dispatches,
                    )
                task_items.extend(
                    (invocation, asyncio.create_task(self._run_agent(invocation)))
                    for invocation in direct_invocations
                )

                ready_preflight_invocations = preflight_invocations
                if (
                    preflight_invocations
                    and repository.preflight.enabled
                    and representative.current_snapshot.state == "opened"
                ):
                    summary.preflight_runs += 1
                    try:
                        preflight_result = await self.preflight.ensure_passed(representative)
                    except Exception as exc:
                        summary.preflight_errors += 1
                        ready_preflight_invocations = []
                        for invocation in preflight_invocations:
                            for event in invocation.events:
                                matched_event_ids.add(event.id)
                                errors_by_event[event.id].append(str(exc))
                    else:
                        if preflight_result.status in {"failure", "timed_out"}:
                            summary.preflight_failures += 1
                            ready_preflight_invocations = []
                            for invocation in preflight_invocations:
                                matched_event_ids.update(
                                    event.id for event in invocation.events
                                )
                        elif preflight_result.status != "success":
                            summary.preflight_errors += 1
                            ready_preflight_invocations = []
                            error = (
                                preflight_result.error
                                or f"Preflight 当前状态为 {preflight_result.status}"
                            )
                            for invocation in preflight_invocations:
                                for event in invocation.events:
                                    matched_event_ids.add(event.id)
                                    errors_by_event[event.id].append(error)

                if ready_preflight_invocations:
                    preflight_dispatches = [
                        (
                            event.id,
                            invocation.idempotency_key,
                            invocation.rule.name,
                            invocation.agent_name,
                        )
                        for invocation in ready_preflight_invocations
                        for event in invocation.events
                    ]
                    preflight_event_ids = tuple(
                        dict.fromkeys(item[0] for item in preflight_dispatches)
                    )
                    try:
                        await asyncio.to_thread(
                            self.store.record_event_dispatches,
                            preflight_event_ids,
                            preflight_dispatches,
                        )
                    except Exception as exc:
                        for invocation in ready_preflight_invocations:
                            for event in invocation.events:
                                matched_event_ids.add(event.id)
                                errors_by_event[event.id].append(str(exc))
                    else:
                        matched_event_ids.update(item[0] for item in preflight_dispatches)
                        task_items.extend(
                            (invocation, asyncio.create_task(self._run_agent(invocation)))
                            for invocation in ready_preflight_invocations
                        )
            except Exception as exc:
                for event in claimed_events:
                    errors_by_event[event.id].append(str(exc))

            if task_items:
                results = await asyncio.gather(
                    *(task for _, task in task_items),
                    return_exceptions=True,
                )
                summary.agent_runs += sum(
                    isinstance(result, AgentResult) for result in results
                )
                for (invocation, _), result in zip(task_items, results, strict=True):
                    run_id = result.run_id if isinstance(result, AgentResult) else None
                    if run_id is not None:
                        cancel_source = await asyncio.to_thread(
                            self.store.agent_run_cancel_source,
                            run_id,
                        )
                    else:
                        cancel_source = await asyncio.to_thread(
                            self.store.agent_run_cancel_source_by_idempotency,
                            invocation.idempotency_key,
                        )
                    if cancel_source is None and self._shutdown_requested:
                        cancel_source = CANCEL_SOURCE_SERVICE_SHUTDOWN
                    if cancel_source == CANCEL_SOURCE_SERVICE_SHUTDOWN:
                        service_interrupted_event_ids.update(
                            event.id for event in invocation.events
                        )
                        continue
                    if cancel_source == CANCEL_SOURCE_ADMINISTRATOR:
                        administrator_cancelled_event_ids.update(
                            event.id for event in invocation.events
                        )
                        continue
                    if isinstance(result, BaseException):
                        error = str(result)
                    elif isinstance(result, AgentResult) and result.status != "completed":
                        error = result.error or f"Agent 运行状态为 {result.status}"
                    else:
                        continue
                    for event in invocation.events:
                        errors_by_event[event.id].append(error)

            for event in claimed_events:
                if event.id in service_interrupted_event_ids:
                    await asyncio.to_thread(
                        self.store.release_event_after_service_shutdown,
                        event.id,
                    )
                    service_interrupted = True
                    continue
                if event.id in administrator_cancelled_event_ids:
                    await asyncio.to_thread(
                        self.store.finish_event,
                        event.id,
                        status="cancelled",
                        error="关联 Agent 运行已由管理员取消",
                    )
                    summary.processed_events += 1
                    continue
                error = "; ".join(errors_by_event[event.id]) or None
                if event.id not in matched_event_ids and error is None:
                    await asyncio.to_thread(
                        self.store.finish_event,
                        event.id,
                        status="unmatched",
                    )
                    await asyncio.to_thread(
                        self.store.cleanup_terminal_transient_event,
                        event.id,
                    )
                    summary.processed_events += 1
                    continue
                if error:
                    summary.errors.append(f"处理事件 {event.id} 失败：{error}")
                    retry_deferred = True
                await asyncio.to_thread(self.store.finish_event, event.id, error=error)
                if error is None:
                    await asyncio.to_thread(
                        self.store.cleanup_terminal_transient_event,
                        event.id,
                    )
                summary.processed_events += 1
            if service_interrupted:
                return "service_shutdown"
            if retry_deferred:
                return "retry"
        return None

    async def process_events(self, summary: CycleSummary) -> None:
        """并发处理不同 PR / MR，并在活动期间持续补充新事件。"""

        active: dict[tuple[str, int], asyncio.Task[str | None]] = {}
        max_attempts = self.config.runtime.event_retry_count + 1
        stop_scheduling = False
        retry_after: dict[tuple[str, int], float] = {}
        loop = asyncio.get_running_loop()
        while not self._shutdown_requested:
            resources = []
            if not stop_scheduling:
                resources = await asyncio.to_thread(
                    self.store.pending_event_resources,
                    max_attempts,
                )
            resource_set = set(resources)
            for resource in tuple(retry_after):
                if resource not in resource_set and resource not in active:
                    retry_after.pop(resource, None)
            now = loop.time()
            for resource in resources:
                if resource in active:
                    waiting_events = await asyncio.to_thread(
                        self.store.pending_events_for_resource,
                        resource[0],
                        resource[1],
                        max_attempts=max_attempts,
                    )
                    await asyncio.to_thread(
                        self.store.set_event_queue_reason,
                        (event.id for event in waiting_events),
                        "change_request_order",
                    )
                    continue
                retry_deadline = retry_after.get(resource)
                if retry_deadline is not None and now < retry_deadline:
                    waiting_events = await asyncio.to_thread(
                        self.store.pending_events_for_resource,
                        resource[0],
                        resource[1],
                        max_attempts=max_attempts,
                    )
                    await asyncio.to_thread(
                        self.store.set_event_queue_reason,
                        (event.id for event in waiting_events),
                        "event_retry_backoff",
                    )
                    continue
                retry_after.pop(resource, None)
                active[resource] = asyncio.create_task(
                    self._process_resource_events(summary, resource),
                    name=f"event-dispatch-{resource[0]}-{resource[1]}",
                )

            if not active:
                if stop_scheduling:
                    return
                if retry_after:
                    nearest_retry = min(retry_after.values())
                    await asyncio.sleep(
                        min(0.25, max(0.0, nearest_retry - loop.time()))
                    )
                    continue
                return
            done, _ = await asyncio.wait(
                tuple(active.values()),
                timeout=0.25,
                return_when=asyncio.FIRST_COMPLETED,
            )
            for resource, task in tuple(active.items()):
                if task not in done:
                    continue
                active.pop(resource, None)
                try:
                    outcome = await task
                    if outcome == "service_shutdown":
                        stop_scheduling = True
                    elif outcome == "retry":
                        retryable = await asyncio.to_thread(
                            self.store.has_retryable_failed_events_for_resource,
                            resource[0],
                            resource[1],
                            max_attempts=max_attempts,
                        )
                        if retryable:
                            retry_after[resource] = (
                                loop.time() + EVENT_RETRY_BACKOFF_SECONDS
                            )
                            waiting_events = await asyncio.to_thread(
                                self.store.pending_events_for_resource,
                                resource[0],
                                resource[1],
                                max_attempts=max_attempts,
                            )
                            await asyncio.to_thread(
                                self.store.set_event_queue_reason,
                                (event.id for event in waiting_events),
                                "event_retry_backoff",
                            )
                        else:
                            retry_after.pop(resource, None)
                except Exception as exc:
                    retry_after[resource] = (
                        loop.time() + EVENT_RETRY_BACKOFF_SECONDS
                    )
                    summary.errors.append(
                        f"处理 {resource[0]} #{resource[1]} 的事件队列失败：{exc}"
                    )
            if stop_scheduling and not active:
                return

        if active:
            await asyncio.gather(*active.values(), return_exceptions=True)

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
