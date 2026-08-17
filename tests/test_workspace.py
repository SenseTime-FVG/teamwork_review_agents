"""本地 Git 工作目录自动管理测试。"""

from __future__ import annotations

import subprocess

from teamwork_review_agents.config import ProviderConfig, RepositoryConfig
from teamwork_review_agents.workspace import prepare_change_request_workspace


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
