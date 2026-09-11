"""Windows 沙盒 Git HTTPS 的兼容配置、凭据边界与阻断回归。"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from contextlib import contextmanager, nullcontext
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from teamwork_review_agents.agent_workspace import AgentWorkspacePreparationResult, prepare_agent_workspace
from teamwork_review_agents.codex_model_runner import CodexModelRunner
from teamwork_review_agents.codex_runner import CodexRunner
from teamwork_review_agents.config import AgentWorkspaceConfig, AgentWorkspacePrepareStepConfig
from teamwork_review_agents.environment import SecretRedactor
from teamwork_review_agents.events import detect_events
from teamwork_review_agents.executor import AgentExecutionError, AgentExecutor
from teamwork_review_agents.git_auth import GitCredentialContext
from teamwork_review_agents.managed_sandbox import ManagedSandboxInspection, permission_profile_override
from teamwork_review_agents.mcp_bridge import McpBridgeChannel
from teamwork_review_agents.model_tools import ModelToolExecutor
from teamwork_review_agents.models import AgentResult, InvocationContext
from teamwork_review_agents.preflight import StepExecutionOutcome
from teamwork_review_agents.sandbox_git import (
    SandboxGitContext, SandboxGitError, append_git_config, classify_git_failure,
    current_sandbox_git, windows_sandbox_git_enabled,
)
from teamwork_review_agents.state import StateStore


@contextmanager
def active_git(environment=None, *, workspace=None):
    """在当前任务中成对建立和释放上下文，避免测试间残留。"""

    with nullcontext(workspace) if workspace is not None else TemporaryDirectory(prefix="test-git-workspace-") as directory:
        if workspace is None:
            workspace = Path(directory)
            (workspace / ".git").mkdir()
        context = SandboxGitContext(environment or {}, verified_workspace=workspace).start()
        try:
            yield context
        finally:
            context.close()


def process_result(stdout="", stderr="", code=0, timed_out=False):
    """构造与真实工具进程相同的结果结构。"""

    return {"stdout": stdout, "stderr": stderr, "exit_code": code,
            "timed_out": timed_out, "truncated": False}


def git_process(workspace, arguments, environment):
    """在测试临时仓库执行原生 Git，既不改开发仓库也不访问外部服务。"""

    return subprocess.run(
        ["git", "-C", str(workspace), *arguments], env=environment,
        text=True, encoding="utf-8", errors="replace", capture_output=True, timeout=20,
    )


@pytest.fixture
def git_repositories(tmp_path):
    """隔离系统/用户 Git 配置，准备 clone、linked worktree 和未授权仓库。"""

    global_config = tmp_path / "git-global"
    global_config.write_text("[safe]\n\tdirectory = *\n", encoding="utf-8")
    home = tmp_path / "temporary-home"
    home.mkdir()
    environment = {key: value for key, value in os.environ.items() if not key.upper().startswith("GIT_")}
    environment.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": str(global_config),
                        "HOME": str(home), "USERPROFILE": str(home)})
    source = tmp_path / "source"
    source.mkdir()
    commands = [
        ["init", "--initial-branch=main"],
        ["-c", "user.name=Test", "-c", "user.email=test@example.invalid",
         "-c", "commit.gpgsign=false", "commit", "--allow-empty", "-m", "测试基准"],
        ["remote", "add", "origin", "https://example.invalid/owner/repo.git"],
        ["clone", "--no-hardlinks", str(source), str(tmp_path / "clone with spaces")],
        ["worktree", "add", "--detach", str(tmp_path / "linked worktree"), "HEAD"],
    ]
    for arguments in commands:
        result = git_process(source, arguments, environment)
        assert result.returncode == 0, result.stderr
    clone = tmp_path / "clone with spaces"
    result = git_process(clone, ["remote", "set-url", "origin", "https://example.invalid/owner/repo.git"], environment)
    assert result.returncode == 0, result.stderr
    environment["GIT_TEST_ASSUME_DIFFERENT_OWNER"] = "1"
    return source, clone, tmp_path / "linked worktree", environment


@pytest.mark.parametrize("kind", ["clone", "linked"])
def test_exact_trust_allows_only_current_foreign_owned_workspace(git_repositories, kind):
    """用 Git 自身的异主模式验证精确信任，并覆盖全局及环境中的通配信任重置。"""

    source, clone, linked, environment = git_repositories
    workspace = clone if kind == "clone" else linked
    local_config = (workspace if kind == "clone" else source) / ".git/config"
    before = local_config.read_bytes()
    global_before = Path(environment["GIT_CONFIG_GLOBAL"]).read_bytes()
    rejected_environment = dict(environment)
    append_git_config(rejected_environment, {"safe.directory": ""})
    rejected = git_process(workspace, ["remote", "get-url", "origin"], rejected_environment)
    assert rejected.returncode != 0
    assert "detected dubious ownership" in rejected.stderr

    # 模拟继承旧运行的环境配置，当前运行必须重置列表而不是只追加新路径。
    append_git_config(environment, [("safe.directory", "*"), ("credential.helper", ""),
                                    ("core.excludesFile", str(source / "ignore"))])
    original = dict(environment)
    with active_git(environment, workspace=workspace) as context:
        origin = git_process(workspace, ["remote", "get-url", "origin"], context.environment)
        assert origin.returncode == 0, origin.stderr
        assert origin.stdout.strip() == "https://example.invalid/owner/repo.git"
        head = git_process(workspace, ["rev-parse", "HEAD"], context.environment)
        assert head.returncode == 0, head.stderr
        assert len(head.stdout.strip()) == 40
        status = git_process(workspace, ["status", "--porcelain"], context.environment)
        assert status.returncode == 0, status.stderr
        outside = git_process(source, ["rev-parse", "HEAD"], context.environment)
        assert outside.returncode != 0
        assert "detected dubious ownership" in outside.stderr
        assert context.environment["GIT_CONFIG_KEY_1"] == "credential.helper"
        assert context.environment["GIT_CONFIG_KEY_2"] == "core.excludesFile"
    assert environment == original
    assert local_config.read_bytes() == before
    assert Path(environment["GIT_CONFIG_GLOBAL"]).read_bytes() == global_before


def test_child_trust_replaces_parent_and_inherited_workspace_reuses_exact_path(git_repositories):
    """新子工作区不继承父目录信任，复用父工作区则保留同一路径；退出不污染父环境。"""

    _, parent_workspace, child_workspace, environment = git_repositories
    with active_git(environment, workspace=parent_workspace) as parent:
        before = dict(parent.environment)
        with active_git(parent.environment, workspace=child_workspace) as child:
            assert git_process(child_workspace, ["rev-parse", "HEAD"], child.environment).returncode == 0
            assert git_process(parent_workspace, ["rev-parse", "HEAD"], child.environment).returncode != 0
        with active_git(parent.environment, workspace=parent_workspace) as inherited:
            assert inherited.verified_workspace == parent.verified_workspace
            assert git_process(parent_workspace, ["rev-parse", "HEAD"], inherited.environment).returncode == 0
        assert parent.environment == before
        assert current_sandbox_git() is parent


@pytest.mark.parametrize("runner_class", [CodexRunner, CodexModelRunner])
def test_runner_environment_preserves_trust_after_excludes_injection(
    git_repositories, configured_app_factory, runner_class,
):
    """两种 Runner 合成最终环境并追加 Skill excludes 后，仍只信任本轮仓库。"""

    from teamwork_review_agents.codex_runner import _add_git_excludes_file as cli_excludes
    from teamwork_review_agents.codex_model_runner import _add_git_excludes_file as model_excludes

    source, workspace, _, environment = git_repositories
    runner = runner_class(configured_app_factory())
    with active_git(environment, workspace=workspace) as context:
        if runner_class is CodexRunner:
            child_environment = runner.child_environment(context.environment)
            cli_excludes(child_environment, source / "test-excludes")
        else:
            child_environment = runner.child_environment(
                context.environment, temporary_home=None, tool_codex_home=source / "test-codex-home",
            )
            model_excludes(child_environment, source / "test-excludes")
        result = git_process(workspace, ["rev-parse", "HEAD"], child_environment)
        assert result.returncode == 0, result.stderr
        rejected = git_process(source, ["rev-parse", "HEAD"], child_environment)
        assert rejected.returncode != 0
        assert "detected dubious ownership" in rejected.stderr
        assert git_process(workspace, ["config", "--get", "http.sslBackend"], child_environment).stdout.strip() == "openssl"
        assert git_process(workspace, ["config", "--get", "http.sslVerify"], child_environment).stdout.strip() == "true"


@pytest.mark.parametrize("invalid", ["missing", "not-git", "file", "root"])
def test_workspace_trust_requires_existing_precise_git_directory(tmp_path, invalid):
    """缺失或宽泛目录不能被静默加入信任列表，也不创建凭据 helper。"""

    workspace = tmp_path / invalid
    if invalid == "not-git":
        workspace.mkdir()
    elif invalid == "file":
        workspace.write_text("不是目录", encoding="utf-8")
    elif invalid == "root":
        workspace = Path(tmp_path.anchor)
    context = SandboxGitContext({"TEAMWORK_GIT_TOKEN": "test"}, verified_workspace=workspace)
    with pytest.raises(SandboxGitError) as failure:
        context.start()
    assert failure.value.error_code == "sandbox_git_workspace_invalid"
    assert failure.value.retryable is False
    assert context.directory is None
    assert current_sandbox_git() is None


async def test_prepare_steps_inherit_precise_trust(git_repositories, configured_app_factory, monkeypatch):
    """准备步骤的临时 HOME 不能丢失运行级信任；测试只模拟沙盒包装，真实运行 Git。"""

    _, workspace, _, environment = git_repositories
    config = configured_app_factory()
    repository = config.repositories[0]
    repository.workspace = workspace
    repository.agent_workspace = AgentWorkspaceConfig(prepare_steps=[
        AgentWorkspacePrepareStepConfig(name="读取远端", command=["git", "remote", "get-url", "origin"]),
        AgentWorkspacePrepareStepConfig(name="读取提交", command=["git", "rev-parse", "HEAD"]),
    ])
    monkeypatch.setattr("teamwork_review_agents.agent_workspace.inspect_managed_sandbox", lambda *args:
                        ManagedSandboxInspection(available=True, platform="win32", backend="windows"))
    monkeypatch.setattr("teamwork_review_agents.agent_workspace.resolve_codex_executable", lambda *args: "codex")
    commands = []

    def wrapper(**kwargs):
        commands.append(kwargs)
        return kwargs["inner_command"]

    monkeypatch.setattr("teamwork_review_agents.agent_workspace.wrap_managed_sandbox_command", wrapper)
    with active_git(environment, workspace=workspace) as context:
        result = await prepare_agent_workspace(
            config=config, repository=repository, agent=config.agents["code-reviewer"],
            process_environment=context.environment, redactor=SecretRedactor(()),
            log_callback=AsyncMock(), cancel_check=lambda: False,
        )
    assert result.outcome.status == "success", result.outcome.error
    assert len(commands) == 2
    for command in commands:
        assert command["environment"]["HOME"] != environment["HOME"]
        count = int(command["environment"]["GIT_CONFIG_COUNT"])
        assert command["environment"][f"GIT_CONFIG_KEY_{count - 1}"] == "safe.directory"
        assert command["environment"][f"GIT_CONFIG_VALUE_{count - 1}"] == workspace.resolve().as_posix()


async def test_ownership_failure_blocks_before_https_probe(tool, monkeypatch):
    """读取本地 origin 被拒绝时只执行一次，不再尝试 HTTPS，也不标为可重试网络错误。"""

    run = AsyncMock(return_value=process_result(stderr="fatal: detected dubious ownership in repository at 'D:/run'", code=128))
    monkeypatch.setattr(tool, "_run_process", run)
    with active_git(workspace=tool.repository.workspace), pytest.raises(SandboxGitError) as error:
        await tool.check_git_https()
    assert error.value.error_code == "sandbox_git_ownership_mismatch"
    assert error.value.retryable is False
    assert "所有权" in str(error.value)
    assert run.await_count == 1


@pytest.mark.parametrize("inherit,preparation_fails", [(False, False), (True, False), (True, True)])
async def test_executor_injects_validated_workspace_before_preparation(
    git_repositories, configured_app_factory, snapshot_factory, monkeypatch, inherit, preparation_fails,
):
    """实际执行器向准备步骤和 Runner 传递校验结果，准备失败时也保存不可重试状态。"""

    source, workspace, _, _ = git_repositories
    config = configured_app_factory()
    config.runtime.codex.execution_mode = "cli"
    config.runtime.codex.model = "gpt-test"
    config.repositories[0].workspace = source
    config.agents["code-reviewer"].sandbox = "workspace-write"
    config.agents["code-reviewer"].write_scopes = ["workspace"]
    store = StateStore(config.database.path)
    store.initialize()
    executor = AgentExecutor(config, store)
    monkeypatch.setattr("teamwork_review_agents.executor.windows_sandbox_git_enabled", lambda **kwargs: True)
    monkeypatch.setattr("teamwork_review_agents.executor.resolve_provider_token", lambda *args: "")
    monkeypatch.setattr("teamwork_review_agents.executor.resolve_model_snapshot", lambda *args: {"model": "gpt-test"})
    monkeypatch.setattr("teamwork_review_agents.executor.prepare_change_request_workspace", lambda *args, **kwargs: "HEAD")
    # 创建分支使用已准备的真实 clone；继承分支仍执行真实 validate_run_workspace。
    monkeypatch.setattr("teamwork_review_agents.executor.ensure_isolated_clone", lambda *args, **kwargs: workspace)

    def assert_trust(environment):
        count = int(environment["GIT_CONFIG_COUNT"])
        assert environment[f"GIT_CONFIG_VALUE_{count - 2}"] == ""
        assert environment[f"GIT_CONFIG_KEY_{count - 1}"] == "safe.directory"
        assert environment[f"GIT_CONFIG_VALUE_{count - 1}"] == workspace.resolve().as_posix()
        assert current_sandbox_git().verified_workspace == workspace.resolve()

    async def prepare(**kwargs):
        assert kwargs["repository"].workspace == workspace.resolve()
        assert_trust(kwargs["process_environment"])
        if preparation_fails:
            return AgentWorkspacePreparationResult(
                outcome=StepExecutionOutcome(status="failure", exit_code=128,
                                             output="fatal: detected dubious ownership in repository at 'D:/run'"),
                cache_environment={}, cache_root=None,
            )
        return await prepare_agent_workspace(**kwargs)

    async def run(**kwargs):
        assert_trust(kwargs["process_environment"])
        return AgentResult(run_id=kwargs["run_id"], root_run_id=kwargs["root_run_id"],
                           parent_run_id=kwargs["parent_run_id"], agent_name=kwargs["agent_name"], status="completed")

    preparation = AsyncMock(side_effect=prepare)
    runner = SimpleNamespace(run=AsyncMock(side_effect=run))
    monkeypatch.setattr("teamwork_review_agents.executor.prepare_agent_workspace", preparation)
    monkeypatch.setattr(executor, "_runner_for_provider", lambda *args: runner)
    event = detect_events(None, snapshot_factory(provider="github-main"), emit_initial=True)[0]
    arguments = dict(agent_name="code-reviewer", event=event, idempotency_key="trust-test")
    if inherit:
        arguments.update(task="继承工作区测试", root_run_id="parent", parent_run_id="parent",
                         depth=1, inherit_workspace=True, parent_workspace=workspace)
    if preparation_fails:
        for _ in range(2):
            with pytest.raises(AgentExecutionError) as error:
                await executor.execute(**arguments)
            assert error.value.error_code == "sandbox_git_ownership_mismatch"
            assert error.value.retryable is False
        assert preparation.await_count == 1
        runner.run.assert_not_called()
    else:
        result = await executor.execute(**arguments)
        assert result.status == "completed"
        assert runner.run.await_count == 1
    record = store.list_runs()[0]
    logs = store.list_run_logs(record["run_id"])
    trusted = next(json.loads(log["payload"]) for log in logs if log["event_type"] == "run.git_workspace_trusted")
    assert trusted["trusted_workspace"] == workspace.resolve().as_posix()
    assert current_sandbox_git() is None


@pytest.fixture
def tool(configured_app_factory, snapshot_factory, monkeypatch):
    """构造独立工作区；仅模拟 Codex 可执行路径，不借用开发机安装。"""

    config = configured_app_factory()
    repository = config.repositories[0]
    (repository.workspace / ".git").mkdir()
    agent = config.agents["code-reviewer"]
    agent.sandbox = "workspace-write"
    agent.network_access = True
    agent.network_domains = ["github.com"]
    snapshot = snapshot_factory(provider=repository.provider, repository_id=repository.id)
    context = InvocationContext(
        config_path=str(config.config_path), current_agent="code-reviewer",
        run_id="run-git", root_run_id="run-git",
        active_workspace=str(repository.workspace),
        event=detect_events(None, snapshot, emit_initial=True)[0],
    )
    monkeypatch.setattr("teamwork_review_agents.model_tools.resolve_codex_executable", lambda *args: "codex")
    return ModelToolExecutor(
        config=config, agent=agent, repository=repository, context=context,
        environment={}, managed_sandbox=True, cancel_check=None,
        progress_callback=lambda: None, invoke_agent_callback=None,
    )


def test_git_environment_is_additive_and_scoped(monkeypatch):
    """保留原索引、CA 与代理；不修改输入环境和宿主配置。"""

    original = {"GIT_CONFIG_COUNT": "2", "GIT_CONFIG_KEY_0": "credential.helper",
                "GIT_CONFIG_VALUE_0": "", "GIT_CONFIG_KEY_1": "core.excludesFile",
                "GIT_CONFIG_VALUE_1": "run-ignore", "GIT_SSL_CAINFO": "enterprise.pem",
                "HTTPS_PROXY": "http://proxy", "GIT_SSL_NO_VERIFY": "1"}
    monkeypatch.setenv("TEAMWORK_GIT_TOKEN", "host-only-token")
    with active_git(original) as context:
        environment = context.environment
        assert environment["GIT_CONFIG_COUNT"] == "6"
        assert environment["GIT_CONFIG_KEY_2"] == "http.sslBackend"
        assert environment["GIT_CONFIG_VALUE_2"] == "openssl"
        assert environment["GIT_CONFIG_KEY_3"] == "http.sslVerify"
        assert environment["GIT_CONFIG_VALUE_3"] == "true"
        assert environment["GIT_CONFIG_KEY_4"] == "safe.directory"
        assert environment["GIT_CONFIG_VALUE_4"] == ""
        assert environment["GIT_CONFIG_KEY_5"] == "safe.directory"
        assert environment["GIT_CONFIG_VALUE_5"] == context.verified_workspace.as_posix()
        assert environment["GIT_CONFIG_VALUE_1"] == "run-ignore"
        assert environment["GIT_SSL_CAINFO"] == "enterprise.pem"
        assert environment["HTTPS_PROXY"] == "http://proxy"
        assert "GIT_SSL_NO_VERIFY" not in environment
        assert "TEAMWORK_GIT_TOKEN" not in environment
        assert context.directory is None
        assert context.readable_directories == ()
    assert original["GIT_CONFIG_COUNT"] == "2"
    assert original["GIT_SSL_NO_VERIFY"] == "1"
    assert current_sandbox_git() is None


@pytest.mark.parametrize("environment", [
    {"GIT_CONFIG_COUNT": "bad"}, {"GIT_CONFIG_COUNT": "-1"},
    {"GIT_CONFIG_COUNT": "257"}, {"GIT_CONFIG_COUNT": "1"},
])
def test_invalid_git_config_does_not_silently_overwrite(environment):
    """损坏索引必须明确阻断，不能覆盖未识别的环境配置。"""

    with pytest.raises(SandboxGitError) as error:
        append_git_config(environment, {"http.sslBackend": "openssl"})
    assert error.value.error_code == "sandbox_git_config_invalid"
    assert error.value.retryable is False


@pytest.mark.parametrize("platform,managed,expected", [
    ("win32", True, True), ("win32", False, False), ("darwin", False, False),
])
def test_windows_compatibility_gate(monkeypatch, platform, managed, expected):
    """非托管进程不应启用兼容；模拟平台时不修改 pathlib 的操作系统类别。"""

    monkeypatch.setattr(sys, "platform", platform)
    assert windows_sandbox_git_enabled(managed=managed) is expected


def test_helper_is_separate_read_only_and_contains_no_token(tool):
    """沙盒 helper 不与宿主共用；真实执行无密钥自检。"""

    with GitCredentialContext("test-private-token", provider_kind="github") as host:
        original_helper = host.environment["GIT_ASKPASS"]
        with active_git(host.environment) as context:
            assert context.directory is not None
            directory = context.directory
            contents = (directory / "askpass.py").read_text(encoding="utf-8")
            assert "test-private-token" not in contents
            assert original_helper != context.environment["GIT_ASKPASS"]
            assert host.environment["GIT_ASKPASS"] == original_helper
            assert context.probe_command[1] == "-I"
            # 实际 Python helper 可在三种平台执行，不依赖 Codex 或真实 Token。
            result = subprocess.run(context.probe_command, env={**os.environ, **context.environment},
                                    text=True, capture_output=True, check=True)
            assert result.stdout.strip() == "teamwork-askpass-ready"
            assert "test-private-token" not in result.stdout + result.stderr
            profile = permission_profile_override(tool.agent)
            assert f'{json.dumps(str(directory.resolve()))}="read"' in profile
            assert f'{json.dumps(str(directory.resolve()))}="write"' not in profile
            assert str(directory.parent) + '\"=\"read\"' not in profile
            assert 'mode="limited"' in profile
            assert '"github.com"="allow"' in profile
        assert not directory.exists()
        assert current_sandbox_git() is None
        assert host.environment["GIT_ASKPASS"] == original_helper


def test_git_can_execute_quoted_helper_with_spaces(tmp_path, monkeypatch):
    """直接让原生 Git 解析 askpass 命令，覆盖 Windows 路径空格而不访问网络。"""

    directory = tmp_path / "helper with spaces"
    directory.mkdir()
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr("teamwork_review_agents.sandbox_git.tempfile.mkdtemp", lambda **kwargs: str(directory))
    with active_git({"TEAMWORK_GIT_TOKEN": "dummy-credential"}, workspace=tmp_path) as context:
        result = subprocess.run(
            ["git", "-c", "credential.helper=", "credential", "fill"],
            input="protocol=https\nhost=example.test\n\n", text=True,
            capture_output=True, timeout=10, env={**os.environ, **context.environment},
        )
        assert result.returncode == 0, result.stderr
        assert "username=x-access-token" in result.stdout
        assert "password=dummy-credential" in result.stdout
    assert not directory.exists()


async def test_nested_and_concurrent_contexts_are_isolated():
    """父子上下文可恢复，并发运行不会交换 helper 或 Token。"""

    async def child(token):
        with active_git({"TEAMWORK_GIT_TOKEN": token}) as context:
            await asyncio.sleep(0)
            assert current_sandbox_git() is context
            count = int(context.environment["GIT_CONFIG_COUNT"])
            assert context.environment[f"GIT_CONFIG_VALUE_{count - 1}"] == context.verified_workspace.as_posix()
            return context.directory

    with active_git() as parent:
        directories = await asyncio.gather(child("first"), child("second"))
        assert directories[0] != directories[1]
        assert all(not path.exists() for path in directories)
        assert current_sandbox_git() is parent
    assert current_sandbox_git() is None


@pytest.mark.parametrize("output,code", [
    ("fatal: detected dubious ownership in repository at 'D:/worktrees/run'", "sandbox_git_ownership_mismatch"),
    ("schannel: AcquireCredentialsHandle failed: SEC_E_NO_CREDENTIALS", "sandbox_git_schannel_credentials"),
    ("fatal: Unsupported SSL backend 'openssl'", "sandbox_git_openssl_unavailable"),
    ("SSL certificate problem: unable to get local issuer certificate", "sandbox_git_certificate_invalid"),
    ("error: unable to read askpass response", "sandbox_git_askpass_unavailable"),
    ("fatal: Authentication failed for 'https://token:password@github.com/private?key=secret'", "sandbox_git_auth_failed"),
])
def test_infrastructure_failure_has_specific_code(output, code):
    """提供真实类别并屏蔽 URL 中的凭据，确定性故障不整体重试。"""

    error = classify_git_failure(output)
    assert error.error_code == code
    assert error.retryable is False
    assert "password@" not in str(error)
    assert "key=secret" not in str(error)


@pytest.mark.parametrize("output", [
    "CONFLICT (content): Merge conflict in README.md", "rejected (non-fast-forward)",
    "fatal: couldn't find remote ref missing", "error: pathspec 'missing' did not match",
])
def test_ordinary_git_failures_are_not_infrastructure_errors(output):
    """普通 Git 业务错误保留给 Agent 处理。"""

    assert classify_git_failure(output) is None


async def test_probe_uses_same_sandbox_and_never_passes_token_in_arguments(tool, monkeypatch):
    """三个探测都进入同一沙盒，只读取 HEAD，不改变远端或清除网络白名单。"""

    calls = []
    responses = iter([
        process_result("https://github.com/owner/repo.git\n"),
        process_result("teamwork-askpass-ready\n"),
        process_result("a" * 40 + "\tHEAD\n"),
    ])

    async def run(command, **kwargs):
        calls.append(command)
        assert kwargs["cwd"] == tool.repository.workspace
        assert kwargs["timeout_seconds"] <= 30
        return next(responses)

    monkeypatch.setattr(tool, "_run_process", run)
    with active_git({"TEAMWORK_GIT_TOKEN": "probe-secret"}) as context:
        tool.environment = context.environment
        result = await tool.check_git_https()
        assert result["status"] == "ready"
        assert result["sha"] == "a" * 40
        assert result["host"] == "github.com"
        for command in calls:
            assert command[:2] == ["codex", "sandbox"]
            assert 'mode="limited"' in " ".join(command)
            assert "probe-secret" not in " ".join(command)
            assert f'{json.dumps(str(context.directory.resolve()))}="read"' in " ".join(command)
        assert calls[-1][calls[-1].index("--") + 2:] == ["ls-remote", "--exit-code", "origin", "HEAD"]


@pytest.mark.parametrize("case", ["no_context", "not_managed", "no_network", "no_git", "ssh", "no_origin"])
async def test_probe_skips_without_unnecessary_network(tool, monkeypatch, case):
    """禁网、非 HTTPS 和没有工作区等情况不能发起联网探测。"""

    run = AsyncMock(return_value=(process_result(stderr="error: No such remote 'origin'", code=2)
                                 if case == "no_origin" else process_result("git@github.com:owner/repo.git")))
    monkeypatch.setattr(tool, "_run_process", run)
    if case == "no_context":
        assert (await tool.check_git_https())["status"] == "skipped"
        run.assert_not_called()
        return
    if case == "not_managed":
        tool.managed_sandbox = False
    if case == "no_network":
        tool.agent.network_access = False
    if case == "no_git":
        (tool.repository.workspace / ".git").rmdir()
    with active_git():
        assert (await tool.check_git_https())["status"] == "skipped"
    assert run.await_count == (1 if case in {"ssh", "no_origin"} else 0)


@pytest.mark.parametrize("error_output,expected", [
    ("fatal: Unsupported SSL backend 'openssl'", "sandbox_git_openssl_unavailable"),
    ("SSL certificate problem: certificate has expired", "sandbox_git_certificate_invalid"),
    ("fatal: Authentication failed", "sandbox_git_auth_failed"),
])
async def test_probe_blocks_known_https_failures(tool, monkeypatch, error_output, expected):
    """失败时不返回 ready，也不在宿主重试远端操作。"""

    run = AsyncMock(side_effect=[process_result("https://github.com/o/r"), process_result(stderr=error_output, code=128)])
    monkeypatch.setattr(tool, "_run_process", run)
    with active_git(), pytest.raises(SandboxGitError) as error:
        await tool.check_git_https()
    assert error.value.error_code == expected
    assert run.await_count == 2


async def test_probe_helper_failure_never_echoes_stdout(tool, monkeypatch):
    """异常 helper 的标准输出不进入错误，防止它意外返回 Token。"""

    run = AsyncMock(side_effect=[process_result("https://github.com/o/r"), process_result("accidental-secret", code=1)])
    monkeypatch.setattr(tool, "_run_process", run)
    with active_git({"TEAMWORK_GIT_TOKEN": "test"}), pytest.raises(SandboxGitError) as error:
        await tool.check_git_https()
    assert error.value.error_code == "sandbox_git_askpass_unavailable"
    assert "accidental-secret" not in str(error.value)
    assert run.await_count == 2


async def test_probe_timeout_is_retryable(tool, monkeypatch):
    """临时超时与固定配置错误分开，不误标不可重试。"""

    monkeypatch.setattr(tool, "_run_process", AsyncMock(return_value=process_result(timed_out=True, code=-1)))
    with active_git(), pytest.raises(SandboxGitError) as error:
        await tool.check_git_https()
    assert error.value.error_code == "sandbox_git_probe_timeout"
    assert error.value.retryable is True


async def test_probe_cancellation_cleans_running_task(tool, monkeypatch):
    """完整 CLI 启动前的长探测同样响应取消，并等待子任务退出。"""

    cancelled = asyncio.Event()

    async def run(*args, **kwargs):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr(tool, "_run_process", run)
    tool.cancel_check = AsyncMock(return_value=True)
    with active_git(), pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(tool.check_git_https(), timeout=2)
    assert cancelled.is_set()


async def test_execute_command_blocks_tls_but_not_merge_conflict(tool, monkeypatch):
    """模型工具不能吞掉 TLS 基础设施故障继续运行；业务错误仍返回工具结果。"""

    run = AsyncMock(side_effect=[
        process_result(stderr="schannel: AcquireCredentialsHandle failed", code=128),
        process_result(stderr="CONFLICT (content): Merge conflict", code=1),
    ])
    monkeypatch.setattr(tool, "_run_process", run)
    with active_git():
        with pytest.raises(SandboxGitError):
            await tool.execute("execute_command", {"command": "git fetch origin"})
        result = await tool.execute("execute_command", {"command": "git merge origin/main"})
    assert result["exit_code"] == 1


@pytest.mark.parametrize("runner_class", [CodexModelRunner, CodexRunner])
async def test_runner_probe_failure_prevents_model_start(tool, monkeypatch, runner_class):
    """两种运行模式都在模型调用前失败，并保留脱敏、错误码与重试语义。"""

    tool.config.runtime.codex.model = "gpt-test"
    for module in ("codex_runner", "codex_model_runner"):
        monkeypatch.setattr(f"teamwork_review_agents.{module}.inspect_managed_sandbox", lambda *args, **kwargs:
                            ManagedSandboxInspection(available=True, platform="win32", backend="windows"))
        monkeypatch.setattr(f"teamwork_review_agents.{module}.validate_codex_version", lambda *args: None)
    failure = SandboxGitError("SSL certificate problem: test-token", error_code="sandbox_git_certificate_invalid")
    monkeypatch.setattr(ModelToolExecutor, "check_git_https", AsyncMock(side_effect=failure))
    model_call = AsyncMock(side_effect=AssertionError("探测失败后不应调用模型"))
    monkeypatch.setattr("teamwork_review_agents.codex_model_client.CodexResponsesClient.create_response", model_call)
    logs = []

    async def log(stream, event, payload):
        logs.append((event, payload))

    with active_git() as context:
        result = await runner_class(tool.config).run(
            run_id="run-git", root_run_id="run-git", parent_run_id=None,
            agent_name="code-reviewer", agent=tool.agent, repository=tool.repository,
            context=tool.context, prompt="测试", process_environment=context.environment,
            redactor=SecretRedactor(("test-token",)), log_callback=log,
        )
    assert result.status == "failed", result.error
    assert result.error_code == failure.error_code
    assert result.retryable is False
    assert "test-token" not in result.error
    assert any(event == "run.git_https_failed" for event, _ in logs)
    assert not any(event == "turn.started" for event, _ in logs)
    model_call.assert_not_called()


async def test_child_git_failure_propagates_to_parent(tool, monkeypatch):
    """委托阶段的基础设施故障必须打断父循环，不能当作普通工具反馈继续。"""

    executor = AgentExecutor(tool.config, StateStore(tool.config.database.path))
    error = AgentExecutionError("Git 证书失败", error_code="sandbox_git_certificate_invalid", retryable=False)
    monkeypatch.setattr(executor, "execute", AsyncMock(side_effect=error))
    with pytest.raises(SandboxGitError) as failure:
        await executor._invoke_embedded_agent(tool.context, "security-reviewer", "测试依赖", None, None)
    assert failure.value.error_code == error.error_code
    assert failure.value.retryable is False


async def test_model_loop_does_not_continue_after_git_failure(tool, monkeypatch):
    """启动探测通过后才发生的 Git 故障也必须阻断下一次模型和工具调用。"""

    tool.config.runtime.codex.model = "gpt-test"
    monkeypatch.setattr("teamwork_review_agents.codex_model_runner.inspect_managed_sandbox", lambda *args:
                        ManagedSandboxInspection(available=True, platform="win32", backend="windows"))
    monkeypatch.setattr("teamwork_review_agents.codex_model_runner.validate_codex_version", lambda *args: None)
    monkeypatch.setattr(ModelToolExecutor, "check_git_https", AsyncMock(return_value={"status": "ready"}))
    call = AsyncMock(return_value={"id": "response", "output": [
        {"type": "function_call", "call_id": "first", "name": "execute_command",
         "arguments": json.dumps({"command": "git fetch origin"})},
        {"type": "function_call", "call_id": "second", "name": "invoke_agent",
         "arguments": json.dumps({"agent_name": "security-reviewer", "task": "不应继续"})},
    ]})
    monkeypatch.setattr("teamwork_review_agents.codex_model_client.CodexResponsesClient.create_response", call)
    execute = AsyncMock(side_effect=SandboxGitError("证书失败", error_code="sandbox_git_certificate_invalid"))
    monkeypatch.setattr(ModelToolExecutor, "execute", execute)
    with active_git() as context:
        result = await CodexModelRunner(tool.config).run(
            run_id="run-git", root_run_id="run-git", parent_run_id=None,
            agent_name="code-reviewer", agent=tool.agent, repository=tool.repository,
            context=tool.context, prompt="测试", process_environment=context.environment,
        )
    assert result.status == "failed"
    assert result.error_code == "sandbox_git_certificate_invalid"
    assert execute.await_count == 1
    assert call.await_count == 1


async def test_cli_command_failure_terminates_process(tool, monkeypatch, tmp_path):
    """模拟 CLI 返回失败命令后继续等待，宿主必须终止进程而不是接受成功结论。"""

    runner = CodexRunner(tool.config)
    monkeypatch.setattr("teamwork_review_agents.codex_runner.validate_codex_version", lambda *args: None)
    monkeypatch.setattr(ModelToolExecutor, "check_git_https", AsyncMock(return_value={"status": "ready"}))
    event = {"type": "item.completed", "item": {
        "type": "command_execution", "exit_code": 128,
        "aggregated_output": "fatal: Unsupported SSL backend 'openssl'",
    }}
    # 模拟器仅输出协议后等待，不运行模型、网络或用户命令。
    program = f"import sys, time; sys.stdin.read(); print({json.dumps(event)!r}, flush=True); time.sleep(30)"
    monkeypatch.setattr(runner, "build_command", lambda *args, **kwargs: [sys.executable, "-c", program])
    with active_git() as context:
        result = await asyncio.wait_for(runner._run_with_projection(
            run_id="run-git", root_run_id="run-git", parent_run_id=None,
            agent_name="code-reviewer", agent=tool.agent, repository=tool.repository,
            context=tool.context, prompt="测试", process_environment=context.environment,
            skill_files={}, git_excludes_file=None, temporary_home=None, temporary_codex_home=None,
            managed_inspection=ManagedSandboxInspection(available=True, platform="win32", backend="windows"),
            mcp_bridge=McpBridgeChannel(directory=tmp_path, token="test", response_timeout_seconds=5),
            mcp_bridge_error=None,
        ), timeout=10)
    assert result.status == "failed"
    assert result.error_code == "sandbox_git_openssl_unavailable"
    assert result.retryable is False
