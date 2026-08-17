"""MCP STDIO 协议握手测试。"""

import sys
from pathlib import Path

import pytest

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from teamwork_review_agents.codex_runner import encode_invocation_context
from teamwork_review_agents.config import load_config
from teamwork_review_agents.events import detect_events
from teamwork_review_agents.mcp_server import invoke_agent
from teamwork_review_agents.models import InvocationContext


async def test_mcp_server_lists_invoke_agent() -> None:
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "teamwork_review_agents.mcp_server"],
    )
    async with stdio_client(parameters) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize()
            tools = await session.list_tools()
    assert [tool.name for tool in tools.tools] == ["invoke_agent"]


async def test_mcp_rejects_agent_outside_allowlist(monkeypatch, snapshot_factory) -> None:
    root = Path(__file__).resolve().parents[1]
    config = load_config(root / "config_example.yaml")
    repository = config.repositories[0]
    snapshot = snapshot_factory(repository_id=repository.id, provider=repository.provider)
    event = detect_events(None, snapshot, emit_initial=True)[0]
    context = InvocationContext(
        config_path=str(config.config_path),
        current_agent="security-reviewer",
        run_id="run-parent",
        root_run_id="run-parent",
        call_chain=("security-reviewer",),
        event=event,
    )
    monkeypatch.setenv("TEAMWORK_CONFIG_PATH", str(config.config_path))
    monkeypatch.setenv(
        "TEAMWORK_INVOCATION_CONTEXT",
        encode_invocation_context(context),
    )
    with pytest.raises(PermissionError, match="不允许调用"):
        await invoke_agent("code-reviewer", "请进行审查")
