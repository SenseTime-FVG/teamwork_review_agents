"""本地 Git 工作目录的自动克隆、校验与变更请求引用准备。"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import urlparse

from .config import ProviderConfig, RepositoryConfig
from .models import ChangeRequestSnapshot


class WorkspaceError(RuntimeError):
    """表示本地 Git 工作目录无法安全准备。"""


def repository_clone_url(
    provider: ProviderConfig,
    repository: RepositoryConfig,
) -> str:
    """返回显式克隆地址，缺失时根据平台 API 主机生成 SSH 地址。"""

    if repository.clone_url:
        parsed = urlparse(repository.clone_url)
        if parsed.scheme in {"http", "https"} and (
            parsed.username or parsed.password
        ):
            raise WorkspaceError("HTTPS 克隆地址不能内嵌用户名或 Token，请改用 SSH")
        return repository.clone_url
    host = urlparse(provider.base_url).hostname
    if not host:
        raise WorkspaceError("无法从平台 API 地址推导 Git 克隆主机")
    return f"git@{host}:{repository.project}.git"


def change_request_ref(provider: ProviderConfig, number: int) -> tuple[str, str]:
    """返回平台变更请求源引用与本地稳定引用。"""

    if provider.kind == "github":
        source = f"refs/pull/{number}/head"
    else:
        source = f"refs/merge-requests/{number}/head"
    destination = f"refs/teamwork/change-requests/{number}/head"
    return source, destination


def _run_git(
    arguments: list[str],
    *,
    timeout_seconds: int = 300,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    """以参数数组运行 Git，并避免将可能包含凭据的输出写入异常。"""

    git_binary = shutil.which("git")
    if not git_binary:
        raise WorkspaceError("系统中没有找到 git 命令")
    try:
        result = subprocess.run(
            [git_binary, *arguments],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise WorkspaceError("Git 操作超时，请检查网络和 SSH 认证") from exc
    if check and result.returncode != 0:
        raise WorkspaceError("Git 操作失败，请检查仓库地址、网络和 SSH/HTTPS 权限")
    return result


def ensure_repository_workspace(
    provider: ProviderConfig,
    repository: RepositoryConfig,
) -> Path:
    """目录不存在时原子克隆，存在时只校验而不覆盖用户文件。"""

    workspace = repository.workspace.resolve()
    if not workspace.exists():
        workspace.parent.mkdir(parents=True, exist_ok=True)
        clone_url = repository_clone_url(provider, repository)
        with tempfile.TemporaryDirectory(
            dir=workspace.parent,
            prefix=f".{workspace.name}.clone-",
        ) as temporary_root:
            checkout = Path(temporary_root) / "repository"
            _run_git(["clone", "--origin", "origin", "--", clone_url, str(checkout)])
            if workspace.exists():
                # 另一进程可能已经完成克隆，保留先完成的工作目录。
                pass
            else:
                checkout.replace(workspace)
    if not workspace.is_dir():
        raise WorkspaceError(f"本地工作目录不是文件夹：{workspace}")
    result = _run_git(
        ["-C", str(workspace), "rev-parse", "--is-inside-work-tree"],
        check=False,
    )
    if result.returncode != 0 or result.stdout.strip() != "true":
        raise WorkspaceError(f"本地工作目录不是 Git 仓库：{workspace}")
    return workspace


def prepare_change_request_workspace(
    provider: ProviderConfig,
    repository: RepositoryConfig,
    snapshot: ChangeRequestSnapshot,
) -> str:
    """更新远端引用并准备 MR/PR head，但不切换用户当前分支。"""

    workspace = ensure_repository_workspace(provider, repository)
    _run_git(["-C", str(workspace), "fetch", "--prune", "origin"])
    source_ref, destination_ref = change_request_ref(provider, snapshot.number)
    fetch_result = _run_git(
        [
            "-C",
            str(workspace),
            "fetch",
            "origin",
            f"+{source_ref}:{destination_ref}",
        ],
        check=False,
    )
    if fetch_result.returncode != 0:
        existing_commit = _run_git(
            ["-C", str(workspace), "cat-file", "-e", f"{snapshot.head_sha}^{{commit}}"],
            check=False,
        )
        if existing_commit.returncode != 0:
            raise WorkspaceError(
                "无法获取 MR/PR 代码引用，请检查远端权限或平台引用格式"
            )
    return destination_ref
