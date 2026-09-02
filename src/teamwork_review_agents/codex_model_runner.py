"""由 Teamwork 托管工具与 Agent 循环的 Codex 模型运行器。"""

from __future__ import annotations

import asyncio
import json
import os
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import suppress
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, SchemaError, ValidationError

from .agent_home import TemporaryAgentHome, cleanup_stale_agent_homes_once
from .codex_model_client import (
    CodexOAuthError,
    CodexOAuthStore,
    CodexResponsesClient,
    CodexUpstreamError,
)
from .codex_settings import (
    codex_home,
    read_user_inherited_settings,
    read_user_model,
    validate_codex_version,
)
from .config import AgentConfig, AppConfig, RepositoryConfig
from .environment import SecretRedactor
from .model_provider_client import ExternalModelClient, ModelProviderRequestError
from .model_provider_credentials import ModelProviderCredentialStore
from .model_provider_runtime import (
    ResolvedModelSelection,
    effective_agent_config,
    resolve_model_plan,
    supports_reasoning_effort,
)
from .managed_sandbox import inspect_managed_sandbox
from .model_tools import (
    CancelCheck,
    InvokeAgentCallback,
    ModelToolExecutor,
    teamwork_function_tools,
)
from .models import AgentResult, InvocationContext
from .skill_files import SkillProjection
from .subprocess_utils import (
    WINDOWS_REQUIRED_ENVIRONMENT_NAMES,
    remove_environment_names,
    selected_environment,
)


LogCallback = Callable[[str, str, str | dict[str, Any]], Awaitable[None]]
ModelSnapshotCallback = Callable[[dict[str, Any]], Awaitable[None]]
_MAX_TOOL_ROUNDS = 64
_BASE_ENVIRONMENT_NAMES = {
    "PATH",
    "HOME",
    "USER",
    "LOGNAME",
    "SHELL",
    "LANG",
    "LC_ALL",
    "TMPDIR",
    "CODEX_HOME",
    "GH_CONFIG_DIR",
    "GLAB_CONFIG_DIR",
    "GIT_CONFIG_GLOBAL",
    "SSH_AUTH_SOCK",
    "SSH_AGENT_PID",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
} | WINDOWS_REQUIRED_ENVIRONMENT_NAMES


class CodexModelRunner:
    """直接调用 Codex 模型，由 Teamwork 执行函数工具。"""

    def __init__(
        self,
        config: AppConfig,
        *,
        provider_id: str = "codex-cli",
        invoke_agent_callback: InvokeAgentCallback | None = None,
    ) -> None:
        self.config = config
        self.provider_id = provider_id
        provider = config.model_providers.get(provider_id)
        if provider is None:
            raise KeyError(provider_id)
        self.provider = provider
        self._uses_codex_model_base = (
            provider.driver == "codex_cli"
            and config.runtime.codex.execution_mode == "model"
        )
        self.invoke_agent_callback = invoke_agent_callback
        self.credential_store = ModelProviderCredentialStore(
            config.database.path.parent / "model-provider-credentials"
        )
        self._provider_semaphores: dict[str, asyncio.Semaphore | None] = {
            provider_id: (
                asyncio.Semaphore(provider.max_concurrency)
                if provider.max_concurrency is not None
                else None
            )
            for provider_id, provider in config.model_providers.items()
        }
        cleanup_stale_agent_homes_once()

    async def run(
        self,
        *,
        run_id: str,
        root_run_id: str,
        parent_run_id: str | None,
        agent_name: str,
        agent: AgentConfig,
        repository: RepositoryConfig,
        context: InvocationContext,
        prompt: str,
        process_environment: dict[str, str] | None = None,
        redactor: SecretRedactor | None = None,
        log_callback: LogCallback | None = None,
        cancel_check: CancelCheck | None = None,
        model_plan: Sequence[ResolvedModelSelection] | None = None,
        model_snapshot_callback: ModelSnapshotCallback | None = None,
    ) -> AgentResult:
        """准备投影与临时 HOME，然后执行带看门狗的模型工具循环。"""

        resolved_plan = tuple(model_plan or resolve_model_plan(self.config, agent).selections)
        inherited_secrets = list((redactor or SecretRedactor(())).secret_values)
        for selection in resolved_plan:
            if selection.provider.driver == "codex_cli":
                continue
            try:
                provider_api_key = self.credential_store.reveal(selection.provider_id)
            except KeyError:
                continue
            inherited_secrets.append(provider_api_key)
        active_redactor = SecretRedactor(inherited_secrets)

        async def emit(
            stream: str,
            event_type: str,
            payload: str | dict[str, Any],
        ) -> None:
            """日志写入失败不能反向中断 Agent。"""

            if log_callback is None:
                return
            try:
                redacted = (
                    active_redactor.text(payload)
                    if isinstance(payload, str)
                    else active_redactor.data(payload)
                )
                await log_callback(stream, event_type, redacted)
            except Exception:
                return

        temporary_home: TemporaryAgentHome | None = None
        tool_runtime_home: TemporaryAgentHome | None = None
        projection: SkillProjection | None = None
        try:
            if agent.home_mode == "temporary":
                temporary_home = TemporaryAgentHome.create(run_id)
            tool_runtime_home = TemporaryAgentHome.create(f"{run_id}-model-tools")
            tool_codex_home = tool_runtime_home.path / "codex-home"
            tool_codex_home.mkdir(mode=0o700)
            projection = SkillProjection(
                repository.workspace,
                {
                    skill_id: skill.path
                    for skill_id, skill in self.config.skills.items()
                },
                self.config.revision,
            ).prepare()
            environment = self.child_environment(
                process_environment,
                temporary_home=temporary_home,
                tool_codex_home=tool_codex_home,
            )
            if temporary_home is not None:
                await emit(
                    "system",
                    "run.home_prepared",
                    {
                        "mode": "temporary",
                        "path": str(temporary_home.path),
                        "bridges": list(temporary_home.bridges),
                    },
                )
            if projection.marker.is_file():
                _add_git_excludes_file(environment, projection.marker)

            if any(
                item.provider.driver == "codex_cli"
                and self.config.runtime.codex.execution_mode == "model"
                for item in resolved_plan
            ):
                version_error = await asyncio.to_thread(
                    validate_codex_version,
                    self.config.runtime.codex_binary,
                    self.config.runtime.expected_codex_version,
                    self.config.runtime.codex_home,
                )
                if version_error:
                    await emit("system", "run.version_mismatch", version_error)
                    return _failed_result(
                        run_id,
                        root_run_id,
                        parent_run_id,
                        agent_name,
                        version_error,
                    )

            managed_sandbox = False
            if agent.sandbox != "danger-full-access":
                if not self.config.runtime.managed_sandbox.enabled:
                    error = (
                        "模型 Provider 驱动下，受限 Agent 必须启用 Teamwork 外层沙盒"
                    )
                    await emit("system", "run.sandbox_unavailable", error)
                    return _failed_result(
                        run_id,
                        root_run_id,
                        parent_run_id,
                        agent_name,
                        error,
                    )
                inspection = await asyncio.to_thread(
                    inspect_managed_sandbox,
                    self.config.runtime.codex_binary,
                    self.config.runtime.codex_home,
                )
                if not inspection.available:
                    error = inspection.error or "Teamwork 外层沙盒能力不可用"
                    await emit(
                        "system",
                        "run.sandbox_unavailable",
                        {
                            "platform": inspection.platform,
                            "backend": inspection.backend,
                            "error": error,
                            "fail_closed": True,
                            "execution_mode": "model",
                        },
                    )
                    return _failed_result(
                        run_id,
                        root_run_id,
                        parent_run_id,
                        agent_name,
                        error,
                    )
                managed_sandbox = True
                await emit(
                    "system",
                    "run.sandbox_prepared",
                    {
                        "mode": agent.sandbox,
                        "managed": True,
                        "platform": inspection.platform,
                        "backend": inspection.backend,
                        "network_mode": (
                            "disabled"
                            if not agent.network_access
                            else "allowlist"
                            if agent.network_domains
                            else "full"
                        ),
                        "network_domain_count": len(agent.network_domains),
                        "execution_mode": "model",
                    },
                )
            else:
                await emit(
                    "system",
                    "run.sandbox_prepared",
                    {
                        "mode": agent.sandbox,
                        "managed": False,
                        "backend": None,
                        "network_mode": "full",
                        "execution_mode": "model",
                    },
                )

            return await self._run_guarded(
                run_id=run_id,
                root_run_id=root_run_id,
                parent_run_id=parent_run_id,
                agent_name=agent_name,
                agent=agent,
                repository=repository,
                context=context,
                prompt=prompt,
                environment=environment,
                codex_runtime_directory=tool_codex_home,
                skill_files=projection.skill_files,
                managed_sandbox=managed_sandbox,
                redactor=active_redactor,
                emit=emit,
                cancel_check=cancel_check,
                model_plan=resolved_plan,
                model_snapshot_callback=model_snapshot_callback,
            )
        finally:
            try:
                if projection is not None:
                    projection.cleanup()
            finally:
                try:
                    if tool_runtime_home is not None:
                        tool_runtime_home.cleanup()
                finally:
                    if temporary_home is not None:
                        home_path = str(temporary_home.path)
                        cleanup_error = temporary_home.cleanup()
                        await emit(
                            "system",
                            (
                                "run.home_cleanup_failed"
                                if cleanup_error
                                else "run.home_cleaned"
                            ),
                            {
                                "mode": "temporary",
                                "path": home_path,
                                "cleaned": cleanup_error is None,
                                **({"error": cleanup_error} if cleanup_error else {}),
                            },
                        )

    def child_environment(
        self,
        agent_environment: dict[str, str] | None,
        *,
        temporary_home: TemporaryAgentHome | None,
        tool_codex_home: Path,
    ) -> dict[str, str]:
        """只给工具进程继承明确允许的宿主和 Agent 环境。"""

        environment = selected_environment(_BASE_ENVIRONMENT_NAMES)
        environment.update(agent_environment or {})
        real_codex_home = codex_home(self.config.runtime.codex_home)
        if temporary_home is not None:
            temporary_home.apply_environment(
                environment,
                codex_home=real_codex_home,
            )
        # 模型客户端单独读取真实登录；工具只看见空的本轮 Codex 运行目录。
        environment["CODEX_HOME"] = str(tool_codex_home)
        # 模型凭据始终隔离；Provider Token 仅在用户显式允许时由 Agent 环境传入。
        remove_environment_names(
            environment,
            (
                "CODEX_API_KEY",
                "OPENAI_API_KEY",
            ),
        )
        environment["PYTHONUNBUFFERED"] = "1"
        return environment

    async def _run_guarded(
        self,
        *,
        run_id: str,
        root_run_id: str,
        parent_run_id: str | None,
        agent_name: str,
        agent: AgentConfig,
        repository: RepositoryConfig,
        context: InvocationContext,
        prompt: str,
        environment: dict[str, str],
        codex_runtime_directory: Path,
        skill_files: Mapping[str, Path],
        managed_sandbox: bool,
        redactor: SecretRedactor,
        emit: LogCallback,
        cancel_check: CancelCheck | None,
        model_plan: Sequence[ResolvedModelSelection],
        model_snapshot_callback: ModelSnapshotCallback | None,
    ) -> AgentResult:
        """用统一看门狗覆盖模型请求、命令和 sub-agent 等待。"""

        started_at = time.monotonic()
        last_progress_at = started_at
        stop_reason: str | None = None
        idle_timeout = (
            agent.idle_timeout_seconds
            or self.config.runtime.agent_idle_timeout_seconds
        )

        def progress() -> None:
            """模型事件和工具输出都视为本轮语义进展。"""

            nonlocal last_progress_at
            last_progress_at = time.monotonic()

        main_task = asyncio.create_task(
            self._agent_loop(
                run_id=run_id,
                root_run_id=root_run_id,
                parent_run_id=parent_run_id,
                agent_name=agent_name,
                agent=agent,
                repository=repository,
                context=context,
                prompt=prompt,
                environment=environment,
                codex_runtime_directory=codex_runtime_directory,
                skill_files=skill_files,
                managed_sandbox=managed_sandbox,
                redactor=redactor,
                emit=emit,
                cancel_check=cancel_check,
                progress=progress,
                model_plan=model_plan,
                model_snapshot_callback=model_snapshot_callback,
            )
        )

        async def watchdog() -> None:
            """轮询持久化取消、总时限与无进展时限。"""

            nonlocal stop_reason
            while not main_task.done():
                await asyncio.sleep(0.25)
                now = time.monotonic()
                if cancel_check is not None:
                    try:
                        if await cancel_check():
                            stop_reason = "cancelled"
                    except Exception:
                        pass
                if stop_reason is None and now - started_at >= agent.timeout_seconds:
                    stop_reason = "total_timeout"
                if stop_reason is None and now - last_progress_at >= idle_timeout:
                    stop_reason = "idle_timeout"
                if stop_reason is not None:
                    main_task.cancel()
                    return

        watchdog_task = asyncio.create_task(watchdog())
        try:
            return await main_task
        except asyncio.CancelledError:
            if stop_reason is None:
                stop_reason = "cancelled"
            if stop_reason == "total_timeout":
                await emit("system", "run.timed_out", "Agent 超过总运行时限")
                status = "timed_out"
                error = "Agent 超过总运行时限"
            elif stop_reason == "idle_timeout":
                await emit("system", "run.idle_timed_out", "Agent 长时间没有模型或工具进展")
                status = "timed_out"
                error = "Agent 长时间没有模型或工具进展"
            else:
                await emit("system", "run.cancelled", "运行已由管理员取消")
                status = "cancelled"
                error = "运行已由管理员取消"
            return AgentResult(
                run_id=run_id,
                root_run_id=root_run_id,
                parent_run_id=parent_run_id,
                agent_name=agent_name,
                status=status,
                error=error,
            )
        except Exception as exc:
            error = redactor.text(str(exc))
            await emit("system", "error", error)
            return _failed_result(
                run_id,
                root_run_id,
                parent_run_id,
                agent_name,
                error,
            )
        finally:
            watchdog_task.cancel()
            await asyncio.gather(watchdog_task, return_exceptions=True)

    async def _agent_loop(
        self,
        *,
        run_id: str,
        root_run_id: str,
        parent_run_id: str | None,
        agent_name: str,
        agent: AgentConfig,
        repository: RepositoryConfig,
        context: InvocationContext,
        prompt: str,
        environment: dict[str, str],
        codex_runtime_directory: Path,
        skill_files: Mapping[str, Path],
        managed_sandbox: bool,
        redactor: SecretRedactor,
        emit: LogCallback,
        cancel_check: CancelCheck | None,
        progress: Callable[[], None],
        model_plan: Sequence[ResolvedModelSelection],
        model_snapshot_callback: ModelSnapshotCallback | None,
    ) -> AgentResult:
        """执行 Responses/function_call_output 多轮循环。"""

        schema: dict[str, Any] | None = None
        text_config: dict[str, Any] = {}
        model = ""
        reasoning_effort: str | None = None
        reasoning_effort_source = "provider_default"
        fast_mode = False
        verbosity: str | None = None
        personality: str | None = None
        instructions = ""
        client: Any = None
        current_selection: ResolvedModelSelection | None = None
        current_agent = agent
        current_index = -1
        attempts: list[dict[str, Any]] = []
        round_had_model_events = False

        def fallbackable_error(error: Exception) -> bool:
            """只把 Provider 暂时不可用类错误交给回退链。"""

            if isinstance(error, ModelProviderRequestError):
                if error.fallbackable is not None:
                    return error.fallbackable
                return False
            if isinstance(error, CodexUpstreamError):
                if error.fallbackable is not None:
                    return error.fallbackable
                return error.status_code in {
                    401,
                    402,
                    403,
                    404,
                    408,
                    409,
                    429,
                } or (
                    error.status_code is not None
                    and error.status_code >= 500
                )
            if isinstance(error, CodexOAuthError):
                return True
            return False

        def selection_identity(selection: ResolvedModelSelection) -> str:
            """返回日志中使用的无密钥模型身份。"""

            return f"{selection.provider_id}/{selection.model or 'provider-default'}"

        async def activate(index: int) -> bool:
            """切换到下一个可用候选，并为其创建协议客户端。"""

            nonlocal client, current_selection, current_index
            nonlocal current_agent, model, reasoning_effort, reasoning_effort_source, fast_mode
            nonlocal verbosity, personality, instructions, text_config, schema
            while index < len(model_plan):
                selection = model_plan[index]
                if not selection.provider.enabled:
                    attempts.append(
                        {
                            "provider_id": selection.provider_id,
                            "model": selection.model,
                            "status": "skipped",
                            "reason": "provider_disabled",
                        }
                    )
                    index += 1
                    continue
                if not selection.model:
                    attempts.append(
                        {
                            "provider_id": selection.provider_id,
                            "model": None,
                            "status": "skipped",
                            "reason": selection.unresolved_reason or "model_unresolved",
                        }
                    )
                    index += 1
                    continue
                if (
                    selection.provider.driver == "codex_cli"
                    and self.config.runtime.codex.execution_mode == "cli"
                ):
                    # 完整 Codex CLI 是不透明子进程，不能在内嵌模型循环中动态切入。
                    attempts.append(
                        {
                            "provider_id": selection.provider_id,
                            "model": selection.model,
                            "status": "skipped",
                            "reason": "codex_cli_opaque_mode",
                        }
                    )
                    index += 1
                    continue
                current_selection = selection
                current_index = index
                current_agent = effective_agent_config(
                    self.config,
                    agent,
                    selection,
                )
                model, reasoning_effort, fast_mode, verbosity, personality = (
                    self._settings_for_provider(
                        current_agent,
                        selection.provider,
                        codex_model_base=(
                            selection.provider.driver == "codex_cli"
                            and self.config.runtime.codex.execution_mode == "model"
                        ),
                    )
                )
                if reasoning_effort:
                    reasoning_effort_source = (
                        "model_selection"
                        if selection.reasoning_effort
                        else "agent"
                        if agent.model_reasoning_effort
                        else "provider"
                        if selection.provider.model_reasoning_effort
                        else "runtime"
                        if selection.provider.driver == "codex_cli"
                        else "provider_default"
                    )
                else:
                    reasoning_effort_source = (
                        "unsupported"
                        if selection.model and not supports_reasoning_effort(
                            selection.provider,
                            selection.model,
                        )
                        else "provider_default"
                    )
                schema, text_config = _response_text_config(
                    current_agent.output_schema,
                    verbosity,
                )
                instructions = _instructions(
                    repository=repository,
                    agent=current_agent,
                    personality=personality,
                    skill_files=skill_files,
                    provider_name=selection.provider.display_name,
                )
                uses_codex_model_base = (
                    selection.provider.driver == "codex_cli"
                    and self.config.runtime.codex.execution_mode == "model"
                )
                if uses_codex_model_base:
                    oauth = CodexOAuthStore(codex_home(self.config.runtime.codex_home))
                    client = CodexResponsesClient(
                        oauth=oauth,
                        codex_binary=self.config.runtime.codex_binary,
                        timeout_seconds=float(agent.timeout_seconds),
                        idle_timeout_seconds=float(
                            agent.idle_timeout_seconds
                            or self.config.runtime.agent_idle_timeout_seconds
                        ),
                    )
                else:
                    try:
                        api_key = self.credential_store.reveal(selection.provider_id)
                    except KeyError as exc:
                        attempts.append(
                            {
                                "provider_id": selection.provider_id,
                                "model": selection.model,
                                "status": "failed",
                                "reason": "missing_api_key",
                            }
                        )
                        await emit(
                            "system",
                            "model.attempt_failed",
                            {
                                "provider_id": selection.provider_id,
                                "model": selection.model,
                                "reason": "Provider 尚未配置 API Key",
                            },
                        )
                        index += 1
                        continue
                    client = ExternalModelClient(
                        selection.provider,
                        api_key,
                        timeout_seconds=float(
                            min(
                                agent.timeout_seconds,
                                selection.provider.request_timeout_seconds,
                            )
                        ),
                        idle_timeout_seconds=float(
                            agent.idle_timeout_seconds
                            or self.config.runtime.agent_idle_timeout_seconds
                        ),
                    )
                return True
            return False

        if not await activate(0):
            raise RuntimeError("模型主链与回退链没有可用的 Provider/模型")
        tools = teamwork_function_tools(
            allow_sub_agents=bool(agent.allowed_sub_agents),
            allow_publish_comment=agent.managed_comment,
        )
        tool_executor = ModelToolExecutor(
            config=self.config,
            agent=agent,
            repository=repository,
            context=context,
            environment=environment,
            managed_sandbox=managed_sandbox,
            cancel_check=cancel_check,
            progress_callback=progress,
            invoke_agent_callback=self.invoke_agent_callback,
            codex_runtime_directory=codex_runtime_directory,
        )
        history: list[dict[str, Any]] = [
            {
                "role": "user",
                "content": [{"type": "input_text", "text": prompt}],
            }
        ]
        usage: dict[str, Any] = {}
        events: list[dict[str, Any]] = []
        response_id: str | None = None
        thread_started = False
        round_message_keys: set[str] = set()
        round_message_parts: list[str] = []

        await emit(
            "system",
            "turn.started",
            {
                "execution_mode": "model",
                "provider_id": current_selection.provider_id if current_selection else None,
                "provider_driver": current_selection.provider.driver if current_selection else None,
                "model": model,
                "reasoning_effort": reasoning_effort,
                "reasoning_effort_source": reasoning_effort_source,
                "tool_count": len(tools),
                "skill_count": len(agent.skills),
            },
        )

        async def receive_event(event: dict[str, Any]) -> None:
            """用 SSE 事件续期，并只持久化不含加密思维链的安全摘要。"""

            nonlocal thread_started, round_had_model_events
            progress()
            round_had_model_events = True
            event_type = str(event.get("type") or "response.event")
            if event_type == "response.created":
                response = event.get("response")
                created_id = response.get("id") if isinstance(response, dict) else None
                if created_id and not thread_started:
                    thread_started = True
                    await emit("stdout", "thread.started", {"thread_id": created_id})
            if event_type == "response.output_item.done":
                item = event.get("item")
                if isinstance(item, dict):
                    message_text = _response_item_text(item)
                    message_key = str(item.get("id") or message_text)
                    if message_text and message_key not in round_message_keys:
                        round_message_keys.add(message_key)
                        round_message_parts.append(message_text)
                        message_event = {
                            "type": "item.completed",
                            "item": {
                                "type": "agent_message",
                                "text": message_text,
                            },
                        }
                        if len(events) < self.config.runtime.max_jsonl_events:
                            events.append(redactor.data(message_event))
                        await emit("stdout", "item.completed", message_event)
            if event_type in {
                "response.created",
                "response.output_item.done",
                "response.completed",
            }:
                safe = _safe_response_event(event)
                if len(events) < self.config.runtime.max_jsonl_events:
                    events.append(redactor.data(safe))

        for round_index in range(_MAX_TOOL_ROUNDS):
            while True:
                payload: dict[str, Any] = {
                    "model": model,
                    "instructions": instructions,
                    "input": history,
                    "tools": tools,
                    "tool_choice": "auto",
                    "parallel_tool_calls": False,
                    "stream": True,
                    "store": False,
                }
                if reasoning_effort:
                    payload["reasoning"] = {
                        "effort": reasoning_effort,
                        "summary": "auto",
                    }
                if (
                    current_selection is not None
                    and current_selection.provider.driver == "codex_cli"
                    and self.config.runtime.codex.execution_mode == "model"
                ):
                    payload["include"] = ["reasoning.encrypted_content"]
                if fast_mode:
                    payload["service_tier"] = "priority"
                if text_config:
                    payload["text"] = text_config
                round_message_keys.clear()
                round_message_parts.clear()
                round_had_model_events = False
                try:
                    provider_semaphore = self._semaphore_for(
                        current_selection.provider_id if current_selection else self.provider_id,
                        current_selection.provider if current_selection else self.provider,
                    )
                    if provider_semaphore is None:
                        response = await client.create_response(
                            payload,
                            event_callback=receive_event,
                        )
                    else:
                        async with provider_semaphore:
                            response = await client.create_response(
                                payload,
                                event_callback=receive_event,
                            )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    failed_selection = current_selection
                    failure_payload = {
                        "provider_id": (
                            failed_selection.provider_id
                            if failed_selection is not None
                            else self.provider_id
                        ),
                        "model": model,
                        "reasoning_effort": reasoning_effort,
                        "reasoning_effort_source": reasoning_effort_source,
                        "status": "failed",
                        "reason": redactor.text(str(exc)),
                    }
                    attempts.append(failure_payload)
                    await emit("system", "model.attempt_failed", failure_payload)
                    if model_snapshot_callback is not None:
                        await model_snapshot_callback(
                            _model_snapshot_update(
                                model_plan,
                                attempts,
                                current_selection=failed_selection,
                                reasoning_effort=reasoning_effort,
                                reasoning_effort_source=reasoning_effort_source,
                                fallback_used=bool(attempts),
                            )
                        )
                    if (
                        not round_had_model_events
                        and fallbackable_error(exc)
                        and await activate(current_index + 1)
                    ):
                        next_selection = current_selection
                        await emit(
                            "system",
                            "model.fallback",
                            {
                                "from": selection_identity(failed_selection)
                                if failed_selection is not None
                                else None,
                                "to": selection_identity(next_selection)
                                if next_selection is not None
                                else None,
                                "reason": redactor.text(str(exc)),
                            },
                        )
                        if model_snapshot_callback is not None:
                            await model_snapshot_callback(
                                _model_snapshot_update(
                                    model_plan,
                                    attempts,
                                    current_selection=next_selection,
                                    reasoning_effort=reasoning_effort,
                                    reasoning_effort_source=reasoning_effort_source,
                                    fallback_used=True,
                                )
                            )
                        continue
                    raise
                attempts.append(
                    {
                        "provider_id": current_selection.provider_id
                        if current_selection is not None
                        else self.provider_id,
                        "model": model,
                        "reasoning_effort": reasoning_effort,
                        "reasoning_effort_source": reasoning_effort_source,
                        "status": "response",
                    }
                )
                if model_snapshot_callback is not None:
                    await model_snapshot_callback(
                        _model_snapshot_update(
                            model_plan,
                            attempts,
                            current_selection=current_selection,
                            reasoning_effort=reasoning_effort,
                            reasoning_effort_source=reasoning_effort_source,
                            fallback_used=any(
                                item.get("status") == "failed" for item in attempts
                            ),
                        )
                    )
                break
            response_id = str(response.get("id") or response_id or "") or None
            response_usage = response.get("usage")
            if isinstance(response_usage, dict):
                usage = _merge_usage(usage, response_usage)
            output = response.get("output")
            if not isinstance(output, list):
                output = []
            history.extend(item for item in output if isinstance(item, dict))
            calls = [
                item
                for item in output
                if isinstance(item, dict) and item.get("type") == "function_call"
            ]
            if not calls:
                final_message = _response_text(response)
                if not final_message:
                    raise RuntimeError("Codex 模型回合没有返回最终消息或函数调用")
                if schema is not None:
                    _validate_output_schema(final_message, schema)
                if final_message != "".join(round_message_parts):
                    final_event = {
                        "type": "item.completed",
                        "item": {"type": "agent_message", "text": final_message},
                    }
                    if len(events) < self.config.runtime.max_jsonl_events:
                        events.append(redactor.data(final_event))
                    await emit("stdout", "item.completed", final_event)
                await emit("stdout", "turn.completed", {"usage": usage})
                return AgentResult(
                    run_id=run_id,
                    root_run_id=root_run_id,
                    parent_run_id=parent_run_id,
                    agent_name=agent_name,
                    status="completed",
                    final_message=final_message,
                    thread_id=response_id,
                    usage=usage,
                    events=events,
                )

            for call in calls:
                name = str(call.get("name") or "")
                call_id = str(call.get("call_id") or call.get("id") or "")
                if not name or not call_id:
                    raise RuntimeError("Codex 函数调用缺少 name 或 call_id")
                arguments = _function_arguments(call.get("arguments"))
                started_item = _tool_log_item(
                    name,
                    arguments,
                    started=True,
                    call_id=call_id,
                )
                await emit("stdout", "item.started", {"item": started_item})

                async def report_invoke_agent_started(
                    linked_run: dict[str, Any],
                ) -> None:
                    """把运行中的 sub-agent 精确关联更新到同一工具调用。"""

                    linked_item = _tool_log_item(
                        name,
                        arguments,
                        started=True,
                        call_id=call_id,
                        linked_run=linked_run,
                    )
                    linked_event = {"type": "item.updated", "item": linked_item}
                    if len(events) < self.config.runtime.max_jsonl_events:
                        events.append(redactor.data(linked_event))
                    await emit("stdout", "item.updated", {"item": linked_item})

                try:
                    tool_result = await tool_executor.execute(
                        name,
                        arguments,
                        invoke_agent_started_callback=(
                            report_invoke_agent_started
                            if name == "invoke_agent"
                            else None
                        ),
                    )
                    output_text = json.dumps(tool_result, ensure_ascii=False)
                    completed_item = _tool_log_item(
                        name,
                        arguments,
                        started=False,
                        call_id=call_id,
                        result=redactor.data(tool_result),
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    error = redactor.text(str(exc))
                    output_text = json.dumps(
                        {"error": error, "error_type": type(exc).__name__},
                        ensure_ascii=False,
                    )
                    completed_item = _tool_log_item(
                        name,
                        arguments,
                        started=False,
                        call_id=call_id,
                        error=error,
                    )
                progress()
                completed_event = {"item": completed_item}
                if len(events) < self.config.runtime.max_jsonl_events:
                    events.append(
                        redactor.data({"type": "item.completed", **completed_event})
                    )
                await emit("stdout", "item.completed", completed_event)
                history.append(
                    {
                        "type": "function_call_output",
                        "call_id": call_id,
                        "output": output_text,
                    }
                )

        raise RuntimeError(f"Codex 模型工具调用超过 {_MAX_TOOL_ROUNDS} 轮")

    def _settings(
        self,
        agent: AgentConfig,
    ) -> tuple[str, str | None, bool, str | None, str | None]:
        """按 Agent、Teamwork 和 Codex 用户配置顺序解析模型参数。"""

        return self._settings_for_provider(
            agent,
            self.provider,
            codex_model_base=self._uses_codex_model_base,
        )

    def _semaphore_for(
        self,
        provider_id: str,
        provider: Any,
    ) -> asyncio.Semaphore | None:
        """返回指定 Provider 的并发信号量。"""

        if provider_id not in self._provider_semaphores:
            self._provider_semaphores[provider_id] = (
                asyncio.Semaphore(provider.max_concurrency)
                if provider.max_concurrency is not None
                else None
            )
        return self._provider_semaphores[provider_id]

    def _settings_for_provider(
        self,
        agent: AgentConfig,
        provider: Any,
        *,
        codex_model_base: bool,
    ) -> tuple[str, str | None, bool, str | None, str | None]:
        """按候选 Provider 的配置解析本次模型参数。"""

        home = codex_home(self.config.runtime.codex_home)
        user_model, _, _ = read_user_model(home)
        model = agent.model or provider.default_model
        if model is None and provider.models:
            model = provider.models[0]
        if model is None and codex_model_base:
            model = self.config.runtime.codex.model or user_model
        if not model:
            raise RuntimeError(
                f"模型 Provider {provider.display_name} 没有可解析的模型"
            )
        inherited, _ = read_user_inherited_settings(home)

        def inherited_value(name: str) -> str | None:
            item = inherited.get(name)
            value = item.get("value") if isinstance(item, dict) else None
            return value if isinstance(value, str) and value else None

        codex_provider = codex_model_base
        reasoning = agent.model_reasoning_effort or provider.model_reasoning_effort
        if not supports_reasoning_effort(provider, model):
            reasoning = None
        if reasoning is None and codex_provider:
            reasoning = (
                self.config.runtime.codex.model_reasoning_effort
                or inherited_value("model_reasoning_effort")
            )
        if reasoning is None and codex_provider:
            reasoning = "medium"
        fast_setting = agent.fast_mode
        if fast_setting == "inherit" and codex_provider:
            fast_setting = self.config.runtime.codex.fast_mode
        if fast_setting == "inherit" and codex_provider:
            fast_setting = inherited_value("fast_mode") or "standard"
        if fast_setting == "inherit":
            fast_setting = "standard"
        verbosity = agent.model_verbosity or provider.model_verbosity
        if verbosity is None and codex_provider:
            verbosity = (
                self.config.runtime.codex.model_verbosity
                or inherited_value("model_verbosity")
            )
        personality = agent.personality or provider.personality
        if personality is None and codex_provider:
            personality = self.config.runtime.codex.personality
        return model, reasoning, fast_setting == "fast", verbosity, personality


def _model_snapshot_update(
    model_plan: Sequence[ResolvedModelSelection],
    attempts: list[dict[str, Any]],
    *,
    current_selection: ResolvedModelSelection | None,
    reasoning_effort: str | None,
    reasoning_effort_source: str,
    fallback_used: bool,
) -> dict[str, Any]:
    """生成回退过程中的有界模型快照，不包含任何凭据。"""

    snapshot = {
        "execution_mode": "model",
        "provider_id": current_selection.provider_id if current_selection else None,
        "provider_name": current_selection.provider.display_name
        if current_selection
        else None,
        "provider_driver": current_selection.provider.driver
        if current_selection
        else None,
        "provider_enabled": current_selection.provider.enabled
        if current_selection
        else None,
        "model": current_selection.model if current_selection else None,
        "model_source": current_selection.model_source
        if current_selection
        else None,
        "resolved_label": current_selection.resolved_label
        if current_selection
        else None,
        "reasoning_effort": reasoning_effort,
        "reasoning_effort_source": reasoning_effort_source,
        "fallback_plan": [
            {
                "provider_id": item.provider_id,
                "provider_name": item.provider.display_name,
                "provider_driver": item.provider.driver,
                "provider_enabled": item.provider.enabled,
                "model": item.model,
                "model_source": item.model_source,
                "resolved_label": item.resolved_label,
                "reasoning_effort": (
                    item.reasoning_effort
                    if supports_reasoning_effort(item.provider, item.model)
                    else None
                ),
                "reasoning_effort_source": (
                    "model_selection"
                    if item.reasoning_effort and supports_reasoning_effort(item.provider, item.model)
                    else "unsupported"
                    if item.model and not supports_reasoning_effort(item.provider, item.model)
                    else "provider_default"
                ),
                "unresolved_reason": item.unresolved_reason,
            }
            for item in model_plan
        ],
        "fallback_attempts": attempts[-32:],
        "fallback_used": fallback_used,
    }
    return snapshot


def _failed_result(
    run_id: str,
    root_run_id: str,
    parent_run_id: str | None,
    agent_name: str,
    error: str,
) -> AgentResult:
    """构造内嵌运行器统一失败结果。"""

    return AgentResult(
        run_id=run_id,
        root_run_id=root_run_id,
        parent_run_id=parent_run_id,
        agent_name=agent_name,
        status="failed",
        error=error,
    )


def _instructions(
    *,
    repository: RepositoryConfig,
    agent: AgentConfig,
    personality: str | None,
    skill_files: Mapping[str, Path],
    provider_name: str = "Codex",
) -> str:
    """构造 Teamwork 运行时边界并内联已选 Skill 指令。"""

    sections = [
        f"你是由 Teamwork 编排的编码 Agent，当前模型由 {provider_name} 提供。",
        (
            "Teamwork 提供工作区和全部可用工具；不要假设存在 Codex CLI 内置工具、"
            "用户 MCP、浏览器或未列出的能力。"
        ),
        f"当前工作区根目录：{repository.workspace}",
        (
            "execute_command 用于检查和运行命令；apply_patch 只接受标准 unified diff；"
            "invoke_agent 只能调用配置白名单中的 sub-agent。"
        ),
        "完成前应验证实际改动，并在最终消息中明确结果、验证和阻断项。",
    ]
    if personality and personality != "none":
        sections.append(f"交互风格：{personality}。")
    selected = set(agent.skills)
    for skill_id, path in sorted(skill_files.items()):
        if skill_id not in selected:
            continue
        content = path.read_text(encoding="utf-8")
        sections.append(
            "\n".join(
                [
                    f"# 已启用 Skill：{skill_id}",
                    f"投影文件：{path}",
                    content,
                ]
            )
        )
    return "\n\n".join(sections)


def _response_text_config(
    output_schema: Path | None,
    verbosity: str | None,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """加载输出 Schema 并生成 Responses text 配置。"""

    text: dict[str, Any] = {}
    schema: dict[str, Any] | None = None
    if verbosity:
        text["verbosity"] = verbosity
    if output_schema is not None:
        try:
            document = json.loads(output_schema.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"无法读取 Agent 输出 Schema：{exc}") from exc
        if not isinstance(document, dict):
            raise RuntimeError("Agent 输出 Schema 顶层必须是 JSON 对象")
        try:
            Draft202012Validator.check_schema(document)
        except SchemaError as exc:
            raise RuntimeError(f"Agent 输出 Schema 无效：{exc.message}") from exc
        schema = document
        text["format"] = {
            "type": "json_schema",
            "name": "teamwork_agent_output",
            "schema": document,
            "strict": True,
        }
    return schema, text


def _validate_output_schema(final_message: str, schema: dict[str, Any]) -> None:
    """再次在本地校验模型最终 JSON，防止上游忽略格式约束。"""

    try:
        document = json.loads(final_message)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Agent 最终消息不是输出 Schema 要求的 JSON") from exc
    try:
        Draft202012Validator(schema).validate(document)
    except ValidationError as exc:
        raise RuntimeError(f"Agent 最终消息不符合输出 Schema：{exc.message}") from exc


def _function_arguments(value: Any) -> dict[str, Any]:
    """解析 Responses 函数调用参数。"""

    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        raise ValueError("Codex 函数调用 arguments 不是 JSON 对象")
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError("Codex 函数调用 arguments 不是有效 JSON") from exc
    if not isinstance(parsed, dict):
        raise ValueError("Codex 函数调用 arguments 顶层必须是对象")
    return parsed


def _response_text(response: dict[str, Any]) -> str:
    """从 completed response 中提取最终文本。"""

    direct = response.get("output_text")
    if isinstance(direct, str) and direct:
        return direct
    parts: list[str] = []
    output = response.get("output")
    if not isinstance(output, list):
        return ""
    for item in output:
        if isinstance(item, dict):
            parts.append(_response_item_text(item))
    return "".join(parts)


def _response_item_text(item: dict[str, Any]) -> str:
    """从 Responses message item 中提取可展示的 Agent 文本。"""

    if item.get("type") != "message":
        return ""
    content = item.get("content")
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") in {"output_text", "text"} and isinstance(
            block.get("text"), str
        ):
            parts.append(block["text"])
    return "".join(parts)


def _merge_usage(current: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    """递归归并多轮 Responses 用量数字。"""

    merged = dict(current)
    for key, value in new.items():
        existing = merged.get(key)
        if isinstance(value, dict):
            merged[key] = _merge_usage(existing if isinstance(existing, dict) else {}, value)
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            merged[key] = (
                existing + value
                if isinstance(existing, (int, float)) and not isinstance(existing, bool)
                else value
            )
        else:
            merged[key] = value
    return merged


def _safe_response_event(event: dict[str, Any]) -> dict[str, Any]:
    """裁剪 SSE 事件，绝不记录 encrypted_content 或完整 completed 响应。"""

    event_type = str(event.get("type") or "response.event")
    response = event.get("response")
    if event_type == "response.completed" and isinstance(response, dict):
        return {
            "type": event_type,
            "response": {
                "id": response.get("id"),
                "status": response.get("status"),
                "usage": response.get("usage"),
            },
        }

    def clean(value: Any) -> Any:
        """递归删除上游加密思维和疑似认证字段。"""

        if isinstance(value, list):
            return [clean(item) for item in value]
        if not isinstance(value, dict):
            return value
        return {
            key: clean(item)
            for key, item in value.items()
            if key.lower()
            not in {
                "encrypted_content",
                "access_token",
                "refresh_token",
                "authorization",
            }
        }

    return clean(event)


def _tool_log_item(
    name: str,
    arguments: dict[str, Any],
    *,
    started: bool,
    call_id: str,
    result: dict[str, Any] | None = None,
    error: str | None = None,
    linked_run: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """把 Teamwork 工具映射为现有运行日志可展示的 Codex item。"""

    if name == "execute_command":
        item: dict[str, Any] = {
            "type": "command_execution",
            "call_id": call_id,
            "command": arguments.get("command", ""),
            "status": "in_progress" if started else "failed" if error else "completed",
        }
        if not started:
            item["exit_code"] = result.get("exit_code") if result else None
            item["aggregated_output"] = (
                "\n".join(
                    part
                    for part in (
                        str(result.get("stdout") or "") if result else "",
                        str(result.get("stderr") or "") if result else "",
                    )
                    if part
                )
                if not error
                else error
            )
        return item
    if name == "apply_patch":
        return {
            "type": "file_change",
            "call_id": call_id,
            "status": "in_progress" if started else "failed" if error else "completed",
            "changes": [] if started else (result or {"error": error}),
        }
    return {
        "type": "mcp_tool_call",
        "call_id": call_id,
        "server": "teamwork_runtime",
        "tool": name,
        "status": "in_progress" if started else "failed" if error else "completed",
        "arguments": arguments,
        **({"linked_run": linked_run} if linked_run is not None else {}),
        **({"result": result} if result is not None else {}),
        **({"error": error} if error else {}),
    }


def _add_git_excludes_file(environment: dict[str, str], path: Path) -> None:
    """让工具进程忽略本轮 Skill 投影，不修改仓库配置。"""

    try:
        count = int(environment.get("GIT_CONFIG_COUNT", "0"))
    except ValueError:
        count = 0
    environment["GIT_CONFIG_COUNT"] = str(count + 1)
    environment[f"GIT_CONFIG_KEY_{count}"] = "core.excludesFile"
    environment[f"GIT_CONFIG_VALUE_{count}"] = str(path)
