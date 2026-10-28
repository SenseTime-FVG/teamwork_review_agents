"""子 Agent 名单通过工具枚举展示，业务 Prompt 和服务端授权保持独立。"""

import pytest
from jsonschema import ValidationError, validate

from teamwork_review_agents import mcp_server
from teamwork_review_agents.events import detect_events
from teamwork_review_agents.model_tools import teamwork_function_tools
from teamwork_review_agents.models import InvocationContext
from teamwork_review_agents.sub_agent_tools import configured_sub_agent_names


def test_enum_uses_configured_names_and_excludes_missing_children(configured_app_factory):
    """自定义名称原样保留、顺序去重，删除的 Agent 不展示为可调用。"""

    config = configured_app_factory()
    parent = config.agents["code-reviewer"]
    name = "custom-文档&更新"
    config.agents[name] = config.agents["security-reviewer"].model_copy(deep=True)
    parent.allowed_sub_agents = [name, "missing", "security-reviewer", name]
    tools = teamwork_function_tools(allowed_sub_agents=configured_sub_agent_names(config, parent))
    tool = next(item for item in tools if item["name"] == "invoke_agent")
    assert tool["parameters"]["properties"]["agent_name"]["enum"] == [name, "security-reviewer"]
    validate({"agent_name": name, "task": "更新文档"}, tool["parameters"])
    with pytest.raises(ValidationError):
        validate({"agent_name": "missing", "task": "不应调用"}, tool["parameters"])
    assert "无需另外查询允许名单" in tool["description"]
    assert "仍会校验权限" in tool["description"]


def test_empty_enum_hides_invoke_tool():
    """无子 Agent 时不暴露可填写任意名称的工具。"""

    tools = teamwork_function_tools(allowed_sub_agents=[], allow_publish_comment=True)
    assert [tool["name"] for tool in tools] == ["wait_for_ci", "execute_command", "apply_patch", "publish_comment"]


async def test_mcp_catalog_refresh_does_not_mutate_registered_schema(configured_app_factory, snapshot_factory, monkeypatch):
    """同进程重复读目录不能串名单，授权删除后目录立即反映新配置。"""

    config = configured_app_factory()
    event = detect_events(None, snapshot_factory(provider="github-main"), emit_initial=True)[0]
    context = InvocationContext(config_path=str(config.config_path), current_agent="code-reviewer",
                                run_id="test", root_run_id="test", event=event)
    monkeypatch.setattr(mcp_server, "_load_invocation", lambda: (config, context))
    original = (await mcp_server.mcp.list_tools())[0]
    config.agents["code-reviewer"].allowed_sub_agents = []
    assert all(tool.name != "invoke_agent" for tool in await mcp_server.mcp.list_tools())
    config.agents["code-reviewer"].allowed_sub_agents = ["code-reviewer"]
    updated = (await mcp_server.mcp.list_tools())[0]
    assert updated.inputSchema["properties"]["agent_name"]["enum"] == ["code-reviewer"]
    assert original.inputSchema["properties"]["agent_name"]["enum"] == ["security-reviewer"]


async def test_missing_invocation_cannot_return_unrestricted_catalog(monkeypatch):
    """缺少调用身份时不能发布允许任意名称的工具目录。"""

    monkeypatch.delenv("TEAMWORK_CONFIG_PATH", raising=False)
    monkeypatch.delenv("TEAMWORK_INVOCATION_CONTEXT", raising=False)
    with pytest.raises(RuntimeError, match="缺少调用上下文"):
        await mcp_server.mcp.list_tools()
