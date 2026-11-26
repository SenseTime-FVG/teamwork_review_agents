"""GitHub 完整日志、平台链接、异步终态和断点恢复的隔离回归测试。"""

from __future__ import annotations

import asyncio
import html
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from teamwork_review_agents.config import EnvironmentVariable
from teamwork_review_agents.events import detect_events
from teamwork_review_agents.managed_comments import ManagedCommentService
from teamwork_review_agents.models import CommitStatusTarget, PreflightResult
from teamwork_review_agents.preflight import PreflightExecutor
from teamwork_review_agents.preflight_log_worker import PreflightLogWorker
from teamwork_review_agents.providers.base import ProviderError
from teamwork_review_agents.providers.github import GitHubProvider
from teamwork_review_agents.state import StateStore


@pytest.fixture
def github_logs(configured_app_factory, snapshot_factory, monkeypatch):
    """使用内存平台替身，记录评论正文、原生链接和状态写回顺序。"""

    config = configured_app_factory()
    repository = config.repositories[0]
    repository.preflight.publish_failure_comment = True
    repository.environment["PRIVATE_VALUE"] = EnvironmentVariable(value="split-private-secret", secret=True)
    monkeypatch.setenv("GITHUB_TOKEN", "provider-secret")
    monkeypatch.setattr("teamwork_review_agents.github_ci_logs.COMMENT_INTERVAL_SECONDS", 0)
    store = StateStore(config.database.path)
    store.initialize()
    web_url = "https://github.com/owner/demo/pull/7"
    snapshot = snapshot_factory(provider="github-main", web_url=web_url)
    event = detect_events(None, snapshot, emit_initial=True)[0]
    store.save_snapshot_and_events(snapshot, [event])
    store.begin_preflight_run(
        proposed_run_id="ci-github", idempotency_key="ci-github", event_id=event.id,
        repository_id=repository.id, number=7, head_sha=snapshot.head_sha,
        config_revision=config.revision, max_attempts=3,
    )
    result = PreflightResult(
        run_id="ci-github", repository_id=repository.id, number=7, head_sha=snapshot.head_sha,
        status="failure", failed_step="tests", exit_code=1, output="失败末尾输出",
    )
    store.finish_preflight_run(result)
    full_output = "完整中文😀 & < > </pre></details>\n" * 4000
    for chunk in ("日志开头 split-private-", "secret\n" + full_output, "日志结束 provider-secret"):
        store.append_preflight_log(result.run_id, stream="stdout", event_type="output", payload=chunk)
    comments: dict[str, str] = {}
    created: list[str] = []

    async def create(_repository, _number, body):
        """递增远端评论 ID，不因内容相同而自动去重。"""

        comment_id = str(len(created) + 1)
        created.append(comment_id)
        comments[comment_id] = body
        return comment_id

    async def update(_repository, comment_id, body, **_kwargs):
        """远端删除由缺失响应表示，其余更新保留原评论位置。"""

        if comment_id not in comments:
            return False
        comments[comment_id] = body
        return True

    async def delete(_repository, comment_id, **_kwargs):
        """删除远端受管日志评论，用于验证旧版目录收敛。"""

        comments.pop(comment_id, None)

    remote = AsyncMock()
    remote.__aenter__.return_value = remote
    remote.create_change_request_comment.side_effect = create
    remote.update_change_request_comment.side_effect = update
    remote.delete_change_request_comment.side_effect = delete
    remote.change_request_comment_url.side_effect = lambda _r, _n, cid: f"{web_url}#issuecomment-{cid}" if cid in comments else None
    remote.set_commit_status.return_value = CommitStatusTarget(project=repository.project, sha=result.head_sha)
    for module in ("preflight", "managed_comments"):
        monkeypatch.setattr(f"teamwork_review_agents.{module}.create_provider", lambda *_a, **_kw: remote)
    return SimpleNamespace(
        config=config, repository=repository, store=store, result=result, remote=remote,
        web_url=web_url, comments=comments, created=created, full_output=full_output,
        create=create, event=event,
    )


@pytest.mark.parametrize("status", ["success", "failure", "timed_out"])
async def test_github_terminal_precedes_complete_collapsible_log_publication(github_logs, status):
    """评论发布再慢也先回写 CI 终态，完整 Unicode 日志及报告均留在 GitHub。"""

    case = github_logs
    case.result = case.result.model_copy(update={"status": status})
    case.store.finish_preflight_run(case.result)
    executor = PreflightExecutor(case.config, case.store)
    result = await executor._publish_terminal_result(case.repository, case.result)
    assert result.status == status and result.status_published
    assert case.remote.set_commit_status.call_args.kwargs["target_url"] == case.web_url
    assert not any("完整日志 · 第" in body for body in case.comments.values())
    assert case.store.get_preflight_log_delivery(result.run_id)["status"] == "queued"
    entered, release = asyncio.Event(), asyncio.Event()

    async def hold(repository, number, body):
        """网络屏障只拦住完整日志，验证终态不依赖日志上传速度。"""

        entered.set()
        await release.wait()
        return await case.create(repository, number, body)

    case.remote.create_change_request_comment.side_effect = hold
    task = asyncio.create_task(PreflightLogWorker(lambda: case.config, case.store).drain())
    try:
        await asyncio.wait_for(entered.wait(), 3)
        assert case.remote.set_commit_status.await_count == 1
        assert not task.done()
    finally:
        release.set()
        await task

    run = case.store.get_preflight_run(result.run_id)
    assert run["status"] == status and run["log_delivery"]["status"] == "completed"
    index_url = run["log_upload"]["url"]
    index_body = case.comments[index_url.rsplit("-", 1)[-1]]
    assert "完整 CI 运行日志" in index_body and "[第 1 段]" in index_body
    parts = [body for body in case.comments.values() if body.startswith("### Teamwork CI 完整日志")]
    assert len(parts) > 1
    exported = "".join(html.unescape(re.search(r"<pre>(.*)</pre>", part, re.S).group(1)) for part in parts)
    assert case.full_output in exported
    assert "日志开头" in exported and "日志结束" in exported
    assert "split-private-secret" not in exported and "provider-secret" not in exported
    assert all(len(body.encode("utf-8")) < 60 * 1024 for body in case.comments.values())
    assert all(body.count("</pre>") == 1 and body.count("</details>") == 1 for body in parts)
    assert all("#preflight=" not in call.kwargs["target_url"] for call in case.remote.set_commit_status.call_args_list)
    final_url = case.remote.set_commit_status.call_args.kwargs["target_url"]
    if status == "success":
        assert final_url == index_url
    else:
        report = case.comments[final_url.rsplit("-", 1)[-1]]
        assert "失败末尾输出" in report and final_url == index_url
        assert "<summary>末尾输出</summary>" in report
        assert "查看失败输出与诊断报告" not in index_body


async def test_github_partial_failure_resumes_without_duplicate_parts(github_logs):
    """部分评论已成功时保留映射，后台重试不会再次新建相同分段。"""

    case = github_logs
    await PreflightExecutor(case.config, case.store)._publish_terminal_result(case.repository, case.result)
    failed = False

    async def fail_second(repository, number, body):
        """只让第二段首次写入失败，模拟常见网络中断。"""

        nonlocal failed
        if "展开第 2 段日志" in body and not failed:
            failed = True
            raise ProviderError("临时发布失败 provider-secret")
        return await case.create(repository, number, body)

    case.remote.create_change_request_comment.side_effect = fail_second
    worker = PreflightLogWorker(lambda: case.config, case.store)
    await worker.drain()
    job = case.store.get_preflight_log_delivery(case.result.run_id)
    assert job["status"] == "queued" and job["attempts"] == 1
    assert "provider-secret" not in job["error"]
    assert case.store.get_preflight_run(case.result.run_id)["status"] == "failure"
    case.store.update_preflight_log_delivery(case.result.run_id, 1, status="queued", attempts=1)
    await worker.drain()
    assert case.store.get_preflight_log_delivery(case.result.run_id)["status"] == "completed"
    assert sum("展开第 1 段日志" in body for body in case.comments.values()) == 1
    assert not any("展开第 1 段日志" in call.args[2] for call in case.remote.update_change_request_comment.await_args_list)
    assert sum("<summary>完整 CI 运行日志</summary>" in body for body in case.comments.values()) == 1
    count = len(case.created)
    await worker.drain()
    assert len(case.created) == count


async def test_github_old_report_stops_without_overwriting_new_run(github_logs):
    """发布途中出现同 SHA 新运行时终止旧目录，不能再次写回旧终态。"""

    case = github_logs
    await PreflightExecutor(case.config, case.store)._publish_terminal_result(case.repository, case.result)
    entered, release = asyncio.Event(), asyncio.Event()

    async def hold(repository, number, body):
        """第一段发送中允许新运行抢先进入，随后检查旧任务停止。"""

        entered.set()
        await release.wait()
        return await case.create(repository, number, body)

    case.remote.create_change_request_comment.side_effect = hold
    task = asyncio.create_task(PreflightLogWorker(lambda: case.config, case.store).drain())
    try:
        await asyncio.wait_for(entered.wait(), 3)
        case.store.begin_preflight_run(
            proposed_run_id="ci-new", idempotency_key="ci-new", event_id=case.event.id,
            repository_id=case.repository.id, number=7, head_sha=case.result.head_sha,
            config_revision=case.config.revision, max_attempts=3,
        )
    finally:
        release.set()
        await task
    assert case.store.get_preflight_log_delivery(case.result.run_id)["status"] == "obsolete"
    assert case.remote.set_commit_status.await_count == 1
    assert case.store.get_preflight_run("ci-new")["status"] == "running"


async def test_github_without_failure_comment_links_log_directory(github_logs):
    """关闭活动失败摘要不影响完整日志目录和平台状态入口。"""

    case = github_logs
    case.repository.preflight.publish_failure_comment = False
    await PreflightExecutor(case.config, case.store)._publish_terminal_result(case.repository, case.result)
    await PreflightLogWorker(lambda: case.config, case.store).drain()
    url = case.store.get_preflight_log_upload(case.result.run_id)["url"]
    assert case.remote.set_commit_status.call_args.kwargs["target_url"] == url
    assert "完整 CI 运行日志" in case.comments[url.rsplit("-", 1)[-1]]
    assert not any("Teamwork 本地 CI 未通过" in body for body in case.comments.values())


def _use_short_log(case):
    """替换为能连同失败摘要和诊断放进单条评论的完整日志。"""

    with case.store.connect() as connection:
        connection.execute("DELETE FROM preflight_logs WHERE run_id=?", (case.result.run_id,))
    case.store.append_preflight_log(
        case.result.run_id, stream="stdout", event_type="output",
        payload="完整日志首行😀\n失败末尾输出\n完整日志末行\n",
    )


@pytest.mark.parametrize("status", ["success", "failure", "timed_out"])
async def test_github_short_log_has_only_one_ci_comment(github_logs, status):
    """普通日志直接并入主评论，失败摘要、完整输出和晚到诊断不各发一条。"""

    case = github_logs
    _use_short_log(case)
    case.result = case.result.model_copy(update={"status": status})
    case.store.finish_preflight_run(case.result)
    await PreflightExecutor(case.config, case.store)._publish_terminal_result(case.repository, case.result)
    original = case.created[0] if case.created else None
    await PreflightLogWorker(lambda: case.config, case.store).drain()
    assert len(case.comments) == 1 and len(case.created) == 1
    url = case.store.get_preflight_log_upload(case.result.run_id)["url"]
    comment_id = url.rsplit("-", 1)[-1]
    assert original is None or comment_id == original
    body = case.comments[comment_id]
    assert "<summary>完整 CI 运行日志</summary>" in body
    assert "完整日志首行😀" in body and "完整日志末行" in body
    assert "[第 1 段]" not in body
    assert len(body.encode()) < 60 * 1024
    if status != "success":
        case.store.begin_preflight_diagnosis(case.result.run_id, "diagnosis")
        case.store.finish_preflight_diagnosis(case.result.run_id, status="completed", report="晚到诊断：断言错误")
        await ManagedCommentService(case.config, case.store).publish_latest(
            repository_id=case.repository.id, number=7, namespace="preflight",
            slot=case.repository.preflight.status_context, source_generation=1,
            source_head_sha=case.result.head_sha, body="更新诊断",
            replace_existing=False, update_existing=True, preflight_run_id=case.result.run_id,
            refresh_preflight_body=True,
        )
        assert len(case.comments) == 1 and len(case.created) == 1
        assert "晚到诊断：断言错误" in case.comments[comment_id]
        assert "完整日志末行" in case.comments[comment_id]


async def test_github_legacy_three_comments_merge_before_cleanup(github_logs):
    """原主评论更新成功后才删本次旧目录和日志，不触碰人工或其他运行的评论。"""

    case = github_logs
    _use_short_log(case)
    await PreflightExecutor(case.config, case.store)._publish_terminal_result(case.repository, case.result)
    service = ManagedCommentService(case.config, case.store)
    for slot in ("ci-github:1:part:1", "ci-github:1:index", "other-run:1:index"):
        await service.publish(
            repository_id=case.repository.id, number=7, namespace="preflight-log", slot=slot,
            source_generation=1, source_head_sha=case.result.head_sha, body="旧版独立日志评论",
        )
    case.comments["user-comment"] = "人工评论"
    case.store.save_preflight_log_upload(
        case.result.run_id, 1, status="completed", attempts=1, url=case.web_url + "#issuecomment-3",
    )
    update = case.remote.update_change_request_comment.side_effect
    case.remote.update_change_request_comment.side_effect = ProviderError("模拟合并失败")
    worker = PreflightLogWorker(lambda: case.config, case.store)
    await worker.drain()
    assert all(cid in case.comments for cid in ("1", "2", "3", "4", "user-comment"))
    case.remote.delete_change_request_comment.assert_not_awaited()

    case.remote.update_change_request_comment.side_effect = update
    case.store.update_preflight_log_delivery(case.result.run_id, 1, status="queued", attempts=1)
    await worker.drain()
    assert set(case.comments) == {"1", "4", "user-comment"}
    assert "完整日志首行😀" in case.comments["1"]
    assert case.store.get_preflight_log_upload(case.result.run_id)["url"] == case.web_url + "#issuecomment-1"
    assert case.store.list_preflight_log_comments(case.result.run_id, 1) == []
    assert len(case.created) == 4


async def test_github_full_log_and_diagnosis_share_existing_report(github_logs):
    """完整日志与诊断能放下时缩短重复尾部，不能因此多发目录或日志评论。"""

    case = github_logs
    _use_short_log(case)
    text = "完整输出首行😀\n" + "x" * 41000 + "\n末尾错误"
    with case.store.connect() as connection:
        connection.execute("DELETE FROM preflight_logs WHERE run_id=?", (case.result.run_id,))
    case.store.append_preflight_log(case.result.run_id, stream="stdout", event_type="output", payload=text)
    case.result = case.result.model_copy(update={"output": text[-26000:]})
    case.store.finish_preflight_run(case.result)
    case.store.begin_preflight_diagnosis(case.result.run_id, "diagnosis")
    report = "已确认的诊断\n" * 300
    case.store.finish_preflight_diagnosis(case.result.run_id, status="completed", report=report)
    await PreflightExecutor(case.config, case.store)._publish_terminal_result(case.repository, case.result)
    await PreflightLogWorker(lambda: case.config, case.store).drain()
    assert len(case.comments) == 1 and len(case.created) == 1
    body = next(iter(case.comments.values()))
    assert "完整输出首行😀" in body and "末尾错误" in body
    assert report.strip() in body
    assert len(body.encode()) < 60 * 1024


def test_github_legacy_upload_table_adds_inline_material_without_losing_url(github_logs):
    """老数据库升级时保留已有远端地址和发布状态，为按需合并提供空材料。"""

    case = github_logs
    with case.store.connect() as connection:
        connection.execute("ALTER TABLE preflight_log_uploads DROP COLUMN inline_body")
        connection.execute(
            "INSERT INTO preflight_log_uploads (run_id, ci_attempt, status, attempts, url, updated_at) VALUES (?, 1, 'completed', 1, ?, 0)",
            (case.result.run_id, case.web_url + "#issuecomment-3"),
        )
    case.store.initialize()
    record = case.store.get_preflight_log_upload(case.result.run_id)
    assert record["status"] == "completed" and record["url"] == case.web_url + "#issuecomment-3"
    assert record["inline_body"] == ""


async def test_github_large_directory_is_grouped(github_logs, monkeypatch):
    """目录自身过长时创建分组索引，各段日志仍能逐级访问。"""

    case = github_logs
    monkeypatch.setattr("teamwork_review_agents.github_ci_logs.DIRECTORY_BYTES", 400)
    await PreflightExecutor(case.config, case.store)._publish_terminal_result(case.repository, case.result)
    await PreflightLogWorker(lambda: case.config, case.store).drain()
    url = case.store.get_preflight_log_upload(case.result.run_id)["url"]
    root = case.comments[url.rsplit("-", 1)[-1]]
    assert "[日志目录 1]" in root
    group_url = re.search(r"\[日志目录 1\]\(([^)]+)\)", root).group(1)
    group = case.comments[group_url.rsplit("-", 1)[-1]]
    assert "[第 1 段]" in group


async def test_github_completed_log_retries_only_link_delivery(github_logs):
    """日志已发布但状态补链失败时复用目录，跨工作器重试不重新传输日志。"""

    case = github_logs
    await PreflightExecutor(case.config, case.store)._publish_terminal_result(case.repository, case.result)
    case.remote.set_commit_status.side_effect = ProviderError("临时状态写回失败")
    await PreflightLogWorker(lambda: case.config, case.store).drain()
    assert case.store.get_preflight_log_upload(case.result.run_id)["status"] == "completed"
    count = len(case.created)
    case.remote.set_commit_status.side_effect = None
    case.store.update_preflight_log_delivery(case.result.run_id, 1, status="queued", attempts=1)
    await PreflightLogWorker(lambda: case.config, case.store).drain()
    assert case.store.get_preflight_log_delivery(case.result.run_id)["status"] == "completed"
    assert len(case.created) == count


async def test_github_deleted_failure_report_is_recreated_before_linking(github_logs):
    """原失败摘要被人工删除后重建，最终状态必须定位新评论。"""

    case = github_logs
    await PreflightExecutor(case.config, case.store)._publish_terminal_result(case.repository, case.result)
    del case.comments["1"]
    await PreflightLogWorker(lambda: case.config, case.store).drain()
    url = case.remote.set_commit_status.call_args.kwargs["target_url"]
    assert url != case.web_url + "#issuecomment-1"
    assert "失败末尾输出" in case.comments[url.rsplit("-", 1)[-1]]


@pytest.mark.parametrize("diagnosis_first", [True, False])
async def test_github_diagnosis_and_log_keep_each_others_content(github_logs, diagnosis_first):
    """诊断和日志以任意顺序完成时，报告均保留原始失败证据及两个入口。"""

    case = github_logs
    await PreflightExecutor(case.config, case.store)._publish_terminal_result(case.repository, case.result)

    async def finish_diagnosis():
        """复用诊断完成后的真实持久化与锁内正文合并路径。"""

        case.store.begin_preflight_diagnosis(case.result.run_id, "diagnosis")
        case.store.finish_preflight_diagnosis(case.result.run_id, status="completed", report="诊断结论：业务断言失败")
        await ManagedCommentService(case.config, case.store).publish_latest(
            repository_id=case.repository.id, number=7, namespace="preflight",
            slot=case.repository.preflight.status_context, source_generation=1,
            source_head_sha=case.result.head_sha, body="占位正文",
            replace_existing=False, update_existing=True, preflight_run_id=case.result.run_id,
            refresh_preflight_body=True,
        )

    if diagnosis_first:
        await finish_diagnosis()
    await PreflightLogWorker(lambda: case.config, case.store).drain()
    if not diagnosis_first:
        await finish_diagnosis()
    url = case.remote.set_commit_status.call_args.kwargs["target_url"]
    report = case.comments[url.rsplit("-", 1)[-1]]
    assert "诊断结论：业务断言失败" in report
    assert "<summary>CI 失败诊断报告</summary>" in report
    assert "失败末尾输出" in report
    assert "<summary>完整 CI 运行日志</summary>" in report
    assert "[第 1 段]" in report


@pytest.mark.parametrize("url,valid", [
    ("https://github.com/owner/demo/pull/7#issuecomment-123", True),
    ("https://github.com/owner/demo/issues/7#issuecomment-123", True),
    ("http://10.151.92.100:8080/#preflight=run", False),
    ("https://evil.example/owner/demo/pull/7#issuecomment-123", False),
    ("https://github.com/owner/demo/pull/8#issuecomment-123", False),
    ("https://github.com/owner/demo/pull/7?token=private#issuecomment-123", False),
])
async def test_github_uses_only_native_comment_permalink(github_logs, url, valid):
    """真实 Provider 请求精确评论 API，拒绝后台、异站、错误 PR 和凭据参数。"""

    case = github_logs
    provider = GitHubProvider("github-main", case.config.providers["github-main"], case.config.scanner, token="test")
    await provider.client.aclose()

    def handler(request):
        """只返回内存响应，不产生平台读取或写入。"""

        assert request.url.path == "/repos/owner/demo/issues/comments/123"
        return httpx.Response(200, json={"html_url": url})

    provider.client = httpx.AsyncClient(base_url="https://api.github.com/", transport=httpx.MockTransport(handler))
    async with provider:
        if valid:
            assert await provider.change_request_comment_url(case.repository, 7, "123") == url
        else:
            with pytest.raises(ProviderError, match="平台永久链接"):
                await provider.change_request_comment_url(case.repository, 7, "123")
