"""向 Codex CLI 暴露配置化 sub-agent 调用工具。"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

from mcp.server.fastmcp.server import FastMCP, Settings

from .codex_runner import decode_invocation_context
from .config import load_config
from .executor import AgentExecutor, sub_agent_idempotency_key
from .managed_comments import ManagedCommentService
from .state import StateStore


# Python 3.14 不会自动解析该泛型前向引用，显式重建可避免设置字段不完整。
Settings.model_rebuild(_types_namespace={"FastMCP": FastMCP})


mcp = FastMCP(
    "teamwork-agent-gateway",
    instructions=(
        "invoke_agent 只能调用当前 Agent 配置白名单中的 sub-agent。"
        "publish_comment 只维护当前 Agent 在当前 PR/MR 源版本代次的顶层评论。"
        "委托内容必须具体、最小化；sub-agent 只自动复用当前仓库上下文，"
        "是否传递 MR / PR 信息由父 Agent 决定。"
    ),
)


# 一个父 Agent 的 MCP 进程内，共享目录或写源分支的委托必须串行。
_sub_agent_write_lock = asyncio.Lock()


def _load_invocation():
    """从受控环境加载并校验本次 MCP 调用上下文。"""

    config_path = os.environ.get("TEAMWORK_CONFIG_PATH")
    encoded_context = os.environ.get("TEAMWORK_INVOCATION_CONTEXT")
    if not config_path or not encoded_context:
        raise RuntimeError("MCP Server 缺少调用上下文环境变量")
    config = load_config(config_path)
    context = decode_invocation_context(encoded_context)
    if context.config_path != str(config.config_path):
        raise RuntimeError("调用上下文与 MCP 配置文件不一致")
    return config, context


@mcp.tool(
    name="invoke_agent",
    description="调用一个配置允许的 Codex CLI sub-agent，并等待其结构化结果。",
)
async def invoke_agent(
    agent_name: str,
    task: str,
    extra_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """校验父子关系与调用限额，然后启动独立 Codex CLI。"""

    config, context = _load_invocation()
    if context.current_agent not in config.agents:
        raise RuntimeError(f"父 Agent 不存在：{context.current_agent}")
    parent = config.agents[context.current_agent]
    if agent_name not in parent.allowed_sub_agents:
        raise PermissionError(
            f"Agent {context.current_agent} 不允许调用 sub-agent {agent_name}"
        )
    child_depth = context.depth + 1
    if child_depth > config.runtime.max_sub_agent_depth:
        raise RuntimeError(
            f"sub-agent 深度 {child_depth} 超过限制 {config.runtime.max_sub_agent_depth}"
        )
    if agent_name in context.call_chain:
        raise RuntimeError(
            f"检测到 Agent 调用环：{' -> '.join((*context.call_chain, agent_name))}"
        )
    child = config.agents[agent_name]

    store = StateStore(config.database.path)
    store.initialize()
    executor = AgentExecutor(config, store)
    execute_arguments = {
        "agent_name": agent_name,
        "event": context.event,
        "task": task,
        "extra_context": extra_context,
        "root_run_id": context.root_run_id,
        "parent_run_id": context.run_id,
        "depth": child_depth,
        "call_chain": context.call_chain,
        "inherit_workspace": context.inherit_workspace,
        "parent_workspace": (
            Path(context.active_workspace) if context.active_workspace else None
        ),
        "idempotency_key": sub_agent_idempotency_key(
            root_run_id=context.root_run_id,
            parent_run_id=context.run_id,
            agent_name=agent_name,
            event_id=context.event.id,
            task=task,
            extra_context=extra_context,
        ),
    }
    if context.inherit_workspace or "workspace" in child.write_scopes:
        async with _sub_agent_write_lock:
            result = await executor.execute(**execute_arguments)
    else:
        result = await executor.execute(**execute_arguments)
    if result is None:
        return {
            "status": "deduplicated",
            "agent_name": agent_name,
            "message": "相同委托已经运行或完成",
        }
    return {
        "status": result.status,
        "run_id": result.run_id,
        "agent_name": result.agent_name,
        "final_message": result.final_message,
        "usage": result.usage,
    }


@mcp.tool(
    name="publish_comment",
    description="发布或更新当前 Agent 在当前源版本代次的 PR/MR 顶层评论。",
)
async def publish_comment(body: str) -> dict[str, Any]:
    """只接受正文，其余目标信息全部来自服务签发的可信上下文。"""

    config, context = _load_invocation()
    store = StateStore(config.database.path)
    await asyncio.to_thread(store.initialize)
    return await ManagedCommentService(
        config,
        store,
    ).publish_agent_comment(context, body)


def main() -> None:
    """以 STDIO 传输启动 MCP Server。"""

    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
