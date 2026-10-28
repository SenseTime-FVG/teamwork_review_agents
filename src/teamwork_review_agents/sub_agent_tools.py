"""共享子 Agent 工具目录，明确展示配置授权而不替代调用时校验。"""

from collections.abc import Sequence
from typing import Any

from .config import AgentConfig, AppConfig


INVOKE_AGENT_DESCRIPTION = (
    "调用当前 Agent 配置允许的 sub-agent，并等待结构化结果。"
    "agent_name 的枚举由后台根据当前配置生成，其中的 Agent 均已配置且获准调用，"
    "无需另外查询允许名单。只在任务确实需要委托时使用；"
    "调用时后台仍会校验权限、调用环、深度与运行额度。"
)


def configured_sub_agent_names(config: AppConfig, agent: AgentConfig) -> list[str]:
    """只展示父 Agent 获准且仍存在的名称，按配置顺序去重。"""

    return list(dict.fromkeys(name for name in agent.allowed_sub_agents if name in config.agents))


def sub_agent_name_parameter(names: Sequence[str]) -> dict[str, Any]:
    """候选为空时由调用方隐藏工具，避免提供空枚举或任意字符串。"""

    if not names:
        raise ValueError("没有可展示的子 Agent")
    return {
        "type": "string",
        "enum": list(dict.fromkeys(names)),
        "description": "从后台提供的允许名单中选择子 Agent 名称，必须与枚举值完全一致。",
    }
