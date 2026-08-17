"""变更请求快照差异检测。"""

from __future__ import annotations

from typing import Any

from .models import ChangeEvent, ChangeRequestSnapshot, stable_hash


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
) -> ChangeEvent:
    """创建带稳定标识的事件。"""

    return ChangeEvent(
        id=_event_id(new, event_type, old_value, new_value),
        type=event_type,
        provider=new.provider,
        repository_id=new.repository_id,
        number=new.number,
        old=old,
        new=new,
        changed_fields=changed_fields,
        occurred_at=new.updated_at,
    )


def detect_events(
    old: ChangeRequestSnapshot | None,
    new: ChangeRequestSnapshot,
    *,
    emit_initial: bool = False,
) -> list[ChangeEvent]:
    """比较新旧快照并返回按语义拆分的事件列表。"""

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
        )
    )
    return events
