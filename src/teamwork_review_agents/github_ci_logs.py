"""把完整本地 CI 日志发布为 GitHub PR 内可折叠、可恢复的分段评论。"""

from __future__ import annotations

import asyncio
import codecs
import html
import tempfile
import uuid
from typing import BinaryIO, Iterator

from .config import AppConfig, RepositoryConfig
from .environment import SecretRedactor, resolve_provider_token, resolve_repository_process_environment
from .locks import ResourceLease
from .managed_comments import ManagedCommentService
from .models import stable_hash
from .preflight_log_uploads import export_ci_log
from .providers.base import BaseProvider, ProviderError
from .state import StateStore


LOG_PART_BYTES = 48 * 1024
DIRECTORY_BYTES = 40 * 1024
COMMENT_INTERVAL_SECONDS = 1
REPORT_TIMEOUT_SECONDS = 600


class _ObsoleteReport(Exception):
    """旧运行停止发布日志，但不作为传输失败重试。"""


def _escaped_log_parts(file: BinaryIO) -> Iterator[str]:
    """按转义后的字节数切段，不拆 Unicode 字符或 HTML 实体，不截断日志。"""

    reader = codecs.getreader("utf-8")(file)
    part: list[str] = []
    size = 0
    while text := reader.read(8192):
        for character in text:
            escaped = html.escape(character, quote=False)
            length = len(escaped.encode("utf-8"))
            if size + length > LOG_PART_BYTES:
                yield "".join(part)
                part, size = [], 0
            part.append(escaped)
            size += length
    if part:
        yield "".join(part)


def _is_current(store: StateStore, repository: RepositoryConfig, run: dict) -> bool:
    """发布前核对运行、执行尝试和源代次，避免重试覆盖新结果。"""

    current = store.get_preflight_run(run["run_id"])
    generation = store.source_generation(repository.id, run["number"])
    return bool(
        current and current["attempts"] == run["attempts"]
        and current["status"] == run["status"] and current["status_published"]
        and store.latest_preflight_run_id(repository.id, run["number"]) == run["run_id"]
        and generation in (None, run["source_generation"])
    )


async def ensure_github_log_report(
    config: AppConfig, store: StateStore, repository: RepositoryConfig,
    run_id: str, remote: BaseProvider,
) -> str | None:
    """复用已完成目录，部分失败保留评论映射，由外层队列有限重试。"""

    run = await asyncio.to_thread(store.get_preflight_run, run_id)
    if run is None or run["number"] is None or run["trigger_source"] != "event":
        return None
    attempt = run["attempts"]
    async with ResourceLease(
        store, [f"ci-log-upload:{run_id}:{attempt}"], f"ci-log-upload:{uuid.uuid4()}",
        ttl_seconds=60, timeout_seconds=35,
    ):
        if not await asyncio.to_thread(_is_current, store, repository, run):
            return None
        record = await asyncio.to_thread(store.get_preflight_log_upload, run_id)
        if record and record["status"] == "completed":
            return record["url"]
        tries = (record["attempts"] if record else 0) + 1
        await asyncio.to_thread(
            store.save_preflight_log_upload, run_id, attempt, status="uploading", attempts=tries,
        )
        token = resolve_provider_token(config, config.providers[repository.provider], repository)
        environment = resolve_repository_process_environment(config, repository, run_id)
        redactor = SecretRedactor((*environment.secret_values, token))
        try:
            async with asyncio.timeout(REPORT_TIMEOUT_SECONDS):
                url = await _publish_report(config, store, repository, run, remote, redactor)
        except _ObsoleteReport:
            await asyncio.to_thread(
                store.save_preflight_log_upload, run_id, attempt, status="obsolete", attempts=tries,
            )
            return None
        except Exception as exc:
            await asyncio.to_thread(
                store.save_preflight_log_upload, run_id, attempt, status="failed", attempts=tries,
                error=redactor.text(str(exc) or type(exc).__name__),
            )
            raise
        await asyncio.to_thread(
            store.save_preflight_log_upload, run_id, attempt, status="completed", attempts=tries, url=url,
        )
        return url


async def _publish_report(
    config: AppConfig, store: StateStore, repository: RepositoryConfig,
    run: dict, remote: BaseProvider, redactor: SecretRedactor,
) -> str:
    """目录与分段各有固定槽位，不占用会在成功后删除的活动失败评论。"""

    service = ManagedCommentService(config, store)
    prefix = f"{run['run_id']}:{run['attempts']}"
    title = "## Teamwork 本地 CI 运行报告"
    status = {
        "success": "通过", "failure": "未通过", "timed_out": "超时",
        "error": "基础设施异常", "cancelled": "已取消",
    }.get(run["status"], run["status"])
    summary = (
        f"{title}\n\n- 状态：**{html.escape(status)}**\n"
        f"- Head SHA：`{html.escape(run['head_sha'])}`\n"
        f"- CI 运行：`{html.escape(prefix)}`\n"
        f"- 退出码：{run['exit_code'] if run['exit_code'] is not None else '—'}\n"
    )
    if run["failed_step"]:
        summary += f"- 失败步骤：<code>{html.escape(redactor.text(run['failed_step'])[:1000])}</code>\n"
    if run["status"] in {"failure", "timed_out", "error"}:
        records = await asyncio.to_thread(
            store.list_managed_comments, repository_id=repository.id, number=run["number"],
            namespace="preflight", slot=repository.preflight.status_context,
        )
        if records and records[-1]["source_head_sha"] == run["head_sha"]:
            report_url = await remote.change_request_comment_url(
                repository, run["number"], str(records[-1]["remote_comment_id"]),
            )
            if report_url:
                summary += f"\n[查看失败输出与诊断报告]({report_url})\n"

    async def publish(slot: str, body: str) -> str:
        """每次写入前检查新运行，并为创建操作留出写入间隔。"""

        if not await asyncio.to_thread(_is_current, store, repository, run):
            raise _ObsoleteReport()
        record = await asyncio.to_thread(
            store.get_managed_comment, repository_id=repository.id, number=run["number"],
            namespace="preflight-log", slot=f"{prefix}:{slot}", source_generation=run["source_generation"],
        )
        if record and record["content_hash"] == stable_hash(body.strip()):
            # 已成功的分段只核对链接，不重复写入和等待，长日志重试才能继续向前。
            url = await remote.change_request_comment_url(repository, run["number"], str(record["remote_comment_id"]))
            if url:
                return url
        result = await service.publish(
            repository_id=repository.id, number=run["number"], namespace="preflight-log",
            slot=f"{prefix}:{slot}", source_generation=run["source_generation"],
            source_head_sha=run["head_sha"], body=body,
        )
        url = await remote.change_request_comment_url(repository, run["number"], result["comment_id"])
        if not url:
            raise ProviderError("刚发布的 GitHub CI 日志评论已不可用，等待队列重试")
        await asyncio.sleep(COMMENT_INTERVAL_SECONDS)
        return url

    with tempfile.TemporaryFile(mode="w+b") as file:
        await asyncio.to_thread(export_ci_log, store, run, file, redactor)
        links: list[str] = []
        for index, text in enumerate(_escaped_log_parts(file), 1):
            body = (
                f"### Teamwork CI 完整日志 · 第 {index} 段\n\n"
                f"Head SHA：`{html.escape(run['head_sha'])}` · CI：`{html.escape(prefix)}`\n\n"
                f"<details>\n<summary>展开第 {index} 段日志</summary>\n\n"
                f"<pre>{text}</pre>\n</details>"
            )
            url = await publish(f"part:{index}", body)
            links.append(f"- [第 {index} 段]({url})")

    # 超长记录的目录也分组，避免所有日志已上传却因单条评论上限丢失入口。
    level = 0
    while len("\n".join(links).encode("utf-8")) > DIRECTORY_BYTES:
        grouped: list[str] = []
        for offset in range(0, len(links), 100):
            page = offset // 100 + 1
            url = await publish(f"directory:{level}:{page}", summary + "\n" + "\n".join(links[offset:offset + 100]))
            grouped.append(f"- [日志目录 {page}]({url})")
        links = grouped
        level += 1
    return await publish("index", summary + "\n### 完整运行日志\n\n" + "\n".join(links))
