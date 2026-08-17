"""后台服务进程管理集成测试。"""

from __future__ import annotations

import socket
import time
import urllib.request

import yaml

from teamwork_review_agents.process_manager import (
    running_process,
    start_background,
    stop_managed_process,
)


def _unused_port() -> int:
    """向操作系统申请一个暂时未使用的本机端口。"""

    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        return int(server.getsockname()[1])


def test_background_service_starts_and_stops(tmp_path) -> None:
    """后台子进程应能提供健康检查并由 stop 优雅结束。"""

    port = _unused_port()
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
        deadline = time.monotonic() + 5
        while True:
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/api/health",
                    timeout=1,
                ) as response:
                    assert response.status == 200
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.1)
    finally:
        stopped = stop_managed_process(config_path, timeout_seconds=10)
    assert stopped.exit_code == 0, stopped.message
    assert running_process(config_path) is None
