"""Agent 提示词组装、幂等运行和资源锁编排。"""

from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path
from typing import Any, Sequence

from .codex_runner import CodexRunner
from .config import AppConfig, RepositoryConfig
from .environment import SecretRedactor, render_prompt, resolve_environment
from .locks import ResourceLease
from .models import AgentResult, ChangeEvent, InvocationContext, stable_hash
from .state import StateStore
from .workspace import (
    change_request_ref,
    cleanup_expired_worktrees,
    cleanup_run_worktree,
    ensure_isolated_worktree,
    mark_active_worktree,
    prepare_change_request_workspace,
    validate_linked_workspace,
    worktree_head,
    worktree_starting_head,
)


class AgentExecutionError(RuntimeError):
    """表示 Agent 配置、限额、资源或 Codex 执行失败。"""


def _action_name(event_type: str) -> str:
    """将内部完整事件名转换为给 Agent 使用的短动作名。"""

    return event_type.removeprefix("change_request.")


def _repository_payload(
    repository: RepositoryConfig,
) -> dict[str, Any]:
    """返回根 Agent 与 sub-agent 共用的仓库上下文。"""

    return {
        "id": repository.id,
        "project": repository.project,
        "provider": repository.provider,
        "workspace": str(repository.workspace),
    }


def _mr_payload(
    event: ChangeEvent,
    repository: RepositoryConfig,
    actions: Sequence[str],
    change_ref: str,
) -> dict[str, Any]:
    """返回根 Agent 所需的统一 MR / PR 当前信息。"""

    snapshot = event.current_snapshot
    return {
        "repository": _repository_payload(repository),
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
    }


class AgentExecutor:
    """统一执行根 Agent 和 MCP 调起的 sub-agent。"""

    def __init__(self, config: AppConfig, store: StateStore) -> None:
        self.config = config
        self.store = store
        self.runner = CodexRunner(config)
        self.repositories = config.repository_map()

    def run_workspace_path(
        self,
        repository: RepositoryConfig,
        run_id: str,
    ) -> Path:
        """返回一次 Agent 运行专属的临时 worktree 路径。"""

        repository_directory = stable_hash(repository.id)[:16]
        return (
            self.config.database.path.parent
            / "worktrees"
            / repository_directory
            / run_id
        ).resolve()

    def git_admin_lock_key(self, repository: RepositoryConfig) -> str:
        """返回基础仓库 fetch 与 worktree 管理使用的短时锁。"""

        return f"git_repository:{repository.workspace.resolve()}"

    def build_prompt(
        self,
        *,
        agent_name: str,
        event: ChangeEvent,
        repository: RepositoryConfig,
        task: str | None,
        extra_context: dict[str, Any] | None,
        prompt_values: dict[str, str],
        change_ref: str,
        actions: Sequence[str],
    ) -> str:
        """组合 Agent 固定角色、触发上下文和临时委托任务。"""

        agent = self.config.agents[agent_name]
        if agent.prompt_file:
            template = agent.prompt_file.read_text(encoding="utf-8")
        else:
            template = agent.prompt or ""
        role_prompt = render_prompt(template, prompt_values).strip()
        if task is None:
            context: dict[str, Any] = {
                "mr": _mr_payload(event, repository, actions, change_ref),
            }
        else:
            context = {
                "repository": _repository_payload(repository),
                "delegated_task": task,
            }
            if extra_context:
                context["delegated_context"] = extra_context
        return (
            f"{role_prompt}\n\n"
            "# 本次运行上下文\n\n"
            f"```json\n{json.dumps(context, ensure_ascii=False, indent=2)}\n```\n\n"
            "如果工具列表中存在 `invoke_agent`，只能在确有必要时调用配置允许的 "
            "sub-agent，并向它传递边界清晰的任务。最终必须明确说明实际执行的操作、"
            "验证结果和仍存在的阻断项。"
        )

    def lock_keys(
        self,
        agent_name: str,
        event: ChangeEvent,
        repository: RepositoryConfig,
    ) -> list[str]:
        """按 Agent 声明生成变更请求级和源分支级写锁。"""

        scopes = self.config.agents[agent_name].write_scopes
        keys: list[str] = []
        if "change_request" in scopes:
            keys.append(f"change_request:{event.resource_key}")
        if "workspace" in scopes:
            keys.append(
                "repository_branch:"
                f"{event.provider}:{repository.id}:{event.current_snapshot.source_branch}"
            )
        return keys

    async def execute(
        self,
        *,
        agent_name: str,
        event: ChangeEvent,
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
    ) -> AgentResult | None:
        """申请审计记录与写锁，然后运行一个 Codex CLI Agent。"""

        if agent_name not in self.config.agents:
            raise AgentExecutionError(f"不存在 Agent：{agent_name}")
        if event.repository_id not in self.repositories:
            raise AgentExecutionError(f"不存在仓库：{event.repository_id}")
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

        configured_repository = self.repositories[event.repository_id]
        provider = self.config.providers[configured_repository.provider]
        agent = self.config.agents[agent_name]
        proposed_run_id = str(uuid.uuid4())
        reservation = await asyncio.to_thread(
            self.store.begin_agent_run,
            proposed_run_id=proposed_run_id,
            root_run_id=root_run_id,
            parent_run_id=parent_run_id,
            idempotency_key=idempotency_key,
            event_id=event.id,
            rule_name=rule_name,
            agent_name=agent_name,
            resource_key=event.resource_key,
            prompt="",
            environment={},
            config_revision=self.config.revision,
            max_attempts=self.config.runtime.event_retry_count + 1,
        )
        if reservation is None:
            status = await asyncio.to_thread(
                self.store.agent_run_status,
                idempotency_key,
            )
            if status in {"completed", "queued", "running"}:
                return None
            raise AgentExecutionError(
                f"幂等任务已经达到重试上限，当前状态：{status or 'unknown'}"
            )

        keys = self.lock_keys(agent_name, event, configured_repository)
        lease = ResourceLease(
            self.store,
            keys,
            reservation.root_run_id,
            ttl_seconds=self.config.runtime.lock_ttl_seconds,
            timeout_seconds=self.config.runtime.lock_timeout_seconds,
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
        starting_head = event.current_snapshot.head_sha
        result: AgentResult
        try:
            async with lease:
                if task is not None and inherit_workspace:
                    if parent_workspace is None:
                        raise AgentExecutionError(
                            "工作区继承已开启，但父 Agent 工作目录缺失"
                        )
                    active_workspace = await asyncio.to_thread(
                        validate_linked_workspace,
                        configured_repository.workspace,
                        parent_workspace,
                    )
                    change_ref = change_request_ref(provider, event.number)[1]
                    workspace_mode = "inherited"
                    workspace_reason = "复用父 Agent 本次运行的临时 worktree"
                else:
                    active_workspace = self.run_workspace_path(
                        configured_repository,
                        reservation.run_id,
                    )
                    git_admin_lease = ResourceLease(
                        self.store,
                        [self.git_admin_lock_key(configured_repository)],
                        reservation.run_id,
                        ttl_seconds=self.config.runtime.lock_ttl_seconds,
                        timeout_seconds=self.config.runtime.lock_timeout_seconds,
                    )
                    async with git_admin_lease:
                        change_ref = await asyncio.to_thread(
                            prepare_change_request_workspace,
                            provider,
                            configured_repository,
                            event.current_snapshot,
                        )
                        await asyncio.to_thread(
                            cleanup_expired_worktrees,
                            configured_repository.workspace,
                            active_workspace.parent,
                        )
                        original_starting_head = await asyncio.to_thread(
                            worktree_starting_head,
                            active_workspace,
                        )
                        active_workspace = await asyncio.to_thread(
                            ensure_isolated_worktree,
                            configured_repository.workspace,
                            active_workspace,
                            change_ref,
                        )
                        starting_head = original_starting_head or await asyncio.to_thread(
                            worktree_head,
                            active_workspace,
                        )
                        await asyncio.to_thread(
                            mark_active_worktree,
                            active_workspace,
                            starting_head=starting_head,
                            retention_days=self.config.runtime.worktree_retention_days,
                            timeout_seconds=agent.timeout_seconds,
                        )
                    owned_workspace = True
                    workspace_mode = (
                        "root-isolated" if task is None else "sub-agent-isolated"
                    )
                    workspace_reason = "本次 Agent 运行独享临时 worktree"

                repository = configured_repository.model_copy(
                    update={"workspace": active_workspace},
                )
                resolved_environment = resolve_environment(
                    self.config,
                    repository,
                    agent,
                    event,
                    reservation.run_id,
                    include_change_request=task is None,
                )
                redactor = SecretRedactor(resolved_environment.secret_values)
                effective_actions = tuple(actions or (event.type,))
                prompt = self.build_prompt(
                    agent_name=agent_name,
                    event=event,
                    repository=repository,
                    task=task,
                    extra_context=extra_context,
                    prompt_values=resolved_environment.prompt_values,
                    change_ref=change_ref,
                    actions=effective_actions,
                )
                await asyncio.to_thread(
                    self.store.update_agent_run_inputs,
                    reservation.run_id,
                    prompt=redactor.text(prompt),
                    environment=resolved_environment.audit_values,
                    config_revision=self.config.revision,
                )
                await asyncio.to_thread(
                    self.store.update_agent_run_workspace,
                    reservation.run_id,
                    path=str(active_workspace),
                    status="inherited" if not owned_workspace else "active",
                    reason=workspace_reason,
                )
                workspace_prepared = True
                await persist_log(
                    "system",
                    "workspace.prepared",
                    {
                        "mode": workspace_mode,
                        "path": str(active_workspace),
                        "reason": workspace_reason,
                    },
                )
                await asyncio.to_thread(
                    self.store.mark_agent_run_running,
                    reservation.run_id,
                )
                await persist_log(
                    "system",
                    "run.started",
                    {
                        "agent_name": agent_name,
                        "config_revision": self.config.revision,
                        "environment": resolved_environment.audit_values,
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
                )
                result = await self.runner.run(
                    run_id=reservation.run_id,
                    root_run_id=reservation.root_run_id,
                    parent_run_id=reservation.parent_run_id,
                    agent_name=agent_name,
                    agent=agent,
                    repository=repository,
                    context=context,
                    prompt=prompt,
                    process_environment=resolved_environment.process_values,
                    redactor=redactor,
                    log_callback=persist_log,
                )
                if lease.lost:
                    result.status = "failed"
                    result.error = "运行期间写资源租约丢失，结果不再视为可信"
        except Exception as exc:
            error = redactor.text(str(exc))
            result = AgentResult(
                run_id=reservation.run_id,
                root_run_id=reservation.root_run_id,
                parent_run_id=reservation.parent_run_id,
                agent_name=agent_name,
                status="failed",
                error=error,
            )

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
