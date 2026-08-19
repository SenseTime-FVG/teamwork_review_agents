"""外部命令解析与受控子进程环境工具。"""

from __future__ import annotations

import os
import shutil
from collections.abc import Iterable, Mapping, MutableMapping
from pathlib import Path


WINDOWS_REQUIRED_ENVIRONMENT_NAMES = frozenset(
    {
        "SYSTEMROOT",
        "COMSPEC",
        "PATHEXT",
        "TEMP",
        "TMP",
        "USERPROFILE",
        "APPDATA",
        "LOCALAPPDATA",
    }
)


def selected_environment(
    names: Iterable[str],
    source: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """按不区分大小写的名称复制明确允许的宿主环境。"""

    allowed = {name.upper() for name in names}
    host = source if source is not None else os.environ
    return {
        name.upper(): value
        for name, value in host.items()
        if name.upper() in allowed
    }


def remove_environment_names(
    environment: MutableMapping[str, str],
    names: Iterable[str],
) -> None:
    """按 Windows 环境变量语义移除全部同名键。"""

    blocked = {name.upper() for name in names}
    for name in tuple(environment):
        if name.upper() in blocked:
            environment.pop(name, None)


def resolve_executable(
    command: str,
    environment: Mapping[str, str] | None = None,
) -> str:
    """按子进程 PATH 解析命令，保留找不到时的原命令用于明确报错。"""

    candidate = Path(command).expanduser()
    if candidate.is_file():
        return str(candidate.resolve())
    search_path = None
    if environment is not None:
        search_path = next(
            (
                value
                for name, value in environment.items()
                if name.upper() == "PATH"
            ),
            os.defpath,
        )
    try:
        resolved = shutil.which(command, path=search_path)
    except AttributeError:
        # 跨平台测试会模拟 sys.platform；非 Windows 解释器没有 _winapi。
        resolved = None
    return resolved or command
