"""上下文窗口分层继承、迁移与持久化回归。"""

import copy
import json

import pytest
import yaml
from pydantic import ValidationError

from teamwork_review_agents.config import AgentConfig, normalize_model_provider_document, parse_config_data
from teamwork_review_agents.config_manager import ConfigManager
from teamwork_review_agents.model_provider_runtime import effective_agent_config, resolve_model_plan, resolve_model_selection, resolve_model_snapshot


def _document():
    """主模型、回退和 Agent 分别使用可区分的 Provider 窗口。"""

    return {
        "database": {"path": "state.db"},
        "model_providers": {
            "a": {"display_name": "A", "driver": "openai_responses", "base_url": "https://a.example.test", "default_model": "gpt-a", "context_window_tokens": 100000},
            "b": {"display_name": "B", "driver": "openai_responses", "base_url": "https://b.example.test", "default_model": "gpt-b", "context_window_tokens": 200000},
        },
        "runtime": {"default_model": {"provider": "a", "context_window_tokens": 150000}, "default_model_fallbacks": [{"provider": "b", "model": "gpt-global-fallback"}]},
        "agents": {"inherited": {"prompt": "原任务"}, "explicit": {"prompt": "原任务", "model_provider": "b", "model_fallbacks": [{"provider": "a", "model": "gpt-agent-fallback"}]}},
    }


@pytest.mark.parametrize("agent,expected,source", [
    ({}, 150000, "global"),
    ({"context_window_tokens": 180000}, 180000, "agent"),
    ({"model_provider": "a"}, 100000, "provider:a"),
    ({"model_provider": "b"}, 200000, "provider:b"),
    ({"model_provider": "b", "model": "gpt-other"}, 200000, "provider:b"),
    ({"model_provider": "b", "context_window_tokens": 220000}, 220000, "agent"),
    ({"model_provider": "codex-cli"}, 272000, "system_default"),
])
def test_agent_window_inheritance(tmp_path, agent, expected, source):
    """显式 Provider 优先于全局窗口；Agent 显式数值始终优先。"""

    config = parse_config_data(_document(), tmp_path / "config.yaml")
    selected_agent = AgentConfig(prompt="原任务", **agent)
    selected = resolve_model_selection(config, selected_agent)
    assert (selected.context_window_tokens, selected.context_window_source) == (expected, source)
    assert effective_agent_config(config, selected_agent, selected).context_window_tokens == expected
    snapshot = resolve_model_snapshot(config, selected_agent)
    assert snapshot["context_window_tokens"] == expected
    assert snapshot["context_window_source"] == source


def test_all_fallbacks_inherit_their_own_provider(tmp_path):
    """全局、Agent 回退不借用前一个节点的窗口，显式覆盖随节点移动。"""

    raw = _document()
    config = parse_config_data(raw, tmp_path / "config.yaml")
    plan = resolve_model_plan(config, config.agents["explicit"])
    assert [item.context_window_tokens for item in plan.selections] == [200000, 100000, 150000, 200000]
    raw["runtime"]["default_model_fallbacks"][0]["context_window_tokens"] = 210000
    raw["agents"]["explicit"]["model_fallbacks"][0]["context_window_tokens"] = 110000
    config = parse_config_data(raw, tmp_path / "config.yaml")
    snapshot = resolve_model_snapshot(config, config.agents["explicit"])
    assert [item["context_window_tokens"] for item in snapshot["fallback_plan"]] == [200000, 110000, 150000, 210000]
    assert [item["context_window_source"] for item in snapshot["fallback_plan"]] == ["provider:b", "agent_fallback", "global", "global_fallback"]


def test_defaults_ignore_codex_cache_and_do_not_materialize(tmp_path):
    """不同驱动采用同一个系统默认；缓存不得覆盖 UI 中可见的预算。"""

    (tmp_path / "models_cache.json").write_text(json.dumps({"models": [{"slug": "gpt-cached", "context_window": 999999}]}), encoding="utf-8")
    raw = _document()
    raw["model_providers"]["a"].pop("context_window_tokens")
    raw["runtime"] = {"codex_home": str(tmp_path), "default_model": {"provider": "codex-cli", "model": "gpt-cached"}}
    original = copy.deepcopy(raw)
    config = parse_config_data(raw, tmp_path / "config.yaml")
    for agent in (AgentConfig(prompt="原任务"), AgentConfig(prompt="原任务", model_provider="a")):
        assert resolve_model_selection(config, agent).context_window_tokens == 272000
    assert raw == original
    assert config.runtime.default_model.context_window_tokens is None
    assert config.model_providers["a"].context_window_tokens is None


@pytest.mark.parametrize("path", ["provider", "global", "global_fallback", "agent", "agent_fallback"])
@pytest.mark.parametrize("value", [0, -1, 2047, 4608, 8192.5, True, "8192"])
def test_invalid_windows_fail_config_validation(tmp_path, path, value):
    """各入口一致拒绝非法数字及不足以容纳输出预留的窗口。"""

    raw = _document()
    nodes = {"provider": raw["model_providers"]["a"], "global": raw["runtime"]["default_model"], "global_fallback": raw["runtime"]["default_model_fallbacks"][0], "agent": raw["agents"]["explicit"], "agent_fallback": raw["agents"]["explicit"]["model_fallbacks"][0]}
    nodes[path]["context_window_tokens"] = value
    with pytest.raises(ValidationError):
        parse_config_data(raw, tmp_path / "config.yaml")


def test_old_windows_migrate_once_without_hidden_overrides(tmp_path):
    """旧配置迁移到表单可见节点；新字段含 null 优先，未启用覆盖归档保留。"""

    raw = _document()
    raw["model_providers"]["a"].pop("context_window_tokens")
    raw["runtime"]["default_model"].pop("context_window_tokens")
    raw["agents"]["explicit"]["context_window_tokens"] = None
    raw["runtime"]["context_compaction"] = {"default_context_window_tokens": 131072, "model_context_windows": {"a": {"gpt-a": 160000, "gpt-agent-fallback": 170000, "unused": 180000}, "b": {"gpt-b": 190000, "gpt-global-fallback": 210000}}}
    original = copy.deepcopy(raw)
    migrated = normalize_model_provider_document(raw)
    assert normalize_model_provider_document(migrated) == migrated
    assert raw == original
    settings = migrated["runtime"]["context_compaction"]
    assert settings == {"legacy_model_context_windows": {"a": {"unused": 180000}}}
    assert migrated["model_providers"]["a"]["context_window_tokens"] == 131072
    assert migrated["model_providers"]["b"]["context_window_tokens"] == 200000
    assert migrated["agents"]["explicit"]["context_window_tokens"] is None
    config = parse_config_data(migrated, tmp_path / "config.yaml")
    assert [item.context_window_tokens for item in resolve_model_plan(config, config.agents["explicit"]).selections] == [200000, 170000, 160000, 210000]


def test_save_clear_and_provider_change_keep_inheritance(tmp_path):
    """实际配置管理器保存留空字段后，重载和上游变更仍动态继承。"""

    raw = _document()
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    manager = ConfigManager(path)
    raw["runtime"]["default_model"]["context_window_tokens"] = None
    raw["agents"]["explicit"]["context_window_tokens"] = None
    manager.save(raw)
    manager = ConfigManager(path)
    saved = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert saved["runtime"]["default_model"]["context_window_tokens"] is None
    assert saved["agents"]["explicit"]["context_window_tokens"] is None
    provider = {**saved["model_providers"]["b"], "context_window_tokens": 240000}
    manager.save_model_provider(expected_revision=manager.config.revision, provider_id="b", provider=provider)
    assert resolve_model_selection(manager.config, manager.config.agents["explicit"]).context_window_tokens == 240000
    assert resolve_model_selection(manager.config, manager.config.agents["inherited"]).context_window_tokens == 100000


def test_explicit_large_windows_validate_against_actual_budget(tmp_path):
    """所有 Provider 已显式配置大窗口时，不用未生效的系统默认拒绝输出预留。"""

    config = parse_config_data({
        "database": {"path": "state.db"},
        "model_providers": {"codex-cli": {"driver": "codex_cli", "display_name": "Codex CLI", "context_window_tokens": 1000000}},
        "runtime": {"context_compaction": {"reserved_output_tokens": 300000}},
    }, tmp_path / "config.yaml")
    assert resolve_model_selection(config, AgentConfig(prompt="原任务")).context_window_tokens == 1000000
