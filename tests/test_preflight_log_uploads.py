"""GitLab 完整 CI 日志附件、脱敏、重试与独立故障状态回归。"""

from __future__ import annotations

import asyncio
import io
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from teamwork_review_agents.config import EnvironmentVariable
from teamwork_review_agents.environment import SecretRedactor
from teamwork_review_agents.events import detect_events
from teamwork_review_agents.managed_comments import ManagedCommentService
from teamwork_review_agents.models import CommitStatusTarget, PreflightResult
from teamwork_review_agents.preflight import PreflightExecutor, build_failure_comment_body
from teamwork_review_agents.preflight_log_worker import PreflightLogWorker
from teamwork_review_agents.preflight_log_uploads import ensure_gitlab_log_upload, export_ci_log
from teamwork_review_agents.providers.base import ProviderError
from teamwork_review_agents.providers.gitlab import GitLabProvider
from teamwork_review_agents.state import StateStore


@pytest.fixture
def upload_case(configured_app_factory, snapshot_factory, monkeypatch):
    """构造隔离数据库，所有远端请求均由替身承接。"""

    config = configured_app_factory()
    repository = config.repositories[0]
    repository.provider = "gitlab-main"
    repository.environment["PRIVATE_VALUE"] = EnvironmentVariable(value="split-private-secret", secret=True)
    monkeypatch.setenv("GITLAB_TOKEN", "provider-secret")
    store = StateStore(config.database.path)
    store.initialize()
    snapshot = snapshot_factory(provider="gitlab-main", repository_id=repository.id, head_sha="a" * 40)
    event = detect_events(None, snapshot, emit_initial=True)[0]
    store.save_snapshot_and_events(snapshot, [event])
    store.begin_preflight_run(
        proposed_run_id="ci-upload", idempotency_key="ci-upload", event_id=event.id,
        repository_id=repository.id, number=7, head_sha="a" * 40,
        config_revision=config.revision, max_attempts=3,
    )
    store.initialize_preflight_steps("ci-upload", [{"name": "tests", "command": ["bun", "test"], "timeout_seconds": 60}])
    result = PreflightResult(run_id="ci-upload", repository_id=repository.id, number=7,
                             head_sha="a" * 40, status="failure", failed_step="tests", exit_code=1,
                             output="仅有界末尾摘要")
    store.finish_preflight_run(result)
    for chunk in ("日志开头\nsplit-private-", "secret\n" + "完整中文输出😀\n" * 3000, "末行 provider-secret\n"):
        store.append_preflight_log(result.run_id, stream="stdout", event_type="output", payload=chunk)
    url = "https://gitlab.example.com/-/project/23/uploads/abc/ci.log"
    remote = SimpleNamespace(upload_ci_log=AsyncMock(return_value=url))
    return SimpleNamespace(config=config, repository=repository, store=store, result=result, remote=remote, url=url, event=event)


async def upload(case):
    """通过真实附件服务入口运行。"""

    return await ensure_gitlab_log_upload(case.config, case.store, case.repository, case.result.run_id, case.remote)


@pytest.mark.parametrize("status", ["success", "failure", "timed_out", "cancelled", "error"])
async def test_upload_preserves_complete_output_and_redacts_across_chunks(upload_case, status):
    """所有执行终态保存完整日志，命令和首尾输出可见，已知凭据不能泄露。"""

    case = upload_case
    case.store.finish_preflight_run(case.result.model_copy(update={"status": status}))
    captured = []

    async def capture(_repository, file, filename):
        """上传时读取临时文件，验证其后会被自动关闭。"""

        captured.append((file.read().decode(), filename, file))
        return case.url

    case.remote.upload_ci_log.side_effect = capture
    assert await upload(case) == case.url
    content, filename, file = captured[0]
    assert "日志开头" in content and "末行" in content
    assert content.count("完整中文输出😀") == 3000
    assert "split-private" not in content and "provider-secret" not in content
    assert "********" in content and '"bun"' in content
    assert '"started_at"' in content and '"exit_code": 1' in content
    assert "仅有界末尾摘要" not in content
    assert filename == "ci-ci-upload-1.log" and file.closed
    assert case.store.get_preflight_run(case.result.run_id)["status"] == status
    assert case.store.get_preflight_run(case.result.run_id)["log_upload"]["status"] == "completed"


async def test_concurrent_upload_and_restart_reuse_saved_attachment(upload_case):
    """并发请求与服务重建只上传一次，重复状态回写直接使用已保存附件。"""

    case = upload_case
    assert await asyncio.gather(upload(case), upload(case)) == [case.url, case.url]
    case.store = StateStore(case.config.database.path)
    case.store.initialize()
    assert await upload(case) == case.url
    case.remote.upload_ci_log.assert_awaited_once()


async def test_interrupted_upload_resumes_remaining_attempts(upload_case):
    """中断保留已用次数，重建服务后恢复传输，不能把实际 CI 结果改成取消。"""

    case = upload_case
    entered = asyncio.Event()

    async def hold(*_args):
        """仅在网络等待阶段模拟中断。"""

        entered.set()
        await asyncio.Event().wait()

    case.remote.upload_ci_log.side_effect = hold
    task = asyncio.create_task(upload(case))
    await asyncio.wait_for(entered.wait(), timeout=3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert case.store.get_preflight_log_upload(case.result.run_id)["status"] == "uploading"
    case.store = StateStore(case.config.database.path)
    case.remote.upload_ci_log.side_effect = None
    assert await upload(case) == case.url
    assert case.store.get_preflight_log_upload(case.result.run_id)["attempts"] == 2
    assert case.store.get_preflight_run(case.result.run_id)["status"] == "failure"


async def test_successful_attachment_survives_auxiliary_log_failure(upload_case, monkeypatch):
    """附加日志无法写入时保留已上传地址，避免后续重试重复上传文件。"""

    case = upload_case

    def fail_log(*_args, **_kwargs):
        """只模拟附加日志故障，不影响运行和附件状态表。"""

        raise OSError("log unavailable")

    monkeypatch.setattr(case.store, "append_preflight_log", fail_log)
    assert await upload(case) == case.url
    assert await upload(case) == case.url
    case.remote.upload_ci_log.assert_awaited_once()


async def test_new_execution_attempt_does_not_reuse_old_attachment(upload_case):
    """同一运行 ID 的基础设施重试是新的执行，附件按尝试次数隔离。"""

    case = upload_case
    assert await upload(case) == case.url
    case.store.finish_preflight_run(case.result.model_copy(update={"status": "error"}))
    reservation = case.store.begin_preflight_run(
        proposed_run_id="unused", idempotency_key="ci-upload", event_id=case.event.id,
        repository_id=case.repository.id, number=7, head_sha=case.result.head_sha,
        config_revision=case.config.revision, max_attempts=3,
    )
    assert reservation.attempts == 2
    assert case.store.get_preflight_log_upload(case.result.run_id) is None
    case.store.finish_preflight_run(case.result)
    assert await upload(case) == case.url
    assert case.remote.upload_ci_log.await_count == 2
    assert case.remote.upload_ci_log.call_args.args[2].endswith("-2.log")


async def test_transient_upload_retry_keeps_same_file(upload_case):
    """临时网络错误限次重试，重试成功后保存一次可复用结果。"""

    case = upload_case
    case.remote.upload_ci_log.side_effect = [httpx.ConnectError("temporary"), case.url]
    assert await upload(case) == case.url
    assert case.store.get_preflight_log_upload(case.result.run_id)["attempts"] == 2
    assert case.remote.upload_ci_log.call_args_list[0].args[1] is case.remote.upload_ci_log.call_args_list[1].args[1]


@pytest.mark.parametrize("failure", ["timeout", "denied", "exhausted"])
async def test_upload_failure_is_bounded_and_does_not_change_ci(upload_case, monkeypatch, failure):
    """总超时、权限拒绝及重试耗尽均独立报错，不改写 CI 结果或反复上传。"""

    case = upload_case
    if failure == "timeout":
        monkeypatch.setattr("teamwork_review_agents.preflight_log_uploads.UPLOAD_TOTAL_TIMEOUT_SECONDS", 0.05)

        async def hang(*_args):
            """由服务总时限取消等待，不消耗真实网络资源。"""

            await asyncio.sleep(10)

        case.remote.upload_ci_log.side_effect = hang
    elif failure == "denied":
        request = httpx.Request("POST", "https://gitlab.example.com/api/v4/projects/23/uploads")
        cause = httpx.HTTPStatusError("denied", request=request, response=httpx.Response(403, request=request))
        error = ProviderError("拒绝 provider-secret")
        error.__cause__ = cause
        case.remote.upload_ci_log.side_effect = error
    else:
        case.remote.upload_ci_log.side_effect = httpx.ConnectError("provider-secret")
    assert await upload(case) is None
    saved = case.store.get_preflight_log_upload(case.result.run_id)
    assert saved["status"] == "failed" and "provider-secret" not in saved["error"]
    assert case.store.get_preflight_run(case.result.run_id)["status"] == "failure"
    assert case.remote.upload_ci_log.await_count == (3 if failure == "exhausted" else 1)
    assert await upload(case) is None
    logs = list(case.store.iter_preflight_logs(case.result.run_id))
    assert any(entry["event_type"] == "log_upload_error" for entry in logs)


async def test_status_failure_still_publishes_real_result_without_internal_url(upload_case, monkeypatch):
    """上传失败不能阻止终态回写，也不能恢复管理员后台链接。"""

    case = upload_case
    case.config.web.public_url = "https://admin.example.com"
    case.remote.upload_ci_log.side_effect = ProviderError("没有上传权限")
    case.remote.set_commit_status = AsyncMock(return_value=None)

    class RemoteContext:
        """只模拟 Provider 生命周期，保持真实编排和持久化。"""

        async def __aenter__(self):
            return case.remote

        async def __aexit__(self, *_args):
            return None

    monkeypatch.setattr("teamwork_review_agents.preflight.create_provider", lambda *_a, **_kw: RemoteContext())
    result = await PreflightExecutor(case.config, case.store)._publish_terminal_result(case.repository, case.result)
    assert result.status == "failure" and result.status_published
    assert case.remote.set_commit_status.call_args.kwargs["state"] == "failure"
    assert case.remote.set_commit_status.call_args.kwargs["target_url"] == ""
    case.remote.upload_ci_log.assert_not_awaited()
    assert case.store.get_preflight_log_delivery(result.run_id)["status"] == "queued"
    await PreflightLogWorker(lambda: case.config, case.store).drain()
    assert case.store.get_preflight_log_delivery(result.run_id)["status"] == "failed"
    assert case.store.get_preflight_run(result.run_id)["status_published"]
    assert case.store.get_preflight_run(result.run_id)["status"] == "failure"


@pytest.fixture
def delivery_case(upload_case, monkeypatch):
    """使用真实队列和发布逻辑，模拟远端流水线、评论与文件传输。"""

    case = upload_case
    target = CommitStatusTarget(project="23", sha=case.result.head_sha, ref="refs/merge-requests/7/head", pipeline_id=71)
    case.remote.set_commit_status = AsyncMock(return_value=target)
    case.remote.create_change_request_comment = AsyncMock(return_value="comment-1")
    case.remote.update_change_request_comment = AsyncMock(return_value=True)
    case.remote.delete_change_request_comment = AsyncMock()
    case.repository.preflight.publish_failure_comment = True

    class RemoteContext:
        """跨任务共享调用记录，不访问真实 GitLab。"""

        async def __aenter__(self):
            return case.remote

        async def __aexit__(self, *_args):
            return None

    for module in ("preflight", "managed_comments"):
        monkeypatch.setattr(f"teamwork_review_agents.{module}.create_provider", lambda *_a, **_kw: RemoteContext())
    return case


@pytest.mark.parametrize("status", ["success", "failure"])
async def test_ci_terminal_is_visible_while_upload_is_blocked(delivery_case, status):
    """即使附件网络永久等待，门禁已经返回且平台已收到终态。"""

    case = delivery_case
    case.result = case.result.model_copy(update={"status": status})
    case.store.finish_preflight_run(case.result)
    entered, release = asyncio.Event(), asyncio.Event()

    async def hold(*_args):
        """用同步屏障观察状态顺序，不依靠上传速度推断。"""

        entered.set()
        await release.wait()
        return case.url

    case.remote.upload_ci_log.side_effect = hold
    result = await asyncio.wait_for(PreflightExecutor(case.config, case.store)._publish_terminal_result(case.repository, case.result), 3)
    assert result.status == status and result.status_published
    case.remote.upload_ci_log.assert_not_awaited()
    task = asyncio.create_task(PreflightLogWorker(lambda: case.config, case.store).drain())
    try:
        await asyncio.wait_for(entered.wait(), 3)
        assert not task.done()
        assert case.remote.set_commit_status.await_count == 1
        assert case.remote.set_commit_status.call_args.kwargs["state"] == status
        assert case.remote.set_commit_status.call_args.kwargs["target_url"] == ""
        assert case.store.get_preflight_run(result.run_id)["status_published"]
    finally:
        release.set()
        await task
    assert [call.kwargs["state"] for call in case.remote.set_commit_status.call_args_list] == [status, status]
    assert case.remote.set_commit_status.call_args.kwargs["target_url"] == case.url
    assert case.store.get_preflight_log_delivery(result.run_id)["status"] == "completed"
    if status == "failure":
        assert case.url in case.remote.update_change_request_comment.call_args.args[2]


async def test_restart_retries_link_only_and_stops_after_limit(delivery_case):
    """链接发布错误跨重启重试，但已成功的附件不重传，重试有上限。"""

    case = delivery_case
    await PreflightExecutor(case.config, case.store)._publish_terminal_result(case.repository, case.result)
    case.remote.set_commit_status.side_effect = ProviderError("临时错误 provider-secret")
    for attempt in range(1, 4):
        case.store = StateStore(case.config.database.path)
        case.store.initialize()
        await PreflightLogWorker(lambda: case.config, case.store).drain()
        job = case.store.get_preflight_log_delivery(case.result.run_id)
        assert job["attempts"] == attempt
        assert job["status"] == ("failed" if attempt == 3 else "queued")
        assert "provider-secret" not in job["error"]
        assert case.store.get_preflight_run(case.result.run_id)["status_published"]
        with case.store.connect() as connection:
            connection.execute("UPDATE preflight_log_deliveries SET next_attempt_at=0")
    case.remote.upload_ci_log.assert_awaited_once()
    assert not case.store.pending_preflight_log_deliveries()


async def test_old_upload_cannot_overwrite_new_ci_result(delivery_case):
    """同一源提交的新 CI 已经通过时，迟到的旧失败附件不得复活失败状态或评论。"""

    case = delivery_case
    executor = PreflightExecutor(case.config, case.store)
    await executor._publish_terminal_result(case.repository, case.result)
    entered, release = asyncio.Event(), asyncio.Event()

    async def hold(*_args):
        """延迟旧上传，让新 CI 在相同 MR 与 SHA 上先发布。"""

        entered.set()
        await release.wait()
        return case.url

    case.remote.upload_ci_log.side_effect = hold
    task = asyncio.create_task(PreflightLogWorker(lambda: case.config, case.store).drain())
    try:
        await asyncio.wait_for(entered.wait(), 3)
        case.store.begin_preflight_run(
            proposed_run_id="new-ci", idempotency_key="new-ci", event_id=case.event.id,
            repository_id=case.repository.id, number=7, head_sha=case.result.head_sha,
            config_revision=case.config.revision, max_attempts=3,
        )
        newer = case.result.model_copy(update={"run_id": "new-ci", "status": "success"})
        case.store.finish_preflight_run(newer)
        await executor._publish_terminal_result(case.repository, newer)
    finally:
        release.set()
        await task
    assert [call.kwargs["state"] for call in case.remote.set_commit_status.call_args_list] == ["failure", "success"]
    case.remote.update_change_request_comment.assert_not_awaited()
    assert case.store.get_preflight_log_delivery(case.result.run_id)["status"] == "obsolete"


async def test_background_stop_cancels_upload_and_restart_resumes(delivery_case):
    """关闭后台立即取消上传等待，重启消费剩余额度且不重跑 CI。"""

    case = delivery_case
    await PreflightExecutor(case.config, case.store)._publish_terminal_result(case.repository, case.result)
    entered = asyncio.Event()

    async def hold(*_args):
        """只等待取消，不主动结束网络阶段。"""

        entered.set()
        await asyncio.Event().wait()

    case.remote.upload_ci_log.side_effect = hold
    worker = PreflightLogWorker(lambda: case.config, case.store)
    stop = asyncio.Event()
    task = asyncio.create_task(worker.run(stop, lambda: False, lambda: None))
    try:
        await asyncio.wait_for(entered.wait(), 3)
    finally:
        stop.set()
        await asyncio.wait_for(task, 2)
    assert not worker.running
    assert case.store.get_preflight_log_delivery(case.result.run_id)["status"] == "running"
    case.store = StateStore(case.config.database.path)
    case.store.initialize()
    case.remote.upload_ci_log.side_effect = None
    await PreflightLogWorker(lambda: case.config, case.store).drain()
    assert case.store.get_preflight_log_delivery(case.result.run_id)["status"] == "completed"
    assert case.store.get_preflight_run(case.result.run_id)["status"] == "failure"
    assert case.remote.set_commit_status.await_count == 2


@pytest.mark.parametrize("report_first", [True, False])
async def test_report_and_attachment_updates_preserve_each_other(delivery_case, report_first):
    """无论报告或附件先完成，后到的旧正文都必须在评论锁内重建。"""

    case = delivery_case
    await PreflightExecutor(case.config, case.store)._publish_terminal_result(case.repository, case.result)
    stale_body = case.remote.create_change_request_comment.call_args.args[2]

    async def publish_report():
        """模拟诊断早已构造正文，实际发布时需重新读取当前附件。"""

        case.store.begin_preflight_diagnosis(case.result.run_id, "diagnoser")
        case.store.finish_preflight_diagnosis(case.result.run_id, status="completed", report="诊断结论：测试断言错误")
        await ManagedCommentService(case.config, case.store).publish_latest(
            repository_id=case.repository.id, number=7, namespace="preflight",
            slot=case.repository.preflight.status_context, source_generation=1,
            source_head_sha=case.result.head_sha, body=stale_body,
            replace_existing=False, update_existing=True, preflight_run_id=case.result.run_id,
            refresh_preflight_body=True,
        )

    if report_first:
        await publish_report()
    await PreflightLogWorker(lambda: case.config, case.store).drain()
    if not report_first:
        await publish_report()
    body = case.remote.update_change_request_comment.call_args.args[2]
    assert case.url in body
    assert "诊断结论：测试断言错误" in body
    assert "仅有界末尾摘要" in body
    assert "<summary>CI 失败诊断报告</summary>" in body


def test_legacy_log_fallback_is_explicit_and_attachment_survives_diagnosis(upload_case):
    """旧记录只剩摘要时不能称为完整输出，诊断评论仍保留项目附件入口。"""

    case = upload_case
    with case.store.connect() as connection:
        connection.execute("DELETE FROM preflight_logs WHERE run_id=?", (case.result.run_id,))
    file = io.BytesIO()
    export_ci_log(case.store, case.store.get_preflight_run(case.result.run_id), file, SecretRedactor(()))
    assert "并非完整日志" in file.getvalue().decode()
    assert "仅有界末尾摘要" in file.getvalue().decode()
    body = build_failure_comment_body(case.repository, case.result, redactor=SecretRedactor(()),
                                     diagnosis_report="## 原因\n断言错误", log_url=case.url)
    assert f"[下载完整 CI 运行日志]({case.url})" in body
    assert "<summary>CI 失败诊断报告</summary>" in body


@pytest.mark.parametrize("payload,path", [
    ({"full_path": "/-/project/23/uploads/hash/ci.log"}, "/-/project/23/uploads/hash/ci.log"),
    ({"full_path": "/gitlab/owner/demo/uploads/hash/ci.log"}, "/gitlab/owner/demo/uploads/hash/ci.log"),
    ({"url": "/uploads/hash/ci.log"}, "/gitlab/owner/demo/uploads/hash/ci.log"),
])
async def test_gitlab_multipart_upload_and_versioned_paths(upload_case, payload, path):
    """真实 multipart 请求包含文件且只访问当前实例，兼容旧路径与反向代理前缀。"""

    case = upload_case
    provider = GitLabProvider("gitlab-main", case.config.providers["gitlab-main"], case.config.scanner, token="provider-secret")
    provider.config.base_url = "https://gitlab.example.com/gitlab/api/v4"
    await provider.client.aclose()

    def handler(request):
        """所有认证与文件断言均基于虚构凭据及内存内容。"""

        assert request.url.raw_path == b"/gitlab/api/v4/projects/owner%2Fdemo/uploads"
        assert request.headers["PRIVATE-TOKEN"] == "provider-secret"
        assert request.headers["Content-Type"].startswith("multipart/form-data;")
        assert b'filename="ci.log"' in request.content and b"full log" in request.content
        return httpx.Response(201, json=payload)

    provider.client = httpx.AsyncClient(base_url=provider.config.base_url + "/", transport=httpx.MockTransport(handler), headers=provider.headers())
    async with provider:
        assert await provider.upload_ci_log(case.repository, io.BytesIO(b"full log"), "ci.log") == "https://gitlab.example.com" + path


@pytest.mark.parametrize("path", ["https://evil.example/uploads/log", "//evil.example/uploads/log", "/uploads/log?token=x", "/uploads/../log", "/uploads/has space.log"])
async def test_gitlab_rejects_unexpected_attachment_url(upload_case, path):
    """异常响应不能引入跨站链接、查询凭据或路径跳转。"""

    case = upload_case
    provider = GitLabProvider("gitlab-main", case.config.providers["gitlab-main"], case.config.scanner, token="test")
    await provider.client.aclose()
    provider.client = httpx.AsyncClient(base_url=provider.config.base_url + "/", transport=httpx.MockTransport(lambda _r: httpx.Response(201, json={"full_path": path})))
    async with provider:
        with pytest.raises(ProviderError):
            await provider.upload_ci_log(case.repository, io.BytesIO(b"log"), "ci.log")
