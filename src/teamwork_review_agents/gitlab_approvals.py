"""按可信平台版本区分 GitLab 可选审批与实际强制门禁。"""

from __future__ import annotations

from typing import Any

from .models import ChangeRequestSnapshot


def gitlab_approval_context(snapshot: ChangeRequestSnapshot) -> dict[str, Any]:
    """只传递审批白名单证据，未知版本和明确强制要求均不得视为可选。"""

    instance = snapshot.raw.get("gitlab_instance")
    enterprise = instance.get("enterprise") if isinstance(instance, dict) else None
    edition = (
        "community" if enterprise is False
        else "enterprise" if enterprise is True
        else "unknown"
    )
    approvals = snapshot.raw.get("approvals")
    approvals = approvals if isinstance(approvals, dict) else {}
    approved = approvals.get("approved")
    counts: dict[str, int | None] = {}
    invalid_counts = False
    for field in ("approvals_required", "approvals_left"):
        value = approvals.get(field)
        # 布尔值、字符串及负数不能冒充明确的审批数量，缺失也不能当作零。
        counts[field] = value if type(value) is int and value >= 0 else None
        if value is not None and counts[field] is None:
            invalid_counts = True

    requirement = "unknown"
    if snapshot.merge_status == "not_approved" or any(
        value is not None and value > 0 for value in counts.values()
    ):
        requirement = "required"
    elif edition == "community" and not invalid_counts:
        requirement = "optional"

    return {
        "edition": edition,
        "edition_source": "version_api" if edition != "unknown" else "unavailable",
        "requirement": requirement,
        "approved": approved if type(approved) is bool else None,
        **counts,
    }
