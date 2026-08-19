"""本地 Git 工作目录自动管理测试。"""

from __future__ import annotations

import json
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
    _safe_git_command,
    cleanup_expired_worktrees,
    cleanup_run_worktree,
    ensure_isolated_clone,
    ensure_isolated_worktree,
    prepare_change_request_workspace,
    retained_marker_path,
    run_workspace_kind,
    temporary_change_request_worktree,
    validate_isolated_clone,
    validate_linked_workspace,
    validate_run_workspace,
    WorkspaceCancelled,
    WorkspaceError,
    worktree_ref_head,
)


def run_git(*arguments: str, cwd=None) -> str:
    """运行测试用 Git 命令并返回标准输出。"""

    result = subprocess.run(
        ["git", *arguments],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return result.stdout.strip()


def test_git_command_display_removes_url_credentials_and_query() -> None:
    """Git 详情命令不得暴露 URL 用户信息或查询参数。"""

    command = _safe_git_command(
        [
            "clone",
            "https://user:secret@example.com/owner/demo.git?token=hidden",
            "/tmp/demo",
        ]
    )
    assert command == "git clone https://example.com/owner/demo.git /tmp/demo"
    assert "secret" not in command
    assert "hidden" not in command


@pytest.mark.skipif(
    os.name == "nt",
    reason="该用例使用 POSIX shell 构造 Git 子进程，Windows 由跨平台进程树测试覆盖",
)
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
    progress = []

    with pytest.raises(WorkspaceCancelled):
        _run_git(
            [str(child_pid_path)],
            timeout_seconds=5,
            operation="测试 Git 操作",
            cancel_check=child_pid_path.exists,
            progress_callback=progress.append,
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
    assert progress[0].operation == "测试 Git 操作"
    assert progress[0].state == "started"
    assert progress[0].elapsed_seconds == 0
    assert progress[0].command.startswith("git ")
    assert progress[-1].state == "cancelled"
    assert progress[-1].error == "Git 操作已由管理员取消"


@pytest.mark.skipif(
    os.name == "nt",
    reason="该用例使用 POSIX shell 构造 Git 子进程，Windows 由跨平台进程树测试覆盖",
)
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
    assert worktree_ref_head(workspace, "refs/remotes/origin/main") == head_sha
    with pytest.raises(WorkspaceError, match="无法读取目标分支引用"):
        worktree_ref_head(workspace, "refs/remotes/origin/missing")

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

    cloned = ensure_isolated_clone(
        workspace,
        tmp_path / "data" / "worktrees" / "writable-clone",
        change_ref,
        change_ref=change_ref,
    )
    assert run_workspace_kind(cloned) == "clone"
    assert (cloned / ".git").is_dir()
    assert not (cloned / ".git/objects/info/alternates").exists()
    assert validate_isolated_clone(workspace, cloned) == cloned
    assert validate_run_workspace(workspace, cloned) == cloned
    assert run_git("rev-parse", "HEAD", cwd=cloned) == head_sha
    assert run_git("rev-parse", change_ref, cwd=cloned) == head_sha
    assert worktree_ref_head(cloned, "refs/remotes/origin/main") == head_sha
    assert run_git("remote", "get-url", "origin", cwd=cloned) == str(origin)
    run_git("config", "user.name", "Clone User", cwd=cloned)
    run_git("config", "user.email", "clone@example.com", cwd=cloned)
    run_git("switch", "-c", "agent-output", cwd=cloned)
    (cloned / "agent-output.txt").write_text("独立提交\n", encoding="utf-8")
    run_git("add", "agent-output.txt", cwd=cloned)
    run_git("commit", "-m", "增加独立提交", cwd=cloned)
    run_git("push", "origin", "HEAD:refs/heads/agent-output", cwd=cloned)
    assert run_git("branch", "--show-current", cwd=workspace) == "main"
    assert not (workspace / "agent-output.txt").exists()
    cleanup = cleanup_run_worktree(
        workspace,
        cloned,
        run_status="completed",
        starting_head=head_sha,
        retention_days=7,
    )
    assert cleanup.status == "removed"
    assert not cloned.exists()

    retained_clone = ensure_isolated_clone(
        workspace,
        tmp_path / "data" / "worktrees" / "retained-clone",
        change_ref,
        change_ref=change_ref,
    )
    (retained_clone / "待恢复.txt").write_text("不要删除\n", encoding="utf-8")
    cleanup = cleanup_run_worktree(
        workspace,
        retained_clone,
        run_status="completed",
        starting_head=head_sha,
        retention_days=7,
    )
    assert cleanup.status == "retained"
    assert retained_clone.exists()
    removed = cleanup_expired_worktrees(
        workspace,
        retained_clone.parent,
        now=float("inf"),
    )
    assert retained_clone.resolve() in removed
    assert not retained_clone.exists()

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
    target_head_sha = run_git("rev-parse", "HEAD", cwd=source)
    run_git("remote", "add", "origin", str(origin), cwd=source)
    run_git("push", "origin", "main", cwd=source)
    run_git("--git-dir", str(origin), "symbolic-ref", "HEAD", "refs/heads/main")
    (source / "feature.txt").write_text("PR 变更\n", encoding="utf-8")
    run_git("add", "feature.txt", cwd=source)
    run_git("commit", "-m", "增加 PR 变更", cwd=source)
    head_sha = run_git("rev-parse", "HEAD", cwd=source)
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
    config.runtime.managed_sandbox.enabled = False
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
    assert f'"head_sha": "{head_sha}"' in detail["prompt"]
    assert f'"target_head_sha": "{target_head_sha}"' in detail["prompt"]
    assert head_sha != target_head_sha
    log_types = {
        item["event_type"]
        for item in store.list_run_logs(result.run_id)
    }
    assert "workspace.git.started" in log_types
    assert "workspace.git.completed" in log_types
    assert "workspace.prepared" in log_types
    git_payloads = [
        json.loads(item["payload"])
        for item in store.list_run_logs(result.run_id)
        if item["event_type"].startswith("workspace.git.")
    ]
    assert all(payload["source"] == "agent" for payload in git_payloads)
    assert all(payload["run_id"] == result.run_id for payload in git_payloads)
    assert all(payload["command_id"] for payload in git_payloads)
    clone_payload = next(
        payload
        for payload in git_payloads
        if payload["operation"] == "克隆基础仓库"
    )
    assert clone_payload["timeout_seconds"] == 1800
    prepared_payload = json.loads(
        next(
            item["payload"]
            for item in store.list_run_logs(result.run_id)
            if item["event_type"] == "workspace.prepared"
        )
    )
    assert prepared_payload["mode"] == "root-worktree"

    config.agents["code-reviewer"].sandbox = "workspace-write"
    config.agents["code-reviewer"].write_scopes = ["workspace"]
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
    retained_workspace = Path(retained_detail["workspace_path"])
    assert retained_workspace.exists()
    assert (retained_workspace / ".git").is_dir()
    assert "未提交" in retained_detail["workspace_reason"]
    retained_prepared_payload = json.loads(
        next(
            item["payload"]
            for item in store.list_run_logs(retained_result.run_id)
            if item["event_type"] == "workspace.prepared"
        )
    )
    assert retained_prepared_payload["mode"] == "root-clone"

    inherited_result = await AgentExecutor(config, store).execute(
        agent_name="code-reviewer",
        event=event,
        idempotency_key="sub-agent-inherited-clone",
        task="继续检查父 Agent 的本地修改",
        root_run_id=retained_result.root_run_id,
        parent_run_id=retained_result.run_id,
        depth=1,
        inherit_workspace=True,
        parent_workspace=retained_workspace,
    )
    assert inherited_result is not None
    inherited_detail = store.get_run(inherited_result.run_id)
    assert inherited_detail is not None
    assert inherited_detail["workspace_status"] == "inherited"
    assert inherited_detail["workspace_path"] == str(retained_workspace)
    inherited_prepared_payload = json.loads(
        next(
            item["payload"]
            for item in store.list_run_logs(inherited_result.run_id)
            if item["event_type"] == "workspace.prepared"
        )
    )
    assert inherited_prepared_payload["mode"] == "inherited-clone"


def test_temporary_change_request_worktree_isolated_and_removed(
    tmp_path,
    snapshot_factory,
) -> None:
    """CI 检出必须使用临时 worktree，且不能改动持久工作目录。"""

    origin = tmp_path / "origin.git"
    source = tmp_path / "source"
    workspace = tmp_path / "managed" / "demo"
    run_git("init", "--bare", str(origin))
    source.mkdir()
    run_git("init", "--initial-branch=main", cwd=source)
    run_git("config", "user.name", "Test User", cwd=source)
    run_git("config", "user.email", "test@example.com", cwd=source)
    (source / "README.md").write_text("PR 内容\n", encoding="utf-8")
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
    prepare_change_request_workspace(provider, repository, snapshot)
    dirty_file = workspace / "local-notes.txt"
    dirty_file.write_text("保留本地修改\n", encoding="utf-8")

    with temporary_change_request_worktree(provider, repository, snapshot) as checkout:
        checkout_path = checkout
        assert checkout != workspace
        assert run_git("rev-parse", "HEAD", cwd=checkout) == head_sha
        assert (checkout / "README.md").read_text(encoding="utf-8") == "PR 内容\n"

    assert not checkout_path.exists()
    assert dirty_file.read_text(encoding="utf-8") == "保留本地修改\n"
    assert run_git("branch", "--show-current", cwd=workspace) == "main"
