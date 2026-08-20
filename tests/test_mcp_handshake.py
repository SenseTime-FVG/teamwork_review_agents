"""MCP STDIO 协议握手测试。"""

import sys

import pytest

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from teamwork_review_agents.codex_runner import encode_invocation_context
from teamwork_review_agents.events import detect_events
from teamwork_review_agents.models import AgentResult
from teamwork_review_agents import mcp_server as mcp_server_module
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
    assert [tool.name for tool in tools.tools] == [
        "invoke_agent",
        "publish_comment",
    ]


async def test_mcp_rejects_agent_outside_allowlist(
    monkeypatch,
    snapshot_factory,
    configured_app_factory,
) -> None:
    config = configured_app_factory()
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


async def test_mcp_propagates_shared_workspace_policy(
    monkeypatch,
    snapshot_factory,
    configured_app_factory,
) -> None:
    """开启继承时，MCP 调用应把父 Agent 当前目录传给 sub-agent。"""

    config = configured_app_factory()
    repository = config.repositories[0]
    snapshot = snapshot_factory(repository_id=repository.id, provider=repository.provider)
    event = detect_events(None, snapshot, emit_initial=True)[0]
    context = InvocationContext(
        config_path=str(config.config_path),
        current_agent="code-reviewer",
        run_id="run-parent",
        root_run_id="run-parent",
        call_chain=("code-reviewer",),
        inherit_workspace=True,
        active_workspace=str(repository.workspace),
        event=event,
    )
    captured: dict[str, object] = {}

    class FakeExecutor:
        """只记录 MCP 传给执行器的参数。"""

        def __init__(self, *_args, **_kwargs) -> None:
            pass

        async def execute(self, **kwargs):
            captured.update(kwargs)
            return AgentResult(
                run_id="run-child",
                root_run_id="run-parent",
                parent_run_id="run-parent",
                agent_name="security-reviewer",
                status="completed",
            )

    monkeypatch.setattr(mcp_server_module, "AgentExecutor", FakeExecutor)
    monkeypatch.setenv("TEAMWORK_CONFIG_PATH", str(config.config_path))
    monkeypatch.setenv(
        "TEAMWORK_INVOCATION_CONTEXT",
        encode_invocation_context(context),
    )

    result = await invoke_agent("security-reviewer", "检查当前分支中的依赖")

    assert result["status"] == "completed"
    assert captured["inherit_workspace"] is True
    assert captured["parent_workspace"] == repository.workspace
