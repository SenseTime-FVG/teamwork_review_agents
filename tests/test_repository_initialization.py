"""基础仓库初始化、更新、共享锁和管理 API 测试。"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

import yaml
from fastapi.testclient import TestClient

from teamwork_review_agents.webapp import create_app
from teamwork_review_agents.workspace import repository_git_lock_key


def run_git(*arguments: str, cwd: Path | None = None) -> str:
    """运行测试仓库使用的 Git 命令。"""

    result = subprocess.run(
        ["git", *arguments],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def create_origin(tmp_path: Path) -> tuple[Path, Path]:
    """创建具有一个 main 提交的本地裸仓库。"""

    origin = tmp_path / "remote.git"
    source = tmp_path / "source"
    run_git("init", "--bare", str(origin))
    source.mkdir()
    run_git("init", "--initial-branch=main", cwd=source)
    run_git("config", "user.name", "Test User", cwd=source)
    run_git("config", "user.email", "test@example.com", cwd=source)
    (source / "README.md").write_text("首次提交\n", encoding="utf-8")
    run_git("add", "README.md", cwd=source)
    run_git("commit", "-m", "初始化", cwd=source)
    run_git("remote", "add", "origin", str(origin), cwd=source)
    run_git("push", "origin", "main", cwd=source)
    run_git("--git-dir", str(origin), "symbolic-ref", "HEAD", "refs/heads/main")
    return origin, source


def write_config(
    tmp_path: Path,
    origin: Path,
    workspace: Path,
    *,
    enabled: bool = True,
) -> Path:
    """写入只包含一个仓库的最小服务配置。"""

    document = {
        "database": {"path": str(tmp_path / "state.db")},
        "runtime": {
            "git_timeout_seconds": 10,
            "lock_timeout_seconds": 5,
            "lock_ttl_seconds": 10,
        },
        "providers": {
            "github-main": {
                "kind": "github",
                "base_url": "https://api.github.com",
                "token_env": "GITHUB_TOKEN",
            }
        },
        "repositories": [
            {
                "id": "demo",
                "provider": "github-main",
                "project": "owner/demo",
                "clone_url": str(origin),
                "workspace": str(workspace),
                "enabled": enabled,
            }
        ],
        "agents": {},
        "rules": [],
    }
    path = tmp_path / "config.yaml"
    path.write_text(
        yaml.safe_dump(document, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    return path


def wait_for_status(
    client: TestClient,
    expected: set[str],
    *,
    timeout_seconds: float = 5,
) -> dict:
    """轮询单仓库状态直到进入预期集合。"""

    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        response = client.get("/api/repositories/demo/workspace")
        assert response.status_code == 200
        item = response.json()
        if item["status"] in expected:
            return item
        time.sleep(0.02)
    raise AssertionError(f"基础仓库没有进入预期状态：{expected}")


def test_repository_workspace_can_initialize_and_update(tmp_path: Path) -> None:
    """首次操作应克隆仓库，后续操作应只执行增量更新。"""

    origin, source = create_origin(tmp_path)
    workspace = tmp_path / "managed" / "demo"
    app = create_app(
        write_config(tmp_path, origin, workspace),
        start_scheduler=False,
    )

    with TestClient(app) as client:
        initial = client.get("/api/repositories/workspaces")
        assert initial.status_code == 200
        assert initial.json()[0]["status"] == "uninitialized"
        assert "clone_url" not in initial.json()[0]
        assert "project" not in initial.json()[0]
        assert str(origin) not in json.dumps(initial.json(), ensure_ascii=False)

        started = client.post(
            "/api/repositories/demo/workspace/initialize"
        )
        assert started.status_code == 200
        ready = wait_for_status(client, {"ready"})
        assert ready["ready"] is True
        assert ready["operation"] == "initialize"
        assert ready["size_bytes"] > 0
        detail = client.get(
            "/api/repositories/demo/workspace/details"
        ).json()
        clone_command = next(
            command
            for command in detail["commands"]
            if command["operation"] == "克隆基础仓库"
        )
        assert clone_command["state"] == "completed"
        assert clone_command["timeout_seconds"] == 1800
        assert clone_command["command"].startswith("git clone ")
        assert run_git("rev-parse", "HEAD", cwd=workspace) == run_git(
            "rev-parse",
            "HEAD",
            cwd=source,
        )

        (source / "README.md").write_text("第二次提交\n", encoding="utf-8")
        run_git("add", "README.md", cwd=source)
        run_git("commit", "-m", "更新", cwd=source)
        updated_sha = run_git("rev-parse", "HEAD", cwd=source)
        run_git("push", "origin", "main", cwd=source)

        updated = client.post(
            "/api/repositories/demo/workspace/initialize"
        )
        assert updated.status_code == 200
        ready = wait_for_status(client, {"ready"})
        assert ready["operation"] == "update"
        detail = client.get(
            "/api/repositories/demo/workspace/details"
        ).json()
        update_command = next(
            command
            for command in detail["commands"]
            if command["operation"] == "更新基础仓库"
        )
        assert update_command["timeout_seconds"] == 10
        assert run_git("rev-parse", "origin/main", cwd=workspace) == updated_sha


def test_repository_workspace_wait_can_be_deduplicated_and_cancelled(
    tmp_path: Path,
) -> None:
    """共享仓库锁被占用时应等待、复用同一任务，并支持取消。"""

    origin, _ = create_origin(tmp_path)
    workspace = tmp_path / "managed" / "demo"
    app = create_app(
        write_config(tmp_path, origin, workspace),
        start_scheduler=False,
    )

    with TestClient(app) as client:
        manager = app.state.config_manager
        repository = manager.config.repository_map()["demo"]
        lock_key = repository_git_lock_key(repository)
        assert manager.store.acquire_locks([lock_key], "test-blocker", 30)
        try:
            first = client.post(
                "/api/repositories/demo/workspace/initialize"
            ).json()
            waiting = wait_for_status(client, {"waiting"})
            second = client.post(
                "/api/repositories/demo/workspace/initialize"
            ).json()
            assert first["started_at"] == waiting["started_at"]
            assert second["started_at"] == waiting["started_at"]

            cancelled = client.post(
                "/api/repositories/demo/workspace/cancel"
            )
            assert cancelled.status_code == 200
            final = wait_for_status(client, {"cancelled"})
            assert final["cancel_requested"] is True
            assert not workspace.exists()
        finally:
            manager.store.release_locks([lock_key], "test-blocker")


def test_disabled_repository_cannot_be_initialized(tmp_path: Path) -> None:
    """未启用仓库只能查看状态，不能启动写操作。"""

    origin, _ = create_origin(tmp_path)
    workspace = tmp_path / "managed" / "demo"
    app = create_app(
        write_config(tmp_path, origin, workspace, enabled=False),
        start_scheduler=False,
    )

    with TestClient(app) as client:
        response = client.post(
            "/api/repositories/demo/workspace/initialize"
        )
        assert response.status_code == 409
        assert not workspace.exists()


def test_repository_detail_exposes_agent_git_steps_without_raw_output(
    tmp_path: Path,
) -> None:
    """仓库详情应聚合 Agent Git 日志，并只返回安全命令元数据。"""

    origin, _ = create_origin(tmp_path)
    workspace = tmp_path / "managed" / "demo"
    app = create_app(
        write_config(tmp_path, origin, workspace),
        start_scheduler=False,
    )

    with TestClient(app) as client:
        store = app.state.config_manager.store
        reservation = store.begin_agent_run(
            proposed_run_id="run-git-detail",
            root_run_id=None,
            parent_run_id=None,
            idempotency_key="git-detail-test",
            event_id=None,
            rule_name="review",
            agent_name="reviewer",
            resource_key="github:demo:7",
            prompt="测试",
            max_attempts=1,
        )
        assert reservation is not None
        assert store.mark_agent_run_preparing(reservation.run_id)
        store.append_run_log(
            reservation.run_id,
            stream="system",
            event_type="workspace.git.started",
            payload={
                "command_id": "command-1",
                "operation": "克隆基础仓库",
                "command": "git clone git@github.com:owner/demo.git /tmp/demo",
                "state": "started",
                "elapsed_seconds": 0,
                "timeout_seconds": 1800,
                "started_at": time.time(),
                "finished_at": None,
                "exit_code": None,
                "error": None,
                "source": "agent",
                "repository_id": "demo",
                "run_id": reservation.run_id,
            },
        )

        status = client.get("/api/repositories/demo/workspace").json()
        assert status["status"] == "initializing"
        assert status["detail_source"] == "agent"
        detail = client.get(
            "/api/repositories/demo/workspace/details"
        ).json()
        assert detail["source"] == "agent"
        assert detail["run_id"] == reservation.run_id
        assert detail["commands"][0]["command_id"] == "command-1"
        encoded = json.dumps(detail, ensure_ascii=False)
        assert "stdout" not in encoded
        assert "stderr" not in encoded
