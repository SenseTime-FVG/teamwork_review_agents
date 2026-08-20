"""仓库级 Agent 工作区快照测试。"""

from __future__ import annotations

import io
import json
import subprocess
import tarfile
import time
from pathlib import Path

import pytest

from teamwork_review_agents.config import (
    AgentWorkspaceConfig,
    AgentWorkspacePrepareStepConfig,
)
from teamwork_review_agents.workspace_snapshot import (
    ARCHIVE_FILE_NAME,
    METADATA_FILE_NAME,
    WorkspaceSnapshotError,
    create_workspace_snapshot,
    restore_workspace_snapshot,
    workspace_snapshot_fingerprint,
    workspace_snapshot_root,
)


def _initialize_git_workspace(path: Path) -> None:
    """创建可供未跟踪产物扫描使用的最小 Git 仓库。"""

    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init", str(path)],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def test_snapshot_fingerprint_reuses_equal_locks_across_branches(
    tmp_path: Path,
    configured_app_factory,
) -> None:
    """分支名不参与指纹，相同锁文件应共享，内容变化后必须失效。"""

    config = configured_app_factory()
    repository = config.repositories[0]
    repository.agent_workspace = AgentWorkspaceConfig(
        cache_enabled=True,
        prepare_steps=[
            AgentWorkspacePrepareStepConfig(
                name="安装依赖",
                command=["npm", "ci"],
            )
        ],
    )
    first = tmp_path / "branch-one"
    second = tmp_path / "branch-two"
    for workspace in (first, second):
        workspace.mkdir()
        (workspace / "package-lock.json").write_text(
            '{"lockfileVersion": 3}\n',
            encoding="utf-8",
        )

    repository.workspace = first
    first_fingerprint, _ = workspace_snapshot_fingerprint(repository, {})
    repository.workspace = second
    second_fingerprint, _ = workspace_snapshot_fingerprint(repository, {})
    assert first_fingerprint == second_fingerprint

    (second / "package-lock.json").write_text(
        '{"lockfileVersion": 3, "changed": true}\n',
        encoding="utf-8",
    )
    changed_fingerprint, _ = workspace_snapshot_fingerprint(repository, {})
    assert changed_fingerprint != first_fingerprint


def test_snapshot_lru_keeps_only_three_recent_versions(
    tmp_path: Path,
    configured_app_factory,
) -> None:
    """同一仓库创建第四份快照时应清理最旧版本。"""

    config = configured_app_factory()
    repository = config.repositories[0]
    repository.workspace = tmp_path / "workspace"
    _initialize_git_workspace(repository.workspace)
    repository.agent_workspace = AgentWorkspaceConfig(
        cache_enabled=True,
        prepare_steps=[
            AgentWorkspacePrepareStepConfig(
                name="准备",
                command=["tool", "prepare"],
            )
        ],
    )
    artifact = repository.workspace / "dependency.bin"
    artifact.write_bytes(b"dependency")

    for index in range(4):
        metadata = create_workspace_snapshot(
            config,
            repository,
            f"fingerprint-{index}",
            "signature",
        )
        assert metadata is not None
        time.sleep(0.002)

    directories = sorted(
        item.name
        for item in workspace_snapshot_root(config, repository).iterdir()
        if item.is_dir() and not item.name.startswith(".")
    )
    assert directories == [
        "fingerprint-1",
        "fingerprint-2",
        "fingerprint-3",
    ]


def test_snapshot_restore_rejects_parent_directory_escape(
    tmp_path: Path,
    configured_app_factory,
) -> None:
    """即使归档元数据完整，恢复也必须拒绝写出工作区。"""

    config = configured_app_factory()
    repository = config.repositories[0]
    repository.workspace = tmp_path / "workspace"
    _initialize_git_workspace(repository.workspace)
    fingerprint = "unsafe-snapshot"
    directory = workspace_snapshot_root(config, repository) / fingerprint
    directory.mkdir(parents=True)
    archive = directory / ARCHIVE_FILE_NAME
    with tarfile.open(archive, mode="w") as bundle:
        member = tarfile.TarInfo("../escaped.txt")
        payload = b"unsafe"
        member.size = len(payload)
        bundle.addfile(member, io.BytesIO(payload))
    (directory / METADATA_FILE_NAME).write_text(
        json.dumps(
            {
                "fingerprint": fingerprint,
                "created_at": time.time(),
                "last_used_at": time.time(),
                "size_bytes": archive.stat().st_size,
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(WorkspaceSnapshotError, match="不安全路径"):
        restore_workspace_snapshot(
            config,
            repository,
            fingerprint,
        )
    assert not (tmp_path / "escaped.txt").exists()
