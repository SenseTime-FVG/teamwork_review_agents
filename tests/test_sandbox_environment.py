"""两层目录环境、真实启动桥及显式启用的 Windows 原生沙盒验收。"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from teamwork_review_agents import agent_workspace, managed_sandbox, sandbox_environment
from teamwork_review_agents.agent_home import TemporaryAgentHome
from teamwork_review_agents.codex_runner import CodexRunner
from teamwork_review_agents.config import AgentWorkspaceConfig, AgentWorkspacePrepareStepConfig
from teamwork_review_agents.environment import SecretRedactor
from teamwork_review_agents.events import detect_events
from teamwork_review_agents.model_tools import ModelToolExecutor
from teamwork_review_agents.mcp_bridge import McpBridgeChannel
from teamwork_review_agents.models import InvocationContext
from teamwork_review_agents.process_control import hidden_process_options, pid_exists
from teamwork_review_agents.sandbox_environment import (
    DIRECTORY_ENVIRONMENT_NAMES, _INNER_DIRECTORIES_KEY,
    sandbox_executable_environment, sandbox_host_environment, separate_sandbox_environment,
)
from teamwork_review_agents.sandbox_git import SandboxGitContext, append_git_config


@pytest.fixture
def tool(tmp_path, configured_app_factory, snapshot_factory, monkeypatch):
    """建立带独立临时目录的工具执行器；不调用模型或网络。"""

    config = configured_app_factory()
    config.runtime.codex_home = tmp_path / "宿主 Codex"
    repository = config.repositories[0]
    repository.workspace = tmp_path / "workspace"
    repository.workspace.mkdir()
    snapshot = snapshot_factory(repository_id=repository.id, provider=repository.provider)
    context = InvocationContext(
        config_path=str(config.config_path), current_agent="code-reviewer",
        run_id="environment-test", root_run_id="environment-test",
        event=detect_events(None, snapshot, emit_initial=True)[0],
    )
    home = TemporaryAgentHome.create("environment-test", root=tmp_path / "homes")
    environment = dict(os.environ)
    home.apply_environment(environment, codex_home=home.path / "codex-home")
    environment["TEAMWORK_GIT_TOKEN"] = "dummy-not-a-real-token"
    environment["PYTHONIOENCODING"] = "utf-8"
    executor = ModelToolExecutor(
        config=config, agent=config.agents["code-reviewer"], repository=repository,
        context=context, environment=environment, managed_sandbox=True,
        cancel_check=None, progress_callback=lambda: None, invoke_agent_callback=None,
        codex_runtime_directory=home.path,
    )
    monkeypatch.setattr(managed_sandbox, "windows_environment_separation", lambda: True)
    monkeypatch.setattr("teamwork_review_agents.model_tools.resolve_codex_executable", lambda *args: "codex")
    try:
        yield executor
    finally:
        assert home.cleanup() is None


@pytest.fixture
def simulated_outer(monkeypatch):
    """仅模拟外层沙盒建立，真正执行沙盒内启动桥；不能代替 ACL 验收。"""

    original = asyncio.create_subprocess_exec
    calls = []

    async def launch(*command, **kwargs):
        calls.append((command, dict(kwargs["env"])))
        assert command[1] == "sandbox"
        environment = dict(kwargs["env"])
        environment["HTTPS_PROXY"] = "http://sandbox-proxy.invalid:1234"
        return await original(*command[command.index("--") + 1:], **{**kwargs, "env": environment})

    monkeypatch.setattr(asyncio, "create_subprocess_exec", launch)
    return calls


@pytest.mark.parametrize("source", ["config", "service", "default"])
def test_host_home_priority_and_case_insensitive_replacement(tmp_path, source):
    """宿主配置来源不能被 Agent 目录覆盖，且不向外层补入宿主秘密。"""

    host = {name.lower(): str(tmp_path / f"host-{name}") for name in DIRECTORY_ENVIRONMENT_NAMES}
    host["HOST_PRIVATE_TOKEN"] = "host-secret"
    if source == "default":
        host.pop("codex_home")
    configured = tmp_path / "configured" if source == "config" else None
    inner = {"Home": "agent-home", "CODEX_HOME": "agent-codex", "LOCALAPPDATA": "agent-local", "PATH": "child-path"}
    outer = sandbox_host_environment(inner, codex_home=configured, host_environment=host)
    expected = configured or (Path(host["codex_home"]) if source == "service" else Path(host["userprofile"]) / ".codex")
    assert outer["CODEX_HOME"] == str(expected.resolve())
    assert outer["HOME"] == host["home"]
    assert outer["LOCALAPPDATA"] == host["localappdata"]
    assert "Home" not in outer
    assert "HOST_PRIVATE_TOKEN" not in outer
    assert outer["PATH"] == "child-path"
    assert inner["Home"] == "agent-home"


def test_bridge_restores_missing_values_and_keeps_sandbox_proxy(tmp_path, monkeypatch):
    """缺失的内层目录必须删除；代理、凭据只保留既有授权，不进入桥参数。"""

    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "host-runtime"))
    environment = dict(os.environ)
    for name in tuple(environment):
        if name.upper() in DIRECTORY_ENVIRONMENT_NAMES:
            del environment[name]
    environment.update({"HOME": str(tmp_path / '临时 home " 引号'), "TEAMWORK_GIT_TOKEN": "dummy-secret"})
    inspected_names = {*DIRECTORY_ENVIRONMENT_NAMES, "TEAMWORK_GIT_TOKEN", "HTTPS_PROXY", _INNER_DIRECTORIES_KEY}
    code = f"import os,json; print(json.dumps({{k:v for k,v in os.environ.items() if k in {inspected_names!r}}}))"
    command, outer = separate_sandbox_environment([sys.executable, "-I", "-c", code], environment)
    outer["HTTPS_PROXY"] = "http://sandbox-proxy.invalid:1234"
    assert "dummy-secret" not in " ".join(command)
    assert "dummy-secret" not in outer[_INNER_DIRECTORIES_KEY]
    assert set(json.loads(outer[_INNER_DIRECTORIES_KEY])) == DIRECTORY_ENVIRONMENT_NAMES
    result = subprocess.run(command, env=outer, capture_output=True, text=True, timeout=15, **hidden_process_options())
    assert result.returncode == 0, result.stderr
    inner = json.loads(result.stdout)
    assert inner["HOME"] == environment["HOME"]
    assert "CODEX_HOME" not in inner
    assert "XDG_RUNTIME_DIR" not in inner
    assert _INNER_DIRECTORIES_KEY not in inner
    assert inner["HTTPS_PROXY"] == outer["HTTPS_PROXY"]
    assert inner["TEAMWORK_GIT_TOKEN"] == "dummy-secret"


async def test_tool_launch_uses_paired_environment_and_transparent_streams(tool, simulated_outer):
    """真实桥保留 stdin、标准错误和退出码，进程获得隔离目录而外层获得宿主目录。"""

    original = dict(tool.environment)
    code = (
        "import os,sys,json; sys.stdin.reconfigure(encoding='utf-8'); print(json.dumps({'home':os.environ['CODEX_HOME'],"
        "'proxy':os.environ['HTTPS_PROXY'],'stdin':sys.stdin.read()})); "
        "print('diagnostic',file=sys.stderr); sys.exit(23)"
    )
    result = await tool._run_process(tool._wrap([sys.executable, "-I", "-c", code]),
                                     cwd=tool.repository.workspace, timeout_seconds=15, input_text="prompt 输入")
    assert result["exit_code"] == 23
    assert result["stderr"].strip() == "diagnostic"
    inner = json.loads(result["stdout"])
    assert inner["home"] == original["CODEX_HOME"]
    assert inner["stdin"] == "prompt 输入"
    assert inner["proxy"] == "http://sandbox-proxy.invalid:1234"
    assert simulated_outer[0][1]["CODEX_HOME"] == str(tool.config.runtime.codex_home.resolve())
    assert tool.environment == original


@pytest.mark.parametrize("finish", ["timeout", "cancel"])
async def test_bridge_descendants_stop_with_tool(tool, simulated_outer, finish):
    """新增桥不创建脱离的进程组，超时与取消都必须终止真正命令及其子进程。"""

    marker = tool.repository.workspace / "pids.json"
    code = (
        "import os,sys,subprocess,time,json; from pathlib import Path; "
        "options={'creationflags':subprocess.CREATE_NO_WINDOW} if os.name=='nt' else {}; "
        "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'],**options); "
        f"p=Path({str(marker)!r}+'.tmp'); p.write_text(json.dumps([os.getpid(),child.pid])); "
        f"p.replace({str(marker)!r}); time.sleep(60)"
    )
    task = asyncio.create_task(tool._run_process(
        tool._wrap([sys.executable, "-I", "-c", code]), cwd=tool.repository.workspace,
        timeout_seconds=8 if finish == "timeout" else 30,
    ))
    try:
        async with asyncio.timeout(10):
            while not marker.exists():
                await asyncio.sleep(0.05)
        pids = json.loads(marker.read_text())
        if finish == "cancel":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            assert (await task)["timed_out"] is True
        async with asyncio.timeout(5):
            while any(pid_exists(pid) for pid in pids):
                await asyncio.sleep(0.05)
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_preparation_uses_empty_inner_codex_home(tool, simulated_outer, monkeypatch):
    """准备步骤消费成对环境，并在结束后清理空临时目录，不使用宿主登录态。"""

    monkeypatch.setattr(agent_workspace, "windows_environment_separation", lambda: True)
    monkeypatch.setattr(agent_workspace, "inspect_managed_sandbox", lambda *args:
                        managed_sandbox.ManagedSandboxInspection(True, "Windows", "windows"))
    monkeypatch.setattr(agent_workspace, "resolve_codex_executable", lambda *args: "codex")
    marker = tool.repository.workspace / "prepared.json"
    code = (
        "import os,json; from pathlib import Path; p=Path(os.environ['CODEX_HOME']); "
        f"Path({str(marker)!r}).write_text(json.dumps([str(p),list(p.iterdir())])); print('prepared')"
    )
    tool.repository.agent_workspace = AgentWorkspaceConfig(prepare_steps=[
        AgentWorkspacePrepareStepConfig(name="检查准备环境", command=[sys.executable, "-I", "-c", code]),
    ])
    async def log(*args):
        """不输出测试环境内容。"""

    result = await agent_workspace.prepare_agent_workspace(
        config=tool.config, repository=tool.repository, agent=tool.agent,
        process_environment={"CODEX_HOME": "untrusted-override"},
        redactor=SecretRedactor(()), log_callback=log, cancel_check=lambda: False,
    )
    assert result.outcome.status == "success", result.outcome.error
    home, files = json.loads(marker.read_text())
    assert files == []
    assert not Path(home).exists()
    assert simulated_outer[0][1]["CODEX_HOME"] == str(tool.config.runtime.codex_home.resolve())


def test_cli_launch_and_inspection_do_not_inherit_agent_home(tool):
    """完整 CLI 与能力诊断采用相同宿主来源，内层仍指向原有临时 Codex home。"""

    launch = CodexRunner(tool.config).build_launch(
        tool.agent, tool.repository, tool.context, managed_sandbox=True,
        environment=tool.environment, codex_runtime_directory=tool.codex_runtime_directory,
    )
    expected = str(tool.config.runtime.codex_home.resolve())
    assert launch.environment["CODEX_HOME"] == expected
    assert json.loads(launch.environment[_INNER_DIRECTORIES_KEY])["CODEX_HOME"] == tool.environment["CODEX_HOME"]
    outer_arguments = launch.command[:launch.command.index("--")]
    assert expected not in " ".join(outer_arguments)
    assert "--dangerously-bypass-approvals-and-sandbox" in launch.command[launch.command.index("--") + 1:]
    inspection = managed_sandbox._inspection_environment(tool.config.runtime.codex_home, tool.environment)
    assert inspection["CODEX_HOME"] == expected
    assert "TEAMWORK_GIT_TOKEN" not in inspection


async def test_cli_process_consumes_outer_environment(tool, monkeypatch):
    """正式 CLI 启动点必须消费配对环境，不能只正确构造但启动时仍传旧环境。"""

    runner = CodexRunner(tool.config)
    monkeypatch.setattr(runner, "child_environment", lambda *args, **kwargs: dict(tool.environment))
    monkeypatch.setattr("teamwork_review_agents.codex_runner.validate_codex_version", lambda *args: None)
    monkeypatch.setattr("teamwork_review_agents.codex_runner.resolve_codex_executable", lambda *args, **kwargs: "codex")
    class LaunchChecked(Exception):
        """到达受检查启动点即停止，不运行模型。"""

    async def checked_launch(*command, **kwargs):
        assert kwargs["env"]["CODEX_HOME"] == str(tool.config.runtime.codex_home.resolve())
        assert json.loads(kwargs["env"][_INNER_DIRECTORIES_KEY])["CODEX_HOME"] == tool.environment["CODEX_HOME"]
        assert command[command.index("--") + 1] == sys.executable
        raise LaunchChecked

    monkeypatch.setattr(asyncio, "create_subprocess_exec", checked_launch)
    with pytest.raises(LaunchChecked):
        await runner._run_with_projection(
            run_id="test", root_run_id="test", parent_run_id=None,
            agent_name="code-reviewer", agent=tool.agent, repository=tool.repository,
            context=tool.context, prompt="测试", process_environment=tool.environment,
            skill_files={}, git_excludes_file=None, temporary_home=None, temporary_codex_home=None,
            managed_inspection=managed_sandbox.ManagedSandboxInspection(True, "Windows", "windows"),
            mcp_bridge=McpBridgeChannel(directory=tool.codex_runtime_directory, token="dummy-channel", response_timeout_seconds=30),
            mcp_bridge_error=None, cancel_check=None, log_callback=None, redactor=None,
        )


def test_posix_wrapper_does_not_change_environment(tool, monkeypatch):
    """Linux/macOS 仍直接执行原内层命令，不增加启动桥或修改环境。"""

    monkeypatch.setattr(managed_sandbox, "windows_environment_separation", lambda: False)
    inner = [sys.executable, "-c", "pass"]
    launch = tool._wrap(inner)
    assert launch.command[launch.command.index("--") + 1:] == inner
    assert launch.environment == tool.environment
    assert launch.environment is not tool.environment


def test_program_discovery_uses_host_installation_directory(tmp_path, monkeypatch):
    """临时 LOCALAPPDATA 不应使外层 Codex 丢失自动发现结果；PATH 覆盖仍保留。"""

    monkeypatch.setattr(sandbox_environment, "windows_environment_separation", lambda: True)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "host-local"))
    inner = {"LOCALAPPDATA": str(tmp_path / "agent-local"), "PATH": "explicit-child-path"}
    outer = sandbox_executable_environment(inner)
    assert outer["LOCALAPPDATA"] == str(tmp_path / "host-local")
    assert outer["PATH"] == "explicit-child-path"
    assert inner["LOCALAPPDATA"] == str(tmp_path / "agent-local")


async def test_real_windows_sandbox_askpass_and_credential_fill(tool, monkeypatch):
    """显式验收真实 Windows ACL；不调用模型、GitHub 或真实 Token。"""

    if os.environ.get("TEAMWORK_TEST_WINDOWS_SANDBOX") != "1":
        pytest.skip("需显式设置 TEAMWORK_TEST_WINDOWS_SANDBOX=1 才执行真实 Windows 沙盒")
    assert sys.platform == "win32", "真实验收必须在 Windows 运行"
    from teamwork_review_agents.codex_executable import resolve_codex_executable

    # 使用正常服务宿主 Codex home，不创建未经初始化的外层临时 home。
    tool.config.runtime.codex_home = None
    tool.config.runtime.codex_binary = os.environ.get("TEAMWORK_TEST_CODEX_BINARY", "codex")
    monkeypatch.setattr("teamwork_review_agents.model_tools.resolve_codex_executable", resolve_codex_executable)
    inspection = managed_sandbox.inspect_managed_sandbox(tool.config.runtime.codex_binary)
    assert inspection.available, inspection.error
    git = shutil.which("git")
    assert git, "验收需要安装 Git for Windows"
    tool.config.runtime.codex_binary = inspection.resolved_path
    environment = dict(tool.environment)
    for key in tuple(environment):
        if key.upper().startswith(("GIT_", "TEAMWORK_GIT_")):
            del environment[key]
    environment.update({"TEAMWORK_GIT_TOKEN": "dummy-not-a-real-token", "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull})
    result = subprocess.run([git, "init", str(tool.repository.workspace)], env=environment,
                            capture_output=True, timeout=15, **hidden_process_options())
    assert result.returncode == 0
    append_git_config(environment, {"credential.helper": ""})
    # 还原报告中的条件：helper 位于内层 CODEX_HOME 的可写子目录。
    inner_home = Path(environment["CODEX_HOME"])
    inner_home.mkdir(parents=True, exist_ok=True)
    context = SandboxGitContext(environment, verified_workspace=tool.repository.workspace, helper_root=inner_home).start()
    try:
        tool.environment = context.environment
        probe = await tool._run_process(tool._wrap(context.probe_command), cwd=tool.repository.workspace, timeout_seconds=60)
        assert probe["exit_code"] == 0, probe["stderr"]
        assert probe["stdout"].strip() == "teamwork-askpass-ready"
        # 凭据只在沙盒内比较，不让密码进入 pytest 输出、日志或快照。
        code = (
            "import os,subprocess,sys; "
            "p=subprocess.run([sys.argv[1],'credential','fill'],input='protocol=https\\nhost=example.test\\n\\n',"
            "text=True,capture_output=True,timeout=20,creationflags=subprocess.CREATE_NO_WINDOW); "
            "v=dict(line.split('=',1) for line in p.stdout.splitlines() if '=' in line); "
            "ok=p.returncode==0 and v.get('password')==os.environ['TEAMWORK_GIT_TOKEN'] and v.get('username')=='x-access-token'; "
            "print('credential-ready' if ok else 'credential-failed'); sys.exit(0 if ok else 1)"
        )
        fill = await tool._run_process(tool._wrap([sys.executable, "-I", "-c", code, git]),
                                       cwd=tool.repository.workspace, timeout_seconds=60)
        assert fill["exit_code"] == 0, fill["stderr"]
        assert fill["stdout"].strip() == "credential-ready"
        for path in context.directory.iterdir():
            assert b"dummy-not-a-real-token" not in path.read_bytes()
    finally:
        directory = context.directory
        context.close()
    assert not directory.exists()
