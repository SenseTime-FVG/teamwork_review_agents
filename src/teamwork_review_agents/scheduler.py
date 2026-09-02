"""定时规则的签名、间隔换算和下一次触发时间计算。"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from croniter import croniter

from .config import ScheduledRuleConfig
from .models import stable_hash


_INTERVAL_SECONDS = {
    "minutes": 60,
    "hours": 3600,
    "days": 86400,
}


def schedule_signature(rule: ScheduledRuleConfig) -> str:
    """返回调度定义变化后必然变化的稳定签名。"""

    return stable_hash(rule.model_dump(mode="json"))


def next_scheduled_at(rule: ScheduledRuleConfig, after: float) -> float:
    """返回严格晚于给定时间点的下一次计划时间。"""

    schedule = rule.schedule
    if schedule.kind == "interval":
        return after + schedule.interval_value * _INTERVAL_SECONDS[
            schedule.interval_unit
        ]
    timezone = ZoneInfo(schedule.timezone)
    base = datetime.fromtimestamp(after, tz=timezone)
    return croniter(schedule.cron, base).get_next(datetime).timestamp()


def schedule_summary(rule: ScheduledRuleConfig) -> str:
    """返回适合 API 与界面展示的简短调度说明。"""

    schedule = rule.schedule
    if schedule.kind == "cron":
        return f"Cron {schedule.cron}"
    unit = {
        "minutes": "分钟",
        "hours": "小时",
        "days": "天",
    }[schedule.interval_unit]
    return f"每 {schedule.interval_value} {unit}"
