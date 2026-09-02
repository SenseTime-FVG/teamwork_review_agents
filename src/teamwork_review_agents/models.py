"""系统内部使用的统一数据模型。"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


ChangeRequestState = Literal["opened", "closed", "merged"]


class ChangeRequestSnapshot(BaseModel):
    """GitHub PR 与 GitLab MR 的统一快照。"""

    provider: str
    repository_id: str
    number: int
    title: str
    state: ChangeRequestState
    draft: bool = False
    source_branch: str
    target_branch: str
    head_sha: str
    source_project: str = ""
    target_head_sha: str = ""
    labels: tuple[str, ...] = ()
    approvals: int = 0
    pipeline_status: str = "unknown"
    merge_status: str = "unknown"
    created_at: datetime | None = None
    updated_at: datetime
    web_url: str
    raw: dict[str, Any] = Field(default_factory=dict)

    @property
    def key(self) -> str:
        """返回适合持久化的稳定快照键。"""

        return f"{self.repository_id}:{self.number}"

    def normalized_payload(self) -> dict[str, Any]:
        """返回排除平台易变原始字段后的可比较数据。"""

        return self.model_dump(mode="json", exclude={"raw"})


class ChangeRequestActivity(BaseModel):
    """Provider 活动流中的一条稳定 MR/PR 动作。"""

    id: str
    type: str
    occurred_at: datetime | None = None
    data: dict[str, Any] = Field(default_factory=dict)


class ChangeRequestActivityBatch(BaseModel):
    """一次活动流增量读取结果及下一次使用的不透明游标。"""

    activities: tuple[ChangeRequestActivity, ...] = ()
    latest_activity: ChangeRequestActivity | None = None
    cursor: dict[str, Any] = Field(default_factory=dict)
    baseline: bool = False


class ChangeEvent(BaseModel):
    """由快照差异或 Provider 活动生成的语义事件。"""

    id: str
    type: str
    provider: str
    repository_id: str
    number: int
    old: ChangeRequestSnapshot | None
    new: ChangeRequestSnapshot
    current: ChangeRequestSnapshot | None = None
    batch_id: str = ""
    changed_fields: tuple[str, ...] = ()
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    origin: Literal["scanner", "manual"] = "scanner"
    source_activity_id: str | None = None
    source_activity_type: str | None = None
    source_occurred_at: datetime | None = None
    source_event_id: str | None = None
    source_event_occurred_at: datetime | None = None
    source_generation: int = Field(default=1, ge=1)

    @property
    def resource_key(self) -> str:
        """返回变更请求级资源键。"""

        return f"{self.provider}:{self.repository_id}:{self.number}"

    @property
    def current_snapshot(self) -> ChangeRequestSnapshot:
        """返回扫描结束时的当前快照，兼容旧事件数据。"""

        return self.current or self.new


class AgentResult(BaseModel):
    """Codex CLI 或内嵌模型单次运行的结构化结果。"""

    run_id: str
    root_run_id: str
    parent_run_id: str | None = None
    agent_name: str
    status: Literal["completed", "failed", "timed_out", "cancelled"]
    final_message: str = ""
    thread_id: str | None = None
    usage: dict[str, Any] = Field(default_factory=dict)
    events: list[dict[str, Any]] = Field(default_factory=list)
    error: str | None = None


class PreflightResult(BaseModel):
    """一次确定性 CI 前置检查的最终结果。"""

    run_id: str
    repository_id: str
    number: int | None = None
    head_sha: str
    source_generation: int = Field(default=1, ge=1)
    status: Literal[
        "running",
        "success",
        "failure",
        "timed_out",
        "error",
        "cancelled",
        "superseded",
    ]
    failed_step: str | None = None
    exit_code: int | None = None
    output: str = ""
    error: str | None = None
    status_published: bool = False
    reused: bool = False


class ScheduledRunContext(BaseModel):
    """一次定时周期传递给根 Agent 与 sub-agent 的可信上下文。"""

    rule_name: str
    occurrence_id: str
    scheduled_at: datetime
    created_at: datetime
    repository_id: str
    branch: str = ""
    head_sha: str = ""


class InvocationContext(BaseModel):
    """传递给 sub-agent MCP Server 的最小调用上下文。"""

    config_path: str
    current_agent: str
    run_id: str
    root_run_id: str
    depth: int = 0
    call_chain: tuple[str, ...] = ()
    inherit_workspace: bool = False
    active_workspace: str = ""
    event: ChangeEvent | None = None
    schedule: ScheduledRunContext | None = None

    @model_validator(mode="after")
    def validate_trigger_context(self) -> "InvocationContext":
        """一次调用必须且只能携带一种根触发来源。"""

        if (self.event is None) == (self.schedule is None):
            raise ValueError("调用上下文必须且只能包含事件或定时触发来源")
        return self


def stable_hash(*parts: Any) -> str:
    """对可序列化内容计算稳定 SHA-256。"""

    encoded = json.dumps(parts, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
