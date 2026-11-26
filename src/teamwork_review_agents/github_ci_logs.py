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
from .managed_comments import MANAGED_COMMENT_BODY_BYTES, ManagedCommentService
from .models import PreflightResult, stable_hash
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
        diagnosis = await asyncio.to_thread(store.get_preflight_diagnosis, run_id)
        layout_key = stable_hash(
            "github-inline-log-v2", MANAGED_COMMENT_BODY_BYTES,
            diagnosis.get("status") if diagnosis else None,
            diagnosis.get("report", "") if diagnosis else "",
        )
        if (record and record["status"] == "completed" and record.get("inline_body")
            and record.get("layout_key") == layout_key):
            return record["url"]
        tries = (record["attempts"] if record else 0) + 1
        await asyncio.to_thread(
            store.save_preflight_log_upload, run_id, attempt, status="uploading", attempts=tries,
            inline_body=(record or {}).get("inline_body", ""),
            layout_key=layout_key,
        )
        token = resolve_provider_token(config, config.providers[repository.provider], repository)
        environment = resolve_repository_process_environment(config, repository, run_id)
        redactor = SecretRedactor((*environment.secret_values, token))
        try:
            async with asyncio.timeout(REPORT_TIMEOUT_SECONDS):
                url, details = await _publish_report(config, store, repository, run, remote, redactor, tries)
        except _ObsoleteReport:
            await asyncio.to_thread(
                store.save_preflight_log_upload, run_id, attempt, status="obsolete", attempts=tries,
            )
            return None
        except Exception as exc:
            prepared = await asyncio.to_thread(store.get_preflight_log_upload, run_id)
            await asyncio.to_thread(
                store.save_preflight_log_upload, run_id, attempt, status="failed", attempts=tries,
                error=redactor.text(str(exc) or type(exc).__name__),
                inline_body=(prepared or {}).get("inline_body", ""),
                layout_key=layout_key,
            )
            raise
        await asyncio.to_thread(
            store.save_preflight_log_upload, run_id, attempt, status="completed", attempts=tries, url=url,
            inline_body=details,
            layout_key=layout_key,
        )
        return url


async def _publish_report(
    config: AppConfig, store: StateStore, repository: RepositoryConfig,
    run: dict, remote: BaseProvider, redactor: SecretRedactor, tries: int,
) -> tuple[str, str]:
    """优先把整份日志补入主评论，只有实际放不下时才创建续页。"""

    from .preflight import build_failure_comment_body

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
    diagnosis = await asyncio.to_thread(store.get_preflight_diagnosis, run["run_id"])
    use_failure_comment = repository.preflight.publish_failure_comment and (
        run["status"] in {"failure", "timed_out"}
        or (run["status"] == "error" and diagnosis and diagnosis["status"] == "completed")
    )
    kept_slots: set[str] = set()

    async def publish(slot: str, body: str) -> str:
        """每次写入前检查新运行，并为创建操作留出写入间隔。"""

        if not await asyncio.to_thread(_is_current, store, repository, run):
            raise _ObsoleteReport()
        kept_slots.add(f"{prefix}:{slot}")
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

    def log_section(content: str) -> str:
        """日志正文或续页目录都在主评论的同一个折叠区内。"""

        return f"<details>\n<summary>完整 CI 运行日志</summary>\n\n{content}\n</details>"

    with tempfile.TemporaryFile(mode="w+b") as file:
        await asyncio.to_thread(export_ci_log, store, run, file, redactor)
        raw = file.read(MANAGED_COMMENT_BODY_BYTES + 1)
        details = ""
        if len(raw) <= MANAGED_COMMENT_BODY_BYTES:
            candidate = log_section(f"<pre>{html.escape(raw.decode('utf-8'), quote=False)}</pre>")
            baseline = summary + candidate
            reserve = 0
            if use_failure_comment:
                baseline = build_failure_comment_body(
                    repository, PreflightResult.model_validate(run), redactor=redactor, log_details=candidate,
                )
                diagnosis = await asyncio.to_thread(store.get_preflight_diagnosis, run["run_id"])
                # 诊断稍后完成时仍保留空间；已完成报告按其实际大小判断是否需要日志续页。
                if diagnosis and diagnosis["status"] == "completed":
                    reserve = len(redactor.text(diagnosis["report"]).encode("utf-8")) + 200
                elif repository.preflight.failure_diagnosis_enabled and (
                    not diagnosis or diagnosis["status"] in {"queued", "running"}
                ):
                    reserve = 12 * 1024
            if len(baseline.encode("utf-8")) + reserve <= MANAGED_COMMENT_BODY_BYTES:
                details = candidate

        file.seek(0)
        links: list[str] = []
        if not details:
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
    if not details:
        details = log_section("\n".join(links))
    if not await asyncio.to_thread(_is_current, store, repository, run):
        raise _ObsoleteReport()
    # 先保存合并材料，再在评论锁内读取最新诊断，避免后完成的报告覆盖完整日志。
    await asyncio.to_thread(
        store.save_preflight_log_upload, run["run_id"], run["attempts"], status="uploading", attempts=tries,
        inline_body=details,
    )
    if use_failure_comment:
        result = await service.publish_latest(
            repository_id=repository.id, number=run["number"], namespace="preflight",
            slot=repository.preflight.status_context, source_generation=run["source_generation"],
            source_head_sha=run["head_sha"], body="更新当前 CI 完整日志",
            replace_existing=False, update_existing=True, preflight_run_id=run["run_id"],
            refresh_preflight_body=True,
        )
        if result["action"] == "obsolete":
            raise _ObsoleteReport()
        url = await remote.change_request_comment_url(repository, run["number"], result["comment_id"])
        if not url:
            raise ProviderError("GitHub CI 主评论已不可用，等待队列重试")
    else:
        url = await publish("index", summary + "\n" + details)

    # 主评论成功合并后才删除本次执行多余的旧目录和分段，失败时保留可访问证据。
    records = await asyncio.to_thread(store.list_preflight_log_comments, run["run_id"], run["attempts"])
    for record in records:
        if record["slot"] in kept_slots:
            continue
        if not await asyncio.to_thread(_is_current, store, repository, run):
            raise _ObsoleteReport()
        await remote.delete_change_request_comment(repository, str(record["remote_comment_id"]), number=run["number"])
        await asyncio.to_thread(
            store.delete_managed_comment, repository_id=repository.id, number=run["number"],
            namespace="preflight-log", slot=record["slot"], source_generation=record["source_generation"],
        )
        await asyncio.sleep(COMMENT_INTERVAL_SECONDS)
    return url, details
