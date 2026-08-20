"""Codex CLI 配置覆盖生成与本机运行能力检查。"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tomllib
from pathlib import Path
from typing import Any

from .subprocess_utils import resolve_executable

from .config import (
    AgentConfig,
    CodexConfigValue,
    CodexRuntimeConfig,
    ManagedSandboxConfig,
)
from .managed_sandbox import inspect_managed_sandbox


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


def agent_network_overrides(settings: AgentConfig) -> list[str]:
    """生成 workspace-write 沙箱的命令联网与域名代理覆盖。"""

    if settings.sandbox != "workspace-write":
        return []
    overrides = [
        "sandbox_workspace_write.network_access="
        f"{toml_value(settings.network_access)}",
    ]
    if not settings.network_access or not settings.network_domains:
        overrides.append("features.network_proxy.enabled=false")
        return overrides
    domains = ", ".join(
        f"{toml_value(domain)} = \"allow\""
        for domain in settings.network_domains
    )
    overrides.extend(
        [
            "features.network_proxy.enabled=true",
            f"features.network_proxy.domains={{ {domains} }}",
        ]
    )
    return overrides


def codex_home(configured: Path | None = None) -> Path:
    """返回后台运行实际使用的 Codex 配置目录。"""

    if configured is not None:
        return configured.expanduser().resolve()
    configured = os.getenv("CODEX_HOME")
    return Path(configured).expanduser() if configured else Path.home() / ".codex"


def _read_user_config(home: Path) -> tuple[dict[str, Any], str | None, str]:
    """读取 Codex 用户配置，失败时返回空配置和可展示错误。"""

    path = home / "config.toml"
    if not path.is_file():
        return {}, None, str(path)
    try:
        with path.open("rb") as file:
            document = tomllib.load(file)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        return {}, str(exc), str(path)
    return document, None, str(path)


def read_user_model(home: Path | None = None) -> tuple[str | None, str | None, str]:
    """读取用户 Codex 配置中的顶层模型，并把失败作为展示信息返回。"""

    document, error, path = _read_user_config(home or codex_home())
    if error:
        return None, error, path
    model = document.get("model")
    return (str(model) if isinstance(model, str) and model.strip() else None), None, path


def read_user_mcp_servers(home: Path | None = None) -> tuple[list[str], str | None]:
    """返回 Codex 用户配置中声明的 MCP Server 名称。"""

    document, error, _ = _read_user_config(home or codex_home())
    if error:
        return [], error
    servers = document.get("mcp_servers", {})
    if not isinstance(servers, dict):
        return [], None
    return sorted(str(name) for name in servers), None


def _inherited_setting(
    value: str | None,
    source: str,
    *,
    known: bool,
) -> dict[str, Any]:
    """构造可安全返回给管理界面的单项继承诊断。"""

    return {
        "value": value,
        "source": source,
        "known": known,
    }


def _inherited_settings_from_document(
    document: dict[str, Any],
    *,
    configured_source: str,
    allow_builtin_defaults: bool,
) -> dict[str, dict[str, Any]]:
    """从已完成分层的配置中裁剪允许展示的继承字段。"""

    def configured_string(key: str) -> str | None:
        value = document.get(key)
        return value.strip() if isinstance(value, str) and value.strip() else None

    reasoning = configured_string("model_reasoning_effort")
    verbosity = configured_string("model_verbosity")
    personality = configured_string("personality")
    service_tier = configured_string("service_tier")
    web_search = configured_string("web_search")

    if service_tier in {"fast", "priority"}:
        fast_mode = _inherited_setting("fast", configured_source, known=True)
    elif service_tier == "default":
        fast_mode = _inherited_setting("standard", configured_source, known=True)
    elif service_tier:
        fast_mode = _inherited_setting(service_tier, configured_source, known=True)
    elif allow_builtin_defaults:
        fast_mode = _inherited_setting("standard", "builtin", known=True)
    else:
        fast_mode = _inherited_setting(None, "unknown", known=False)

    if not web_search:
        features = document.get("features")
        if isinstance(features, dict) and features.get("web_search_request") is True:
            web_search = "live"
        elif isinstance(features, dict) and features.get("web_search_cached") is True:
            web_search = "cached"

    if web_search:
        inherited_web_search = _inherited_setting(
            web_search,
            configured_source,
            known=True,
        )
    elif allow_builtin_defaults:
        inherited_web_search = _inherited_setting(
            "cached",
            "builtin",
            known=True,
        )
    else:
        inherited_web_search = _inherited_setting(None, "unknown", known=False)

    return {
        "model_reasoning_effort": _inherited_setting(
            reasoning,
            configured_source if reasoning else "unknown",
            known=bool(reasoning),
        ),
        "fast_mode": fast_mode,
        "model_verbosity": _inherited_setting(
            verbosity,
            configured_source if verbosity else "unknown",
            known=bool(verbosity),
        ),
        "personality": _inherited_setting(
            personality,
            configured_source if personality else "unknown",
            known=bool(personality),
        ),
        "web_search": inherited_web_search,
    }


def read_user_inherited_settings(
    home: Path | None = None,
) -> tuple[dict[str, dict[str, Any]], str | None]:
    """读取允许展示的 Codex 用户默认值，不返回其他配置内容。"""

    document, error, _ = _read_user_config(home or codex_home())
    if error:
        unknown = _inherited_setting(None, "unknown", known=False)
        return {
            "model_reasoning_effort": dict(unknown),
            "fast_mode": dict(unknown),
            "model_verbosity": dict(unknown),
            "personality": dict(unknown),
            "web_search": dict(unknown),
        }, error
    return _inherited_settings_from_document(
        document,
        configured_source="user",
        allow_builtin_defaults=False,
    ), None


def resolve_agent_model_snapshot(
    runtime: CodexRuntimeConfig,
    agent: AgentConfig,
    configured_home: Path | None = None,
) -> dict[str, Any]:
    """按实际继承顺序返回可持久化的 Agent 模型设置快照。"""

    home = codex_home(configured_home)
    user_model, _, _ = read_user_model(home)
    inherited, _ = read_user_inherited_settings(home)

    if agent.model:
        model = agent.model
        model_source = "agent"
    elif runtime.model:
        model = runtime.model
        model_source = "runtime"
    elif user_model:
        model = user_model
        model_source = "codex_user"
    else:
        model = None
        model_source = "codex_default"

    def inherited_value(key: str) -> str | None:
        item = inherited.get(key)
        value = item.get("value") if isinstance(item, dict) else None
        return str(value) if value is not None else None

    if agent.model_reasoning_effort:
        reasoning_effort = agent.model_reasoning_effort
        reasoning_effort_source = "agent"
    elif runtime.model_reasoning_effort:
        reasoning_effort = runtime.model_reasoning_effort
        reasoning_effort_source = "runtime"
    else:
        reasoning_effort = inherited_value("model_reasoning_effort")
        reasoning_effort_source = (
            "codex_user" if reasoning_effort is not None else "codex_default"
        )

    if agent.fast_mode != "inherit":
        fast_mode = agent.fast_mode
        fast_mode_source = "agent"
    elif runtime.fast_mode != "inherit":
        fast_mode = runtime.fast_mode
        fast_mode_source = "runtime"
    else:
        fast_mode = inherited_value("fast_mode") or "standard"
        fast_mode_source = (
            "codex_user"
            if inherited_value("fast_mode") is not None
            else "codex_default"
        )

    if agent.model_verbosity:
        verbosity = agent.model_verbosity
        verbosity_source = "agent"
    elif runtime.model_verbosity:
        verbosity = runtime.model_verbosity
        verbosity_source = "runtime"
    else:
        verbosity = inherited_value("model_verbosity")
        verbosity_source = (
            "codex_user" if verbosity is not None else "codex_default"
        )

    return {
        "execution_mode": runtime.execution_mode,
        "model": model,
        "model_source": model_source,
        "reasoning_effort": reasoning_effort,
        "reasoning_effort_source": reasoning_effort_source,
        "fast_mode": fast_mode,
        "fast_mode_source": fast_mode_source,
        "verbosity": verbosity,
        "verbosity_source": verbosity_source,
    }


def _codex_environment(home: Path | None) -> dict[str, str]:
    """为诊断命令构造与后台 Agent 一致的 Codex Home 环境。"""

    environment = dict(os.environ)
    if home is not None:
        environment["CODEX_HOME"] = str(home)
    return environment


def inspect_codex_binary(
    codex_binary: str,
    home: Path | None = None,
) -> dict[str, str | None]:
    """解析 Codex CLI 路径和版本，不执行任何 Agent 任务。"""

    environment = _codex_environment(home)
    command = resolve_executable(codex_binary, environment)
    resolved = command if Path(command).is_file() else None
    try:
        result = subprocess.run(
            [command, "--version"],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            env=environment,
        )
        output = (result.stdout or result.stderr).strip()
    except (OSError, subprocess.SubprocessError) as exc:
        return {
            "resolved_path": resolved,
            "version": None,
            "version_output": None,
            "error": str(exc),
        }
    match = re.search(r"(?<!\d)(\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.-]+)?)", output)
    return {
        "resolved_path": resolved,
        "version": match.group(1) if match else None,
        "version_output": output,
        "error": None,
    }


def validate_codex_version(
    codex_binary: str,
    expected_version: str | None,
    home: Path | None = None,
) -> str | None:
    """验证后台实际 Codex CLI 版本，返回阻断执行的错误。"""

    if not expected_version:
        return None
    inspection = inspect_codex_binary(codex_binary, home)
    if inspection["error"]:
        return f"无法检查 Codex CLI 版本：{inspection['error']}"
    actual = inspection["version"]
    if actual != expected_version:
        return f"Codex CLI 版本不匹配：期望 {expected_version}，实际 {actual or '无法识别'}"
    return None


def inspect_model_cache(home: Path) -> dict[str, Any]:
    """读取模型缓存记录的客户端版本，仅用于诊断版本竞争。"""

    path = home / "models_cache.json"
    if not path.is_file():
        return {"path": str(path), "client_version": None, "error": None}
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"path": str(path), "client_version": None, "error": str(exc)}
    version = document.get("client_version") if isinstance(document, dict) else None
    return {
        "path": str(path),
        "client_version": str(version) if version else None,
        "error": None,
    }


def _normalize_model_catalog(
    raw_models: Any,
    *,
    visible_only: bool = False,
) -> list[dict[str, Any]]:
    """把 Codex 的账号缓存或内置目录收敛为管理界面需要的字段。"""

    if not isinstance(raw_models, list):
        return []
    models: list[dict[str, Any]] = []
    for item in raw_models:
        if not isinstance(item, dict) or not item.get("slug"):
            continue
        if visible_only and item.get("visibility") != "list":
            continue
        reasoning = item.get("supported_reasoning_levels", [])
        levels = (
            [
                str(level.get("effort") if isinstance(level, dict) else level)
                for level in reasoning
                if (isinstance(level, dict) and level.get("effort"))
                or (isinstance(level, str) and level)
            ]
            if isinstance(reasoning, list)
            else []
        )
        speed_tiers = item.get("additional_speed_tiers", [])
        service_tiers = item.get("service_tiers", [])
        supports_fast = (isinstance(speed_tiers, list) and "fast" in speed_tiers) or (
            isinstance(service_tiers, list)
            and any(
                isinstance(tier, dict)
                and (
                    tier.get("id") in {"fast", "priority"}
                    or tier.get("name") == "Fast"
                )
                for tier in service_tiers
            )
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
    return models


def read_account_models(
    home: Path,
) -> tuple[list[dict[str, Any]], str | None]:
    """读取当前 Codex Home 的账号可见模型；缓存缺失不是错误。"""

    path = home / "models_cache.json"
    if not path.is_file():
        return [], None
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [], str(exc)
    if not isinstance(document, dict):
        return [], "模型缓存根节点不是对象"
    return _normalize_model_catalog(
        document.get("models", []),
        visible_only=True,
    ), None


def read_bundled_models(
    codex_binary: str,
    home: Path | None = None,
) -> tuple[list[dict[str, Any]], str | None]:
    """从本机 Codex CLI 读取模型目录；失败时保留手工填写能力。"""

    environment = _codex_environment(home)
    command = resolve_executable(codex_binary, environment)
    try:
        result = subprocess.run(
            [command, "debug", "models", "--bundled"],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=8,
            env=environment,
        )
        document = json.loads(result.stdout)
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
        return [], str(exc)

    raw_models = document.get("models", []) if isinstance(document, dict) else []
    return _normalize_model_catalog(raw_models), None


def inspect_runtime_options(
    settings: CodexRuntimeConfig,
    codex_binary: str,
    configured_home: Path | None = None,
    expected_version: str | None = None,
    effective_config: dict[str, Any] | None = None,
    effective_config_error: str | None = None,
    managed_sandbox: ManagedSandboxConfig | None = None,
) -> dict[str, Any]:
    """组合模型目录与可验证的默认模型来源，供管理 UI 展示。"""

    home = codex_home(configured_home)
    models, account_catalog_error = read_account_models(home)
    if models:
        catalog_source = "account_cache"
        catalog_error = None
    else:
        models, catalog_error = read_bundled_models(codex_binary, home)
        catalog_source = "bundled" if catalog_error is None else "unavailable"
        if catalog_error and account_catalog_error:
            catalog_error = (
                f"账号模型缓存：{account_catalog_error}；"
                f"Codex CLI 内置目录：{catalog_error}"
            )
    user_model, user_error, user_config_path = read_user_model(home)
    binary = inspect_codex_binary(codex_binary, home)
    cache = inspect_model_cache(home)
    user_mcp_servers, user_mcp_error = read_user_mcp_servers(home)
    sandbox_settings = managed_sandbox or ManagedSandboxConfig()
    sandbox_inspection = inspect_managed_sandbox(codex_binary, configured_home)
    if effective_config is not None:
        inherited_settings = _inherited_settings_from_document(
            effective_config,
            configured_source="codex",
            allow_builtin_defaults=True,
        )
        inherited_settings_error = None
        effective_model_value = effective_config.get("model")
        effective_model = (
            effective_model_value.strip()
            if isinstance(effective_model_value, str) and effective_model_value.strip()
            else None
        )
    else:
        inherited_settings, inherited_settings_error = read_user_inherited_settings(home)
        effective_model = None
    codex_model = effective_model or user_model
    codex_model_source = (
        "codex" if effective_model else "user" if user_model else "builtin"
    )
    version_warning: str | None = None
    actual_version = binary.get("version")
    cache_version = cache.get("client_version")
    if actual_version and cache_version and actual_version != cache_version:
        version_warning = (
            f"当前 CLI {actual_version} 与模型缓存客户端 {cache_version} 不一致；"
            "建议为后台配置独立 CODEX_HOME，或固定所有调用方使用同一 Codex 版本"
        )
    if expected_version and actual_version != expected_version:
        version_warning = (
            f"当前 CLI {actual_version or '无法识别'} 与配置期望版本 "
            f"{expected_version} 不一致"
        )
    if settings.model:
        inherited_model = {
            "value": settings.model,
            "source": "runtime",
            "label": f"继承 Teamwork 运行时默认（{settings.model}）",
        }
    elif effective_model:
        inherited_model = {
            "value": effective_model,
            "source": "codex",
            "label": f"继承 Codex 有效配置（{effective_model}）",
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
        "catalog_source": catalog_source,
        "inherited_model": inherited_model,
        "codex_model": codex_model,
        "codex_model_source": codex_model_source,
        "user_model": user_model,
        "user_config_path": user_config_path,
        "catalog_error": catalog_error,
        "user_config_error": user_error,
        "codex_home": str(home),
        "binary": binary,
        "model_cache": cache,
        "expected_version": expected_version,
        "version_warning": version_warning,
        "user_mcp_servers": user_mcp_servers,
        "user_mcp_error": user_mcp_error,
        "inherited_settings": inherited_settings,
        "inherited_settings_error": inherited_settings_error,
        "effective_config_error": effective_config_error,
        "managed_sandbox": {
            **sandbox_inspection.as_dict(),
            "enabled": sandbox_settings.enabled,
            "fail_closed": sandbox_settings.fail_closed,
        },
    }
