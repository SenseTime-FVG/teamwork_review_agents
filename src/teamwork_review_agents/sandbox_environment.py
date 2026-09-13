"""Windows 外层沙盒与内层 Agent 的目录环境分离。"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Mapping

from .subprocess_utils import remove_environment_names, selected_environment


DIRECTORY_ENVIRONMENT_NAMES = frozenset({
    "CODEX_HOME", "HOME", "USERPROFILE", "HOMEDRIVE", "HOMEPATH",
    "APPDATA", "LOCALAPPDATA", "TEMP", "TMP", "TMPDIR",
    "XDG_CACHE_HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME",
    "XDG_RUNTIME_DIR",
})
_INNER_DIRECTORIES_KEY = "TEAMWORK_SANDBOX_INNER_DIRECTORIES"

# 此代码只在原生沙盒建立后执行；不读取脚本文件，避免可写文件被宿主执行。
# 只恢复目录字段，不覆盖沙盒注入的代理。子进程留在原进程树并继承标准流。
_INNER_LAUNCHER = """
import json, os, subprocess, sys
names = frozenset(%r)
payload = json.loads(os.environ.pop(%r))
if set(payload) != names or any(value is not None and not isinstance(value, str) for value in payload.values()):
    raise ValueError("沙盒内层目录环境无效")
for key in tuple(os.environ):
    if key.upper() in names:
        del os.environ[key]
for key, value in payload.items():
    if value is not None:
        os.environ[key] = value
options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
# 隐窗模式必须显式传递标准句柄，否则 Windows 可能丢失输出或等待不存在的控制台输入。
process = subprocess.Popen(sys.argv[1:], stdin=sys.stdin, stdout=sys.stdout, stderr=sys.stderr, **options)
sys.exit(process.wait())
""" % (tuple(sorted(DIRECTORY_ENVIRONMENT_NAMES)), _INNER_DIRECTORIES_KEY)


def windows_environment_separation() -> bool:
    """只对 Windows 原生沙盒启用双层环境。"""

    return os.name == "nt" or sys.platform == "win32"


def sandbox_executable_environment(environment: Mapping[str, str]) -> dict[str, str]:
    """外层程序发现也使用宿主安装目录，避免临时 LOCALAPPDATA 遮蔽 Windows 安装。"""

    return sandbox_host_environment(environment) if windows_environment_separation() else dict(environment)


def sandbox_host_environment(
    environment: Mapping[str, str], *, codex_home: Path | None = None,
    host_environment: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """仅替换目录字段，宿主来源独立于 Agent 环境及其同名覆盖。"""

    host = selected_environment(
        DIRECTORY_ENVIRONMENT_NAMES,
        os.environ if host_environment is None else host_environment,
    )
    host_home = host.get("USERPROFILE") or host.get("HOME") or str(Path.home())
    selected_home = codex_home if codex_home is not None else Path(
        host.get("CODEX_HOME") or str(Path(host_home) / ".codex")
    )
    host["CODEX_HOME"] = str(selected_home.expanduser().resolve())
    result = dict(environment)
    remove_environment_names(result, DIRECTORY_ENVIRONMENT_NAMES | {_INNER_DIRECTORIES_KEY})
    result.update(host)
    return result


def separate_sandbox_environment(
    inner_command: list[str], environment: Mapping[str, str], *,
    codex_home: Path | None = None,
) -> tuple[list[str], dict[str, str]]:
    """绑定外层宿主环境与只在沙盒内生效的目录恢复桥。"""

    inner = selected_environment(DIRECTORY_ENVIRONMENT_NAMES, environment)
    outer = sandbox_host_environment(environment, codex_home=codex_home)
    # 缺失值也需恢复，避免内层意外继承宿主 CODEX_HOME 或缓存路径。
    outer[_INNER_DIRECTORIES_KEY] = json.dumps(
        {name: inner.get(name) for name in sorted(DIRECTORY_ENVIRONMENT_NAMES)},
        ensure_ascii=True,
    )
    return [sys.executable, "-I", "-c", _INNER_LAUNCHER, *inner_command], outer
