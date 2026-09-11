"""Windows 托管沙盒的运行级 Git HTTPS 兼容环境。"""

from __future__ import annotations

import os
import re
import shlex
import sys
import tempfile
from collections.abc import Iterable, Mapping
from contextvars import ContextVar, Token
from pathlib import Path
from urllib.parse import urlsplit

from .filesystem import remove_tree
from .git_auth import write_askpass_helper


class SandboxGitError(RuntimeError):
    """需要阻断 Agent 的 Git 基础设施故障，不等同于普通命令退出失败。"""

    def __init__(self, message: str, *, error_code: str, retryable: bool = False) -> None:
        # 诊断只保留远端主机，避免 URL 中的用户名、密码或查询参数进入日志。
        def redact_url(match: re.Match[str]) -> str:
            try:
                parsed = urlsplit(match.group())
                return f"{parsed.scheme}://{parsed.hostname or '[远端]'}/…"
            except ValueError:
                return "[已隐藏的远端 URL]"

        super().__init__(re.sub(r"https?://[^\s\"'<>]+", redact_url, message))
        self.error_code = error_code
        self.retryable = retryable


_ACTIVE: ContextVar[SandboxGitContext | None] = ContextVar("sandbox_git_context", default=None)


def current_sandbox_git() -> SandboxGitContext | None:
    """仅从宿主内存读取授权路径，不信任环境中的目录声明。"""

    return _ACTIVE.get()


def windows_sandbox_git_enabled(*, managed: bool) -> bool:
    """将兼容设置限制在 Windows 托管沙盒。"""

    return managed and (os.name == "nt" or sys.platform == "win32")


def append_git_config(
    environment: dict[str, str], entries: Mapping[str, str] | Iterable[tuple[str, str]],
) -> None:
    """追加命令作用域配置，保留多值键顺序且不覆盖已有凭据或 excludes。"""

    try:
        count = int(environment.get("GIT_CONFIG_COUNT", "0") or "0")
        if not 0 <= count <= 256:
            raise ValueError
        for index in range(count):
            if f"GIT_CONFIG_KEY_{index}" not in environment or f"GIT_CONFIG_VALUE_{index}" not in environment:
                raise ValueError
    except ValueError as exc:
        raise SandboxGitError("运行环境的 Git 配置索引无效", error_code="sandbox_git_config_invalid") from exc
    for key, value in entries.items() if isinstance(entries, Mapping) else entries:
        environment[f"GIT_CONFIG_KEY_{count}"] = key
        environment[f"GIT_CONFIG_VALUE_{count}"] = value
        count += 1
    environment["GIT_CONFIG_COUNT"] = str(count)


class SandboxGitContext:
    """为一个运行准备环境和只读 helper，绝不复用宿主可执行凭据文件。"""

    def __init__(self, environment: Mapping[str, str], *, verified_workspace: Path) -> None:
        # 路径必须由执行器在创建/继承校验后提供，不能从环境或命令错误中推断。
        self.verified_workspace = verified_workspace
        self.environment = {
            key.upper() if key.upper().startswith(("GIT_", "TEAMWORK_GIT_")) else key: value
            for key, value in environment.items()
        }
        self.directory: Path | None = None
        self.readable_directories: tuple[Path, ...] = ()
        self.probe_command: list[str] | None = None
        self._context_token: Token | None = None

    def start(self) -> SandboxGitContext:
        """只信任本轮已校验工作区，并消费已获准传入工具进程的 Token。"""

        try:
            workspace = self.verified_workspace.resolve(strict=True)
            if (
                not workspace.is_dir() or workspace.parent == workspace
                or "*" in workspace.as_posix()
                or not ((workspace / ".git").is_dir() or (workspace / ".git").is_file())
            ):
                raise ValueError("工作区必须是存在的非根 Git 目录，且不含通配符")
        except (OSError, ValueError, RuntimeError) as exc:
            raise SandboxGitError(
                f"本轮已校验工作区不可用：{self.verified_workspace}：{exc}",
                error_code="sandbox_git_workspace_invalid",
            ) from exc
        self.verified_workspace = workspace
        # 空值清除本轮继承的信任列表，再仅信任精确目录；不修改全局配置或 ACL。
        append_git_config(self.environment, [
            ("http.sslBackend", "openssl"), ("http.sslVerify", "true"),
            ("safe.directory", ""), ("safe.directory", workspace.as_posix()),
        ])
        self.environment.pop("GIT_SSL_NO_VERIFY", None)
        self.environment["GIT_TERMINAL_PROMPT"] = "0"
        self.environment["GCM_INTERACTIVE"] = "never"
        try:
            if self.environment.get("TEAMWORK_GIT_TOKEN"):
                self.directory = Path(tempfile.mkdtemp(prefix="teamwork-sandbox-git-"))
                command = write_askpass_helper(self.directory / "askpass.py")
                # GIT_ASKPASS 是文件名而不是 shell 命令。Git for Windows 支持带
                # shebang 的脚本，由随 Git 提供的 sh 执行；参数只在脚本内转义。
                launcher = self.directory / "askpass.sh"
                python_command = shlex.join([part.replace("\\", "/") for part in command])
                launcher.write_text(
                    f'#!/bin/sh\n# 凭据仅由隔离的 Python helper 从环境读取。\nexec {python_command} "$@"\n',
                    encoding="utf-8", newline="\n",
                )
                launcher.chmod(0o700)
                self.environment["GIT_ASKPASS"] = str(launcher)
                self.probe_command = [*command, "teamwork-helper-probe"]
                self.readable_directories = tuple(dict.fromkeys((
                    self.directory.resolve(), Path(sys.executable).resolve().parent,
                    Path(sys.base_prefix).resolve(),
                )))
            self._context_token = _ACTIVE.set(self)
            return self
        except Exception:
            self.close()
            raise

    def close(self) -> None:
        """恢复父运行上下文并清理当前 helper，不删除宿主或其他运行目录。"""

        if self._context_token is not None:
            _ACTIVE.reset(self._context_token)
            self._context_token = None
        if self.directory is not None:
            remove_tree(self.directory)
            self.directory = None


def classify_git_failure(output: str) -> SandboxGitError | None:
    """识别所有权、TLS 和认证基础设施错误，普通冲突等交回模型处理。"""

    lowered = output.lower()
    categories = (
        ("sandbox_git_ownership_mismatch", "工作区所有权校验失败，Git 尚未进入远端网络认证", ("fatal: detected dubious ownership in repository",)),
        ("sandbox_git_schannel_credentials", "Windows 沙盒无法初始化 Schannel TLS 凭据", ("schannel: acquirecredentialshandle failed", "sec_e_no_credentials")),
        ("sandbox_git_openssl_unavailable", "当前 Git 不支持所需 OpenSSL 后端", ("fatal: unsupported ssl backend",)),
        ("sandbox_git_certificate_invalid", "Git HTTPS 证书校验失败，请检查可信 CA 配置", ("ssl certificate problem:", "error setting certificate file:", "error setting certificate verify locations:")),
        ("sandbox_git_askpass_unavailable", "沙盒无法执行 Git askpass helper", ("unable to read askpass response", "cannot run git_askpass")),
        ("sandbox_git_auth_failed", "Git HTTPS 身份认证失败，请检查获准传入进程的仓库凭据", ("fatal: authentication failed", "fatal: could not read username for 'https://", "fatal: could not read password for 'https://", "the requested url returned error: 401", "the requested url returned error: 403")),
    )
    for code, message, markers in categories:
        if any(marker in lowered for marker in markers):
            return SandboxGitError(f"{message}：{output[-1200:]}", error_code=code)
    return None
