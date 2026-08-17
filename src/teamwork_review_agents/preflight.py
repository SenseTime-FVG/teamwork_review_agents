"""确定性 CI 前置检查、输出边界与幂等执行。"""

from __future__ import annotations

import asyncio
import os
import signal
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .config import AppConfig, PreflightConfig
from .environment import resolve_provider_token
from .models import ChangeEvent, PreflightResult, stable_hash
from .providers import create_provider
from .state import StateStore
from .workspace import temporary_change_request_worktree


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
    "UV_CACHE_DIR",
    "UV_LINK_MODE",
}


@dataclass(frozen=True)
class StepExecutionOutcome:
    """一组 CI 步骤的确定性执行结果。"""

    status: Literal["success", "failure", "timed_out", "error"]
    failed_step: str | None = None
    exit_code: int | None = None
    output: str = ""
    error: str | None = None


def build_preflight_environment(*, home: Path | None = None) -> dict[str, str]:
    """只继承运行工具所需的宿主机变量，不传递平台或模型凭据。"""

    environment = {
        name: value
        for name, value in os.environ.items()
        if name.upper() in SAFE_HOST_ENVIRONMENT
    }
    environment.update(
        {
            "CI": "true",
            "TEAMWORK_PREFLIGHT": "1",
            "PYTHONUNBUFFERED": "1",
        }
    )
    if home is not None:
        environment["HOME"] = str(home)
    return environment


def _append_bounded_output(current: str, addition: bytes, limit: int) -> str:
    """按 UTF-8 字节上限保留最新输出，避免无界日志进入 SQLite。"""

    encoded = current.encode("utf-8") + addition
    if len(encoded) > limit:
        encoded = encoded[-limit:]
    return encoded.decode("utf-8", errors="ignore")


async def _terminate_process(process: asyncio.subprocess.Process) -> None:
    """终止超时步骤及其子进程组。"""

    if process.returncode is not None:
        return
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
    except ProcessLookupError:
        pass
    await process.wait()


async def _read_bounded_stream(
    stream: asyncio.StreamReader,
    limit: int,
) -> str:
    """持续排空子进程输出，但内存中只保留最新的有界内容。"""

    output = ""
    while chunk := await stream.read(65536):
        output = _append_bounded_output(output, chunk, limit)
    return output


async def execute_preflight_steps(
    config: PreflightConfig,
    *,
    cwd: Path,
    environment: dict[str, str],
) -> StepExecutionOutcome:
    """按配置顺序执行 CI 步骤，并返回首个失败或最终成功。"""

    loop = asyncio.get_running_loop()
    deadline = loop.time() + config.timeout_seconds
    output = ""
    for step in config.steps:
        remaining = deadline - loop.time()
        if remaining <= 0:
            return StepExecutionOutcome(
                status="timed_out",
                failed_step=step.name,
                output=output,
                error="Preflight 总运行时间超时",
            )
        step_timeout = min(float(step.timeout_seconds or remaining), remaining)
        output = _append_bounded_output(
            output,
            f"\n[{step.name}]\n".encode("utf-8"),
            config.max_output_bytes,
        )
        try:
            process = await asyncio.create_subprocess_exec(
                *step.command,
                cwd=cwd,
                env=environment,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                start_new_session=os.name == "posix",
            )
        except (OSError, ValueError) as exc:
            return StepExecutionOutcome(
                status="error",
                failed_step=step.name,
                output=output,
                error=str(exc),
            )

        assert process.stdout is not None
        output_reader = asyncio.create_task(
            _read_bounded_stream(process.stdout, config.max_output_bytes)
        )
        try:
            await asyncio.wait_for(
                process.wait(),
                timeout=step_timeout,
            )
        except TimeoutError:
            await _terminate_process(process)
            step_output = await output_reader
            output = _append_bounded_output(
                output,
                step_output.encode("utf-8"),
                config.max_output_bytes,
            )
            return StepExecutionOutcome(
                status="timed_out",
                failed_step=step.name,
                output=output,
                error=f"步骤 {step.name} 超过 {step_timeout:.0f} 秒",
            )

        step_output = await output_reader
        output = _append_bounded_output(
            output,
            step_output.encode("utf-8"),
            config.max_output_bytes,
        )
        if process.returncode != 0:
            return StepExecutionOutcome(
                status="failure",
                failed_step=step.name,
                exit_code=process.returncode,
                output=output,
            )

    return StepExecutionOutcome(status="success", output=output)


def preflight_idempotency_key(config: AppConfig, event: ChangeEvent) -> str:
    """返回按仓库、PR、Head 与配置版本隔离的 CI 幂等键。"""

    return stable_hash(
        "preflight",
        event.repository_id,
        event.number,
        event.new.head_sha,
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
    return "本地 CI 基础设施错误"


class PreflightExecutor:
    """准备隔离 worktree、执行 CI，并持久化可复用结果。"""

    def __init__(self, config: AppConfig, store: StateStore) -> None:
        self.config = config
        self.store = store
        self.repositories = config.repository_map()

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
            token=resolve_provider_token(self.config, provider_config),
        ) as remote:
            await remote.set_commit_status(
                repository,
                sha,
                state=state,
                context=repository.preflight.status_context,
                description=description,
            )

    async def _publish_terminal_result(
        self,
        repository,
        result: PreflightResult,
    ) -> PreflightResult:
        """发布已持久化的终态；失败时返回可重试错误但保留本地终态。"""

        remote_state = {
            "success": "success",
            "failure": "failure",
            "timed_out": "failure",
            "error": "error",
        }[result.status]
        try:
            await self._set_remote_status(
                repository,
                result.head_sha,
                state=remote_state,
                description=_status_description(result),
            )
        except Exception as exc:
            return PreflightResult(
                run_id=result.run_id,
                repository_id=result.repository_id,
                number=result.number,
                head_sha=result.head_sha,
                status="error",
                error=f"本地 CI 已完成，但 GitHub 状态回写失败：{exc}",
            )
        await asyncio.to_thread(
            self.store.mark_preflight_status_published,
            result.run_id,
        )
        return result.model_copy(update={"status_published": True})

    async def ensure_passed(self, event: ChangeEvent) -> PreflightResult:
        """返回当前 Head 的已有终态，或执行一次新的 Preflight。"""

        repository = self.repositories[event.repository_id]
        provider_config = self.config.providers[repository.provider]
        key = preflight_idempotency_key(self.config, event)
        cached = await asyncio.to_thread(self.store.load_preflight_result, key)
        if cached is not None and cached.status != "error":
            if cached.status_published:
                return cached
            return await self._publish_terminal_result(repository, cached)

        proposed_run_id = str(uuid.uuid4())
        reservation = await asyncio.to_thread(
            self.store.begin_preflight_run,
            proposed_run_id=proposed_run_id,
            idempotency_key=key,
            event_id=event.id,
            repository_id=repository.id,
            number=event.number,
            head_sha=event.new.head_sha,
            config_revision=self.config.revision,
            max_attempts=self.config.runtime.event_retry_count + 1,
        )
        if reservation is None:
            current = await asyncio.to_thread(self.store.load_preflight_result, key)
            if current is not None:
                return current
            return PreflightResult(
                run_id=proposed_run_id,
                repository_id=repository.id,
                number=event.number,
                head_sha=event.new.head_sha,
                status="error",
                error="无法创建或读取 Preflight 运行记录",
            )

        result: PreflightResult
        try:
            await self._set_remote_status(
                repository,
                event.new.head_sha,
                state="pending",
                description="本地 CI 正在运行",
            )
            manager = temporary_change_request_worktree(
                provider_config,
                repository,
                event.new,
            )
            checkout: Path | None = None
            try:
                checkout = await asyncio.to_thread(manager.__enter__)
                with tempfile.TemporaryDirectory(
                    prefix="teamwork-preflight-home-"
                ) as home:
                    outcome = await execute_preflight_steps(
                        repository.preflight,
                        cwd=checkout,
                        environment=build_preflight_environment(home=Path(home)),
                    )
            finally:
                if checkout is not None:
                    await asyncio.to_thread(manager.__exit__, None, None, None)
            result = PreflightResult(
                run_id=reservation.run_id,
                repository_id=repository.id,
                number=event.number,
                head_sha=event.new.head_sha,
                status=outcome.status,
                failed_step=outcome.failed_step,
                exit_code=outcome.exit_code,
                output=outcome.output,
                error=outcome.error,
            )
        except Exception as exc:
            result = PreflightResult(
                run_id=reservation.run_id,
                repository_id=repository.id,
                number=event.number,
                head_sha=event.new.head_sha,
                status="error",
                error=str(exc),
            )

        await asyncio.to_thread(self.store.finish_preflight_run, result)
        return await self._publish_terminal_result(repository, result)
