"""仓库身份与本地目录的可恢复迁移，不改写历史日志和仓库内容。"""

from __future__ import annotations

import base64
import json
import os
import shutil
import sqlite3
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

from .config import AppConfig, RepositoryConfig
from .filesystem import remove_tree
from .models import stable_hash
from .preflight_cache import repository_cache_root
from .state import StateStore
from .workspace_snapshot import SNAPSHOT_DIRECTORY_NAME


REPOSITORY_TABLES = (
    "event_inbox",
    "provider_activity_cursors",
    "agent_runs",
    "preflight_runs",
    "preflight_failure_comments",
    "change_request_source_generations",
    "managed_comments",
)


def _atomic_write(path: Path, content: bytes, mode: int = 0o600) -> None:
    """同目录原子替换；恢复日志仅本人可读，其他文件保留原权限。"""

    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=path.parent, prefix=f".{path.name}.", delete=False
        ) as file:
            temporary = Path(file.name)
            os.chmod(temporary, mode)
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _journal_path(config_path: Path) -> Path:
    """每份配置至多允许一项尚未完成的迁移。"""

    return config_path.with_name(f".{config_path.name}.repository-migration.json")


def has_pending_repository_migration(config_path: Path) -> bool:
    """未完成恢复或清理时不能覆盖恢复日志，也不能继续使用半迁移目录。"""

    return _journal_path(config_path).exists()


def _mapped_path(value: str, moves: list[tuple[Path, Path]]) -> str:
    """只替换完整路径前缀，不误改相似名称或字符串中的仓库 ID。"""

    if not value:
        return value
    path = Path(value)
    for source, target in sorted(
        moves, key=lambda item: len(item[0].parts), reverse=True
    ):
        if path == source or source in path.parents:
            return str(target / path.relative_to(source))
    return value


def _overlaps(left: Path, right: Path) -> bool:
    """判断路径是否重合或互相包含。"""

    return left == right or left in right.parents or right in left.parents


def plan_repository_moves(
    config: AppConfig,
    old: RepositoryConfig,
    new: RepositoryConfig,
    config_path: Path,
) -> list[tuple[Path, Path]]:
    """规划基础仓库、运行目录和下载缓存，并拒绝覆盖或共享目录。"""

    data = config.database.path.parent.resolve()
    candidates = [
        (old.workspace, new.workspace),
        (
            data / "worktrees" / stable_hash(old.id)[:16],
            data / "worktrees" / stable_hash(new.id)[:16],
        ),
        (repository_cache_root(config, old), repository_cache_root(config, new)),
    ]
    for index, pair in enumerate(candidates):
        if any(
            _overlaps(a.resolve(), b.resolve())
            for other in candidates[index + 1 :]
            for a in pair
            for b in other
        ):
            raise ValueError("基础仓库、运行工作区和缓存目录不能互相包含")
    moves = []
    for source, target in candidates:
        if source.is_symlink() or target.is_symlink():
            raise ValueError("迁移的来源或目标目录不能是符号链接")
        source, target = source.resolve(), target.resolve()
        if source == target:
            continue
        if _overlaps(source, target):
            raise ValueError("迁移的来源与目标目录不能互相包含")
        for protected in (config_path, config.database.path, Path.home()):
            if (
                protected == source
                or source in protected.parents
                or protected == target
                or target in protected.parents
            ):
                raise ValueError("不能迁移包含配置、数据库或用户主目录的路径")
        if target.exists():
            raise ValueError(f"目标目录已存在，不能覆盖或合并：{target}")
        if source.exists() and not source.is_dir():
            raise ValueError(f"来源不是目录：{source}")
        for other in config.repositories:
            if other.id == old.id:
                continue
            other_paths = (
                other.workspace,
                repository_cache_root(config, other),
                data / "worktrees" / stable_hash(other.id)[:16],
            )
            if any(
                _overlaps(path.resolve(), endpoint)
                for path in other_paths
                for endpoint in (source, target)
            ):
                raise ValueError(f"迁移目录与仓库 {other.id} 的目录重叠")
        moves.append((source, target))
    for index, pair in enumerate(moves):
        if any(
            _overlaps(a, b) for other in moves[index + 1 :] for a in pair for b in other
        ):
            raise ValueError("基础仓库、运行工作区和缓存的迁移路径不能互相包含")
    if (old.workspace / ".git").is_file():
        raise ValueError(
            "基础目录是外部 Git worktree，请使用独立克隆作为基础仓库后再迁移"
        )
    if (
        old.workspace.is_dir()
        and not (old.workspace / ".git").is_dir()
        and any(old.workspace.iterdir())
    ):
        raise ValueError("基础目录非空且不是独立 Git 仓库，不能自动搬移其中的文件")
    return moves


def _assert_idle(connection: sqlite3.Connection, old_id: str, new_id: str) -> None:
    """事务内再次检查活动运行、事件和资源租约，避免搬走使用中的文件。"""

    for table, statuses in (
        ("agent_runs", ("queued", "preparing", "running")),
        ("preflight_runs", ("running",)),
        ("event_inbox", ("processing", "triggered")),
    ):
        placeholders = ",".join("?" for _ in statuses)
        if connection.execute(
            f"SELECT 1 FROM {table} WHERE status IN ({placeholders}) LIMIT 1", statuses
        ).fetchone():
            raise ValueError("后台有待执行或运行中的任务，请等待任务完成后再迁移仓库")
    if connection.execute(
        "SELECT 1 FROM resource_locks WHERE expires_at > ? LIMIT 1", (time.time(),)
    ).fetchone():
        raise ValueError("后台仍持有工作资源锁，请等待操作完成后再迁移仓库")
    if old_id != new_id:
        for table in REPOSITORY_TABLES:
            if connection.execute(
                f"SELECT 1 FROM {table} WHERE repository_id = ? LIMIT 1", (new_id,)
            ).fetchone():
                raise ValueError(f"仓库 ID {new_id} 已有关联历史，不能合并")
        if connection.execute(
            "SELECT 1 FROM snapshots WHERE json_extract(payload, '$.repository_id') = ? LIMIT 1",
            (new_id,),
        ).fetchone():
            raise ValueError(f"仓库 ID {new_id} 已有 PR 快照，不能合并")
        if connection.execute(
            "SELECT 1 FROM service_state WHERE state_key=?",
            (f"repository_scan:{new_id}",),
        ).fetchone():
            raise ValueError(f"仓库 ID {new_id} 已有扫描历史，不能合并")


def migrate_repository_state(
    connection: sqlite3.Connection,
    old: RepositoryConfig,
    new: RepositoryConfig,
    moves: list[tuple[Path, Path]],
) -> None:
    """迁移查询关联与当前操作路径，保留事件 ID、运行 ID 和原始审计数据。"""

    old_prefix, new_prefix = f"{old.provider}:{old.id}:", f"{old.provider}:{new.id}:"
    rows = connection.execute(
        """SELECT * FROM agent_runs WHERE repository_id = ? OR event_id IN
        (SELECT event_id FROM event_inbox WHERE repository_id = ?)
        OR substr(resource_key, 1, ?) = ?""",
        (old.id, old.id, len(old_prefix), old_prefix),
    ).fetchall()
    for row in rows:
        key = row["resource_key"]
        if key.startswith(old_prefix):
            key = new_prefix + key[len(old_prefix) :]
        context = json.loads(row["trigger_context"]) if row["trigger_context"] else None
        if context and context.get("repository_id") == old.id:
            context["repository_id"] = new.id
            if row["trigger_source"] == "schedule":
                key = f"schedule:{context['rule_name']}:{new.id}:{context['occurrence_id']}"
        connection.execute(
            "UPDATE agent_runs SET repository_id=?, resource_key=?, workspace_path=?, trigger_context=? WHERE run_id=?",
            (
                new.id,
                key,
                _mapped_path(row["workspace_path"], moves)
                if row["workspace_path"]
                else None,
                json.dumps(context, ensure_ascii=False)
                if context
                else row["trigger_context"],
                row["run_id"],
            ),
        )
    for row in connection.execute(
        "SELECT run_id, cache_path FROM preflight_runs WHERE repository_id=?", (old.id,)
    ).fetchall():
        if row["cache_path"]:
            connection.execute(
                "UPDATE preflight_runs SET cache_path=? WHERE run_id=?",
                (_mapped_path(row["cache_path"], moves), row["run_id"]),
            )
    if old.id == new.id:
        return
    for row in connection.execute(
        "SELECT snapshot_key, payload FROM snapshots WHERE json_extract(payload, '$.repository_id')=?",
        (old.id,),
    ).fetchall():
        payload = json.loads(row["payload"])
        payload["repository_id"] = new.id
        connection.execute(
            "UPDATE snapshots SET snapshot_key=?, payload=? WHERE snapshot_key=?",
            (
                f"{new.id}:{payload['number']}",
                json.dumps(payload, ensure_ascii=False),
                row["snapshot_key"],
            ),
        )
    for row in connection.execute(
        "SELECT event_id, payload FROM event_inbox WHERE repository_id=?", (old.id,)
    ).fetchall():
        payload = json.loads(row["payload"])
        payload["repository_id"] = new.id
        for field in ("old", "new", "current"):
            if (
                isinstance(payload.get(field), dict)
                and payload[field].get("repository_id") == old.id
            ):
                payload[field]["repository_id"] = new.id
        connection.execute(
            "UPDATE event_inbox SET payload=? WHERE event_id=?",
            (json.dumps(payload, ensure_ascii=False), row["event_id"]),
        )
    for table in REPOSITORY_TABLES:
        connection.execute(
            f"UPDATE {table} SET repository_id=? WHERE repository_id=?",
            (new.id, old.id),
        )
    connection.execute(
        "UPDATE service_state SET state_key=? WHERE state_key=?",
        (f"repository_scan:{new.id}", f"repository_scan:{old.id}"),
    )


def _validate_metadata_path(path: Path, root: Path) -> None:
    """受管元数据不能经由中间符号链接写到其他目录。"""

    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError(f"Git 或快照元数据超出受管目录：{path}")
    for component in (path, *path.parents):
        if component.is_symlink():
            raise ValueError(f"Git 或快照元数据路径不能包含符号链接：{path}")
        if component == root:
            break


def _metadata_edits(
    old: RepositoryConfig, moves: list[tuple[Path, Path]], data: Path, cache: Path
) -> list[dict[str, Any]]:
    """只修复 Git 管理文件、保留标记和快照元数据，不替换用户文件。"""

    edits: dict[Path, bytes] = {}
    worktrees = data / "worktrees" / stable_hash(old.id)[:16]
    if (
        (old.workspace / ".git").is_symlink()
        or worktrees.is_symlink()
        or cache.is_symlink()
        or (data / "worktrees").is_symlink()
        or (data / "preflight-cache").is_symlink()
    ):
        raise ValueError("仓库管理目录不能是符号链接")
    for marker in worktrees.glob(".*.retained.json"):
        if marker.is_symlink():
            raise ValueError("工作区保留标记不能是符号链接")
        payload = json.loads(marker.read_text(encoding="utf-8"))
        payload["workspace"] = _mapped_path(str(payload["workspace"]), moves)
        edits[marker] = json.dumps(payload, ensure_ascii=False, indent=2).encode()
    admin_root = old.workspace / ".git" / "worktrees"
    if admin_root.is_symlink() or (cache / SNAPSHOT_DIRECTORY_NAME).is_symlink():
        raise ValueError("Git 管理目录和依赖快照目录不能是符号链接")
    for admin in admin_root.glob("*"):
        gitdir = admin / "gitdir"
        if not gitdir.is_file():
            continue
        if admin.is_symlink() or gitdir.is_symlink():
            raise ValueError("Git worktree 管理路径不能是符号链接")
        _validate_metadata_path(gitdir, old.workspace)
        pointer = Path(gitdir.read_text(encoding="utf-8").strip())
        if not pointer.is_absolute():
            pointer = gitdir.parent / pointer
        if pointer.is_symlink():
            raise ValueError("Git worktree 入口不能是符号链接")
        pointer = pointer.resolve()
        if not pointer.exists():
            # 已被外部删除的工作区只修正登记路径，不重新创建其文件。
            edits[gitdir] = (_mapped_path(str(pointer), moves) + "\n").encode()
            continue
        if not pointer.is_relative_to(
            worktrees.resolve()
        ) and not pointer.is_relative_to(old.workspace):
            raise ValueError(
                f"基础仓库关联了外部 worktree，暂不能迁移：{pointer.parent}"
            )
        _validate_metadata_path(
            pointer, worktrees if pointer.is_relative_to(worktrees) else old.workspace
        )
        if pointer.name != ".git" or not pointer.is_file():
            raise ValueError(f"Git worktree 登记没有指向有效的 .git 文件：{pointer}")
        entry = pointer.read_text(encoding="utf-8").strip()
        linked_admin = Path(entry.removeprefix("gitdir: "))
        if not linked_admin.is_absolute():
            linked_admin = pointer.parent / linked_admin
        if (
            not entry.startswith("gitdir: ")
            or linked_admin.resolve() != admin.resolve()
        ):
            raise ValueError(f"Git worktree 双向登记不一致，请先修复：{pointer}")
        edits[gitdir] = (_mapped_path(str(pointer), moves) + "\n").encode()
        edits[pointer] = (
            "gitdir: " + _mapped_path(str(admin.resolve()), moves) + "\n"
        ).encode()
    for workspace in [old.workspace, *worktrees.glob("*")]:
        if workspace.is_symlink() or (workspace / ".git").is_symlink():
            raise ValueError("运行工作区及 Git 管理目录不能是符号链接")
        alternate = workspace / ".git" / "objects" / "info" / "alternates"
        if alternate.is_file():
            if alternate.is_symlink():
                raise ValueError("Git 对象引用文件不能是符号链接")
            _validate_metadata_path(alternate, workspace)
            edits[alternate] = (
                "\n".join(
                    _mapped_path(line, moves)
                    for line in alternate.read_text(encoding="utf-8").splitlines()
                )
                + "\n"
            ).encode()
    if moves:
        # 快照中的虚拟环境可能含旧绝对路径，标记失效后让准备步骤重建。
        for path in (cache / SNAPSHOT_DIRECTORY_NAME).glob("*/metadata.json"):
            if path.is_symlink() or path.parent.is_symlink():
                raise ValueError("依赖快照元数据不能是符号链接")
            _validate_metadata_path(path, cache)
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["invalidated_by_migration"] = True
            edits[path] = json.dumps(payload, ensure_ascii=False, indent=2).encode()
    return [
        {
            "old_path": str(path),
            "new_path": _mapped_path(str(path), moves),
            "before": base64.b64encode(path.read_bytes()).decode(),
            "after": base64.b64encode(content).decode(),
            "mode": path.stat().st_mode & 0o777,
        }
        for path, content in edits.items()
        if path.read_bytes() != content
    ]


def _finish_journal(
    config_path: Path, journal: dict[str, Any], *, committed: bool
) -> None:
    """根据数据库提交标记完成清理或恢复；重复执行也能安全完成。"""

    if committed:
        for move in journal["moves"]:
            source = Path(move["source"])
            if move["copy"] and source.exists():
                if (
                    source.is_symlink()
                    or _directory_identity(source) != move["source_identity"]
                ):
                    raise ValueError(f"原目录身份已变化，停止清理：{source}")
                target = Path(move["target"])
                if not target.exists() or _directory_identity(target) != move.get(
                    "target_identity"
                ):
                    raise ValueError(f"迁移目标缺失或发生变化，保留原目录：{source}")
                remove_tree(source)
    else:
        for move in reversed(journal["moves"]):
            source, target = Path(move["source"]), Path(move["target"])
            if not move["exists"]:
                continue
            if move["copy"]:
                if (
                    not source.exists()
                    or _directory_identity(source) != move["source_identity"]
                ):
                    raise ValueError(f"原目录缺失或发生变化，停止自动恢复：{source}")
                if target.exists() and _directory_identity(target) == move.get(
                    "target_identity"
                ):
                    remove_tree(target)
                staging = Path(move["staging"])
                if staging.exists():
                    remove_tree(staging)
            elif target.exists() and not source.exists():
                if (
                    target.is_symlink()
                    or _directory_identity(target) != move["source_identity"]
                ):
                    raise ValueError(f"迁移目标身份已变化，停止自动恢复：{target}")
                os.rename(target, source)
            if (
                not source.exists()
                or _directory_identity(source) != move["source_identity"]
            ):
                raise ValueError(f"无法安全恢复原目录：{source}")
        for edit in journal["edits"]:
            _atomic_write(
                Path(edit["old_path"]), base64.b64decode(edit["before"]), edit["mode"]
            )
        _atomic_write(
            config_path,
            base64.b64decode(journal["config_before"]),
            journal["config_mode"],
        )
        for parent in reversed(journal["parents"]):
            try:
                Path(parent).rmdir()
            except (FileNotFoundError, OSError):
                # 不删除并发用户操作创建的内容，只清理本次创建的空父目录。
                pass
    _journal_path(config_path).unlink(missing_ok=True)


def _directory_identity(path: Path) -> list[int]:
    """用目录对象身份区分本次复制结果与并发创建的同名目录。"""

    stat = path.stat()
    return [stat.st_dev, stat.st_ino]


def _needs_copy(source: Path, parent: Path) -> bool:
    """只有跨文件系统才复制，普通改名不产生第二份仓库。"""

    return source.exists() and source.stat().st_dev != parent.stat().st_dev


def recover_repository_migration(config_path: Path) -> None:
    """启动时优先恢复中断的迁移，再加载配置或恢复后台任务。"""

    path = _journal_path(config_path)
    if not path.exists():
        return
    journal = json.loads(path.read_text(encoding="utf-8"))
    with StateStore(journal["database"]).connect() as connection:
        committed = (
            connection.execute(
                "SELECT 1 FROM service_state WHERE state_key=?", (journal["marker"],)
            ).fetchone()
            is not None
        )
    _finish_journal(config_path, journal, committed=committed)


def migrate_repository(
    *,
    config_path: Path,
    store: StateStore,
    config: AppConfig,
    old: RepositoryConfig,
    new: RepositoryConfig,
    content: bytes,
    revision: str,
    masked_content: str,
    source: str,
) -> None:
    """在单个数据库事务与持久恢复日志保护下迁移仓库。"""

    if has_pending_repository_migration(config_path):
        raise ValueError("上一次仓库迁移尚未恢复，请重启服务完成恢复后再操作")
    moves = plan_repository_moves(config, old, new, config_path)
    edits = _metadata_edits(
        old,
        moves,
        config.database.path.parent.resolve(),
        repository_cache_root(config, old),
    )
    journal: dict[str, Any] = {
        "database": str(config.database.path),
        "marker": f"repository-migration:{uuid.uuid4().hex}",
        "config_before": base64.b64encode(config_path.read_bytes()).decode(),
        "config_mode": config_path.stat().st_mode & 0o777,
        "moves": [],
        "edits": edits,
        "parents": [],
    }
    for origin, target in moves:
        parent = target.parent
        missing = []
        while not parent.exists():
            missing.append(str(parent))
            parent = parent.parent
        journal["parents"].extend(
            item for item in reversed(missing) if item not in journal["parents"]
        )
        journal["moves"].append(
            {
                "source": str(origin),
                "target": str(target),
                "exists": origin.exists(),
                "source_identity": _directory_identity(origin)
                if origin.exists()
                else None,
                "copy": _needs_copy(origin, parent),
                "staging": str(
                    target.with_name(f".{target.name}.migration-{uuid.uuid4().hex}")
                ),
            }
        )
    committed = False
    try:
        with store.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            _assert_idle(connection, old.id, new.id)
            _atomic_write(
                _journal_path(config_path),
                json.dumps(journal, ensure_ascii=False).encode(),
            )
            for move in journal["moves"]:
                if not move["exists"]:
                    continue
                origin, target = Path(move["source"]), Path(move["target"])
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.exists():
                    raise ValueError(f"目标目录已存在，不能覆盖：{target}")
                if move["copy"]:
                    # 跨盘先复制到专属暂存目录；失败恢复不会删除别人创建的目标。
                    staging = Path(move["staging"])
                    staging.mkdir()
                    move["target_identity"] = _directory_identity(staging)
                    _atomic_write(
                        _journal_path(config_path),
                        json.dumps(journal, ensure_ascii=False).encode(),
                    )
                    shutil.copytree(origin, staging, symlinks=True, dirs_exist_ok=True)
                    if target.exists():
                        raise ValueError(f"目标目录已存在，不能覆盖：{target}")
                    os.rename(staging, target)
                else:
                    os.rename(origin, target)
            for edit in edits:
                _atomic_write(
                    Path(edit["new_path"]),
                    base64.b64decode(edit["after"]),
                    edit["mode"],
                )
            migrate_repository_state(connection, old, new, moves)
            _atomic_write(config_path, content, journal["config_mode"])
            connection.execute(
                "INSERT OR IGNORE INTO config_versions(revision, content, source, created_at) VALUES (?,?,?,?)",
                (revision, masked_content, source, time.time()),
            )
            connection.execute(
                "INSERT INTO service_state(state_key, payload, updated_at) VALUES (?, ?, ?)",
                (
                    journal["marker"],
                    json.dumps({"old_id": old.id, "new_id": new.id}),
                    time.time(),
                ),
            )
        committed = True
    finally:
        if _journal_path(config_path).exists():
            # 提交阶段发生异常时，以持久标记为准，避免将已经提交的数据配上旧目录。
            if not committed:
                with store.connect() as connection:
                    committed = (
                        connection.execute(
                            "SELECT 1 FROM service_state WHERE state_key=?",
                            (journal["marker"],),
                        ).fetchone()
                        is not None
                    )
            _finish_journal(config_path, journal, committed=committed)
