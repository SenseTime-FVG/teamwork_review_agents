"""托管沙盒 MCP 文件桥接测试。"""

from __future__ import annotations

import os
import sys

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from teamwork_review_agents.codex_runner import encode_invocation_context
from teamwork_review_agents.events import detect_events
from teamwork_review_agents.mcp_bridge import ManagedMcpBroker, call_bridge
from teamwork_review_agents.models import InvocationContext


async def test_mcp_proxy_only_exposes_invoke_agent() -> None:
    """沙盒内代理应只暴露 invoke_agent，不直接装载业务配置。"""

    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "teamwork_review_agents.mcp_proxy"],
    )
    async with stdio_client(parameters) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize()
            tools = await session.list_tools()
    assert [tool.name for tool in tools.tools] == ["invoke_agent"]


async def test_mcp_bridge_delegates_validation_outside_sandbox(
    snapshot_factory,
    configured_app_factory,
) -> None:
    """文件通道应由外部 Broker 复用原有 sub-agent 白名单校验。"""

    config = configured_app_factory()
    repository = config.repositories[0]
    snapshot = snapshot_factory(
        repository_id=repository.id,
        provider=repository.provider,
    )
    event = detect_events(None, snapshot, emit_initial=True)[0]
    context = InvocationContext(
        config_path=str(config.config_path),
        current_agent="security-reviewer",
        run_id="run-bridge-parent",
        root_run_id="run-bridge-parent",
        call_chain=("security-reviewer",),
        event=event,
    )
    broker = await ManagedMcpBroker.start(
        run_id="run-bridge-parent",
        config_path=config.config_path,
        encoded_context=encode_invocation_context(context),
        base_environment=os.environ,
        response_timeout_seconds=5,
    )
    channel_path = broker.channel.directory
    try:
        with pytest.raises(RuntimeError, match="不允许调用"):
            await call_bridge(
                broker.channel,
                agent_name="code-reviewer",
                task="请进行审查",
                extra_context=None,
            )
    finally:
        await broker.close()

    assert not channel_path.exists()
