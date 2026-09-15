"""以隔离、禁用 site-packages 的 Python 验证独立 MCP 协议和文件通道。"""

import asyncio
import json
import os
import sys

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from teamwork_review_agents.mcp_bridge import McpBridgeChannel, _atomic_write_json
from teamwork_review_agents.sandbox_mcp import standalone_mcp_command


pytestmark = pytest.mark.usefixtures("verified_test_sandbox_python")


async def test_standalone_proxy_preserves_real_broker_authority(configured_app_factory, snapshot_factory):
    """新解释器代理不能绕过宿主 Broker 的 sub-agent 白名单。"""

    from teamwork_review_agents.codex_runner import encode_invocation_context
    from teamwork_review_agents.events import detect_events
    from teamwork_review_agents.mcp_bridge import ManagedMcpBroker
    from teamwork_review_agents.models import InvocationContext

    config = configured_app_factory()
    repository = config.repositories[0]
    event = detect_events(None, snapshot_factory(repository_id=repository.id, provider=repository.provider), emit_initial=True)[0]
    context = InvocationContext(config_path=str(config.config_path), current_agent="security-reviewer",
                                run_id="standalone-authority", root_run_id="standalone-authority",
                                call_chain=("security-reviewer",), event=event)
    broker = await ManagedMcpBroker.start(run_id=context.run_id, config_path=config.config_path,
                                        encoded_context=encode_invocation_context(context),
                                        base_environment=os.environ, response_timeout_seconds=5)
    command = standalone_mcp_command()
    try:
        async with stdio_client(StdioServerParameters(command=command[0], args=command[1:], env=broker.channel.environment_overrides())) as streams:
            async with ClientSession(*streams) as session:
                await session.initialize()
                result = await session.call_tool("invoke_agent", {"agent_name": "code-reviewer", "task": "不应执行"})
                assert result.isError
                assert "不允许调用" in result.content[0].text
    finally:
        await broker.close()
    assert not broker.channel.directory.exists()


async def _request(channel):
    """有界等待代理真正提交请求，避免错误测试永久挂起。"""

    async with asyncio.timeout(10):
        while not (paths := list(channel.requests_directory.glob("*.request.json"))):
            await asyncio.sleep(0.02)
    path = paths[0]
    payload = json.loads(path.read_text(encoding="utf-8"))
    path.unlink()
    return payload


async def test_standalone_proxy_has_no_site_dependency_and_keeps_protocol():
    """真实 SDK 握手、两种工具、错误与结构化结果保持兼容，不安装项目依赖。"""

    channel = McpBridgeChannel.create("standalone-test", response_timeout_seconds=5)
    command = standalone_mcp_command()
    assert command[1:3] == ["-I", "-S"]
    assert len(" ".join(command)) < 12000
    parameters = StdioServerParameters(command=command[0], args=command[1:], env=channel.environment_overrides())
    try:
        async with stdio_client(parameters) as streams:
            async with ClientSession(*streams) as session:
                await session.initialize()
                assert [item.name for item in (await session.list_tools()).tools] == ["invoke_agent", "publish_comment", "wait_for_ci"]
                await session.send_ping()
                for name, arguments in [("invoke_agent", {"agent_name": "child", "task": "检查中文"}), ("publish_comment", {"body": "评论"}), ("wait_for_ci", {"number": 12, "expected_head_sha": "a" * 40})]:
                    pending = asyncio.create_task(session.call_tool(name, arguments))
                    request = await _request(channel)
                    assert request["method"] == name
                    assert request["token"] == channel.token
                    _atomic_write_json(channel.responses_directory / f"{request['request_id']}.response.json", {
                        **request, "ok": True, "result": {"status": "completed", "text": "成功"},
                    })
                    response = await pending
                    assert not response.isError
                    assert response.structuredContent == {"status": "completed", "text": "成功"}
                    assert json.loads(response.content[0].text) == response.structuredContent
                invalid = await session.call_tool("publish_comment", {"body": "x", "other": "forbidden"})
                assert invalid.isError
                assert not list(channel.requests_directory.iterdir())
                pending = asyncio.create_task(session.call_tool("publish_comment", {"body": "x"}))
                request = await _request(channel)
                _atomic_write_json(channel.responses_directory / f"{request['request_id']}.response.json", {
                    **request, "ok": False, "error": {"message": "拒绝调用 " + channel.token},
                })
                failed = await pending
                assert failed.isError
                assert channel.token not in failed.content[0].text
    finally:
        channel.cleanup()


@pytest.mark.parametrize("stop", ["notification", "eof", "timeout"])
async def test_standalone_proxy_cancellation_reaches_broker(stop):
    """客户端取消、关闭输入和等待超时都必须通知宿主 Broker。"""

    channel = McpBridgeChannel.create("standalone-cancel", response_timeout_seconds=0.2 if stop == "timeout" else 10)
    command = standalone_mcp_command()
    from teamwork_review_agents.process_control import process_group_options

    process = await asyncio.create_subprocess_exec(
        *command, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        env={**os.environ, **channel.environment_overrides()}, **process_group_options(),
    )
    try:
        process.stdin.write((json.dumps({"jsonrpc": "2.0", "id": 10, "method": "tools/call", "params": {"name": "publish_comment", "arguments": {"body": "x"}}}) + "\n").encode())
        await process.stdin.drain()
        request = await _request(channel)
        if stop == "notification":
            process.stdin.write(b'{"jsonrpc":"2.0","method":"notifications/cancelled","params":{"requestId":10}}\n')
            await process.stdin.drain()
        elif stop == "eof":
            process.stdin.close()
        async with asyncio.timeout(5):
            marker = channel.cancellations_directory / f"{request['request_id']}.cancel.json"
            while not marker.exists():
                await asyncio.sleep(0.02)
        cancellation = json.loads(marker.read_text())
        assert cancellation["token"] == channel.token
        assert cancellation["version"] == 1
        if stop == "timeout":
            response = json.loads(await asyncio.wait_for(process.stdout.readline(), timeout=3))
            assert response["result"]["isError"]
    finally:
        process.stdin.close()
        await asyncio.wait_for(process.wait(), timeout=5)
        channel.cleanup()
