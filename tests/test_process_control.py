"""跨平台进程创建与进程树回收测试。"""

from __future__ import annotations

import os
import subprocess
import sys
import time

from teamwork_review_agents.process_control import (
    iter_process_commands,
    pid_exists,
    process_group_options,
    process_started_at,
    terminate_process,
)


def test_process_group_options_match_current_platform() -> None:
    """子进程组参数必须只使用当前平台支持的字段。"""

    foreground = process_group_options()
    detached = process_group_options(detached=True)
    if os.name == "nt":
        assert "creationflags" in foreground
        assert "start_new_session" not in foreground
        assert detached["creationflags"] != foreground["creationflags"]
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
