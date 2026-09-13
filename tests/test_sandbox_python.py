"""沙盒解释器选择、真实启动探针与运行绑定的跨平台回归。"""

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from teamwork_review_agents import sandbox_python, runtime_readiness
from teamwork_review_agents.codex_executable import CodexExecutable, CodexRuntimeError, active_codex_executable
from teamwork_review_agents.config import ManagedSandboxConfig
from teamwork_review_agents.managed_sandbox import ManagedSandboxInspection
from teamwork_review_agents.sandbox_python import SandboxPython, inspect_sandbox_python


def test_discovery_uses_host_runtime_not_agent_environment(tmp_path, monkeypatch):
    """只枚举服务账户的已有运行时，主运行时优先，显式路径永不隐式回退。"""

    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    for name in ("codex-primary-runtime", "other-runtime"):
        path = tmp_path / ".cache" / "codex-runtimes" / name / "dependencies" / "python" / "python.exe"
        path.parent.mkdir(parents=True)
        path.touch()
    candidates = sandbox_python._candidates(None)
    assert candidates[0][0].parts[-4] == "codex-primary-runtime"
    assert candidates[-1][0] == Path(sys.executable).resolve()
    missing = tmp_path / "not-installed.exe"
    assert sandbox_python._candidates(missing) == [(missing, "configured_path")]
    with pytest.raises(CodexRuntimeError) as raised:
        inspect_sandbox_python("codex", configured=missing, codex_home=None, environment={})
    assert raised.value.error_code == "sandbox_python_unavailable"
    assert not raised.value.retryable
    assert raised.value.details["python_candidates"][0]["reason"] == "not_found"


@pytest.mark.parametrize("failure", ["denied", "timeout", "invalid"])
def test_probe_requires_sandbox_execution_and_redacts_output(tmp_path, monkeypatch, failure):
    """文件存在不能代表可启动；超时可重试，拒绝执行与损坏响应不重试。"""

    calls = []
    killed = []

    class Probe:
        """记录真实启动形状，模拟 Windows ACL 拒绝。"""

        pid = 12345
        returncode = 1 if failure == "denied" else 0

        def __init__(self, command, **kwargs):
            calls.append((command, kwargs))

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def communicate(self, timeout=None):
            if failure == "timeout" and timeout is not None:
                raise subprocess.TimeoutExpired("codex", timeout)
            return b'{}', b"dummy-secret CreateProcessAsUserW failed: 5"

    monkeypatch.setattr(sandbox_python.subprocess, "Popen", Probe)
    monkeypatch.setattr(sandbox_python, "terminate_process", lambda pid, **kwargs: killed.append((pid, kwargs)))
    with pytest.raises(CodexRuntimeError) as raised:
        inspect_sandbox_python("trusted-codex", configured=Path(sys.executable), codex_home=tmp_path / "host-codex",
                               environment={**os.environ, "TEAMWORK_GIT_TOKEN": "dummy-secret", "PYTHONPATH": "untrusted"})
    command, options = calls[0]
    assert command[command.index("--") + 1:][:3] == [str(Path(sys.executable).resolve()), "-I", "-S"]
    assert "TEAMWORK_SANDBOX_INNER_DIRECTORIES" not in options["env"]
    assert "TEAMWORK_GIT_TOKEN" not in options["env"]
    assert "PYTHONPATH" not in options["env"]
    assert "network={enabled=false}" in command[command.index("--config") + 1]
    assert "dummy-secret" not in str(raised.value) + json.dumps(raised.value.details)
    assert raised.value.retryable is (failure == "timeout")
    assert not Path(command[command.index("--cd") + 1]).exists()
    assert bool(killed) is (failure == "timeout")
    if failure == "denied":
        assert raised.value.details["python_candidates"][0]["winerror"] == 5


def test_failed_candidate_falls_back_to_verified_candidate(tmp_path, monkeypatch):
    """自动模式按实际探针结果选择，路径与依赖元数据由沙盒内返回。"""

    denied = tmp_path / "denied.exe"
    denied.touch()
    candidate = Path(sys.executable).resolve()
    monkeypatch.setattr(sandbox_python, "_candidates", lambda configured: [(denied, "codex_runtime"), (candidate, "service_python")])
    original = subprocess.Popen
    calls = []

    def launch(command, **kwargs):
        calls.append(command)
        if len(calls) == 1:
            raise PermissionError(5, "拒绝访问")
        # 模拟原生沙盒已建立，执行真正的隔离标准库探针；不声称验证 ACL。
        return original(command[command.index("--") + 1:], **kwargs)

    monkeypatch.setattr(sandbox_python.subprocess, "Popen", launch)
    result = inspect_sandbox_python("codex", configured=None, codex_home=None, environment=os.environ)
    assert result.executable == str(candidate)
    assert result.discovery_source == "service_python"
    assert str(candidate.parent) in result.readable_directories
    assert len(calls) == 2


def test_windows_without_binding_fails_closed(monkeypatch):
    """调用者不能绕过预检偷偷使用不可执行的服务 Python。"""

    token = active_codex_executable.set(None)
    monkeypatch.setattr(sandbox_python, "os", SimpleNamespace(name="nt"))
    try:
        with pytest.raises(CodexRuntimeError, match="尚未通过"):
            sandbox_python.current_sandbox_python()
        selected = SandboxPython("verified-python", "configured_path", ("verified-lib",))
        active_codex_executable.set(CodexExecutable("codex", "codex.exe", "path", selected))
        assert sandbox_python.current_sandbox_python() is selected
    finally:
        active_codex_executable.reset(token)


def test_readiness_returns_python_binding(configured_app_factory, monkeypatch):
    """线程内返回验证结果，由执行器绑定，不能误以为线程内 ContextVar 会传播。"""

    config = configured_app_factory()
    config.runtime.codex_binary = sys.executable
    config.runtime.expected_codex_version = None
    config.runtime.managed_sandbox.python_binary = Path(sys.executable)
    monkeypatch.setattr(runtime_readiness, "windows_environment_separation", lambda: True)
    monkeypatch.setattr(runtime_readiness, "inspect_managed_sandbox", lambda *args, **kwargs: ManagedSandboxInspection(True, "Windows", "windows"))
    selected = SandboxPython("verified-python", "configured_path", ("verified-lib",))
    monkeypatch.setattr(runtime_readiness, "inspect_sandbox_python", lambda *args, **kwargs: selected)
    result = runtime_readiness.check_runtime_readiness(config, config.agents["code-reviewer"], config.repositories[0], {}, cli_execution=True)
    assert result.sandbox_python is selected
    assert result.as_dict()["sandbox_python"]["discovery_source"] == "configured_path"
    assert ManagedSandboxConfig(python_binary=" ").python_binary is None


def test_config_resolves_python_path_without_turning_blank_into_directory(configured_app_factory):
    """YAML 相对路径以配置文件为基准，空白与 null 都保留自动检测语义。"""

    import yaml
    from teamwork_review_agents.config import load_config

    config = configured_app_factory()
    document = yaml.safe_load(config.config_path.read_text(encoding="utf-8"))
    for value in (None, " ", "runtimes/python.exe"):
        document["runtime"]["managed_sandbox"] = {"python_binary": value, "enabled": True}
        config.config_path.write_text(yaml.safe_dump(document, allow_unicode=True), encoding="utf-8")
        result = load_config(config.config_path).runtime.managed_sandbox
        expected = config.config_path.parent / "runtimes/python.exe" if value == "runtimes/python.exe" else None
        assert result.python_binary == expected


def test_bridge_helper_and_mcp_use_same_selected_python(tmp_path, monkeypatch):
    """选定解释器与服务 Python 不同，三个消费点及只读路径必须一致。"""

    from teamwork_review_agents import managed_sandbox
    from teamwork_review_agents.sandbox_environment import separate_sandbox_environment
    from teamwork_review_agents.sandbox_git import SandboxGitContext
    from teamwork_review_agents.sandbox_mcp import standalone_mcp_command
    from teamwork_review_agents.config import AgentConfig

    executable = tmp_path / "bundled-python" / "python.exe"
    executable.parent.mkdir()
    executable.touch()
    selected = SandboxPython(str(executable), "codex_runtime", (str(executable.parent),))
    token = active_codex_executable.set(CodexExecutable("codex", "codex", "path", selected))
    workspace = tmp_path / "workspace"
    (workspace / ".git").mkdir(parents=True)
    helper_root = tmp_path / "git-runtime"
    helper_root.mkdir()
    monkeypatch.setattr(managed_sandbox, "windows_environment_separation", lambda: True)
    context = SandboxGitContext({"TEAMWORK_GIT_TOKEN": "dummy-token"}, verified_workspace=workspace, helper_root=helper_root)
    try:
        command, _ = separate_sandbox_environment(["git", "status"], {})
        assert command[0] == selected.executable
        assert standalone_mcp_command()[0] == selected.executable
        context.start()
        assert context.probe_command[0] == selected.executable
        assert executable.as_posix() in (context.directory / "askpass.sh").read_text(encoding="utf-8")
        profile = managed_sandbox.permission_profile_override(AgentConfig(prompt="测试", sandbox="read-only"))
        assert json.dumps(str(executable.parent)) + '="read"' in profile
        assert json.dumps(str(Path(sys.base_prefix).resolve())) + '="read"' not in profile
    finally:
        context.close()
        active_codex_executable.reset(token)
