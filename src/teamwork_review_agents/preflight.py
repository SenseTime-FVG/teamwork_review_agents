"""确定性 CI 前置检查、输出边界与幂等执行。"""

from __future__ import annotations

import asyncio
import os
import signal
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .config import AppConfig, PreflightConfig
from .models import ChangeEvent, PreflightResult, stable_hash
from .state import StateStore
from .workspace import temporary_change_request_worktree


SAFE_HOST_ENVIRONMENT = {
    "PATH",
    "HOME",
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


def build_preflight_environment() -> dict[str, str]:
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
    return environment


def _append_bounded_output(current: str, addition: bytes, limit: int) -> str:
    """按 UTF-8 字节上限保留最新输出，避免无界日志进入 SQLite。"""

    encoded = current.encode("utf-8") + addition
    if len(encoded) > limit:
        encoded = encoded[-limit:]
    return encoded.decode("utf-8", errors="replace")


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

        communicate = asyncio.create_task(process.communicate())
        try:
            stdout, _ = await asyncio.wait_for(
                asyncio.shield(communicate),
                timeout=step_timeout,
            )
        except TimeoutError:
            await _terminate_process(process)
            stdout, _ = await communicate
            output = _append_bounded_output(
                output,
                stdout or b"",
                config.max_output_bytes,
            )
            return StepExecutionOutcome(
                status="timed_out",
                failed_step=step.name,
                output=output,
                error=f"步骤 {step.name} 超过 {step_timeout:.0f} 秒",
            )

        output = _append_bounded_output(
            output,
            stdout or b"",
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


class PreflightExecutor:
    """准备隔离 worktree、执行 CI，并持久化可复用结果。"""

    def __init__(self, config: AppConfig, store: StateStore) -> None:
        self.config = config
        self.store = store
        self.repositories = config.repository_map()

    async def ensure_passed(self, event: ChangeEvent) -> PreflightResult:
        """返回当前 Head 的已有终态，或执行一次新的 Preflight。"""

        repository = self.repositories[event.repository_id]
        provider = self.config.providers[repository.provider]
        key = preflight_idempotency_key(self.config, event)
        cached = await asyncio.to_thread(self.store.load_preflight_result, key)
        if cached is not None and cached.status != "error":
            return cached

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

        manager = temporary_change_request_worktree(provider, repository, event.new)
        checkout: Path | None = None
        try:
            checkout = await asyncio.to_thread(manager.__enter__)
            outcome = await execute_preflight_steps(
                repository.preflight,
                cwd=checkout,
                environment=build_preflight_environment(),
            )
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
        finally:
            if checkout is not None:
                try:
                    await asyncio.to_thread(manager.__exit__, None, None, None)
                except Exception as exc:
                    result = PreflightResult(
                        run_id=reservation.run_id,
                        repository_id=repository.id,
                        number=event.number,
                        head_sha=event.new.head_sha,
                        status="error",
                        error=f"清理 Preflight worktree 失败：{exc}",
                    )

        await asyncio.to_thread(self.store.finish_preflight_run, result)
        return result
