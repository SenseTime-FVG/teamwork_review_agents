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


# 非 Windows 的 stat 不一定导出该 SDK 常量，保留同值以便跨平台验证识别逻辑。
_WINDOWS_MOUNT_POINT_TAG = getattr(stat, "IO_REPARSE_TAG_MOUNT_POINT", 0xA0000003)


def _is_junction(info: os.stat_result) -> bool:
    """从不跟随链接的元数据识别 Windows junction，兼容 Python 3.11。"""

    return bool(
        getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
        and getattr(info, "st_reparse_tag", None) == _WINDOWS_MOUNT_POINT_TAG
    )


def _remove_directory_link(target: Path) -> bool:
    """只删除链接入口，失效联接也不能跟随目标或递归清理。"""

    info = target.lstat()
    if stat.S_ISLNK(info.st_mode):
        target.unlink()
    elif _is_junction(info):
        # Windows 的 rmdir 对 junction 只移除联接，目标即使非空也不受影响。
        target.rmdir()
    else:
        return False
    return True


def _make_writable(path: str | os.PathLike[str]) -> None:
    """只清除待删除对象自身的只读限制，不跟随目录链接。"""

    target = Path(path)
    info = target.lstat()
    if stat.S_ISLNK(info.st_mode) or _is_junction(info):
        raise PermissionError(f"拒绝修改目录链接目标的权限：{target}")
    mode = info.st_mode
    writable_mode = mode | stat.S_IWRITE | stat.S_IREAD
    if stat.S_ISDIR(mode):
        writable_mode |= stat.S_IEXEC
    try:
        os.chmod(target, writable_mode, follow_symlinks=False)
    except (NotImplementedError, TypeError):
        # 旧版 Windows 不支持 follow_symlinks=False，回退前重新排除目录联接。
        info = target.lstat()
        if stat.S_ISLNK(info.st_mode) or _is_junction(info):
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

    def handle_remove_error(function, failed_path, exception_info) -> None:
        """只为权限错误清除只读属性，其他错误保持原样抛出。"""

        error = exception_info[1]
        if isinstance(error, FileNotFoundError) and missing_ok:
            return
        if not isinstance(error, PermissionError):
            raise error
        try:
            # rmtree 内嵌联接的删除也可能短暂失败；只重试删除入口，不修改目标权限。
            if _remove_directory_link(Path(failed_path)):
                return
            _make_writable(failed_path)
            function(failed_path)
        except FileNotFoundError:
            # 子项在错误回调期间消失时应继续遍历，不能提前结束整个目录的回收。
            if not missing_ok:
                raise

    for attempt in range(attempts):
        try:
            # 每次重试都重新识别入口，不能沿用失败前的目录类型判断。
            if _remove_directory_link(target):
                return
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
