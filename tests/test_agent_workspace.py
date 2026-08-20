"""Agent 工作区准备步骤与仓库级缓存测试。"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from teamwork_review_agents.agent_workspace import prepare_agent_workspace
from teamwork_review_agents.config import (
    AgentConfig,
    AgentWorkspaceConfig,
    AgentWorkspacePrepareStepConfig,
)
from teamwork_review_agents.environment import SecretRedactor
from teamwork_review_agents.workspace_snapshot import (
    ARCHIVE_FILE_NAME,
    workspace_snapshot_root,
)


@pytest.mark.parametrize("cwd", ["/tmp", "../ui", "C:\\temp"])
def test_prepare_step_rejects_paths_outside_repository(cwd: str) -> None:
    """配置阶段应拒绝绝对目录、父目录跳转和 Windows 盘符。"""

    with pytest.raises(ValidationError):
        AgentWorkspacePrepareStepConfig(
            name="安装依赖",
            cwd=cwd,
            command=["npm", "ci"],
        )


def test_prepare_step_normalizes_repository_relative_directory() -> None:
    """跨平台路径分隔符应统一保存为仓库相对路径。"""

    step = AgentWorkspacePrepareStepConfig(
        name="安装依赖",
        cwd=" ui\\client ",
        command=["npm", "ci"],
    )

    assert step.cwd == "ui/client"


@pytest.mark.asyncio
async def test_prepare_agent_workspace_runs_in_configured_directory_and_reuses_cache(
    tmp_path: Path,
    configured_app_factory,
) -> None:
    """用户参数应直接执行，并向不同分支工作区注入同一仓库缓存。"""

    config = configured_app_factory()
    repository = config.repositories[0]
    repository.workspace = tmp_path / "agent-worktree"
    step_directory = repository.workspace / "ui"
    step_directory.mkdir(parents=True)
    subprocess.run(
        ["git", "init", str(repository.workspace)],
        check=True,
        stdout=subprocess.DEVNULL,
    )
    repository.agent_workspace = AgentWorkspaceConfig(
        cache_enabled=True,
        prepare_steps=[
            AgentWorkspacePrepareStepConfig(
                name="准备前端依赖",
                cwd="ui",
                command=[
                    sys.executable,
                    "-c",
                    (
                        "from pathlib import Path; import os; "
                        "Path('prepared.txt').write_text("
                        "os.environ['TEAMWORK_REPOSITORY_CACHE_DIR'], "
                        "encoding='utf-8'); print('prepared')"
                    ),
                ],
            )
        ],
    )
    agent = AgentConfig(prompt="测试", sandbox="danger-full-access")
    events: list[tuple[str, str, str | dict[str, object]]] = []

    async def log_callback(
        stream: str,
        event_type: str,
        payload: str | dict[str, object],
    ) -> None:
        """收集准备过程，验证页面所需的实时事件完整。"""

        events.append((stream, event_type, payload))

    result = await prepare_agent_workspace(
        config=config,
        repository=repository,
        agent=agent,
        process_environment={},
        redactor=SecretRedactor(()),
        log_callback=log_callback,
        cancel_check=lambda: False,
    )

    assert result.outcome.status == "success"
    assert result.cache_root is not None
    assert result.cache_environment["TEAMWORK_REPOSITORY_CACHE_DIR"] == str(
        result.cache_root.resolve()
    )
    assert (step_directory / "prepared.txt").read_text(encoding="utf-8") == str(
        result.cache_root.resolve()
    )
    event_types = [event_type for _, event_type, _ in events]
    assert event_types == [
        "workspace.snapshot.lookup",
        "workspace.snapshot.missed",
        "workspace.prepare.started",
        "workspace.prepare.output",
        "workspace.prepare.step_started",
        "workspace.prepare.output",
        "workspace.prepare.step_completed",
        "workspace.prepare.completed",
        "workspace.snapshot.created",
    ]
    assert result.snapshot_status == "created"

    (step_directory / "prepared.txt").unlink()
    restored_events: list[str] = []

    async def restored_log_callback(
        _stream: str,
        event_type: str,
        _payload: str | dict[str, object],
    ) -> None:
        """第二次运行应直接恢复，不再执行准备命令。"""

        restored_events.append(event_type)

    restored = await prepare_agent_workspace(
        config=config,
        repository=repository,
        agent=agent,
        process_environment={},
        redactor=SecretRedactor(()),
        log_callback=restored_log_callback,
        cancel_check=lambda: False,
    )

    assert restored.outcome.status == "success"
    assert restored.snapshot_status == "restored"
    assert (step_directory / "prepared.txt").exists()
    assert restored_events == [
        "workspace.snapshot.lookup",
        "workspace.snapshot.restored",
    ]


@pytest.mark.asyncio
async def test_inherited_workspace_skips_repeated_prepare_steps(
    tmp_path: Path,
    configured_app_factory,
) -> None:
    """继承父 Agent 工作区的 sub-agent 不应再次执行依赖安装。"""

    config = configured_app_factory()
    repository = config.repositories[0]
    repository.workspace = tmp_path / "inherited-worktree"
    repository.workspace.mkdir()
    repository.agent_workspace = AgentWorkspaceConfig(
        cache_enabled=True,
        prepare_steps=[
            AgentWorkspacePrepareStepConfig(
                name="不应重复执行",
                command=[
                    sys.executable,
                    "-c",
                    "from pathlib import Path; Path('unexpected').touch()",
                ],
            )
        ],
    )
    events: list[str] = []

    async def log_callback(
        _stream: str,
        event_type: str,
        _payload: str | dict[str, object],
    ) -> None:
        """只记录继承判定事件。"""

        events.append(event_type)

    result = await prepare_agent_workspace(
        config=config,
        repository=repository,
        agent=AgentConfig(prompt="测试", sandbox="danger-full-access"),
        process_environment={},
        redactor=SecretRedactor(()),
        log_callback=log_callback,
        cancel_check=lambda: False,
        inherited_workspace=True,
    )

    assert result.outcome.status == "success"
    assert result.snapshot_status == "inherited"
    assert events == ["workspace.prepare.inherited"]
    assert not (repository.workspace / "unexpected").exists()


@pytest.mark.asyncio
async def test_corrupted_snapshot_falls_back_to_prepare_steps(
    tmp_path: Path,
    configured_app_factory,
) -> None:
    """快照损坏时应清理旧归档并重新执行用户准备步骤。"""

    config = configured_app_factory()
    repository = config.repositories[0]
    repository.workspace = tmp_path / "corrupted-worktree"
    repository.workspace.mkdir()
    subprocess.run(
        ["git", "init", str(repository.workspace)],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    repository.agent_workspace = AgentWorkspaceConfig(
        cache_enabled=True,
        prepare_steps=[
            AgentWorkspacePrepareStepConfig(
                name="重新准备",
                command=[
                    sys.executable,
                    "-c",
                    "from pathlib import Path; Path('prepared.bin').write_bytes(b'ok')",
                ],
            )
        ],
    )

    async def discard_log(
        _stream: str,
        _event_type: str,
        _payload: str | dict[str, object],
    ) -> None:
        """首次运行不需要检查日志内容。"""

    first = await prepare_agent_workspace(
        config=config,
        repository=repository,
        agent=AgentConfig(prompt="测试", sandbox="danger-full-access"),
        process_environment={},
        redactor=SecretRedactor(()),
        log_callback=discard_log,
        cancel_check=lambda: False,
    )
    assert first.snapshot_fingerprint is not None
    archive = (
        workspace_snapshot_root(config, repository)
        / first.snapshot_fingerprint
        / ARCHIVE_FILE_NAME
    )
    archive.write_bytes(b"corrupted")
    (repository.workspace / "prepared.bin").unlink()
    events: list[str] = []

    async def collect_log(
        _stream: str,
        event_type: str,
        _payload: str | dict[str, object],
    ) -> None:
        """记录缓存回退路径。"""

        events.append(event_type)

    second = await prepare_agent_workspace(
        config=config,
        repository=repository,
        agent=AgentConfig(prompt="测试", sandbox="danger-full-access"),
        process_environment={},
        redactor=SecretRedactor(()),
        log_callback=collect_log,
        cancel_check=lambda: False,
    )

    assert second.outcome.status == "success"
    assert second.snapshot_status == "created"
    assert (repository.workspace / "prepared.bin").read_bytes() == b"ok"
    assert "workspace.snapshot.restore_failed" in events
    assert "workspace.prepare.started" in events


@pytest.mark.asyncio
async def test_prepare_agent_workspace_rejects_symlink_directory_escape(
    tmp_path: Path,
    configured_app_factory,
) -> None:
    """即使配置路径合法，运行时也不能通过符号链接写出工作区。"""

    config = configured_app_factory()
    repository = config.repositories[0]
    repository.workspace = tmp_path / "agent-worktree"
    repository.workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (repository.workspace / "linked").symlink_to(outside, target_is_directory=True)
    repository.agent_workspace = AgentWorkspaceConfig(
        prepare_steps=[
            AgentWorkspacePrepareStepConfig(
                name="越界写入",
                cwd="linked",
                command=[
                    sys.executable,
                    "-c",
                    "from pathlib import Path; Path('escaped.txt').write_text('bad')",
                ],
            )
        ],
    )

    async def log_callback(
        _stream: str,
        _event_type: str,
        _payload: str | dict[str, object],
    ) -> None:
        """测试不需要持久化日志。"""

    result = await prepare_agent_workspace(
        config=config,
        repository=repository,
        agent=AgentConfig(prompt="测试", sandbox="danger-full-access"),
        process_environment={},
        redactor=SecretRedactor(()),
        log_callback=log_callback,
        cancel_check=lambda: False,
    )

    assert result.outcome.status == "error"
    assert "逃逸" in (result.outcome.error or "")
    assert not (outside / "escaped.txt").exists()
