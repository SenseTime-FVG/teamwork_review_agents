"""Teamwork 跨平台外层沙盒测试。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from teamwork_review_agents.codex_runner import CodexRunner
from teamwork_review_agents.config import AgentConfig
from teamwork_review_agents.events import detect_events
from teamwork_review_agents.managed_sandbox import (
    ManagedSandboxInspection,
    _inspect_cached,
    _platform_backend,
    inspect_managed_sandbox,
    permission_profile_override,
    wrap_managed_sandbox_command,
)
from teamwork_review_agents.mcp_bridge import McpBridgeChannel
from teamwork_review_agents.models import InvocationContext


def test_managed_sandbox_profiles_cover_files_and_network() -> None:
    """权限档案应同时表达文件写边界与三种网络策略。"""

    read_only = AgentConfig(prompt="测试", sandbox="read-only")
    read_only_profile = permission_profile_override(read_only)
    assert 'extends=":read-only"' in read_only_profile
    assert "network={enabled=false}" in read_only_profile

    restricted = AgentConfig(
        prompt="测试",
        sandbox="workspace-write",
        network_access=True,
        network_domains=["api.github.com", "*.github.com"],
    )
    restricted_profile = permission_profile_override(restricted)
    assert 'extends=":workspace"' in restricted_profile
    assert 'filesystem={":workspace_roots"={".git"="write"}}' in restricted_profile
    assert 'mode="limited"' in restricted_profile
    assert '"api.github.com"="allow"' in restricted_profile
    assert '"*.github.com"="allow"' in restricted_profile

    restricted.network_domains = []
    assert 'network={enabled=true,mode="full"}' in permission_profile_override(
        restricted
    )


def test_managed_sandbox_profile_only_adds_the_run_ipc_directory(
    tmp_path: Path,
) -> None:
    """MCP Bridge 只能为当前随机通道增加一个精确的写例外。"""

    ipc_directory = tmp_path / "mcp-channel"
    ipc_directory.mkdir()
    agent = AgentConfig(prompt="测试", sandbox="read-only")

    profile = permission_profile_override(agent, ipc_directory=ipc_directory)

    assert f'{json.dumps(str(ipc_directory.resolve()))}="write"' in profile
    assert "TEAMWORK_CONFIG_PATH" not in profile


def test_managed_sandbox_wrapper_separates_outer_and_inner_arguments(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """外层命令应固定当前工作区，并显式隔开内层 Codex 参数。"""

    monkeypatch.setattr(sys, "platform", "darwin")
    agent = AgentConfig(
        prompt="测试",
        sandbox="workspace-write",
        network_access=True,
        network_domains=["api.github.com"],
    )
    inner = ["codex", "exec", "--json", "-"]

    command = wrap_managed_sandbox_command(
        codex_binary="codex",
        workspace=tmp_path,
        agent=agent,
        inner_command=inner,
        environment={"SSH_AUTH_SOCK": "/tmp/test-ssh-agent.sock"},
    )

    separator = command.index("--")
    assert command[:2] == ["codex", "sandbox"]
    assert command[command.index("--cd") + 1] == str(tmp_path)
    assert command[command.index("--allow-unix-socket") + 1] == (
        "/tmp/test-ssh-agent.sock"
    )
    assert "features.network_proxy=true" in command[:separator]
    assert command[separator + 1 :] == inner


def test_runner_uses_sandbox_proxy_without_exposing_service_paths(
    tmp_path: Path,
    snapshot_factory,
    configured_app_factory,
) -> None:
    """托管命令只向代理传临时通道，不得传配置路径与调用上下文。"""

    config = configured_app_factory()
    repository = config.repositories[0]
    agent = config.agents["code-reviewer"]
    snapshot = snapshot_factory(
        repository_id=repository.id,
        provider=repository.provider,
    )
    event = detect_events(None, snapshot, emit_initial=True)[0]
    context = InvocationContext(
        config_path=str(config.config_path),
        current_agent="code-reviewer",
        run_id="run-bridge-command",
        root_run_id="run-bridge-command",
        event=event,
    )
    channel_directory = tmp_path / "channel"
    channel_directory.mkdir()
    bridge = McpBridgeChannel(
        directory=channel_directory,
        token="test-channel-token",
        response_timeout_seconds=30,
    )

    command = CodexRunner(config).build_command(
        agent,
        repository,
        context,
        managed_sandbox=True,
        mcp_bridge=bridge,
    )
    joined = " ".join(command)

    assert "teamwork_review_agents.mcp_proxy" in joined
    assert "TEAMWORK_MCP_CHANNEL_DIR" in joined
    assert "TEAMWORK_MCP_CHANNEL_TOKEN" in joined
    assert "TEAMWORK_CONFIG_PATH" not in joined
    assert "TEAMWORK_INVOCATION_CONTEXT" not in joined
    assert str(config.config_path) not in joined
    assert f'{json.dumps(str(channel_directory.resolve()))}="write"' in joined


def test_managed_command_without_bridge_still_never_exposes_service_paths(
    snapshot_factory,
    configured_app_factory,
) -> None:
    """意外缺少通道时命令也只能得到不可用的最小代理。"""

    config = configured_app_factory()
    repository = config.repositories[0]
    agent = config.agents["code-reviewer"]
    snapshot = snapshot_factory(
        repository_id=repository.id,
        provider=repository.provider,
    )
    event = detect_events(None, snapshot, emit_initial=True)[0]
    context = InvocationContext(
        config_path=str(config.config_path),
        current_agent="code-reviewer",
        run_id="run-missing-bridge-command",
        root_run_id="run-missing-bridge-command",
        event=event,
    )

    command = CodexRunner(config).build_command(
        agent,
        repository,
        context,
        managed_sandbox=True,
        mcp_bridge=None,
    )
    joined = " ".join(command)

    assert "teamwork_review_agents.mcp_proxy" in joined
    assert "teamwork_review_agents.mcp_server" not in joined
    assert "TEAMWORK_CONFIG_PATH" not in joined
    assert "TEAMWORK_INVOCATION_CONTEXT" not in joined
    assert str(config.config_path) not in joined


def test_managed_sandbox_only_enables_proxy_for_domain_allowlist(
    tmp_path: Path,
) -> None:
    """普通联网与禁网不应无意义地启动域名代理。"""

    agent = AgentConfig(
        prompt="测试",
        sandbox="workspace-write",
        network_access=True,
    )
    command = wrap_managed_sandbox_command(
        codex_binary="codex",
        workspace=tmp_path,
        agent=agent,
        inner_command=["codex", "exec", "-"],
        environment={},
    )
    assert "features.network_proxy=true" not in command

    agent.network_access = False
    command = wrap_managed_sandbox_command(
        codex_binary="codex",
        workspace=tmp_path,
        agent=agent,
        inner_command=["codex", "exec", "-"],
        environment={},
    )
    assert "features.network_proxy=true" not in command


@pytest.mark.parametrize(
    ("platform_value", "wsl_name", "expected"),
    [
        ("darwin", None, ("macOS", "seatbelt")),
        ("linux", "Ubuntu", ("WSL", "linux")),
        ("win32", None, ("Windows", "windows")),
    ],
)
def test_managed_sandbox_detects_supported_platforms(
    platform_value: str,
    wsl_name: str | None,
    expected: tuple[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """macOS、WSL 与原生 Windows 应映射到对应原生后端。"""

    monkeypatch.setattr(sys, "platform", platform_value)
    if wsl_name is None:
        monkeypatch.delenv("WSL_DISTRO_NAME", raising=False)
    else:
        monkeypatch.setenv("WSL_DISTRO_NAME", wsl_name)
    assert _platform_backend() == expected


def test_managed_sandbox_detects_linux(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """普通 Linux 不应被误判为 WSL。"""

    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.delenv("WSL_DISTRO_NAME", raising=False)
    monkeypatch.setattr(Path, "read_text", lambda *args, **kwargs: "6.8.0-linux")
    assert _platform_backend() == ("Linux", "linux")


def test_managed_sandbox_inspection_checks_codex_capability(
    tmp_path: Path,
) -> None:
    """能力探测应要求当前 Codex 暴露命名权限档案参数。"""

    fake_codex = tmp_path / "fake-codex-sandbox"
    fake_codex.write_text(
        f"""#!{sys.executable}
import sys
if sys.argv[1:] == ["sandbox", "--help"]:
    print("--permission-profile PROFILE")
    raise SystemExit(0)
raise SystemExit(2)
""",
        encoding="utf-8",
    )
    fake_codex.chmod(0o755)
    _inspect_cached.cache_clear()

    inspection = inspect_managed_sandbox(str(fake_codex))

    assert inspection.available is True
    assert inspection.backend is not None


async def test_runner_fails_closed_before_starting_codex(
    snapshot_factory,
    configured_app_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """外层能力不可用时默认应在模型进程启动前失败。"""

    config = configured_app_factory()
    repository = config.repositories[0]
    agent = config.agents["code-reviewer"]
    agent.skip_git_repo_check = True
    snapshot = snapshot_factory(
        repository_id=repository.id,
        provider=repository.provider,
    )
    event = detect_events(None, snapshot, emit_initial=True)[0]
    context = InvocationContext(
        config_path=str(config.config_path),
        current_agent="code-reviewer",
        run_id="run-sandbox-fail-closed",
        root_run_id="run-sandbox-fail-closed",
        event=event,
    )
    emitted: list[str] = []

    monkeypatch.setattr(
        "teamwork_review_agents.codex_runner.inspect_managed_sandbox",
        lambda *args, **kwargs: ManagedSandboxInspection(
            available=False,
            platform="测试平台",
            backend=None,
            error="外层沙盒不可用",
        ),
    )

    async def capture_log(stream: str, event_type: str, payload: object) -> None:
        """只记录事件类型。"""

        del stream, payload
        emitted.append(event_type)

    result = await CodexRunner(config).run(
        run_id="run-sandbox-fail-closed",
        root_run_id="run-sandbox-fail-closed",
        parent_run_id=None,
        agent_name="code-reviewer",
        agent=agent,
        repository=repository,
        context=context,
        prompt="测试",
        log_callback=capture_log,
    )

    assert result.status == "failed"
    assert result.error == "外层沙盒不可用"
    assert "run.sandbox_unavailable" in emitted


async def test_runner_can_fall_back_to_codex_internal_sandbox(
    tmp_path: Path,
    snapshot_factory,
    configured_app_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """关闭失败即阻断后，只能回退到同等级的 Codex 内层沙盒。"""

    fake_codex = tmp_path / "fake-codex-fallback"
    fake_codex.write_text(
        f"""#!{sys.executable}
import json
import sys
sys.stdin.read()
print(json.dumps({{"type": "item.completed", "item": {{"type": "agent_message", "text": "安全回退"}}}}, ensure_ascii=False), flush=True)
""",
        encoding="utf-8",
    )
    fake_codex.chmod(0o755)
    config = configured_app_factory()
    config.runtime.codex_binary = str(fake_codex)
    config.runtime.managed_sandbox.fail_closed = False
    repository = config.repositories[0]
    agent = config.agents["code-reviewer"]
    agent.skip_git_repo_check = True
    snapshot = snapshot_factory(
        repository_id=repository.id,
        provider=repository.provider,
    )
    event = detect_events(None, snapshot, emit_initial=True)[0]
    context = InvocationContext(
        config_path=str(config.config_path),
        current_agent="code-reviewer",
        run_id="run-sandbox-fallback",
        root_run_id="run-sandbox-fallback",
        event=event,
    )
    emitted: list[str] = []

    monkeypatch.setattr(
        "teamwork_review_agents.codex_runner.inspect_managed_sandbox",
        lambda *args, **kwargs: ManagedSandboxInspection(
            available=False,
            platform="测试平台",
            backend=None,
            error="外层沙盒不可用",
        ),
    )

    async def capture_log(stream: str, event_type: str, payload: object) -> None:
        """只记录事件类型。"""

        del stream, payload
        emitted.append(event_type)

    result = await CodexRunner(config).run(
        run_id="run-sandbox-fallback",
        root_run_id="run-sandbox-fallback",
        parent_run_id=None,
        agent_name="code-reviewer",
        agent=agent,
        repository=repository,
        context=context,
        prompt="测试",
        log_callback=capture_log,
    )

    assert result.status == "completed"
    assert result.final_message == "安全回退"
    assert "run.sandbox_fallback" in emitted
