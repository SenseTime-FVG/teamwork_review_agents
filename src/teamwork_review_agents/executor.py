"""Agent 提示词组装、幂等运行和资源锁编排。"""

from __future__ import annotations

import asyncio
import json
import threading
import uuid
from pathlib import Path
from typing import Any, Awaitable, Callable, Sequence

from .agent_workspace import (
    agent_repository_cache_environment,
    prepare_agent_workspace,
)
from .codex_model_runner import CodexModelRunner
from .codex_runner import CodexRunner
from .config import AppConfig, ProviderConfig, RepositoryConfig
from .environment import (
    PromptRenderError,
    SecretRedactor,
    render_prompt,
    resolve_environment,
)
from .locks import ResourceLease
from .models import (
    AgentResult,
    ChangeEvent,
    InvocationContext,
    ScheduledRunContext,
    stable_hash,
)
from .model_tools import InvokeAgentStartedCallback
from .model_provider_runtime import (
    effective_agent_config,
    resolve_model_plan,
    resolve_model_snapshot,
)
from .state import (
    CANCEL_SOURCE_ADMINISTRATOR,
    CANCEL_SOURCE_SERVICE_SHUTDOWN,
    RunReservation,
    StateStore,
)
from .workspace import (
    GitProgressEvent,
    change_request_ref,
    cleanup_expired_worktrees,
    cleanup_run_worktree,
    ensure_isolated_clone,
    ensure_isolated_worktree,
    mark_active_worktree,
    prepare_change_request_workspace,
    prepare_default_branch_workspace,
    repository_git_lock_key,
    run_workspace_kind,
    validate_run_workspace,
    WorkspaceCancelled,
    worktree_head,
    worktree_ref_head,
    worktree_starting_head,
)


class AgentExecutionError(RuntimeError):
    """表示 Agent 配置、限额、资源或 Codex 执行失败。"""


class AgentWorkspacePreparationError(RuntimeError):
    """表示模型启动前的仓库工作区准备未成功。"""

    def __init__(self, message: str, *, status: str) -> None:
        super().__init__(message)
        self.status = status


def _action_name(event_type: str) -> str:
    """将内部完整事件名转换为给 Agent 使用的短动作名。"""

    return event_type.removeprefix("change_request.")


def _repository_payload(
    repository: RepositoryConfig,
    provider: ProviderConfig,
) -> dict[str, Any]:
    """返回根 Agent 与 sub-agent 共用的仓库上下文。"""

    return {
        "id": repository.id,
        "project": repository.project,
        "provider": repository.provider,
        "provider_kind": provider.kind,
        "provider_base_url": provider.base_url,
        "workspace": str(repository.workspace),
    }


def _mr_payload(
    event: ChangeEvent,
    repository: RepositoryConfig,
    provider: ProviderConfig,
    actions: Sequence[str],
    change_ref: str,
    target_head_sha: str,
) -> dict[str, Any]:
    """返回根 Agent 所需的统一 MR / PR 当前信息。"""

    snapshot = event.current_snapshot
    payload = {
        "repository": _repository_payload(repository, provider),
        "number": snapshot.number,
        "title": snapshot.title,
        "state": snapshot.state,
        "action": [_action_name(action) for action in actions],
        "draft": snapshot.draft,
        "source_branch": snapshot.source_branch,
        "target_branch": snapshot.target_branch,
        "head_sha": snapshot.head_sha,
        "labels": list(snapshot.labels),
        "approvals": snapshot.approvals,
        "pipeline_status": snapshot.pipeline_status,
        "merge_status": snapshot.merge_status,
        "updated_at": snapshot.updated_at.isoformat(),
        "url": snapshot.web_url,
        "change_ref": change_ref,
        "target_ref": f"refs/remotes/origin/{snapshot.target_branch}",
        "target_head_sha": target_head_sha,
    }
    if snapshot.source_project:
        payload["source_project"] = snapshot.source_project
    return payload


def _schedule_payload(
    schedule: ScheduledRunContext,
    repository: RepositoryConfig,
    provider: ProviderConfig,
) -> dict[str, Any]:
    """返回定时根 Agent 所需的默认分支执行上下文。"""

    return {
        "name": schedule.rule_name,
        "scheduled_at": schedule.scheduled_at.isoformat(),
        "created_at": schedule.created_at.isoformat(),
        "occurrence_id": schedule.occurrence_id,
        "repository": _repository_payload(repository, provider),
        "branch": schedule.branch,
        "head_sha": schedule.head_sha,
    }


class AgentExecutor:
    """统一执行根 Agent 和 CLI/MCP/内嵌模式调起的 sub-agent。"""

    def __init__(self, config: AppConfig, store: StateStore) -> None:
        self.config = config
        self.store = store
        self._provider_runners: dict[str, CodexRunner | CodexModelRunner] = {}
        # 保留旧调用方读取 runner 的兼容视图；实际运行按每个 Agent 的 Provider 路由。
        self.runner = (
            CodexModelRunner(
                config,
                invoke_agent_callback=self._invoke_embedded_agent,
            )
            if config.runtime.codex.execution_mode == "model"
            else CodexRunner(config)
        )
        self.repositories = config.repository_map()
        self._shutdown_requested = threading.Event()

    def _runner_for_provider(
        self,
        provider_id: str,
    ) -> CodexRunner | CodexModelRunner:
        """为模型 Provider 复用对应 Runner 实例。"""

        runner = self._provider_runners.get(provider_id)
        if runner is not None:
            return runner
        provider = self.config.model_providers[provider_id]
        if provider.driver == "codex_cli":
            runner = (
                CodexModelRunner(
                    self.config,
                    provider_id=provider_id,
                    invoke_agent_callback=self._invoke_embedded_agent,
                )
                if self.config.runtime.codex.execution_mode == "model"
                else CodexRunner(self.config)
            )
        else:
            runner = CodexModelRunner(
                self.config,
                provider_id=provider_id,
                invoke_agent_callback=self._invoke_embedded_agent,
            )
        self._provider_runners[provider_id] = runner
        return runner

    async def _invoke_embedded_agent(
        self,
        context: InvocationContext,
        agent_name: str,
        task: str,
        extra_context: dict[str, Any] | None,
        started_callback: InvokeAgentStartedCallback | None = None,
    ) -> dict[str, Any]:
        """复用现有执行器语义直接调度内嵌模式 sub-agent。"""

        if context.config_path != str(self.config.config_path):
            raise AgentExecutionError("调用上下文与当前配置文件不一致")
        if context.current_agent not in self.config.agents:
            raise AgentExecutionError(f"父 Agent 不存在：{context.current_agent}")
        parent = self.config.agents[context.current_agent]
        if agent_name not in parent.allowed_sub_agents:
            raise PermissionError(
                f"Agent {context.current_agent} 不允许调用 sub-agent {agent_name}"
            )
        if agent_name not in self.config.agents:
            raise AgentExecutionError(f"sub-agent 不存在：{agent_name}")
        child_depth = context.depth + 1
        if child_depth > self.config.runtime.max_sub_agent_depth:
            raise AgentExecutionError(
                f"sub-agent 深度 {child_depth} 超过限制 "
                f"{self.config.runtime.max_sub_agent_depth}"
            )
        if agent_name in context.call_chain:
            raise AgentExecutionError(
                f"检测到 Agent 调用环：{' -> '.join((*context.call_chain, agent_name))}"
            )
        execute_arguments = {
            "agent_name": agent_name,
            "event": context.event,
            "schedule": context.schedule,
            "task": task,
            "extra_context": extra_context,
            "root_run_id": context.root_run_id,
            "parent_run_id": context.run_id,
            "depth": child_depth,
            "call_chain": context.call_chain,
            "inherit_workspace": context.inherit_workspace,
            "parent_workspace": (
                Path(context.active_workspace) if context.active_workspace else None
            ),
            "idempotency_key": sub_agent_idempotency_key(
                root_run_id=context.root_run_id,
                parent_run_id=context.run_id,
                agent_name=agent_name,
                event_id=(
                    context.event.id
                    if context.event is not None
                    else context.schedule.occurrence_id
                ),
                task=task,
                extra_context=extra_context,
            ),
        }
        # Responses 明确关闭并行工具调用，同一父回合的委托天然串行；
        # 子 Agent 仍由资源租约处理跨运行写冲突，避免嵌套委托持锁死锁。
        async def report_started(reservation: RunReservation) -> None:
            """在子运行占位完成后立即把精确关联交回父运行日志。"""

            if started_callback is None:
                return
            await started_callback(
                {
                    "run_id": reservation.run_id,
                    "root_run_id": reservation.root_run_id,
                    "parent_run_id": reservation.parent_run_id,
                    "agent_name": agent_name,
                    "status": "queued",
                }
            )

        result = await self.execute(
            **execute_arguments,
            run_started_callback=report_started,
        )
        if result is None:
            return {
                "status": "deduplicated",
                "agent_name": agent_name,
                "message": "相同委托已经运行或完成",
            }
        return {
            "status": result.status,
            "run_id": result.run_id,
            "agent_name": result.agent_name,
            "final_message": result.final_message,
            "usage": result.usage,
        }

    def begin_shutdown(self) -> None:
        """阻止本执行器继续创建 Codex，并让准备阶段尽快退出。"""

        self._shutdown_requested.set()

    def _cancel_requested(self, run_id: str) -> bool:
        """合并服务停止标志与持久化的单次运行取消请求。"""

        return self._shutdown_requested.is_set() or self.store.agent_run_cancel_requested(
            run_id
        )

    async def _wait_for_run_capacity(
        self,
        run_id: str,
        *,
        agent_name: str,
        depth: int,
    ) -> bool:
        """等待全局根任务额度和同名 Agent 额度。"""

        agent_limit = self.config.agents[agent_name].max_concurrent_runs
        while not self._cancel_requested(run_id):
            acquired, reason = await asyncio.to_thread(
                self.store.try_acquire_agent_run_capacity,
                run_id,
                global_limit=self.config.runtime.max_concurrent_agents,
                runtime_limit=self.config.runtime.agent_concurrency_limit,
                agent_limit=agent_limit,
                acquire_global=depth == 0,
            )
            if acquired:
                return True
            if reason is None:
                return False
            await asyncio.sleep(0.2)
        return False

    def run_workspace_path(
        self,
        repository: RepositoryConfig,
        run_id: str,
    ) -> Path:
        """返回一次 Agent 运行专属的临时 Git 工作区路径。"""

        repository_directory = stable_hash(repository.id)[:16]
        return (
            self.config.database.path.parent
            / "worktrees"
            / repository_directory
            / run_id
        ).resolve()

    def git_admin_lock_key(self, repository: RepositoryConfig) -> str:
        """返回基础仓库 fetch 与运行工作区管理使用的短时锁。"""

        return repository_git_lock_key(repository)

    def build_prompt(
        self,
        *,
        agent_name: str,
        event: ChangeEvent | None,
        repository: RepositoryConfig,
        task: str | None,
        extra_context: dict[str, Any] | None,
        prompt_values: dict[str, str],
        change_ref: str,
        actions: Sequence[str],
        target_head_sha: str | None = None,
        schedule: ScheduledRunContext | None = None,
    ) -> str:
        """组合 Agent 固定角色、触发上下文和临时委托任务。"""

        agent = self.config.agents[agent_name]
        if agent.prompt_file:
            template = agent.prompt_file.read_text(encoding="utf-8")
        else:
            template = agent.prompt or ""
        try:
            role_prompt = render_prompt(template, prompt_values).strip()
        except PromptRenderError as exc:
            raise AgentExecutionError(str(exc)) from exc
        provider = self.config.providers[repository.provider]
        if task is None and event is not None:
            if not target_head_sha:
                raise AgentExecutionError("根 Agent 缺少目标分支当前提交")
            context: dict[str, Any] = {
                "mr": _mr_payload(
                    event,
                    repository,
                    provider,
                    actions,
                    change_ref,
                    target_head_sha,
                ),
            }
        elif task is None and schedule is not None:
            context = {
                "schedule": _schedule_payload(schedule, repository, provider),
            }
        else:
            context = {
                "repository": _repository_payload(repository, provider),
                "delegated_task": task,
            }
            if extra_context:
                context["delegated_context"] = extra_context
            if schedule is not None:
                context["schedule"] = _schedule_payload(
                    schedule,
                    repository,
                    provider,
                )
        managed_comment_instruction = ""
        if agent.managed_comment and event is not None:
            managed_comment_instruction = (
                "最终顶层评论必须调用 `publish_comment` 工具进行发布或更新；"
                "不得使用 `gh`、`glab` 或平台 API 另行发布顶层总结评论。"
                "该工具只接收完整评论正文，评论目标和源版本代次由 Teamwork "
                "根据本次可信运行上下文确定。\n\n"
            )
        return (
            f"{role_prompt}\n\n"
            "# 本次运行上下文\n\n"
            f"```json\n{json.dumps(context, ensure_ascii=False, indent=2)}\n```\n\n"
            f"{managed_comment_instruction}"
            "如果工具列表中存在 `invoke_agent`，只能在确有必要时调用配置允许的 "
            "sub-agent，并向它传递边界清晰的任务。最终必须明确说明实际执行的操作、"
            "验证结果和仍存在的阻断项。"
        )

    def lock_keys(
        self,
        agent_name: str,
        event: ChangeEvent | None,
        repository: RepositoryConfig,
        schedule: ScheduledRunContext | None = None,
    ) -> list[str]:
        """按 Agent 声明生成变更请求级和源分支级写锁。"""

        scopes = self.config.agents[agent_name].write_scopes
        keys: list[str] = []
        if "change_request" in scopes and event is not None:
            keys.append(f"change_request:{event.resource_key}")
        if "workspace" in scopes:
            if event is not None:
                branch = event.current_snapshot.source_branch
                provider_name = event.provider
            else:
                branch = schedule.branch if schedule is not None else "default"
                provider_name = repository.provider
            keys.append(
                "repository_branch:"
                f"{provider_name}:{repository.id}:{branch or 'default'}"
            )
        return keys

    async def execute(
        self,
        *,
        agent_name: str,
        event: ChangeEvent | None = None,
        schedule: ScheduledRunContext | None = None,
        idempotency_key: str,
        rule_name: str | None = None,
        task: str | None = None,
        extra_context: dict[str, Any] | None = None,
        root_run_id: str | None = None,
        parent_run_id: str | None = None,
        depth: int = 0,
        call_chain: tuple[str, ...] = (),
        actions: Sequence[str] | None = None,
        inherit_workspace: bool = False,
        parent_workspace: Path | None = None,
        run_started_callback: Callable[[RunReservation], Awaitable[None]] | None = None,
    ) -> AgentResult | None:
        """申请审计记录与写锁，然后运行所选 Codex 执行模式。"""

        if self._shutdown_requested.is_set():
            raise AgentExecutionError("服务正在停止，不再创建新的 Agent 运行")

        if (event is None) == (schedule is None):
            raise AgentExecutionError("运行必须且只能包含事件或定时触发来源")
        if agent_name not in self.config.agents:
            raise AgentExecutionError(f"不存在 Agent：{agent_name}")
        repository_id = (
            event.repository_id if event is not None else schedule.repository_id
        )
        if repository_id not in self.repositories:
            raise AgentExecutionError(f"不存在仓库：{repository_id}")
        if depth > self.config.runtime.max_sub_agent_depth:
            raise AgentExecutionError(
                f"sub-agent 深度 {depth} 超过限制 {self.config.runtime.max_sub_agent_depth}"
            )
        root_run_count = 0
        if root_run_id:
            root_run_count = await asyncio.to_thread(
                self.store.count_root_runs,
                root_run_id,
            )
        if root_run_count >= self.config.runtime.max_agent_runs_per_root:
            raise AgentExecutionError(
                f"根任务 {root_run_id} 已达到 Agent 调用上限 "
                f"{self.config.runtime.max_agent_runs_per_root}"
            )

        configured_repository = self.repositories[repository_id]
        provider = self.config.providers[configured_repository.provider]
        configured_agent = self.config.agents[agent_name]
        model_plan = resolve_model_plan(self.config, configured_agent)

        def executable_selection(selection: Any) -> bool:
            """判断候选是否能启动当前执行器。完整 CLI 允许模型由 CLI 自行决定。"""

            if not selection.provider.enabled:
                return False
            if selection.model:
                return True
            return (
                selection.provider.driver == "codex_cli"
                and self.config.runtime.codex.execution_mode == "cli"
            )

        model_selection = next(
            (
                selection
                for selection in model_plan.selections
                if executable_selection(selection)
            ),
            None,
        )
        if model_selection is None:
            raise AgentExecutionError(
                "模型主链与回退链没有可用的 Provider/模型"
            )
        agent = effective_agent_config(
            self.config,
            configured_agent,
            model_selection,
        )
        model_snapshot = await asyncio.to_thread(
            resolve_model_snapshot,
            self.config,
            configured_agent,
            self.config.runtime.codex_home,
        )
        runner = self._runner_for_provider(model_selection.provider_id)
        proposed_run_id = str(uuid.uuid4())
        reservation = await asyncio.to_thread(
            self.store.begin_agent_run,
            proposed_run_id=proposed_run_id,
            root_run_id=root_run_id,
            parent_run_id=parent_run_id,
            idempotency_key=idempotency_key,
            event_id=event.id if event is not None else None,
            rule_name=rule_name,
            agent_name=agent_name,
            resource_key=(
                event.resource_key
                if event is not None
                else f"schedule:{schedule.rule_name}:{repository_id}:"
                f"{schedule.occurrence_id}"
            ),
            prompt="",
            environment={},
            config_revision=self.config.revision,
            max_attempts=self.config.runtime.event_retry_count + 1,
            model_snapshot=model_snapshot,
            repository_id=repository_id,
            trigger_source="event" if event is not None else "schedule",
            trigger_context=(
                schedule.model_dump(mode="json")
                if schedule is not None
                else None
            ),
        )
        if reservation is None:
            status = await asyncio.to_thread(
                self.store.agent_run_status,
                idempotency_key,
            )
            if status in {"completed", "queued", "preparing", "running"}:
                return None
            raise AgentExecutionError(
                f"幂等任务已经达到重试上限，当前状态：{status or 'unknown'}"
            )
        if run_started_callback is not None:
            await run_started_callback(reservation)

        async def cancellation_source() -> str | None:
            """读取持久化来源，并兜住停止请求与数据库写入之间的竞态。"""

            source = await asyncio.to_thread(
                self.store.agent_run_cancel_source,
                reservation.run_id,
            )
            if source is not None:
                return source
            if self._shutdown_requested.is_set():
                return CANCEL_SOURCE_SERVICE_SHUTDOWN
            return None

        if self._shutdown_requested.is_set():
            await asyncio.to_thread(
                self.store.request_cancel_run,
                reservation.run_id,
                source=CANCEL_SOURCE_SERVICE_SHUTDOWN,
            )

        await self._wait_for_run_capacity(
            reservation.run_id,
            agent_name=agent_name,
            depth=depth,
        )

        keys = self.lock_keys(
            agent_name,
            event,
            configured_repository,
            schedule,
        )
        if keys:
            await asyncio.to_thread(
                self.store.set_agent_run_queue_reason,
                reservation.run_id,
                "resource_lock",
            )
        lease = ResourceLease(
            self.store,
            keys,
            reservation.root_run_id,
            ttl_seconds=self.config.runtime.lock_ttl_seconds,
            timeout_seconds=self.config.runtime.lock_timeout_seconds,
            cancel_check=lambda: self._cancel_requested(reservation.run_id),
        )

        async def persist_log(
            stream: str,
            event_type: str,
            payload: str | dict[str, Any],
        ) -> None:
            """将 Runner 流式事件写入当前运行日志。"""

            await asyncio.to_thread(
                self.store.append_run_log,
                reservation.run_id,
                stream=stream,
                event_type=event_type,
                payload=payload,
            )

        redactor = SecretRedactor(())
        active_workspace: Path | None = None
        owned_workspace = False
        workspace_prepared = False
        resolved_schedule = schedule
        starting_head = (
            event.current_snapshot.head_sha if event is not None else schedule.head_sha
        )
        target_head_sha: str | None = None
        result: AgentResult
        try:
            async with lease:
                if task is not None and inherit_workspace:
                    preparing = await asyncio.to_thread(
                        self.store.mark_agent_run_preparing,
                        reservation.run_id,
                    )
                    if not preparing:
                        raise WorkspaceCancelled(
                            "运行在继承父工作区前被管理员取消"
                        )
                    if parent_workspace is None:
                        raise AgentExecutionError(
                            "工作区继承已开启，但父 Agent 工作目录缺失"
                        )
                    active_workspace = await asyncio.to_thread(
                        validate_run_workspace,
                        configured_repository.workspace,
                        parent_workspace,
                        timeout_seconds=self.config.runtime.git_timeout_seconds,
                    )
                    change_ref = (
                        change_request_ref(provider, event.number)[1]
                        if event is not None
                        else schedule.head_sha
                    )
                    inherited_kind = await asyncio.to_thread(
                        run_workspace_kind,
                        active_workspace,
                    )
                    workspace_mode = f"inherited-{inherited_kind}"
                    workspace_reason = "复用父 Agent 本次运行的临时 Git 工作区"
                else:
                    active_workspace = self.run_workspace_path(
                        configured_repository,
                        reservation.run_id,
                    )
                    await asyncio.to_thread(
                        self.store.set_agent_run_queue_reason,
                        reservation.run_id,
                        "repository_lock",
                    )
                    git_admin_lease = ResourceLease(
                        self.store,
                        [self.git_admin_lock_key(configured_repository)],
                        reservation.run_id,
                        ttl_seconds=self.config.runtime.lock_ttl_seconds,
                        timeout_seconds=self.config.runtime.lock_timeout_seconds,
                        cancel_check=lambda: self._cancel_requested(reservation.run_id),
                    )
                    async with git_admin_lease:
                        preparing = await asyncio.to_thread(
                            self.store.mark_agent_run_preparing,
                            reservation.run_id,
                        )
                        if not preparing:
                            raise WorkspaceCancelled(
                                "运行在 Git 工作区准备前被管理员取消"
                            )
                        event_loop = asyncio.get_running_loop()

                        def report_git_progress(
                            git_event: GitProgressEvent,
                        ) -> None:
                            """从 Git 工作线程向运行日志提交脱敏阶段进度。"""

                            asyncio.run_coroutine_threadsafe(
                                persist_log(
                                    "system",
                                    f"workspace.git.{git_event.state}",
                                    {
                                        **git_event.as_dict(),
                                        "source": "agent",
                                        "repository_id": configured_repository.id,
                                        "run_id": reservation.run_id,
                                    },
                                ),
                                event_loop,
                            )

                        git_cancel_check = lambda: self._cancel_requested(
                            reservation.run_id
                        )
                        if event is not None:
                            change_ref = await asyncio.to_thread(
                                prepare_change_request_workspace,
                                provider,
                                configured_repository,
                                event.current_snapshot,
                                timeout_seconds=self.config.runtime.git_timeout_seconds,
                                initialization_timeout_seconds=(
                                    self.config.runtime.repository_initialization_timeout_seconds
                                ),
                                cancel_check=git_cancel_check,
                                progress_callback=report_git_progress,
                            )
                        else:
                            _, branch, head_sha = await asyncio.to_thread(
                                prepare_default_branch_workspace,
                                provider,
                                configured_repository,
                                timeout_seconds=self.config.runtime.git_timeout_seconds,
                                initialization_timeout_seconds=(
                                    self.config.runtime.repository_initialization_timeout_seconds
                                ),
                                cancel_check=git_cancel_check,
                                progress_callback=report_git_progress,
                            )
                            selected_branch = schedule.branch or branch
                            selected_head_sha = schedule.head_sha or head_sha
                            resolved_schedule = schedule.model_copy(
                                update={
                                    "branch": selected_branch,
                                    "head_sha": selected_head_sha,
                                },
                            )
                            starting_head = selected_head_sha
                            change_ref = selected_head_sha
                            await asyncio.to_thread(
                                self.store.update_agent_run_trigger_context,
                                reservation.run_id,
                                resolved_schedule.model_dump(mode="json"),
                            )
                        await asyncio.to_thread(
                            cleanup_expired_worktrees,
                            configured_repository.workspace,
                            active_workspace.parent,
                            git_timeout_seconds=self.config.runtime.git_timeout_seconds,
                        )
                        original_starting_head = await asyncio.to_thread(
                            worktree_starting_head,
                            active_workspace,
                        )
                        uses_independent_clone = "workspace" in agent.write_scopes
                        if uses_independent_clone:
                            active_workspace = await asyncio.to_thread(
                                ensure_isolated_clone,
                                configured_repository.workspace,
                                active_workspace,
                                change_ref,
                                change_ref=change_ref,
                                timeout_seconds=self.config.runtime.git_timeout_seconds,
                                cancel_check=git_cancel_check,
                                progress_callback=report_git_progress,
                            )
                        else:
                            active_workspace = await asyncio.to_thread(
                                ensure_isolated_worktree,
                                configured_repository.workspace,
                                active_workspace,
                                change_ref,
                                timeout_seconds=self.config.runtime.git_timeout_seconds,
                                cancel_check=git_cancel_check,
                                progress_callback=report_git_progress,
                            )
                        starting_head = original_starting_head or await asyncio.to_thread(
                            worktree_head,
                            active_workspace,
                            timeout_seconds=self.config.runtime.git_timeout_seconds,
                        )
                        await asyncio.to_thread(
                            mark_active_worktree,
                            active_workspace,
                            starting_head=starting_head,
                            retention_days=self.config.runtime.worktree_retention_days,
                            timeout_seconds=agent.timeout_seconds,
                        )
                    owned_workspace = True
                    workspace_kind = "clone" if uses_independent_clone else "worktree"
                    workspace_mode = (
                        f"root-{workspace_kind}"
                        if task is None
                        else f"sub-agent-{workspace_kind}"
                    )
                    workspace_reason = (
                        "本次可写 Agent 运行独享本地 clone 与 Git 元数据"
                        if uses_independent_clone
                        else "本次只读 Agent 运行独享轻量 linked worktree"
                    )

                repository = configured_repository.model_copy(
                    update={"workspace": active_workspace},
                )
                resolved_environment = resolve_environment(
                    self.config,
                    repository,
                    agent,
                    event,
                    reservation.run_id,
                    include_change_request=task is None and event is not None,
                    schedule=resolved_schedule,
                )
                redactor = SecretRedactor(resolved_environment.secret_values)
                cache_root, cache_environment = agent_repository_cache_environment(
                    self.config,
                    repository,
                )
                process_environment = {
                    **resolved_environment.process_values,
                    **cache_environment,
                }
                audit_environment = dict(resolved_environment.audit_values)
                if cache_root is not None:
                    audit_environment["TEAMWORK_REPOSITORY_CACHE_DIR"] = str(
                        cache_root.resolve()
                    )
                effective_actions = tuple(
                    actions or ((event.type,) if event is not None else ())
                )
                if task is None and event is not None and target_head_sha is None:
                    target_head_sha = await asyncio.to_thread(
                        worktree_ref_head,
                        active_workspace,
                        f"refs/remotes/origin/{event.current_snapshot.target_branch}",
                        timeout_seconds=self.config.runtime.git_timeout_seconds,
                    )
                prompt = self.build_prompt(
                    agent_name=agent_name,
                    event=event,
                    schedule=resolved_schedule,
                    repository=repository,
                    task=task,
                    extra_context=extra_context,
                    prompt_values=resolved_environment.prompt_values,
                    change_ref=change_ref,
                    actions=effective_actions,
                    target_head_sha=target_head_sha,
                )
                await asyncio.to_thread(
                    self.store.update_agent_run_inputs,
                    reservation.run_id,
                    prompt=redactor.text(prompt),
                    environment=audit_environment,
                    config_revision=self.config.revision,
                )
                await asyncio.to_thread(
                    self.store.update_agent_run_workspace,
                    reservation.run_id,
                    path=str(active_workspace),
                    status="inherited" if not owned_workspace else "active",
                    reason=workspace_reason,
                )
                preparation = await prepare_agent_workspace(
                    config=self.config,
                    repository=repository,
                    agent=agent,
                    process_environment=process_environment,
                    redactor=redactor,
                    log_callback=persist_log,
                    cancel_check=lambda: self._cancel_requested(
                        reservation.run_id
                    ),
                    inherited_workspace=task is not None and inherit_workspace,
                )
                if preparation.outcome.status != "success":
                    detail = preparation.outcome.error or (
                        f"步骤 {preparation.outcome.failed_step or 'unknown'} 失败"
                    )
                    if preparation.outcome.exit_code is not None:
                        detail = (
                            f"{detail}，退出码 {preparation.outcome.exit_code}"
                        )
                    raise AgentWorkspacePreparationError(
                        detail,
                        status=preparation.outcome.status,
                    )
                workspace_prepared = True
                await persist_log(
                    "system",
                    "workspace.prepared",
                    {
                        "mode": workspace_mode,
                        "path": str(active_workspace),
                        "reason": workspace_reason,
                        "snapshot_status": preparation.snapshot_status,
                        "snapshot_fingerprint": preparation.snapshot_fingerprint,
                    },
                )
                started = await asyncio.to_thread(
                    self.store.mark_agent_run_running,
                    reservation.run_id,
                )
                if not started:
                    source = await cancellation_source()
                    result = AgentResult(
                        run_id=reservation.run_id,
                        root_run_id=reservation.root_run_id,
                        parent_run_id=reservation.parent_run_id,
                        agent_name=agent_name,
                        status="cancelled",
                        error=(
                            "服务停止时在 Codex CLI 启动前中断运行"
                            if source == CANCEL_SOURCE_SERVICE_SHUTDOWN
                            else "运行在 Codex CLI 启动前被管理员取消"
                        ),
                    )
                else:
                    await persist_log(
                        "system",
                        "run.started",
                        {
                            "agent_name": agent_name,
                            "execution_mode": (
                                self.config.runtime.codex.execution_mode
                                if model_selection.provider.driver == "codex_cli"
                                else "model"
                            ),
                            "provider_id": model_selection.provider_id,
                            "provider_driver": model_selection.provider.driver,
                            "model": model_selection.model,
                            "config_revision": self.config.revision,
                            "environment": audit_environment,
                            "workspace_mode": workspace_mode,
                            "workspace": str(active_workspace),
                        },
                    )
                    context = InvocationContext(
                        config_path=str(self.config.config_path),
                        current_agent=agent_name,
                        run_id=reservation.run_id,
                        root_run_id=reservation.root_run_id,
                        depth=depth,
                        call_chain=(*call_chain, agent_name),
                        inherit_workspace=inherit_workspace,
                        active_workspace=str(active_workspace),
                        event=event,
                        schedule=resolved_schedule,
                    )
                    result = await runner.run(
                        run_id=reservation.run_id,
                        root_run_id=reservation.root_run_id,
                        parent_run_id=reservation.parent_run_id,
                        agent_name=agent_name,
                        agent=(
                            configured_agent
                            if isinstance(runner, CodexModelRunner)
                            else agent
                        ),
                        repository=repository,
                        context=context,
                        prompt=prompt,
                        process_environment=process_environment,
                        redactor=redactor,
                        log_callback=persist_log,
                        cancel_check=lambda: asyncio.to_thread(
                            self._cancel_requested,
                            reservation.run_id,
                        ),
                        **(
                            {
                                "model_plan": model_plan.selections,
                                "model_snapshot_callback": (
                                    lambda snapshot: asyncio.to_thread(
                                        self.store.update_agent_run_model_snapshot,
                                        reservation.run_id,
                                        snapshot,
                                    )
                                ),
                            }
                            if isinstance(runner, CodexModelRunner)
                            else {}
                        ),
                    )
                if lease.lost:
                    result.status = "failed"
                    result.error = "运行期间写资源租约丢失，结果不再视为可信"
        except AgentWorkspacePreparationError as exc:
            mapped_status = (
                "cancelled"
                if exc.status == "cancelled"
                else "timed_out"
                if exc.status == "timed_out"
                else "failed"
            )
            result = AgentResult(
                run_id=reservation.run_id,
                root_run_id=reservation.root_run_id,
                parent_run_id=reservation.parent_run_id,
                agent_name=agent_name,
                status=mapped_status,
                error=redactor.text(str(exc)),
            )
        except WorkspaceCancelled as exc:
            source = await cancellation_source()
            result = AgentResult(
                run_id=reservation.run_id,
                root_run_id=reservation.root_run_id,
                parent_run_id=reservation.parent_run_id,
                agent_name=agent_name,
                status="cancelled",
                error=(
                    "服务停止时中断 Git 工作区准备"
                    if source == CANCEL_SOURCE_SERVICE_SHUTDOWN
                    else redactor.text(str(exc))
                ),
            )
        except Exception as exc:
            cancelled = await asyncio.to_thread(
                self._cancel_requested,
                reservation.run_id,
            )
            source = await cancellation_source() if cancelled else None
            error = (
                "服务停止时中断运行"
                if source == CANCEL_SOURCE_SERVICE_SHUTDOWN
                else "运行已由管理员取消"
                if cancelled
                else redactor.text(str(exc))
            )
            result = AgentResult(
                run_id=reservation.run_id,
                root_run_id=reservation.root_run_id,
                parent_run_id=reservation.parent_run_id,
                agent_name=agent_name,
                status="cancelled" if cancelled else "failed",
                error=error,
            )

        if result.status == "cancelled":
            source = await cancellation_source()
            if source == CANCEL_SOURCE_SERVICE_SHUTDOWN:
                result.error = result.error or "服务停止时中断运行"
            elif source == CANCEL_SOURCE_ADMINISTRATOR:
                result.error = result.error or "运行已由管理员取消"

        if owned_workspace and active_workspace is not None:
            try:
                cleanup_lease = ResourceLease(
                    self.store,
                    [self.git_admin_lock_key(configured_repository)],
                    reservation.run_id,
                    ttl_seconds=self.config.runtime.lock_ttl_seconds,
                    timeout_seconds=self.config.runtime.lock_timeout_seconds,
                )
                async with cleanup_lease:
                    cleanup = await asyncio.to_thread(
                        cleanup_run_worktree,
                        configured_repository.workspace,
                        active_workspace,
                        run_status=result.status,
                        starting_head=starting_head,
                        retention_days=self.config.runtime.worktree_retention_days,
                        git_timeout_seconds=self.config.runtime.git_timeout_seconds,
                    )
                await asyncio.to_thread(
                    self.store.update_agent_run_workspace,
                    reservation.run_id,
                    path=str(active_workspace),
                    status=cleanup.status,
                    reason=cleanup.reason,
                )
                await persist_log(
                    "system",
                    f"workspace.{cleanup.status}",
                    {"path": str(active_workspace), "reason": cleanup.reason},
                )
            except Exception as exc:
                cleanup_error = redactor.text(f"工作区清理检查失败：{exc}")
                await asyncio.to_thread(
                    self.store.update_agent_run_workspace,
                    reservation.run_id,
                    path=str(active_workspace),
                    status="retained",
                    reason=cleanup_error,
                )
                await persist_log(
                    "system",
                    "workspace.retained",
                    {"path": str(active_workspace), "reason": cleanup_error},
                )
        elif not workspace_prepared:
            await asyncio.to_thread(
                self.store.update_agent_run_workspace,
                reservation.run_id,
                path="",
                status="not-created",
                reason="Agent 启动前未能准备临时工作区",
            )
        await persist_log(
            "system",
            f"run.{result.status}",
            {
                "status": result.status,
                "error": result.error,
                "usage": result.usage,
            },
        )
        await asyncio.to_thread(self.store.finish_agent_run, result)
        if result.status != "completed":
            raise AgentExecutionError(result.error or f"Agent {agent_name} 执行失败")
        return result


def sub_agent_idempotency_key(
    *,
    root_run_id: str,
    parent_run_id: str,
    agent_name: str,
    event_id: str,
    task: str,
    extra_context: dict[str, Any] | None,
) -> str:
    """生成同一父任务内的 sub-agent 幂等键。"""

    return stable_hash(
        "sub-agent",
        root_run_id,
        parent_run_id,
        agent_name,
        event_id,
        task,
        extra_context or {},
    )
