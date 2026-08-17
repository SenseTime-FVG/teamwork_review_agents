"""变更请求快照差异检测。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from .models import (
    ChangeEvent,
    ChangeRequestActivity,
    ChangeRequestSnapshot,
    stable_hash,
)


FIELD_EVENTS = {
    "head_sha": "change_request.commits_changed",
    "draft": "change_request.draft_changed",
    "labels": "change_request.labels_changed",
    "approvals": "change_request.approvals_changed",
    "pipeline_status": "change_request.pipeline_changed",
    "merge_status": "change_request.merge_status_changed",
}


def _event_id(
    snapshot: ChangeRequestSnapshot,
    event_type: str,
    old_value: Any,
    new_value: Any,
) -> str:
    """根据一次具体变化生成稳定事件 ID。"""

    return stable_hash(
        snapshot.provider,
        snapshot.repository_id,
        snapshot.number,
        event_type,
        snapshot.updated_at,
        old_value,
        new_value,
    )


def _create_event(
    event_type: str,
    old: ChangeRequestSnapshot | None,
    new: ChangeRequestSnapshot,
    changed_fields: tuple[str, ...],
    old_value: Any,
    new_value: Any,
    batch_id: str,
    *,
    event_id: str | None = None,
    occurred_at: datetime | None = None,
    current: ChangeRequestSnapshot | None = None,
) -> ChangeEvent:
    """创建带稳定标识的事件。"""

    return ChangeEvent(
        id=event_id or _event_id(new, event_type, old_value, new_value),
        type=event_type,
        provider=new.provider,
        repository_id=new.repository_id,
        number=new.number,
        old=old,
        new=new,
        current=current,
        batch_id=batch_id,
        changed_fields=changed_fields,
        occurred_at=occurred_at or new.updated_at,
    )


def _activity_event_id(
    snapshot: ChangeRequestSnapshot,
    activity: ChangeRequestActivity,
    event_type: str,
) -> str:
    """使用 Provider 稳定活动 ID 生成可重试的语义事件 ID。"""

    return stable_hash(
        snapshot.provider,
        snapshot.repository_id,
        snapshot.number,
        "provider-activity",
        activity.id,
        event_type,
    )


def _apply_activity(
    before: ChangeRequestSnapshot,
    current: ChangeRequestSnapshot,
    activity: ChangeRequestActivity,
) -> tuple[ChangeRequestSnapshot, str, tuple[str, ...], Any, Any] | None:
    """把一条离散活动应用到事件发生时的中间快照。"""

    occurred_at = activity.occurred_at or current.updated_at
    updates: dict[str, Any] = {"updated_at": occurred_at}
    event_type: str
    changed_fields: tuple[str, ...]
    old_value: Any
    new_value: Any

    if activity.type in {"closed", "reopened", "merged"}:
        state_by_activity = {
            "closed": "closed",
            "reopened": "opened",
            "merged": "merged",
        }
        new_value = state_by_activity[activity.type]
        old_value = before.state
        updates["state"] = new_value
        event_type = f"change_request.{activity.type}"
        changed_fields = ("state",)
    elif activity.type in {"committed", "head_ref_force_pushed"}:
        old_value = before.head_sha
        new_value = str(activity.data.get("sha") or current.head_sha)
        updates["head_sha"] = new_value
        event_type = "change_request.commits_changed"
        changed_fields = ("head_sha",)
    elif activity.type in {"convert_to_draft", "ready_for_review"}:
        old_value = before.draft
        new_value = activity.type == "convert_to_draft"
        updates["draft"] = new_value
        event_type = "change_request.draft_changed"
        changed_fields = ("draft",)
    elif activity.type in {"labeled", "unlabeled"}:
        label = str(activity.data.get("label") or "")
        if not label:
            return None
        old_value = before.labels
        labels = set(before.labels)
        if activity.type == "labeled":
            labels.add(label)
        else:
            labels.discard(label)
        new_value = tuple(sorted(labels))
        updates["labels"] = new_value
        event_type = "change_request.labels_changed"
        changed_fields = ("labels",)
    else:
        return None

    after = before.model_copy(update=updates)
    return after, event_type, changed_fields, old_value, new_value


def detect_activity_events(
    old: ChangeRequestSnapshot,
    current: ChangeRequestSnapshot,
    activities: tuple[ChangeRequestActivity, ...] | list[ChangeRequestActivity],
    *,
    batch_id: str | None = None,
) -> list[ChangeEvent]:
    """按活动顺序恢复中间变化，再用最终快照补齐未覆盖字段。"""

    effective_batch_id = batch_id or stable_hash(
        "scan-batch",
        current.provider,
        current.repository_id,
        current.number,
        current.updated_at,
    )
    events: list[ChangeEvent] = []
    working = old
    for activity in activities:
        transition = _apply_activity(working, current, activity)
        if transition is None:
            continue
        after, event_type, changed_fields, old_value, new_value = transition
        events.append(
            _create_event(
                event_type,
                working,
                after,
                changed_fields,
                old_value,
                new_value,
                effective_batch_id,
                event_id=_activity_event_id(current, activity, event_type),
                occurred_at=activity.occurred_at,
                current=current,
            )
        )
        changed_values = {
            field: {
                "old": working.normalized_payload().get(field),
                "new": after.normalized_payload().get(field),
            }
            for field in changed_fields
        }
        events.append(
            _create_event(
                "change_request.updated",
                working,
                after,
                changed_fields,
                {field: values["old"] for field, values in changed_values.items()},
                {field: values["new"] for field, values in changed_values.items()},
                effective_batch_id,
                event_id=_activity_event_id(
                    current,
                    activity,
                    "change_request.updated",
                ),
                occurred_at=activity.occurred_at,
                current=current,
            )
        )
        working = after

    events.extend(detect_events(working, current, batch_id=effective_batch_id))
    return events


def detect_events(
    old: ChangeRequestSnapshot | None,
    new: ChangeRequestSnapshot,
    *,
    emit_initial: bool = False,
    batch_id: str | None = None,
) -> list[ChangeEvent]:
    """比较新旧快照并返回按语义拆分的事件列表。"""

    effective_batch_id = batch_id or stable_hash(
        "scan-batch",
        new.provider,
        new.repository_id,
        new.number,
        new.updated_at,
    )

    if old is None:
        if not emit_initial:
            return []
        return [
            _create_event(
                "change_request.discovered",
                None,
                new,
                tuple(new.normalized_payload().keys()),
                None,
                new.normalized_payload(),
                effective_batch_id,
            )
        ]

    old_payload = old.normalized_payload()
    new_payload = new.normalized_payload()
    ignored_fields = {"updated_at", "title", "web_url"}
    changed_fields = tuple(
        sorted(
            field
            for field in old_payload.keys() | new_payload.keys()
            if field not in ignored_fields and old_payload.get(field) != new_payload.get(field)
        )
    )
    metadata_changed = tuple(
        sorted(
            field
            for field in ("title", "web_url")
            if old_payload.get(field) != new_payload.get(field)
        )
    )
    all_changed_fields = tuple(sorted(set(changed_fields) | set(metadata_changed)))
    if not all_changed_fields:
        return []

    events: list[ChangeEvent] = []
    if old.state != new.state:
        if new.state == "merged":
            state_event = "change_request.merged"
        elif new.state == "closed":
            state_event = "change_request.closed"
        elif old.state == "closed":
            state_event = "change_request.reopened"
        else:
            state_event = "change_request.opened"
        events.append(
            _create_event(
                state_event,
                old,
                new,
                ("state",),
                old.state,
                new.state,
                effective_batch_id,
            )
        )

    for field, event_type in FIELD_EVENTS.items():
        old_value = old_payload.get(field)
        new_value = new_payload.get(field)
        if old_value != new_value:
            events.append(
                _create_event(
                    event_type,
                    old,
                    new,
                    (field,),
                    old_value,
                    new_value,
                    effective_batch_id,
                )
            )

    changed_values = {
        field: {"old": old_payload.get(field), "new": new_payload.get(field)}
        for field in all_changed_fields
    }
    events.append(
        _create_event(
            "change_request.updated",
            old,
            new,
            all_changed_fields,
            {field: values["old"] for field, values in changed_values.items()},
            {field: values["new"] for field, values in changed_values.items()},
            effective_batch_id,
        )
    )
    return events
