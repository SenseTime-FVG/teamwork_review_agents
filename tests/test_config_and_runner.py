"""配置解析和 Codex 命令边界测试。"""

import sys
from pathlib import Path

from teamwork_review_agents.codex_runner import CodexRunner
from teamwork_review_agents.cli import build_parser
from teamwork_review_agents.config import load_config, validate_runtime_files
from teamwork_review_agents.models import InvocationContext


def test_cli_uses_root_config_by_default() -> None:
    args = build_parser().parse_args(["validate"])
    assert args.config == Path("config.yaml")


def test_cli_allows_config_override() -> None:
    args = build_parser().parse_args(["serve", "-c", "custom.yaml"])
    assert args.config == Path("custom.yaml")


def test_example_config_is_valid() -> None:
    root = Path(__file__).resolve().parents[1]
    config = load_config(root / "config_example.yaml")
    assert validate_runtime_files(config) == []
    assert config.providers == {}
    assert config.repositories == []
    assert config.agents == {}
    assert config.rules == []
    assert config.database.path.is_absolute()


def test_runner_scrubs_provider_tokens(monkeypatch, configured_app_factory) -> None:
    config = configured_app_factory()
    monkeypatch.setenv("GITHUB_TOKEN", "不应进入 Codex")
    monkeypatch.setenv("GITLAB_TOKEN", "也不应进入 Codex")
    monkeypatch.setenv("CODEX_API_KEY", "Codex 自身凭据")
    environment = CodexRunner(config).child_environment()
    assert "GITHUB_TOKEN" not in environment
    assert "GITLAB_TOKEN" not in environment
    assert environment["CODEX_API_KEY"] == "Codex 自身凭据"


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
    assert "--ignore-user-config" in command
    assert "enabled_tools=[\"invoke_agent\"]" in joined
    assert command[-1] == "-"


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
