"""可配置事件规则匹配器。"""

from __future__ import annotations

from typing import Any

from .config import RuleConfig
from .models import ChangeEvent


OPERATORS = ("changed", "contains", "not_contains", "in", "not_in", "gte", "lte", "gt", "lt", "ne")


def _split_operator(key: str) -> tuple[str, str]:
    """从条件键末尾拆分比较操作符。"""

    for operator in OPERATORS:
        suffix = f"__{operator}"
        if key.endswith(suffix):
            return key[: -len(suffix)], operator
    return key, "eq"


def _read_path(event: ChangeEvent, path: str) -> Any:
    """从事件或新旧快照中读取点号路径。"""

    if path == "event":
        return event.type
    if path == "provider":
        return event.provider
    if path == "repository_id":
        return event.repository_id
    if path == "number":
        return event.number

    if path.startswith("old."):
        source: Any = event.old
        segments = path.split(".")[1:]
    elif path.startswith("new."):
        source = event.new
        segments = path.split(".")[1:]
    else:
        source = event.new
        segments = path.split(".")

    for segment in segments:
        if source is None:
            return None
        if isinstance(source, dict):
            source = source.get(segment)
        else:
            source = getattr(source, segment, None)
    return source


def _compare(actual: Any, expected: Any, operator: str, *, changed: bool) -> bool:
    """执行一个规则条件比较。"""

    if operator == "changed":
        return changed is bool(expected)
    if operator == "eq":
        return actual == expected
    if operator == "ne":
        return actual != expected
    if operator == "contains":
        return actual is not None and expected in actual
    if operator == "not_contains":
        return actual is None or expected not in actual
    if operator == "in":
        return actual in expected
    if operator == "not_in":
        return actual not in expected
    if operator == "gte":
        return actual is not None and actual >= expected
    if operator == "lte":
        return actual is not None and actual <= expected
    if operator == "gt":
        return actual is not None and actual > expected
    if operator == "lt":
        return actual is not None and actual < expected
    raise ValueError(f"不支持的规则操作符：{operator}")


def rule_matches(rule: RuleConfig, event: ChangeEvent) -> bool:
    """判断事件是否满足一条规则。"""

    if not rule.enabled or event.type not in rule.events:
        return False
    if rule.repositories and event.repository_id not in rule.repositories:
        return False

    for raw_key, expected in rule.conditions.items():
        path, operator = _split_operator(raw_key)
        normalized_field = path.removeprefix("old.").removeprefix("new.").split(".")[0]
        actual = _read_path(event, path)
        if not _compare(
            actual,
            expected,
            operator,
            changed=normalized_field in event.changed_fields,
        ):
            return False
    return True


def matching_rules(rules: list[RuleConfig], event: ChangeEvent) -> list[RuleConfig]:
    """返回全部匹配规则并保留配置顺序。"""

    return [rule for rule in rules if rule_matches(rule, event)]
