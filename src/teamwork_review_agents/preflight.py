"""确定性 CI 前置检查、输出边界与幂等执行。"""

from __future__ import annotations

import asyncio
import html
import os
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, Literal

from .config import AgentWorkspaceConfig, AppConfig, PreflightConfig
from .environment import SecretRedactor, resolve_provider_token
from .filesystem import temporary_directory
from .managed_comments import ManagedCommentService
from .models import ChangeEvent, PreflightResult, stable_hash
from .preflight_cache import (
    build_repository_cache_environment,
    repository_cache_root,
)
from .process_control import process_group_options, terminate_process
from .providers import create_provider
from .state import StateStore
from .subprocess_utils import (
    WINDOWS_REQUIRED_ENVIRONMENT_NAMES,
    resolve_executable,
    selected_environment,
)
from .workspace import (
    GitProgressEvent,
    WorkspaceSnapshotSuperseded,
    temporary_change_request_worktree,
)


SAFE_HOST_ENVIRONMENT = {
    "PATH",
    "USER",
    "LOGNAME",
    "SHELL",
    "LANG",
    "LC_ALL",
    "TMPDIR",
    "TMP",
    "TEMP",
    "SYSTEMROOT",
    "COMSPEC",
    "PATHEXT",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "REQUESTS_CA_BUNDLE",
    "CURL_CA_BUNDLE",
    "UV_LINK_MODE",
    "SSH_AUTH_SOCK",
    "SSH_AGENT_PID",
} | WINDOWS_REQUIRED_ENVIRONMENT_NAMES
ANSI_ESCAPE_PATTERN = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
PREFLIGHT_COMMENT_OUTPUT_CHARS = 12_000


@dataclass(frozen=True)
class StepExecutionOutcome:
    """一组 CI 步骤的确定性执行结果。"""

    status: Literal["success", "failure", "timed_out", "error", "cancelled"]
    failed_step: str | None = None
    exit_code: int | None = None
    output: str = ""
    error: str | None = None


@dataclass(frozen=True)
class PreflightStepUpdate:
    """一次 CI 步骤状态变化。"""

    step_index: int
    status: Literal[
        "running",
        "success",
        "failure",
        "timed_out",
        "error",
        "cancelled",
    ]
    timeout_seconds: float | None = None
    exit_code: int | None = None
    error: str | None = None


StepUpdateCallback = Callable[[PreflightStepUpdate], Awaitable[None]]
OutputCallback = Callable[[str], Awaitable[None]]
CancelCheck = Callable[[], bool]
CommandWrapper = Callable[[list[str], Path], list[str]]


async def _emit_step_update(
    callback: StepUpdateCallback | None,
    update: PreflightStepUpdate,
) -> None:
    """存在记录器时串行持久化步骤变化。"""

    if callback is not None:
        await callback(update)


def build_preflight_environment(
    *,
    home: Path | None = None,
    cache_environment: dict[str, str] | None = None,
    windows: bool | None = None,
) -> dict[str, str]:
    """只继承运行工具所需的宿主机变量，不传递平台或模型凭据。"""

    environment = selected_environment(SAFE_HOST_ENVIRONMENT)
    environment.update(
        {
            "CI": "true",
            "TEAMWORK_PREFLIGHT": "1",
            "PYTHONUNBUFFERED": "1",
            "PYTHONUTF8": "1",
            "PYTHONIOENCODING": "utf-8",
        }
    )
    if home is not None:
        environment["HOME"] = str(home)
        active_windows = os.name == "nt" if windows is None else windows
        if active_windows:
            roaming = home / "AppData/Roaming"
            local = home / "AppData/Local"
            temporary = home / "tmp"
            for directory in (roaming, local, temporary):
                directory.mkdir(parents=True, exist_ok=True)
            environment["USERPROFILE"] = str(home)
            environment["APPDATA"] = str(roaming)
            environment["LOCALAPPDATA"] = str(local)
            environment["TEMP"] = str(temporary)
            environment["TMP"] = str(temporary)
    if cache_environment:
        environment.update(cache_environment)
    return environment


def _append_bounded_output(current: str, addition: bytes, limit: int) -> str:
    """按 UTF-8 字节上限保留最新输出，避免无界日志进入 SQLite。"""

    encoded = current.encode("utf-8") + addition
    if len(encoded) > limit:
        encoded = encoded[-limit:]
    return encoded.decode("utf-8", errors="ignore")


async def _terminate_process(process: asyncio.subprocess.Process) -> None:
    """请求终止超时步骤及其子进程组，不在此处无界等待退出。"""

    try:
        # 主进程可能已退出，但继承输出管道的后代仍必须被完整回收。
        terminate_process(process.pid, force=True, tree=True)
    except (ProcessLookupError, PermissionError):
        pass


async def _read_bounded_stream(
    stream: asyncio.StreamReader,
    limit: int,
    latest: list[str] | None = None,
    on_output: OutputCallback | None = None,
) -> str:
    """持续排空子进程输出，但内存中只保留最新的有界内容。"""

    output = ""
    while chunk := await stream.read(65536):
        output = _append_bounded_output(output, chunk, limit)
        if latest is not None:
            latest[:] = [output]
        if on_output is not None:
            try:
                await on_output(chunk.decode("utf-8", errors="replace"))
            except Exception:
                # 实时日志写入失败不能改变被测命令本身的执行结果。
                pass
    return output


async def execute_preflight_steps(
    config: PreflightConfig | AgentWorkspaceConfig,
    *,
    cwd: Path,
    environment: dict[str, str],
    on_step_update: StepUpdateCallback | None = None,
    on_output: OutputCallback | None = None,
    cancel_check: CancelCheck | None = None,
    unlimited: bool = False,
    command_wrapper: CommandWrapper | None = None,
    operation_name: str = "Preflight",
    cancellation_message: str = "用户取消了手动 CI",
) -> StepExecutionOutcome:
    """按配置顺序执行参数数组步骤，并支持目录、输出、取消和超时。"""

    loop = asyncio.get_running_loop()
    deadline = None if unlimited else loop.time() + config.timeout_seconds
    output = ""
    steps = (
        config.prepare_steps
        if isinstance(config, AgentWorkspaceConfig)
        else config.steps
    )
    resolved_cwd = cwd.resolve()
    for step_index, step in enumerate(steps):
        if cancel_check is not None and cancel_check():
            return StepExecutionOutcome(
                status="cancelled",
                failed_step=step.name,
                output=output,
                error=cancellation_message,
            )
        remaining = None if deadline is None else deadline - loop.time()
        if remaining is not None and remaining <= 0:
            error = f"{operation_name} 总运行时间超时"
            await _emit_step_update(
                on_step_update,
                PreflightStepUpdate(
                    step_index=step_index,
                    status="timed_out",
                    timeout_seconds=0,
                    error=error,
                ),
            )
            return StepExecutionOutcome(
                status="timed_out",
                failed_step=step.name,
                output=output,
                error=error,
            )
        step_timeout = (
            None
            if unlimited
            else min(float(step.timeout_seconds or remaining), float(remaining))
        )
        heading = f"\n[{step.name}]\n"
        output = _append_bounded_output(
            output, heading.encode("utf-8"), config.max_output_bytes
        )
        if on_output is not None:
            await on_output(heading)
        await _emit_step_update(
            on_step_update,
            PreflightStepUpdate(
                step_index=step_index,
                status="running",
                timeout_seconds=step_timeout,
            ),
        )
        try:
            configured_cwd = getattr(step, "cwd", ".")
            step_cwd = (resolved_cwd / configured_cwd).resolve()
            try:
                step_cwd.relative_to(resolved_cwd)
            except ValueError as exc:
                raise ValueError(
                    f"步骤 {step.name} 的工作目录逃逸了 Agent 工作区"
                ) from exc
            if not step_cwd.is_dir():
                raise ValueError(
                    f"步骤 {step.name} 的工作目录不存在：{configured_cwd}"
                )
            command = [
                resolve_executable(step.command[0], environment),
                *step.command[1:],
            ]
            if command_wrapper is not None:
                command = command_wrapper(command, step_cwd)
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=step_cwd,
                env=environment,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                **process_group_options(),
            )
        except (OSError, ValueError) as exc:
            await _emit_step_update(
                on_step_update,
                PreflightStepUpdate(
                    step_index=step_index,
                    status="error",
                    timeout_seconds=step_timeout,
                    error=str(exc),
                ),
            )
            return StepExecutionOutcome(
                status="error",
                failed_step=step.name,
                output=output,
                error=str(exc),
            )

        assert process.stdout is not None
        latest_step_output = [""]
        output_reader = asyncio.create_task(
            _read_bounded_stream(
                process.stdout,
                config.max_output_bytes,
                latest_step_output,
                on_output,
            )
        )
        process_waiter = asyncio.create_task(process.wait())
        timed_out = False
        cancelled = False
        step_deadline = (
            None if step_timeout is None else loop.time() + step_timeout
        )
        while not (process_waiter.done() and output_reader.done()):
            if cancel_check is not None and cancel_check():
                cancelled = True
                break
            if step_deadline is not None and loop.time() >= step_deadline:
                timed_out = True
                break
            wait_seconds = 0.25
            if step_deadline is not None:
                wait_seconds = min(
                    wait_seconds,
                    max(0.01, step_deadline - loop.time()),
                )
            # 只等待尚未完成的任务，避免 stdout 已关闭但进程仍收尾时空转。
            pending_tasks = {
                task
                for task in (process_waiter, output_reader)
                if not task.done()
            }
            await asyncio.wait(
                pending_tasks,
                timeout=wait_seconds,
                return_when=asyncio.FIRST_COMPLETED,
            )
        if timed_out or cancelled:
            await _terminate_process(process)
            cleanup_done, cleanup_pending = await asyncio.wait(
                {process_waiter, output_reader},
                timeout=1,
                return_when=asyncio.ALL_COMPLETED,
            )
            step_output = (
                output_reader.result()
                if output_reader in cleanup_done
                else latest_step_output[0]
            )
            if cleanup_pending:
                stdout_transport = getattr(process.stdout, "_transport", None)
                if stdout_transport is not None:
                    stdout_transport.close()
            for task in cleanup_pending:
                task.cancel()
            if cleanup_pending:
                await asyncio.gather(*cleanup_pending, return_exceptions=True)
                await asyncio.sleep(0)
            output = _append_bounded_output(
                output,
                step_output.encode("utf-8"),
                config.max_output_bytes,
            )
            if cancelled:
                error = cancellation_message
                terminal_status = "cancelled"
            else:
                error = f"步骤 {step.name} 超过 {step_timeout:.0f} 秒"
                terminal_status = "timed_out"
            await _emit_step_update(
                on_step_update,
                PreflightStepUpdate(
                    step_index=step_index,
                    status=terminal_status,
                    timeout_seconds=step_timeout,
                    error=error,
                ),
            )
            return StepExecutionOutcome(
                status=terminal_status,
                failed_step=step.name,
                output=output,
                error=error,
            )

        step_output = output_reader.result()
        output = _append_bounded_output(
            output,
            step_output.encode("utf-8"),
            config.max_output_bytes,
        )
        if process.returncode != 0:
            await _emit_step_update(
                on_step_update,
                PreflightStepUpdate(
                    step_index=step_index,
                    status="failure",
                    timeout_seconds=step_timeout,
                    exit_code=process.returncode,
                ),
            )
            return StepExecutionOutcome(
                status="failure",
                failed_step=step.name,
                exit_code=process.returncode,
                output=output,
            )
        await _emit_step_update(
            on_step_update,
            PreflightStepUpdate(
                step_index=step_index,
                status="success",
                timeout_seconds=step_timeout,
                exit_code=process.returncode,
            ),
        )

    return StepExecutionOutcome(status="success", output=output)


def preflight_idempotency_key(config: AppConfig, event: ChangeEvent) -> str:
    """按批次最终 Head 与配置版本返回 CI 幂等键。"""

    return stable_hash(
        "preflight",
        event.repository_id,
        event.number,
        event.current_snapshot.head_sha,
        config.revision,
    )


def _status_description(result: PreflightResult) -> str:
    """返回适合远端 Commit Status 的简短说明。"""

    if result.status == "success":
        return "本地 CI 全部通过"
    if result.status == "failure":
        suffix = "" if result.exit_code is None else f"，退出码 {result.exit_code}"
        return f"步骤 {result.failed_step or 'unknown'} 失败{suffix}"
    if result.status == "timed_out":
        return f"步骤 {result.failed_step or 'unknown'} 超时"
    if result.status == "superseded":
        return "事件 Head 已更新，本地 CI 已跳过"
    raise ValueError("基础设施异常没有可发布的 Commit Status")


def _bounded_comment_text(value: str, *, limit: int) -> str:
    """移除终端控制字符，并只保留适合评论展示的尾部内容。"""

    normalized = ANSI_ESCAPE_PATTERN.sub("", value).strip()
    if len(normalized) <= limit:
        return normalized
    return f"…（仅显示末尾 {limit} 个字符）\n{normalized[-limit:]}"


def _failure_comment_body(
    repository,
    result: PreflightResult,
    *,
    redactor: SecretRedactor,
) -> str:
    """构造有界且脱敏的 GitHub PR 本地 CI 失败评论。"""

    status_label = {
        "failure": "未通过",
        "timed_out": "超时",
    }.get(result.status, result.status)
    marker = stable_hash(
        "preflight-failure-comment",
        repository.id,
        result.number,
    )[:16]
    failed_step = redactor.text(result.failed_step or "unknown")
    error = _bounded_comment_text(
        redactor.text(result.error or ""),
        limit=2_000,
    )
    output = _bounded_comment_text(
        redactor.text(result.output),
        limit=PREFLIGHT_COMMENT_OUTPUT_CHARS,
    )
    sections = [
        f"<!-- teamwork-preflight-failure:{marker} -->",
        "## Teamwork 本地 CI 未通过",
        "",
        f"- 状态：**{html.escape(status_label)}**",
        f"- Head SHA：`{html.escape(result.head_sha)}`",
        f"- 失败步骤：`{html.escape(failed_step)}`",
        "- 退出码："
        + ("—" if result.exit_code is None else f"`{result.exit_code}`"),
    ]
    if error:
        sections.extend([
            "",
            "<details open>",
            "<summary>错误信息</summary>",
            "",
            f"<pre>{html.escape(error)}</pre>",
            "</details>",
        ])
    if output:
        sections.extend([
            "",
            "<details>",
            "<summary>末尾输出</summary>",
            "",
            f"<pre>{html.escape(output)}</pre>",
            "</details>",
        ])
    sections.extend([
        "",
        "> 后续真实 CI 再次失败时会删除本评论并在时间线底部发布最新结果；本地 CI 通过后会自动删除活动失败评论。",
    ])
    return "\n".join(sections)


class PreflightExecutor:
    """准备隔离 worktree、执行 CI，并持久化可复用结果。"""

    def __init__(self, config: AppConfig, store: StateStore) -> None:
        self.config = config
        self.store = store
        self.repositories = config.repository_map()

    async def _append_log(
        self,
        run_id: str,
        payload: str | dict[str, object],
        *,
        stream: str = "system",
        event_type: str = "message",
    ) -> None:
        """追加 CI 实时日志，日志故障不应覆盖实际检查结果。"""

        try:
            await asyncio.to_thread(
                self.store.append_preflight_log,
                run_id,
                stream=stream,
                event_type=event_type,
                payload=payload,
            )
        except Exception:
            pass

    async def _set_remote_status(
        self,
        repository,
        sha: str,
        *,
        state: str,
        description: str,
    ) -> None:
        """使用服务自身凭据发布状态，不把凭据交给被测命令。"""

        provider_config = self.config.providers[repository.provider]
        async with create_provider(
            repository.provider,
            provider_config,
            self.config.scanner,
            token=resolve_provider_token(self.config, provider_config, repository),
        ) as remote:
            await remote.set_commit_status(
                repository,
                sha,
                state=state,
                context=repository.preflight.status_context,
                description=description,
            )

    async def _sync_failure_comment(
        self,
        repository,
        result: PreflightResult,
        *,
        replace_existing: bool = True,
    ) -> None:
        """维护当前 PR 的单条活动 CI 失败评论。"""

        if (
            not repository.preflight.publish_failure_comment
            or result.number is None
        ):
            return
        service = ManagedCommentService(self.config, self.store)
        slot = repository.preflight.status_context
        if result.status == "success":
            current_generation = await asyncio.to_thread(
                self.store.source_generation,
                repository.id,
                result.number,
            )
            if (
                current_generation is not None
                and result.source_generation < current_generation
            ):
                return
            deleted = await service.delete_all(
                repository_id=repository.id,
                number=result.number,
                namespace="preflight",
                slot=slot,
            )
            if deleted:
                await self._append_log(
                    result.run_id,
                    f"本地 CI 已通过，已删除 {deleted} 条活动失败评论",
                    event_type="comment_deleted",
                )
            return
        if result.status not in {"failure", "timed_out"}:
            return

        provider_config = self.config.providers[repository.provider]
        token = resolve_provider_token(self.config, provider_config, repository)
        body = _failure_comment_body(
            repository,
            result,
            redactor=SecretRedactor((token,)),
        )
        synced = await service.publish_latest(
            repository_id=repository.id,
            number=result.number,
            namespace="preflight",
            slot=slot,
            source_generation=result.source_generation,
            source_head_sha=result.head_sha,
            body=body,
            replace_existing=replace_existing,
        )
        if synced["action"] == "preserved":
            return
        action = {
            "created": "创建",
            "replaced": "刷新",
        }[str(synced["action"])]
        await self._append_log(
            result.run_id,
            f"已{action}本地 CI 活动失败评论",
            event_type="comment_synced",
        )

    async def _sync_failure_comment_safely(
        self,
        repository,
        result: PreflightResult,
        *,
        replace_existing: bool = True,
    ) -> None:
        """隔离评论写回错误，确保平台权限问题不篡改 CI 真实终态。"""

        try:
            await self._sync_failure_comment(
                repository,
                result,
                replace_existing=replace_existing,
            )
        except Exception as exc:
            provider_config = self.config.providers[repository.provider]
            token = resolve_provider_token(self.config, provider_config, repository)
            error = SecretRedactor((token,)).text(str(exc))
            await self._append_log(
                result.run_id,
                f"本地 CI 评论写回失败：{error}",
                stream="stderr",
                event_type="comment_error",
            )

    async def _publish_terminal_result(
        self,
        repository,
        result: PreflightResult,
        *,
        replace_existing_comment: bool = True,
    ) -> PreflightResult:
        """只发布代码门禁终态；写回失败时保留本地真实结论。"""

        if result.status in {"error", "superseded"}:
            return result

        remote_state = {
            "success": "success",
            "failure": "failure",
            "timed_out": "failure",
        }[result.status]
        status_error: Exception | None = None
        try:
            await self._set_remote_status(
                repository,
                result.head_sha,
                state=remote_state,
                description=_status_description(result),
            )
        except Exception as exc:
            status_error = exc
        else:
            await asyncio.to_thread(
                self.store.mark_preflight_status_published,
                result.run_id,
            )
        await self._sync_failure_comment_safely(
            repository,
            result,
            replace_existing=replace_existing_comment,
        )
        if status_error is not None:
            return PreflightResult(
                run_id=result.run_id,
                repository_id=result.repository_id,
                number=result.number,
                head_sha=result.head_sha,
                source_generation=result.source_generation,
                status="error",
                error=f"本地 CI 已完成，但 GitHub 状态回写失败：{status_error}",
            )
        return result.model_copy(update={"status_published": True})

    async def ensure_passed(self, event: ChangeEvent) -> PreflightResult:
        """返回当前 Head 的已有终态，或执行一次新的 Preflight。"""

        repository = self.repositories[event.repository_id]
        provider_config = self.config.providers[repository.provider]
        snapshot = event.current_snapshot
        key = preflight_idempotency_key(self.config, event)
        cached = await asyncio.to_thread(self.store.load_preflight_result, key)
        if cached is not None and cached.status != "error":
            cached = cached.model_copy(
                update={"source_generation": event.source_generation}
            )
            if cached.status == "superseded":
                return cached.model_copy(update={"reused": True})
            if cached.status_published:
                await self._sync_failure_comment_safely(
                    repository,
                    cached,
                    replace_existing=False,
                )
                return cached.model_copy(update={"reused": True})
            published = await self._publish_terminal_result(
                repository,
                cached,
                replace_existing_comment=False,
            )
            return published.model_copy(update={"reused": True})

        proposed_run_id = str(uuid.uuid4())
        reservation = await asyncio.to_thread(
            self.store.begin_preflight_run,
            proposed_run_id=proposed_run_id,
            idempotency_key=key,
            event_id=event.id,
            repository_id=repository.id,
            number=event.number,
            head_sha=snapshot.head_sha,
            source_generation=event.source_generation,
            config_revision=self.config.revision,
            max_attempts=self.config.runtime.event_retry_count + 1,
            restart_exhausted_error=event.origin == "manual",
        )
        if reservation is None:
            current = await asyncio.to_thread(self.store.load_preflight_result, key)
            if current is not None:
                return current.model_copy(
                    update={
                        "reused": True,
                        "source_generation": event.source_generation,
                    }
                )
            return PreflightResult(
                run_id=proposed_run_id,
                repository_id=repository.id,
                number=event.number,
                head_sha=snapshot.head_sha,
                source_generation=event.source_generation,
                status="error",
                error="无法创建或读取 Preflight 运行记录",
            )

        result: PreflightResult
        try:
            await asyncio.to_thread(
                self.store.set_preflight_phase,
                reservation.run_id,
                "preparing",
                branch=snapshot.source_branch,
            )
            await self._append_log(
                reservation.run_id,
                "正在准备 MR / PR 临时工作区\n",
            )
            await asyncio.to_thread(
                self.store.initialize_preflight_steps,
                reservation.run_id,
                (
                    {
                        "name": step.name,
                        "command": list(step.command),
                        "timeout_seconds": step.timeout_seconds,
                    }
                    for step in repository.preflight.steps
                ),
            )

            async def record_step(update: PreflightStepUpdate) -> None:
                """把执行器产生的步骤变化写入共享状态库。"""

                await asyncio.to_thread(
                    self.store.update_preflight_step,
                    reservation.run_id,
                    update.step_index,
                    status=update.status,
                    timeout_seconds=update.timeout_seconds,
                    exit_code=update.exit_code,
                    error=update.error,
                )

            async def record_output(chunk: str) -> None:
                """持续保存合并后的 stdout / stderr 输出。"""

                await self._append_log(
                    reservation.run_id,
                    chunk,
                    stream="stdout",
                    event_type="output",
                )

            def record_git_progress(git_event: GitProgressEvent) -> None:
                """从 Git 工作线程写入脱敏阶段，不保存认证和命令输出。"""

                try:
                    self.store.append_preflight_log(
                        reservation.run_id,
                        stream="system",
                        event_type="git_progress",
                        payload=git_event.as_dict(),
                    )
                except Exception:
                    pass

            manager = temporary_change_request_worktree(
                provider_config,
                repository,
                snapshot,
                timeout_seconds=self.config.runtime.git_timeout_seconds,
                initialization_timeout_seconds=(
                    self.config.runtime.repository_initialization_timeout_seconds
                ),
                progress_callback=record_git_progress,
            )
            checkout: Path | None = None
            try:
                checkout = await asyncio.to_thread(manager.__enter__)
                await self._set_remote_status(
                    repository,
                    snapshot.head_sha,
                    state="pending",
                    description="本地 CI 正在运行",
                )
                cache_environment: dict[str, str] = {}
                cache_path: str | None = None
                if repository.preflight.cache_enabled:
                    cache_root = repository_cache_root(self.config, repository)
                    cache_environment = await asyncio.to_thread(
                        build_repository_cache_environment,
                        cache_root,
                    )
                    cache_path = str(cache_root.expanduser().resolve())
                    await self._append_log(
                        reservation.run_id,
                        f"已启用仓库级依赖缓存：{cache_path}\n",
                    )
                await asyncio.to_thread(
                    self.store.set_preflight_phase,
                    reservation.run_id,
                    "running_steps",
                    cache_path=cache_path,
                )
                with temporary_directory(
                    prefix="teamwork-preflight-home-"
                ) as home:
                    outcome = await execute_preflight_steps(
                        repository.preflight,
                        cwd=checkout,
                        environment=build_preflight_environment(
                            home=home,
                            cache_environment=cache_environment,
                        ),
                        on_step_update=record_step,
                        on_output=record_output,
                    )
            finally:
                if checkout is not None:
                    await asyncio.to_thread(manager.__exit__, None, None, None)
            result = PreflightResult(
                run_id=reservation.run_id,
                repository_id=repository.id,
                number=event.number,
                head_sha=snapshot.head_sha,
                source_generation=event.source_generation,
                status=outcome.status,
                failed_step=outcome.failed_step,
                exit_code=outcome.exit_code,
                output=outcome.output,
                error=outcome.error,
            )
        except WorkspaceSnapshotSuperseded as exc:
            await self._append_log(
                reservation.run_id,
                str(exc),
                stream="system",
                event_type="superseded",
            )
            result = PreflightResult(
                run_id=reservation.run_id,
                repository_id=repository.id,
                number=event.number,
                head_sha=snapshot.head_sha,
                source_generation=event.source_generation,
                status="superseded",
                error=str(exc),
            )
        except Exception as exc:
            await self._append_log(
                reservation.run_id,
                f"Preflight 基础设施错误：{exc}\n",
                stream="stderr",
                event_type="error",
            )
            result = PreflightResult(
                run_id=reservation.run_id,
                repository_id=repository.id,
                number=event.number,
                head_sha=snapshot.head_sha,
                source_generation=event.source_generation,
                status="error",
                error=str(exc),
            )

        await asyncio.to_thread(self.store.finish_preflight_run, result)
        await self._append_log(
            reservation.run_id,
            {
                "status": result.status,
                "failed_step": result.failed_step,
                "exit_code": result.exit_code,
                "error": result.error,
            },
            event_type="completed",
        )
        if result.status == "superseded":
            return result
        return await self._publish_terminal_result(repository, result)
