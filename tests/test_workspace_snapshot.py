"""仓库级 Agent 工作区快照测试。"""

from __future__ import annotations

import io
import json
import os
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


@pytest.mark.skipif(os.name == "nt", reason="POSIX 链接边界")
@pytest.mark.parametrize("attack", [
    "ordinary_absolute", "foreign_interpreter", "absolute_hardlink", "relative_escape",
    "existing_link_overwrite", "deferred_duplicate", "deferred_child", "deferred_hardlink",
])
def test_python_link_exception_keeps_archive_boundaries(tmp_path, configured_app_factory, attack):
    """标准解释器例外不能放行普通外链、宿主覆盖、重复成员或延后链接攻击。"""

    config = configured_app_factory()
    repository = config.repositories[0]
    repository.workspace = tmp_path / "restore"
    repository.workspace.mkdir()
    trusted = tmp_path / "trusted" / "python"
    trusted.parent.mkdir()
    trusted.write_bytes(b"trusted-interpreter")
    secret = tmp_path / "secret"
    secret.write_bytes(b"private-value")
    repository.agent_workspace = AgentWorkspaceConfig(
        cache_enabled=True, python_venv="ci/.venv",
        prepare_steps=[AgentWorkspacePrepareStepConfig(name="可信解释器", command=[str(trusted), "-m", "venv", "ci/.venv"])],
    )
    fingerprint = "python-boundary"
    directory = workspace_snapshot_root(config, repository) / fingerprint
    directory.mkdir(parents=True)
    member = tarfile.TarInfo("ci/.venv/bin/python")
    member.type = tarfile.SYMTYPE
    member.linkname = str(trusted)
    extra = None
    if attack == "ordinary_absolute":
        member.name = "node_modules/ordinary"
    elif attack == "foreign_interpreter":
        member.linkname = str(secret)
    elif attack == "absolute_hardlink":
        member.type = tarfile.LNKTYPE
    elif attack == "relative_escape":
        member.linkname = "../../../../secret"
    elif attack == "existing_link_overwrite":
        target = repository.workspace / member.name
        target.parent.mkdir(parents=True)
        target.symlink_to(trusted)
        member.type = tarfile.REGTYPE
        member.size = 6
    elif attack in {"deferred_duplicate", "deferred_child"}:
        extra = tarfile.TarInfo(member.name + ("/payload" if attack == "deferred_child" else ""))
        extra.size = 6
    elif attack == "deferred_hardlink":
        extra = tarfile.TarInfo("copy")
        extra.type = tarfile.LNKTYPE
        extra.linkname = member.name
    archive = directory / ARCHIVE_FILE_NAME
    with tarfile.open(archive, "w") as bundle:
        bundle.addfile(member, io.BytesIO(b"unsafe") if member.isfile() else None)
        if extra is not None:
            bundle.addfile(extra, io.BytesIO(b"unsafe") if extra.isfile() else None)
    (directory / METADATA_FILE_NAME).write_text(json.dumps({"fingerprint": fingerprint}))
    with pytest.raises(WorkspaceSnapshotError):
        restore_workspace_snapshot(config, repository, fingerprint)
    assert trusted.read_bytes() == b"trusted-interpreter"
    assert secret.read_bytes() == b"private-value"


def test_snapshot_creation_does_not_publish_unrestorable_absolute_link(tmp_path, configured_app_factory):
    """创建阶段就拒绝普通绝对外链，不能把明知不可恢复的归档标成已就绪。"""

    config = configured_app_factory()
    repository = config.repositories[0]
    _initialize_git_workspace(repository.workspace)
    outside = tmp_path / "outside"
    outside.write_text("private")
    try:
        (repository.workspace / "ordinary-link").symlink_to(outside)
    except OSError:
        pytest.skip("当前账号不能创建符号链接")
    with pytest.raises(WorkspaceSnapshotError, match="绝对链接"):
        create_workspace_snapshot(config, repository, "unsafe-link", "signature")
    assert not (workspace_snapshot_root(config, repository) / "unsafe-link").exists()


def test_snapshot_missing_configured_venv_reports_recoverable_restore_error(configured_app_factory):
    """旧快照缺少配置的虚拟环境时，返回可触发准备步骤重建的恢复异常。"""

    config = configured_app_factory()
    repository = config.repositories[0]
    _initialize_git_workspace(repository.workspace)
    repository.agent_workspace.python_venv = ".venv"
    (repository.workspace / "artifact.txt").write_text("旧准备产物")
    create_workspace_snapshot(config, repository, "missing-python", "signature")
    with pytest.raises(WorkspaceSnapshotError, match="Python 环境不可恢复"):
        restore_workspace_snapshot(config, repository, "missing-python")
