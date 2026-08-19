"""Teamwork 托管的 Codex 跨平台外层沙盒。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Mapping

from .config import AgentConfig


_PROFILE_NAME = "teamwork_managed"
_INSPECTION_TIMEOUT_SECONDS = 5


@dataclass(frozen=True)
class ManagedSandboxInspection:
    """当前主机与 Codex CLI 的外层沙盒能力诊断。"""

    available: bool
    platform: str
    backend: str | None
    error: str | None = None

    def as_dict(self) -> dict[str, object]:
        """转换为可由管理 API 返回的脱敏结构。"""

        return {
            "available": self.available,
            "platform": self.platform,
            "backend": self.backend,
            "error": self.error,
        }


def _platform_backend() -> tuple[str, str | None]:
    """识别当前平台以及 Codex 使用的原生沙盒后端。"""

    if sys.platform == "darwin":
        return "macOS", "seatbelt"
    if sys.platform.startswith("linux"):
        is_wsl = bool(os.environ.get("WSL_DISTRO_NAME"))
        if not is_wsl:
            try:
                release = Path("/proc/sys/kernel/osrelease").read_text(
                    encoding="utf-8",
                    errors="replace",
                )
            except OSError:
                release = ""
            is_wsl = "microsoft" in release.lower()
        return ("WSL" if is_wsl else "Linux"), "linux"
    if os.name == "nt" or sys.platform == "win32":
        return "Windows", "windows"
    return sys.platform, None


def _inspection_environment(codex_home: Path | None) -> dict[str, str]:
    """构造不读取业务凭据的 Codex 能力诊断环境。"""

    environment = os.environ.copy()
    if codex_home is not None:
        environment["CODEX_HOME"] = str(codex_home.expanduser().resolve())
    return environment


@lru_cache(maxsize=16)
def _inspect_cached(
    codex_binary: str,
    codex_home_text: str | None,
    platform_name: str,
    backend: str | None,
) -> ManagedSandboxInspection:
    """按二进制和配置目录缓存沙盒执行器能力。"""

    if backend is None:
        return ManagedSandboxInspection(
            available=False,
            platform=platform_name,
            backend=None,
            error=f"当前平台 {platform_name} 不在 Teamwork 外层沙盒支持范围内",
        )
    configured_home = Path(codex_home_text) if codex_home_text else None
    try:
        completed = subprocess.run(
            [codex_binary, "sandbox", "--help"],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_INSPECTION_TIMEOUT_SECONDS,
            env=_inspection_environment(configured_home),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return ManagedSandboxInspection(
            available=False,
            platform=platform_name,
            backend=backend,
            error=f"无法检查 Codex 外层沙盒执行器：{exc}",
        )
    output = f"{completed.stdout}\n{completed.stderr}".lower()
    if completed.returncode == 0 and "permission-profile" in output:
        return ManagedSandboxInspection(
            available=True,
            platform=platform_name,
            backend=backend,
        )
    detail = (completed.stderr or completed.stdout).strip()
    if len(detail) > 300:
        detail = f"{detail[:300]}…"
    return ManagedSandboxInspection(
        available=False,
        platform=platform_name,
        backend=backend,
        error=(
            "当前 Codex CLI 未提供可用的 `codex sandbox --permission-profile`"
            + (f"：{detail}" if detail else "")
        ),
    )


def inspect_managed_sandbox(
    codex_binary: str,
    codex_home: Path | None = None,
) -> ManagedSandboxInspection:
    """检查当前 Codex CLI 是否能作为受托的原生沙盒执行器。"""

    platform_name, backend = _platform_backend()
    home_text = str(codex_home.expanduser().resolve()) if codex_home else None
    return _inspect_cached(codex_binary, home_text, platform_name, backend)


def _toml_string(value: str) -> str:
    """生成兼容 TOML 的字符串字面量。"""

    return json.dumps(value, ensure_ascii=False)


def _network_policy(agent: AgentConfig) -> str:
    """把 Agent 联网配置映射为权限档案网络策略。"""

    if not agent.network_access:
        return "network={enabled=false}"
    if not agent.network_domains:
        return 'network={enabled=true,mode="full"}'
    domains = ", ".join(
        f"{_toml_string(domain)}=\"allow\""
        for domain in agent.network_domains
    )
    return f'network={{enabled=true,mode="limited",domains={{{domains}}}}}'


def permission_profile_override(
    agent: AgentConfig,
    *,
    ipc_directory: Path | None = None,
) -> str:
    """生成只描述当前 Agent 文件与网络边界的命名权限档案。"""

    ipc_entry = (
        f"{_toml_string(str(ipc_directory.resolve()))}=\"write\""
        if ipc_directory is not None
        else None
    )
    if agent.sandbox == "read-only":
        fields = [
            'description="Teamwork 托管的只读 Agent 外层沙盒"',
            'extends=":read-only"',
            *([f"filesystem={{{ipc_entry}}}"] if ipc_entry else []),
            _network_policy(agent),
        ]
    elif agent.sandbox == "workspace-write":
        filesystem_entries = ['":workspace_roots"={".git"="write"}']
        if ipc_entry:
            filesystem_entries.append(ipc_entry)
        fields = [
            'description="Teamwork 托管的可写 Agent 外层沙盒"',
            'extends=":workspace"',
            f"filesystem={{{','.join(filesystem_entries)}}}",
            _network_policy(agent),
        ]
    else:
        raise ValueError("完全访问 Agent 不应生成受限外层沙盒权限档案")
    return f"permissions.{_PROFILE_NAME}={{{','.join(fields)}}}"


def wrap_managed_sandbox_command(
    *,
    codex_binary: str,
    workspace: Path,
    agent: AgentConfig,
    inner_command: list[str],
    environment: Mapping[str, str],
    ipc_directory: Path | None = None,
) -> list[str]:
    """用 Codex 原生平台沙盒包裹已关闭内层沙盒的执行命令。"""

    command = [
        codex_binary,
        "sandbox",
        "--permission-profile",
        _PROFILE_NAME,
        "--cd",
        str(workspace),
        "--config",
        permission_profile_override(agent, ipc_directory=ipc_directory),
    ]
    if agent.network_access and agent.network_domains:
        # 权限档案只声明域名规则；必须启用网络代理才能真正强制执行白名单。
        command.extend(["--config", "features.network_proxy=true"])
    socket_path = environment.get("SSH_AUTH_SOCK")
    if sys.platform == "darwin" and socket_path:
        command.extend(["--allow-unix-socket", socket_path])
    # 显式结束外层参数，避免内层 Codex 选项被外层解析器误认。
    command.append("--")
    command.extend(inner_command)
    return command
