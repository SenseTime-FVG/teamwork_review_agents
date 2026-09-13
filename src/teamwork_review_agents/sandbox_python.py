"""发现并在真实 Windows 沙盒内验证本轮基础设施 Python。"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from .codex_executable import CodexRuntimeError, active_codex_executable
from .process_control import process_group_options, terminate_process
from .subprocess_utils import WINDOWS_REQUIRED_ENVIRONMENT_NAMES, selected_environment


@dataclass(frozen=True)
class SandboxPython:
    """仅包含路径与来源的运行级验证结果，不携带凭据。"""

    executable: str
    discovery_source: str
    readable_directories: tuple[str, ...]


def current_sandbox_python() -> SandboxPython:
    """Windows 必须使用就绪检查的绑定结果，不能隐式退回未验证的宿主 Python。"""

    active = active_codex_executable.get()
    if active is not None and active.sandbox_python is not None:
        return active.sandbox_python
    if os.name == "nt" or sys.platform == "win32":
        raise CodexRuntimeError(
            "Windows 沙盒 Python 尚未通过运行前验证",
            error_code="sandbox_python_unavailable",
        )
    # 非 Windows 不更改原有解释器行为；也便于执行跨平台启动桥回归。
    return SandboxPython(sys.executable, "service_python", tuple(dict.fromkeys((
        str(Path(sys.executable).resolve().parent), str(Path(sys.base_prefix).resolve()),
    ))))


def _candidates(configured: Path | None) -> list[tuple[Path, str]]:
    """只读取当前服务账户的安装目录，不从 Agent 环境或任意 PATH 发现程序。"""

    if configured is not None:
        return [(configured.expanduser().resolve(), "configured_path")]
    host = selected_environment({"USERPROFILE", "HOME"}, os.environ)
    root = Path(host.get("USERPROFILE") or host.get("HOME") or str(Path.home()))
    runtimes = root / ".cache" / "codex-runtimes"
    try:
        installed = sorted(
            (path for path in runtimes.glob("*/dependencies/python/python.exe") if path.is_file()),
            key=lambda path: (path.parts[-4] == "codex-primary-runtime", path.stat().st_mtime_ns),
            reverse=True,
        )[:8]
    except OSError:
        installed = []
    candidates = [(path.resolve(), "codex_runtime") for path in installed]
    candidates.append((Path(sys.executable).resolve(), "service_python"))
    return list(dict.fromkeys(candidates))


def _readable_roots(candidate: Path) -> list[Path]:
    """解析解释器的静态依赖根，虚拟环境不需要借助宿主启动来探测。"""

    readable = [candidate.parent]
    if candidate == Path(sys.executable).resolve():
        readable.append(Path(sys.base_prefix).resolve())
    for parent in (candidate.parent, candidate.parent.parent):
        configuration = parent / "pyvenv.cfg"
        if configuration.is_file():
            for line in configuration.read_text(encoding="utf-8").splitlines():
                key, separator, value = line.partition("=")
                if separator and key.strip() == "home" and Path(value.strip()).is_absolute():
                    readable.append(Path(value.strip()).resolve())
    return list(dict.fromkeys(path for path in readable if path.parent != path))


# 同时验证桥、askpass 与独立 MCP 所需标准库；只输出可信程序路径和固定标识。
_PROBE = """
import asyncio, base64, contextlib, json, os, pathlib, subprocess, sys, time, uuid, zlib
print(json.dumps({"marker": "teamwork-sandbox-python-ready", "executable": sys.executable,
                  "base_prefix": sys.base_prefix, "stdlib": str(pathlib.Path(asyncio.__file__).parent.parent)}))
"""


def inspect_sandbox_python(
    codex_binary: str, *, configured: Path | None, codex_home: Path | None,
    environment: Mapping[str, str],
) -> SandboxPython:
    """创建工作区前用无凭据、禁网的原生沙盒验证；不能经待验证的 Python 桥启动。"""

    from .sandbox_environment import sandbox_host_environment

    diagnostic = selected_environment(
        WINDOWS_REQUIRED_ENVIRONMENT_NAMES | {"PATH", "HOME", "CODEX_HOME"}, environment,
    )
    diagnostic = sandbox_host_environment(diagnostic, codex_home=codex_home)
    failures: list[dict[str, object]] = []
    timed_out = False
    for candidate, source in _candidates(configured):
        failure: dict[str, object] = {"path": str(candidate), "source": source}
        failures.append(failure)
        try:
            if not candidate.is_file():
                failure["reason"] = "not_found"
                continue
            readable = _readable_roots(candidate)
        except (OSError, ValueError) as exc:
            failure.update(reason="installation_unreadable", winerror=getattr(exc, "winerror", None))
            continue
        entries = ",".join(f"{json.dumps(str(path))}=\"read\"" for path in readable)
        profile = (
            'permissions.teamwork_python_probe={extends=":read-only",'
            f'filesystem={{{entries}}},network={{enabled=false}}}}'
        )
        try:
            with tempfile.TemporaryDirectory(prefix="teamwork-python-probe-") as directory:
                command = [codex_binary, "sandbox", "--permission-profile", "teamwork_python_probe",
                           "--cd", directory, "--config", profile, "--",
                           str(candidate), "-I", "-S", "-c", _PROBE]
                with subprocess.Popen(
                    command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    env=diagnostic, **process_group_options(),
                ) as process:
                    try:
                        output, errors = process.communicate(timeout=20)
                    except subprocess.TimeoutExpired:
                        # 超时必须结束整个沙盒进程树，不能留下等待输入的桥或探针。
                        terminate_process(process.pid, force=True)
                        process.communicate()
                        timed_out = True
                        failure["reason"] = "timeout"
                        continue
                failure["returncode"] = process.returncode
                if process.returncode != 0:
                    failure["reason"] = "sandbox_launch_failed"
                    # 只抽取系统错误编号，绝不把启动器回显的环境或命令送入日志。
                    match = re.search(rb"(?:CreateProcessAsUserW failed:|Windows error)\s*(\d+)", errors)
                    if match:
                        failure["winerror"] = int(match[1])
                    continue
                result = json.loads(output.decode("utf-8").strip())
                if not isinstance(result, dict) or result.get("marker") != "teamwork-sandbox-python-ready":
                    raise ValueError("缺少探针标识")
                if Path(result["executable"]).resolve() != candidate:
                    raise ValueError("解释器解析结果与候选不一致")
                roots = [candidate.parent, Path(result["base_prefix"]), Path(result["stdlib"])]
                if any(not path.is_absolute() or path.parent == path for path in roots):
                    raise ValueError("解释器依赖目录无效")
                return SandboxPython(str(candidate), source, tuple(dict.fromkeys(str(path.resolve()) for path in roots)))
        except (OSError, ValueError, KeyError, TypeError) as exc:
            failure.update(reason=type(exc).__name__, winerror=getattr(exc, "winerror", None))
    # 不记录原始 stderr：外部启动器可能将完整环境或命令回显到错误流。
    raise CodexRuntimeError(
        "Windows 沙盒无法执行可用 Python；请配置 runtime.managed_sandbox.python_binary "
        "指向沙盒账户可执行的 Python，或检查该安装目录的执行权限",
        error_code="sandbox_python_unavailable", retryable=timed_out,
        details={"stage": "before_workspace", "python_candidates": failures},
    )
