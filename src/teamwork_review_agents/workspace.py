"""本地 Git 工作目录的自动克隆、校验与变更请求引用准备。"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from .config import ProviderConfig, RepositoryConfig
from .models import ChangeRequestSnapshot


class WorkspaceError(RuntimeError):
    """表示本地 Git 工作目录无法安全准备。"""


@dataclass(frozen=True)
class WorkspaceCleanupResult:
    """一次临时 worktree 清理判断的结果。"""

    status: str
    reason: str


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


def _git_common_directory(workspace: Path) -> Path:
    """返回工作树所属仓库的公共 Git 目录。"""

    result = _run_git(
        ["-C", str(workspace), "rev-parse", "--git-common-dir"],
        check=False,
    )
    if result.returncode != 0:
        raise WorkspaceError(f"本地工作目录不是 Git 工作树：{workspace}")
    common_directory = Path(result.stdout.strip())
    if not common_directory.is_absolute():
        common_directory = workspace / common_directory
    return common_directory.resolve()


def validate_linked_workspace(source_workspace: Path, workspace: Path) -> Path:
    """校验候选工作树与主工作目录属于同一个 Git 仓库。"""

    source = source_workspace.resolve()
    candidate = workspace.resolve()
    if not candidate.is_dir():
        raise WorkspaceError(f"继承的工作目录不存在：{candidate}")
    if _git_common_directory(source) != _git_common_directory(candidate):
        raise WorkspaceError("继承的工作目录与配置仓库不属于同一个 Git 仓库")
    return candidate


def ensure_isolated_worktree(
    source_workspace: Path,
    target_workspace: Path,
    revision: str,
) -> Path:
    """为一次 Agent 运行创建或复用独立 detached Git worktree。"""

    source = source_workspace.resolve()
    target = target_workspace.resolve()
    if target.exists():
        return validate_linked_workspace(source, target)
    target.parent.mkdir(parents=True, exist_ok=True)
    result = _run_git(
        [
            "-C",
            str(source),
            "worktree",
            "add",
            "--detach",
            str(target),
            revision,
        ],
        check=False,
    )
    if result.returncode != 0:
        # 相同幂等任务并发准备时，另一进程可能已经先创建成功。
        if target.exists():
            return validate_linked_workspace(source, target)
        raise WorkspaceError("无法创建 Agent 独立 Git worktree")
    return validate_linked_workspace(source, target)


def retained_marker_path(workspace: Path) -> Path:
    """返回保留工作区对应的外部标记文件路径。"""

    target = workspace.resolve()
    return target.parent / f".{target.name}.retained.json"


def worktree_starting_head(workspace: Path) -> str | None:
    """读取临时 worktree 首次创建时记录的基线提交。"""

    marker = retained_marker_path(workspace)
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None
    value = payload.get("starting_head")
    return str(value) if value else None


def worktree_head(workspace: Path) -> str:
    """返回工作树当前 HEAD 提交。"""

    result = _run_git(
        ["-C", str(workspace.resolve()), "rev-parse", "HEAD"],
        check=False,
    )
    if result.returncode != 0 or not result.stdout.strip():
        raise WorkspaceError("无法读取临时 worktree 的 HEAD")
    return result.stdout.strip()


def clear_retained_marker(workspace: Path) -> None:
    """清除已经重新投入使用或完成清理的保留标记。"""

    retained_marker_path(workspace).unlink(missing_ok=True)


def mark_active_worktree(
    workspace: Path,
    *,
    starting_head: str,
    retention_days: int,
    timeout_seconds: int,
) -> None:
    """为运行中的 worktree 写入崩溃后仍可生效的兜底清理标记。"""

    marker = retained_marker_path(workspace)
    marker.parent.mkdir(parents=True, exist_ok=True)
    now = time.time()
    marker.write_text(
        json.dumps(
            {
                "workspace": str(workspace.resolve()),
                "starting_head": starting_head,
                "reason": "Agent 运行中；若服务异常退出则按保留期限清理",
                "retained_at": now,
                "cleanup_at": now + timeout_seconds + retention_days * 86400,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def _retain_worktree(
    workspace: Path,
    reason: str,
    retention_days: int,
) -> WorkspaceCleanupResult:
    """写入保留标记，确保异常工作区可在期限内恢复。"""

    marker = retained_marker_path(workspace)
    marker.parent.mkdir(parents=True, exist_ok=True)
    now = time.time()
    starting_head = worktree_starting_head(workspace)
    marker.write_text(
        json.dumps(
            {
                "workspace": str(workspace.resolve()),
                "starting_head": starting_head,
                "reason": reason,
                "retained_at": now,
                "cleanup_at": now + retention_days * 86400,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return WorkspaceCleanupResult(status="retained", reason=reason)


def _remove_worktree(source_workspace: Path, workspace: Path) -> None:
    """通过 Git 删除临时 worktree，并同步清除其保留标记。"""

    source = source_workspace.resolve()
    target = validate_linked_workspace(source, workspace)
    result = _run_git(
        ["-C", str(source), "worktree", "remove", "--force", str(target)],
        check=False,
    )
    if result.returncode != 0:
        raise WorkspaceError("Git 无法删除临时 worktree")
    clear_retained_marker(target)
    _run_git(["-C", str(source), "worktree", "prune"], check=False)


def cleanup_run_worktree(
    source_workspace: Path,
    workspace: Path,
    *,
    run_status: str,
    starting_head: str,
    retention_days: int,
) -> WorkspaceCleanupResult:
    """仅在没有丢失本地工作的风险时删除本次运行 worktree。"""

    target = workspace.resolve()
    if run_status != "completed":
        return _retain_worktree(
            target,
            f"Agent 运行状态为 {run_status}，保留现场用于排查或恢复",
            retention_days,
        )

    status = _run_git(
        ["-C", str(target), "status", "--porcelain", "--untracked-files=all"],
        check=False,
    )
    if status.returncode != 0:
        return _retain_worktree(
            target,
            "无法读取 Git 工作区状态，为避免误删而保留",
            retention_days,
        )
    if status.stdout.strip():
        return _retain_worktree(
            target,
            "工作区存在未提交或未跟踪文件",
            retention_days,
        )

    head_result = _run_git(
        ["-C", str(target), "rev-parse", "HEAD"],
        check=False,
    )
    if head_result.returncode != 0:
        return _retain_worktree(
            target,
            "无法确认当前提交，为避免误删而保留",
            retention_days,
        )
    current_head = head_result.stdout.strip()
    if current_head != starting_head:
        source = source_workspace.resolve()
        _run_git(["-C", str(source), "fetch", "--prune", "origin"], check=False)
        remote_refs = _run_git(
            [
                "-C",
                str(source),
                "for-each-ref",
                "--format=%(refname)",
                "--contains",
                current_head,
                "refs/remotes/origin",
            ],
            check=False,
        )
        if remote_refs.returncode != 0 or not remote_refs.stdout.strip():
            return _retain_worktree(
                target,
                "工作区包含尚未确认已推送到 origin 的提交",
                retention_days,
            )

    try:
        _remove_worktree(source_workspace, target)
    except Exception as exc:
        return _retain_worktree(
            target,
            f"自动清理失败：{exc}",
            retention_days,
        )
    reason = (
        "运行未产生本地修改，临时工作区已删除"
        if current_head == starting_head
        else "新增提交已存在于 origin，临时工作区已删除"
    )
    return WorkspaceCleanupResult(status="removed", reason=reason)


def cleanup_expired_worktrees(
    source_workspace: Path,
    worktrees_root: Path,
    *,
    now: float | None = None,
) -> list[Path]:
    """强制清理由保留期限控制且已经过期的临时 worktree。"""

    current_time = time.time() if now is None else now
    removed: list[Path] = []
    if not worktrees_root.is_dir():
        return removed
    for marker in worktrees_root.glob(".*.retained.json"):
        try:
            payload = json.loads(marker.read_text(encoding="utf-8"))
            target = Path(str(payload["workspace"])).resolve()
            cleanup_at = float(payload["cleanup_at"])
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
            # 无法验证的标记不自动删除，避免错误路径导致数据丢失。
            continue
        if cleanup_at > current_time:
            continue
        if not target.exists():
            marker.unlink(missing_ok=True)
            continue
        try:
            _remove_worktree(source_workspace, target)
        except Exception:
            continue
        removed.append(target)
    return removed


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
