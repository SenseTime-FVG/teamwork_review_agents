"""GitLab 社区版审批语义、能力探测与 Agent 输入回归。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from teamwork_review_agents.config import ProviderConfig, RepositoryConfig, ScannerConfig
from teamwork_review_agents.events import detect_events
from teamwork_review_agents.executor import AgentExecutor
from teamwork_review_agents.gitlab_approvals import gitlab_approval_context
from teamwork_review_agents.providers.gitlab import GitLabProvider
from teamwork_review_agents.state import StateStore


@pytest.mark.parametrize(
    ("version_response", "expected"),
    [
        (httpx.Response(200, json={"enterprise": False, "revision": "不应传入"}), {"enterprise": False}),
        (httpx.Response(200, json={"enterprise": True}), {"enterprise": True}),
        (httpx.Response(200, json={"version": "17.5.1"}), {}),
        (httpx.Response(200, json={"enterprise": "false"}), {}),
        (httpx.Response(200, json={"enterprise": 0}), {}),
        (httpx.Response(200, json={"enterprise": None}), {}),
        (httpx.Response(200, json=[]), {}),
        (httpx.Response(200, content=b"not-json"), {}),
        (httpx.Response(401), {}),
        (httpx.Response(403), {}),
        (httpx.Response(404), {}),
        (httpx.Response(500), {}),
        (None, {}),
    ],
)
async def test_gitlab_instance_probe_is_shared_optional_and_read_only(
    version_response, expected, snapshot_factory,
) -> None:
    """并发 MR 只探测一次版本，失败不丢快照且绝不通过响应形状猜测版本。"""

    paths: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.headers["PRIVATE-TOKEN"] == "test-token"
        paths.append(request.url.path)
        if request.url.path == "/api/v4/version":
            # 让其他 MR 同时等待版本锁，验证不是各自发起一次探测。
            await asyncio.sleep(0)
            if version_response is None:
                raise httpx.ReadTimeout("测试超时", request=request)
            return version_response
        if request.url.path.endswith("/approvals"):
            return httpx.Response(200, json={"approved": False, "approved_by": []})
        if request.url.path.endswith("/merge_requests/7"):
            return httpx.Response(200, json={
                "iid": 7, "title": "测试审批", "state": "opened",
                "source_branch": "feature/demo", "target_branch": "main",
                "sha": "a" * 40, "detailed_merge_status": "mergeable",
                "updated_at": "2026-08-17T08:00:00Z",
            })
        raise AssertionError(f"不应读取额外接口：{request.url.path}")

    provider = GitLabProvider(
        "gitlab-main",
        ProviderConfig(kind="gitlab", base_url="https://gitlab.example.com/api/v4", token_env="GITLAB_TOKEN"),
        ScannerConfig(), token="test-token",
    )
    await provider.client.aclose()
    provider.client = httpx.AsyncClient(
        base_url="https://gitlab.example.com/api/v4/",
        headers=provider.headers(), transport=httpx.MockTransport(handler),
    )
    repository = RepositoryConfig(
        id="demo", provider="gitlab-main", project="group/demo", workspace=Path("/tmp/demo"),
    )
    try:
        snapshots = await asyncio.gather(*(
            provider._build_snapshot(repository, "group%2Fdemo", {"iid": 7})
            for _ in range(8)
        ))
        later = await provider._build_snapshot(repository, "group%2Fdemo", {"iid": 7})
    finally:
        await provider.close()

    assert paths.count("/api/v4/version") == 1
    assert len(paths) == 19
    for snapshot in [*snapshots, later]:
        assert snapshot.raw["gitlab_instance"] == expected
        assert snapshot.approvals == 0
        assert snapshot.raw["approvals"]["approved"] is False
        old = snapshot_factory(**snapshot.model_dump(exclude={"raw"}), raw={})
        assert snapshot.normalized_payload() == old.normalized_payload()
        assert detect_events(old, snapshot) == []


@pytest.mark.parametrize(
    ("instance", "approvals", "merge_status", "edition", "requirement"),
    [
        ({"enterprise": False}, {"approved": False}, "mergeable", "community", "optional"),
        ({"enterprise": False}, {"approved": True}, "mergeable", "community", "optional"),
        ({"enterprise": False}, {}, "mergeable", "community", "optional"),
        ({"enterprise": True}, {"approved": False}, "mergeable", "enterprise", "unknown"),
        ({"enterprise": True}, {"approved": True}, "mergeable", "enterprise", "unknown"),
        ({}, {"approved": False}, "mergeable", "unknown", "unknown"),
        ({"enterprise": "false"}, {}, "mergeable", "unknown", "unknown"),
        ({"enterprise": 0}, {}, "mergeable", "unknown", "unknown"),
        (None, None, "mergeable", "unknown", "unknown"),
        ({"enterprise": False}, {"approved": False}, "not_approved", "community", "required"),
        ({"enterprise": False}, {"approvals_required": 2}, "mergeable", "community", "required"),
        ({"enterprise": False}, {"approvals_left": 1}, "mergeable", "community", "required"),
        ({"enterprise": True}, {"approvals_required": 2, "approvals_left": 0, "approved": True}, "mergeable", "enterprise", "required"),
        ({}, {"approvals_left": 1}, "mergeable", "unknown", "required"),
        ({"enterprise": False}, {"approvals_required": "1"}, "mergeable", "community", "unknown"),
        ({"enterprise": False}, {"approvals_left": False}, "mergeable", "community", "unknown"),
        ({"enterprise": False}, {"approvals_left": -1}, "mergeable", "community", "unknown"),
    ],
)
def test_gitlab_approval_context_preserves_evidence_and_explicit_gates(
    instance, approvals, merge_status, edition, requirement, snapshot_factory,
) -> None:
    """社区版必须有布尔版本证据，明确强制门禁或异常数量不能被可选提示覆盖。"""

    snapshot = snapshot_factory(
        merge_status=merge_status,
        raw={"gitlab_instance": instance, "approvals": approvals},
    )
    context = gitlab_approval_context(snapshot)

    assert context["edition"] == edition
    assert context["requirement"] == requirement
    assert context["edition_source"] == ("unavailable" if edition == "unknown" else "version_api")
    assert context["approved"] is (approvals or {}).get("approved")
    for field in ("approvals_required", "approvals_left"):
        value = (approvals or {}).get(field)
        assert context[field] == (value if type(value) is int and value >= 0 else None)
    assert snapshot.approvals == 0


@pytest.mark.parametrize("approved", [0, 1, "false", "true", {}, []])
def test_gitlab_approval_context_rejects_non_boolean_approved(approved, snapshot_factory) -> None:
    """非布尔批准状态保持未知，不允许通过真值转换伪造批准证据。"""

    context = gitlab_approval_context(snapshot_factory(raw={"approvals": {"approved": approved}}))
    assert context["approved"] is None
    assert context["edition"] == "unknown"
    assert context["requirement"] == "unknown"


@pytest.mark.parametrize("auto_merge", ["true", "false"])
@pytest.mark.parametrize("provider_name", ["gitlab-main", "github-main"])
@pytest.mark.parametrize("enterprise", [False, True, None])
def test_review_prompt_receives_safe_gitlab_approval_context(
    auto_merge, provider_name, enterprise, snapshot_factory, configured_app_factory,
) -> None:
    """真实根输入携带社区版证据而不泄漏原始审批资料，也不污染 GitHub。"""

    config = configured_app_factory()
    repository = config.repositories[0]
    repository.provider = provider_name
    repository.project = "group/demo"
    config.agents["code-reviewer"] = config.agents["code-reviewer"].model_copy(update={
        "prompt": None,
        "prompt_file": Path(__file__).resolve().parents[1] / "prompts/general-review.md",
    })
    snapshot = snapshot_factory(
        provider=provider_name, merge_status="mergeable",
        raw={
            "gitlab_instance": {"enterprise": enterprise, "untrusted": "不可进入上下文的版本资料"},
            "approvals": {"approved": False, "approved_by": [], "untrusted": "不可进入上下文的审批资料"},
            "merge_request": {"description": "不可进入上下文的原始描述"},
        },
    )
    event = detect_events(None, snapshot, emit_initial=True)[0]
    prompt = AgentExecutor(config, StateStore(config.database.path)).build_prompt(
        agent_name="code-reviewer", event=event, repository=repository,
        task=None, extra_context=None, prompt_values={"REVIEW_AUTO_MERGE": auto_merge},
        change_ref="refs/teamwork/change-requests/7/head", actions=(event.type,),
        target_head_sha="d" * 40,
    )
    context_json = prompt.split("# 本次运行上下文\n\n```json\n", 1)[1].split("\n```", 1)[0]
    mr = json.loads(context_json)["mr"]

    assert mr["approvals"] == 0
    if provider_name == "gitlab-main":
        expected = "community" if enterprise is False else "enterprise" if enterprise is True else "unknown"
        assert mr["approval_context"] == {
            "edition": expected,
            "edition_source": "unavailable" if enterprise is None else "version_api",
            "requirement": "optional" if enterprise is False else "unknown",
            "approved": False, "approvals_required": None, "approvals_left": None,
        }
    else:
        assert "approval_context" not in mr
    assert "不可进入上下文的" not in prompt
    assert "人工审批：不适用（社区版可选审批；当前无人批准）" in prompt
    assert "不因旧上下文仍为 `unknown` 而阻断" in prompt
    assert "企业版的 `approved: false` 不能套用社区版解释" in prompt
    assert "不能把 403、404、空响应或缺失的审批数量当成“无需审批”" in prompt
    assert "`mergeable: true` 本身不能覆盖这些阻断" in prompt
    assert "不为消除该状态主动提交正式 Approval" in prompt
    assert "本轮有效的远端 CI 明确失败就立即终止审核" in prompt
    assert "当前目标 SHA 仍等于 `REVIEW_TARGET_SHA`" in prompt
