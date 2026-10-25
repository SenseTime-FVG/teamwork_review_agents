"""仅在 SQLite 故障时采集只读诊断，不访问 SQL 参数或凭据。"""

from __future__ import annotations

import json
import logging
import os
import shutil
import sqlite3
import stat
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

import psutil


LOGGER = logging.getLogger(__name__)


def _probe(operation: Callable[[], Any]) -> Any:
    """探针失败仅返回其真实错误，不中断其余采集。"""

    try:
        return operation()
    except Exception as exc:
        return {"error_type": type(exc).__name__, "error": str(exc), "errno": getattr(exc, "errno", None)}


def _path_details(path: Path) -> dict[str, Any]:
    """读取文件元数据和当前进程的访问检查结果，不创建测试文件。"""

    metadata = path.stat()
    return {
        "path": str(path),
        "mode": oct(stat.S_IMODE(metadata.st_mode)),
        "uid": metadata.st_uid,
        "gid": metadata.st_gid,
        "size_bytes": metadata.st_size,
        "hard_links": metadata.st_nlink,
        "is_directory": stat.S_ISDIR(metadata.st_mode),
        "is_symlink": path.is_symlink(),
        "readable": os.access(path, os.R_OK, effective_ids=os.access in os.supports_effective_ids),
        "writable": os.access(path, os.W_OK, effective_ids=os.access in os.supports_effective_ids),
        "searchable": os.access(path, os.X_OK, effective_ids=os.access in os.supports_effective_ids),
    }


def _disk_details(directory: Path) -> dict[str, Any]:
    """目录不存在时检查最近的现存父目录，并明确记录采样位置。"""

    sample = next(path for path in (directory, *directory.parents) if path.is_dir())
    usage = shutil.disk_usage(sample)
    result: dict[str, Any] = {
        "sample_path": str(sample),
        "total_bytes": usage.total,
        "used_bytes": usage.used,
        "free_bytes": usage.free,
    }
    if hasattr(os, "statvfs"):
        def filesystem_details() -> dict[str, Any]:
            """补充当前用户可用空间、inode 及文件系统只读标志。"""

            filesystem = os.statvfs(sample)
            readonly_flag = getattr(os, "ST_RDONLY", None)
            return {
                "available_bytes": filesystem.f_bavail * filesystem.f_frsize,
                "free_inodes": filesystem.f_ffree,
                "available_inodes": filesystem.f_favail,
                "read_only": bool(filesystem.f_flag & readonly_flag) if readonly_flag is not None else None,
            }

        result["filesystem"] = _probe(filesystem_details)
    return result


def _descriptor_limits() -> dict[str, int]:
    """POSIX 读取文件描述符上限；其他平台由探针标记为不可用。"""

    import resource

    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    return {"soft": soft, "hard": hard}


def _descriptor_counts() -> dict[str, Any]:
    """进程句柄查询单独隔离，受限时不影响磁盘和权限诊断。"""

    process = psutil.Process()
    return {
        "open_fds": _probe(process.num_fds) if hasattr(process, "num_fds") else None,
        "open_handles": _probe(process.num_handles) if hasattr(process, "num_handles") else None,
    }


def collect_sqlite_diagnostics(path: Path, phase: str, exc: BaseException) -> dict[str, Any]:
    """记录故障当下的资源状态，不能据此反推历史时点的状态。"""

    database = path.absolute()
    code = getattr(exc, "sqlite_errorcode", None)
    details: dict[str, Any] = {
        "observed_at": datetime.now(UTC).isoformat(),
        "phase": phase,
        "database_path": str(database),
        "exception_type": type(exc).__name__,
        "sqlite_errorcode": code,
        "sqlite_primary_errorcode": code & 0xFF if isinstance(code, int) else None,
        "sqlite_errorname": getattr(exc, "sqlite_errorname", None),
        "os_errno": getattr(exc, "errno", None),
        "process": {
            "pid": os.getpid(),
            "effective_uid": os.geteuid() if hasattr(os, "geteuid") else None,
            "effective_gid": os.getegid() if hasattr(os, "getegid") else None,
            "descriptor_counts": _probe(_descriptor_counts),
            "fd_limits": _probe(_descriptor_limits),
        },
        "paths": {
            name: _probe(lambda target=target: _path_details(target))
            for name, target in {
                "directory": database.parent,
                "database": database,
                "wal": Path(f"{database}-wal"),
                "shm": Path(f"{database}-shm"),
                "journal": Path(f"{database}-journal"),
            }.items()
        },
        "disk": _probe(lambda: _disk_details(database.parent)),
    }
    return details


def log_sqlite_failure(path: Path, phase: str, exc: BaseException, *, secondary: bool = False) -> None:
    """诊断和日志写入均尽力完成，绝不覆盖待传播的原始异常。"""

    if not isinstance(exc, (sqlite3.Error, OSError)):
        return
    try:
        details = collect_sqlite_diagnostics(path, phase, exc)
    except Exception as diagnostic_error:
        details = {
            "database_path": str(path),
            "phase": phase,
            "sqlite_errorcode": getattr(exc, "sqlite_errorcode", None),
            "sqlite_errorname": getattr(exc, "sqlite_errorname", None),
            "diagnostics_error": f"{type(diagnostic_error).__name__}: {diagnostic_error}",
        }
    details["secondary_failure"] = secondary
    try:
        LOGGER.error(
            "SQLite 底层操作失败，诊断：%s",
            json.dumps(details, ensure_ascii=False),
            exc_info=(type(exc), exc, exc.__traceback__),
        )
    except Exception:
        # 磁盘满或日志处理器损坏时仍由调用方收到最初的数据库异常。
        pass
