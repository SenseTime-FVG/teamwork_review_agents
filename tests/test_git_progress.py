"""覆盖 Git 流式进度、安全边界以及无进展超时。"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import threading
import time

import pytest

from teamwork_review_agents.git_progress import GitOutputReader, GitProgressTracker, with_git_progress
from teamwork_review_agents.process_control import pid_exists
from teamwork_review_agents.workspace import WorkspaceCancelled, WorkspaceError, _run_git


def test_progress_chunks_stages_bytes_and_duplicate_high_water():
    """回车、分片、字节推进可识别，重复、倒退和速度变化不能续期。"""

    tracker = GitProgressTracker(time.monotonic())
    tracker.feed(b"Receiving objects:  20% (2/")
    assert tracker.snapshot()[2] is None
    tracker.feed(b"10), 1.00 MiB | 2.00 KiB/s\r")
    first = tracker.snapshot()
    assert first[2] == {
        "stage": "Receiving objects", "label": "接收对象", "percent": 20,
        "current": 2, "total": 10, "received_bytes": 1048576,
        "bytes_per_second": 2048.0,
    }
    tracker.feed(b"Receiving objects:  20% (2/10), 1.00 MiB | 3.00 KiB/s\r")
    tracker.feed(b"Receiving objects:  10% (1/10), 0.50 MiB | 3.00 KiB/s\r")
    assert tracker.snapshot()[:2] == first[:2]
    tracker.feed(b"Receiving objects:  20% (2/10), 1.01 MiB | 3.00 KiB/s\r")
    assert tracker.snapshot()[0] > first[0]
    tracker.feed(b"Resolving deltas: 100% (4/4), done.\n")
    completed_stage = tracker.snapshot()
    assert completed_stage[2]["label"] == "解析差异"
    tracker.feed(b"Receiving objects: 20% (2/10), 1.01 MiB | 0 B/s\r")
    tracker.feed(b"Resolving deltas: 100% (4/4), done.\n")
    assert tracker.snapshot()[:2] == completed_stage[:2]
    tracker.feed(b"Updating files: 50% (1/2)\r")
    assert tracker.snapshot()[2]["label"] == "检出文件"


@pytest.mark.parametrize("line", [
    b"remote: Enumerating objects: 42, done.\n",
    b"Counting objects: 100% (42/42), done.\r\n",
    b"Compressing objects: 15% (3/20)\r",
    b"Checking out files: 100% (4/4), done.\n",
    b"Writing objects: 100% (2/2), 512 bytes | 512 bytes/s, done.\n",
])
def test_known_progress_lines(line):
    """枚举、压缩、检出和写入采用固定白名单语法。"""

    tracker = GitProgressTracker(time.monotonic())
    tracker.feed(line)
    assert tracker.snapshot()[2] is not None


def test_arbitrary_long_secret_and_invalid_lines_are_not_progress():
    """任意文本、超长行和非法数值不进入安全事件，也不能重置时钟。"""

    tracker = GitProgressTracker(time.monotonic())
    before = tracker.snapshot()
    for line in [
        b"heartbeat token=fake-secret\r",
        b"Receiving objects: 101% (1/1)\n",
        b"Receiving objects: 10% (20/10)\n",
        b"Receiving objects: 10% (1/10), fake-secret\n",
        b"x" * 6000 + b"Receiving objects: 10% (1/10)\n",
        "正在等待 fake-secret\r".encode(),
    ]:
        tracker.feed(line)
    assert tracker.snapshot() == before


def test_reader_drains_and_bounds_complete_error_records():
    """完整消费管道，错误尾部有界，过长密钥所在行整行丢弃。"""

    tracker = GitProgressTracker(time.monotonic())
    output = b"x" * 20000 + b"\n" + (b"Receiving objects: 20% (2/10)\r" * 10000) + b"fatal: safe error"
    reader = GitOutputReader(io.BytesIO(output), finished=threading.Event(), tracker=tracker)
    reader.start()
    reader.join(timeout=5)
    assert not reader.is_alive() and not reader.error
    assert len(reader.data) <= 65536
    assert "xxxx" not in reader.text()
    assert reader.text().endswith("fatal: safe error\n")
    assert tracker.snapshot()[2]["current"] == 2


@pytest.mark.parametrize(("arguments", "expected"), [
    (["clone", "--", "origin", "target"], ["clone", "--progress", "--", "origin", "target"]),
    (["-C", "fetch", "fetch", "origin"], ["-C", "fetch", "fetch", "--progress", "origin"]),
    (["-c", "core.x=y", "checkout", "--detach", "HEAD"], ["-c", "core.x=y", "checkout", "--progress", "--detach", "HEAD"]),
    (["clone", "--progress", "url"], ["clone", "--progress", "url"]),
    (["fetch", "--no-progress", "origin"], ["fetch", "--no-progress", "origin"]),
    (["rev-parse", "fetch"], ["rev-parse", "fetch"]),
    (["worktree", "add", "target"], ["worktree", "add", "target"]),
])
def test_progress_flags_do_not_rewrite_paths(arguments, expected):
    """参数插入只作用于受支持子命令，不误认目录或分支名称。"""

    assert with_git_progress(arguments) == expected


@pytest.fixture
def python_git(monkeypatch):
    """用跨平台 Python 子进程模拟耗时 Git，不接触外部仓库。"""

    monkeypatch.setattr("teamwork_review_agents.workspace.shutil.which", lambda _: sys.executable)


def test_download_keeps_running_past_old_total_timeout(python_git):
    """总耗时超过阈值但接收字节不断增长时应成功，哪怕对象数不变。"""

    events = []
    result = _run_git(["-u", "-c", """
import sys, time
for i in range(12):
    sys.stderr.write(f'Receiving objects: 10% (1/10), {i+1}.00 KiB | 5.00 KiB/s\\r')
    sys.stderr.flush()
    time.sleep(0.15)
print('查询结果')
"""], timeout_seconds=1, progress_callback=events.append)
    assert result.returncode == 0 and result.stdout.strip() == "查询结果"
    assert events[-1].elapsed_seconds >= 1
    assert events[-1].progress["received_bytes"] == 12 * 1024
    assert any(event.state == "progress" and event.progress for event in events)
    assert all(event.timeout_kind == "idle" for event in events)


def test_repeated_progress_and_noise_cannot_prevent_timeout(python_git):
    """重复进度、速度变化以及 stdout/stderr 噪声均不能冒充进展。"""

    events = []
    started = time.monotonic()
    with pytest.raises(WorkspaceError, match="连续无有效进展超过 1 秒"):
        _run_git(["-u", "-c", """
import sys, time
for i in range(100):
    sys.stderr.write(f'Receiving objects: 10% (1/10), 1.00 KiB | {i+1}.00 KiB/s\\r')
    sys.stderr.write('still alive\\n')
    sys.stderr.flush()
    print('heartbeat', flush=True)
    time.sleep(0.1)
"""], timeout_seconds=1, progress_callback=events.append)
    assert time.monotonic() - started < 5
    assert events[-1].state == "timed_out" and events[-1].idle_seconds >= 1


def test_cancel_during_active_transfer(python_git):
    """正在增长的下载仍能人工取消，不等待无进展时限。"""

    events = []
    started = time.monotonic()
    with pytest.raises(WorkspaceCancelled):
        _run_git(["-u", "-c", """
import sys, time
for i in range(100):
    sys.stderr.write(f'Receiving objects: {i}% ({i}/100)\\r')
    sys.stderr.flush()
    time.sleep(0.1)
"""], timeout_seconds=10, cancel_check=lambda: time.monotonic() - started > 0.5,
            progress_callback=events.append)
    assert events[-1].state == "cancelled"
    assert time.monotonic() - started < 5


@pytest.mark.skipif(os.name == "nt", reason="只有 POSIX 支持在测试子进程内忽略 SIGTERM")
def test_idle_timeout_kills_descendant_holding_pipe(python_git, tmp_path):
    """根进程退出后，忽略温和终止且持有管道的后代也必须强制清理。"""

    pid_path = tmp_path / "child.pid"
    script = f"""
import subprocess, sys, time
subprocess.Popen([sys.executable, '-c', "import os, signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); open({str(pid_path)!r}, 'w').write(str(os.getpid())); time.sleep(30)"])
time.sleep(30)
"""
    with pytest.raises(WorkspaceError, match="连续无有效进展"):
        _run_git(["-u", "-c", script], timeout_seconds=1)
    assert not pid_exists(int(pid_path.read_text()))


def test_real_git_clone_reports_safe_structured_progress(tmp_path):
    """真实 file 协议克隆覆盖 Git 自身的标准进度，事件不含原始内容。"""

    source, target = tmp_path / "source", tmp_path / "target"
    source.mkdir()
    subprocess.run(["git", "init", str(source)], check=True, capture_output=True)
    (source / "data.bin").write_bytes(os.urandom(2 * 1024 * 1024))
    subprocess.run(["git", "-C", str(source), "add", "data.bin"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(source), "-c", "user.name=测试", "-c", "user.email=test@example.invalid", "commit", "-m", "测试"], check=True, capture_output=True)
    events = []
    _run_git(["clone", "--", source.as_uri(), str(target)], timeout_seconds=10, progress_callback=events.append)
    assert (target / "data.bin").read_bytes() == (source / "data.bin").read_bytes()
    assert events[-1].state == "completed" and events[-1].progress is not None
    assert events[-1].last_progress_at is not None
    assert "stdout" not in json.dumps([event.as_dict() for event in events])
    assert "stderr" not in json.dumps([event.as_dict() for event in events])
