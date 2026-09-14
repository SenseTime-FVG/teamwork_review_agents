"""快速配置的范围矩阵、原子保存、凭据保护及仓库 Skill 隔离。"""

import copy
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient

from teamwork_review_agents import quick_setup
from teamwork_review_agents.codex_model_runner import _instructions
from teamwork_review_agents.config import AgentConfig, RepositoryConfig, effective_skill_ids
from teamwork_review_agents.config_manager import ConfigManager, ConfigRevisionConflict
from teamwork_review_agents.environment import resolve_provider_token
from teamwork_review_agents.git_auth import current_git_environment
from teamwork_review_agents.quick_setup import SetupRequest, apply_rule_selection, check_setup_connection
from teamwork_review_agents.webapp import create_app


@pytest.fixture
def setup_manager(tmp_path):
    """保留一个暂时停用仓库，确保范围转换不会丢掉它的规则关系。"""

    document = {
        "database": {"path": "./state.db"},
        "providers": {"github": {"kind": "github", "base_url": "https://api.github.com", "token_env": "GITHUB_TOKEN"}},
        "environment": {"global": {"GITHUB_TOKEN": {"value": "old-secret", "secret": True}}},
        "repositories": [
            {"id": name, "provider": "github", "project": f"owner/{name}", "workspace": f"./{name}", "enabled": name != "old-b"}
            for name in ("old-a", "old-b")
        ],
        "agents": {"review": {"prompt": "审核", "allowed_sub_agents": ["child"]}, "child": {"prompt": "辅助"}},
        "rules": [
            {"name": "all", "events": ["change_request.opened"], "agents": ["review"], "deduplicate_per_scan": True},
            {"name": "some", "events": ["change_request.commits_changed"], "agents": ["review"], "repositories": ["old-a"]},
            {"name": "off", "events": ["change_request.merged"], "agents": ["review"], "enabled": False},
        ],
        "scheduled_rules": [{"name": "all", "agents": ["review"], "schedule": {"kind": "cron", "cron": "0 * * * *"}}],
    }
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(document, allow_unicode=True), encoding="utf-8")
    return ConfigManager(path)


def draft(manager, **kwargs):
    """按当前配置版本生成新仓库请求，避免使用真实凭据。"""

    return SetupRequest(**{
        "revision": manager.config.revision, "kind": "github", "base_url": "https://api.github.com",
        "remote": "https://github.com/owner/new.git", "token": "setup-test-secret", **kwargs,
    })


@pytest.mark.parametrize("scope, enabled, selected, expected_scope, expected_enabled", [
    (None, True, True, None, True),
    ([], True, True, [], True),
    ([], True, False, ["old-a", "old-b"], True),
    (["old-a"], True, True, ["old-a", "new"], True),
    (["old-a"], True, False, ["old-a"], True),
    (["new"], True, False, [], False),
    ([], False, True, ["new"], True),
    (["old-a"], False, True, ["new"], True),
    (["old-a"], False, False, ["old-a"], False),
])
def test_rule_scope_matrix(scope, enabled, selected, expected_scope, expected_enabled):
    """全量、白名单、关闭三种原状态均只改变目标仓库的适用性。"""

    rule = {"name": "dynamic", "repositories": scope, "enabled": enabled, "agents": ["worker"]}
    original = copy.deepcopy(rule)
    result = apply_rule_selection(rule, "new", ["old-a", "old-b"], selected)
    assert result["repositories"] == expected_scope
    assert result["enabled"] is expected_enabled
    assert rule == original


def test_first_repository_unselected_rule_is_disabled():
    """零仓库不应被写成空白名单并错误启用全局规则。"""

    assert apply_rule_selection({"enabled": True}, "first", [], False) == {"enabled": False, "repositories": []}


def test_preview_then_atomic_save_preserves_other_configuration(setup_manager):
    """预览不落盘；完成时同步保存仓库、Token 和两类动态规则。"""

    manager = setup_manager
    original = manager.path.read_bytes()
    request = draft(manager, event_rules=["all", "off"])
    preview, repository_id = manager.prepare_setup(request)
    assert manager.path.read_bytes() == original
    assert not preview.repository_map()[repository_id].workspace.exists()
    config, saved_id = manager.prepare_setup(request, persist=True)
    assert saved_id == repository_id
    assert config.rules[0].repositories is None
    assert config.rules[1].repositories == ["old-a"]
    assert config.rules[2].repositories == [repository_id]
    assert config.scheduled_rules[0].repositories == ["old-a", "old-b"]
    assert config.rules[0].deduplicate_per_scan
    repository = config.repository_map()[repository_id]
    credential = repository.environment["GITHUB_TOKEN"]
    assert credential.secret and credential.expose_to_process and not credential.expose_to_prompt
    assert resolve_provider_token(config, config.providers[repository.provider], repository) == "setup-test-secret"
    assert resolve_provider_token(config, config.providers["github"], config.repositories[0]) == "old-secret"
    assert repository.allowed_skills is None
    assert config.runtime.default_model.provider == "codex-cli"
    assert "setup-test-secret" not in str(manager.document())
    assert len(config.providers) == 1
    with pytest.raises(ConfigRevisionConflict):
        manager.prepare_setup(request, persist=True)


def test_reject_duplicate_and_allow_explicit_update(setup_manager):
    """同平台项目必须明确更新，保留旧仓库身份、目录与停用状态。"""

    manager = setup_manager
    with pytest.raises(ValueError, match="已配置"):
        manager.prepare_setup(draft(manager, remote="https://github.com/owner/old-b.git"))
    config, repository_id = manager.prepare_setup(draft(
        manager, remote="git@github.com:owner/old-b.git", existing_repository_id="old-b",
        token_source="existing", display_name="新名称", scheduled_rules=["all"],
    ), persist=True)
    assert repository_id == "old-b" and len(config.repositories) == 2
    repository = config.repository_map()[repository_id]
    assert not repository.enabled and repository.workspace == manager.path.parent / "old-b"
    assert config.rules[0].repositories == ["old-a"]
    assert config.scheduled_rules[0].repositories == []


@pytest.mark.parametrize("overrides", [
    {"event_rules": ["不存在"]},
    {"token": "********"},
    {"base_url": "http://api.github.com"},
    {"remote": "https://user:secret@github.com/owner/repo.git"},
    {"remote": "http://github.com/owner/repo.git"},
    {"remote": "https://evil.example/owner/repo.git"},
    {"remote": "https://github.com/owner/repo.git?token=secret"},
    {"remote": "--upload-pack=unsafe"},
    {"token_source": "existing"},
    {"token_source": "system", "token_system_variable": "INVALID-NAME"},
    {"use_skills": True, "event_rules": ["all"], "agent_skills": {"review": ["missing"]}},
])
def test_invalid_draft_cannot_partially_save(setup_manager, overrides):
    """所有配置校验必须在任何落盘动作之前完成。"""

    previous = setup_manager.path.read_bytes()
    with pytest.raises(ValueError):
        setup_manager.prepare_setup(draft(setup_manager, **overrides), persist=True)
    assert setup_manager.path.read_bytes() == previous


def test_gitlab_system_token_and_scoped_skills(setup_manager, tmp_path, monkeypatch):
    """自建 GitLab、子组项目、宿主机 Token 与 sub-agent 独立 Skill 分配。"""

    skill = tmp_path / "skill"
    skill.mkdir()
    (skill / "SKILL.md").write_text("---\nname: test-skill\ndescription: 测试技能\n---\n技能正文", encoding="utf-8")
    monkeypatch.setenv("SETUP_GITLAB_TOKEN", "gitlab-test-secret")
    request = draft(setup_manager, kind="gitlab", base_url="https://git.example.com/api/v4",
                    remote="ssh://git@git.example.com/group/sub/project.git", token_source="system",
                    token_system_variable="SETUP_GITLAB_TOKEN", event_rules=["all"], use_skills=True,
                    new_skills={"test-skill": str(skill), "unused": "/missing/unused-skill"}, agent_skills={"child": ["test-skill"]})
    config, repository_id = setup_manager.prepare_setup(request, persist=True)
    repository = config.repository_map()[repository_id]
    assert "unused" not in config.skills
    assert repository.project == "group/sub/project"
    assert resolve_provider_token(config, config.providers[repository.provider], repository) == "gitlab-test-secret"
    assert effective_skill_ids(config.agents["child"], repository, "child") == ["test-skill"]
    assert effective_skill_ids(config.agents["review"], repository, "review") == []
    assert config.agents["child"].skills == []
    assert effective_skill_ids(config.agents["child"], config.repositories[0], "child") == []
    instructions = _instructions(repository=repository, agent=config.agents["child"], agent_name="child", personality=None, skill_files={"test-skill": skill / "SKILL.md"})
    assert "技能正文" in instructions
    updated = setup_manager.save_agent(expected_revision=config.revision, name="renamed", original_name="child", agent={"prompt": "辅助"})
    assert "renamed" in updated.repository_map()[repository_id].agent_skills
    deleted = setup_manager.delete_agent(expected_revision=updated.revision, name="renamed")
    assert "renamed" not in deleted.repository_map()[repository_id].agent_skills


def test_repository_policy_still_bounds_overrides():
    """仓库覆盖列表不是绕过仓库 Skill 禁用或白名单的后门。"""

    agent = AgentConfig(prompt="测试", skills=["global"])
    repository = RepositoryConfig(id="repo", provider="github", project="a/b", workspace=".", agent_skills={"worker": ["local", "global"]}, allowed_skills=["local"])
    assert effective_skill_ids(agent, repository, "worker") == ["local"]
    repository.allowed_skills = None
    assert effective_skill_ids(agent, repository, "worker") == []


def test_setup_api_masks_success_error_and_validation(setup_manager):
    """成功、业务失败及请求体格式错误均不能返回 Token 明文。"""

    client = TestClient(create_app(setup_manager.path, start_scheduler=False))
    payload = draft(setup_manager, event_rules=["all"]).model_dump(mode="json")
    payload["token"] = "visible-only-in-request"
    original = setup_manager.path.read_bytes()
    preview = client.post("/api/setup/preview", json=payload)
    assert preview.status_code == 200, preview.text
    assert setup_manager.path.read_bytes() == original
    invalid = client.post("/api/setup/preview", json={**payload, "kind": "unsupported"})
    missing = client.post("/api/setup/preview", json={"token": payload["token"]})
    invalid_skill = client.post("/api/setup/preview", json={
        **payload, "use_skills": True, "agent_skills": {"review": [payload["token"]]},
    })
    assert invalid.status_code == missing.status_code == 422
    assert invalid_skill.status_code == 422
    saved = client.post("/api/setup/complete", json=payload)
    assert saved.status_code == 200, saved.text
    conflict = client.post("/api/setup/complete", json=payload)
    assert conflict.status_code == 409
    for response in (preview, invalid, missing, invalid_skill, saved, conflict):
        assert payload["token"] not in response.text
    with setup_manager.store.connect() as connection:
        rows = connection.execute("SELECT * FROM config_versions").fetchall()
        assert payload["token"] not in str([tuple(row) for row in rows])


def test_setup_routes_require_admin_auth(setup_manager, monkeypatch):
    """快速入口仍受现有管理鉴权保护，包括只读预览和连接检查。"""

    document = setup_manager.document(mask_secrets=False)
    document["web"] = {"admin_token_env": "SETUP_TEST_ADMIN"}
    monkeypatch.setenv("SETUP_TEST_ADMIN", "admin-test-secret")
    setup_manager.save(document)
    client = TestClient(create_app(setup_manager.path, start_scheduler=False))
    for endpoint in ("preview", "check", "complete"):
        response = client.post(f"/api/setup/{endpoint}", json={})
        assert response.status_code == 401


@pytest.mark.asyncio
async def test_connection_probe_reads_only_and_cleans_credentials(setup_manager, monkeypatch):
    """API 使用仓库 Token，Git Token 不在参数中，检测完销毁临时凭据。"""

    config, repository_id = setup_manager.prepare_setup(draft(setup_manager))
    response = httpx.Response(200, json={"id": 7})
    client = AsyncMock()
    client.get.return_value = response
    client.__aenter__.return_value = client
    monkeypatch.setattr(quick_setup.httpx, "AsyncClient", Mock(return_value=client))
    def git(arguments, **kwargs):
        """检查 Git 使用独立环境，禁止执行 clone/fetch/push。"""
        assert arguments[0] == "ls-remote"
        assert "setup-test-secret" not in str(arguments)
        assert current_git_environment()["TEAMWORK_GIT_TOKEN"] == "setup-test-secret"
        assert kwargs["timeout_seconds"] == 15
    monkeypatch.setattr(quick_setup, "_run_git", git)
    result = await check_setup_connection(config, repository_id)
    assert all(item["ok"] for item in result)
    assert current_git_environment() is None
    assert client.get.call_args.kwargs["headers"] == {"Authorization": "Bearer setup-test-secret"}


@pytest.mark.asyncio
async def test_probe_errors_never_echo_upstream_secret(setup_manager, monkeypatch):
    """网络或 Git 错误可能包含敏感数据，只返回固定排障提示。"""

    config, repository_id = setup_manager.prepare_setup(draft(setup_manager))
    client = AsyncMock()
    client.__aenter__.return_value = client
    client.get.side_effect = RuntimeError("setup-test-secret")
    monkeypatch.setattr(quick_setup.httpx, "AsyncClient", Mock(return_value=client))
    monkeypatch.setattr(quick_setup, "_run_git", Mock(side_effect=RuntimeError("setup-test-secret")))
    result = await check_setup_connection(config, repository_id)
    assert all(not item["ok"] for item in result)
    assert "setup-test-secret" not in str(result)
    assert current_git_environment() is None
