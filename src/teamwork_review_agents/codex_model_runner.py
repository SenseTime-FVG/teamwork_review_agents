"""由 Teamwork 托管工具与 Agent 循环的 Codex 模型运行器。"""

from __future__ import annotations

import asyncio
import json
import os
import time
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, SchemaError, ValidationError

from .agent_home import TemporaryAgentHome, cleanup_stale_agent_homes_once
from .codex_model_client import (
    CodexOAuthStore,
    CodexResponsesClient,
)
from .codex_settings import (
    codex_home,
    read_user_inherited_settings,
    read_user_model,
    validate_codex_version,
)
from .config import AgentConfig, AppConfig, RepositoryConfig
from .environment import SecretRedactor
from .managed_sandbox import inspect_managed_sandbox
from .model_tools import (
    CancelCheck,
    InvokeAgentCallback,
    ModelToolExecutor,
    teamwork_function_tools,
)
from .models import AgentResult, InvocationContext
from .skill_files import SkillProjection


LogCallback = Callable[[str, str, str | dict[str, Any]], Awaitable[None]]
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
}


class CodexModelRunner:
    """直接调用 Codex 模型，由 Teamwork 执行函数工具。"""

    def __init__(
        self,
        config: AppConfig,
        *,
        invoke_agent_callback: InvokeAgentCallback | None = None,
    ) -> None:
        self.config = config
        self.invoke_agent_callback = invoke_agent_callback
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
    ) -> AgentResult:
        """准备投影与临时 HOME，然后执行带看门狗的模型工具循环。"""

        active_redactor = redactor or SecretRedactor(())

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
                        "Codex 模型基座模式下，受限 Agent 必须启用 Teamwork 外层沙盒"
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

        environment = {
            name: value
            for name, value in os.environ.items()
            if name in _BASE_ENVIRONMENT_NAMES
        }
        environment.update(agent_environment or {})
        real_codex_home = codex_home(self.config.runtime.codex_home)
        if temporary_home is not None:
            temporary_home.apply_environment(
                environment,
                codex_home=real_codex_home,
            )
        # 模型客户端单独读取真实登录；工具只看见空的本轮 Codex 运行目录。
        environment["CODEX_HOME"] = str(tool_codex_home)
        # OAuth 与 Provider 凭据都不能进入 Teamwork 工具子进程。
        environment.pop("CODEX_API_KEY", None)
        environment.pop("OPENAI_API_KEY", None)
        for provider in self.config.providers.values():
            environment.pop(provider.token_env, None)
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
    ) -> AgentResult:
        """执行 Responses/function_call_output 多轮循环。"""

        model, reasoning_effort, fast_mode, verbosity, personality = self._settings(agent)
        schema, text_config = _response_text_config(agent.output_schema, verbosity)
        instructions = _instructions(
            repository=repository,
            agent=agent,
            personality=personality,
            skill_files=skill_files,
        )
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
        tools = teamwork_function_tools(
            allow_sub_agents=bool(agent.allowed_sub_agents)
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

        await emit(
            "system",
            "turn.started",
            {
                "execution_mode": "model",
                "model": model,
                "tool_count": len(tools),
                "skill_count": len(agent.skills),
            },
        )

        async def receive_event(event: dict[str, Any]) -> None:
            """用 SSE 事件续期，并只持久化不含加密思维链的安全摘要。"""

            progress()
            event_type = str(event.get("type") or "response.event")
            if event_type == "response.created":
                response = event.get("response")
                created_id = response.get("id") if isinstance(response, dict) else None
                if created_id:
                    await emit("stdout", "thread.started", {"thread_id": created_id})
            if event_type in {
                "response.created",
                "response.output_item.done",
                "response.completed",
            }:
                safe = _safe_response_event(event)
                if len(events) < self.config.runtime.max_jsonl_events:
                    events.append(redactor.data(safe))
                if event_type != "response.completed":
                    await emit("stdout", event_type, safe)

        for round_index in range(_MAX_TOOL_ROUNDS):
            payload: dict[str, Any] = {
                "model": model,
                "instructions": instructions,
                "input": history,
                "tools": tools,
                "tool_choice": "auto",
                "parallel_tool_calls": False,
                "stream": True,
                "store": False,
                "reasoning": {
                    "effort": reasoning_effort,
                    "summary": "auto",
                },
                "include": ["reasoning.encrypted_content"],
            }
            if fast_mode:
                payload["service_tier"] = "priority"
            if text_config:
                payload["text"] = text_config
            response = await client.create_response(
                payload,
                event_callback=receive_event,
            )
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
                started_item = _tool_log_item(name, arguments, started=True)
                await emit("stdout", "item.started", {"item": started_item})
                try:
                    tool_result = await tool_executor.execute(name, arguments)
                    output_text = json.dumps(tool_result, ensure_ascii=False)
                    completed_item = _tool_log_item(
                        name,
                        arguments,
                        started=False,
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
    ) -> tuple[str, str, bool, str | None, str | None]:
        """按 Agent、Teamwork 和 Codex 用户配置顺序解析模型参数。"""

        home = codex_home(self.config.runtime.codex_home)
        user_model, _, _ = read_user_model(home)
        model = agent.model or self.config.runtime.codex.model or user_model
        if not model:
            raise RuntimeError(
                "Codex 模型基座模式需要在 Agent、runtime.codex.model 或 Codex config.toml 中配置模型"
            )
        inherited, _ = read_user_inherited_settings(home)

        def inherited_value(name: str) -> str | None:
            item = inherited.get(name)
            value = item.get("value") if isinstance(item, dict) else None
            return value if isinstance(value, str) and value else None

        reasoning = (
            agent.model_reasoning_effort
            or self.config.runtime.codex.model_reasoning_effort
            or inherited_value("model_reasoning_effort")
            or "medium"
        )
        fast_setting = agent.fast_mode
        if fast_setting == "inherit":
            fast_setting = self.config.runtime.codex.fast_mode
        if fast_setting == "inherit":
            fast_setting = inherited_value("fast_mode") or "standard"
        verbosity = (
            agent.model_verbosity
            or self.config.runtime.codex.model_verbosity
            or inherited_value("model_verbosity")
        )
        personality = agent.personality or self.config.runtime.codex.personality
        return model, reasoning, fast_setting == "fast", verbosity, personality


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
) -> str:
    """构造 Teamwork 运行时边界并内联已选 Skill 指令。"""

    sections = [
        "你是 Codex 编码 Agent，但当前只使用 Codex 模型基座。",
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
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        content = item.get("content")
        if not isinstance(content, list):
            continue
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
    result: dict[str, Any] | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    """把 Teamwork 工具映射为现有运行日志可展示的 Codex item。"""

    if name == "execute_command":
        item: dict[str, Any] = {
            "type": "command_execution",
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
            "status": "in_progress" if started else "failed" if error else "completed",
            "changes": [] if started else (result or {"error": error}),
        }
    return {
        "type": "mcp_tool_call",
        "server": "teamwork_runtime",
        "tool": name,
        "status": "in_progress" if started else "failed" if error else "completed",
        "arguments": arguments,
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
