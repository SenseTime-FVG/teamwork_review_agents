"""区分明确额度耗尽与临时请求限流。"""

from collections.abc import Mapping


_EXHAUSTED_QUOTA_MARKERS = frozenset({
    "insufficient_quota",
    "credit_balance_exhausted",
    "organization_spend_limit_exceeded",
    "project_spend_limit_exceeded",
    "organization_usage_limit_exceeded",
    "usage_limit_reached",
    "billing_hard_limit_reached",
    "insufficient_balance",
    "quota_exhausted",
})


def is_quota_exhausted(fields: Mapping[str, str]) -> bool:
    """只根据明确错误标识判断，避免把普通 429 或模糊文本记入跳过名单。"""

    return any(
        fields.get(name, "").strip().lower() in _EXHAUSTED_QUOTA_MARKERS
        for name in ("code", "type")
    )
