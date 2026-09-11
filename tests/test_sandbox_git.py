"""Windows 沙盒 Git HTTPS 的兼容配置、凭据边界与阻断回归。"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from teamwork_review_agents.codex_model_runner import CodexModelRunner
from teamwork_review_agents.codex_runner import CodexRunner
from teamwork_review_agents.environment import SecretRedactor
from teamwork_review_agents.events import detect_events
from teamwork_review_agents.executor import AgentExecutionError, AgentExecutor
from teamwork_review_agents.git_auth import GitCredentialContext
from teamwork_review_agents.managed_sandbox import ManagedSandboxInspection, permission_profile_override
from teamwork_review_agents.mcp_bridge import McpBridgeChannel
from teamwork_review_agents.model_tools import ModelToolExecutor
from teamwork_review_agents.models import InvocationContext
from teamwork_review_agents.sandbox_git import (
    SandboxGitContext, SandboxGitError, append_git_config, classify_git_failure,
    current_sandbox_git, windows_sandbox_git_enabled,
)
from teamwork_review_agents.state import StateStore


@contextmanager
def active_git(environment=None):
    """在当前任务中成对建立和释放上下文，避免测试间残留。"""

    context = SandboxGitContext(environment or {}).start()
    try:
        yield context
    finally:
        context.close()


def process_result(stdout="", stderr="", code=0, timed_out=False):
    """构造与真实工具进程相同的结果结构。"""

    return {"stdout": stdout, "stderr": stderr, "exit_code": code,
            "timed_out": timed_out, "truncated": False}


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
        assert environment["GIT_CONFIG_COUNT"] == "4"
        assert environment["GIT_CONFIG_KEY_2"] == "http.sslBackend"
        assert environment["GIT_CONFIG_VALUE_2"] == "openssl"
        assert environment["GIT_CONFIG_KEY_3"] == "http.sslVerify"
        assert environment["GIT_CONFIG_VALUE_3"] == "true"
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
    monkeypatch.setattr("teamwork_review_agents.sandbox_git.tempfile.mkdtemp", lambda **kwargs: str(directory))
    with active_git({"TEAMWORK_GIT_TOKEN": "dummy-credential"}) as context:
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
            return context.directory

    with active_git() as parent:
        directories = await asyncio.gather(child("first"), child("second"))
        assert directories[0] != directories[1]
        assert all(not path.exists() for path in directories)
        assert current_sandbox_git() is parent
    assert current_sandbox_git() is None


@pytest.mark.parametrize("output,code", [
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
