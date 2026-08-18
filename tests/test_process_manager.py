"""后台服务进程管理集成测试。"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import yaml

from teamwork_review_agents.process_control import process_group_options
from teamwork_review_agents.process_manager import (
    running_process,
    runtime_paths,
    start_background,
    stop_managed_process,
)


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
        startup_timeout_seconds=10,
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
            startup_timeout_seconds=10,
        )
        assert duplicate.exit_code == 0, duplicate.message
        assert duplicate.record is not None
        assert duplicate.record.pid == result.record.pid
    finally:
        stopped = stop_managed_process(config_path, timeout_seconds=10)
    assert stopped.exit_code == 0, stopped.message
    assert running_process(config_path) is None


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
        startup_timeout_seconds=10,
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
        startup_timeout_seconds=10,
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
            timeout=30,
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
        startup_timeout_seconds=10,
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
        assert second.wait(timeout=3) == 0
    finally:
        cleanup = stop_managed_process(config_path, timeout_seconds=10)
        if second.poll() is None:
            second.terminate()
            second.wait(timeout=3)
    assert cleanup.exit_code == 0, cleanup.message
    assert running_process(config_path) is None
