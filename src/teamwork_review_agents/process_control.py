"""跨平台子进程组创建、身份读取与进程树终止。"""

from __future__ import annotations

import os
import signal
import subprocess
from contextlib import suppress
from typing import Any

import psutil


_WINDOWS_CREATE_NEW_PROCESS_GROUP = getattr(
    subprocess,
    "CREATE_NEW_PROCESS_GROUP",
    0x00000200,
)
_WINDOWS_DETACHED_PROCESS = getattr(
    subprocess,
    "DETACHED_PROCESS",
    0x00000008,
)


def process_group_options(*, detached: bool = False) -> dict[str, Any]:
    """返回当前平台创建独立进程组所需的 subprocess 参数。"""

    if os.name == "nt":
        creationflags = _WINDOWS_CREATE_NEW_PROCESS_GROUP
        if detached:
            creationflags |= _WINDOWS_DETACHED_PROCESS
        return {"creationflags": creationflags}
    return {"start_new_session": True}


def process_started_at(pid: int) -> str | None:
    """返回可持久化比较的进程启动时间。"""

    try:
        return f"{psutil.Process(pid).create_time():.6f}"
    except (psutil.NoSuchProcess, psutil.ZombieProcess, psutil.AccessDenied):
        return None


def process_state(pid: int) -> str | None:
    """返回跨平台进程状态，僵尸或死亡进程统一标记为 Z。"""

    try:
        status = psutil.Process(pid).status()
    except (psutil.NoSuchProcess, psutil.ZombieProcess):
        return None
    except psutil.AccessDenied:
        return "unknown"
    if status in {psutil.STATUS_ZOMBIE, psutil.STATUS_DEAD}:
        return "Z"
    return status


def pid_exists(pid: int) -> bool:
    """检查 PID 是否对应仍可识别的非僵尸进程。"""

    return pid > 0 and process_state(pid) not in {None, "Z"}


def iter_process_commands() -> list[tuple[int, list[str], str]]:
    """枚举可读取命令行、启动时间且仍存活的系统进程。"""

    processes: list[tuple[int, list[str], str]] = []
    for process in psutil.process_iter(["pid", "cmdline", "create_time", "status"]):
        try:
            info = process.info
            status = str(info.get("status") or "")
            if status in {psutil.STATUS_ZOMBIE, psutil.STATUS_DEAD}:
                continue
            arguments = [str(item) for item in (info.get("cmdline") or [])]
            created = info.get("create_time")
            if not arguments or created is None:
                continue
            processes.append((int(info["pid"]), arguments, f"{float(created):.6f}"))
        except (psutil.NoSuchProcess, psutil.ZombieProcess, psutil.AccessDenied):
            continue
    return processes


def _windows_process_targets(pid: int, *, tree: bool) -> list[psutil.Process]:
    """返回 Windows 目标进程，并找回直接父进程已退出的后代。"""

    try:
        root = psutil.Process(pid)
    except (psutil.NoSuchProcess, psutil.ZombieProcess):
        root = None
    if not tree:
        return [root] if root is not None else []

    children_by_parent: dict[int, list[psutil.Process]] = {}
    for process in psutil.process_iter(["pid", "ppid"]):
        try:
            parent_pid = int(process.info["ppid"])
        except (KeyError, TypeError, ValueError, psutil.Error):
            continue
        children_by_parent.setdefault(parent_pid, []).append(process)

    descendants: list[psutil.Process] = []
    pending = [pid]
    seen = {pid}
    while pending:
        parent_pid = pending.pop()
        for child in children_by_parent.get(parent_pid, []):
            if child.pid in seen:
                continue
            seen.add(child.pid)
            descendants.append(child)
            pending.append(child.pid)

    targets = list(reversed(descendants))
    if root is not None:
        targets.append(root)
    return targets


def terminate_process(
    pid: int,
    *,
    force: bool = False,
    tree: bool = True,
) -> None:
    """终止指定进程；需要时同时覆盖完整后代进程树。"""

    if pid <= 0:
        return
    if os.name != "nt":
        target_signal = signal.SIGKILL if force else signal.SIGTERM
        if tree:
            os.killpg(pid, target_signal)
        else:
            os.kill(pid, target_signal)
        return

    targets = _windows_process_targets(pid, tree=tree)
    denied: list[int] = []
    for process in targets:
        try:
            if force:
                process.kill()
            else:
                process.terminate()
        except (psutil.NoSuchProcess, psutil.ZombieProcess):
            continue
        except psutil.AccessDenied:
            denied.append(process.pid)
    if denied:
        joined = ", ".join(str(item) for item in denied)
        raise PermissionError(f"无权结束进程 PID {joined}")


def reap_child(pid: int) -> None:
    """仅在 POSIX 当前进程是父进程时回收已经退出的子进程。"""

    if os.name == "nt" or not hasattr(os, "waitpid"):
        return
    with suppress(ChildProcessError, OSError):
        os.waitpid(pid, os.WNOHANG)
