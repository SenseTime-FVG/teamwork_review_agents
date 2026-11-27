"""Agent 工作区准备产物的仓库级指纹快照。"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import platform
import re
import subprocess
import sys
import tarfile
import threading
import time
import uuid
from functools import lru_cache
from pathlib import Path, PurePosixPath
from typing import Any, Callable

from .config import AppConfig, RepositoryConfig
from .filesystem import remove_tree
from .models import stable_hash
from .preflight_cache import repository_cache_root
from .process_control import hidden_process_options
from .subprocess_utils import resolve_executable
from .workspace_python import relocate_python_script_paths


SNAPSHOT_FORMAT_VERSION = 1
MAX_SNAPSHOTS_PER_REPOSITORY = 3
MAX_SNAPSHOT_BYTES_PER_REPOSITORY = 5 * 1024 * 1024 * 1024
SNAPSHOT_DIRECTORY_NAME = "workspace-snapshots"
METADATA_FILE_NAME = "metadata.json"
ARCHIVE_FILE_NAME = "artifacts.tar"
_LOCK = threading.RLock()

DEPENDENCY_FILE_NAMES = frozenset(
    {
        "package.json",
        "package-lock.json",
        "npm-shrinkwrap.json",
        "pnpm-lock.yaml",
        "pnpm-workspace.yaml",
        "yarn.lock",
        "bun.lock",
        "bun.lockb",
        "pyproject.toml",
        "uv.lock",
        "poetry.lock",
        "pdm.lock",
        "Pipfile",
        "Pipfile.lock",
        "Cargo.toml",
        "Cargo.lock",
        "go.mod",
        "go.sum",
        "pom.xml",
        "gradle.lockfile",
        "packages.lock.json",
        "composer.json",
        "composer.lock",
        "deno.json",
        "deno.jsonc",
        "deno.lock",
    }
)
DEPENDENCY_FILE_PATTERNS = ("requirements*.txt",)
IGNORED_SCAN_DIRECTORIES = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        "node_modules",
        ".venv",
        "venv",
        "dist",
        "build",
        "target",
        ".tox",
        ".mypy_cache",
        ".pytest_cache",
    }
)


class WorkspaceSnapshotError(RuntimeError):
    """表示依赖快照无法安全创建或恢复。"""


class WorkspaceSnapshotCancelled(WorkspaceSnapshotError):
    """表示用户在快照创建或恢复期间取消了当前操作。"""


def workspace_snapshot_root(
    config: AppConfig,
    repository: RepositoryConfig,
) -> Path:
    """返回当前仓库独占的工作区快照根目录。"""

    return repository_cache_root(config, repository) / SNAPSHOT_DIRECTORY_NAME


def _step_payload(repository: RepositoryConfig) -> list[dict[str, Any]]:
    """把准备步骤转换为稳定且不含 Secret 的指纹输入。"""

    return [
        {
            "name": step.name,
            "cwd": step.cwd,
            "command": list(step.command),
            "timeout_seconds": step.timeout_seconds,
        }
        for step in repository.agent_workspace.prepare_steps
    ]


def _program_payload(
    repository: RepositoryConfig,
    environment: dict[str, str],
) -> list[dict[str, Any]]:
    """记录准备程序的解析结果和文件版本代理信息。"""

    payload: list[dict[str, Any]] = []
    for step in repository.agent_workspace.prepare_steps:
        program = resolve_executable(step.command[0], environment)
        candidate = Path(program).expanduser()
        item: dict[str, Any] = {"command": step.command[0], "resolved": program}
        try:
            resolved = candidate.resolve(strict=True)
            stat = resolved.stat()
            item.update(
                {
                    "resolved": str(resolved),
                    "size": stat.st_size,
                    "mtime_ns": stat.st_mtime_ns,
                }
            )
        except OSError:
            # 程序不存在时仍保留解析文本，后续真实执行会给出明确错误。
            pass
        payload.append(item)
    return payload


def workspace_preparation_signature(
    repository: RepositoryConfig,
    environment: dict[str, str],
) -> str:
    """计算不依赖具体分支锁文件的准备配置签名。"""

    return stable_hash(
        SNAPSHOT_FORMAT_VERSION,
        _step_payload(repository),
        repository.agent_workspace.python_venv,
        repository.agent_workspace.python_check_modules,
        platform.system(),
        platform.machine(),
        platform.release(),
        sys.version_info[:3],
        _program_payload(repository, environment),
    )


def _hash_file(path: Path) -> str:
    """流式计算文件摘要，避免大型锁文件一次性进入内存。"""

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _dependency_descriptors(workspace: Path) -> list[dict[str, Any]]:
    """收集仓库中的常见依赖描述文件，不进入安装产物目录。"""

    resolved = workspace.resolve()
    descriptors: list[dict[str, Any]] = []
    for root, directories, files in os.walk(resolved, followlinks=False):
        directories[:] = sorted(
            name
            for name in directories
            if name not in IGNORED_SCAN_DIRECTORIES
            and not (Path(root) / name).is_symlink()
        )
        for name in sorted(files):
            if name not in DEPENDENCY_FILE_NAMES and not any(
                fnmatch.fnmatch(name, pattern)
                for pattern in DEPENDENCY_FILE_PATTERNS
            ):
                continue
            path = Path(root) / name
            if path.is_symlink() or not path.is_file():
                continue
            relative = path.relative_to(resolved).as_posix()
            descriptors.append(
                {
                    "path": relative,
                    "size": path.stat().st_size,
                    "sha256": _hash_file(path),
                }
            )
    return descriptors


def _git_output(workspace: Path, *arguments: str, input: bytes | None = None) -> bytes:
    """只读本地 Git 对象，限制等待时间，不把仓库原始错误正文带入页面。"""

    try:
        result = subprocess.run(
            ["git", "-C", str(workspace), *arguments], input=input,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            timeout=10, check=False, **hidden_process_options(),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise WorkspaceSnapshotError("无法读取默认分支依赖对象") from exc
    if result.returncode != 0:
        raise WorkspaceSnapshotError("默认分支依赖对象尚不可读取")
    return result.stdout


def _cached_default_branch_head(workspace: Path) -> str | None:
    """沿用预热的默认分支回退顺序，只解析已有引用，不联网或变更检出。"""

    for reference in ("refs/remotes/origin/HEAD", "origin/main", "origin/master", "HEAD"):
        try:
            return _git_output(workspace, "rev-parse", "--verify", f"{reference}^{{commit}}").decode().strip()
        except WorkspaceSnapshotError:
            continue
    return None


@lru_cache(maxsize=64)
def _commit_dependency_records(workspace: str, commit: str) -> tuple[tuple[str, int, str], ...]:
    """按不可变提交缓存依赖摘要；批量读取 blob，避免状态轮询逐文件启动进程。"""

    root = Path(workspace)
    blobs: list[tuple[str, str]] = []
    records: list[tuple[str, int, str]] = []
    for entry in _git_output(root, "ls-tree", "-rz", commit).split(b"\0"):
        if not entry:
            continue
        metadata, raw_path = entry.split(b"\t", 1)
        mode, kind, object_id = metadata.split()
        path = raw_path.decode("utf-8", errors="surrogateescape")
        parts = PurePosixPath(path).parts
        if any(part in IGNORED_SCAN_DIRECTORIES for part in parts[:-1]) or (
            kind == b"commit" and parts[-1] in IGNORED_SCAN_DIRECTORIES
        ):
            continue
        if kind == b"commit":
            # 子模块也按父提交记录的版本读取，不能采用基础目录当前检出的版本。
            if not (root / path).resolve().is_relative_to(root.resolve()):
                raise WorkspaceSnapshotError("默认分支子模块目录逃逸基础仓库")
            nested = _commit_dependency_records(str(root / path), object_id.decode())
            records.extend((f"{path}/{name}", size, digest) for name, size, digest in nested)
        elif kind == b"blob" and mode != b"120000" and (
            parts[-1] in DEPENDENCY_FILE_NAMES
            or any(fnmatch.fnmatch(parts[-1], pattern) for pattern in DEPENDENCY_FILE_PATTERNS)
        ):
            blobs.append((path, object_id.decode()))
    if blobs:
        identifiers = list(dict.fromkeys(oid for _, oid in blobs))
        output = _git_output(root, "cat-file", "--batch", input="".join(f"{oid}\n" for oid in identifiers).encode())
        offset = 0
        objects: dict[str, tuple[int, str]] = {}
        for identifier in identifiers:
            end = output.find(b"\n", offset)
            header = output[offset:end].split() if end >= 0 else []
            if len(header) != 3 or header[0].decode() != identifier or header[1] != b"blob":
                raise WorkspaceSnapshotError("默认分支依赖对象返回格式异常")
            size = int(header[2])
            content = output[end + 1:end + 1 + size]
            if len(content) != size:
                raise WorkspaceSnapshotError("默认分支依赖对象内容不完整")
            objects[identifier] = (size, hashlib.sha256(content).hexdigest())
            offset = end + size + 2
        records.extend((path, *objects[oid]) for path, oid in blobs)
    # 与 os.walk 的目录优先遍历和同目录文件排序一致，保证真实检出可命中同一指纹。
    records.sort(key=lambda item: (PurePosixPath(item[0]).parts[:-1], PurePosixPath(item[0]).name))
    return tuple(records)


def workspace_snapshot_fingerprint(
    repository: RepositoryConfig,
    environment: dict[str, str],
    *,
    commit: str | None = None,
) -> tuple[str, str]:
    """返回完整准备指纹和便于状态比较的配置签名。"""

    signature = workspace_preparation_signature(repository, environment)
    fingerprint = stable_hash(
        signature,
        _dependency_descriptors(repository.workspace) if commit is None else [
            {"path": path, "size": size, "sha256": digest}
            for path, size, digest in _commit_dependency_records(str(repository.workspace.resolve()), commit)
        ],
    )
    return fingerprint, signature


def _snapshot_directory(root: Path, fingerprint: str) -> Path:
    """返回一个指纹对应的快照目录。"""

    return root / fingerprint


def _read_metadata(directory: Path) -> dict[str, Any] | None:
    """读取并基础校验快照元数据。"""

    try:
        payload = json.loads(
            (directory / METADATA_FILE_NAME).read_text(encoding="utf-8")
        )
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or payload.get("invalidated_by_migration"):
        return None
    archive = directory / ARCHIVE_FILE_NAME
    if not archive.is_file() or payload.get("fingerprint") != directory.name:
        return None
    return payload


def inspect_workspace_snapshots(
    config: AppConfig,
    repository: RepositoryConfig,
    environment: dict[str, str] | None = None,
    *,
    default_branch: bool = False,
) -> dict[str, Any]:
    """返回仓库页使用的持久快照摘要。"""

    root = workspace_snapshot_root(config, repository)
    items: list[dict[str, Any]] = []
    if root.is_dir():
        for directory in root.iterdir():
            if not directory.is_dir() or directory.name.startswith("."):
                continue
            metadata = _read_metadata(directory)
            if metadata is not None:
                items.append(metadata)
    items.sort(
        key=lambda item: float(item.get("last_used_at") or item.get("created_at") or 0),
        reverse=True,
    )
    active_environment = environment or dict(os.environ)
    signature = (
        workspace_preparation_signature(repository, active_environment)
        if repository.agent_workspace.prepare_steps
        else None
    )
    fingerprint: str | None = None
    try:
        if repository.agent_workspace.prepare_steps and repository.workspace.is_dir():
            commit = _cached_default_branch_head(repository.workspace) if default_branch else None
            if not default_branch or commit is not None:
                fingerprint, _ = workspace_snapshot_fingerprint(
                    repository, active_environment, commit=commit,
                )
    except (OSError, WorkspaceSnapshotError):
        # 基础仓库尚未就绪时仍可展示已有快照，只是不宣称当前指纹命中。
        fingerprint = None
    exact = [item for item in items if item.get("fingerprint") == fingerprint]
    matching_signature = [
        item for item in items if item.get("preparation_signature") == signature
    ]
    latest = (
        exact[0]
        if exact
        else matching_signature[0]
        if matching_signature
        else items[0]
        if items
        else None
    )
    status = (
        "disabled"
        if not repository.agent_workspace.cache_enabled
        else "unconfigured"
        if not repository.agent_workspace.prepare_steps
        else "ready"
        if exact
        else "outdated"
        if items
        else "uninitialized"
    )
    return {
        "status": status,
        "current_fingerprint": fingerprint,
        "snapshot_count": len(items),
        "total_size_bytes": sum(int(item.get("size_bytes") or 0) for item in items),
        "latest": latest,
    }


def _run_git_paths(workspace: Path, *, ignored: bool) -> set[str]:
    """读取 Git 未跟踪或忽略文件，供通用准备产物归档使用。"""

    command = [
        "git",
        "-C",
        str(workspace.resolve()),
        "ls-files",
        "-z",
        "--others",
        "--exclude-standard",
    ]
    if ignored:
        command.append("--ignored")
    process = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        **hidden_process_options(),
    )
    if process.returncode != 0:
        raise WorkspaceSnapshotError("无法读取准备步骤产生的未跟踪文件")
    return {
        item.decode("utf-8", errors="surrogateescape")
        for item in process.stdout.split(b"\0")
        if item
    }


def _artifact_paths(workspace: Path) -> list[Path]:
    """返回准备步骤产生的可复用工作区文件。"""

    resolved = workspace.resolve()
    paths = _run_git_paths(resolved, ignored=False) | _run_git_paths(
        resolved,
        ignored=True,
    )
    artifacts: list[Path] = []
    for relative_text in sorted(paths):
        relative = Path(relative_text)
        if relative.is_absolute() or ".." in relative.parts or ".git" in relative.parts:
            continue
        candidate = resolved / relative
        if candidate.is_file() or candidate.is_symlink():
            artifacts.append(relative)
    return artifacts


def _workspace_head(workspace: Path) -> str | None:
    """读取快照来源提交，仅用于审计元数据。"""

    process = subprocess.run(
        ["git", "-C", str(workspace.resolve()), "rev-parse", "HEAD"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        check=False,
        **hidden_process_options(),
    )
    value = process.stdout.strip()
    return value if process.returncode == 0 and value else None


def _python_entry(member: tarfile.TarInfo, repository: RepositoryConfig) -> bool:
    """只识别显式配置虚拟环境入口内的 Python 名称，不能借此放行普通外链。"""

    relative = repository.agent_workspace.python_venv
    path = PurePosixPath(member.name)
    return bool(
        relative and path.parent == PurePosixPath(relative) / "bin"
        and re.fullmatch(r"python(?:\d+(?:\.\d+)*)?", path.name)
    )


def _trusted_python_targets(repository: RepositoryConfig, environment: dict[str, str] | None) -> frozenset[Path]:
    """信任当前基础 Python 和用户准备命令解析到的 Python，不采信归档提供的白名单。"""

    targets = {Path(getattr(sys, "_base_executable", None) or sys.executable).resolve()}
    for step in repository.agent_workspace.prepare_steps:
        if not re.fullmatch(r"python(?:\d+(?:\.\d+)*)?(?:\.exe)?", Path(step.command[0]).name):
            continue
        program = Path(resolve_executable(step.command[0], environment))
        if program.is_absolute() and program.is_file():
            targets.add(program.resolve())
    return frozenset(targets)


def _validate_member(
    member: tarfile.TarInfo, workspace: Path, *,
    python_entry: bool = False, trusted_python: frozenset[Path] = frozenset(),
) -> None:
    """拒绝归档路径和链接目标逃逸当前工作区。"""

    pure = PurePosixPath(member.name)
    if pure.is_absolute() or ".." in pure.parts or ".git" in pure.parts:
        raise WorkspaceSnapshotError("依赖快照包含不安全路径")
    if not (member.isfile() or member.isdir() or member.issym() or member.islnk()):
        raise WorkspaceSnapshotError("依赖快照包含不安全文件类型")
    destination = workspace.joinpath(*pure.parts)
    parent = destination.parent.resolve()
    try:
        parent.relative_to(workspace.resolve())
    except ValueError as exc:
        raise WorkspaceSnapshotError("依赖快照目标目录逃逸工作区") from exc
    if member.isfile() and destination.is_symlink():
        raise WorkspaceSnapshotError("依赖快照不能通过已有符号链接覆盖文件")
    if member.issym() or member.islnk():
        link = PurePosixPath(member.linkname)
        if link.is_absolute():
            if python_entry and member.issym() and Path(member.linkname).resolve() in trusted_python:
                return
            raise WorkspaceSnapshotError("依赖快照包含绝对链接")
        base = pure.parent if member.issym() else PurePosixPath(".")
        normalized: list[str] = []
        for part in (*base.parts, *link.parts):
            if part in {"", "."}:
                continue
            if part == "..":
                if not normalized:
                    raise WorkspaceSnapshotError("依赖快照链接逃逸工作区")
                normalized.pop()
            else:
                normalized.append(part)
        if member.islnk():
            target = workspace.joinpath(*normalized)
            if not target.is_file() or target.is_symlink() or not target.resolve().is_relative_to(workspace.resolve()):
                # 不让 tarfile 为缺失的硬链接目标递归提取成员，绕开延后的解释器链接检查。
                raise WorkspaceSnapshotError("依赖快照硬链接目标尚不存在或逃逸工作区")


def restore_workspace_snapshot(
    config: AppConfig,
    repository: RepositoryConfig,
    fingerprint: str,
    *,
    cancel_check: Callable[[], bool] | None = None,
    environment: dict[str, str] | None = None,
) -> dict[str, Any] | None:
    """安全恢复命中快照；不存在时返回空。"""

    root = workspace_snapshot_root(config, repository)
    directory = _snapshot_directory(root, fingerprint)
    with _LOCK:
        metadata = _read_metadata(directory)
        if metadata is None:
            return None
        archive = directory / ARCHIVE_FILE_NAME
        trusted_python = _trusted_python_targets(repository, environment)
        interpreter_links: list[tarfile.TarInfo] = []
        seen: set[str] = set()
        try:
            with tarfile.open(archive, mode="r") as bundle:
                for member in bundle:
                    if cancel_check is not None and cancel_check():
                        raise WorkspaceSnapshotCancelled("依赖快照恢复已取消")
                    name = PurePosixPath(member.name).as_posix()
                    if name in seen:
                        raise WorkspaceSnapshotError("依赖快照包含重复路径")
                    seen.add(name)
                    is_python = _python_entry(member, repository)
                    _validate_member(member, repository.workspace, python_entry=is_python, trusted_python=trusted_python)
                    if is_python and member.issym():
                        # 先完成普通成员写入，再创建解释器链接，防止后续成员穿过它写到宿主。
                        interpreter_links.append(member)
                        continue
                    bundle.extract(member, path=repository.workspace)
            for member in interpreter_links:
                if cancel_check is not None and cancel_check():
                    raise WorkspaceSnapshotCancelled("依赖快照恢复已取消")
                _validate_member(member, repository.workspace, python_entry=True, trusted_python=trusted_python)
                destination = repository.workspace.joinpath(*PurePosixPath(member.name).parts)
                destination.parent.mkdir(parents=True, exist_ok=True)
                if destination.is_dir():
                    raise WorkspaceSnapshotError("依赖快照解释器入口被目录占用")
                destination.unlink(missing_ok=True)
                # 使用当前已验证的规范路径，不能保留指向可被替换的中间绝对链接。
                target = str(Path(member.linkname).resolve()) if Path(member.linkname).is_absolute() else member.linkname
                destination.symlink_to(target)
            if repository.agent_workspace.python_venv:
                try:
                    relocate_python_script_paths(repository.workspace, repository.agent_workspace.python_venv)
                except ValueError as exc:
                    # 旧快照缺失或损坏的虚拟环境仍走原有重建流程，不能绕过恢复失败处理。
                    raise WorkspaceSnapshotError(f"依赖快照 Python 环境不可恢复：{exc}") from exc
        except (OSError, tarfile.TarError) as exc:
            raise WorkspaceSnapshotError(f"依赖快照损坏：{exc}") from exc
        metadata["last_used_at"] = time.time()
        temporary = directory / f".{METADATA_FILE_NAME}.{uuid.uuid4().hex}.tmp"
        temporary.write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(directory / METADATA_FILE_NAME)
        return metadata


def invalidate_workspace_snapshot(
    config: AppConfig,
    repository: RepositoryConfig,
    fingerprint: str,
) -> None:
    """删除一份已经确认损坏的快照。"""

    with _LOCK:
        remove_tree(
            _snapshot_directory(
                workspace_snapshot_root(config, repository),
                fingerprint,
            )
        )


def _cleanup_snapshots(root: Path, protected: Path) -> None:
    """按数量和总空间限制清理最近最少使用快照。"""

    items: list[tuple[Path, dict[str, Any]]] = []
    for directory in root.iterdir():
        if not directory.is_dir() or directory.name.startswith("."):
            continue
        metadata = _read_metadata(directory)
        if metadata is None:
            # 无法校验的目录既不能恢复，也不能绕过仓库级空间限制。
            remove_tree(directory)
            continue
        items.append((directory, metadata))
    items.sort(
        key=lambda item: float(
            item[1].get("last_used_at") or item[1].get("created_at") or 0
        ),
        reverse=True,
    )
    total = sum(int(metadata.get("size_bytes") or 0) for _, metadata in items)
    for index, (directory, metadata) in enumerate(items):
        over_count = index >= MAX_SNAPSHOTS_PER_REPOSITORY
        over_size = total > MAX_SNAPSHOT_BYTES_PER_REPOSITORY
        if directory == protected or (not over_count and not over_size):
            continue
        total -= int(metadata.get("size_bytes") or 0)
        remove_tree(directory)


def create_workspace_snapshot(
    config: AppConfig,
    repository: RepositoryConfig,
    fingerprint: str,
    preparation_signature: str,
    *,
    source_head: str | None = None,
    cancel_check: Callable[[], bool] | None = None,
    environment: dict[str, str] | None = None,
) -> dict[str, Any] | None:
    """把准备成功后的未跟踪产物原子保存为仓库级快照。"""

    artifacts = _artifact_paths(repository.workspace)
    if not artifacts:
        return None
    estimated_size = 0
    for relative in artifacts:
        candidate = repository.workspace / relative
        if candidate.is_file() and not candidate.is_symlink():
            estimated_size += candidate.stat().st_size
    if estimated_size > MAX_SNAPSHOT_BYTES_PER_REPOSITORY:
        raise WorkspaceSnapshotError("准备产物超过仓库级快照空间上限")
    resolved_source_head = source_head or _workspace_head(repository.workspace)

    root = workspace_snapshot_root(config, repository)
    target = _snapshot_directory(root, fingerprint)
    with _LOCK:
        existing = _read_metadata(target)
        if existing is not None:
            return existing
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = root / f".{fingerprint}.{uuid.uuid4().hex}.tmp"
        temporary.mkdir(mode=0o700)
        archive = temporary / ARCHIVE_FILE_NAME
        trusted_python = _trusted_python_targets(repository, environment)
        try:
            with tarfile.open(archive, mode="w", dereference=False) as bundle:
                for relative in artifacts:
                    if cancel_check is not None and cancel_check():
                        raise WorkspaceSnapshotCancelled("依赖快照创建已取消")
                    path = repository.workspace / relative
                    member = bundle.gettarinfo(path, arcname=relative.as_posix())
                    _validate_member(
                        member, repository.workspace,
                        python_entry=_python_entry(member, repository), trusted_python=trusted_python,
                    )
                    if member.isfile():
                        with path.open("rb") as handle:
                            bundle.addfile(member, handle)
                    else:
                        bundle.addfile(member)
            size_bytes = archive.stat().st_size
            if size_bytes > MAX_SNAPSHOT_BYTES_PER_REPOSITORY:
                raise WorkspaceSnapshotError("依赖快照超过仓库级空间上限")
            now = time.time()
            metadata = {
                "format_version": SNAPSHOT_FORMAT_VERSION,
                "fingerprint": fingerprint,
                "preparation_signature": preparation_signature,
                "created_at": now,
                "last_used_at": now,
                "size_bytes": size_bytes,
                "artifact_count": len(artifacts),
                "source_head": resolved_source_head,
                "steps": _step_payload(repository),
            }
            (temporary / METADATA_FILE_NAME).write_text(
                json.dumps(metadata, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            if target.exists():
                remove_tree(target)
            temporary.replace(target)
            _cleanup_snapshots(root, target)
            return metadata
        except Exception:
            remove_tree(temporary)
            raise
