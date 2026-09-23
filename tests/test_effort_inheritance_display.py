"""验证只读 effort 诊断与基座运行器默认一致，不使用真实 CLI 或账号。"""

from types import SimpleNamespace

import pytest

from teamwork_review_agents import codex_settings
from teamwork_review_agents.codex_model_runner import CodexModelRunner
from teamwork_review_agents.config import AgentConfig, CodexRuntimeConfig


@pytest.mark.parametrize("user_effort, expected, source", [
    (None, "medium", "builtin"), ("low", "low", "user"), ("high", "high", "user"),
])
def test_model_base_diagnostic_matches_runner(
    tmp_path, monkeypatch, configured_app_factory, user_effort, expected, source,
):
    """CLI 有效配置即使不同，也不能冒充基座实际采用的默认 effort。"""

    if user_effort:
        (tmp_path / "config.toml").write_text(
            f'model_reasoning_effort = "{user_effort}"\n', encoding="utf-8",
        )
    monkeypatch.setattr(codex_settings, "inspect_codex_binary", lambda *_: {})
    monkeypatch.setattr(codex_settings, "inspect_managed_sandbox", lambda *_: SimpleNamespace(as_dict=lambda: {}))
    result = codex_settings.inspect_runtime_options(
        CodexRuntimeConfig(), "unused-codex", tmp_path,
        effective_config={"model_reasoning_effort": "xhigh", "private": "不能返回"},
        live_models=[],
    )
    assert result["model_base_reasoning_effort"] == {"value": expected, "source": source, "known": True}
    assert result["inherited_settings"]["model_reasoning_effort"]["value"] == "xhigh"
    assert "不能返回" not in str(result)
    config = configured_app_factory()
    config.runtime.codex_home = tmp_path
    config.runtime.codex.model_reasoning_effort = None
    provider = config.model_providers["codex-cli"]
    provider.model_reasoning_effort = None
    actual = CodexModelRunner(config)._settings_for_provider(
        AgentConfig(model="gpt-test", prompt="测试默认参数"), provider, codex_model_base=True,
    )
    assert actual[1] == result["model_base_reasoning_effort"]["value"]


def test_invalid_user_config_keeps_effort_unknown(tmp_path, monkeypatch):
    """无法解析用户配置时不把缺失值显示为确定的默认值。"""

    (tmp_path / "config.toml").write_text("[invalid", encoding="utf-8")
    monkeypatch.setattr(codex_settings, "inspect_codex_binary", lambda *_: {})
    monkeypatch.setattr(codex_settings, "inspect_managed_sandbox", lambda *_: SimpleNamespace(as_dict=lambda: {}))
    result = codex_settings.inspect_runtime_options(CodexRuntimeConfig(), "unused", tmp_path, live_models=[])
    assert result["model_base_reasoning_effort"] == {"value": None, "source": "unknown", "known": False}
