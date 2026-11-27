"""审核模式的 CI 等待工具目录、旧上下文兼容和误调用边界。"""

from pathlib import Path

import pytest

from teamwork_review_agents import mcp_server
from teamwork_review_agents.codex_runner import CodexRunner, decode_invocation_context, encode_invocation_context
from teamwork_review_agents.environment import prompt_allows_ci_wait, render_prompt
from teamwork_review_agents.events import detect_events
from teamwork_review_agents.model_tools import ModelToolExecutor, teamwork_function_tools
from teamwork_review_agents.models import InvocationContext


@pytest.mark.parametrize("value,expected", [(None, False), ("false", False), ("true", True), (" TRUE ", True), ("1", False)])
def test_builtin_review_uses_same_mode_for_prompt_and_tools(value, expected):
    """真实内置模板的默认模式及显式真语义，与模型看到的操作模式保持一致。"""

    template = (Path(__file__).resolve().parents[1] / "prompts/general-review.md").read_text()
    values = {} if value is None else {"REVIEW_AUTO_MERGE": value}
    assert prompt_allows_ci_wait(template, values) is expected
    assert ("本轮操作模式：审核并自动合并" in render_prompt(template, values)) is expected


@pytest.mark.parametrize("template,expected", [
    ("请更新依赖并等待 CI。", True),
    ("文字 REVIEW_AUTO_MERGE 不代表审核开关。", True),
    ("{# {{ REVIEW_AUTO_MERGE }} #} 请更新依赖。", True),
    ("旧变量=${{REVIEW_AUTO_MERGE}}", False),
])
def test_ordinary_agents_keep_wait_tool_despite_global_review_setting(template, expected):
    """仅模板真实引用审核变量才裁剪，其他 Agent 不受全局同名变量干扰。"""

    assert prompt_allows_ci_wait(template, {"REVIEW_AUTO_MERGE": "false"}) is expected


@pytest.mark.parametrize("enabled", [False, True])
def test_function_catalog_and_cli_share_frozen_tool_policy(configured_app_factory, snapshot_factory, enabled):
    """函数目录、CLI 白名单和编码往返使用同一策略，评论工具保持可用。"""

    config = configured_app_factory()
    repository = config.repositories[0]
    agent = config.agents["code-reviewer"]
    agent.managed_comment = True
    event = detect_events(None, snapshot_factory(provider=repository.provider), emit_initial=True)[0]
    context = InvocationContext(
        config_path=str(config.config_path), current_agent="code-reviewer",
        run_id="visibility", root_run_id="visibility", event=event, allow_ci_wait=enabled,
    )
    restored = decode_invocation_context(encode_invocation_context(context))
    assert restored.allow_ci_wait is enabled
    names = [tool["name"] for tool in teamwork_function_tools(
        allowed_sub_agents=[], allow_publish_comment=True, allow_ci_wait=restored.allow_ci_wait,
    )]
    assert ("wait_for_ci" in names) is enabled
    assert "publish_comment" in names and "execute_command" in names
    command = CodexRunner(config).build_launch(agent, repository, restored).command
    tools_override = next(value for value in command if ".enabled_tools=" in value)
    assert ('"wait_for_ci"' in tools_override) is enabled
    assert '"publish_comment"' in tools_override
    legacy = context.model_dump(exclude={"allow_ci_wait"})
    assert InvocationContext.model_validate(legacy).allow_ci_wait is True


@pytest.mark.parametrize("mode", ["embedded", "mcp"])
async def test_hidden_tool_cannot_start_waiting(configured_app_factory, snapshot_factory, monkeypatch, mode):
    """旧模型误调用不可见工具时，在数据库或平台操作之前返回不可用。"""

    config = configured_app_factory()
    repository = config.repositories[0]
    event = detect_events(None, snapshot_factory(provider=repository.provider), emit_initial=True)[0]
    context = InvocationContext(
        config_path=str(config.config_path), current_agent="code-reviewer",
        run_id="hidden-wait", root_run_id="hidden-wait", event=event, allow_ci_wait=False,
    )
    if mode == "mcp":
        monkeypatch.setattr(mcp_server, "_load_invocation", lambda: (config, context))
        invoke = mcp_server.wait_for_ci
    else:
        executor = ModelToolExecutor(
            config=config, agent=config.agents["code-reviewer"], repository=repository,
            context=context, environment={}, managed_sandbox=False,
            cancel_check=None, progress_callback=lambda: None, invoke_agent_callback=None,
        )

        async def invoke(number, expected_head_sha):
            """通过实际函数分派入口验证等待工具没有副作用。"""

            return await executor.execute("wait_for_ci", {"number": number, "expected_head_sha": expected_head_sha})

    with pytest.raises(PermissionError, match="未提供 wait_for_ci"):
        await invoke(7, "a" * 40)
    assert not config.database.path.exists()
