"""托管沙盒 MCP 文件桥接测试。"""

from __future__ import annotations

import os
import sys

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from teamwork_review_agents.codex_runner import encode_invocation_context
from teamwork_review_agents.events import detect_events
from teamwork_review_agents.mcp_bridge import ManagedMcpBroker, _call_bridge_method, call_bridge
from teamwork_review_agents.models import InvocationContext


async def test_mcp_proxy_only_exposes_teamwork_gateway_tools(configured_app_factory, snapshot_factory) -> None:
    """沙盒代理从宿主读取授权目录，代理环境没有业务配置路径和调用身份。"""

    config = configured_app_factory()
    event = detect_events(None, snapshot_factory(provider="github-main"), emit_initial=True)[0]
    context = InvocationContext(config_path=str(config.config_path), current_agent="code-reviewer",
                                run_id="proxy-catalog", root_run_id="proxy-catalog", event=event)
    broker = await ManagedMcpBroker.start(
        run_id=context.run_id, config_path=config.config_path, encoded_context=encode_invocation_context(context),
        base_environment=os.environ, response_timeout_seconds=5,
    )
    try:
        parameters = StdioServerParameters(command=sys.executable, args=["-m", "teamwork_review_agents.mcp_proxy"],
                                           env=broker.channel.environment_overrides())
        async with stdio_client(parameters) as streams:
            async with ClientSession(*streams) as session:
                await session.initialize()
                tools = await session.list_tools()
    finally:
        await broker.close()
    assert [tool.name for tool in tools.tools] == [
        "invoke_agent",
        "publish_comment",
        "wait_for_ci",
    ]
    assert tools.tools[0].inputSchema["properties"]["agent_name"]["enum"] == ["security-reviewer"]


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
        # 目录查询只接受当前身份，不能通过参数伪装为权限更高的父 Agent。
        with pytest.raises(RuntimeError, match="不接受参数"):
            await _call_bridge_method(broker.channel, method="list_tools", params={"current_agent": "code-reviewer"})
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
