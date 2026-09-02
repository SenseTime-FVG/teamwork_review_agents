"""本地 Git 工作目录的自动克隆、校验与变更请求引用准备。"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import time
import uuid
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator, Literal
from urllib.parse import urlparse

from .config import ProviderConfig, RepositoryConfig
from .filesystem import remove_tree, temporary_directory
from .models import ChangeRequestSnapshot
from .process_control import process_group_options, terminate_process


class WorkspaceError(RuntimeError):
    """表示本地 Git 工作目录无法安全准备。"""


class WorkspaceCancelled(WorkspaceError):
    """表示管理员在 Git 工作区准备期间取消了运行。"""


class WorkspaceSnapshotSuperseded(WorkspaceError):
    """表示事件快照提交已被远端更新取代且无法再获取。"""

    def __init__(self, expected_head: str, current_head: str) -> None:
        self.expected_head = expected_head
        self.current_head = current_head
        super().__init__(
            "事件 Head 已被后续提交取代，跳过旧快照："
            f"期望 {expected_head}，当前 {current_head}"
        )


GitCancelCheck = Callable[[], bool]
RunWorkspaceKind = Literal["worktree", "clone"]


@dataclass(frozen=True)
class GitProgressEvent:
    """一条不包含原始输出或认证信息的 Git 命令状态。"""

    command_id: str
    operation: str
    command: str
    state: str
    elapsed_seconds: int
    timeout_seconds: int
    started_at: float
    finished_at: float | None = None
    exit_code: int | None = None
    error: str | None = None

    def as_dict(self) -> dict[str, object]:
        """返回可直接写入日志或管理 API 的安全结构。"""

        return {
            "command_id": self.command_id,
            "operation": self.operation,
            "command": self.command,
            "state": self.state,
            "elapsed_seconds": self.elapsed_seconds,
            "timeout_seconds": self.timeout_seconds,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "exit_code": self.exit_code,
            "error": self.error,
        }


GitProgressCallback = Callable[[GitProgressEvent], None]


@dataclass(frozen=True)
class WorkspaceCleanupResult:
    """一次临时 Git 工作区清理判断的结果。"""

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


def repository_git_lock_key(repository: RepositoryConfig) -> str:
    """返回基础仓库 fetch 与运行工作区管理共用的资源锁键。"""

    return f"git_repository:{repository.workspace.resolve()}"


def change_request_ref(provider: ProviderConfig, number: int) -> tuple[str, str]:
    """返回平台变更请求源引用与本地稳定引用。"""

    if provider.kind == "github":
        source = f"refs/pull/{number}/head"
    else:
        source = f"refs/merge-requests/{number}/head"
    destination = f"refs/teamwork/change-requests/{number}/head"
    return source, destination


def _safe_git_command(arguments: list[str]) -> str:
    """生成适合管理界面展示的 Git 命令，并移除 URL 认证和查询参数。"""

    safe_arguments: list[str] = []
    for argument in arguments:
        parsed = urlparse(argument)
        if parsed.scheme in {"http", "https"} and parsed.hostname:
            host = parsed.netloc.rsplit("@", 1)[-1]
            argument = parsed._replace(
                netloc=host,
                query="",
                fragment="",
            ).geturl()
        safe_arguments.append(argument)
    return shlex.join(["git", *safe_arguments])


def _run_git(
    arguments: list[str],
    *,
    timeout_seconds: int = 600,
    check: bool = True,
    operation: str = "Git 操作",
    cancel_check: GitCancelCheck | None = None,
    progress_callback: GitProgressCallback | None = None,
) -> subprocess.CompletedProcess[str]:
    """在独立进程组运行 Git，并支持安全进度、超时与取消。"""

    git_binary = shutil.which("git")
    if not git_binary:
        raise WorkspaceError("系统中没有找到 git 命令")
    command_id = str(uuid.uuid4())
    started_at = time.time()
    monotonic_started_at = time.monotonic()
    safe_command = _safe_git_command(arguments)

    def report(
        state: str,
        *,
        exit_code: int | None = None,
        error: str | None = None,
    ) -> None:
        """回调只携带脱敏命令元数据，禁止泄露原始 Git 输出。"""

        if progress_callback is None:
            return
        with suppress(Exception):
            progress_callback(
                GitProgressEvent(
                    command_id=command_id,
                    operation=operation,
                    command=safe_command,
                    state=state,
                    elapsed_seconds=int(
                        time.monotonic() - monotonic_started_at
                    ),
                    timeout_seconds=timeout_seconds,
                    started_at=started_at,
                    finished_at=time.time()
                    if state in {"completed", "failed", "timed_out", "cancelled"}
                    else None,
                    exit_code=exit_code,
                    error=error,
                )
            )

    def terminate(process: subprocess.Popen[str]) -> None:
        """终止 Git 进程组或进程树，避免 ssh 与 index-pack 成为遗留进程。"""

        if process.poll() is not None:
            return
        with suppress(ProcessLookupError, PermissionError):
            terminate_process(process.pid, force=False, tree=True)
        try:
            process.communicate(timeout=2)
        except subprocess.TimeoutExpired:
            with suppress(ProcessLookupError, PermissionError):
                terminate_process(process.pid, force=True, tree=True)
            with suppress(subprocess.TimeoutExpired):
                process.communicate(timeout=2)

    try:
        process = subprocess.Popen(
            [git_binary, *arguments],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            **process_group_options(),
        )
    except OSError as exc:
        report("failed", error="无法启动 Git 命令")
        raise WorkspaceError("无法启动 Git 命令") from exc
    report("started")
    last_progress_at = 0
    while True:
        if cancel_check is not None and cancel_check():
            terminate(process)
            report("cancelled", error="Git 操作已由管理员取消")
            raise WorkspaceCancelled("运行已在 Git 工作区准备期间取消")
        elapsed = time.monotonic() - monotonic_started_at
        if elapsed >= timeout_seconds:
            terminate(process)
            report(
                "timed_out",
                error=f"Git 操作超过 {timeout_seconds} 秒",
            )
            raise WorkspaceError(
                f"Git 操作超过 {timeout_seconds} 秒，请检查网络和 SSH 认证"
            )
        try:
            stdout, stderr = process.communicate(
                timeout=min(0.25, max(0.01, timeout_seconds - elapsed))
            )
            break
        except subprocess.TimeoutExpired:
            elapsed_seconds = int(time.monotonic() - monotonic_started_at)
            if elapsed_seconds - last_progress_at >= 10:
                last_progress_at = elapsed_seconds
                report("progress")
    result = subprocess.CompletedProcess(
        [git_binary, *arguments],
        process.returncode,
        stdout,
        stderr,
    )
    if result.returncode == 0:
        report("completed", exit_code=result.returncode)
    else:
        report(
            "failed",
            exit_code=result.returncode,
            error="Git 命令返回非零退出码",
        )
    if check and result.returncode != 0:
        raise WorkspaceError("Git 操作失败，请检查仓库地址、网络和 SSH/HTTPS 权限")
    return result


def ensure_repository_workspace(
    provider: ProviderConfig,
    repository: RepositoryConfig,
    *,
    timeout_seconds: int = 600,
    initialization_timeout_seconds: int = 1800,
    cancel_check: GitCancelCheck | None = None,
    progress_callback: GitProgressCallback | None = None,
) -> Path:
    """目录不存在时原子克隆，存在时只校验而不覆盖用户文件。"""

    workspace = repository.workspace.resolve()
    if not workspace.exists():
        workspace.parent.mkdir(parents=True, exist_ok=True)
        clone_url = repository_clone_url(provider, repository)
        with temporary_directory(
            directory=workspace.parent,
            prefix=f".{workspace.name}.clone-",
        ) as temporary_root:
            checkout = temporary_root / "repository"
            _run_git(
                ["clone", "--origin", "origin", "--", clone_url, str(checkout)],
                timeout_seconds=initialization_timeout_seconds,
                operation="克隆基础仓库",
                cancel_check=cancel_check,
                progress_callback=progress_callback,
            )
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
        timeout_seconds=timeout_seconds,
        operation="校验基础仓库",
        cancel_check=cancel_check,
        progress_callback=progress_callback,
    )
    if result.returncode != 0 or result.stdout.strip() != "true":
        raise WorkspaceError(f"本地工作目录不是 Git 仓库：{workspace}")
    return workspace


def inspect_repository_workspace(
    workspace: Path,
    *,
    timeout_seconds: int = 10,
) -> tuple[bool, str | None]:
    """只读检查路径是否为完整可用的 Git 工作目录。"""

    target = workspace.expanduser().resolve()
    if not target.exists():
        return False, None
    if not target.is_dir():
        return False, "基础仓库路径不是目录"
    result = _run_git(
        ["-C", str(target), "rev-parse", "--is-inside-work-tree"],
        check=False,
        timeout_seconds=timeout_seconds,
        operation="检查基础仓库",
    )
    if result.returncode != 0 or result.stdout.strip() != "true":
        return False, "基础仓库目录不是有效的 Git 仓库"
    return True, None


def initialize_repository_workspace(
    provider: ProviderConfig,
    repository: RepositoryConfig,
    *,
    timeout_seconds: int = 600,
    initialization_timeout_seconds: int = 1800,
    cancel_check: GitCancelCheck | None = None,
    progress_callback: GitProgressCallback | None = None,
) -> tuple[Path, str]:
    """初始化缺失的基础仓库，或增量更新已经存在的仓库。"""

    existed = repository.workspace.expanduser().resolve().exists()
    workspace = ensure_repository_workspace(
        provider,
        repository,
        timeout_seconds=timeout_seconds,
        initialization_timeout_seconds=initialization_timeout_seconds,
        cancel_check=cancel_check,
        progress_callback=progress_callback,
    )
    if existed:
        _run_git(
            ["-C", str(workspace), "fetch", "--prune", "origin"],
            timeout_seconds=timeout_seconds,
            operation="更新基础仓库",
            cancel_check=cancel_check,
            progress_callback=progress_callback,
        )
    return workspace, "update" if existed else "initialize"


def _git_common_directory(workspace: Path, *, timeout_seconds: int = 600) -> Path:
    """返回工作树所属仓库的公共 Git 目录。"""

    result = _run_git(
        ["-C", str(workspace), "rev-parse", "--git-common-dir"],
        check=False,
        timeout_seconds=timeout_seconds,
    )
    if result.returncode != 0:
        raise WorkspaceError(f"本地工作目录不是 Git 工作树：{workspace}")
    common_directory = Path(result.stdout.strip())
    if not common_directory.is_absolute():
        common_directory = workspace / common_directory
    return common_directory.resolve()


def validate_linked_workspace(
    source_workspace: Path,
    workspace: Path,
    *,
    timeout_seconds: int = 600,
) -> Path:
    """校验候选工作树与主工作目录属于同一个 Git 仓库。"""

    source = source_workspace.resolve()
    candidate = workspace.resolve()
    if not candidate.is_dir():
        raise WorkspaceError(f"继承的工作目录不存在：{candidate}")
    if _git_common_directory(
        source,
        timeout_seconds=timeout_seconds,
    ) != _git_common_directory(candidate, timeout_seconds=timeout_seconds):
        raise WorkspaceError("继承的工作目录与配置仓库不属于同一个 Git 仓库")
    return candidate


def _git_remote_url(workspace: Path, *, timeout_seconds: int = 600) -> str:
    """读取工作区 origin 地址，不把地址写入运行日志。"""

    result = _run_git(
        ["-C", str(workspace.resolve()), "remote", "get-url", "origin"],
        check=False,
        timeout_seconds=timeout_seconds,
    )
    if result.returncode != 0 or not result.stdout.strip():
        raise WorkspaceError("Git 工作区缺少 origin 远端")
    return result.stdout.strip()


def validate_isolated_clone(
    source_workspace: Path,
    workspace: Path,
    *,
    timeout_seconds: int = 600,
) -> Path:
    """校验候选目录是属于配置仓库的独立本地 clone。"""

    source = source_workspace.resolve()
    candidate = workspace.resolve()
    if not candidate.is_dir():
        raise WorkspaceError(f"继承的工作目录不存在：{candidate}")
    if (
        candidate == source
        or candidate in source.parents
        or source in candidate.parents
    ):
        raise WorkspaceError("独立运行工作区不能与基础仓库互相包含")
    git_directory = candidate / ".git"
    if not git_directory.is_dir():
        raise WorkspaceError("候选工作目录不是拥有独立 Git 元数据的 clone")
    if _git_common_directory(
        candidate,
        timeout_seconds=timeout_seconds,
    ) != git_directory.resolve():
        raise WorkspaceError("候选 clone 的 Git 元数据不在运行目录内")
    alternates = git_directory / "objects/info/alternates"
    if alternates.is_file() and alternates.read_text(
        encoding="utf-8",
        errors="replace",
    ).strip():
        raise WorkspaceError("候选 clone 仍依赖基础仓库对象库，不是自包含运行仓库")
    if _git_remote_url(
        source,
        timeout_seconds=timeout_seconds,
    ) != _git_remote_url(candidate, timeout_seconds=timeout_seconds):
        raise WorkspaceError("候选 clone 的 origin 与配置仓库不一致")
    return candidate


def run_workspace_kind(workspace: Path) -> RunWorkspaceKind:
    """根据 `.git` 形态辨认独立 clone 或 linked worktree。"""

    git_path = workspace.resolve() / ".git"
    if git_path.is_dir():
        return "clone"
    if git_path.is_file():
        return "worktree"
    raise WorkspaceError("临时目录不是可识别的 Git 工作区")


def validate_run_workspace(
    source_workspace: Path,
    workspace: Path,
    *,
    timeout_seconds: int = 600,
) -> Path:
    """校验可继承的运行工作区属于配置基础仓库。"""

    if run_workspace_kind(workspace) == "clone":
        return validate_isolated_clone(
            source_workspace,
            workspace,
            timeout_seconds=timeout_seconds,
        )
    return validate_linked_workspace(
        source_workspace,
        workspace,
        timeout_seconds=timeout_seconds,
    )


def ensure_isolated_worktree(
    source_workspace: Path,
    target_workspace: Path,
    revision: str,
    *,
    timeout_seconds: int = 600,
    cancel_check: GitCancelCheck | None = None,
    progress_callback: GitProgressCallback | None = None,
) -> Path:
    """为一次 Agent 运行创建或复用独立 detached Git worktree。"""

    source = source_workspace.resolve()
    target = target_workspace.resolve()
    if target.exists():
        return validate_linked_workspace(
            source,
            target,
            timeout_seconds=timeout_seconds,
        )
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
        timeout_seconds=timeout_seconds,
        operation="创建隔离工作区",
        cancel_check=cancel_check,
        progress_callback=progress_callback,
    )
    if result.returncode != 0:
        # 相同幂等任务并发准备时，另一进程可能已经先创建成功。
        if target.exists():
            return validate_linked_workspace(
                source,
                target,
                timeout_seconds=timeout_seconds,
            )
        raise WorkspaceError("无法创建 Agent 独立 Git worktree")
    return validate_linked_workspace(
        source,
        target,
        timeout_seconds=timeout_seconds,
    )


def ensure_isolated_clone(
    source_workspace: Path,
    target_workspace: Path,
    revision: str,
    *,
    change_ref: str,
    timeout_seconds: int = 600,
    cancel_check: GitCancelCheck | None = None,
    progress_callback: GitProgressCallback | None = None,
) -> Path:
    """从基础仓库快速创建拥有独立 `.git` 的运行 clone。"""

    source = source_workspace.resolve()
    target = target_workspace.resolve()
    if target.exists():
        return validate_isolated_clone(
            source,
            target,
            timeout_seconds=timeout_seconds,
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    origin_url = _git_remote_url(source, timeout_seconds=timeout_seconds)
    revision_head = worktree_ref_head(
        source,
        revision,
        timeout_seconds=timeout_seconds,
    )
    with temporary_directory(
        directory=target.parent,
        prefix=f".{target.name}.clone-",
    ) as temporary_root:
        staged = temporary_root / "checkout"
        clone_result = _run_git(
            [
                "clone",
                "--no-checkout",
                "--shared",
                "--",
                str(source),
                str(staged),
            ],
            check=False,
            timeout_seconds=timeout_seconds,
            operation="创建可写运行仓库",
            cancel_check=cancel_check,
            progress_callback=progress_callback,
        )
        if clone_result.returncode != 0:
            raise WorkspaceError("无法从基础仓库创建可写运行 clone")
        _run_git(
            [
                "-C",
                str(staged),
                "fetch",
                "--prune",
                "origin",
                "+refs/remotes/origin/*:refs/remotes/origin/*",
            ],
            timeout_seconds=timeout_seconds,
            operation="复制基础仓库远端引用",
            cancel_check=cancel_check,
            progress_callback=progress_callback,
        )
        _run_git(
            ["-C", str(staged), "update-ref", change_ref, revision_head],
            timeout_seconds=timeout_seconds,
            operation="写入变更请求引用",
            cancel_check=cancel_check,
            progress_callback=progress_callback,
        )
        # 共享克隆完成后复制全部可达对象，再移除 alternates，避免运行仓库
        # 在基础仓库清理对象后损坏，也避免外层沙盒必须读取基础对象库。
        _run_git(
            ["-C", str(staged), "repack", "-a", "-d"],
            timeout_seconds=timeout_seconds,
            operation="解除基础仓库对象依赖",
            cancel_check=cancel_check,
            progress_callback=progress_callback,
        )
        alternates = staged / ".git" / "objects" / "info" / "alternates"
        try:
            alternates.unlink(missing_ok=True)
        except OSError as exc:
            raise WorkspaceError("无法解除运行 clone 的基础对象库依赖") from exc
        _run_git(
            ["-C", str(staged), "remote", "set-url", "origin", origin_url],
            timeout_seconds=timeout_seconds,
            cancel_check=cancel_check,
        )
        _run_git(
            ["-C", str(staged), "checkout", "--detach", revision_head],
            timeout_seconds=timeout_seconds,
            operation="检出变更请求提交",
            cancel_check=cancel_check,
            progress_callback=progress_callback,
        )
        if target.exists():
            return validate_isolated_clone(
                source,
                target,
                timeout_seconds=timeout_seconds,
            )
        staged.replace(target)
    return validate_isolated_clone(
        source,
        target,
        timeout_seconds=timeout_seconds,
    )


def retained_marker_path(workspace: Path) -> Path:
    """返回保留工作区对应的外部标记文件路径。"""

    target = workspace.resolve()
    return target.parent / f".{target.name}.retained.json"


def worktree_starting_head(workspace: Path) -> str | None:
    """读取临时 Git 工作区首次创建时记录的基线提交。"""

    marker = retained_marker_path(workspace)
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None
    value = payload.get("starting_head")
    return str(value) if value else None


def worktree_head(workspace: Path, *, timeout_seconds: int = 600) -> str:
    """返回临时 Git 工作区当前 HEAD 提交。"""

    result = _run_git(
        ["-C", str(workspace.resolve()), "rev-parse", "HEAD"],
        check=False,
        timeout_seconds=timeout_seconds,
    )
    if result.returncode != 0 or not result.stdout.strip():
        raise WorkspaceError("无法读取临时 worktree 的 HEAD")
    return result.stdout.strip()


def worktree_ref_head(
    workspace: Path,
    ref: str,
    *,
    timeout_seconds: int = 600,
) -> str:
    """返回 Git 工作区可见的指定引用对应提交。"""

    result = _run_git(
        [
            "-C",
            str(workspace.resolve()),
            "rev-parse",
            "--verify",
            f"{ref}^{{commit}}",
        ],
        check=False,
        timeout_seconds=timeout_seconds,
        operation="解析目标分支提交",
    )
    if result.returncode != 0 or not result.stdout.strip():
        raise WorkspaceError(f"无法读取目标分支引用 {ref} 对应的提交")
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
    """为运行中的 Git 工作区写入崩溃后仍可生效的清理标记。"""

    marker = retained_marker_path(workspace)
    marker.parent.mkdir(parents=True, exist_ok=True)
    now = time.time()
    marker.write_text(
        json.dumps(
            {
                "workspace": str(workspace.resolve()),
                "workspace_kind": run_workspace_kind(workspace),
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
    """写入保留标记，确保异常 Git 工作区可在期限内恢复。"""

    marker = retained_marker_path(workspace)
    marker.parent.mkdir(parents=True, exist_ok=True)
    now = time.time()
    starting_head = worktree_starting_head(workspace)
    try:
        workspace_kind: str = run_workspace_kind(workspace)
    except WorkspaceError:
        # Agent 破坏 Git 元数据时仍保留外部标记，但绝不自动删除未知目录。
        workspace_kind = "unknown"
    marker.write_text(
        json.dumps(
            {
                "workspace": str(workspace.resolve()),
                "workspace_kind": workspace_kind,
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


def _remove_worktree(
    source_workspace: Path,
    workspace: Path,
    *,
    timeout_seconds: int = 600,
) -> None:
    """通过 Git 删除临时 worktree，并同步清除其保留标记。"""

    source = source_workspace.resolve()
    target = validate_linked_workspace(
        source,
        workspace,
        timeout_seconds=timeout_seconds,
    )
    result = _run_git(
        ["-C", str(source), "worktree", "remove", "--force", str(target)],
        check=False,
        timeout_seconds=timeout_seconds,
    )
    if result.returncode != 0:
        raise WorkspaceError("Git 无法删除临时 worktree")
    clear_retained_marker(target)
    _run_git(
        ["-C", str(source), "worktree", "prune"],
        check=False,
        timeout_seconds=timeout_seconds,
    )


def _remove_run_workspace(
    source_workspace: Path,
    workspace: Path,
    *,
    timeout_seconds: int = 600,
) -> None:
    """按工作区类型删除已验证的 linked worktree 或独立 clone。"""

    kind = run_workspace_kind(workspace)
    if kind == "worktree":
        _remove_worktree(
            source_workspace,
            workspace,
            timeout_seconds=timeout_seconds,
        )
        return
    target = validate_isolated_clone(
        source_workspace,
        workspace,
        timeout_seconds=timeout_seconds,
    )
    remove_tree(target)
    clear_retained_marker(target)


def cleanup_run_worktree(
    source_workspace: Path,
    workspace: Path,
    *,
    run_status: str,
    starting_head: str,
    retention_days: int,
    git_timeout_seconds: int = 600,
) -> WorkspaceCleanupResult:
    """仅在没有丢失本地工作的风险时删除本次运行 Git 工作区。"""

    target = workspace.resolve()
    try:
        validate_run_workspace(
            source_workspace,
            target,
            timeout_seconds=git_timeout_seconds,
        )
    except WorkspaceError as exc:
        return _retain_worktree(
            target,
            f"工作区归属校验失败，为避免误删而保留：{exc}",
            retention_days,
        )
    if run_status != "completed":
        return _retain_worktree(
            target,
            f"Agent 运行状态为 {run_status}，保留现场用于排查或恢复",
            retention_days,
        )

    status = _run_git(
        ["-C", str(target), "status", "--porcelain", "--untracked-files=all"],
        check=False,
        timeout_seconds=git_timeout_seconds,
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
        timeout_seconds=git_timeout_seconds,
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
        _run_git(
            ["-C", str(source), "fetch", "--prune", "origin"],
            check=False,
            timeout_seconds=git_timeout_seconds,
        )
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
            timeout_seconds=git_timeout_seconds,
        )
        if remote_refs.returncode != 0 or not remote_refs.stdout.strip():
            return _retain_worktree(
                target,
                "工作区包含尚未确认已推送到 origin 的提交",
                retention_days,
            )

    try:
        _remove_run_workspace(
            source_workspace,
            target,
            timeout_seconds=git_timeout_seconds,
        )
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
    git_timeout_seconds: int = 600,
) -> list[Path]:
    """强制清理由保留期限控制且已经过期的临时 Git 工作区。"""

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
        if target.parent != worktrees_root.resolve():
            # 标记只能管理同目录下的运行工作区，避免越界删除。
            continue
        if not target.exists():
            marker.unlink(missing_ok=True)
            continue
        try:
            _remove_run_workspace(
                source_workspace,
                target,
                timeout_seconds=git_timeout_seconds,
            )
        except Exception:
            continue
        removed.append(target)
    return removed


def prepare_change_request_workspace(
    provider: ProviderConfig,
    repository: RepositoryConfig,
    snapshot: ChangeRequestSnapshot,
    *,
    timeout_seconds: int = 600,
    initialization_timeout_seconds: int = 1800,
    cancel_check: GitCancelCheck | None = None,
    progress_callback: GitProgressCallback | None = None,
) -> str:
    """更新远端引用并准备 MR/PR head，但不切换用户当前分支。"""

    workspace = ensure_repository_workspace(
        provider,
        repository,
        timeout_seconds=timeout_seconds,
        initialization_timeout_seconds=initialization_timeout_seconds,
        cancel_check=cancel_check,
        progress_callback=progress_callback,
    )
    _run_git(
        ["-C", str(workspace), "fetch", "--prune", "origin"],
        timeout_seconds=timeout_seconds,
        operation="更新远端引用",
        cancel_check=cancel_check,
        progress_callback=progress_callback,
    )
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
        timeout_seconds=timeout_seconds,
        operation="获取变更请求引用",
        cancel_check=cancel_check,
        progress_callback=progress_callback,
    )
    current_head = ""
    if fetch_result.returncode == 0:
        current_ref = _run_git(
            ["-C", str(workspace), "rev-parse", destination_ref],
            check=False,
            timeout_seconds=timeout_seconds,
            operation="读取变更请求当前提交",
            cancel_check=cancel_check,
            progress_callback=progress_callback,
        )
        if current_ref.returncode == 0:
            current_head = current_ref.stdout.strip()

    existing_commit = _run_git(
        ["-C", str(workspace), "cat-file", "-e", f"{snapshot.head_sha}^{{commit}}"],
        check=False,
        timeout_seconds=timeout_seconds,
        operation="校验事件快照提交",
        cancel_check=cancel_check,
        progress_callback=progress_callback,
    )
    if existing_commit.returncode != 0:
        _run_git(
            ["-C", str(workspace), "fetch", "origin", snapshot.head_sha],
            check=False,
            timeout_seconds=timeout_seconds,
            operation="获取事件快照提交",
            cancel_check=cancel_check,
            progress_callback=progress_callback,
        )
        existing_commit = _run_git(
            ["-C", str(workspace), "cat-file", "-e", f"{snapshot.head_sha}^{{commit}}"],
            check=False,
            timeout_seconds=timeout_seconds,
            operation="复核事件快照提交",
            cancel_check=cancel_check,
            progress_callback=progress_callback,
        )
    if existing_commit.returncode != 0:
        if current_head and current_head != snapshot.head_sha:
            raise WorkspaceSnapshotSuperseded(snapshot.head_sha, current_head)
        raise WorkspaceError(
            "无法获取 MR/PR 事件快照提交，请检查远端权限、平台引用格式或提交是否仍可访问"
        )

    _run_git(
        [
            "-C",
            str(workspace),
            "update-ref",
            destination_ref,
            snapshot.head_sha,
        ],
        timeout_seconds=timeout_seconds,
        operation="固定变更请求事件提交",
        cancel_check=cancel_check,
        progress_callback=progress_callback,
    )
    return destination_ref


@contextmanager
def temporary_change_request_worktree(
    provider: ProviderConfig,
    repository: RepositoryConfig,
    snapshot: ChangeRequestSnapshot,
    *,
    timeout_seconds: int = 600,
    initialization_timeout_seconds: int = 1800,
    cancel_check: GitCancelCheck | None = None,
    progress_callback: GitProgressCallback | None = None,
) -> Iterator[Path]:
    """在临时 worktree 中检出准确的 MR/PR Head，并在退出时清理。"""

    prepare_change_request_workspace(
        provider,
        repository,
        snapshot,
        timeout_seconds=timeout_seconds,
        initialization_timeout_seconds=initialization_timeout_seconds,
        cancel_check=cancel_check,
        progress_callback=progress_callback,
    )
    workspace = repository.workspace.resolve()
    with temporary_directory(
        directory=workspace.parent,
        prefix=f".{workspace.name}.preflight-",
    ) as temporary_root:
        checkout = temporary_root / "checkout"
        _run_git(
            [
                "-C",
                str(workspace),
                "worktree",
                "add",
                "--detach",
                str(checkout),
                snapshot.head_sha,
            ],
            timeout_seconds=timeout_seconds,
            operation="创建 Preflight 工作区",
            cancel_check=cancel_check,
            progress_callback=progress_callback,
        )
        try:
            actual_head = _run_git(
                ["-C", str(checkout), "rev-parse", "HEAD"],
                timeout_seconds=timeout_seconds,
                operation="校验 Preflight 提交",
                cancel_check=cancel_check,
                progress_callback=progress_callback,
            ).stdout.strip()
            if actual_head != snapshot.head_sha:
                raise WorkspaceError(
                    f"临时工作目录 Head 不匹配：期望 {snapshot.head_sha}，实际 {actual_head}"
                )
            _run_git(
                [
                    "-C",
                    str(checkout),
                    "submodule",
                    "update",
                    "--init",
                    "--recursive",
                ],
                timeout_seconds=timeout_seconds,
                operation="初始化 Preflight 子模块",
                cancel_check=cancel_check,
                progress_callback=progress_callback,
            )
            yield checkout
        finally:
            _run_git(
                [
                    "-C",
                    str(workspace),
                    "worktree",
                    "remove",
                    "--force",
                    str(checkout),
                ],
                check=False,
                timeout_seconds=timeout_seconds,
                operation="清理 Preflight 工作区",
            )
            _run_git(
                ["-C", str(workspace), "worktree", "prune"],
                check=False,
            )


def prepare_default_branch_workspace(
    provider: ProviderConfig,
    repository: RepositoryConfig,
    *,
    timeout_seconds: int = 600,
    initialization_timeout_seconds: int = 1800,
    cancel_check: GitCancelCheck | None = None,
    progress_callback: GitProgressCallback | None = None,
) -> tuple[Path, str, str]:
    """更新基础仓库，并解析远端默认分支及其最新提交。"""

    workspace, _ = initialize_repository_workspace(
        provider,
        repository,
        timeout_seconds=timeout_seconds,
        initialization_timeout_seconds=initialization_timeout_seconds,
        cancel_check=cancel_check,
        progress_callback=progress_callback,
    )
    symbolic = _run_git(
        [
            "-C",
            str(workspace),
            "symbolic-ref",
            "--quiet",
            "--short",
            "refs/remotes/origin/HEAD",
        ],
        check=False,
        timeout_seconds=timeout_seconds,
        operation="识别远端默认分支",
        cancel_check=cancel_check,
        progress_callback=progress_callback,
    )
    if symbolic.returncode != 0 or not symbolic.stdout.strip():
        _run_git(
            ["-C", str(workspace), "remote", "set-head", "origin", "--auto"],
            check=False,
            timeout_seconds=timeout_seconds,
            operation="同步远端默认分支",
            cancel_check=cancel_check,
            progress_callback=progress_callback,
        )
        symbolic = _run_git(
            [
                "-C",
                str(workspace),
                "symbolic-ref",
                "--quiet",
                "--short",
                "refs/remotes/origin/HEAD",
            ],
            check=False,
            timeout_seconds=timeout_seconds,
            operation="读取远端默认分支",
            cancel_check=cancel_check,
            progress_callback=progress_callback,
        )
    remote_ref = symbolic.stdout.strip()
    if not remote_ref:
        for candidate in ("origin/main", "origin/master", "HEAD"):
            exists = _run_git(
                ["-C", str(workspace), "rev-parse", "--verify", candidate],
                check=False,
                timeout_seconds=timeout_seconds,
                operation="查找默认分支提交",
                cancel_check=cancel_check,
                progress_callback=progress_callback,
            )
            if exists.returncode == 0 and exists.stdout.strip():
                remote_ref = candidate
                break
    if not remote_ref:
        raise WorkspaceError("无法识别仓库远端默认分支")
    head_sha = _run_git(
        ["-C", str(workspace), "rev-parse", remote_ref],
        timeout_seconds=timeout_seconds,
        operation="读取默认分支提交",
        cancel_check=cancel_check,
        progress_callback=progress_callback,
    ).stdout.strip()
    branch = (
        remote_ref.removeprefix("origin/")
        if remote_ref.startswith("origin/")
        else remote_ref
    )
    return workspace, branch, head_sha


@contextmanager
def temporary_default_branch_worktree(
    provider: ProviderConfig,
    repository: RepositoryConfig,
    *,
    timeout_seconds: int = 600,
    initialization_timeout_seconds: int = 1800,
    cancel_check: GitCancelCheck | None = None,
    progress_callback: GitProgressCallback | None = None,
) -> Iterator[tuple[Path, str, str]]:
    """更新基础仓库，并在临时 worktree 检出远端默认分支最新提交。"""

    workspace, branch, head_sha = prepare_default_branch_workspace(
        provider,
        repository,
        timeout_seconds=timeout_seconds,
        initialization_timeout_seconds=initialization_timeout_seconds,
        cancel_check=cancel_check,
        progress_callback=progress_callback,
    )
    with temporary_directory(
        directory=workspace.parent,
        prefix=f".{workspace.name}.manual-preflight-",
    ) as temporary_root:
        checkout = temporary_root / "checkout"
        _run_git(
            [
                "-C",
                str(workspace),
                "worktree",
                "add",
                "--detach",
                str(checkout),
                head_sha,
            ],
            timeout_seconds=timeout_seconds,
            operation="创建手动 CI 工作区",
            cancel_check=cancel_check,
            progress_callback=progress_callback,
        )
        try:
            _run_git(
                [
                    "-C",
                    str(checkout),
                    "submodule",
                    "update",
                    "--init",
                    "--recursive",
                ],
                timeout_seconds=timeout_seconds,
                operation="初始化子模块",
                cancel_check=cancel_check,
                progress_callback=progress_callback,
            )
            yield checkout, branch, head_sha
        finally:
            _run_git(
                [
                    "-C",
                    str(workspace),
                    "worktree",
                    "remove",
                    "--force",
                    str(checkout),
                ],
                check=False,
                timeout_seconds=timeout_seconds,
                operation="清理手动 CI 工作区",
            )
            _run_git(
                ["-C", str(workspace), "worktree", "prune"],
                check=False,
            )
