"""本地 CI 与 GitLab MR 流水线关联、生命周期及跨重启补发回归。"""

from __future__ import annotations

import json
import sqlite3
import sys
from contextlib import contextmanager
from pathlib import Path

import httpx
import pytest

from teamwork_review_agents.config import ProviderConfig, RepositoryConfig, ScannerConfig, parse_config_data
from teamwork_review_agents.events import detect_events
from teamwork_review_agents.models import CommitStatusTarget
from teamwork_review_agents.preflight import PreflightExecutor, StepExecutionOutcome, preflight_idempotency_key
from teamwork_review_agents.providers.base import ProviderError
from teamwork_review_agents.providers.github import GitHubProvider
from teamwork_review_agents.providers.gitlab import GitLabProvider
from teamwork_review_agents.state import StateStore


SHA = "a" * 40
MR_REF = "refs/merge-requests/7/head"


def mr_pipeline(**overrides):
    """默认模拟 MR 与分支流水线同时存在时，页面正在展示的 MR 流水线。"""

    return {"id": 71, "project_id": 23, "sha": SHA, "source": "merge_request_event",
            "ref": MR_REF, **overrides}


async def status_provider(handler, kind="gitlab"):
    """所有请求都落到内存替身，不创建或更新真实 GitLab 作业。"""

    provider_type = GitLabProvider if kind == "gitlab" else GitHubProvider
    provider = provider_type(
        "platform", ProviderConfig(kind=kind, base_url="https://platform.example/api/v4",
                                   token_env="TEST_TOKEN"), ScannerConfig(), token="test-token",
    )
    await provider.client.aclose()
    provider.client = httpx.AsyncClient(
        base_url="https://platform.example/api/v4/", transport=httpx.MockTransport(handler),
    )
    return provider


def repository():
    """构造目标项目，显式跨 Fork 场景由每个调用指定源项目。"""

    return RepositoryConfig(id="demo", provider="platform", project="group/demo", workspace=Path("/tmp/demo"))


@pytest.mark.parametrize("state,expected", [
    ("pending", "pending"), ("running", "running"), ("success", "success"),
    ("failure", "failed"), ("error", "failed"), ("cancelled", "canceled"),
])
async def test_status_uses_matching_mr_pipeline_instead_of_push_pipeline(state, expected):
    """按实际流水线所属项目、引用和 ID 写回，而非按源分支碰运气选址。"""

    requests = []

    def handler(request):
        requests.append(request)
        if request.method == "GET":
            assert request.url.raw_path.decode() == "/api/v4/projects/group%2Fdemo/merge_requests/7"
            return httpx.Response(200, json={"head_pipeline": mr_pipeline()})
        assert request.url.path == f"/api/v4/projects/23/statuses/{SHA}"
        assert json.loads(request.content) == {
            "state": expected, "name": "teamwork/local-ci", "description": "状态",
            "pipeline_id": 71, "ref": MR_REF,
        }
        return httpx.Response(201, json={"pipeline_id": 71})

    async with await status_provider(handler) as provider:
        target = await provider.set_commit_status(
            repository(), SHA, number=7, state=state, context="teamwork/local-ci", description="状态",
            ref="feature/demo", source_project="fork/demo",
        )
    assert len(requests) == 2
    assert target == CommitStatusTarget(project="23", sha=SHA, ref=MR_REF, pipeline_id=71)


@pytest.mark.parametrize("pipeline", [
    None, mr_pipeline(sha="b" * 40), mr_pipeline(source="push"),
    mr_pipeline(id=0), mr_pipeline(id=True), mr_pipeline(ref=None),
])
async def test_unmatched_mr_pipeline_falls_back_to_exact_source_commit(pipeline):
    """不存在匹配项时可用源分支，但不能把旧结果写入新 Head 或拟合并提交。"""

    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json={"head_pipeline": pipeline})
        assert request.url.raw_path.decode() == f"/api/v4/projects/fork%2Fdemo/statuses/{SHA}"
        payload = json.loads(request.content)
        assert payload["ref"] == "feature/demo" and "pipeline_id" not in payload
        return httpx.Response(201, json={"pipeline_id": 41})

    async with await status_provider(handler) as provider:
        target = await provider.set_commit_status(
            repository(), SHA, number=7, state="pending", context="ci", description="等待",
            ref="feature/demo", source_project="fork/demo",
        )
    assert target == CommitStatusTarget(project="fork/demo", sha=SHA, ref="feature/demo", pipeline_id=41)


async def test_fixed_target_does_not_follow_new_pipeline_and_retries_same_payload(monkeypatch):
    """运行中即使出现更新流水线，或状态接口返回 409，都始终更新首次固定的位置。"""

    reads, posts = [], []

    async def skip_delay(_seconds):
        """冲突重试无需在测试中真实等待。"""

    monkeypatch.setattr("teamwork_review_agents.providers.gitlab.asyncio.sleep", skip_delay)

    def handler(request):
        if request.method == "GET":
            reads.append(request)
            return httpx.Response(200, json={"head_pipeline": mr_pipeline(id=71 if len(reads) == 1 else 99)})
        posts.append(json.loads(request.content))
        if len(posts) == 2:
            return httpx.Response(409, json={"message": "conflict"})
        return httpx.Response(201, json={"pipeline_id": 71})

    async with await status_provider(handler) as provider:
        target = None
        for state in ("pending", "running", "success"):
            target = await provider.set_commit_status(
                repository(), SHA, number=7, state=state, context="ci", description="状态",
                ref="feature/demo", target=target,
            )
    assert len(reads) == 1
    assert [payload["state"] for payload in posts] == ["pending", "running", "running", "success"]
    assert all(payload["pipeline_id"] == 71 and payload["ref"] == MR_REF for payload in posts)


@pytest.mark.parametrize("code", [401, 403, 404, 500])
async def test_mr_lookup_errors_do_not_silently_publish_to_other_pipeline(code):
    """认证、权限或查询错误必须显式失败，不能伪装成 MR 没有流水线。"""

    def handler(request):
        assert request.method == "GET"
        return httpx.Response(code, json={"message": "error"})

    async with await status_provider(handler) as provider:
        with pytest.raises(ProviderError):
            await provider.set_commit_status(
                repository(), SHA, number=7, state="running", context="ci", description="运行",
            )


async def test_fixed_target_rejects_different_commit_and_missing_pipeline():
    """固定目标校验失败或流水线已删除时不能悄悄换流水线并遗留旧运行状态。"""

    requests = []

    def handler(request):
        requests.append(request)
        assert request.method == "POST" and json.loads(request.content)["pipeline_id"] == 71
        return httpx.Response(404, json={"message": "not found"})

    async with await status_provider(handler) as provider:
        target = CommitStatusTarget(project="23", sha="b" * 40, ref=MR_REF, pipeline_id=71)
        with pytest.raises(ProviderError, match="SHA 不一致"):
            await provider.set_commit_status(repository(), SHA, state="success", context="ci", description="通过", target=target)
        assert requests == []
        with pytest.raises(ProviderError):
            await provider.set_commit_status(
                repository(), SHA, state="success", context="ci", description="通过",
                target=target.model_copy(update={"sha": SHA}),
            )
    assert len(requests) == 1


async def test_branch_fallback_stays_in_actual_pipeline_when_mr_pipeline_appears():
    """首轮没有 MR 流水线时，后续出现 MR 流水线也不能遗留原分支作业为等待中。"""

    reads, posts = [], []

    def handler(request):
        if request.method == "GET":
            reads.append(request)
            return httpx.Response(200, json={"head_pipeline": None if len(reads) == 1 else mr_pipeline()})
        posts.append(json.loads(request.content))
        return httpx.Response(201, json={"pipeline_id": 41})

    async with await status_provider(handler) as provider:
        target = None
        for state in ("pending", "running", "success"):
            target = await provider.set_commit_status(
                repository(), SHA, number=7, state=state, context="ci", description="状态",
                ref="feature/demo", target=target,
            )
    assert len(reads) == 1
    assert "pipeline_id" not in posts[0]
    assert [payload["pipeline_id"] for payload in posts[1:]] == [41, 41]
    assert all(payload["ref"] == "feature/demo" for payload in posts)


@pytest.mark.parametrize("state,expected", [("running", "pending"), ("cancelled", "error")])
async def test_github_maps_lifecycle_to_supported_commit_statuses(state, expected):
    """GitHub 继续使用 Commit Status 支持的枚举，不把 GitLab 状态直接发送过去。"""

    def handler(request):
        assert request.method == "POST" and json.loads(request.content)["state"] == expected
        return httpx.Response(201, json={"id": 1})

    async with await status_provider(handler, kind="github") as provider:
        await provider.set_commit_status(repository(), SHA, state=state, context="ci", description="状态")


@pytest.mark.parametrize("kind", ["github", "gitlab"])
@pytest.mark.parametrize("state", ["pending", "running", "success", "failure", "error", "cancelled"])
async def test_commit_status_details_link_is_kept_in_every_lifecycle_state(kind, state):
    """两个平台在等待、执行和各类终态都使用同一运行详情入口。"""

    url = "https://review.example.com/#preflight=run-123"

    def handler(request):
        """只检查 HTTP 请求体，不更新真实平台状态。"""

        payload = json.loads(request.content)
        assert request.method == "POST" and payload["target_url"] == url
        if kind == "gitlab":
            assert payload["pipeline_id"] == 71 and payload["ref"] == MR_REF
        return httpx.Response(201, json={"pipeline_id": 71})

    async with await status_provider(handler, kind=kind) as provider:
        await provider.set_commit_status(
            repository(), SHA, state=state, context="ci", description="状态", target_url=url,
            target=CommitStatusTarget(project="23", sha=SHA, ref=MR_REF, pipeline_id=71),
        )


@pytest.mark.parametrize("exit_code", [0, 7])
async def test_preflight_persists_mr_binding_and_retries_terminal_delivery_after_restart(
    tmp_path, monkeypatch, snapshot_factory, exit_code,
):
    """真实命令只执行一次，终态补发跨执行器重建仍使用原 MR 流水线。"""

    marker = tmp_path / "executions.txt"
    config = parse_config_data({
        "database": {"path": str(tmp_path / "state.db")},
        "web": {"public_url": "https://review.example.com/teamwork"},
        "providers": {"platform": {"kind": "gitlab", "base_url": "https://platform.example/api/v4", "token_env": "TEST_TOKEN"}},
        "repositories": [{"id": "demo", "provider": "platform", "project": "group/demo", "workspace": str(tmp_path / "workspace"),
                          "preflight": {"enabled": True, "cache_enabled": False, "steps": [{"name": "count", "command": [
                              sys.executable, "-c", f"from pathlib import Path; p=Path({str(marker)!r}); p.write_text((p.read_text() if p.exists() else '')+'run\\n'); raise SystemExit({exit_code})",
                          ]}]}}],
    }, tmp_path / "config.yaml")
    event = detect_events(None, snapshot_factory(provider="platform", repository_id="demo", head_sha=SHA), emit_initial=True)[0]
    posts, reads, uploads = [], [], []
    terminal_state = "success" if exit_code == 0 else "failed"

    def handler(request):
        if request.url.path.endswith("/uploads"):
            uploads.append(request.content)
            assert b'Teamwork' in request.content
            return httpx.Response(201, json={"full_path": "/-/project/23/uploads/abc/ci.log"})
        if request.method == "GET":
            reads.append(request)
            return httpx.Response(200, json={"head_pipeline": mr_pipeline(id=71 if len(reads) == 1 else 99)})
        payload = json.loads(request.content)
        posts.append(payload)
        assert payload["pipeline_id"] == 71 and payload["ref"] == MR_REF
        assert payload["target_url"] == (
            "" if payload["state"] in {"pending", "running"}
            else "https://platform.example/-/project/23/uploads/abc/ci.log"
        )
        if len(posts) == 3:
            return httpx.Response(503, json={"message": "retry later"})
        return httpx.Response(201, json={"pipeline_id": 71})

    def factory(*args, token, **kwargs):
        """保持真实 Provider 协议，仅替换 HTTP 传输。"""

        provider = GitLabProvider("platform", config.providers["platform"], config.scanner, token=token)
        provider.client = httpx.AsyncClient(base_url="https://platform.example/api/v4/", transport=httpx.MockTransport(handler))
        return provider

    @contextmanager
    def worktree(*args, **kwargs):
        """使用测试临时目录，不读取或改动任何真实仓库。"""

        yield tmp_path

    monkeypatch.setenv("TEST_TOKEN", "test-token")
    monkeypatch.setattr("teamwork_review_agents.preflight.create_provider", factory)
    monkeypatch.setattr("teamwork_review_agents.preflight.temporary_change_request_worktree", worktree)
    store = StateStore(config.database.path)
    store.initialize()
    first = await PreflightExecutor(config, store).ensure_passed(event)
    assert first.status == "error" and "回写失败" in first.error
    saved = store.load_preflight_result(preflight_idempotency_key(config, event))
    assert saved.status == ("success" if exit_code == 0 else "failure")
    assert saved.remote_status_target.pipeline_id == 71 and not saved.status_published
    restarted_store = StateStore(config.database.path)
    restarted_store.initialize()
    second = await PreflightExecutor(config, restarted_store).ensure_passed(event)
    assert second.reused and second.status == saved.status and second.status_published
    assert marker.read_text() == "run\n"
    assert len(reads) == 1
    assert [payload["state"] for payload in posts] == ["pending", "running", terminal_state, terminal_state]
    assert {payload["target_url"] for payload in posts} == {"", "https://platform.example/-/project/23/uploads/abc/ci.log"}
    assert len(uploads) == 1
    assert restarted_store.load_preflight_result(preflight_idempotency_key(config, event)).status_published


@pytest.mark.parametrize("outcome,expected", [("error", "error"), ("cancelled", "cancelled"), ("timed_out", "failure")])
async def test_started_preflight_always_finishes_remote_status(tmp_path, monkeypatch, snapshot_factory, outcome, expected):
    """命令执行阶段的基础设施异常、取消和超时均收尾固定作业，不遗留 running。"""

    config = parse_config_data({
        "database": {"path": str(tmp_path / "state.db")},
        "providers": {"platform": {"kind": "gitlab", "base_url": "https://platform.example/api/v4", "token_env": "TEST_TOKEN"}},
        "repositories": [{"id": "demo", "provider": "platform", "project": "group/demo", "workspace": str(tmp_path / "workspace"),
                          "preflight": {"enabled": True, "cache_enabled": False, "steps": [{"name": "test", "command": ["unused"]}]}}],
    }, tmp_path / "config.yaml")
    event = detect_events(None, snapshot_factory(provider="platform", repository_id="demo", head_sha=SHA), emit_initial=True)[0]
    target = CommitStatusTarget(project="23", sha=SHA, ref=MR_REF, pipeline_id=71)
    states = []

    async def publish(*args, state, **kwargs):
        """验证在步骤执行前就已保存目标，并收集整个生命周期。"""

        states.append(state)
        if state != "pending":
            assert kwargs["target"] == target
            assert store.load_preflight_result(preflight_idempotency_key(config, event)).remote_status_target == target
        return target

    async def execute(*args, **kwargs):
        """异常从真实编排入口传播，取消和超时使用执行器已有终态协议。"""

        if outcome == "error":
            raise OSError("模拟命令无法启动")
        return StepExecutionOutcome(status=outcome, failed_step="test")

    @contextmanager
    def worktree(*args, **kwargs):
        """使用临时目录替代检出。"""

        yield tmp_path

    monkeypatch.setenv("TEST_TOKEN", "test-token")
    monkeypatch.setattr("teamwork_review_agents.preflight.execute_preflight_steps", execute)
    monkeypatch.setattr("teamwork_review_agents.preflight.temporary_change_request_worktree", worktree)
    store = StateStore(config.database.path)
    store.initialize()
    executor = PreflightExecutor(config, store)
    monkeypatch.setattr(executor, "_set_remote_status", publish)
    result = await executor.ensure_passed(event)
    assert result.status == outcome and result.status_published
    assert states == ["pending", "running", expected]


def test_legacy_preflight_database_adds_nullable_status_target(tmp_path):
    """旧库升级只新增空的状态位置，不修改已有运行结果。"""

    store = StateStore(tmp_path / "legacy.db")
    store.initialize()
    store.create_manual_preflight_run(run_id="old-ci", repository_id="demo", config_revision="old")
    with sqlite3.connect(store.path) as connection:
        connection.execute("ALTER TABLE preflight_runs DROP COLUMN remote_status_target")
    store.initialize()
    result = store.load_preflight_result("manual:old-ci")
    assert result.run_id == "old-ci" and result.status == "running" and result.remote_status_target is None
