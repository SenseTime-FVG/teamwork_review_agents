"""测试共享构造器。"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from teamwork_review_agents.models import ChangeRequestSnapshot


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
