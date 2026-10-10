"""工作区 Python 接入、真实探针及快照环境恢复的回归测试。"""

from __future__ import annotations

import os
import subprocess
import sys
import venv
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
from teamwork_review_agents.model_tools import shell_command
from teamwork_review_agents.preflight import StepExecutionOutcome
from teamwork_review_agents.workspace_python import (
    apply_workspace_python_environment,
    workspace_python_paths,
)
from teamwork_review_agents.workspace_snapshot import (
    ARCHIVE_FILE_NAME,
    workspace_snapshot_root,
)


@pytest.mark.parametrize("path", ["../other", "/tmp/venv", "C:\\venv", "."])
def test_venv_rejects_outside_paths(path):
    """配置不能选择宿主目录或整个工作区作为虚拟环境。"""

    with pytest.raises(ValidationError):
        AgentWorkspaceConfig(python_venv=path)


def test_python_configuration_normalizes_and_validates_modules():
    """模块仅接受导入名称，不能混入表达式；旧配置保持关闭。"""

    assert AgentWorkspaceConfig().python_venv is None
    assert AgentWorkspaceConfig(python_venv=" ").python_venv is None
    settings = AgentWorkspaceConfig(
        python_venv=" env\\test ", python_check_modules=[" json ", "json", "xml.etree"]
    )
    assert settings.python_venv == "env/test"
    assert settings.python_check_modules == ["json", "xml.etree"]
    for values in (["json; print(1)"], ["../json"], [""]):
        with pytest.raises(ValidationError):
            AgentWorkspaceConfig(python_venv=".venv", python_check_modules=values)
    with pytest.raises(ValidationError):
        AgentWorkspaceConfig(python_check_modules=["pytest"])


def test_python_environment_rejects_symlink_escape(tmp_path):
    """配置相对目录仍不能通过符号链接指向宿主虚拟环境。"""

    workspace = tmp_path / "checkout"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        (workspace / ".venv").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("当前平台没有创建测试符号链接的权限")
    with pytest.raises(ValueError, match="逃逸"):
        workspace_python_paths(workspace, ".venv")


def test_windows_environment_overrides_remove_case_aliases():
    """Windows 混合大小写的 PATH 和 PYTHONHOME 也不能覆盖准备结果。"""

    environment = {
        "Path": "old",
        "PythonHome": "wrong",
        "Virtual_Env": "old",
        "TOKEN": "keep",
    }
    overrides = {
        "PATH": "prepared",
        "VIRTUAL_ENV": "new",
        "TEAMWORK_WORKSPACE_PYTHON": "new/python",
    }
    apply_workspace_python_environment(environment, overrides)
    assert environment == {**overrides, "TOKEN": "keep"}


@pytest.fixture
def python_workspace(configured_app_factory, tmp_path):
    """创建不联网、不安装第三方依赖的真实 Python 虚拟环境场景。"""

    config = configured_app_factory()
    repository = config.repositories[0]
    repository.workspace = tmp_path / "checkout"
    repository.workspace.mkdir()
    subprocess.run(
        ["git", "init", str(repository.workspace)], check=True, capture_output=True
    )
    repository.agent_workspace = AgentWorkspaceConfig(
        python_venv=".venv", python_check_modules=["json"]
    )
    events = []

    async def log(stream, event_type, payload):
        """保留事件与脱敏输出，检查成功和失败语义。"""

        events.append((event_type, payload))

    async def prepare(**kwargs):
        """调用真实准备器，允许测试覆盖继承与取消状态。"""

        return await prepare_agent_workspace(
            config=config,
            repository=repository,
            agent=AgentConfig(prompt="测试", sandbox="danger-full-access"),
            process_environment=kwargs.pop("process_environment", {}),
            redactor=SecretRedactor(()),
            log_callback=log,
            cancel_check=kwargs.pop("cancel_check", lambda: False),
            **kwargs,
        )

    return config, repository, events, prepare


@pytest.mark.asyncio
async def test_real_venv_is_used_by_probe_and_shell(python_workspace):
    """真实探针与后续裸 python 命令都必须使用工作区虚拟环境。"""

    _, repository, events, prepare = python_workspace
    venv.EnvBuilder(with_pip=False).create(repository.workspace / ".venv")
    result = await prepare(
        process_environment={"PYTHONHOME": "invalid", "PATH": os.environ["PATH"]}
    )
    assert result.outcome.status == "success"
    assert result.execution_environment["VIRTUAL_ENV"] == str(
        repository.workspace / ".venv"
    )
    assert "-m pytest" in result.runtime_hint
    assert "不代表测试已运行或通过" in result.runtime_hint
    ready = next(
        payload for kind, payload in events if kind == "workspace.python.ready"
    )
    assert ready["checked_modules"] == ["json"]
    environment = dict(os.environ)
    apply_workspace_python_environment(environment, result.execution_environment)
    command = shell_command(
        "python -c 'import sys; print(sys.prefix)'", environment=environment
    )
    if os.name != "nt":
        assert command[1] == "-c"
    completed = subprocess.run(
        command, env=environment, capture_output=True, text=True, check=True
    )
    assert Path(completed.stdout.strip()) == repository.workspace / ".venv"
    # 两种模型运行器都必须保留准备器明确传入的环境覆盖。
    from teamwork_review_agents.codex_model_runner import CodexModelRunner
    from teamwork_review_agents.codex_runner import CodexRunner

    config = python_workspace[0]
    cli_environment = CodexRunner(config).child_environment(
        result.execution_environment
    )
    model_environment = CodexModelRunner(config).child_environment(
        result.execution_environment,
        temporary_home=None,
        tool_codex_home=repository.workspace / "tool-home",
    )
    assert (
        cli_environment["VIRTUAL_ENV"]
        == model_environment["VIRTUAL_ENV"]
        == str(repository.workspace / ".venv")
    )
    assert (
        cli_environment["PATH"]
        == model_environment["PATH"]
        == result.execution_environment["PATH"]
    )


@pytest.mark.asyncio
async def test_missing_module_stops_before_model_without_host_fallback(
    python_workspace,
):
    """模块缺失应成为不可自动重试的环境错误，而不是代码测试失败。"""

    _, repository, events, prepare = python_workspace
    venv.EnvBuilder(with_pip=False).create(repository.workspace / ".venv")
    repository.agent_workspace.python_check_modules = [
        "teamwork_missing_dependency_for_test"
    ]
    result = await prepare()
    assert result.outcome.status == "error"
    assert result.error_code == "workspace_python_environment_unavailable"
    assert result.retryable is False
    assert result.execution_environment is None
    assert "相关测试未执行" in result.outcome.error
    assert "ModuleNotFoundError" in result.outcome.output
    assert any(kind == "workspace.python.failed" for kind, _ in events)


@pytest.mark.asyncio
async def test_missing_venv_and_inherited_venv_are_verified(python_workspace):
    """未安装时不能静默用宿主，继承父目录时仍需接入环境。"""

    _, repository, events, prepare = python_workspace
    result = await prepare(inherited_workspace=True)
    assert result.outcome.status == "error"
    venv.EnvBuilder(with_pip=False).create(repository.workspace / ".venv")
    repository.agent_workspace.prepare_steps = [
        AgentWorkspacePrepareStepConfig(
            name="不能重复安装", command=["nonexistent-install-command"]
        )
    ]
    result = await prepare(inherited_workspace=True)
    assert result.outcome.status == "success"
    assert result.snapshot_status == "inherited"
    assert result.execution_environment["VIRTUAL_ENV"] == str(
        repository.workspace / ".venv"
    )
    assert not any(kind == "workspace.python.rebuilding" for kind, _ in events)


@pytest.mark.asyncio
async def test_bad_cached_python_entry_rebuilds_once(python_workspace, monkeypatch):
    """旧工作区入口不能作为成功快照；只执行一次配置好的重建命令。"""

    config, repository, events, prepare = python_workspace
    repository.agent_workspace.cache_enabled = True
    repository.agent_workspace.prepare_steps = [
        AgentWorkspacePrepareStepConfig(
            name="准备独立 Python",
            command=[
                sys.executable,
                "-c",
                "import venv; venv.EnvBuilder(clear=True, with_pip=False).create('.venv')",
            ],
        )
    ]
    first = await prepare()
    assert first.outcome.status == "success"
    archive = (
        workspace_snapshot_root(config, repository)
        / first.snapshot_fingerprint
        / ARCHIVE_FILE_NAME
    )
    assert archive.exists()
    from teamwork_review_agents import agent_workspace as module

    original_restore = module.restore_workspace_snapshot

    def restore_with_stale_entry(*args, **kwargs):
        """模拟跨运行目录恢复后未重定位的 Python 脚本。"""

        metadata = original_restore(*args, **kwargs)
        scripts = (
            repository.workspace / ".venv" / ("Scripts" if os.name == "nt" else "bin")
        )
        old_python = (
            "Z:/old-checkout/.venv/Scripts/python.exe"
            if os.name == "nt"
            else "/old-checkout/.venv/bin/python"
        )
        (scripts / "pytest").write_text(f"#!{old_python}\n", encoding="utf-8")
        return metadata

    monkeypatch.setattr(module, "restore_workspace_snapshot", restore_with_stale_entry)
    events.clear()
    result = await prepare()
    assert result.outcome.status == "success"
    assert result.snapshot_status == "created"
    assert sum(kind == "workspace.python.rebuilding" for kind, _ in events) == 1
    assert sum(kind == "workspace.prepare.started" for kind, _ in events) == 1


@pytest.mark.asyncio
async def test_valid_snapshot_is_verified_in_new_checkout_without_reinstall(
    python_workspace,
):
    """跨目录恢复可用的真实虚拟环境后，仍校验但不重复安装。"""

    _, repository, events, prepare = python_workspace
    repository.agent_workspace.cache_enabled = True
    repository.agent_workspace.prepare_steps = [
        AgentWorkspacePrepareStepConfig(
            name="准备可迁移 Python",
            command=[
                sys.executable,
                "-c",
                "import venv; venv.EnvBuilder(clear=True, with_pip=False, symlinks=False).create('.venv')",
            ],
        )
    ]
    first = await prepare()
    assert first.outcome.status == "success"
    repository.workspace = repository.workspace.parent / "another-checkout"
    repository.workspace.mkdir()
    subprocess.run(
        ["git", "init", str(repository.workspace)], check=True, capture_output=True
    )
    events.clear()
    restored = await prepare()
    assert restored.outcome.status == "success"
    assert restored.snapshot_status == "restored"
    assert restored.execution_environment["VIRTUAL_ENV"] == str(
        repository.workspace / ".venv"
    )
    assert any(kind == "workspace.python.ready" for kind, _ in events)
    assert not any(kind == "workspace.prepare.started" for kind, _ in events)


@pytest.mark.parametrize("rebuild_install_fails", [False, True])
@pytest.mark.asyncio
async def test_failed_rebuild_stops_and_invalidates_bad_snapshot(
    python_workspace, monkeypatch, rebuild_install_fails
):
    """快照与重新准备都失败时不能再次安装或留下坏快照。"""

    config, repository, events, prepare = python_workspace
    repository.agent_workspace.cache_enabled = True
    repository.agent_workspace.prepare_steps = [
        AgentWorkspacePrepareStepConfig(
            name="准备 Python",
            command=[
                sys.executable,
                "-c",
                "import venv; venv.EnvBuilder(clear=True, with_pip=False).create('.venv')",
            ],
        )
    ]
    first = await prepare()
    assert first.outcome.status == "success"
    from teamwork_review_agents import agent_workspace as module

    original_check = module.check_python_script_paths

    def always_bad(scripts):
        """模拟无法通过校验的环境，并保留一次修复的计数。"""

        original_check(scripts)
        raise ValueError("环境仍不可用")

    monkeypatch.setattr(module, "check_python_script_paths", always_bad)
    if rebuild_install_fails:

        async def fail_install(*args, **kwargs):
            """一次修复中的安装失败也不能通过事件重试重新获得修复额度。"""

            return StepExecutionOutcome(
                status="failure", exit_code=1, error="重建安装命令失败"
            )

        monkeypatch.setattr(module, "execute_preflight_steps", fail_install)
    events.clear()
    result = await prepare()
    assert result.outcome.status == ("failure" if rebuild_install_fails else "error")
    assert result.retryable is False
    assert sum(kind == "workspace.python.rebuilding" for kind, _ in events) == 1
    assert sum(kind == "workspace.prepare.started" for kind, _ in events) == 1
    assert not (
        workspace_snapshot_root(config, repository)
        / first.snapshot_fingerprint
        / ARCHIVE_FILE_NAME
    ).exists()


@pytest.mark.asyncio
async def test_python_probe_obeys_total_timeout_and_cancellation(
    python_workspace, monkeypatch
):
    """耗时模块导入也必须受原有准备总期限和取消约束。"""

    _, repository, _, prepare = python_workspace
    venv.EnvBuilder(with_pip=False).create(repository.workspace / ".venv")
    from teamwork_review_agents import agent_workspace as module

    monkeypatch.setattr(module, "PYTHON_PROBE", "import time; time.sleep(10)")
    repository.agent_workspace.timeout_seconds = 1
    result = await prepare()
    assert result.outcome.status == "timed_out"
    result = await prepare(cancel_check=lambda: True)
    assert result.outcome.status == "cancelled"
