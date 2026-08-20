"""Agent 工作区默认分支手动预热 API 测试。"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import yaml
from fastapi.testclient import TestClient

from teamwork_review_agents.webapp import create_app


def _run_git(*arguments: str, cwd: Path | None = None) -> str:
    """运行本地预热测试所需的 Git 命令。"""

    process = subprocess.run(
        ["git", *arguments],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return process.stdout.strip()


def _create_origin(tmp_path: Path) -> Path:
    """创建包含默认 main 分支和依赖锁文件的本地远端。"""

    origin = tmp_path / "remote.git"
    source = tmp_path / "source"
    _run_git("init", "--bare", str(origin))
    source.mkdir()
    _run_git("init", "--initial-branch=main", cwd=source)
    _run_git("config", "user.name", "Test User", cwd=source)
    _run_git("config", "user.email", "test@example.com", cwd=source)
    (source / "package-lock.json").write_text(
        '{"lockfileVersion": 3}\n',
        encoding="utf-8",
    )
    _run_git("add", "package-lock.json", cwd=source)
    _run_git("commit", "-m", "初始化依赖", cwd=source)
    _run_git("remote", "add", "origin", str(origin), cwd=source)
    _run_git("push", "origin", "main", cwd=source)
    _run_git("--git-dir", str(origin), "symbolic-ref", "HEAD", "refs/heads/main")
    return origin


def _write_config(tmp_path: Path, origin: Path) -> Path:
    """写入启用准备步骤和仓库级快照缓存的最小配置。"""

    document = {
        "database": {"path": str(tmp_path / "state.db")},
        "runtime": {
            "git_timeout_seconds": 10,
            "repository_initialization_timeout_seconds": 10,
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
                "workspace": str(tmp_path / "managed" / "demo"),
                "agent_workspace": {
                    "cache_enabled": True,
                    "prepare_steps": [
                        {
                            "name": "准备依赖",
                            "command": [
                                sys.executable,
                                "-c",
                                (
                                    "from pathlib import Path; "
                                    "Path('node_modules').mkdir(); "
                                    "Path('node_modules/ready.txt').write_text('ready')"
                                ),
                            ],
                        }
                    ],
                },
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


def _wait_for_warmup(client: TestClient) -> dict:
    """轮询手动预热直至完成或失败。"""

    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        response = client.get("/api/repositories/demo/workspace/warmup")
        assert response.status_code == 200
        status = response.json()
        if status["status"] in {"ready", "failed", "cancelled"}:
            return status
        time.sleep(0.02)
    raise AssertionError("Agent 工作区预热未在期限内完成")


def test_repository_workspace_warmup_creates_reusable_snapshot(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """仓库页预热应检出默认分支、实时记录步骤并保存快照。"""

    origin = _create_origin(tmp_path)
    monkeypatch.setattr(
        "teamwork_review_agents.agent_workspace.inspect_managed_sandbox",
        lambda *_args, **_kwargs: SimpleNamespace(available=True, error=None),
    )
    monkeypatch.setattr(
        "teamwork_review_agents.agent_workspace.wrap_managed_sandbox_command",
        lambda *, inner_command, **_kwargs: inner_command,
    )
    app = create_app(_write_config(tmp_path, origin), start_scheduler=False)

    with TestClient(app) as client:
        initial = client.get("/api/repositories/demo/workspace/warmup")
        assert initial.status_code == 200
        assert initial.json()["status"] == "uninitialized"

        started = client.post("/api/repositories/demo/workspace/warmup/start")
        assert started.status_code == 200
        final = _wait_for_warmup(client)
        assert final["status"] == "ready", final.get("error")
        assert final["snapshot_count"] == 1
        assert final["latest"]["artifact_count"] == 1
        assert final["latest"]["source_head"] == final["head_sha"]
        event_types = [item["event_type"] for item in final["logs"]]
        assert "workspace.prepare.started" in event_types
        assert "workspace.snapshot.created" in event_types
