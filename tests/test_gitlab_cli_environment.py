"""GitLab CLI 默认主机、环境隔离与可选真实路由回归。"""

from __future__ import annotations

import http.server
import os
import shutil
import subprocess
import threading

import pytest

from teamwork_review_agents.agent_home import TemporaryAgentHome
from teamwork_review_agents.codex_model_runner import CodexModelRunner
from teamwork_review_agents.codex_runner import CodexRunner
from teamwork_review_agents.config import EnvironmentVariable
from teamwork_review_agents.environment import (
    MASK,
    resolve_environment,
    resolve_repository_process_environment,
)
from teamwork_review_agents.managed_sandbox import wrap_managed_sandbox_command


@pytest.fixture
def gitlab_config(configured_app_factory):
    """复用公共配置，把目标仓库绑定到自建 GitLab。"""

    config = configured_app_factory()
    config.repositories[0].provider = "gitlab-main"
    return config


def resolve_both(config):
    """同时检查 Agent 与不隶属 Agent 的仓库进程路径。"""

    repository = config.repositories[0]
    return (
        resolve_environment(config, repository, config.agents["code-reviewer"], None, "review"),
        resolve_repository_process_environment(config, repository, "prepare"),
    )


@pytest.mark.parametrize(("base_url", "expected"), [
    ("https://gitlab.example.com/api/v4", "gitlab.example.com"),
    ("https://gitlab.example.com:8443/api/v4/", "gitlab.example.com:8443"),
    ("http://gitlab.example.com:8080/gitlab/api/v4", "gitlab.example.com:8080"),
    ("https://[2001:db8::1]/api/v4", "[2001:db8::1]"),
    ("https://[2001:db8::1]:8443/api/v4", "[2001:db8::1]:8443"),
    ("https://user:private-value@gitlab.example.com/api/v4?token=private-value#fragment", "gitlab.example.com"),
])
def test_gitlab_host_comes_from_provider_without_url_secrets(gitlab_config, base_url, expected):
    """只提取主机与端口，不把 URL 凭据或 API 路径传播给 CLI。"""

    gitlab_config.providers["gitlab-main"].base_url = base_url
    before = gitlab_config.model_dump()
    for resolved in resolve_both(gitlab_config):
        assert resolved.process_values["GITLAB_HOST"] == expected
        assert resolved.all_values["GITLAB_HOST"] == expected
        assert resolved.audit_values["GITLAB_HOST"] == expected
        assert resolved.prompt_values["GITLAB_HOST"] == ""
        assert "GITLAB_TOKEN" not in resolved.process_values
        assert "private-value" not in str(resolved)
    assert gitlab_config.model_dump() == before


@pytest.mark.parametrize("base_url", [
    "gitlab.example.com/api/v4", "https:///api/v4", "file:///tmp/gitlab",
    "https://gitlab.example.com:invalid/api/v4", "https://[invalid]/api/v4",
])
def test_invalid_gitlab_url_fails_without_echoing_value(gitlab_config, base_url):
    """无法确定主机时明确失败，不静默回退公网或在报错中回显 URL。"""

    gitlab_config.providers["gitlab-main"].base_url = base_url
    with pytest.raises(ValueError, match="无法生成 GITLAB_HOST") as error:
        resolve_both(gitlab_config)
    assert base_url not in str(error.value)


@pytest.mark.parametrize("level", ["global", "agent", "repository"])
def test_explicit_host_keeps_environment_precedence(gitlab_config, level):
    """自动值位于现有三层配置之下，不覆盖用户明确选择。"""

    repository = gitlab_config.repositories[0]
    agent = gitlab_config.agents["code-reviewer"]
    gitlab_config.environment.global_variables["GITLAB_HOST"] = EnvironmentVariable(value="global.example.com")
    if level in {"agent", "repository"}:
        agent.environment["GITLAB_HOST"] = EnvironmentVariable(value="agent.example.com")
    if level == "repository":
        repository.environment["GITLAB_HOST"] = EnvironmentVariable(value="repository.example.com")
    agent_result, preparation = resolve_both(gitlab_config)
    assert agent_result.process_values["GITLAB_HOST"] == f"{level}.example.com"
    assert preparation.process_values["GITLAB_HOST"] == (
        "repository.example.com" if level == "repository" else "global.example.com"
    )


@pytest.mark.parametrize("definition", [
    EnvironmentVariable(value=""),
    EnvironmentVariable(value="explicit.example.com", expose_to_process=False),
    EnvironmentVariable(from_system="TEST_GITLAB_HOST", secret=True, expose_to_prompt=False),
])
def test_explicit_host_preserves_whole_definition(gitlab_config, monkeypatch, definition):
    """空值、关闭进程暴露及宿主引用均保持整项覆盖，不被默认值恢复。"""

    monkeypatch.setenv("TEST_GITLAB_HOST", "referenced.example.com")
    gitlab_config.repositories[0].environment["GITLAB_HOST"] = definition
    expected = "referenced.example.com" if definition.from_system else definition.value
    for resolved in resolve_both(gitlab_config):
        assert resolved.all_values["GITLAB_HOST"] == expected
        if definition.expose_to_process:
            assert resolved.process_values["GITLAB_HOST"] == expected
        else:
            assert "GITLAB_HOST" not in resolved.process_values
        assert resolved.audit_values["GITLAB_HOST"] == (MASK if definition.secret else expected)


@pytest.mark.parametrize("expose", [True, False])
def test_host_default_does_not_change_token_exposure(gitlab_config, expose):
    """默认主机不能扩大 Token 权限，秘密仍不进入 Prompt 并始终脱敏。"""

    gitlab_config.repositories[0].environment["GITLAB_TOKEN"] = EnvironmentVariable(
        value="dummy-gitlab-secret", secret=True, expose_to_process=expose,
    )
    for resolved in resolve_both(gitlab_config):
        assert resolved.process_values["GITLAB_HOST"] == "gitlab.example.com"
        assert ("GITLAB_TOKEN" in resolved.process_values) is expose
        assert resolved.prompt_values["GITLAB_TOKEN"] == ""
        assert resolved.audit_values["GITLAB_TOKEN"] == MASK
        assert "dummy-gitlab-secret" in resolved.secret_values


def test_provider_hosts_are_isolated_and_github_is_unchanged(gitlab_config):
    """复用同一 Agent 时按当前仓库解析，不污染配置、宿主环境或 GitHub。"""

    config = gitlab_config
    repository = config.repositories[0]
    config.providers["gitlab-other"] = config.providers["gitlab-main"].model_copy(
        update={"base_url": "https://other.example.com/api/v4"},
    )
    before = config.model_dump()
    host_before = dict(os.environ)
    for provider, expected in (
        ("gitlab-main", "gitlab.example.com"), ("gitlab-other", "other.example.com"),
        ("github-main", None), ("gitlab-main", "gitlab.example.com"),
    ):
        scoped_repository = repository.model_copy(update={"provider": provider})
        for name in ("code-reviewer", "security-reviewer"):
            resolved = resolve_environment(
                config, scoped_repository, config.agents[name], None, "run", include_change_request=False,
            )
            assert resolved.process_values.get("GITLAB_HOST") == expected
    assert config.model_dump() == before
    assert dict(os.environ) == host_before


def test_custom_token_name_is_not_aliased_to_glab(gitlab_config):
    """补主机不等于授权复制凭据，自定义 Token 名称保持原样。"""

    gitlab_config.providers["gitlab-main"].token_env = "CORPORATE_TOKEN"
    gitlab_config.repositories[0].environment["CORPORATE_TOKEN"] = EnvironmentVariable(
        value="dummy-custom-token", secret=True,
    )
    for resolved in resolve_both(gitlab_config):
        assert resolved.process_values["GITLAB_HOST"] == "gitlab.example.com"
        assert resolved.process_values["CORPORATE_TOKEN"] == "dummy-custom-token"
        assert "GITLAB_TOKEN" not in resolved.process_values
        assert resolved.audit_values["CORPORATE_TOKEN"] == MASK


def test_unbound_repository_prompt_preview_keeps_working(gitlab_config):
    """未绑定平台的 Prompt 预览仍可解析其他变量，不为占位仓库生成主机。"""

    gitlab_config.repositories[0].provider = "preview-only"
    for resolved in resolve_both(gitlab_config):
        assert "GITLAB_HOST" not in resolved.process_values
        assert resolved.all_values["REPOSITORY_ID"] == "demo"


@pytest.mark.parametrize("runner_kind", ["cli", "model"])
@pytest.mark.parametrize("windows_bridge", [False, True])
def test_host_survives_temporary_home_and_sandbox_launch(
    gitlab_config, tmp_path, monkeypatch, verified_test_sandbox_python, runner_kind, windows_bridge,
):
    """两个运行器及 Windows 环境分离桥都保留默认主机；不启动真实沙盒。"""

    repository = gitlab_config.repositories[0]
    resolved = resolve_both(gitlab_config)[0]
    temporary = TemporaryAgentHome.create("glab-host", root=tmp_path / "homes")
    try:
        if runner_kind == "model":
            environment = CodexModelRunner(gitlab_config).child_environment(
                resolved.process_values, temporary_home=temporary, tool_codex_home=temporary.path / "codex",
            )
        else:
            environment = CodexRunner(gitlab_config).child_environment(
                resolved.process_values, temporary_home=temporary,
            )
        monkeypatch.setattr(
            "teamwork_review_agents.managed_sandbox.windows_environment_separation", lambda: windows_bridge,
        )
        launch = wrap_managed_sandbox_command(
            codex_binary="codex", workspace=repository.workspace,
            agent=gitlab_config.agents["code-reviewer"], inner_command=["glab", "api", "user"],
            environment=environment, codex_runtime_directory=temporary.path,
            codex_home=gitlab_config.runtime.codex_home,
        )
        assert environment["HOME"] == str(temporary.path)
        assert launch.environment["GITLAB_HOST"] == "gitlab.example.com"
        assert "GITLAB_TOKEN" not in launch.environment
    finally:
        assert temporary.cleanup() is None


@pytest.mark.skipif(
    os.environ.get("TEAMWORK_TEST_GLAB_ROUTING") != "1",
    reason="真实 glab 路由验收需显式启用；只使用无效 Token 和本机拒绝转发代理",
)
@pytest.mark.parametrize("authority", ["gitlab.example.com", "gitlab.example.com:8443"])
def test_real_glab_routes_to_provider_with_empty_login_home(gitlab_config, tmp_path, authority):
    """同一无登录配置的 glab，补齐主机后不得再访问 gitlab.com。"""

    glab = shutil.which("glab")
    git = shutil.which("git")
    if not glab or not git:
        pytest.skip("真实路由验收需要本机安装 git 与 glab")
    repository = gitlab_config.repositories[0]
    gitlab_config.providers["gitlab-main"].base_url = f"https://{authority}/api/v4"
    subprocess.run([git, "init", "-q", str(repository.workspace)], check=True, capture_output=True)
    subprocess.run(
        [git, "-C", str(repository.workspace), "remote", "add", "origin", f"https://{authority}/owner/demo.git"],
        check=True, capture_output=True,
    )
    observed = []

    class RejectingProxy(http.server.BaseHTTPRequestHandler):
        """只记录 CONNECT 主机，拒绝建立隧道，不转发任何网络请求。"""

        def do_CONNECT(self):
            observed.append(self.path)
            self.send_error(502)

        def log_message(self, *_args):
            """禁止输出请求日志。"""

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), RejectingProxy)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        home = str(tmp_path / "empty-home")
        proxy = f"http://127.0.0.1:{server.server_port}"
        environment = {key: os.environ[key] for key in ("PATH", "SYSTEMROOT", "WINDIR") if key in os.environ}
        environment.update({
            "HOME": home, "USERPROFILE": home, "XDG_CONFIG_HOME": home, "GLAB_CONFIG_DIR": home,
            "GITLAB_TOKEN": "teamwork-diagnostic-invalid-token", "GLAB_NO_PROMPT": "1",
            "HTTP_PROXY": proxy, "HTTPS_PROXY": proxy, "NO_PROXY": "",
            "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1", "GLAB_CHECK_UPDATE": "false",
        })
        resolved = resolve_both(gitlab_config)[0]
        target = authority if ":" in authority else f"{authority}:443"
        for extra, expected in (({}, "gitlab.com:443"), (resolved.process_values, target)):
            observed.clear()
            result = subprocess.run(
                [glab, "api", "user"], cwd=repository.workspace, env={**environment, **extra},
                capture_output=True, text=True, timeout=15,
            )
            assert result.returncode != 0
            assert observed == [expected]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
