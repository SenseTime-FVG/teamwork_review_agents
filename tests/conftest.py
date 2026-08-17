"""测试共享构造器。"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
import yaml

from teamwork_review_agents.config import load_config
from teamwork_review_agents.models import ChangeRequestSnapshot


@pytest.fixture
def configured_app_factory(tmp_path):
    """创建供运行器与 MCP 边界测试使用的完整配置。"""

    def create():
        workspace = tmp_path / "configured-workspace"
        workspace.mkdir(exist_ok=True)
        document = {
            "database": {"path": str(tmp_path / "state.db")},
            "providers": {
                "github-main": {
                    "kind": "github",
                    "base_url": "https://api.github.com",
                    "token_env": "GITHUB_TOKEN",
                },
                "gitlab-main": {
                    "kind": "gitlab",
                    "base_url": "https://gitlab.example.com/api/v4",
                    "token_env": "GITLAB_TOKEN",
                },
            },
            "repositories": [
                {
                    "id": "demo",
                    "provider": "github-main",
                    "project": "owner/demo",
                    "workspace": str(workspace),
                }
            ],
            "agents": {
                "code-reviewer": {
                    "prompt": "请审查当前变更。",
                    "sandbox": "read-only",
                    "allowed_sub_agents": ["security-reviewer"],
                },
                "security-reviewer": {
                    "prompt": "请执行安全审查。",
                    "sandbox": "read-only",
                    "allowed_sub_agents": [],
                },
            },
            "rules": [],
        }
        config_path = tmp_path / "configured.yaml"
        config_path.write_text(
            yaml.safe_dump(document, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
        return load_config(config_path)

    return create


@pytest.fixture
def snapshot_factory():
    """返回可按字段覆盖的统一快照工厂。"""

    def create(**overrides: object) -> ChangeRequestSnapshot:
        payload: dict[str, object] = {
            "provider": "gitlab-main",
            "repository_id": "demo",
            "number": 7,
            "title": "测试变更",
            "state": "opened",
            "draft": False,
            "source_branch": "feature/demo",
            "target_branch": "main",
            "head_sha": "a" * 40,
            "labels": ("backend",),
            "approvals": 0,
            "pipeline_status": "pending",
            "merge_status": "checking",
            "updated_at": datetime(2026, 8, 17, 8, 0, tzinfo=UTC),
            "web_url": "https://gitlab.example.com/group/demo/-/merge_requests/7",
            "raw": {},
        }
        payload.update(overrides)
        return ChangeRequestSnapshot.model_validate(payload)

    return create
