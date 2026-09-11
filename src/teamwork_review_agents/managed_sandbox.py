"""Teamwork 托管的 Codex 跨平台外层沙盒。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, replace
from functools import lru_cache
from pathlib import Path
from typing import Mapping

from .config import AgentConfig
from .codex_executable import CodexRuntimeError, locate_codex_executable
from .process_control import hidden_process_options
from .sandbox_git import current_sandbox_git
from .subprocess_utils import (
    WINDOWS_REQUIRED_ENVIRONMENT_NAMES,
    selected_environment,
)


_PROFILE_NAME = "teamwork_managed"
_INSPECTION_TIMEOUT_SECONDS = 5


@dataclass(frozen=True)
class ManagedSandboxInspection:
    """当前主机与 Codex CLI 的外层沙盒能力诊断。"""

    available: bool
    platform: str
    backend: str | None
    error: str | None = None
    error_code: str | None = None
    retryable: bool = True
    configured_command: str | None = None
    resolved_path: str | None = None
    discovery_source: str | None = None

    def as_dict(self) -> dict[str, object]:
        """转换为可由管理 API 返回的脱敏结构。"""

        return asdict(self)


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


def _inspection_environment(
    codex_home: Path | None, source: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """构造不读取业务凭据的 Codex 能力诊断环境。"""

    environment = selected_environment(
        WINDOWS_REQUIRED_ENVIRONMENT_NAMES | {"PATH", "HOME", "CODEX_HOME", "LANG", "LC_ALL"},
        source,
    )
    if codex_home is not None:
        environment["CODEX_HOME"] = str(codex_home.expanduser().resolve())
    return environment


@lru_cache(maxsize=16)
def _inspect_cached(
    codex_binary: str,
    codex_home_text: str | None,
    platform_name: str,
    backend: str | None,
    environment_items: tuple[tuple[str, str], ...] = (),
    binary_fingerprint: tuple[int, int, int] = (0, 0, 0),
    cache_period: int = 0,
) -> ManagedSandboxInspection:
    """仅缓存成功结果；环境、文件变化或短期到期后重新探测。"""

    if backend is None:
        raise CodexRuntimeError(
            f"当前平台 {platform_name} 不在 Teamwork 外层沙盒支持范围内",
            error_code="sandbox_platform_unsupported",
        )
    environment = dict(environment_items)
    try:
        completed = subprocess.run(
            [codex_binary, "sandbox", "--help"],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_INSPECTION_TIMEOUT_SECONDS,
            env=environment,
            **hidden_process_options(),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise CodexRuntimeError(
            f"无法检查 Codex 外层沙盒执行器：{exc}",
            error_code="sandbox_probe_failed", retryable=True,
        ) from exc
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
    # 成功返回帮助但缺少参数才是确定性能力缺失；非零退出可能是暂时启动异常。
    unsupported = completed.returncode == 0
    raise CodexRuntimeError(
        (
            "当前 Codex CLI 未提供可用的 `codex sandbox --permission-profile`"
            + (f"：{detail}" if detail else "")
        ),
        error_code="sandbox_capability_missing" if unsupported else "sandbox_probe_failed",
        retryable=not unsupported,
    )


def inspect_managed_sandbox(
    codex_binary: str,
    codex_home: Path | None = None,
    *,
    environment: Mapping[str, str] | None = None,
) -> ManagedSandboxInspection:
    """检查当前 Codex CLI 是否能作为受托的原生沙盒执行器。"""

    platform_name, backend = _platform_backend()
    home_text = str(codex_home.expanduser().resolve()) if codex_home else None
    diagnostic_environment = _inspection_environment(codex_home, environment)
    resolution = None
    try:
        resolution = locate_codex_executable(codex_binary, diagnostic_environment)
        info = Path(resolution.resolved_path).stat()
        result = _inspect_cached(
            resolution.resolved_path, home_text, platform_name, backend,
            tuple(sorted(diagnostic_environment.items())),
            (info.st_mtime_ns, info.st_size, info.st_ino), int(time.monotonic() // 30),
        )
    except (CodexRuntimeError, OSError) as exc:
        result = ManagedSandboxInspection(
            available=False, platform=platform_name, backend=backend, error=str(exc),
            error_code=exc.error_code if isinstance(exc, CodexRuntimeError) else "sandbox_probe_failed",
            retryable=exc.retryable if isinstance(exc, CodexRuntimeError) else True,
        )
    return replace(
        result, configured_command=codex_binary,
        resolved_path=resolution.resolved_path if resolution else None,
        discovery_source=resolution.discovery_source if resolution else None,
    )


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
    codex_runtime_directory: Path | None = None,
    writable_directories: tuple[Path, ...] = (),
) -> str:
    """生成只描述当前 Agent 文件与网络边界的命名权限档案。"""

    extra_filesystem_entries = [
        f"{_toml_string(str(path.resolve()))}=\"write\""
        for path in (ipc_directory, codex_runtime_directory, *writable_directories)
        if path is not None
    ]
    git_context = current_sandbox_git()
    if git_context is not None:
        git_context.validate_helper_directory()
        # 授权来自宿主内存，不从工具环境推断；helper 可写、Python 依赖只读。
        extra_filesystem_entries.extend(
            f"{_toml_string(str(path))}=\"read\""
            for path in git_context.readable_directories
        )
        extra_filesystem_entries.extend(
            f"{_toml_string(str(path))}=\"write\""
            for path in git_context.writable_directories
        )
    if agent.sandbox == "read-only":
        fields = [
            'description="Teamwork 托管的只读 Agent 外层沙盒"',
            'extends=":read-only"',
            *(
                [f"filesystem={{{','.join(extra_filesystem_entries)}}}"]
                if extra_filesystem_entries
                else []
            ),
            _network_policy(agent),
        ]
    elif agent.sandbox == "workspace-write":
        filesystem_entries = ['":workspace_roots"={".git"="write"}']
        filesystem_entries.extend(extra_filesystem_entries)
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
    codex_runtime_directory: Path | None = None,
) -> list[str]:
    """用 Codex 原生平台沙盒包裹已关闭内层沙盒的执行命令。"""

    repository_cache = environment.get("TEAMWORK_REPOSITORY_CACHE_DIR")
    writable_directories = (
        (Path(repository_cache),) if repository_cache else ()
    )
    command = [
        codex_binary,
        "sandbox",
        "--permission-profile",
        _PROFILE_NAME,
        "--cd",
        str(workspace),
        "--config",
        permission_profile_override(
            agent,
            ipc_directory=ipc_directory,
            codex_runtime_directory=codex_runtime_directory,
            writable_directories=writable_directories,
        ),
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
