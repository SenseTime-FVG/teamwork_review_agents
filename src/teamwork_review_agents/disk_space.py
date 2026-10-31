"""本地 CI 磁盘不足的识别与有界周期探测。"""

from __future__ import annotations

import errno
import re
import shutil
import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel

if TYPE_CHECKING:
    from .config import AppConfig, RepositoryConfig

DISK_CHECK_INTERVAL_SECONDS = 60
DISK_RECOVERY_BYTES = 5 * 1024**3


class DiskVolume(BaseModel):
    """一个实际磁盘的探测结果；无法读取时不能视为已恢复。"""

    path: str
    free_bytes: int | None = None
    error: str | None = None


class DiskSpaceWait(BaseModel):
    """可跨服务重启继续使用的资源等待信息。"""

    volumes: list[DiskVolume]
    required_bytes: int = DISK_RECOVERY_BYTES
    next_check_at: float

    @property
    def recovered(self) -> bool:
        """所有相关磁盘都达到阈值才允许重新准备 CI。"""

        return bool(self.volumes) and all(
            volume.free_bytes is not None and volume.free_bytes >= self.required_bytes
            and volume.error is None for volume in self.volumes
        )


def is_disk_full_error(error: BaseException | str) -> bool:
    """只识别明确的空间耗尽，不把权限、配额或普通非零退出归入此类。"""

    if isinstance(error, OSError) and (
        error.errno == errno.ENOSPC or getattr(error, "winerror", None) in {39, 112}
    ):
        return True
    message = str(error).lower()
    return bool(re.search(r"\benospc\b", message)) or any(marker in message for marker in (
        "no space left on device", "[errno 28]",
        "there is not enough space on the disk", "磁盘空间不足",
    ))


def probe_disk_space(paths: list[str | Path]) -> DiskSpaceWait:
    """不存在的目录检查最近父目录，并按设备去重，保留查询失败信息。"""

    volumes: list[DiskVolume] = []
    devices: set[int] = set()
    for path in paths:
        candidate = Path(path).expanduser()
        try:
            candidate = candidate.resolve()
            while not candidate.exists() and candidate.parent != candidate:
                candidate = candidate.parent
            device = candidate.stat().st_dev
            if device in devices:
                continue
            free = shutil.disk_usage(candidate).free
            devices.add(device)
            volumes.append(DiskVolume(path=str(candidate), free_bytes=free))
        except OSError as exc:
            volumes.append(DiskVolume(path=str(candidate), error=str(exc)))
    return DiskSpaceWait(volumes=volumes, next_check_at=time.time() + DISK_CHECK_INTERVAL_SECONDS)


def preflight_disk_paths(config: AppConfig, repository: RepositoryConfig) -> list[Path]:
    """覆盖 checkout、缓存、状态库和临时 HOME 实际写入的位置。"""

    from .preflight_cache import repository_cache_root

    paths = [
        repository.workspace, repository.workspace.resolve().parent,
        config.database.path.parent, Path(tempfile.gettempdir()),
    ]
    if repository.preflight.cache_enabled:
        paths.append(repository_cache_root(config, repository))
    return paths
