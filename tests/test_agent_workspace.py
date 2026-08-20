"""Agent 工作区准备步骤与仓库级缓存测试。"""

from __future__ import annotations

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
        "workspace.prepare.started",
        "workspace.prepare.output",
        "workspace.prepare.step_started",
        "workspace.prepare.output",
        "workspace.prepare.step_completed",
        "workspace.prepare.completed",
    ]


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
