"""后台服务进程管理集成测试。"""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from http.client import IncompleteRead
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import yaml

from teamwork_review_agents import cli, process_manager
from teamwork_review_agents.process_control import (
    pid_exists,
    process_group_options,
    terminate_process,
)
from teamwork_review_agents.process_manager import (
    ServiceLease,
    ProcessActionResult,
    ProcessRecord,
    _check_health,
    _effective_startup_timeout_seconds,
    _read_record,
    _write_stop_request,
    running_process,
    runtime_paths,
    start_background,
    stop_managed_process,
)


_SUCCESSFUL_STARTUP_TIMEOUT_SECONDS = 30 if os.name == "nt" else 10


def _unused_port() -> int:
    """向操作系统申请一个暂时未使用的本机端口。"""

    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        return int(server.getsockname()[1])


def _write_config(tmp_path, port: int):
    """创建使用独立数据库和端口的最小测试配置。"""

    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "database": {"path": str(tmp_path / "state.db")},
                "web": {"host": "127.0.0.1", "port": port},
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return config_path


def _wait_health_pid(port: int, timeout: float = 5) -> int:
    """等待健康接口可用并返回实际服务 PID。"""

    deadline = time.monotonic() + timeout
    while True:
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/api/health",
                timeout=1,
            ) as response:
                payload = json.load(response)
                return int(payload["pid"])
        except OSError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.1)


class _OccupiedHealthHandler(BaseHTTPRequestHandler):
    """模拟已经占用端口的旧健康服务。"""

    def do_GET(self) -> None:
        payload = json.dumps({"status": "ok", "pid": 999999}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *_: object) -> None:
        """测试期间不输出 HTTP 访问日志。"""


def test_background_service_starts_and_stops(tmp_path) -> None:
    """后台子进程应能提供健康检查并由 stop 可靠结束。"""

    port = _unused_port()
    config_path = _write_config(tmp_path, port)

    result = start_background(
        config_path,
        host="127.0.0.1",
        port=port,
        startup_timeout_seconds=_SUCCESSFUL_STARTUP_TIMEOUT_SECONDS,
    )
    try:
        assert result.exit_code == 0, result.message
        assert result.record is not None
        assert result.record.detached is True
        assert _wait_health_pid(port) == result.record.pid
        duplicate = start_background(
            config_path,
            host="127.0.0.1",
            port=port,
            startup_timeout_seconds=_SUCCESSFUL_STARTUP_TIMEOUT_SECONDS,
        )
        assert duplicate.exit_code == 0, duplicate.message
        assert duplicate.record is not None
        assert duplicate.record.pid == result.record.pid
    finally:
        stopped = stop_managed_process(config_path, timeout_seconds=10)
    assert stopped.exit_code == 0, stopped.message
    assert "强制停止" not in stopped.message
    assert not runtime_paths(config_path).stop_file.exists()
    assert running_process(config_path) is None


def test_service_lease_matches_only_its_stop_request(tmp_path) -> None:
    """停止请求必须绑定进程启动时间，陈旧 PID 不能误停新服务。"""

    config_path = _write_config(tmp_path, _unused_port())
    paths = runtime_paths(config_path)
    lease = ServiceLease.acquire(
        config_path,
        host="127.0.0.1",
        port=8080,
        detached=False,
    )
    assert lease is not None
    try:
        assert not lease.stop_requested()
        _write_stop_request(paths, [lease.record])
        assert lease.stop_requested()
    finally:
        lease.release()

    replacement = ServiceLease.acquire(
        config_path,
        host="127.0.0.1",
        port=8080,
        detached=False,
    )
    assert replacement is not None
    try:
        assert not replacement.stop_requested()
    finally:
        replacement.release()


def test_read_record_retries_transient_permission_error(tmp_path, monkeypatch) -> None:
    """PID 文件原子替换期间短暂不可读时应重试并恢复。"""

    config_path = _write_config(tmp_path, 8080)
    paths = runtime_paths(config_path)
    paths.directory.mkdir(parents=True, exist_ok=True)
    paths.pid_file.write_text(
        json.dumps(
            {
                "pid": 1234,
                "config_path": str(config_path.resolve()),
                "process_started_at": "123.000000",
                "host": "127.0.0.1",
                "port": 8080,
                "detached": True,
            }
        ),
        encoding="utf-8",
    )
    original_read_text = Path.read_text
    attempts = 0

    def flaky_read_text(path: Path, *args, **kwargs):
        nonlocal attempts
        if path == paths.pid_file and attempts < 2:
            attempts += 1
            raise PermissionError("模拟 Windows 文件共享冲突")
        attempts += 1
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", flaky_read_text)

    record = _read_record(paths)

    assert record is not None
    assert record.pid == 1234
    assert attempts == 3


def test_background_startup_timeout_uses_platform_default(monkeypatch) -> None:
    """WSL/POSIX 与 Windows 默认均等待 30 秒，显式值优先。"""

    monkeypatch.setattr(
        "teamwork_review_agents.process_manager._is_native_windows",
        lambda: True,
    )
    assert _effective_startup_timeout_seconds(None) == 30
    monkeypatch.setattr(
        "teamwork_review_agents.process_manager._is_native_windows",
        lambda: False,
    )
    assert _effective_startup_timeout_seconds(None) == 30
    assert _effective_startup_timeout_seconds(7.5) == 7.5


@pytest.mark.parametrize("value", [0, -1, float("inf"), float("nan")])
def test_startup_timeout_rejects_invalid_api_value(value) -> None:
    """直接调用启动器也不允许无期限或无效的等待预算。"""

    with pytest.raises(ValueError, match="有限秒数"):
        _effective_startup_timeout_seconds(value)


@pytest.mark.parametrize("command", ["start", "restart"])
@pytest.mark.parametrize("timeout", [None, "60", "7.5"])
def test_cli_passes_startup_timeout(command, timeout, tmp_path, monkeypatch) -> None:
    """CLI 将默认或显式预算传入后台启动器，重启仍先完成停止。"""

    start = Mock(return_value=ProcessActionResult(0, "已启动"))
    stop = Mock(return_value=ProcessActionResult(0, "已停止"))
    monkeypatch.setattr(cli, "start_background", start)
    monkeypatch.setattr(cli, "stop_managed_process", stop)
    config = tmp_path / "config.yaml"
    monkeypatch.setattr(cli, "_server_settings", lambda *args: (config, "127.0.0.1", 8080))
    arguments = ["teamwork-review-agents", command]
    if timeout is not None:
        arguments.extend(["--startup-timeout", timeout])
    monkeypatch.setattr(sys, "argv", arguments)
    with pytest.raises(SystemExit) as exit_result:
        cli.main()
    assert exit_result.value.code == 0
    start.assert_called_once_with(
        config, host="127.0.0.1", port=8080,
        startup_timeout_seconds=None if timeout is None else float(timeout),
    )
    assert stop.call_count == (1 if command == "restart" else 0)


@pytest.mark.parametrize("value", ["0", "-1", "nan", "inf", "abc"])
def test_invalid_cli_timeout_cannot_stop_existing_service(value, monkeypatch) -> None:
    """非法重启参数必须在任何启停动作之前被拒绝。"""

    start, stop = Mock(), Mock()
    monkeypatch.setattr(cli, "start_background", start)
    monkeypatch.setattr(cli, "stop_managed_process", stop)
    monkeypatch.setattr(sys, "argv", ["teamwork-review-agents", "restart", f"--startup-timeout={value}"])
    with pytest.raises(SystemExit) as exit_result:
        cli.main()
    assert exit_result.value.code == 2
    start.assert_not_called()
    stop.assert_not_called()


@pytest.fixture
def simulated_start(tmp_path, monkeypatch):
    """用虚拟时钟覆盖慢启动及失败收尾，无需真正等待 30 秒。"""

    clock = SimpleNamespace(seconds=0.0)

    def advance(seconds):
        """只推进当前进程管理测试的时钟。"""

        clock.seconds += seconds

    monkeypatch.setattr(process_manager, "time", SimpleNamespace(
        monotonic=lambda: clock.seconds, sleep=advance,
    ))
    process = Mock(pid=12345)
    process.poll.return_value = None
    process.wait.return_value = 0
    monkeypatch.setattr(process_manager.subprocess, "Popen", Mock(return_value=process))
    terminate = Mock()
    monkeypatch.setattr(process_manager, "terminate_process", terminate)
    monkeypatch.setattr(process_manager, "_running_processes", lambda *args: [])
    config = tmp_path / "config.yaml"
    record = ProcessRecord(12345, str(config), "start", "127.0.0.1", 8080, True)
    monkeypatch.setattr(process_manager, "_read_record", lambda *args: record)
    monkeypatch.setattr(process_manager, "_record_is_running", lambda *args: True)
    monkeypatch.setattr(process_manager, "_check_health", lambda *args, **kwargs: (12345, None))
    return SimpleNamespace(clock=clock, process=process, terminate=terminate, record=record, config=config)


def test_default_budget_accepts_service_ready_after_five_seconds(simulated_start, monkeypatch) -> None:
    """确认耗时 8 秒的正常服务应成功，并在就绪时立即返回。"""

    state = simulated_start
    monkeypatch.setattr(process_manager, "_check_health", lambda *args, **kwargs:
        (12345, None) if state.clock.seconds >= 8 else (None, "健康接口无法连接")
    )
    result = start_background(state.config, host="127.0.0.1", port=8080)
    assert result.exit_code == 0
    assert 8 <= state.clock.seconds < 9
    state.terminate.assert_not_called()


@pytest.mark.parametrize("failure, expected", [
    ("record", "PID 文件尚未就绪"),
    ("record_pid", "PID 文件不匹配：期望 12345，实际 23456"),
    ("identity", "进程身份检查未通过"),
    ("connection", "健康接口无法连接"),
    ("health_pid", "健康接口 PID 不匹配：期望 12345，实际 999999"),
])
def test_startup_timeout_reports_cause_and_reaps_own_child(
    simulated_start, monkeypatch, failure, expected,
) -> None:
    """超时保留最后未满足条件，并且只终止本次启动的子进程。"""

    state = simulated_start
    if failure == "record":
        monkeypatch.setattr(process_manager, "_read_record", lambda *args: None)
    elif failure == "record_pid":
        monkeypatch.setattr(process_manager, "_read_record", lambda *args:
            ProcessRecord(23456, str(state.config), "other", "127.0.0.1", 8080, True))
    elif failure == "identity":
        monkeypatch.setattr(process_manager, "_record_is_running", lambda *args: False)
    else:
        health = (None, "健康接口无法连接") if failure == "connection" else (999999, None)
        monkeypatch.setattr(process_manager, "_check_health", lambda *args, **kwargs: health)
    result = start_background(state.config, host="127.0.0.1", port=8080, startup_timeout_seconds=7.5)
    assert result.exit_code == 1
    assert "7.5 秒" in result.message
    assert expected in result.message
    assert state.clock.seconds == pytest.approx(7.5)
    state.terminate.assert_called_once_with(12345, force=False, tree=True)
    state.process.wait.assert_called_once_with(timeout=3)


@pytest.fixture
def health_server():
    """提供可指定状态和响应内容的真实本机 HTTP 服务。"""

    class Handler(_OccupiedHealthHandler):
        def do_GET(self):
            """返回测试指定的健康响应。"""

            self.send_response(self.server.response_status)
            self.send_header("Content-Length", str(len(self.server.payload)))
            self.end_headers()
            self.wfile.write(self.server.payload)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.response_status = 200
    server.payload = json.dumps({"pid": os.getpid()}).encode()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_health_check_bypasses_environment_proxy(health_server, monkeypatch) -> None:
    """即使 NO_PROXY 未配置且环境代理不可连接，健康接口仍应直连成功。"""

    for name in ("http_proxy", "HTTP_PROXY", "https_proxy", "HTTPS_PROXY", "ALL_PROXY", "all_proxy"):
        monkeypatch.setenv(name, "http://127.0.0.1:1")
    monkeypatch.setenv("no_proxy", "")
    monkeypatch.setenv("NO_PROXY", "")
    pid, error = _check_health("0.0.0.0", health_server.server_port)
    assert (pid, error) == (os.getpid(), None)
    assert os.environ["http_proxy"] == "http://127.0.0.1:1"


@pytest.mark.parametrize("status, payload, expected", [
    (503, b"busy", "HTTP 503"),
    (200, b"not-json", "UTF-8 JSON"),
    (200, b"{}", "有效 PID"),
    (200, b'{"pid":true}', "有效 PID"),
    (200, b'{"pid":1.5}', "有效 PID"),
])
def test_health_check_explains_invalid_response(health_server, status, payload, expected) -> None:
    """连接成功但响应不符合健康协议时应给出具体诊断。"""

    health_server.response_status = status
    health_server.payload = payload
    pid, error = _check_health("127.0.0.1", health_server.server_port)
    assert pid is None
    assert expected in error


@pytest.mark.parametrize("failure, expected", [
    (TimeoutError(), "连接或读取超时"),
    (urllib.error.URLError(TimeoutError()), "连接或读取超时"),
    (urllib.error.URLError(ConnectionRefusedError()), "无法连接"),
    (IncompleteRead(b"partial"), "HTTP 响应不完整"),
])
def test_health_probe_failures_remain_diagnostic(failure, expected, monkeypatch) -> None:
    """网络和协议异常留在启动重试流程内，不抛出导致收尾中断。"""

    opener = Mock()
    opener.open.side_effect = failure
    monkeypatch.setattr(urllib.request, "build_opener", Mock(return_value=opener))
    pid, error = _check_health("::", 8080, timeout_seconds=0.2)
    assert pid is None
    assert expected in error
    opener.open.assert_called_once_with("http://[::1]:8080/api/health", timeout=0.2)


def test_start_rejects_health_response_from_existing_port_owner(tmp_path) -> None:
    """旧服务占用端口时不能把新子进程误报为启动成功。"""

    server = ThreadingHTTPServer(("127.0.0.1", 0), _OccupiedHealthHandler)
    port = int(server.server_address[1])
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    config_path = _write_config(tmp_path, port)
    try:
        result = start_background(
            config_path,
            host="127.0.0.1",
            port=port,
            startup_timeout_seconds=5,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)

    assert result.exit_code == 1
    assert "后台服务启动失败" in result.message
    assert running_process(config_path) is None


def test_stop_discovers_process_after_pid_and_lock_are_moved(tmp_path) -> None:
    """管理文件被移动后 stop 仍应按命令身份找到并结束服务。"""

    port = _unused_port()
    config_path = _write_config(tmp_path, port)
    started = start_background(
        config_path,
        host="127.0.0.1",
        port=port,
        startup_timeout_seconds=_SUCCESSFUL_STARTUP_TIMEOUT_SECONDS,
    )
    assert started.exit_code == 0, started.message
    assert started.record is not None
    paths = runtime_paths(config_path)
    os.replace(paths.pid_file, tmp_path / "moved.pid")
    if os.name != "nt":
        os.replace(paths.lock_file, tmp_path / "moved.lock")

    try:
        recovered = running_process(config_path)
        assert recovered is not None
        assert recovered.pid == started.record.pid
        stopped = stop_managed_process(config_path, timeout_seconds=10)
    finally:
        cleanup = stop_managed_process(config_path, timeout_seconds=10)
    assert stopped.exit_code == 0, stopped.message
    assert cleanup.exit_code == 0, cleanup.message
    assert running_process(config_path) is None


def test_restart_replaces_process_after_runtime_files_are_moved(tmp_path) -> None:
    """restart 必须结束失联旧 PID，并等待新 PID 的健康接口。"""

    port = _unused_port()
    config_path = _write_config(tmp_path, port)
    started = start_background(
        config_path,
        host="127.0.0.1",
        port=port,
        startup_timeout_seconds=_SUCCESSFUL_STARTUP_TIMEOUT_SECONDS,
    )
    assert started.exit_code == 0, started.message
    assert started.record is not None
    paths = runtime_paths(config_path)
    os.replace(paths.pid_file, tmp_path / "restart-moved.pid")
    if os.name != "nt":
        os.replace(paths.lock_file, tmp_path / "restart-moved.lock")

    command = [
        sys.executable,
        "-m",
        "teamwork_review_agents",
        "restart",
        "-c",
        str(config_path),
    ]
    try:
        result = subprocess.run(
            command,
            cwd=tmp_path,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=75 if os.name == "nt" else 30,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        replacement = running_process(config_path)
        assert replacement is not None
        assert replacement.pid != started.record.pid
        assert _wait_health_pid(port) == replacement.pid
        assert f"服务已停止：PID {started.record.pid}" in result.stdout
        assert f"后台服务已启动：PID {replacement.pid}" in result.stdout
    finally:
        stopped = stop_managed_process(config_path, timeout_seconds=10)
    assert stopped.exit_code == 0, stopped.message


@pytest.mark.skipif(
    os.name == "nt",
    reason="Windows 不允许移动正在持锁的文件，正常路径不会产生第二实例",
)
def test_stop_closes_all_managed_processes_for_same_config(tmp_path) -> None:
    """异常产生多个同配置托管实例时 stop 应将它们全部结束。"""

    first_port = _unused_port()
    second_port = _unused_port()
    config_path = _write_config(tmp_path, first_port)
    first = start_background(
        config_path,
        host="127.0.0.1",
        port=first_port,
        startup_timeout_seconds=_SUCCESSFUL_STARTUP_TIMEOUT_SECONDS,
    )
    assert first.exit_code == 0, first.message
    assert first.record is not None
    paths = runtime_paths(config_path)
    os.replace(paths.pid_file, tmp_path / "multiple-moved.pid")
    os.replace(paths.lock_file, tmp_path / "multiple-moved.lock")
    second = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "teamwork_review_agents",
            "run",
            "-c",
            str(config_path),
            "--host",
            "127.0.0.1",
            "--port",
            str(second_port),
            "--managed-child",
        ],
        cwd=tmp_path,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        **process_group_options(),
    )
    try:
        assert _wait_health_pid(second_port) == second.pid
        stopped = stop_managed_process(config_path, timeout_seconds=10)
        assert stopped.exit_code == 0, stopped.message
        assert str(first.record.pid) in stopped.message
        assert str(second.pid) in stopped.message
        assert second.wait(timeout=3) in {0, -signal.SIGTERM}
    finally:
        cleanup = stop_managed_process(config_path, timeout_seconds=10)
        if second.poll() is None:
            second.terminate()
            second.wait(timeout=3)
    assert cleanup.exit_code == 0, cleanup.message
    assert running_process(config_path) is None


@pytest.mark.skipif(os.name == "nt", reason="测试脚本使用 POSIX 会话与 SIGTERM")
def test_stop_reclaims_managed_service_descendant_in_separate_session(tmp_path) -> None:
    """服务自行退出后，stop 仍应回收已脱离服务进程组的 Codex 类后代。"""

    port = _unused_port()
    config_path = _write_config(tmp_path, port)
    child_pid_file = tmp_path / "managed-descendant.pid"
    service_stub = tmp_path / "managed_service_stub.py"
    service_stub.write_text(
        """import signal
import subprocess
import sys
import time

signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
child = subprocess.Popen(
    [sys.executable, "-c", "import time;time.sleep(30)"],
    start_new_session=True,
)
open(sys.argv[-1], "w", encoding="utf-8").write(str(child.pid))
time.sleep(30)
""",
        encoding="utf-8",
    )
    parent = subprocess.Popen(
        [
            sys.executable,
            str(service_stub),
            "-m",
            "teamwork_review_agents",
            "run",
            "-c",
            str(config_path),
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--managed-child",
            str(child_pid_file),
        ],
        cwd=tmp_path,
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

        stopped = stop_managed_process(config_path, timeout_seconds=5)

        assert stopped.exit_code == 0, stopped.message
        assert not pid_exists(parent.pid)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and pid_exists(child_pid):
            time.sleep(0.05)
        assert not pid_exists(child_pid)
    finally:
        if pid_exists(parent.pid):
            terminate_process(parent.pid, force=True, tree=True)
        if child_pid and pid_exists(child_pid):
            terminate_process(child_pid, force=True, tree=False)
