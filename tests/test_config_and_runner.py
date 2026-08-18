"""配置解析和 Codex 命令边界测试。"""

import asyncio
import os
import signal
import sys
from pathlib import Path

import pytest

from teamwork_review_agents.codex_runner import CodexRunner
from teamwork_review_agents.cli import _server_settings, build_parser
from teamwork_review_agents.config import (
    AgentConfig,
    CodexRuntimeConfig,
    RepositoryConfig,
    ScannerConfig,
    load_config,
    validate_runtime_files,
)
from teamwork_review_agents.models import InvocationContext
from teamwork_review_agents.executor import AgentExecutor
from teamwork_review_agents.state import StateStore
from teamwork_review_agents.process_manager import (
    ServiceLease,
    management_url,
    running_process,
    runtime_paths,
)


def test_cli_uses_root_config_by_default() -> None:
    args = build_parser().parse_args(["validate"])
    assert args.config == Path("config.yaml")


def test_cli_allows_config_override() -> None:
    args = build_parser().parse_args(["serve", "-c", "custom.yaml"])
    assert args.config == Path("custom.yaml")


def test_cli_exposes_foreground_and_background_commands() -> None:
    """前后台服务命令都应支持预期参数。"""

    for command in ("run", "start", "restart", "serve"):
        args = build_parser().parse_args(
            [command, "-c", "custom.yaml", "--host", "localhost", "--port", "9000"]
        )
        assert args.config == Path("custom.yaml")
        assert args.host == "localhost"
        assert args.port == 9000
    for command in ("stop", "end"):
        args = build_parser().parse_args([command, "-c", "custom.yaml"])
        assert args.config == Path("custom.yaml")


def test_service_lease_prevents_duplicate_processes(tmp_path) -> None:
    """同一个配置文件同一时刻只能登记一个服务进程。"""

    config_path = tmp_path / "config.yaml"
    paths = runtime_paths(config_path)
    assert paths.pid_file.name == "teamwork-review-agents.pid"
    assert paths.log_file.name == "teamwork-review-agents.log"

    lease = ServiceLease.acquire(
        config_path,
        host="127.0.0.1",
        port=8080,
        detached=False,
    )
    assert lease is not None
    try:
        record = running_process(config_path)
        assert record is not None
        assert record.pid == lease.record.pid
        assert ServiceLease.acquire(
            config_path,
            host="127.0.0.1",
            port=8081,
            detached=False,
        ) is None
    finally:
        lease.release()
    assert running_process(config_path) is None


def test_management_url_uses_local_openable_address() -> None:
    """通配监听地址应展示为浏览器可以访问的本机地址。"""

    assert management_url("0.0.0.0", 8080) == "http://127.0.0.1:8080"
    assert management_url("::1", 8080) == "http://[::1]:8080"


def test_server_settings_reject_invalid_port(tmp_path, capsys) -> None:
    """命令行覆盖端口也必须遵守有效端口范围。"""

    config_path = tmp_path / "config.yaml"
    config_path.write_text("database:\n  path: ./state.db\n", encoding="utf-8")
    assert _server_settings(config_path, None, 0) is None
    assert "监听端口必须在 1 到 65535 之间" in capsys.readouterr().out


def test_example_config_is_valid() -> None:
    root = Path(__file__).resolve().parents[1]
    config = load_config(root / "config_example.yaml")
    assert validate_runtime_files(config) == []
    assert config.providers == {}
    assert config.repositories == []
    assert set(config.agents) == {
        "general-reviewer",
        "incremental-doc-update-runner",
        "incremental-doc-updater",
    }
    assert all(
        agent.prompt_file is not None and agent.prompt_file.is_file()
        for agent in config.agents.values()
    )
    assert config.agents[
        "incremental-doc-update-runner"
    ].allowed_sub_agents == ["incremental-doc-updater"]
    assert [rule.name for rule in config.rules] == [
        "general-review",
        "增量文档更新",
    ]
    assert all(not rule.enabled for rule in config.rules)
    assert config.rules[0].deduplicate_per_scan is True
    assert config.rules[1].inherit_workspace is True
    assert config.database.path.is_absolute()
    assert config.scanner.interval_seconds == 300
    assert config.scanner.max_items_per_repository == 100
    assert config.scanner.api_page_size == 50
    assert config.runtime.worktree_retention_days == 7
    assert config.runtime.git_timeout_seconds == 600
    assert config.runtime.repository_initialization_timeout_seconds == 1800
    assert config.runtime.codex.fast_mode == "inherit"
    for agent_name in (
        "general-reviewer",
        "incremental-doc-update-runner",
        "incremental-doc-updater",
    ):
        assert config.agents[agent_name].network_access is True
        assert config.agents[agent_name].network_domains == []


def test_scanner_migrates_legacy_pagination_settings() -> None:
    """旧版页数配置应无损迁移为每轮数量上限。"""

    scanner = ScannerConfig.model_validate({"max_pages": 3, "page_size": 20})
    assert scanner.max_items_per_repository == 60
    assert scanner.api_page_size == 20


def test_repository_accepts_project_path_ssh_and_https() -> None:
    """远端仓库输入应统一转换为平台 API 使用的项目路径。"""

    inputs = {
        "SenseTime-FVG/test": "SenseTime-FVG/test",
        "git@github.com:SenseTime-FVG/test.git": "SenseTime-FVG/test",
        "https://github.com/SenseTime-FVG/test.git": "SenseTime-FVG/test",
        "ssh://git@gitlab.example.com/group/subgroup/test.git": "group/subgroup/test",
    }
    for value, expected in inputs.items():
        repository = RepositoryConfig(
            id="test",
            provider="provider-main",
            project=value,
            workspace=Path("/tmp/test-workspace"),
        )
        assert repository.project == expected
        assert repository.clone_url == (
            value if value.startswith(("git@", "http", "ssh://")) else None
        )


def test_runner_scrubs_provider_tokens(monkeypatch, configured_app_factory) -> None:
    config = configured_app_factory()
    monkeypatch.setenv("GITHUB_TOKEN", "不应进入 Codex")
    monkeypatch.setenv("GITLAB_TOKEN", "也不应进入 Codex")
    monkeypatch.setenv("CODEX_API_KEY", "Codex 自身凭据")
    monkeypatch.setenv("HOME", "/tmp/gh-keychain-home")
    environment = CodexRunner(config).child_environment(
        {
            "GITHUB_TOKEN": "Agent 环境也不能重新注入",
            "VISIBLE_AGENT_VALUE": "允许进入 Codex",
        }
    )
    assert "GITHUB_TOKEN" not in environment
    assert "GITLAB_TOKEN" not in environment
    assert environment["CODEX_API_KEY"] == "Codex 自身凭据"
    assert environment["HOME"] == "/tmp/gh-keychain-home"
    assert environment["VISIBLE_AGENT_VALUE"] == "允许进入 Codex"


def test_agent_network_domains_are_normalized_and_validated() -> None:
    """域名白名单应去重规范化，并拒绝无法安全转换的值。"""

    agent = AgentConfig.model_validate(
        {
            "prompt": "测试",
            "sandbox": "workspace-write",
            "network_access": True,
            "network_domains": ["API.GitHub.com", "api.github.com", "*.GitHub.com"],
        }
    )
    assert agent.network_domains == ["api.github.com", "*.github.com"]

    invalid_domains = (
        "https://api.github.com",
        "api.github.com:443",
        "*",
        "github.com/path",
    )
    for domain in invalid_domains:
        with pytest.raises(ValueError, match="命令联网域名"):
            AgentConfig.model_validate(
                {
                    "prompt": "测试",
                    "sandbox": "workspace-write",
                    "network_access": True,
                    "network_domains": [domain],
                }
            )


def test_agent_network_access_rejects_unsupported_sandbox_combinations() -> None:
    """只读模式不能开放命令联网，完全访问也不能伪装成域名隔离。"""

    with pytest.raises(ValueError, match="read-only"):
        AgentConfig.model_validate(
            {"prompt": "测试", "sandbox": "read-only", "network_access": True}
        )
    with pytest.raises(ValueError, match="danger-full-access"):
        AgentConfig.model_validate(
            {
                "prompt": "测试",
                "sandbox": "danger-full-access",
                "network_access": True,
                "network_domains": ["api.github.com"],
            }
        )
    danger = AgentConfig.model_validate(
        {"prompt": "测试", "sandbox": "danger-full-access"}
    )
    assert danger.network_access is True


def test_runner_builds_agent_network_overrides(
    snapshot_factory,
    configured_app_factory,
) -> None:
    """Runner 应把命令联网和可选域名白名单转换为应用托管覆盖。"""

    config = configured_app_factory()
    agent = config.agents["code-reviewer"]
    agent.sandbox = "workspace-write"
    agent.network_access = True
    agent.network_domains = ["api.github.com", "*.github.com"]
    repository = config.repositories[0]
    snapshot = snapshot_factory(
        repository_id=repository.id,
        provider=repository.provider,
    )
    from teamwork_review_agents.events import detect_events

    event = detect_events(None, snapshot, emit_initial=True)[0]
    context = InvocationContext(
        config_path=str(config.config_path),
        current_agent="code-reviewer",
        run_id="run-network",
        root_run_id="run-network",
        event=event,
    )
    command = CodexRunner(config).build_command(agent, repository, context)
    overrides = [
        command[index + 1]
        for index, value in enumerate(command[:-1])
        if value == "--config"
    ]

    assert "sandbox_workspace_write.network_access=true" in overrides
    assert "features.network_proxy.enabled=true" in overrides
    assert (
        'features.network_proxy.domains={ "api.github.com" = "allow", '
        '"*.github.com" = "allow" }'
    ) in overrides

    agent.network_domains = []
    command = CodexRunner(config).build_command(agent, repository, context)
    overrides = [
        command[index + 1]
        for index, value in enumerate(command[:-1])
        if value == "--config"
    ]
    assert "sandbox_workspace_write.network_access=true" in overrides
    assert "features.network_proxy.enabled=false" in overrides

    agent.network_access = False
    command = CodexRunner(config).build_command(agent, repository, context)
    overrides = [
        command[index + 1]
        for index, value in enumerate(command[:-1])
        if value == "--config"
    ]
    assert "sandbox_workspace_write.network_access=false" in overrides
    assert "features.network_proxy.enabled=false" in overrides


def test_runner_enables_only_agent_gateway(snapshot_factory, configured_app_factory) -> None:
    config = configured_app_factory()
    repository = config.repositories[0]
    event_snapshot = snapshot_factory(repository_id=repository.id, provider=repository.provider)
    from teamwork_review_agents.events import detect_events

    event = detect_events(None, event_snapshot, emit_initial=True)[0]
    context = InvocationContext(
        config_path=str(config.config_path),
        current_agent="code-reviewer",
        run_id="run-1",
        root_run_id="run-1",
        event=event,
    )
    command = CodexRunner(config).build_command(
        config.agents["code-reviewer"],
        repository,
        context,
    )
    joined = " ".join(command)
    assert "--ignore-user-config" not in command
    assert "enabled_tools=[\"invoke_agent\"]" in joined
    assert command[-1] == "-"


def test_runner_disables_unapproved_user_mcp_servers(
    tmp_path,
    snapshot_factory,
    configured_app_factory,
) -> None:
    """后台默认应禁用用户 MCP，只保留显式白名单和 Teamwork 网关。"""

    config = configured_app_factory()
    home = tmp_path / "codex-home"
    home.mkdir()
    (home / "config.toml").write_text(
        """
[mcp_servers.node_repl]
command = "node"

[mcp_servers.internal_api]
command = "internal-api"
""".strip(),
        encoding="utf-8",
    )
    config.runtime.codex_home = home
    config.runtime.allowed_user_mcp_servers = ["internal_api"]
    repository = config.repositories[0]
    snapshot = snapshot_factory(
        repository_id=repository.id,
        provider=repository.provider,
    )
    from teamwork_review_agents.events import detect_events

    event = detect_events(None, snapshot, emit_initial=True)[0]
    context = InvocationContext(
        config_path=str(config.config_path),
        current_agent="code-reviewer",
        run_id="run-mcp-isolation",
        root_run_id="run-mcp-isolation",
        event=event,
    )
    command = CodexRunner(config).build_command(
        config.agents["code-reviewer"],
        repository,
        context,
    )
    overrides = [
        command[index + 1]
        for index, value in enumerate(command[:-1])
        if value == "--config"
    ]

    assert "mcp_servers.node_repl.enabled=false" in overrides
    assert "mcp_servers.internal_api.enabled=false" not in overrides
    assert "mcp_servers.teamwork_agent_gateway.enabled=true" in overrides


def test_runner_merges_runtime_and_agent_codex_options(
    snapshot_factory,
    configured_app_factory,
) -> None:
    """Agent 覆盖应排在 Teamwork 运行时默认之后。"""

    config = configured_app_factory()
    config.runtime.codex = CodexRuntimeConfig(
        model="gpt-runtime",
        model_reasoning_effort="medium",
        fast_mode="fast",
        model_verbosity="low",
        personality="friendly",
        web_search="cached",
        extra_config={"history.max_bytes": 1048576},
    )
    agent = config.agents["code-reviewer"]
    agent.model = "gpt-agent"
    agent.model_reasoning_effort = "high"
    agent.fast_mode = "standard"
    agent.web_search = "live"
    repository = config.repositories[0]
    snapshot = snapshot_factory(
        repository_id=repository.id,
        provider=repository.provider,
    )
    from teamwork_review_agents.events import detect_events

    event = detect_events(None, snapshot, emit_initial=True)[0]
    context = InvocationContext(
        config_path=str(config.config_path),
        current_agent="code-reviewer",
        run_id="run-options",
        root_run_id="run-options",
        event=event,
    )
    command = CodexRunner(config).build_command(agent, repository, context)
    overrides = [
        command[index + 1]
        for index, value in enumerate(command[:-1])
        if value == "--config"
    ]

    assert command[command.index("--model") + 1] == "gpt-agent"
    assert 'model="gpt-runtime"' in overrides
    assert overrides.index('model_reasoning_effort="medium"') < overrides.index(
        'model_reasoning_effort="high"'
    )
    assert overrides.index('service_tier="fast"') < overrides.index(
        'service_tier="default"'
    )
    assert overrides.index('web_search="cached"') < overrides.index(
        'web_search="live"'
    )
    assert "history.max_bytes=1048576" in overrides


def test_codex_advanced_config_protects_managed_keys() -> None:
    """高级配置不能绕过结构化字段或应用托管的 MCP 与安全配置。"""

    for key in (
        "model",
        "features.fast_mode",
        "service_tier",
        "approval_policy",
        "model_provider",
        "mcp_servers.untrusted.command",
        "sandbox_workspace_write",
        "features.network_proxy",
        "features.network_proxy.enabled",
        "shell_environment_policy.include_only",
        "skills.config",
    ):
        with pytest.raises(ValueError, match="不能覆盖"):
            CodexRuntimeConfig(extra_config={key: "blocked"})


def test_root_and_sub_agent_prompt_contexts_are_separated(
    snapshot_factory,
    configured_app_factory,
) -> None:
    """根 Agent 接收 MR 信息，sub-agent 只自动接收仓库与父任务。"""

    config = configured_app_factory()
    repository = config.repositories[0]
    snapshot = snapshot_factory(
        repository_id=repository.id,
        provider=repository.provider,
    )
    from teamwork_review_agents.events import detect_events

    event = detect_events(None, snapshot, emit_initial=True)[0]
    store = StateStore(config.database.path)
    store.initialize()
    executor = AgentExecutor(config, store)
    root_prompt = executor.build_prompt(
        agent_name="code-reviewer",
        event=event,
        repository=repository,
        task=None,
        extra_context=None,
        prompt_values={},
        change_ref="refs/teamwork/change-requests/7/head",
        actions=("change_request.reopened", "change_request.updated"),
    )
    child_prompt = executor.build_prompt(
        agent_name="security-reviewer",
        event=event,
        repository=repository,
        task="只检查依赖安全风险",
        extra_context={"focus": "dependencies"},
        prompt_values={},
        change_ref="refs/teamwork/change-requests/7/head",
        actions=(event.type,),
    )

    assert '"mr"' in root_prompt
    assert '"action": [' in root_prompt
    assert '"reopened"' in root_prompt
    assert '"old"' not in root_prompt
    assert '"changed_fields"' not in root_prompt
    assert '"repository"' in child_prompt
    assert '"delegated_task": "只检查依赖安全风险"' in child_prompt
    assert '"delegated_context"' in child_prompt
    assert '"mr"' not in child_prompt
    assert '"action"' not in child_prompt
    assert snapshot.title not in child_prompt


def test_timeline_event_prompt_uses_final_snapshot(
    snapshot_factory,
    configured_app_factory,
) -> None:
    """历史活动用于规则匹配，但根 Agent 输入必须使用扫描结束时的真值。"""

    from teamwork_review_agents.events import detect_activity_events
    from teamwork_review_agents.models import ChangeRequestActivity

    config = configured_app_factory()
    repository = config.repositories[0]
    expected_head = "b" * 40
    old = snapshot_factory(
        provider=repository.provider,
        repository_id=repository.id,
        state="opened",
        updated_at="2026-08-17T08:00:00Z",
    )
    current = snapshot_factory(
        provider=repository.provider,
        repository_id=repository.id,
        state="opened",
        head_sha=expected_head,
        updated_at="2026-08-17T08:05:00Z",
    )
    event = detect_activity_events(
        old,
        current,
        [
            ChangeRequestActivity(
                id="closed-1",
                type="closed",
                occurred_at="2026-08-17T08:01:00Z",
            )
        ],
    )[0]
    prompt = AgentExecutor(config, StateStore(config.database.path)).build_prompt(
        agent_name="code-reviewer",
        event=event,
        repository=repository,
        task=None,
        extra_context=None,
        prompt_values={},
        change_ref="refs/teamwork/change-requests/7/head",
        actions=(event.type,),
    )

    assert event.new.state == "closed"
    assert '"state": "opened"' in prompt
    assert f'"head_sha": "{expected_head}"' in prompt


def test_workspace_write_lock_uses_repository_source_branch(
    snapshot_factory,
    configured_app_factory,
) -> None:
    """不同源分支可以并行，同一源分支必须命中同一个写锁。"""

    config = configured_app_factory()
    config.agents["code-reviewer"].write_scopes = ["workspace"]
    repository = config.repositories[0]
    executor = AgentExecutor(config, StateStore(config.database.path))
    from teamwork_review_agents.events import detect_events

    first = detect_events(
        None,
        snapshot_factory(
            provider=repository.provider,
            repository_id=repository.id,
            source_branch="feature/first",
        ),
        emit_initial=True,
    )[0]
    same_branch = detect_events(
        None,
        snapshot_factory(
            provider=repository.provider,
            repository_id=repository.id,
            number=8,
            source_branch="feature/first",
        ),
        emit_initial=True,
    )[0]
    other_branch = detect_events(
        None,
        snapshot_factory(
            provider=repository.provider,
            repository_id=repository.id,
            number=9,
            source_branch="feature/other",
        ),
        emit_initial=True,
    )[0]

    first_keys = executor.lock_keys("code-reviewer", first, repository)
    assert first_keys == [
        "repository_branch:github-main:demo:feature/first"
    ]
    assert executor.lock_keys("code-reviewer", same_branch, repository) == first_keys
    assert executor.lock_keys("code-reviewer", other_branch, repository) != first_keys


async def test_runner_parses_jsonl_from_process(
    tmp_path,
    snapshot_factory,
    configured_app_factory,
) -> None:
    config = configured_app_factory()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    fake_codex = tmp_path / "fake-codex"
    fake_codex.write_text(
        f"""#!{sys.executable}
import json
import sys
sys.stdin.read()
items = [
    {{"type": "thread.started", "thread_id": "thread-test"}},
    {{"type": "item.completed", "item": {{"type": "agent_message", "text": "执行完成"}}}},
    {{"type": "turn.completed", "usage": {{"input_tokens": 10, "output_tokens": 2}}}},
]
for item in items:
    print(json.dumps(item, ensure_ascii=False), flush=True)
""",
        encoding="utf-8",
    )
    fake_codex.chmod(0o755)
    config.runtime.codex_binary = str(fake_codex)
    repository = config.repositories[0]
    repository.workspace = workspace
    agent = config.agents["code-reviewer"]
    agent.skip_git_repo_check = True
    event_snapshot = snapshot_factory(repository_id=repository.id, provider=repository.provider)
    from teamwork_review_agents.events import detect_events

    event = detect_events(None, event_snapshot, emit_initial=True)[0]
    context = InvocationContext(
        config_path=str(config.config_path),
        current_agent="code-reviewer",
        run_id="run-1",
        root_run_id="run-1",
        event=event,
    )
    result = await CodexRunner(config).run(
        run_id="run-1",
        root_run_id="run-1",
        parent_run_id=None,
        agent_name="code-reviewer",
        agent=agent,
        repository=repository,
        context=context,
        prompt="执行测试",
    )
    assert result.status == "completed"
    assert result.thread_id == "thread-test"
    assert result.final_message == "执行完成"
    assert result.usage == {"input_tokens": 10, "output_tokens": 2}


@pytest.mark.parametrize(
    ("cancel_after_checks", "expected_status", "expected_error"),
    [
        (None, "timed_out", "没有 stdout / JSONL 进展"),
        (2, "cancelled", "管理员取消"),
    ],
)
async def test_runner_stops_idle_or_cancelled_process_group(
    tmp_path,
    snapshot_factory,
    configured_app_factory,
    cancel_after_checks,
    expected_status,
    expected_error,
) -> None:
    """无进展超时和人工取消都必须结束仍在运行的 Codex 进程。"""

    config = configured_app_factory()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    fake_codex = tmp_path / "fake-codex"
    fake_codex.write_text(
        f"""#!{sys.executable}
import json
import sys
import time
sys.stdin.read()
print(json.dumps({{"type": "thread.started", "thread_id": "thread-wait"}}), flush=True)
time.sleep(30)
""",
        encoding="utf-8",
    )
    fake_codex.chmod(0o755)
    config.runtime.codex_binary = str(fake_codex)
    repository = config.repositories[0]
    repository.workspace = workspace
    agent = config.agents["code-reviewer"]
    agent.skip_git_repo_check = True
    agent.idle_timeout_seconds = 1
    snapshot = snapshot_factory(
        repository_id=repository.id,
        provider=repository.provider,
    )
    from teamwork_review_agents.events import detect_events

    event = detect_events(None, snapshot, emit_initial=True)[0]
    context = InvocationContext(
        config_path=str(config.config_path),
        current_agent="code-reviewer",
        run_id="run-stop",
        root_run_id="run-stop",
        event=event,
    )
    checks = 0

    async def cancellation_requested() -> bool:
        """在指定轮次模拟另一个进程写入 SQLite 的取消请求。"""

        nonlocal checks
        checks += 1
        return cancel_after_checks is not None and checks >= cancel_after_checks

    result = await CodexRunner(config).run(
        run_id="run-stop",
        root_run_id="run-stop",
        parent_run_id=None,
        agent_name="code-reviewer",
        agent=agent,
        repository=repository,
        context=context,
        prompt="执行等待测试",
        cancel_check=cancellation_requested,
    )

    assert result.status == expected_status
    assert expected_error in (result.error or "")


async def test_runner_parses_jsonl_larger_than_asyncio_default_limit(
    tmp_path,
    snapshot_factory,
    configured_app_factory,
) -> None:
    """包含大体积命令输出的单条 JSONL 不应被默认 64 KiB 限制截断。"""

    config = configured_app_factory()
    workspace = tmp_path / "workspace-large-jsonl"
    workspace.mkdir()
    fake_codex = tmp_path / "fake-codex-large-jsonl"
    fake_codex.write_text(
        f"""#!{sys.executable}
import json
import sys
sys.stdin.read()
message = "长" * (128 * 1024)
print(json.dumps({{"type": "item.completed", "item": {{"type": "agent_message", "text": message}}}}, ensure_ascii=False), flush=True)
""",
        encoding="utf-8",
    )
    fake_codex.chmod(0o755)
    config.runtime.codex_binary = str(fake_codex)
    repository = config.repositories[0]
    repository.workspace = workspace
    agent = config.agents["code-reviewer"]
    agent.skip_git_repo_check = True
    snapshot = snapshot_factory(
        repository_id=repository.id,
        provider=repository.provider,
    )
    from teamwork_review_agents.events import detect_events

    event = detect_events(None, snapshot, emit_initial=True)[0]
    context = InvocationContext(
        config_path=str(config.config_path),
        current_agent="code-reviewer",
        run_id="run-large-jsonl",
        root_run_id="run-large-jsonl",
        event=event,
    )

    result = await CodexRunner(config).run(
        run_id="run-large-jsonl",
        root_run_id="run-large-jsonl",
        parent_run_id=None,
        agent_name="code-reviewer",
        agent=agent,
        repository=repository,
        context=context,
        prompt="执行超长 JSONL 测试",
    )

    assert result.status == "completed"
    assert result.final_message == "长" * (128 * 1024)


async def test_runner_reports_stream_failure_and_terminates_process(
    tmp_path,
    snapshot_factory,
    configured_app_factory,
    monkeypatch,
) -> None:
    """流读取越界必须明确失败，并结束仍在写入的 Codex 进程。"""

    monkeypatch.setattr(
        "teamwork_review_agents.codex_runner._CODEX_STREAM_LIMIT_BYTES",
        1024,
    )
    config = configured_app_factory()
    workspace = tmp_path / "workspace-stream-failure"
    workspace.mkdir()
    fake_codex = tmp_path / "fake-codex-stream-failure"
    fake_codex.write_text(
        f"""#!{sys.executable}
import sys
import time
sys.stdin.read()
print("x" * 4096, flush=True)
time.sleep(30)
""",
        encoding="utf-8",
    )
    fake_codex.chmod(0o755)
    config.runtime.codex_binary = str(fake_codex)
    repository = config.repositories[0]
    repository.workspace = workspace
    agent = config.agents["code-reviewer"]
    agent.skip_git_repo_check = True
    snapshot = snapshot_factory(
        repository_id=repository.id,
        provider=repository.provider,
    )
    from teamwork_review_agents.events import detect_events

    event = detect_events(None, snapshot, emit_initial=True)[0]
    context = InvocationContext(
        config_path=str(config.config_path),
        current_agent="code-reviewer",
        run_id="run-stream-failure",
        root_run_id="run-stream-failure",
        event=event,
    )
    emitted: list[tuple[str, str]] = []

    async def capture_log(
        stream: str,
        event_type: str,
        payload: str | dict[str, object],
    ) -> None:
        """只记录流和事件类型，避免测试保存大体积输出。"""

        del payload
        emitted.append((stream, event_type))

    result = await asyncio.wait_for(
        CodexRunner(config).run(
            run_id="run-stream-failure",
            root_run_id="run-stream-failure",
            parent_run_id=None,
            agent_name="code-reviewer",
            agent=agent,
            repository=repository,
            context=context,
            prompt="执行流异常测试",
            log_callback=capture_log,
        ),
        timeout=5,
    )

    assert result.status == "failed"
    assert "读取 Codex stdout 失败" in (result.error or "")
    assert ("system", "run.stream_failed") in emitted


async def test_runner_does_not_wait_forever_for_inherited_stream_pipe(
    tmp_path,
    snapshot_factory,
    configured_app_factory,
    monkeypatch,
) -> None:
    """脱离进程组的后代持有管道时，超时运行仍必须有界结束。"""

    monkeypatch.setattr(
        "teamwork_review_agents.codex_runner._STREAM_DRAIN_TIMEOUT_SECONDS",
        0.2,
    )
    monkeypatch.setattr(
        "teamwork_review_agents.codex_runner._PROCESS_TERMINATE_GRACE_SECONDS",
        0.2,
    )
    monkeypatch.setattr(
        "teamwork_review_agents.codex_runner._PROCESS_KILL_GRACE_SECONDS",
        0.2,
    )
    monkeypatch.setattr(
        "teamwork_review_agents.codex_runner._STREAM_CANCEL_TIMEOUT_SECONDS",
        0.2,
    )
    config = configured_app_factory()
    workspace = tmp_path / "workspace-inherited-pipe"
    workspace.mkdir()
    descendant_pid_file = tmp_path / "descendant.pid"
    fake_codex = tmp_path / "fake-codex-inherited-pipe"
    fake_codex.write_text(
        f"""#!{sys.executable}
import json
import subprocess
import sys
import time
from pathlib import Path
sys.stdin.read()
child = subprocess.Popen(
    [sys.executable, "-c", "import time; time.sleep(30)"],
    stdout=sys.stdout,
    stderr=sys.stderr,
    start_new_session=True,
)
Path({str(descendant_pid_file)!r}).write_text(str(child.pid), encoding="utf-8")
print(json.dumps({{"type": "thread.started", "thread_id": "thread-pipe"}}), flush=True)
time.sleep(30)
""",
        encoding="utf-8",
    )
    fake_codex.chmod(0o755)
    config.runtime.codex_binary = str(fake_codex)
    repository = config.repositories[0]
    repository.workspace = workspace
    agent = config.agents["code-reviewer"]
    agent.skip_git_repo_check = True
    agent.idle_timeout_seconds = 1
    snapshot = snapshot_factory(
        repository_id=repository.id,
        provider=repository.provider,
    )
    from teamwork_review_agents.events import detect_events

    event = detect_events(None, snapshot, emit_initial=True)[0]
    context = InvocationContext(
        config_path=str(config.config_path),
        current_agent="code-reviewer",
        run_id="run-inherited-pipe",
        root_run_id="run-inherited-pipe",
        event=event,
    )
    emitted: list[str] = []

    async def capture_log(
        stream: str,
        event_type: str,
        payload: str | dict[str, object],
    ) -> None:
        """记录收尾诊断事件。"""

        del stream, payload
        emitted.append(event_type)

    try:
        result = await asyncio.wait_for(
            CodexRunner(config).run(
                run_id="run-inherited-pipe",
                root_run_id="run-inherited-pipe",
                parent_run_id=None,
                agent_name="code-reviewer",
                agent=agent,
                repository=repository,
                context=context,
                prompt="执行继承管道测试",
                log_callback=capture_log,
            ),
            timeout=5,
        )
    finally:
        if descendant_pid_file.exists():
            descendant_pid = int(descendant_pid_file.read_text(encoding="utf-8"))
            try:
                os.kill(descendant_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    assert result.status == "timed_out"
    assert "没有 stdout / JSONL 进展" in (result.error or "")
    assert "run.stream_drain_timed_out" in emitted
