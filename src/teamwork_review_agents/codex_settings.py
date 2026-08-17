"""Codex CLI 配置覆盖生成与本机运行能力检查。"""

from __future__ import annotations

import json
import os
import subprocess
import tomllib
from pathlib import Path
from typing import Any

from .config import AgentConfig, CodexConfigValue, CodexRuntimeConfig


def toml_value(value: CodexConfigValue) -> str:
    """把受支持的 Python 配置值编码为 Codex 接受的 TOML 值。"""

    return json.dumps(value, ensure_ascii=False)


def _common_overrides(
    settings: CodexRuntimeConfig | AgentConfig,
) -> list[str]:
    """生成运行时默认和 Agent 覆盖共用的 Codex 配置项。"""

    overrides: list[str] = []
    if settings.model_reasoning_effort:
        overrides.append(
            "model_reasoning_effort="
            f"{toml_value(settings.model_reasoning_effort)}"
        )
    if settings.fast_mode != "inherit":
        overrides.append(
            "service_tier="
            f"{toml_value('fast' if settings.fast_mode == 'fast' else 'default')}"
        )
    if settings.model_verbosity:
        overrides.append(
            f"model_verbosity={toml_value(settings.model_verbosity)}"
        )
    if settings.personality:
        overrides.append(f"personality={toml_value(settings.personality)}")
    if settings.web_search:
        overrides.append(f"web_search={toml_value(settings.web_search)}")
    return overrides


def runtime_overrides(settings: CodexRuntimeConfig) -> list[str]:
    """生成优先于 Codex 文件配置的 Teamwork 运行时默认。"""

    overrides: list[str] = []
    if settings.model:
        overrides.append(f"model={toml_value(settings.model)}")
    overrides.extend(_common_overrides(settings))
    overrides.extend(
        f"{key}={toml_value(value)}"
        for key, value in sorted(settings.extra_config.items())
    )
    return overrides


def agent_overrides(settings: AgentConfig) -> list[str]:
    """生成当前 Agent 对 Teamwork 运行时默认的显式覆盖。"""

    return _common_overrides(settings)


def codex_home() -> Path:
    """返回当前服务进程使用的 Codex 配置目录。"""

    configured = os.getenv("CODEX_HOME")
    return Path(configured).expanduser() if configured else Path.home() / ".codex"


def read_user_model() -> tuple[str | None, str | None, str]:
    """读取用户 Codex 配置中的顶层模型，并把失败作为展示信息返回。"""

    path = codex_home() / "config.toml"
    if not path.is_file():
        return None, None, str(path)
    try:
        with path.open("rb") as file:
            document = tomllib.load(file)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        return None, str(exc), str(path)
    model = document.get("model")
    return (str(model) if isinstance(model, str) and model.strip() else None), None, str(path)


def read_bundled_models(codex_binary: str) -> tuple[list[dict[str, Any]], str | None]:
    """从本机 Codex CLI 读取模型目录；失败时保留手工填写能力。"""

    try:
        result = subprocess.run(
            [codex_binary, "debug", "models", "--bundled"],
            check=True,
            capture_output=True,
            text=True,
            timeout=8,
        )
        document = json.loads(result.stdout)
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
        return [], str(exc)

    raw_models = document.get("models", []) if isinstance(document, dict) else []
    models: list[dict[str, Any]] = []
    for item in raw_models:
        if not isinstance(item, dict) or not item.get("slug"):
            continue
        reasoning = item.get("supported_reasoning_levels", [])
        levels = [
            str(level.get("effort"))
            for level in reasoning
            if isinstance(level, dict) and level.get("effort")
        ]
        speed_tiers = item.get("additional_speed_tiers", [])
        service_tiers = item.get("service_tiers", [])
        supports_fast = "fast" in speed_tiers or any(
            isinstance(tier, dict)
            and (
                tier.get("id") in {"fast", "priority"}
                or tier.get("name") == "Fast"
            )
            for tier in service_tiers
        )
        models.append(
            {
                "slug": str(item["slug"]),
                "display_name": str(item.get("display_name") or item["slug"]),
                "default_reasoning_level": item.get("default_reasoning_level"),
                "supported_reasoning_levels": levels,
                "supports_fast_mode": supports_fast,
            }
        )
    return models, None


def inspect_runtime_options(
    settings: CodexRuntimeConfig,
    codex_binary: str,
) -> dict[str, Any]:
    """组合模型目录与可验证的默认模型来源，供管理 UI 展示。"""

    models, catalog_error = read_bundled_models(codex_binary)
    user_model, user_error, user_config_path = read_user_model()
    if settings.model:
        inherited_model = {
            "value": settings.model,
            "source": "runtime",
            "label": f"继承 Teamwork 运行时默认（{settings.model}）",
        }
    elif user_model:
        inherited_model = {
            "value": user_model,
            "source": "user",
            "label": f"继承 Codex 用户配置（{user_model}）",
        }
    else:
        inherited_model = {
            "value": None,
            "source": "builtin",
            "label": "继承 Codex CLI / 账号默认（未配置固定模型）",
        }
    return {
        "models": models,
        "inherited_model": inherited_model,
        "user_model": user_model,
        "user_config_path": user_config_path,
        "catalog_error": catalog_error,
        "user_config_error": user_error,
    }
