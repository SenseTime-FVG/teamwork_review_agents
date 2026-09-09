"""为原生 Git 提供一次运行级的临时 HTTPS 凭证上下文。"""

from __future__ import annotations

import os
import shlex
import sys
import tempfile
import subprocess
from contextvars import ContextVar, Token
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Mapping

from .filesystem import remove_tree


_ACTIVE_ENVIRONMENT: ContextVar[Mapping[str, str] | None] = ContextVar(
    "teamwork_git_environment",
    default=None,
)


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
        helper = directory / "askpass.py"
        helper.write_text(
            "#!/usr/bin/env python3\n"
            "import os, sys\n"
            "prompt = (sys.argv[1] if len(sys.argv) > 1 else '').lower()\n"
            "if 'username' in prompt:\n"
            "    print(os.environ.get('TEAMWORK_GIT_USERNAME', 'x-access-token'))\n"
            "elif 'password' in prompt:\n"
            "    print(os.environ.get('TEAMWORK_GIT_TOKEN', ''))\n"
            "else:\n"
            "    print('')\n",
            encoding="utf-8",
        )
        if os.name != "nt":
            helper.chmod(0o700)
        askpass = (
            subprocess.list2cmdline([sys.executable, str(helper)])
            if os.name == "nt"
            else shlex.join([sys.executable, str(helper)])
        )
        username = "oauth2" if self.provider_kind == "gitlab" else "x-access-token"
        self.environment = {
            "GIT_ASKPASS": askpass,
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
