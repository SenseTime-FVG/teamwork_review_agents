"""受管依赖缓存的并行使用保护、最近使用时间与闲置回收。"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
from pathlib import Path
import re
import stat
import threading
import time
from typing import BinaryIO, Callable
import uuid

import portalocker

from .config import AppConfig
from .filesystem import _is_junction, remove_tree
from .locks import LockCancelledError, LockTimeoutError
from .preflight_cache import repository_cache_root
from .state import StateStore


USAGE_FILE = ".teamwork-cache-usage.json"
LOCK_DIRECTORY = ".usage-locks"
LOGGER = logging.getLogger(__name__)


def _plain_directory(path: Path) -> None:
    """受管入口不得为链接或联接，不能沿路径删除外部内容。"""

    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or _is_junction(info):
        raise ValueError("缓存路径不是普通目录，已跳过")


def _write_usage(root: Path) -> None:
    """使用开始与结束原子更新时间，不以包文件的旧时间推断使用情况。"""

    _plain_directory(root)
    temporary = root / f".cache-usage-{uuid.uuid4()}.tmp"
    try:
        with temporary.open("x", encoding="utf-8") as file:
            json.dump({"version": 1, "cache_name": root.name, "last_used_at": time.time()}, file)
        os.replace(temporary, root / USAGE_FILE)
    finally:
        temporary.unlink(missing_ok=True)


def _last_used(root: Path) -> float | None:
    """没有标记的旧缓存须先登记；损坏或异常标记不能变成过期证据。"""

    marker = root / USAGE_FILE
    try:
        info = marker.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode) or _is_junction(info) or info.st_size > 65536:
        raise ValueError("缓存使用标记不可信，已跳过")
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
        value = payload["last_used_at"]
        if (
            type(payload.get("version")) is not int or payload["version"] != 1 or payload.get("cache_name") != root.name
            or type(value) not in (int, float) or not math.isfinite(value) or value <= 0
        ):
            raise ValueError
        return float(value)
    except (ValueError, KeyError, TypeError, UnicodeError):
        raise ValueError("缓存使用标记无效，已跳过") from None


class RepositoryCacheUsage:
    """任务共享使用锁，清理独占锁；锁入口位于缓存外且不随删除重建。"""

    def __init__(
        self, root: Path | None, *, exclusive: bool = False,
        timeout_seconds: float = 300, cancel_check: Callable[[], bool] | None = None,
    ) -> None:
        self.root = root
        self.exclusive = exclusive
        self.timeout_seconds = timeout_seconds
        self.cancel_check = cancel_check
        self.file: BinaryIO | None = None

    async def __aenter__(self) -> "RepositoryCacheUsage":
        if self.root is None:
            return self
        root = self.root
        root.parent.mkdir(parents=True, exist_ok=True)
        _plain_directory(root.parent)
        lock_root = root.parent / LOCK_DIRECTORY
        lock_root.mkdir(exist_ok=True)
        _plain_directory(lock_root)
        lock_path = lock_root / f"{root.name}.lock"
        if lock_path.is_symlink():
            raise ValueError("缓存锁入口是链接，拒绝使用")
        descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
        self.file = os.fdopen(descriptor, "r+b")
        try:
            info = lock_path.lstat()
            opened = os.fstat(descriptor)
            if (
                not stat.S_ISREG(info.st_mode) or _is_junction(info)
                or (info.st_dev, info.st_ino) != (opened.st_dev, opened.st_ino)
            ):
                raise ValueError("缓存锁入口不是普通文件")
            deadline = time.monotonic() + self.timeout_seconds
            while True:
                if self.cancel_check is not None and self.cancel_check():
                    raise LockCancelledError("缓存使用已取消")
                try:
                    flags = portalocker.LOCK_EX if self.exclusive else portalocker.LOCK_SH
                    portalocker.lock(self.file, flags | portalocker.LOCK_NB)
                    break
                except portalocker.exceptions.LockException:
                    if time.monotonic() >= deadline:
                        raise LockTimeoutError("缓存正在使用或清理，已跳过") from None
                    await asyncio.sleep(0.05)
            if not self.exclusive:
                root.mkdir(exist_ok=True, mode=0o700)
                _write_usage(root)
            return self
        except BaseException:
            self.file.close()
            self.file = None
            raise

    async def __aexit__(self, *_: object) -> None:
        if self.file is None:
            return
        try:
            if not self.exclusive and self.root is not None:
                try:
                    _write_usage(self.root)
                except (OSError, ValueError):
                    LOGGER.warning("缓存最近使用时间回写失败，保留使用开始时间")
        finally:
            try:
                portalocker.unlock(self.file)
            finally:
                self.file.close()
                self.file = None


def _check_active(root: Path, config: AppConfig, store: StateStore) -> None:
    """兼容旧运行：有活动记录但无法确认其缓存归属时，不冒险删除。"""

    current = {repo.id: repository_cache_root(config, repo) for repo in config.repositories}
    with store.connect() as connection:
        for table in ("agent_runs", "preflight_runs"):
            cache_column = "cache_path" if table == "preflight_runs" else "NULL AS cache_path"
            rows = connection.execute(
                f"SELECT repository_id,{cache_column} FROM {table} WHERE status IN ('queued','preparing','running')"
            )
            for row in rows:
                candidate = Path(row["cache_path"]) if row["cache_path"] else current.get(row["repository_id"])
                if candidate is None or candidate.resolve() == root.resolve():
                    raise ValueError("活动任务仍可能使用此缓存，已跳过")


async def cleanup_dependency_caches(
    config: AppConfig, store: StateStore, *, get_config: Callable[[], AppConfig],
    should_stop: Callable[[], bool], maintenance_check: Callable[[], bool],
    measure_bytes: Callable[[Path], int],
) -> dict:
    """沿用已有计划，只处理命名合法、归属明确且超过闲置期的受管缓存。"""

    report = {"scanned": 0, "removed": 0, "skipped": 0, "failed": 0, "reclaimed_bytes": 0, "details": []}
    parent = config.database.path.parent / "preflight-cache"
    if not parent.exists() and not parent.is_symlink():
        return report
    try:
        _plain_directory(parent)
        candidates = sorted(parent.iterdir())
    except OSError:
        report.update(failed=1, scanned=1, details=[{"path": str(parent), "status": "failed", "reason": "缓存目录无法读取"}])
        return report
    except ValueError as error:
        report.update(skipped=1, scanned=1, details=[{"path": str(parent), "status": "skipped", "reason": str(error)}])
        return report

    for target in candidates:
        if should_stop() or maintenance_check():
            report["interrupted"] = True
            break
        if target.name == LOCK_DIRECTORY:
            continue
        outcome = {"path": str(target), "status": "skipped", "reason": "不是受管缓存目录，已跳过"}
        try:
            if re.fullmatch(r"[A-Za-z0-9._-]{1,48}-[0-9a-f]{12}", target.name) is None:
                raise ValueError(outcome["reason"])
            _plain_directory(target)
            identity = target.lstat()
            async with RepositoryCacheUsage(target, exclusive=True, timeout_seconds=0):
                cancelled = threading.Event()
                def perform() -> None:
                    """独占期间重复核验，正常任务不能在检查与删除之间开始使用。"""

                    def check() -> None:
                        """扫描中配置变化、迁移或取消时，不沿用旧删除授权。"""

                        fresh = get_config()
                        if (
                            cancelled.is_set() or should_stop() or maintenance_check()
                            or fresh.runtime.workspace_cleanup != config.runtime.workspace_cleanup
                            or fresh.runtime.dependency_cache_retention_days != config.runtime.dependency_cache_retention_days
                            or fresh.database.path != config.database.path
                        ):
                            raise ValueError("清理配置已变化、服务停止或仓库迁移，已跳过")
                        _plain_directory(parent)
                        _plain_directory(target)
                        current_identity = target.lstat()
                        if (identity.st_dev, identity.st_ino) != (current_identity.st_dev, current_identity.st_ino):
                            raise ValueError("缓存目录身份已变化，已跳过")
                        _check_active(target, fresh, store)

                    check()
                    last_used = _last_used(target)
                    if last_used is None:
                        _write_usage(target)
                        raise ValueError("旧缓存首次登记，给予完整闲置保留期")
                    if last_used + config.runtime.dependency_cache_retention_days * 86400 > time.time():
                        raise ValueError("缓存尚未超过闲置保留期")
                    size = measure_bytes(target)
                    check()
                    if _last_used(target) != last_used:
                        raise ValueError("缓存最近使用时间已变化，已跳过")
                    remove_tree(target, missing_ok=False)
                    outcome.update(status="removed", size_bytes=size, reason="超过闲置保留期，依赖缓存已清理")

                # 任务被取消也须等待文件线程退出，不能提前释放独占锁。
                operation = asyncio.create_task(asyncio.to_thread(perform))
                try:
                    await asyncio.shield(operation)
                except asyncio.CancelledError:
                    cancelled.set()
                    while not operation.done():
                        try:
                            await asyncio.shield(operation)
                        except asyncio.CancelledError:
                            # 重复取消也不能让文件线程失去独占锁保护。
                            continue
                        except Exception:
                            break
                    if not operation.cancelled():
                        operation.exception()
                    raise
        except (ValueError, LockTimeoutError) as error:
            outcome.update(reason=str(error))
        except OSError:
            outcome.update(status="failed", reason="缓存目录权限不足、被占用或文件操作失败")
        except Exception as error:
            outcome.update(status="failed", reason=f"缓存清理失败（{type(error).__name__}）")
        report["scanned"] += 1
        report[outcome["status"]] += 1
        report["reclaimed_bytes"] += outcome.get("size_bytes", 0)
        if len(report["details"]) < 200:
            report["details"].append(outcome)
        await asyncio.sleep(0)
    return report
