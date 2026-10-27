"""将完整本地 CI 记录作为 GitLab 项目附件发布，传输故障不改变 CI 结果。"""

from __future__ import annotations

import asyncio
import json
import re
import tempfile
import uuid
from contextlib import suppress
from datetime import datetime
from typing import BinaryIO, Iterable

import httpx

from .config import AppConfig, RepositoryConfig
from .environment import MASK, SecretRedactor, resolve_provider_token, resolve_repository_process_environment
from .locks import ResourceLease
from .providers.base import BaseProvider
from .state import StateStore


UPLOAD_TOTAL_TIMEOUT_SECONDS = 30
MAX_UPLOAD_ATTEMPTS = 3


def _write_redacted(file: BinaryIO, chunks: Iterable[str], redactor: SecretRedactor) -> None:
    """保留足够的跨块尾部，已知凭据在输出块边界被拆开时也不能泄露。"""

    secrets = redactor.secret_values
    pattern = re.compile("|".join(re.escape(secret) for secret in secrets)) if secrets else None
    overlap = max((len(secret) for secret in secrets), default=1) - 1
    pending = ""
    for chunk in chunks:
        pending += chunk
        cutoff = max(0, len(pending) - overlap)
        if cutoff == 0:
            continue
        start = 0
        if pattern is not None:
            for match in pattern.finditer(pending):
                if match.start() >= cutoff:
                    break
                file.write(pending[start:match.start()].encode("utf-8"))
                file.write(MASK.encode("utf-8"))
                start = match.end()
                cutoff = max(cutoff, start)
        file.write(pending[start:cutoff].encode("utf-8"))
        pending = pending[cutoff:]
    file.write(redactor.text(pending).encode("utf-8"))


def export_ci_log(store: StateStore, run: dict, file: BinaryIO, redactor: SecretRedactor) -> None:
    """导出执行元数据、完整过程及原始输出；旧记录退化时明确提示日志不完整。"""

    def chunks() -> Iterable[str]:
        """连续输出块不插入额外换行，保持原始文本与堆栈完整。"""

        metadata = {key: run.get(key) for key in (
            "run_id", "repository_id", "number", "head_sha", "attempts", "status",
            "failed_step", "exit_code", "error", "steps",
        )}
        for key in ("started_at", "finished_at"):
            value = run.get(key)
            metadata[key] = datetime.fromtimestamp(value).astimezone().isoformat() if value else None
        yield "Teamwork 本地 CI 运行记录\n\n"
        yield json.dumps(redactor.data(metadata), ensure_ascii=False, indent=2)
        yield "\n\n===== 执行过程（stdout / stderr 按采集顺序合并）=====\n"
        has_output = False
        for entry in store.iter_preflight_logs(run["run_id"]):
            if entry["event_type"].startswith("log_upload"):
                continue
            if entry["event_type"] == "output":
                has_output = True
                yield str(entry["payload"])
                continue
            payload = entry["payload"]
            try:
                payload = json.dumps(redactor.data(json.loads(payload)), ensure_ascii=False)
            except (TypeError, ValueError):
                pass
            timestamp = datetime.fromtimestamp(entry["created_at"]).astimezone().isoformat()
            yield f"\n[{timestamp}] [{entry['stream']}/{entry['event_type']}] {payload}\n"
        if not has_output and run.get("output"):
            yield "\n[历史记录缺少逐块日志，以下仅为保留的末尾输出，并非完整日志]\n"
            yield run["output"]
        yield f"\n\n===== 结束：{run['status']}；退出码：{run.get('exit_code')} =====\n"

    _write_redacted(file, chunks(), redactor)
    file.flush()
    file.seek(0)


def _retryable_upload_error(error: Exception) -> bool:
    """仅重试网络或临时服务故障，权限、格式和文件大小问题立即结束。"""

    cause = error.__cause__ or error
    if isinstance(cause, httpx.HTTPStatusError):
        return cause.response.status_code in {408, 429} or cause.response.status_code >= 500
    return isinstance(cause, (httpx.TransportError, TimeoutError))


async def ensure_gitlab_log_upload(
    config: AppConfig, store: StateStore, repository: RepositoryConfig,
    run_id: str, remote: BaseProvider,
) -> str | None:
    """同次执行的成功附件只上传一次，有限重试后独立记录失败，不阻断状态回写。"""

    run = await asyncio.to_thread(store.get_preflight_run, run_id)
    if run is None or run["trigger_source"] != "event" or run["status"] not in {
        "success", "failure", "timed_out", "error", "cancelled",
    }:
        return None
    ci_attempt = run["attempts"]
    attempts = 0
    token = resolve_provider_token(config, config.providers[repository.provider], repository)
    redactor = SecretRedactor((token,))
    try:
        environment = resolve_repository_process_environment(config, repository, run_id)
        redactor = SecretRedactor((*environment.secret_values, token))
        async with ResourceLease(
            store, [f"ci-log-upload:{run_id}:{ci_attempt}"], f"ci-log-upload:{uuid.uuid4()}",
            ttl_seconds=60, timeout_seconds=35,
        ):
            record = await asyncio.to_thread(store.get_preflight_log_upload, run_id)
            if record and record["status"] == "completed":
                return record["url"]
            if record and record["status"] == "failed":
                return None
            attempts = record["attempts"] if record else 0
            return await _upload_locked(store, repository, run, remote, redactor, attempts)
    except Exception as exc:
        # 获取锁或读取环境失败时只记日志，不能改写其他上传者的记录。
        await asyncio.to_thread(
            store.append_preflight_log, run_id, stream="stderr", event_type="log_upload_error",
            payload=f"CI 日志附件发布异常：{redactor.text(str(exc) or type(exc).__name__)}",
        )
        return None


async def _upload_locked(
    store: StateStore, repository: RepositoryConfig, run: dict, remote: BaseProvider,
    redactor: SecretRedactor, attempts: int,
) -> str | None:
    """上传状态写入全程位于租约内，失败不能覆盖并发调用的成功记录。"""

    run_id, ci_attempt = run["run_id"], run["attempts"]
    try:
        with tempfile.TemporaryFile(mode="w+b") as file:
            await asyncio.to_thread(export_ci_log, store, run, file, redactor)
            url = None
            async with asyncio.timeout(UPLOAD_TOTAL_TIMEOUT_SECONDS):
                while attempts < MAX_UPLOAD_ATTEMPTS:
                    attempts += 1
                    await asyncio.to_thread(
                        store.save_preflight_log_upload, run_id, ci_attempt,
                        status="uploading", attempts=attempts,
                    )
                    try:
                        url = await remote.upload_ci_log(repository, file, f"ci-{run_id}-{ci_attempt}.log")
                    except Exception as exc:
                        if attempts >= MAX_UPLOAD_ATTEMPTS or not _retryable_upload_error(exc):
                            raise
                        await asyncio.sleep(0.5 * attempts)
                        continue
                    break
            if url is None:
                raise RuntimeError("日志附件上传已达到重试上限")
            # 成功记录不受网络总时限影响；附加日志写入失败也不能抹掉附件地址。
            await asyncio.to_thread(
                store.save_preflight_log_upload, run_id, ci_attempt,
                status="completed", attempts=attempts, url=url,
            )
            with suppress(Exception):
                await asyncio.to_thread(
                    store.append_preflight_log, run_id, stream="system", event_type="log_upload_completed",
                    payload=f"完整 CI 日志已上传到 GitLab：{url}",
                )
            return url
    except asyncio.CancelledError:
        # 保留 uploading 记录；服务恢复后可在剩余次数内继续，不误报 CI 失败。
        raise
    except Exception as exc:
        message = redactor.text(str(exc) or type(exc).__name__)
        await asyncio.to_thread(
            store.save_preflight_log_upload, run_id, ci_attempt,
            status="failed", attempts=attempts, error=message,
        )
        await asyncio.to_thread(
            store.append_preflight_log, run_id, stream="stderr", event_type="log_upload_error",
            payload=f"CI 日志附件上传失败（不影响 CI 结果）：{message}",
        )
        return None
