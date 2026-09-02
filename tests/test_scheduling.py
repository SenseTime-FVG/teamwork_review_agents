"""定时规则配置、调度幂等和独立运行上下文测试。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from teamwork_review_agents.config import (
    ScheduledRuleConfig,
    parse_config_data,
)
from teamwork_review_agents.environment import resolve_environment
from teamwork_review_agents.models import AgentResult, ScheduledRunContext
from teamwork_review_agents.orchestrator import CycleSummary, Orchestrator
from teamwork_review_agents.runtime import BackgroundRuntime
from teamwork_review_agents.scheduler import next_scheduled_at, schedule_summary
from teamwork_review_agents.state import StateStore


def scheduled_config(tmp_path):
    """创建包含一个仓库、Agent 和定时规则的最小配置。"""

    return parse_config_data(
        {
            "database": {"path": str(tmp_path / "state.db")},
            "providers": {
                "github-main": {
                    "kind": "github",
                    "base_url": "https://api.github.com",
                    "token_env": "GITHUB_TOKEN",
                }
            },
            "repositories": [
                {
                    "id": "demo",
                    "provider": "github-main",
                    "project": "owner/demo",
                    "workspace": str(tmp_path / "workspace"),
                }
            ],
            "agents": {
                "maintainer": {
                    "prompt": "请维护当前仓库。",
                    "sandbox": "read-only",
                }
            },
            "scheduled_rules": [
                {
                    "name": "hourly-maintenance",
                    "agents": ["maintainer"],
                    "repositories": ["demo"],
                    "schedule": {
                        "kind": "interval",
                        "interval_value": 1,
                        "interval_unit": "hours",
                        "timezone": "Asia/Shanghai",
                    },
                }
            ],
        },
        tmp_path / "config.yaml",
    )


def test_schedule_config_validates_interval_cron_and_timezone(tmp_path) -> None:
    """固定间隔与五段 Cron 可用，无效 Cron 和时区在加载期失败。"""

    config = scheduled_config(tmp_path)
    interval_rule = config.scheduled_rules[0]
    assert schedule_summary(interval_rule) == "每 1 小时"
    assert next_scheduled_at(interval_rule, 100.0) == 3700.0

    cron_rule = ScheduledRuleConfig.model_validate(
        {
            "name": "weekday",
            "agents": ["maintainer"],
            "repositories": ["demo"],
            "schedule": {
                "kind": "cron",
                "cron": "0 9 * * 1-5",
                "timezone": "Asia/Shanghai",
            },
        }
    )
    base = datetime(2026, 9, 4, 1, 0, tzinfo=UTC).timestamp()
    assert datetime.fromtimestamp(next_scheduled_at(cron_rule, base), tz=UTC) == datetime(
        2026,
        9,
        7,
        1,
        0,
        tzinfo=UTC,
    )

    with pytest.raises(ValueError, match="标准五段"):
        ScheduledRuleConfig.model_validate(
            {
                **cron_rule.model_dump(mode="json"),
                "schedule": {
                    "kind": "cron",
                    "cron": "0 9 * * 1-5 2026",
                    "timezone": "Asia/Shanghai",
                },
            }
        )
    with pytest.raises(ValueError, match="无效时区"):
        ScheduledRuleConfig.model_validate(
            {
                **cron_rule.model_dump(mode="json"),
                "schedule": {
                    "kind": "interval",
                    "timezone": "Not/A-Timezone",
                },
            }
        )


def test_scheduled_environment_has_no_change_request_variables(tmp_path) -> None:
    """定时运行只暴露定时与仓库变量，不能伪造 MR / PR 上下文。"""

    config = scheduled_config(tmp_path)
    schedule = ScheduledRunContext(
        rule_name="hourly-maintenance",
        occurrence_id="occurrence-1",
        scheduled_at=datetime(2026, 9, 2, 1, 0, tzinfo=UTC),
        created_at=datetime(2026, 9, 2, 1, 0, 1, tzinfo=UTC),
        repository_id="demo",
        branch="main",
        head_sha="a" * 40,
    )
    resolved = resolve_environment(
        config,
        config.repositories[0],
        config.agents["maintainer"],
        None,
        "run-1",
        include_change_request=False,
        schedule=schedule,
    )

    assert resolved.all_values["SCHEDULE_RULE_NAME"] == "hourly-maintenance"
    assert resolved.all_values["SCHEDULE_BRANCH"] == "main"
    assert resolved.all_values["SCHEDULE_HEAD_SHA"] == "a" * 40
    assert "MR_NUMBER" not in resolved.all_values
    assert "EVENT_TYPE" not in resolved.process_values


def test_state_persists_occurrence_and_scheduled_run_context(tmp_path) -> None:
    """周期幂等键和定时运行来源应同时进入 SQLite 审计记录。"""

    store = StateStore(tmp_path / "state.db")
    store.initialize()
    assert store.reserve_scheduled_occurrence(
        occurrence_id="occurrence-1",
        rule_name="hourly-maintenance",
        schedule_signature="signature-1",
        scheduled_at=100.0,
        config_revision="revision-1",
    )
    assert not store.reserve_scheduled_occurrence(
        occurrence_id="occurrence-1",
        rule_name="hourly-maintenance",
        schedule_signature="signature-1",
        scheduled_at=100.0,
        config_revision="revision-1",
    )

    reservation = store.begin_agent_run(
        proposed_run_id="scheduled-run-1",
        root_run_id=None,
        parent_run_id=None,
        idempotency_key="scheduled-key-1",
        event_id=None,
        rule_name="hourly-maintenance",
        agent_name="maintainer",
        resource_key="schedule:hourly-maintenance:demo:occurrence-1",
        prompt="",
        config_revision="revision-1",
        max_attempts=2,
        repository_id="demo",
        trigger_source="schedule",
        trigger_context={
            "rule_name": "hourly-maintenance",
            "occurrence_id": "occurrence-1",
            "repository_id": "demo",
            "branch": "main",
            "head_sha": "a" * 40,
        },
    )
    assert reservation is not None
    run = store.get_run(reservation.run_id)
    assert run is not None
    assert run["event_id"] is None
    assert run["trigger_source"] == "schedule"
    assert run["repository_id"] == "demo"
    assert run["trigger_context"]["branch"] == "main"
    listed = store.list_runs(None)
    assert listed[0]["trigger_context"]["occurrence_id"] == "occurrence-1"


async def test_scheduled_occurrences_overlap_and_each_period_is_idempotent(tmp_path) -> None:
    """前一周期未结束时下一周期仍需并行启动，同一周期重复唤醒则不得重复。"""

    config = scheduled_config(tmp_path)
    orchestrator = Orchestrator(config, recover_interrupted=False)
    both_started = asyncio.Event()
    release = asyncio.Event()

    class FakeExecutor:
        """记录定时上下文并阻塞到两个周期都已经启动。"""

        def __init__(self) -> None:
            self.calls: list[ScheduledRunContext] = []

        async def execute(self, *, agent_name: str, schedule: ScheduledRunContext, **_kwargs):
            self.calls.append(schedule)
            if len(self.calls) >= 2:
                both_started.set()
            await release.wait()
            return AgentResult(
                run_id=f"run-{schedule.occurrence_id}",
                root_run_id=f"run-{schedule.occurrence_id}",
                agent_name=agent_name,
                status="completed",
            )

    executor = FakeExecutor()
    orchestrator.executor = executor
    rule = config.scheduled_rules[0]
    first = asyncio.create_task(
        orchestrator.run_scheduled_rule(
            rule,
            scheduled_at=100.0,
            schedule_signature="signature-1",
        )
    )
    second = asyncio.create_task(
        orchestrator.run_scheduled_rule(
            rule,
            scheduled_at=200.0,
            schedule_signature="signature-1",
        )
    )
    await asyncio.wait_for(both_started.wait(), timeout=1)
    assert len(executor.calls) == 2
    assert all(call.repository_id == "demo" for call in executor.calls)
    release.set()
    summaries = await asyncio.gather(first, second)
    assert [summary.agent_runs for summary in summaries] == [1, 1]

    duplicate = await orchestrator.run_scheduled_rule(
        rule,
        scheduled_at=100.0,
        schedule_signature="signature-1",
    )
    assert duplicate.scheduled_occurrences == 0
    assert len(executor.calls) == 2


async def test_runtime_does_not_wait_for_previous_scheduled_period(
    tmp_path,
    monkeypatch,
) -> None:
    """后台调度循环创建下一周期时不应等待上一周期完成。"""

    config = scheduled_config(tmp_path)
    config.web.config_poll_seconds = 0.01

    class FakeManager:
        """提供调度循环所需的最小配置管理接口。"""

        def __init__(self) -> None:
            self.config = config
            self.store = StateStore(config.database.path)
            self.store.initialize()
            self.last_error = None

        def reload_if_changed(self) -> None:
            return None

    two_periods_started = asyncio.Event()
    release = asyncio.Event()

    class FakeOrchestrator:
        """阻塞周期任务，用来确认调度循环仍会创建下一周期。"""

        def __init__(self) -> None:
            self.calls = 0

        async def scan(self, _summary: CycleSummary) -> None:
            return None

        async def process_events(self, _summary: CycleSummary) -> None:
            return None

        async def run_scheduled_rule(self, *_args, **_kwargs) -> CycleSummary:
            self.calls += 1
            if self.calls >= 2:
                two_periods_started.set()
            await release.wait()
            return CycleSummary(scheduled_occurrences=1, agent_runs=1)

        async def request_shutdown(self) -> list[str]:
            release.set()
            return []

    monkeypatch.setattr(
        "teamwork_review_agents.runtime.next_scheduled_at",
        lambda _rule, after: after + 0.03,
    )
    runtime = BackgroundRuntime(FakeManager())
    orchestrator = FakeOrchestrator()
    runtime._orchestrator = orchestrator
    monkeypatch.setattr(
        "teamwork_review_agents.runtime.Orchestrator",
        lambda *_args, **_kwargs: orchestrator,
    )
    await runtime.start()
    try:
        await asyncio.wait_for(two_periods_started.wait(), timeout=1)
        assert orchestrator.calls >= 2
    finally:
        await runtime.stop()
