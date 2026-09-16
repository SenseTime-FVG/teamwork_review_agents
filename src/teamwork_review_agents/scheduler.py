"""定时规则的签名、间隔换算和下一次触发时间计算。"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from croniter import croniter

from .config import ScheduledRuleConfig, WorkspaceCleanupConfig
from .models import stable_hash


_INTERVAL_SECONDS = {
    "minutes": 60,
    "hours": 3600,
    "days": 86400,
}


def next_workspace_cleanup_at(schedule: WorkspaceCleanupConfig, after: float) -> float:
    """按主机本地日历计算下一次清理，跳过夏令时中不存在的时刻。"""

    if schedule.kind == "interval":
        return after + schedule.interval_value * _INTERVAL_SECONDS[schedule.interval_unit]
    today = datetime.fromtimestamp(after).date()
    candidates: list[float] = []
    # 每周时刻落在夏令时缺口时，需要再找下一周，不能仅搜索未来七天。
    for offset in range(15 if schedule.kind == "weekly" else 8):
        day = today + timedelta(days=offset)
        if schedule.kind == "weekly" and day.weekday() != schedule.weekday:
            continue
        hours = range(24) if schedule.kind == "hourly" else (schedule.hour,)
        for hour in hours:
            wall_time = datetime(day.year, day.month, day.day, hour, schedule.minute)
            # 无时区日期按操作系统本地规则转换；fold=0 固定选择回拨前的一次，避免重复。
            timestamp = wall_time.timestamp()
            if timestamp > after and datetime.fromtimestamp(timestamp) == wall_time:
                candidates.append(timestamp)
    if not candidates:
        raise ValueError("无法计算下一次本地工作区清理时间")
    return min(candidates)


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
