"""模型 Provider 选择、继承和运行快照解析。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .codex_settings import codex_home, read_user_inherited_settings, read_user_model
from .config import AgentConfig, AppConfig, ModelProviderConfig


class ModelProviderUnavailableError(RuntimeError):
    """表示有效模型 Provider 不存在、已停用或没有可解析模型。"""


@dataclass(frozen=True)
class ResolvedModelSelection:
    """一次 Agent 运行实际使用的 Provider 与模型。"""

    provider_id: str
    provider: ModelProviderConfig
    model: str | None
    model_source: str
    resolved_label: str
    unresolved_reason: str | None = None


def resolve_model_selection(
    config: AppConfig,
    agent: AgentConfig,
    *,
    require_enabled: bool = True,
) -> ResolvedModelSelection:
    """按 Agent、全局默认和 Provider 默认顺序解析有效模型。"""

    explicit_provider = bool(agent.model_provider)
    provider_id = agent.model_provider or config.runtime.default_model.provider
    provider = config.model_providers.get(provider_id)
    if provider is None:
        raise ModelProviderUnavailableError(f"模型 Provider 不存在：{provider_id}")
    if require_enabled and not provider.enabled:
        raise ModelProviderUnavailableError(
            f"模型 Provider 已停用：{provider.display_name}（{provider_id}）"
        )

    if agent.model:
        model = agent.model
        source = "agent"
    elif explicit_provider and provider.default_model:
        model = provider.default_model
        source = "provider"
    elif not explicit_provider and config.runtime.default_model.model:
        model = config.runtime.default_model.model
        source = "global"
    elif provider.default_model:
        model = provider.default_model
        source = "provider"
    elif provider.models:
        model = provider.models[0]
        source = "provider"
    else:
        model = None
        source = "provider_default"

    unresolved_reason: str | None = None
    if model is None and provider.driver == "codex_cli":
        user_model, error, _ = read_user_model(codex_home(config.runtime.codex_home))
        if user_model:
            model = user_model
            source = "codex_user"
        else:
            unresolved_reason = error or "Codex 配置和账号目录未给出具体默认模型"
    elif model is None:
        unresolved_reason = "Provider 未配置默认模型"

    concrete = model or f"暂未解析：{unresolved_reason}"
    if source == "agent":
        label = f"{provider.display_name} / {concrete}"
    elif explicit_provider:
        label = f"{provider.display_name} / Provider 默认模型（{concrete}）"
    else:
        label = f"继承全局默认（{provider.display_name} / {concrete}）"
    return ResolvedModelSelection(
        provider_id=provider_id,
        provider=provider,
        model=model,
        model_source=source,
        resolved_label=label,
        unresolved_reason=unresolved_reason,
    )


def effective_agent_config(
    config: AppConfig,
    agent: AgentConfig,
    selection: ResolvedModelSelection,
) -> AgentConfig:
    """把 Provider 和全局默认折叠为 Runner 可直接使用的 Agent 配置。"""

    provider = selection.provider
    updates: dict[str, Any] = {
        "model_provider": selection.provider_id,
        "model": selection.model,
    }
    if agent.model_reasoning_effort is None and provider.model_reasoning_effort:
        updates["model_reasoning_effort"] = provider.model_reasoning_effort
    if agent.model_verbosity is None and provider.model_verbosity:
        updates["model_verbosity"] = provider.model_verbosity
    if agent.personality is None and provider.personality:
        updates["personality"] = provider.personality
    return agent.model_copy(update=updates)


def resolve_model_snapshot(
    config: AppConfig,
    agent: AgentConfig,
    configured_home: Path | None = None,
) -> dict[str, Any]:
    """生成包含 Provider 身份和最终具体模型的运行快照。"""

    selection = resolve_model_selection(config, agent, require_enabled=False)
    effective_agent = effective_agent_config(config, agent, selection)
    provider = selection.provider
    inherited, _ = read_user_inherited_settings(codex_home(configured_home))

    def inherited_value(key: str) -> str | None:
        item = inherited.get(key)
        value = item.get("value") if isinstance(item, dict) else None
        return str(value) if value is not None else None

    if effective_agent.model_reasoning_effort:
        reasoning = effective_agent.model_reasoning_effort
        reasoning_source = (
            "agent" if agent.model_reasoning_effort else "provider"
        )
    elif provider.driver == "codex_cli":
        reasoning = (
            config.runtime.codex.model_reasoning_effort
            or inherited_value("model_reasoning_effort")
        )
        reasoning_source = (
            "runtime"
            if config.runtime.codex.model_reasoning_effort
            else "codex_user"
            if reasoning is not None
            else "codex_default"
        )
    else:
        reasoning = None
        reasoning_source = "provider_default"

    if agent.fast_mode != "inherit":
        fast_mode = agent.fast_mode
        fast_source = "agent"
    elif provider.driver == "codex_cli":
        fast_mode = config.runtime.codex.fast_mode
        if fast_mode == "inherit":
            fast_mode = inherited_value("fast_mode") or "standard"
            fast_source = (
                "codex_user"
                if inherited_value("fast_mode") is not None
                else "codex_default"
            )
        else:
            fast_source = "runtime"
    else:
        fast_mode = "standard"
        fast_source = "provider_default"

    if effective_agent.model_verbosity:
        verbosity = effective_agent.model_verbosity
        verbosity_source = "agent" if agent.model_verbosity else "provider"
    elif provider.driver == "codex_cli":
        verbosity = (
            config.runtime.codex.model_verbosity
            or inherited_value("model_verbosity")
        )
        verbosity_source = (
            "runtime"
            if config.runtime.codex.model_verbosity
            else "codex_user"
            if verbosity is not None
            else "codex_default"
        )
    else:
        verbosity = None
        verbosity_source = "provider_default"

    return {
        "execution_mode": (
            config.runtime.codex.execution_mode
            if provider.driver == "codex_cli"
            else "model"
        ),
        "provider_id": selection.provider_id,
        "provider_name": provider.display_name,
        "provider_driver": provider.driver,
        "provider_enabled": provider.enabled,
        "model": selection.model,
        "model_source": selection.model_source,
        "resolved_label": selection.resolved_label,
        "unresolved_reason": selection.unresolved_reason,
        "reasoning_effort": reasoning,
        "reasoning_effort_source": reasoning_source,
        "fast_mode": fast_mode,
        "fast_mode_source": fast_source,
        "verbosity": verbosity,
        "verbosity_source": verbosity_source,
    }
