"""Codex 专用可执行文件发现与单次运行路径绑定。"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from contextvars import ContextVar
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Mapping

from .process_control import hidden_process_options


class CodexRuntimeError(RuntimeError):
    """保留就绪检查的错误类别，避免把确定性错误当成模型故障重试。"""

    def __init__(
        self, message: str, *, error_code: str, retryable: bool = False,
        details: dict[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.retryable = retryable
        self.details = details or {}


@dataclass(frozen=True)
class CodexExecutable:
    """不含凭据的程序解析结果。"""

    configured_command: str
    resolved_path: str
    discovery_source: str

    def as_dict(self) -> dict[str, object]:
        """供运行日志与诊断接口展示实际使用的程序。"""

        return asdict(self)


# 路径在准备前确定，避免临时 HOME、工具 PATH 或桌面升级改变本轮执行器。
active_codex_executable: ContextVar[CodexExecutable | None] = ContextVar(
    "teamwork_codex_executable", default=None,
)


def _windows_host() -> bool:
    """集中平台判断，便于非 Windows 主机覆盖发现逻辑。"""

    return os.name == "nt" or sys.platform == "win32"


def _environment_value(environment: Mapping[str, str], name: str) -> str:
    """兼容 Windows 环境键的大小写。"""

    wanted = name.upper()
    return next(
        (value for key, value in environment.items() if key.upper() == wanted),
        "",
    )


def locate_codex_executable(
    command: str, environment: Mapping[str, str] | None = None,
) -> CodexExecutable:
    """按显式路径、PATH、当前账户桌面安装顺序解析，不扫描其他账户。"""

    active = active_codex_executable.get()
    if active is not None and command in {active.configured_command, active.resolved_path}:
        return active
    environment = os.environ if environment is None else environment
    configured = command
    explicit = Path(command).is_absolute() or "/" in command or "\\" in command or command.startswith("~")
    if explicit:
        candidate = Path(command).expanduser()
        if candidate.is_file():
            return CodexExecutable(configured, str(candidate.resolve()), "configured_path")
    else:
        path = _environment_value(environment, "PATH") or os.defpath
        try:
            resolved = shutil.which(command, path=path)
        except AttributeError:
            # 平台模拟测试没有 Windows 专属模块，按未找到处理。
            resolved = None
        if resolved:
            return CodexExecutable(configured, str(Path(resolved).resolve()), "path")
        if _windows_host() and command.lower() in {"codex", "codex.exe"}:
            local_app_data = _environment_value(environment, "LOCALAPPDATA")
            if not local_app_data:
                user_profile = _environment_value(environment, "USERPROFILE")
                if user_profile:
                    local_app_data = str(Path(user_profile) / "AppData" / "Local")
            if local_app_data:
                directory = Path(local_app_data) / "OpenAI" / "Codex" / "bin"
                try:
                    candidates = sorted(
                        (item for item in directory.glob("*/codex.exe") if item.is_file()),
                        key=lambda item: (item.stat().st_mtime_ns, str(item)), reverse=True,
                    )
                except OSError as exc:
                    raise CodexRuntimeError(
                        f"无法读取当前账户的 Codex 安装目录：{directory}；{exc}",
                        error_code="codex_discovery_failed", retryable=True,
                    ) from exc
                last_error: Exception | None = None
                for candidate in candidates:
                    try:
                        probe = subprocess.run(
                            [str(candidate), "--version"], capture_output=True,
                            text=True, encoding="utf-8", errors="replace", timeout=5,
                            check=False, env=dict(environment), **hidden_process_options(),
                        )
                    except (OSError, subprocess.SubprocessError) as exc:
                        last_error = exc
                        continue
                    if probe.returncode == 0 and re.search(r"\d+\.\d+\.\d+", probe.stdout):
                        return CodexExecutable(configured, str(candidate.resolve()), "windows_desktop")
                if candidates:
                    raise CodexRuntimeError(
                        "已发现 Codex 安装文件，但版本探测未成功"
                        + (f"：{last_error}" if last_error else "，请检查安装是否完整"),
                        error_code="codex_discovery_probe_failed", retryable=True,
                        details={"configured_command": configured, "resolved_path": None},
                    )
    raise CodexRuntimeError(
        f"找不到 Codex CLI：{configured}；请检查服务 PATH 或 runtime.codex_binary 绝对路径",
        error_code="codex_not_found",
        details={"configured_command": configured, "resolved_path": None},
    )


def resolve_codex_executable(
    command: str,
    environment: Mapping[str, str] | None = None,
    *,
    allow_unresolved: bool = False,
) -> str:
    """返回本轮固定的绝对路径，缺失时保留结构化错误。"""

    try:
        return locate_codex_executable(command, environment).resolved_path
    except CodexRuntimeError:
        if allow_unresolved:
            # 仅用于构造待执行命令；正式运行前的就绪检查仍必须严格失败。
            return command
        raise
