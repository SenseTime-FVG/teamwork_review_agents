"""Skill 配置、导入与运行时隔离测试。"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from teamwork_review_agents.codex_runner import CodexRunner, _add_git_excludes_file
from teamwork_review_agents.config import load_config
from teamwork_review_agents.events import detect_events
from teamwork_review_agents.models import InvocationContext
from teamwork_review_agents.skill_files import (
    SkillProjection,
    import_skill_directory,
    list_skill_directories,
    read_skill_metadata,
)
from teamwork_review_agents.webapp import create_app


SKILL_MD = """---
name: incremental-doc-update
description: 根据代码变化按需更新项目文档。
---

# 增量文档更新
"""


def _write_minimal_config(tmp_path: Path) -> Path:
    """写入只用于管理 API 测试的空白配置。"""

    path = tmp_path / "config.yaml"
    path.write_text(
        yaml.safe_dump(
            {"database": {"path": str(tmp_path / "state.db")}},
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return path


def test_skill_directory_import_preserves_resources(tmp_path) -> None:
    """文件夹导入应读取元数据并保留内部资源层级。"""

    config_path = _write_minimal_config(tmp_path)
    imported = import_skill_directory(
        config_path,
        [
            ("selected-skill/SKILL.md", SKILL_MD.encode()),
            ("selected-skill/scripts/update.py", "print('ok')\n".encode()),
            ("selected-skill/references/policy.md", "规则\n".encode()),
        ],
    )

    assert imported["path"] == "./skills/incremental-doc-update"
    directory = tmp_path / "skills" / "incremental-doc-update"
    assert (directory / "scripts" / "update.py").is_file()
    assert (directory / "references" / "policy.md").is_file()
    assert read_skill_metadata(directory).name == "incremental-doc-update"
    assert list_skill_directories(config_path)[0]["valid"] is True


def test_skill_import_rejects_path_traversal(tmp_path) -> None:
    """浏览器上传路径不能逃离受管 Skill 目录。"""

    config_path = _write_minimal_config(tmp_path)
    with pytest.raises(ValueError, match="路径非法"):
        import_skill_directory(
            config_path,
            [("selected/../SKILL.md", SKILL_MD.encode())],
        )


def test_config_validates_agent_skill_references(tmp_path) -> None:
    """Agent 只能引用存在且元数据有效的 Skill。"""

    skill_path = tmp_path / "configured-skill"
    skill_path.mkdir()
    (skill_path / "SKILL.md").write_text(SKILL_MD, encoding="utf-8")
    document = {
        "database": {"path": str(tmp_path / "state.db")},
        "skills": {"docs": {"path": str(skill_path)}},
        "agents": {
            "reviewer": {
                "prompt": "检查文档",
                "skills": ["missing"],
            }
        },
    }
    config_path = tmp_path / "configured.yaml"
    config_path.write_text(
        yaml.safe_dump(document, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="不存在的 Skill"):
        load_config(config_path)
    document["agents"]["reviewer"]["skills"] = ["docs"]
    config_path.write_text(
        yaml.safe_dump(document, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    assert load_config(config_path).agents["reviewer"].skills == ["docs"]


def test_skill_projection_is_reused_and_cleaned(tmp_path) -> None:
    """继承工作区的 sub-agent 复用投影，但只有创建者负责清理。"""

    source = tmp_path / "source-skill"
    source.mkdir()
    (source / "SKILL.md").write_text(SKILL_MD, encoding="utf-8")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    subprocess.run(
        ["git", "init", "-q", str(workspace)],
        check=True,
        capture_output=True,
        text=True,
    )

    parent = SkillProjection(workspace, {"docs": source}, "a" * 64).prepare()
    projected_manifest = parent.skill_files["docs"]
    assert projected_manifest.is_file()
    git_environment = dict(os.environ)
    _add_git_excludes_file(git_environment, parent.marker)
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=workspace,
        env=git_environment,
        check=True,
        capture_output=True,
        text=True,
    )
    assert status.stdout == ""
    child = SkillProjection(workspace, {"docs": source}, "a" * 64).prepare()
    assert child.skill_files["docs"] == projected_manifest
    child.cleanup()
    assert projected_manifest.is_file()
    parent.cleanup()
    assert not projected_manifest.exists()
    assert not (workspace / ".agents").exists()


def test_runner_enables_only_selected_managed_skills(
    tmp_path,
    snapshot_factory,
    configured_app_factory,
) -> None:
    """Codex 命令应显式启用当前 Agent 选择并禁用其他应用 Skill。"""

    config = configured_app_factory()
    repository = config.repositories[0]
    snapshot = snapshot_factory(repository_id=repository.id, provider=repository.provider)
    event = detect_events(None, snapshot, emit_initial=True)[0]
    context = InvocationContext(
        config_path=str(config.config_path),
        current_agent="code-reviewer",
        run_id="run-skill",
        root_run_id="run-skill",
        event=event,
    )
    agent = config.agents["code-reviewer"]
    agent.skills = ["docs"]
    command = CodexRunner(config).build_command(
        agent,
        repository,
        context,
        {
            "docs": tmp_path / "docs" / "SKILL.md",
            "security": tmp_path / "security" / "SKILL.md",
        },
    )
    joined = " ".join(command)
    assert "skills.config=[" in joined
    assert f'path = "{tmp_path / "docs" / "SKILL.md"}", enabled = true' in joined
    assert f'path = "{tmp_path / "security" / "SKILL.md"}", enabled = false' in joined


def test_skill_directory_web_api_imports_whole_folder(tmp_path) -> None:
    """管理 API 应接收多文件目录上传并返回可配置相对路径。"""

    app = create_app(_write_minimal_config(tmp_path), start_scheduler=False)
    with TestClient(app) as client:
        response = client.post(
            "/api/skill-directories/import",
            files=[
                ("files", ("chosen/SKILL.md", SKILL_MD, "text/markdown")),
                ("files", ("chosen/assets/example.txt", "示例", "text/plain")),
            ],
        )
        assert response.status_code == 200
        assert response.json()["path"] == "./skills/incremental-doc-update"
        listed = client.get("/api/skill-directories").json()
        assert listed[0]["name"] == "incremental-doc-update"
        inspected = client.post(
            "/api/skill-directories/inspect",
            json={"path": listed[0]["path"]},
        )
        assert inspected.status_code == 200
        assert inspected.json()["description"] == "根据代码变化按需更新项目文档。"
