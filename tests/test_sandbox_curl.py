"""curl 选择、沙盒权限与非 Git HTTP 故障的跨平台回归。"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
import yaml

from teamwork_review_agents import sandbox_curl
from teamwork_review_agents.codex_runner import CodexRunner
from teamwork_review_agents.codex_model_runner import CodexModelRunner
from teamwork_review_agents.config import ManagedSandboxConfig, load_config
from teamwork_review_agents.events import detect_events
from teamwork_review_agents.managed_sandbox import ManagedSandboxInspection, permission_profile_override
from teamwork_review_agents.mcp_bridge import McpBridgeChannel
from teamwork_review_agents.model_tools import ModelToolExecutor, shell_command
from teamwork_review_agents.models import InvocationContext
from teamwork_review_agents.sandbox_curl import (
    CurlCandidate, SandboxCurlContext, curl_candidates, current_sandbox_curl,
    https_probe_url, openssl_curl_version,
)
from teamwork_review_agents.sandbox_git import SandboxGitContext, SandboxGitError, classify_git_failure, is_simple_git_command
from teamwork_review_agents.subprocess_utils import ProcessLaunch

pytestmark = pytest.mark.usefixtures("verified_test_sandbox_python")
_VERSION = "curl 8.10.1 libcurl/8.10.1 OpenSSL/3.2.0 (Schannel)\nProtocols: http https\nFeatures: MultiSSL\n"
_CURL_ERROR = "curl: (35) schannel: AcquireCredentialsHandle failed: SEC_E_NO_CREDENTIALS"


def result(stdout="", *, stderr="", code=0, timeout=False):
    """构造与进程执行器相同的有界输出结果。"""

    return {"stdout": stdout, "stderr": stderr, "exit_code": code, "timed_out": timeout, "truncated": False}


@contextmanager
def active_curl(configured=None, *, host=None):
    """测试必须恢复上下文，避免污染并发任务或宿主命令。"""

    context = SandboxCurlContext(configured, host_environment=host or {}).start()
    try:
        yield context
    finally:
        context.close()


@pytest.fixture
def binary(tmp_path):
    """模拟带空格与非 ASCII 字符的安装目录；文件从不在宿主执行。"""

    path = tmp_path / "已安装 Git" / "mingw64" / "bin" / "curl.exe"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"test-only")
    return path


@pytest.fixture
def tool(configured_app_factory, snapshot_factory, monkeypatch):
    """仅替换 Codex 发现；真正的进程行为由各用例分别指定。"""

    config = configured_app_factory()
    repository = config.repositories[0]
    (repository.workspace / ".git").mkdir()
    agent = config.agents["code-reviewer"]
    agent.sandbox = "workspace-write"
    agent.network_access = True
    agent.network_domains = ["github.com"]
    context = InvocationContext(
        config_path=str(config.config_path), current_agent="code-reviewer", run_id="curl-test",
        root_run_id="curl-test", active_workspace=str(repository.workspace),
        event=detect_events(None, snapshot_factory(repository_id=repository.id, provider=repository.provider), emit_initial=True)[0],
    )
    monkeypatch.setattr("teamwork_review_agents.model_tools.resolve_codex_executable", lambda *args: "codex")
    return ModelToolExecutor(config=config, agent=agent, repository=repository, context=context,
                             environment={}, managed_sandbox=True, cancel_check=None,
                             progress_callback=lambda: None, invoke_agent_callback=None)


@pytest.mark.parametrize("first,protocols,expected", [
    ("OpenSSL/3.2 (Schannel)", "http https", "OpenSSL/3.2"),
    ("Schannel (OpenSSL/3.2)", "http https", None),
    ("Schannel", "http https", None),
    ("LibreSSL/4.1", "http https", "LibreSSL/4.1"),
    ("OpenSSL/3.2", "http", None),
])
def test_version_checks_selected_backend_not_available_alternative(first, protocols, expected):
    """MultiSSL 中只有非括号后端代表本次实际使用值。"""

    assert openssl_curl_version(f"curl 8 libcurl/8 {first}\nProtocols: {protocols}\n") == expected
    assert openssl_curl_version("untrusted output") is None


def test_discovery_prefers_git_and_excludes_relative_path(binary, tmp_path, monkeypatch):
    """不执行工作区同名程序，Git 安装中发现的 CA 与路径来源明确。"""

    root = binary.parents[2]
    (root / "cmd").mkdir()
    (root / "cmd" / "git.exe").write_bytes(b"test-only")
    ca = root / "mingw64" / "etc" / "ssl" / "certs" / "ca-bundle.crt"
    ca.parent.mkdir(parents=True)
    ca.write_text("dummy CA", encoding="utf-8")
    other = tmp_path / "system"
    other.mkdir()
    (other / "curl.exe").write_bytes(b"test-only")
    monkeypatch.chdir(other)
    candidates = curl_candidates(None, {"Path": os.pathsep.join((".", str(other), str(root / "cmd"), str(binary.parent)))})
    assert candidates[0] == CurlCandidate(binary.resolve(), "git_installation", ca.resolve())
    assert len(candidates) == 2
    assert curl_candidates(None, {"PATH": "."}) == []
    assert curl_candidates(tmp_path / "missing.exe", {"PATH": str(other)})[0].source == "configured_path"


@pytest.mark.parametrize("value", [None, " ", "runtime/curl.exe"])
def test_curl_configuration_path_round_trip(configured_app_factory, value):
    """空白保持自动发现，相对路径按配置文件目录解析。"""

    config = configured_app_factory()
    document = yaml.safe_load(config.config_path.read_text(encoding="utf-8"))
    document["runtime"]["managed_sandbox"] = {"curl_binary": value}
    config.config_path.write_text(yaml.safe_dump(document), encoding="utf-8")
    managed = load_config(config.config_path).runtime.managed_sandbox
    assert managed.curl_binary == (config.config_path.parent / value if value and value.strip() else None)
    assert ManagedSandboxConfig(curl_binary=" ").curl_binary is None


@pytest.mark.parametrize("url,expected", [
    ("https://user:secret@github.com/private?key=secret", "https://github.com/"),
    ("https://example.test:9443/api/v4", "https://example.test:9443/"),
    ("http://example.test", None), ("ssh://git@github.com/repo", None),
    ("https://host:invalid", None),
])
def test_probe_url_drops_credentials_and_business_path(url, expected):
    """HTTP 探针不复用业务 URL 凭据、查询或路径。"""

    assert https_probe_url(url) == expected


async def test_sandbox_selection_keeps_proxy_ca_and_scopes_permissions(tool, binary, monkeypatch):
    """版本与 HTTPS 都经过同一沙盒，TLS/代理设置及只读权限不丢失。"""

    monkeypatch.setattr("teamwork_review_agents.managed_sandbox.windows_environment_separation", lambda: True)
    original = {"Path": "existing-path", "CURL_CA_BUNDLE": "enterprise.pem", "HTTPS_PROXY": "http://proxy.test",
                "TEAMWORK_GIT_TOKEN": "do-not-log", "CURL_SSL_BACKEND": "schannel"}
    tool.environment = dict(original)
    calls = []

    async def run(launch, **kwargs):
        calls.append(launch)
        assert launch.command[1] == "sandbox"
        assert "--permission-profile" in launch.command
        profile = launch.command[launch.command.index("--config") + 1]
        assert 'network={enabled=true,mode="limited",domains={"github.com"="allow"}}' in profile
        assert f'{json.dumps(str(binary.parent), ensure_ascii=False)}="read"' in profile
        assert launch.environment["HTTPS_PROXY"] == "http://proxy.test"
        assert launch.environment["CURL_CA_BUNDLE"] == "enterprise.pem"
        assert launch.environment["CURL_SSL_BACKEND"] == "openssl"
        assert "do-not-log" not in str(launch.command)
        return result(_VERSION if "--version" in launch.command else "teamwork-curl-ready:403")

    monkeypatch.setattr(tool, "_run_process", run)
    with active_curl(binary) as context:
        diagnostic = await tool.prepare_curl()
        assert diagnostic["status"] == "ready"
        assert diagnostic["https_probe"] == "passed"
        assert context.selected.executable == binary
        assert tool.environment["PATH"].split(os.pathsep)[0] == str(binary.parent)
        assert "Path" not in tool.environment
        assert "do-not-log" not in str(diagnostic)
        assert "--head" in calls[1].command
        assert "-k" not in calls[1].command
        assert await tool.prepare_curl() == diagnostic
        assert len(calls) == 2
    assert original["CURL_SSL_BACKEND"] == "schannel"
    assert current_sandbox_curl() is None


@pytest.mark.parametrize("network,url", [(False, "https://github.com/"), (True, None)])
async def test_skip_network_without_widening_policy(binary, network, url):
    """禁网或非 HTTPS 平台仍可验版本，但不得伪造 HTTPS 已通过。"""

    probe = AsyncMock(return_value=result(_VERSION))
    with active_curl(binary) as context:
        diagnostic = await context.prepare(probe, {}, probe_url=url, network_access=network)
    assert diagnostic["status"] == "ready"
    assert diagnostic["https_probe"] == "skipped"
    assert probe.await_count == 1


@pytest.mark.parametrize("failure", [result(_CURL_ERROR, code=35), result(timeout=True, code=-1),
                                         result("curl 8 libcurl/8 Schannel\nProtocols: https")])
async def test_explicit_failure_does_not_fallback_or_expose_output(binary, failure):
    """显式路径失败只提示，不退回别的安装，不输出含凭据的原始错误。"""

    probe = AsyncMock(return_value={**failure, "stderr": "Authorization: secret"})
    with active_curl(binary) as context:
        diagnostic = await context.prepare(probe, {}, probe_url="https://github.com/", network_access=True)
        assert context.selected is None
        assert context.readable_directories == ()
        assert context.apply_environment({"PATH": "unchanged"}) == {"PATH": "unchanged"}
    assert diagnostic["status"] == "unavailable"
    assert "secret" not in str(diagnostic)
    assert probe.await_count == 1


async def test_automatic_fallback_and_default_ca(binary, tmp_path, monkeypatch):
    """自动发现可以跳过 Schannel-only；显式 CA 配置优先于 Git 随附 CA。"""

    incompatible = tmp_path / "system" / "curl.exe"
    incompatible.parent.mkdir()
    incompatible.write_bytes(b"test-only")
    ca = binary.parent / "ca.crt"
    ca.write_text("test-only", encoding="utf-8")
    monkeypatch.setattr(sandbox_curl, "curl_candidates", lambda *args: [CurlCandidate(incompatible, "service_path"), CurlCandidate(binary, "git_installation", ca)])
    probe = AsyncMock(side_effect=[result("curl 8 libcurl/8 Schannel\nProtocols: https"), result(_VERSION), result("teamwork-curl-ready:200")])
    with active_curl() as context:
        diagnostic = await context.prepare(probe, {}, probe_url="https://github.com/", network_access=True)
        assert diagnostic["curl_binary"] == str(binary)
        assert context.apply_environment({})["CURL_CA_BUNDLE"] == str(ca)
        assert "CURL_CA_BUNDLE" not in context.apply_environment({"SSL_CERT_FILE": "custom.pem"})
    assert probe.await_count == 3


async def test_curl_cancellation_cleans_pending_probe(tool, binary, monkeypatch):
    """取消时完成沙盒子任务回收，候选权限不得残留。"""

    stopped = asyncio.Event()

    async def run(*args, **kwargs):
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    tool.cancel_check = AsyncMock(return_value=True)
    monkeypatch.setattr(tool, "_run_process", run)
    with active_curl(binary) as context:
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(tool.prepare_curl(), timeout=2)
        assert context.readable_directories == ()
    assert stopped.is_set()


async def test_optional_curl_probe_has_total_budget(binary):
    """候选耗尽总预算也只提示，不能变成整轮失败，外部取消则仍正常传播。"""

    stopped = asyncio.Event()

    async def probe(*args):
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    with active_curl(binary) as context:
        diagnostic = await context.prepare(probe, {}, probe_url=None, network_access=False, budget_seconds=0.01)
        assert diagnostic["status"] == "unavailable"
        assert diagnostic["reason"] == "probe_budget_exhausted"
        assert context.readable_directories == ()
    assert stopped.is_set()


async def test_parallel_and_nested_contexts_are_independent(binary):
    """并发子运行不共享可变选择，退出后恢复父绑定。"""

    async def child():
        with active_curl(binary) as context:
            await asyncio.sleep(0)
            assert current_sandbox_curl() is context
            return id(context)

    with active_curl() as parent:
        children = await asyncio.gather(child(), child())
        assert len(set(children)) == 2
        assert current_sandbox_curl() is parent


def test_shell_alias_is_process_local_and_posix_unchanged(binary):
    """只覆盖本次 PowerShell 别名，绝对 System32 命令文本保持不变。"""

    command = r"C:\Windows\System32\curl.exe https://example.test"
    launch = shell_command(command, platform_name="win32", curl_binary=binary)
    assert "Set-Alias -Name curl" in launch[-1]
    assert str(binary) in launch[-1]
    assert launch[-1].endswith(command)
    assert "Set-Alias" not in shell_command(command, platform_name="linux", os_name="posix", curl_binary=binary)[-1]
    assert "Set-Alias" not in shell_command(command, platform_name="win32")[-1]


@pytest.mark.parametrize("command,expected", [
    ("git fetch origin", True), (r'& "C:\Program Files\Git\cmd\git.exe" fetch origin', True),
    ("git status; curl.exe https://example.test", False),
    ("git status && curl.exe https://example.test", False),
    ("curl.exe https://example.test", False), ("python -c 'print(1)'", False),
    ("git fetch $(curl example.test)", False), ("powershell -Command 'git fetch origin'", False),
])
def test_command_origin_is_conservative(command, expected):
    """复合 shell、未知包装器与子表达式不推断 Git 来源。"""

    assert is_simple_git_command(command) is expected


@pytest.mark.parametrize("output", [_CURL_ERROR, "curl: (60) SSL certificate problem: expired", "curl: (22) The requested URL returned error: 403"])
def test_http_output_cannot_become_git_failure(output):
    """相同的 TLS/认证词汇不能把普通 HTTP 失败变成 Git 基础设施故障。"""

    assert classify_git_failure(output) is None
    assert classify_git_failure("fatal: unable to access 'https://github.com/r': schannel: AcquireCredentialsHandle failed") is not None


async def test_tool_curl_failure_is_returned_without_replay(tool, binary, monkeypatch):
    """模型拿到恢复提示并保留原退出码；Git 真错误仍阻断。"""

    run = AsyncMock(return_value=result(stderr=_CURL_ERROR, code=35))
    monkeypatch.setattr(tool, "_run_process", run)
    git = SandboxGitContext({}, verified_workspace=tool.repository.workspace, helper_root=None).start()
    try:
        with active_curl(binary) as context:
            context.selected = CurlCandidate(binary, "configured_path")
            outcome = await tool.execute("execute_command", {"command": "curl.exe https://example.test"})
            assert outcome["exit_code"] == 35
            assert "Teamwork HTTP TLS 提示" in outcome["stderr"]
            assert run.await_count == 1
            with pytest.raises(SandboxGitError):
                await tool.execute("execute_command", {"command": "git fetch origin"})
    finally:
        git.close()


@pytest.mark.parametrize("curl_available", [True, False])
async def test_cli_curl_failure_does_not_terminate_successful_turn(tool, binary, monkeypatch, tmp_path, curl_available):
    """CLI 先报告 curl 失败再正常完成，不得被宿主误杀；启动提示沿用普通命令。"""

    runner = CodexRunner(tool.config)
    monkeypatch.setattr("teamwork_review_agents.codex_runner.validate_codex_version", lambda *args: None)
    monkeypatch.setattr(ModelToolExecutor, "check_git_https", AsyncMock(return_value={"status": "ready"}))
    probes = AsyncMock(side_effect=[result(_VERSION), result("teamwork-curl-ready:200")] if curl_available
                       else [result("curl 8 libcurl/8 Schannel\nProtocols: https")])
    monkeypatch.setattr(ModelToolExecutor, "_run_process", probes)
    events = [
        {"type": "item.completed", "item": {"type": "command_execution", "command": "curl.exe https://example.test", "exit_code": 35, "aggregated_output": _CURL_ERROR}},
        {"type": "item.completed", "item": {"type": "agent_message", "text": "已改用可用命令完成"}},
        {"type": "turn.completed", "usage": {}},
    ]
    program = f"import sys; p=sys.stdin.read(); assert 'curl' in p; print({chr(10).join(json.dumps(event) for event in events)!r}, flush=True)"
    launched = []

    def build(*args, **kwargs):
        """记录最终 CLI 环境，确保选择结果不只停留在预检执行器内。"""

        launched.append(dict(kwargs["environment"]))
        return ProcessLaunch([sys.executable, "-c", program], launched[-1])

    monkeypatch.setattr(runner, "build_launch", build)
    logs = []

    async def log(stream, event, payload):
        logs.append(event)

    git = SandboxGitContext({}, verified_workspace=tool.repository.workspace, helper_root=None).start()
    try:
        with active_curl(binary) as context:
            outcome = await asyncio.wait_for(runner._run_with_projection(
                run_id="curl-test", root_run_id="curl-test", parent_run_id=None,
                agent_name="code-reviewer", agent=tool.agent, repository=tool.repository,
                context=tool.context, prompt="测试", process_environment=git.environment,
                skill_files={}, git_excludes_file=None, temporary_home=None, temporary_codex_home=None,
                managed_inspection=ManagedSandboxInspection(True, "win32", "windows"),
                mcp_bridge=McpBridgeChannel(directory=tmp_path, token="test", response_timeout_seconds=5),
                mcp_bridge_error=None, log_callback=log,
            ), timeout=10)
    finally:
        git.close()
    assert outcome.status == "completed", outcome.error
    assert "run.http_tls_unavailable" in logs
    assert "run.git_https_failed" not in logs
    assert ("run.curl_ready" if curl_available else "run.curl_unavailable") in logs
    if curl_available:
        assert launched[0]["PATH"].split(os.pathsep)[0] == str(binary.parent)
        assert launched[0]["CURL_SSL_BACKEND"] == "openssl"


async def test_model_loop_keeps_existing_tools_and_continues_after_http_error(tool, binary, monkeypatch):
    """Git 预检已过、curl 已选但单条请求失败时，模型仍能接收工具结果继续处理。"""

    tool.config.runtime.codex.model = "gpt-test"
    monkeypatch.setattr("teamwork_review_agents.codex_model_runner.inspect_managed_sandbox", lambda *args: ManagedSandboxInspection(True, "win32", "windows"))
    monkeypatch.setattr("teamwork_review_agents.codex_model_runner.validate_codex_version", lambda *args: None)
    monkeypatch.setattr(ModelToolExecutor, "check_git_https", AsyncMock(return_value={"status": "ready"}))
    # 真实 prepare_curl 接入仍执行，仅将外部进程替换成确定性的探针/命令结果。
    process = AsyncMock(side_effect=[result(_VERSION), result("teamwork-curl-ready:200"), result(stderr=_CURL_ERROR, code=35)])
    monkeypatch.setattr(ModelToolExecutor, "_run_process", process)
    requests = []

    async def respond(payload, **kwargs):
        requests.append(json.loads(json.dumps(payload)))
        if len(requests) == 1:
            return {"id": "first", "output": [{"type": "function_call", "call_id": "http", "name": "execute_command",
                                                "arguments": json.dumps({"command": "curl.exe https://example.test"})}]}
        return {"id": "second", "output": [{"type": "message", "content": [{"type": "output_text", "text": "处理完成"}]}]}

    monkeypatch.setattr("teamwork_review_agents.codex_model_client.CodexResponsesClient.create_response", AsyncMock(side_effect=respond))
    git = SandboxGitContext({}, verified_workspace=tool.repository.workspace, helper_root=None).start()
    try:
        with active_curl(binary):
            outcome = await CodexModelRunner(tool.config).run(
                run_id="curl-test", root_run_id="curl-test", parent_run_id=None,
                agent_name="code-reviewer", agent=tool.agent, repository=tool.repository,
                context=tool.context, prompt="测试", process_environment=git.environment,
            )
    finally:
        git.close()
    assert outcome.status == "completed", outcome.error
    assert len(requests) == 2
    assert process.await_count == 3
    assert {item["name"] for item in requests[0]["tools"]} == {"execute_command", "apply_patch", "invoke_agent"}
    assert str(binary) in requests[0]["input"][0]["content"][0]["text"]
    assert "Teamwork HTTP TLS 提示" in json.dumps(requests[1], ensure_ascii=False)
    selected_environment = process.call_args_list[-1].args[0].environment
    assert selected_environment["PATH"].split(os.pathsep)[0] == str(binary.parent)


async def test_unmanaged_tool_does_not_select_curl(tool, binary, monkeypatch):
    """父运行绑定不能使完全访问工具意外开始探测或改变 PATH。"""

    tool.managed_sandbox = False
    original = dict(tool.environment)
    run = AsyncMock(side_effect=AssertionError("非托管模式不得探测"))
    monkeypatch.setattr(tool, "_run_process", run)
    with active_curl(binary):
        assert await tool.prepare_curl() == {"status": "skipped"}
    assert tool.environment == original


async def test_real_windows_sandbox_curl_https(tool, monkeypatch):
    """显式启用的 Windows TLS 验收：版本、真实 HTTPS 和 PowerShell 路径解析。"""

    if os.environ.get("TEAMWORK_TEST_WINDOWS_CURL") != "1":
        pytest.skip("需显式设置 TEAMWORK_TEST_WINDOWS_CURL=1 才连接 HTTPS 验收")
    assert sys.platform == "win32", "真实 curl 验收必须在 Windows 运行"
    from teamwork_review_agents.codex_executable import CodexExecutable, active_codex_executable, resolve_codex_executable
    from teamwork_review_agents.managed_sandbox import inspect_managed_sandbox
    from teamwork_review_agents.sandbox_python import inspect_sandbox_python

    tool.config.runtime.codex_home = None
    tool.config.runtime.codex_binary = os.environ.get("TEAMWORK_TEST_CODEX_BINARY", "codex")
    monkeypatch.setattr("teamwork_review_agents.model_tools.resolve_codex_executable", resolve_codex_executable)
    inspection = inspect_managed_sandbox(tool.config.runtime.codex_binary)
    assert inspection.available, inspection.error
    python = await asyncio.to_thread(
        inspect_sandbox_python, inspection.resolved_path,
        configured=Path(os.environ["TEAMWORK_TEST_SANDBOX_PYTHON"]) if os.environ.get("TEAMWORK_TEST_SANDBOX_PYTHON") else None,
        codex_home=None, environment=os.environ,
    )
    binding = active_codex_executable.set(CodexExecutable(tool.config.runtime.codex_binary, inspection.resolved_path, inspection.discovery_source, python))
    tool.config.runtime.codex_binary = inspection.resolved_path
    # 只继承运行必需字段和明确的代理/CA，不携带业务凭据或用户 curlrc。
    from teamwork_review_agents.codex_runner import BASE_ENVIRONMENT_NAMES
    from teamwork_review_agents.subprocess_utils import selected_environment
    tool.environment = selected_environment(BASE_ENVIRONMENT_NAMES | {"CURL_CA_BUNDLE"}, os.environ)
    for name in ("OPENAI_API_KEY", "CODEX_API_KEY"):
        tool.environment.pop(name, None)
    tool.agent.network_domains = ["api.github.com"]
    configured = Path(os.environ["TEAMWORK_TEST_SANDBOX_CURL"]) if os.environ.get("TEAMWORK_TEST_SANDBOX_CURL") else None
    try:
        with active_curl(configured, host=os.environ) as context:
            diagnostic = await tool.prepare_curl()
            assert diagnostic["status"] == "ready", diagnostic
            assert diagnostic["https_probe"] == "passed"
            # curl 与 curl.exe 都在 PowerShell 内解析；版本必须仍是选定的 OpenSSL 构建。
            for name in ("curl", "curl.exe"):
                outcome = await tool.execute("execute_command", {"command": f"{name} -q --version"})
                assert outcome["exit_code"] == 0, outcome["stderr"]
                assert openssl_curl_version(outcome["stdout"]) == diagnostic["ssl_backend"]
            assert context.readable_directories
    finally:
        active_codex_executable.reset(binding)
