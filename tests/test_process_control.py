"""跨平台进程创建与进程树回收测试。"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from teamwork_review_agents import process_control
from teamwork_review_agents.process_control import (
    hidden_process_options,
    iter_process_commands,
    pid_exists,
    process_group_options,
    process_started_at,
    terminate_process,
)


@pytest.mark.parametrize("platform", ["nt", "posix"])
def test_hidden_and_detached_process_flags(monkeypatch, platform) -> None:
    """精确检查窗口与进程组位掩码，防止误用优先级标志或混合互斥标志。"""

    # 只替换模块持有的系统信息，避免改变 pytest 和 pathlib 看到的平台。
    monkeypatch.setattr(process_control, "os", SimpleNamespace(name=platform))
    assert process_control._WINDOWS_CREATE_NO_WINDOW == 0x08000000
    if platform == "nt":
        assert hidden_process_options() == {"creationflags": 0x08000000}
        assert process_group_options() == {"creationflags": 0x08000200}
        assert process_group_options(detached=True) == {"creationflags": 0x00000208}
    else:
        assert hidden_process_options() == {}
        assert process_group_options() == {"start_new_session": True}
        assert process_group_options(detached=True) == {"start_new_session": True}


def test_process_group_options_match_current_platform() -> None:
    """子进程组参数必须只使用当前平台支持的字段。"""

    foreground = process_group_options()
    detached = process_group_options(detached=True)
    if os.name == "nt":
        assert "creationflags" in foreground
        assert "start_new_session" not in foreground
        assert foreground["creationflags"] == (
            subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
        )
        assert detached["creationflags"] == (
            subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
        )
    else:
        assert foreground == {"start_new_session": True}
        assert detached == foreground


def test_process_identity_and_command_discovery_include_current_process() -> None:
    """进程身份和命令枚举应覆盖当前 Python 进程。"""

    assert process_started_at(os.getpid()) is not None
    assert pid_exists(os.getpid())
    discovered = {pid: arguments for pid, arguments, _ in iter_process_commands()}
    assert os.getpid() in discovered
    assert discovered[os.getpid()]


def test_terminate_process_reclaims_descendant_tree(tmp_path) -> None:
    """终止独立进程组时必须同时回收其后代进程。"""

    child_pid_file = tmp_path / "child.pid"
    parent_code = (
        "import subprocess,sys,time; "
        "child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)']); "
        "open(sys.argv[1],'w',encoding='utf-8').write(str(child.pid)); "
        "time.sleep(30)"
    )
    parent = subprocess.Popen(
        [sys.executable, "-c", parent_code, str(child_pid_file)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        **process_group_options(),
    )
    child_pid = 0
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not child_pid_file.exists():
            time.sleep(0.05)
        assert child_pid_file.exists()
        child_pid = int(child_pid_file.read_text(encoding="utf-8"))

        terminate_process(parent.pid, force=False, tree=True)
        parent.wait(timeout=5)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and pid_exists(child_pid):
            time.sleep(0.05)
        assert not pid_exists(child_pid)
    finally:
        if parent.poll() is None:
            terminate_process(parent.pid, force=True, tree=True)
            parent.wait(timeout=5)
        if child_pid and pid_exists(child_pid):
            terminate_process(child_pid, force=True, tree=False)


def test_terminate_process_reclaims_descendant_in_separate_session(tmp_path) -> None:
    """后代自行创建独立会话后仍必须被进程树终止逻辑回收。"""

    child_pid_file = tmp_path / "separate-child.pid"
    parent_code = (
        "import subprocess,sys,time; "
        "child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'],"
        "start_new_session=True); "
        "open(sys.argv[1],'w',encoding='utf-8').write(str(child.pid)); "
        "time.sleep(30)"
    )
    parent = subprocess.Popen(
        [sys.executable, "-c", parent_code, str(child_pid_file)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        **process_group_options(),
    )
    child_pid = 0
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not child_pid_file.exists():
            time.sleep(0.05)
        assert child_pid_file.exists()
        child_pid = int(child_pid_file.read_text(encoding="utf-8"))

        terminate_process(parent.pid, force=False, tree=True)
        parent.wait(timeout=5)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and pid_exists(child_pid):
            time.sleep(0.05)
        assert not pid_exists(child_pid)
    finally:
        if parent.poll() is None:
            terminate_process(parent.pid, force=True, tree=True)
            parent.wait(timeout=5)
        if child_pid and pid_exists(child_pid):
            terminate_process(child_pid, force=True, tree=False)


def test_terminate_process_finds_descendant_after_parent_exits(tmp_path) -> None:
    """直接父进程退出后仍应按原父 PID 找回并结束后代。"""

    child_pid_file = tmp_path / "orphan-child.pid"
    parent_code = (
        "import subprocess,sys; "
        "child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)']); "
        "open(sys.argv[1],'w',encoding='utf-8').write(str(child.pid))"
    )
    parent = subprocess.Popen(
        [sys.executable, "-c", parent_code, str(child_pid_file)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        **process_group_options(),
    )
    child_pid = 0
    try:
        parent.wait(timeout=5)
        child_pid = int(child_pid_file.read_text(encoding="utf-8"))

        terminate_process(parent.pid, force=True, tree=True)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and pid_exists(child_pid):
            time.sleep(0.05)
        assert not pid_exists(child_pid)
    finally:
        if child_pid and pid_exists(child_pid):
            terminate_process(child_pid, force=True, tree=False)
