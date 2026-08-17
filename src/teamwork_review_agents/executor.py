"""Agent 提示词组装、幂等运行和资源锁编排。"""

from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path
from typing import Any

from .codex_runner import CodexRunner
from .config import AppConfig, RepositoryConfig
from .environment import SecretRedactor, render_prompt, resolve_environment
from .locks import ResourceLease
from .models import AgentResult, ChangeEvent, InvocationContext, stable_hash
from .state import StateStore


class AgentExecutionError(RuntimeError):
    """表示 Agent 配置、限额、资源或 Codex 执行失败。"""


def event_payload(event: ChangeEvent) -> dict[str, Any]:
    """返回适合提示词使用且不包含冗余平台原始响应的事件。"""

    return event.model_dump(
        mode="json",
        exclude={"old": {"raw"}, "new": {"raw"}},
    )


class AgentExecutor:
    """统一执行根 Agent 和 MCP 调起的 sub-agent。"""

    def __init__(self, config: AppConfig, store: StateStore) -> None:
        self.config = config
        self.store = store
        self.runner = CodexRunner(config)
        self.repositories = config.repository_map()

    def build_prompt(
        self,
        *,
        agent_name: str,
        event: ChangeEvent,
        repository: RepositoryConfig,
        rule_name: str | None,
        task: str | None,
        extra_context: dict[str, Any] | None,
        prompt_values: dict[str, str],
    ) -> str:
        """组合 Agent 固定角色、触发上下文和临时委托任务。"""

        agent = self.config.agents[agent_name]
        if agent.prompt_file:
            template = agent.prompt_file.read_text(encoding="utf-8")
        else:
            template = agent.prompt or ""
        role_prompt = render_prompt(template, prompt_values).strip()
        context = {
            "repository": {
                "id": repository.id,
                "project": repository.project,
                "workspace": str(repository.workspace),
                "provider": repository.provider,
            },
            "rule": rule_name,
            "event": event_payload(event),
            "delegated_task": task,
            "extra_context": extra_context or {},
            "allowed_sub_agents": agent.allowed_sub_agents,
        }
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
        """按 Agent 声明生成变更请求级和工作目录级写锁。"""

        scopes = self.config.agents[agent_name].write_scopes
        keys: list[str] = []
        if "change_request" in scopes:
            keys.append(f"change_request:{event.resource_key}")
        if "workspace" in scopes:
            keys.append(f"workspace:{Path(repository.workspace).resolve()}")
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

        repository = self.repositories[event.repository_id]
        if not repository.workspace.is_dir():
            raise AgentExecutionError(f"工作目录不存在：{repository.workspace}")
        proposed_run_id = str(uuid.uuid4())
        agent = self.config.agents[agent_name]
        resolved_environment = resolve_environment(
            self.config,
            repository,
            agent,
            event,
            proposed_run_id,
        )
        redactor = SecretRedactor(resolved_environment.secret_values)
        prompt = self.build_prompt(
            agent_name=agent_name,
            event=event,
            repository=repository,
            rule_name=rule_name,
            task=task,
            extra_context=extra_context,
            prompt_values=resolved_environment.prompt_values,
        )
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
            prompt=redactor.text(prompt),
            environment=resolved_environment.audit_values,
            config_revision=self.config.revision,
            max_attempts=self.config.runtime.event_retry_count + 1,
        )
        if reservation is None:
            status = await asyncio.to_thread(
                self.store.agent_run_status,
                idempotency_key,
            )
            if status in {"completed", "running"}:
                return None
            raise AgentExecutionError(
                f"幂等任务已经达到重试上限，当前状态：{status or 'unknown'}"
            )

        if reservation.run_id != proposed_run_id:
            resolved_environment = resolve_environment(
                self.config,
                repository,
                agent,
                event,
                reservation.run_id,
            )
            redactor = SecretRedactor(resolved_environment.secret_values)
            prompt = self.build_prompt(
                agent_name=agent_name,
                event=event,
                repository=repository,
                rule_name=rule_name,
                task=task,
                extra_context=extra_context,
                prompt_values=resolved_environment.prompt_values,
            )
            await asyncio.to_thread(
                self.store.update_agent_run_inputs,
                reservation.run_id,
                prompt=redactor.text(prompt),
                environment=resolved_environment.audit_values,
                config_revision=self.config.revision,
            )

        context = InvocationContext(
            config_path=str(self.config.config_path),
            current_agent=agent_name,
            run_id=reservation.run_id,
            root_run_id=reservation.root_run_id,
            depth=depth,
            call_chain=(*call_chain, agent_name),
            event=event,
        )
        keys = self.lock_keys(agent_name, event, repository)
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

        await persist_log(
            "system",
            "run.started",
            {
                "agent_name": agent_name,
                "config_revision": self.config.revision,
                "environment": resolved_environment.audit_values,
            },
        )
        try:
            async with lease:
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
            await persist_log("system", "run.failed", error)
            result = AgentResult(
                run_id=reservation.run_id,
                root_run_id=reservation.root_run_id,
                parent_run_id=reservation.parent_run_id,
                agent_name=agent_name,
                status="failed",
                error=error,
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
