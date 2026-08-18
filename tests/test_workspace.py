"""本地 Git 工作目录自动管理测试。"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from teamwork_review_agents.config import ProviderConfig, RepositoryConfig
from teamwork_review_agents.events import detect_events
from teamwork_review_agents.executor import AgentExecutor
from teamwork_review_agents.state import StateStore
from teamwork_review_agents.workspace import (
    _run_git,
    cleanup_expired_worktrees,
    cleanup_run_worktree,
    ensure_isolated_worktree,
    prepare_change_request_workspace,
    retained_marker_path,
    validate_linked_workspace,
    WorkspaceCancelled,
    WorkspaceError,
)


def run_git(*arguments: str, cwd=None) -> str:
    """运行测试用 Git 命令并返回标准输出。"""

    result = subprocess.run(
        ["git", *arguments],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def test_git_cancel_terminates_process_group_and_reports_safe_progress(
    tmp_path,
    monkeypatch,
) -> None:
    """准备阶段取消应终止 Git 及其子进程，并只报告脱敏阶段信息。"""

    fake_git = tmp_path / "fake-git"
    child_pid_path = tmp_path / "child.pid"
    fake_git.write_text(
        "#!/bin/sh\n"
        "sleep 30 &\n"
        "child=$!\n"
        "printf '%s' \"$child\" > \"$1\"\n"
        "wait \"$child\"\n",
        encoding="utf-8",
    )
    fake_git.chmod(0o755)
    monkeypatch.setattr(
        "teamwork_review_agents.workspace.shutil.which",
        lambda _: str(fake_git),
    )
    progress: list[tuple[str, str, int]] = []

    with pytest.raises(WorkspaceCancelled):
        _run_git(
            [str(child_pid_path)],
            timeout_seconds=5,
            operation="测试 Git 操作",
            cancel_check=child_pid_path.exists,
            progress_callback=lambda operation, state, elapsed: progress.append(
                (operation, state, elapsed)
            ),
        )

    child_pid = int(child_pid_path.read_text(encoding="utf-8"))
    for _ in range(50):
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.02)
    else:
        pytest.fail("Git 子进程在取消后仍然存活")
    assert progress[0] == ("测试 Git 操作", "started", 0)
    assert progress[-1][1] == "cancelled"


def test_git_timeout_terminates_process_group(tmp_path, monkeypatch) -> None:
    """Git 超时应结束整个进程组，而不是只结束直接 git 进程。"""

    fake_git = tmp_path / "fake-git"
    child_pid_path = tmp_path / "timeout-child.pid"
    fake_git.write_text(
        "#!/bin/sh\n"
        "sleep 30 &\n"
        "child=$!\n"
        "printf '%s' \"$child\" > \"$1\"\n"
        "wait \"$child\"\n",
        encoding="utf-8",
    )
    fake_git.chmod(0o755)
    monkeypatch.setattr(
        "teamwork_review_agents.workspace.shutil.which",
        lambda _: str(fake_git),
    )

    with pytest.raises(WorkspaceError, match="超过 1 秒"):
        _run_git([str(child_pid_path)], timeout_seconds=1)

    child_pid = int(child_pid_path.read_text(encoding="utf-8"))
    for _ in range(50):
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.02)
    else:
        pytest.fail("Git 子进程在超时后仍然存活")


def test_workspace_is_cloned_and_change_request_ref_is_fetched(
    tmp_path,
    snapshot_factory,
) -> None:
    """缺失工作目录应自动克隆，并准备稳定的 PR 引用。"""

    origin = tmp_path / "origin.git"
    source = tmp_path / "source"
    workspace = tmp_path / "managed" / "demo"
    run_git("init", "--bare", str(origin))
    source.mkdir()
    run_git("init", "--initial-branch=main", cwd=source)
    run_git("config", "user.name", "Test User", cwd=source)
    run_git("config", "user.email", "test@example.com", cwd=source)
    (source / "README.md").write_text("测试仓库\n", encoding="utf-8")
    run_git("add", "README.md", cwd=source)
    run_git("commit", "-m", "初始化", cwd=source)
    head_sha = run_git("rev-parse", "HEAD", cwd=source)
    run_git("remote", "add", "origin", str(origin), cwd=source)
    run_git("push", "origin", "main", cwd=source)
    run_git("--git-dir", str(origin), "symbolic-ref", "HEAD", "refs/heads/main")
    run_git("push", "origin", "HEAD:refs/pull/7/head", cwd=source)

    provider = ProviderConfig(
        kind="github",
        base_url="https://api.github.com",
        token_env="GITHUB_TOKEN",
    )
    repository = RepositoryConfig(
        id="demo",
        provider="github-main",
        project="owner/demo",
        clone_url=str(origin),
        workspace=workspace,
    )
    snapshot = snapshot_factory(
        provider="github-main",
        repository_id="demo",
        number=7,
        head_sha=head_sha,
    )

    change_ref = prepare_change_request_workspace(provider, repository, snapshot)

    assert change_ref == "refs/teamwork/change-requests/7/head"
    assert (workspace / ".git").exists()
    assert run_git("rev-parse", change_ref, cwd=workspace) == head_sha

    (workspace / "parent-only.txt").write_text("父工作区未提交文件\n", encoding="utf-8")
    isolated = ensure_isolated_worktree(
        workspace,
        tmp_path / "data" / "worktrees" / "child",
        change_ref,
    )
    assert isolated != workspace.resolve()
    assert run_git("rev-parse", "HEAD", cwd=isolated) == head_sha
    assert not (isolated / "parent-only.txt").exists()
    assert validate_linked_workspace(workspace, workspace) == workspace.resolve()
    assert ensure_isolated_worktree(workspace, isolated, change_ref) == isolated

    cleanup = cleanup_run_worktree(
        workspace,
        isolated,
        run_status="completed",
        starting_head=head_sha,
        retention_days=7,
    )
    assert cleanup.status == "removed"
    assert not isolated.exists()

    retained = ensure_isolated_worktree(
        workspace,
        tmp_path / "data" / "worktrees" / "retained",
        change_ref,
    )
    (retained / "未提交.txt").write_text("需要恢复\n", encoding="utf-8")
    cleanup = cleanup_run_worktree(
        workspace,
        retained,
        run_status="completed",
        starting_head=head_sha,
        retention_days=7,
    )
    assert cleanup.status == "retained"
    assert retained.exists()
    assert retained_marker_path(retained).exists()

    removed = cleanup_expired_worktrees(
        workspace,
        retained.parent,
        now=float("inf"),
    )
    assert removed == [retained.resolve()]
    assert not retained.exists()
    assert not retained_marker_path(retained).exists()


async def test_root_agent_runs_in_its_own_temporary_worktree(
    tmp_path,
    snapshot_factory,
    configured_app_factory,
) -> None:
    """根 Agent 不应直接在基础仓库运行，干净结束后应删除临时目录。"""

    origin = tmp_path / "agent-origin.git"
    source = tmp_path / "agent-source"
    run_git("init", "--bare", str(origin))
    source.mkdir()
    run_git("init", "--initial-branch=main", cwd=source)
    run_git("config", "user.name", "Test User", cwd=source)
    run_git("config", "user.email", "test@example.com", cwd=source)
    (source / "README.md").write_text("Agent 测试\n", encoding="utf-8")
    run_git("add", "README.md", cwd=source)
    run_git("commit", "-m", "初始化 Agent 测试", cwd=source)
    head_sha = run_git("rev-parse", "HEAD", cwd=source)
    run_git("remote", "add", "origin", str(origin), cwd=source)
    run_git("push", "origin", "main", cwd=source)
    run_git("--git-dir", str(origin), "symbolic-ref", "HEAD", "refs/heads/main")
    run_git("push", "origin", "HEAD:refs/pull/7/head", cwd=source)

    fake_codex = tmp_path / "fake-codex"
    fake_codex.write_text(
        f"""#!{sys.executable}
import json
import sys
sys.stdin.read()
print(json.dumps({{"type": "item.completed", "item": {{"type": "agent_message", "text": "完成"}}}}, ensure_ascii=False), flush=True)
""",
        encoding="utf-8",
    )
    fake_codex.chmod(0o755)

    config = configured_app_factory()
    config.runtime.codex_binary = str(fake_codex)
    repository = config.repositories[0]
    repository.clone_url = str(origin)
    repository.workspace = tmp_path / "base-repository"
    snapshot = snapshot_factory(
        provider=repository.provider,
        repository_id=repository.id,
        head_sha=head_sha,
    )
    event = detect_events(None, snapshot, emit_initial=True)[0]
    store = StateStore(config.database.path)
    store.initialize()

    result = await AgentExecutor(config, store).execute(
        agent_name="code-reviewer",
        event=event,
        idempotency_key="root-worktree-run",
        rule_name="review",
    )

    assert result is not None
    detail = store.get_run(result.run_id)
    assert detail is not None
    assert detail["workspace_status"] == "removed"
    assert detail["workspace_path"] != str(repository.workspace.resolve())
    assert not Path(detail["workspace_path"]).exists()
    log_types = {
        item["event_type"]
        for item in store.list_run_logs(result.run_id)
    }
    assert "workspace.git.started" in log_types
    assert "workspace.git.completed" in log_types
    assert "workspace.prepared" in log_types

    fake_codex.write_text(
        f"""#!{sys.executable}
import json
import sys
from pathlib import Path
sys.stdin.read()
Path("agent-change.txt").write_text("尚未提交\\n", encoding="utf-8")
print(json.dumps({{"type": "item.completed", "item": {{"type": "agent_message", "text": "完成但有修改"}}}}, ensure_ascii=False), flush=True)
""",
        encoding="utf-8",
    )
    fake_codex.chmod(0o755)
    retained_result = await AgentExecutor(config, store).execute(
        agent_name="code-reviewer",
        event=event,
        idempotency_key="root-worktree-retained",
        rule_name="review",
    )

    assert retained_result is not None
    retained_detail = store.get_run(retained_result.run_id)
    assert retained_detail is not None
    assert retained_detail["workspace_status"] == "retained"
    assert Path(retained_detail["workspace_path"]).exists()
    assert "未提交" in retained_detail["workspace_reason"]
