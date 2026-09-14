"""为原生 Git 提供一次运行级的临时 HTTPS 凭证上下文。"""

from __future__ import annotations

import base64
import os
import re
import shlex
import sys
import tempfile
import unicodedata
from contextlib import contextmanager
from contextvars import ContextVar, Token
from pathlib import Path
from typing import Iterable, Iterator, Mapping
from urllib.parse import quote, quote_plus, urlsplit, urlunsplit

from .filesystem import remove_tree


_ACTIVE_ENVIRONMENT: ContextVar[Mapping[str, str] | None] = ContextVar(
    "teamwork_git_environment",
    default=None,
)


def write_askpass_helper(helper: Path, *, python_binary: str | None = None) -> list[str]:
    """生成不含凭据的 helper；隔离导入环境并提供无密钥自检入口。"""

    helper.write_text(
        "# Git 凭据只从已授权的进程环境读取，不写入文件。\n"
        "import os, sys\n"
        "prompt = (sys.argv[1] if len(sys.argv) > 1 else '').lower()\n"
        "if prompt == 'teamwork-helper-probe':\n"
        "    print('teamwork-askpass-ready')\n"
        "elif 'username' in prompt:\n"
        "    print(os.environ.get('TEAMWORK_GIT_USERNAME', 'x-access-token'))\n"
        "elif 'password' in prompt:\n"
        "    print(os.environ.get('TEAMWORK_GIT_TOKEN', ''))\n"
        "else:\n"
        "    print('')\n",
        encoding="utf-8",
    )
    return [python_binary or sys.executable, "-I", "-S", str(helper)]


def write_askpass_launcher(launcher: Path, command: list[str]) -> None:
    """让 Git 只执行脚本路径，解释器及参数在脚本内部安全转义。"""

    # Git for Windows 使用自带的 sh 解释 shebang，Windows 路径需转换斜杠。
    arguments = [part.replace("\\", "/") for part in command] if os.name == "nt" else command
    launcher.write_text(
        '#!/bin/sh\n# 凭据仅由隔离的 Python helper 从环境读取。\n'
        f'exec {shlex.join(arguments)} "$@"\n',
        encoding="utf-8", newline="\n",
    )
    launcher.chmod(0o700)


def safe_git_error_detail(message: str, *, secrets: Iterable[str] = ()) -> str:
    """先脱敏再截断，供 Git 日志和向导复用，避免暴露认证与代理凭据。"""

    detail = re.sub(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\))", "", message)
    detail = "".join(char for char in detail if char in "\n\t" or unicodedata.category(char) not in {"Cc", "Cf"})
    variants: set[str] = set()
    for secret in secrets:
        if secret:
            variants.update((secret, quote(secret, safe=""), quote_plus(secret, safe="")))
            for prefix in ("", "x-access-token:", "oauth2:"):
                variants.add(base64.b64encode(f"{prefix}{secret}".encode()).decode())
    for secret in sorted(variants, key=len, reverse=True):
        detail = detail.replace(secret, "********")

    def redact_url(match: re.Match[str]) -> str:
        """保留主机与路径用于定位，移除任意 URL 用户凭据和查询参数。"""

        try:
            parsed = urlsplit(match.group())
            host = parsed.hostname or "[远端]"
            if ":" in host:
                host = f"[{host}]"
            authority = f"{host}:{parsed.port}" if parsed.port else host
            return urlunsplit((parsed.scheme, authority, parsed.path, "", ""))
        except ValueError:
            return "[已隐藏的 URL]"

    detail = re.sub(r"\b(?:https?|ssh|socks[45]h?)://[^\s\"'<>]+", redact_url, detail, flags=re.IGNORECASE)
    detail = re.sub(r"(?im)\b((?:proxy-)?authorization\s*[:=])[^\r\n]*", r"\1 ********", detail)
    detail = re.sub(
        r"(?i)\b([\w-]*token|password|passwd|api[_-]?key|client_secret)(\s*[:=]\s*)(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)",
        r"\1\2********", detail,
    )
    return detail.strip()[-800:]


def current_git_environment() -> Mapping[str, str] | None:
    """返回当前异步任务和工作线程共享的 Git 环境补丁。"""

    return _ACTIVE_ENVIRONMENT.get()


class GitCredentialContext:
    """保存临时 askpass helper，并把 Token 只放入子进程环境。"""

    def __init__(self, token: str, *, provider_kind: str) -> None:
        self.token = token
        self.provider_kind = provider_kind
        self._directory = None
        self._context_token: Token[Mapping[str, str] | None] | None = None
        self.environment: dict[str, str] = {}

    def start(self) -> "GitCredentialContext":
        """创建本次运行独有的 helper；没有 Token 时保持匿名 Git。"""

        if not self.token:
            return self
        directory = Path(tempfile.mkdtemp(prefix="teamwork-git-auth-"))
        self._directory = directory
        try:
            command = write_askpass_helper(directory / "askpass.py")
            launcher = directory / "askpass.sh"
            write_askpass_launcher(launcher, command)
        except BaseException:
            # 创建阶段尚未进入 with，也必须清理已经创建的本次临时文件。
            self.close()
            raise
        username = "oauth2" if self.provider_kind == "gitlab" else "x-access-token"
        self.environment = {
            "GIT_ASKPASS": str(launcher),
            "GIT_TERMINAL_PROMPT": "0",
            # 空 credential helper 让当前仓库 Token 优先于宿主机旧凭证。
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "credential.helper",
            "GIT_CONFIG_VALUE_0": "",
            "TEAMWORK_GIT_TOKEN": self.token,
            "TEAMWORK_GIT_USERNAME": username,
        }
        self._context_token = _ACTIVE_ENVIRONMENT.set(self.environment)
        return self

    def close(self) -> None:
        """撤销环境上下文并删除临时 helper。"""

        if self._context_token is not None:
            _ACTIVE_ENVIRONMENT.reset(self._context_token)
            self._context_token = None
        if self._directory is not None:
            remove_tree(self._directory)
            self._directory = None

    def __enter__(self) -> "GitCredentialContext":
        return self.start()

    def __exit__(self, *_: object) -> None:
        self.close()


@contextmanager
def git_credential_context(
    token: str,
    *,
    provider_kind: str,
) -> Iterator[GitCredentialContext]:
    """在代码块内启用 Git 凭证，并保证 helper 最终删除。"""

    context = GitCredentialContext(token, provider_kind=provider_kind).start()
    try:
        yield context
    finally:
        context.close()
