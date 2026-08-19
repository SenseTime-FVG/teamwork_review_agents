"""运行在外层沙盒内的最小 Teamwork MCP 代理。"""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp.server import FastMCP, Settings

from .mcp_bridge import call_bridge, channel_from_environment


# Python 3.14 不会自动解析该泛型前向引用，显式重建可避免设置字段不完整。
Settings.model_rebuild(_types_namespace={"FastMCP": FastMCP})


mcp = FastMCP(
    "teamwork-agent-gateway-proxy",
    instructions=(
        "invoke_agent 请求只会经本次运行的临时通道交给 Teamwork 服务校验；"
        "代理本身不能读取运行配置、SQLite 或其他 Agent 工作区。"
    ),
)


@mcp.tool(
    name="invoke_agent",
    description="调用一个配置允许的 Codex CLI sub-agent，并等待其结构化结果。",
)
async def invoke_agent(
    agent_name: str,
    task: str,
    extra_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """通过本次运行的临时文件通道请求服务侧启动 sub-agent。"""

    return await call_bridge(
        channel_from_environment(),
        agent_name=agent_name,
        task=task,
        extra_context=extra_context,
    )


def main() -> None:
    """以 STDIO 传输启动沙盒内 MCP 代理。"""

    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
