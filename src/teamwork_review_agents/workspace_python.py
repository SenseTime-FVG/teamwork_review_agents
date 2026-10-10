"""仓库工作区 Python 虚拟环境的安全解析与通用运行提示。"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Mapping

from .subprocess_utils import remove_environment_names


PYTHON_PROBE_MARKER = "TEAMWORK_WORKSPACE_PYTHON="
PYTHON_PROBE = """import importlib, json, os, sys
# 校验真实运行的解释器，不能仅凭目录存在认定虚拟环境可用。
expected = os.path.normcase(os.path.realpath(sys.argv[1]))
if os.path.normcase(os.path.realpath(sys.prefix)) != expected:
    raise RuntimeError('Python 解释器未使用配置的工作区虚拟环境')
modules = json.loads(sys.argv[2])
for name in modules:
    importlib.import_module(name)
print('TEAMWORK_WORKSPACE_PYTHON=' + json.dumps({
    'executable': sys.executable,
    'prefix': sys.prefix,
    'version': sys.version.split()[0],
    'checked_modules': modules,
}, ensure_ascii=False))
"""


def workspace_python_paths(
    workspace: Path,
    relative_venv: str,
    *,
    windows: bool | None = None,
) -> tuple[Path, Path, Path]:
    """规范化虚拟环境目录，拒绝符号链接逃逸并保留解释器入口路径。"""

    root = workspace.resolve()
    venv = (root / relative_venv).resolve()
    if venv == root or not venv.is_relative_to(root):
        raise ValueError("Python 虚拟环境目录逃逸了当前 Agent 工作区")
    active_windows = os.name == "nt" if windows is None else windows
    scripts = venv / ("Scripts" if active_windows else "bin")
    if not scripts.resolve().is_relative_to(venv):
        raise ValueError("Python 虚拟环境入口目录逃逸了当前虚拟环境")
    python = scripts / ("python.exe" if active_windows else "python")
    if not (venv / "pyvenv.cfg").is_file() or not python.is_file():
        raise ValueError(f"工作区 Python 虚拟环境未就绪：{relative_venv}")
    # 解释器通常是指向基础 Python 的链接，不能将入口解析成宿主路径。
    return venv, scripts, python


def check_python_script_paths(scripts: Path) -> None:
    """拒绝快照中仍引用旧工作区解释器的绝对 Python 脚本入口。"""

    for entry in scripts.iterdir():
        if not entry.is_file() or entry.name.startswith("python"):
            continue
        with entry.open("rb") as handle:
            first_line = handle.readline(4096)
        if not first_line.startswith(b"#!"):
            continue
        words = first_line[2:].decode("utf-8", errors="replace").strip().split()
        if not words:
            continue
        interpreter = words[0]
        path = Path(interpreter)
        if (
            path.is_absolute()
            and re.fullmatch(r"python(?:\d+(?:\.\d+)*)?(?:\.exe)?", path.name)
            and path.parent.resolve() != scripts.resolve()
        ):
            raise ValueError(f"虚拟环境入口 {entry.name} 仍指向旧目录的 Python")


def workspace_python_environment(
    venv: Path,
    scripts: Path,
    python: Path,
    environment: Mapping[str, str],
) -> dict[str, str]:
    """构造仅包含虚拟环境接入信息的覆盖值，不复制凭据。"""

    path = next(
        (value for name, value in environment.items() if name.upper() == "PATH"),
        os.defpath,
    )
    # 避免继承工作区的子 Agent 重复向 PATH 追加同一入口。
    paths = [
        str(scripts),
        *(item for item in path.split(os.pathsep) if item != str(scripts)),
    ]
    return {
        "PATH": os.pathsep.join(paths),
        "VIRTUAL_ENV": str(venv),
        "TEAMWORK_WORKSPACE_PYTHON": str(python),
    }


def apply_workspace_python_environment(
    environment: dict[str, str],
    overrides: Mapping[str, str],
) -> None:
    """按大小写不敏感语义移除干扰变量，保证 Windows 入口同样生效。"""

    remove_environment_names(environment, {"PYTHONHOME", *overrides.keys()})
    environment.update(overrides)


def python_probe_metadata(output: str) -> dict[str, object]:
    """只解析探针的结构化输出，不把安装输出当成环境检查成功。"""

    for line in reversed(output.splitlines()):
        if line.startswith(PYTHON_PROBE_MARKER):
            value = json.loads(line[len(PYTHON_PROBE_MARKER) :])
            if (
                isinstance(value, dict)
                and value.get("executable")
                and value.get("version")
            ):
                return value
    raise ValueError("工作区 Python 校验未返回解释器信息")


def workspace_python_runtime_hint(metadata: Mapping[str, object]) -> str:
    """向所有 Agent 和运行器说明已准备的解释器及测试证据边界。"""

    executable = json.dumps(str(metadata["executable"]), ensure_ascii=False)
    return (
        "# 当前工作区 Python 环境\n"
        f"已验证解释器：{executable}，Python {metadata['version']}。\n"
        "命令 PATH 与 VIRTUAL_ENV 已接入本次工作区环境；不要另选宿主 Python。\n"
        "若命令工具支持 login 参数，请使用非登录 shell（login=false），避免登录配置覆盖 PATH。\n"
        f"pytest 测试入口：{executable} -m pytest。其他测试遵循仓库说明。\n"
        "启动校验只确认环境可用，不代表测试已运行或通过。"
        "若出现缺依赖导致的导入或收集错误，应报告环境未就绪、相关测试未执行，"
        "不能据此认定已确认的代码测试失败，也不能报告测试通过；"
        "已实际执行的其他测试结果应分别如实记录。不要向宿主环境安装依赖。"
    )
