"""跨平台临时目录创建与可靠删除。"""

from __future__ import annotations

import os
import shutil
import stat
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


def _make_writable(path: str | os.PathLike[str]) -> None:
    """只清除待删除对象自身的只读限制，不跟随目录链接。"""

    target = Path(path)
    mode = target.lstat().st_mode
    writable_mode = mode | stat.S_IWRITE | stat.S_IREAD
    if stat.S_ISDIR(mode):
        writable_mode |= stat.S_IEXEC
    try:
        os.chmod(target, writable_mode, follow_symlinks=False)
    except (NotImplementedError, TypeError):
        if target.is_symlink():
            raise
        os.chmod(target, writable_mode)


def remove_tree(
    path: str | os.PathLike[str],
    *,
    missing_ok: bool = True,
    attempts: int = 3,
    retry_delay_seconds: float = 0.05,
) -> None:
    """删除目录树，并兼容 Windows 只读文件与短暂文件占用。"""

    if attempts < 1:
        raise ValueError("目录删除尝试次数必须大于零")
    target = Path(path)
    if target.is_symlink():
        target.unlink(missing_ok=missing_ok)
        return

    def handle_remove_error(function, failed_path, exception_info) -> None:
        """只为权限错误清除只读属性，其他错误保持原样抛出。"""

        error = exception_info[1]
        if isinstance(error, FileNotFoundError) and missing_ok:
            return
        if not isinstance(error, PermissionError):
            raise error
        _make_writable(failed_path)
        function(failed_path)

    for attempt in range(attempts):
        try:
            shutil.rmtree(target, onerror=handle_remove_error)
            return
        except FileNotFoundError:
            if missing_ok:
                return
            raise
        except OSError:
            if attempt + 1 >= attempts:
                raise
            time.sleep(retry_delay_seconds)


@contextmanager
def temporary_directory(
    *,
    prefix: str | None = None,
    directory: str | os.PathLike[str] | None = None,
) -> Iterator[Path]:
    """创建退出时使用可靠删除器回收的临时目录。"""

    path = Path(tempfile.mkdtemp(prefix=prefix, dir=directory))
    try:
        yield path
    finally:
        remove_tree(path)
