"""Codex CLI 配置覆盖生成与本机运行能力检查。"""

from __future__ import annotations

import json
import os
import re
import shutil
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

    resolved = shutil.which(codex_binary)
    if resolved is None and Path(codex_binary).expanduser().is_file():
        resolved = str(Path(codex_binary).expanduser().resolve())
    command = resolved or codex_binary
    try:
        result = subprocess.run(
            [command, "--version"],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
            env=_codex_environment(home),
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


def read_bundled_models(
    codex_binary: str,
    home: Path | None = None,
) -> tuple[list[dict[str, Any]], str | None]:
    """从本机 Codex CLI 读取模型目录；失败时保留手工填写能力。"""

    try:
        result = subprocess.run(
            [codex_binary, "debug", "models", "--bundled"],
            check=True,
            capture_output=True,
            text=True,
            timeout=8,
            env=_codex_environment(home),
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
    configured_home: Path | None = None,
    expected_version: str | None = None,
) -> dict[str, Any]:
    """组合模型目录与可验证的默认模型来源，供管理 UI 展示。"""

    home = codex_home(configured_home)
    models, catalog_error = read_bundled_models(codex_binary, home)
    user_model, user_error, user_config_path = read_user_model(home)
    binary = inspect_codex_binary(codex_binary, home)
    cache = inspect_model_cache(home)
    user_mcp_servers, user_mcp_error = read_user_mcp_servers(home)
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
        "codex_home": str(home),
        "binary": binary,
        "model_cache": cache,
        "expected_version": expected_version,
        "version_warning": version_warning,
        "user_mcp_servers": user_mcp_servers,
        "user_mcp_error": user_mcp_error,
    }
