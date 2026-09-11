"""推理强度参数拒绝识别与有界兼容降级。"""

from __future__ import annotations

import re
from collections.abc import Mapping


_EFFORT_DESCENDING = ("ultra", "max", "xhigh", "high", "medium", "low")


def next_reasoning_effort(current: str) -> str | None:
    """只向较低档位移动；旧 minimal 和未知值被拒绝后直接省略。"""

    try:
        index = _EFFORT_DESCENDING.index(current)
    except ValueError:
        return None
    return _EFFORT_DESCENDING[index + 1] if index + 1 < len(_EFFORT_DESCENDING) else None


def is_reasoning_effort_rejection(
    fields: Mapping[str, str],
    *,
    status_code: int | None = None,
) -> bool:
    """仅识别明确的 effort/整个 reasoning 拒绝，不把普通请求错误降级。"""

    if status_code not in {None, 400, 422}:
        return False
    code = fields.get("code", "").lower()
    error_type = fields.get("type", "").lower()
    if any(marker in f"{code} {error_type}" for marker in (
        "server_error", "rate_limit", "auth", "permission", "overloaded", "timeout",
    )):
        return False
    param = fields.get("param", "").strip().lower()
    message = fields.get("message", "").lower()
    if param and param not in {"reasoning.effort", "reasoning_effort", "reasoning"}:
        return False
    # 缺少精确参数名时，含 summary 的拒绝不能归因于 effort。
    if param in {"", "reasoning"} and "summary" in message:
        return False
    mentions_effort = re.search(r"\breasoning[._ ]effort\b|\beffort\b", message) is not None
    rejects_parameter = any(marker in message for marker in (
        "unsupported parameter", "unknown parameter", "unrecognized parameter",
        "not supported", "does not support", "不支持",
    ))
    # summary 等子字段报错不能通过降低 effort 修复。
    if param == "reasoning" and not mentions_effort:
        return "summary" not in message and (
            code in {"unsupported_parameter", "unknown_parameter"} or rejects_parameter
        )
    if not param and not mentions_effort:
        return (
            rejects_parameter
            and re.search(r"\breasoning\b(?![._])", message) is not None
            and "summary" not in message
        )
    return code in {
        "unsupported_value", "unsupported_parameter", "unknown_parameter",
        "invalid_value", "invalid_enum_value",
    } or any(marker in message for marker in (
        "unsupported", "not supported", "does not support", "not support",
        "invalid value", "invalid enum", "must be one of", "supported values",
        "unknown parameter", "unrecognized", "not permitted", "not allowed",
        "不支持", "不允许", "无效", "不合法",
    ))
