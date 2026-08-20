"""Preflight 与 Review Agent 编排门禁测试。"""

from __future__ import annotations

import asyncio

from teamwork_review_agents.config import parse_config_data
from teamwork_review_agents.events import detect_events
from teamwork_review_agents.models import (
    AgentResult,
    ChangeRequestActivityBatch,
    PreflightResult,
)
from teamwork_review_agents.orchestrator import CycleSummary, Orchestrator


def preflight_config(tmp_path):
    """创建启用 CI 且包含一个 Review Agent 的最小配置。"""

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
                    "preflight": {
                        "enabled": True,
                        "steps": [{"name": "test", "command": ["pytest"]}],
                    },
                }
            ],
            "agents": {"reviewer": {"prompt": "审查代码。"}},
            "rules": [
                {
                    "name": "review-new-pr",
                    "events": ["change_request.discovered"],
                    "agents": ["reviewer"],
                    "run_preflight": True,
                }
            ],
        },
        tmp_path / "config.yaml",
    )


def enqueue_discovered(orchestrator, snapshot_factory, *, state="opened"):
    """写入一条真实的首次发现事件并返回它。"""

    snapshot = snapshot_factory(
        provider="github-main",
        repository_id="demo",
        head_sha="b" * 40,
        state=state,
    )
    event = detect_events(None, snapshot, emit_initial=True)[0]
    orchestrator.store.save_snapshot_and_events(snapshot, [event])
    return event


class FakeAgentExecutor:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def execute(self, *, agent_name, **_kwargs):
        self.calls.append(agent_name)
        return AgentResult(
            run_id=f"run-{agent_name}",
            root_run_id=f"run-{agent_name}",
            agent_name=agent_name,
            status="completed",
        )


class FakePreflightExecutor:
    def __init__(self, result: PreflightResult) -> None:
        self.result = result
        self.calls = 0

    async def ensure_passed(self, _event):
        self.calls += 1
        return self.result


def result(status: str, *, error: str | None = None) -> PreflightResult:
    return PreflightResult(
        run_id="preflight-1",
        repository_id="demo",
        number=7,
        head_sha="b" * 40,
        status=status,
        failed_step="tests" if status != "success" else None,
        exit_code=1 if status == "failure" else None,
        error=error,
    )


def test_rule_can_request_preflight_without_repository_ci_configuration(
    tmp_path,
) -> None:
    """规则选择全部仓库时，不要求每个仓库都配置 CI。"""

    config = parse_config_data(
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
            "agents": {"reviewer": {"prompt": "审查代码。"}},
            "rules": [
                {
                    "name": "all-repositories",
                    "events": ["change_request.opened"],
                    "agents": ["reviewer"],
                    "run_preflight": True,
                }
            ],
        },
        tmp_path / "config.yaml",
    )

    assert config.rules[0].run_preflight is True
    assert config.rules[0].repositories is None
    assert config.repositories[0].preflight.enabled is False


async def test_failed_preflight_completes_event_without_starting_review_agent(
    tmp_path,
    snapshot_factory,
) -> None:
    """代码测试失败是终态门禁结果，不得启动 Agent 或周期性重试同一 SHA。"""

    orchestrator = Orchestrator(preflight_config(tmp_path), recover_interrupted=False)
    enqueue_discovered(orchestrator, snapshot_factory)
    agents = FakeAgentExecutor()
    orchestrator.executor = agents
    orchestrator.preflight = FakePreflightExecutor(result("failure"))
    summary = CycleSummary()

    await orchestrator.process_events(summary)

    assert agents.calls == []
    assert orchestrator.store.pending_events() == []
    assert summary.preflight_failures == 1
    assert summary.agent_runs == 0


async def test_reused_preflight_result_is_linked_to_current_event(
    tmp_path,
    snapshot_factory,
) -> None:
    """复用 CI 时当前事件仍应能展示对应失败详情。"""

    orchestrator = Orchestrator(preflight_config(tmp_path), recover_interrupted=False)
    event = enqueue_discovered(orchestrator, snapshot_factory)
    reservation = orchestrator.store.begin_preflight_run(
        proposed_run_id="preflight-reused",
        idempotency_key="demo:7:reused-preflight",
        event_id=event.id,
        repository_id=event.repository_id,
        number=event.number,
        head_sha=event.new.head_sha,
        config_revision=orchestrator.config.revision,
        max_attempts=2,
    )
    assert reservation is not None
    reused = PreflightResult(
        run_id=reservation.run_id,
        repository_id=event.repository_id,
        number=event.number,
        head_sha=event.new.head_sha,
        status="failure",
        failed_step="tests",
        exit_code=1,
        output="断言失败",
        status_published=True,
        reused=True,
    )
    orchestrator.store.finish_preflight_run(reused)
    orchestrator.executor = FakeAgentExecutor()
    orchestrator.preflight = FakePreflightExecutor(reused)

    await orchestrator.process_events(CycleSummary())

    detail = orchestrator.store.get_event_detail(event.id)
    assert detail is not None
    assert detail["preflight"]["run_id"] == reservation.run_id
    assert detail["preflight"]["reused"] == 1
    assert "output" not in detail["preflight"]
    preflight_detail = orchestrator.store.get_preflight_run(reservation.run_id)
    assert preflight_detail is not None
    assert preflight_detail["output"] == "断言失败"


async def test_successful_preflight_allows_matching_review_agents(
    tmp_path,
    snapshot_factory,
) -> None:
    """只有 CI 成功时，现有规则匹配到的 Review Agent 才能运行。"""

    orchestrator = Orchestrator(preflight_config(tmp_path), recover_interrupted=False)
    enqueue_discovered(orchestrator, snapshot_factory)
    agents = FakeAgentExecutor()
    orchestrator.executor = agents
    orchestrator.preflight = FakePreflightExecutor(result("success"))
    summary = CycleSummary()

    await orchestrator.process_events(summary)

    assert agents.calls == ["reviewer"]
    assert orchestrator.store.pending_events() == []
    assert summary.preflight_failures == 0
    assert summary.agent_runs == 1


async def test_matching_event_stays_processing_while_preflight_is_running(
    tmp_path,
    snapshot_factory,
) -> None:
    """等待本地 CI 的匹配事件不能提前显示为未触发。"""

    orchestrator = Orchestrator(preflight_config(tmp_path), recover_interrupted=False)
    event = enqueue_discovered(orchestrator, snapshot_factory)
    unmatched_event = event.model_copy(
        update={
            "id": f"{event.id}-updated",
            "type": "change_request.updated",
        }
    )
    orchestrator.store.save_snapshot_and_events(
        event.current_snapshot,
        [unmatched_event],
    )
    agents = FakeAgentExecutor()
    started = asyncio.Event()
    release = asyncio.Event()

    class WaitingRecordedPreflightExecutor:
        async def ensure_passed(self, current_event):
            reservation = orchestrator.store.begin_preflight_run(
                proposed_run_id="preflight-waiting",
                idempotency_key="demo:7:waiting-preflight",
                event_id=current_event.id,
                repository_id=current_event.repository_id,
                number=current_event.number,
                head_sha=current_event.new.head_sha,
                config_revision=orchestrator.config.revision,
                max_attempts=2,
            )
            assert reservation is not None
            started.set()
            await release.wait()
            completed = PreflightResult(
                run_id=reservation.run_id,
                repository_id=current_event.repository_id,
                number=current_event.number,
                head_sha=current_event.new.head_sha,
                status="success",
                status_published=True,
            )
            orchestrator.store.finish_preflight_run(completed)
            return completed

    orchestrator.executor = agents
    orchestrator.preflight = WaitingRecordedPreflightExecutor()
    summary = CycleSummary()
    processing = asyncio.create_task(orchestrator.process_events(summary))

    await asyncio.wait_for(started.wait(), timeout=1)
    record = next(
        item for item in orchestrator.store.list_events() if item["event_id"] == event.id
    )
    assert record["status"] == "processing"
    assert record["preflight_status"] == "running"
    assert record["trigger_count"] == 0
    unmatched_record = next(
        item
        for item in orchestrator.store.list_events()
        if item["event_id"] == unmatched_event.id
    )
    assert unmatched_record["status"] == "unmatched"
    assert unmatched_record["preflight_status"] is None
    assert unmatched_record["trigger_count"] == 0

    release.set()
    await asyncio.wait_for(processing, timeout=1)

    completed = next(
        item for item in orchestrator.store.list_events() if item["event_id"] == event.id
    )
    assert completed["status"] == "completed"
    assert completed["preflight_status"] == "success"
    assert completed["trigger_count"] == 1
    assert agents.calls == ["reviewer"]


async def test_rule_without_preflight_does_not_wait_for_enabled_repository_ci(
    tmp_path,
    snapshot_factory,
) -> None:
    """仓库启用 CI 时，未选择 CI 的规则仍须直接运行 Agent。"""

    config = preflight_config(tmp_path)
    config.rules[0].run_preflight = False
    orchestrator = Orchestrator(config, recover_interrupted=False)
    enqueue_discovered(orchestrator, snapshot_factory)
    agents = FakeAgentExecutor()
    preflight = FakePreflightExecutor(result("failure"))
    orchestrator.executor = agents
    orchestrator.preflight = preflight
    summary = CycleSummary()

    await orchestrator.process_events(summary)

    assert agents.calls == ["reviewer"]
    assert preflight.calls == 0
    assert summary.preflight_runs == 0
    assert summary.agent_runs == 1


async def test_rule_preflight_is_bypassed_when_repository_ci_is_disabled(
    tmp_path,
    snapshot_factory,
) -> None:
    """规则选择 CI 但仓库未启用时不得报错或阻断 Agent。"""

    config = preflight_config(tmp_path)
    config.repositories[0].preflight.enabled = False
    orchestrator = Orchestrator(config, recover_interrupted=False)
    enqueue_discovered(orchestrator, snapshot_factory)
    agents = FakeAgentExecutor()
    preflight = FakePreflightExecutor(result("failure"))
    orchestrator.executor = agents
    orchestrator.preflight = preflight
    summary = CycleSummary()

    await orchestrator.process_events(summary)

    assert agents.calls == ["reviewer"]
    assert preflight.calls == 0
    assert summary.preflight_runs == 0
    assert summary.errors == []


async def test_rule_preflight_is_bypassed_for_non_open_change_request(
    tmp_path,
    snapshot_factory,
) -> None:
    """已关闭或合并的变更请求不执行 Head CI，但仍运行匹配 Agent。"""

    orchestrator = Orchestrator(preflight_config(tmp_path), recover_interrupted=False)
    enqueue_discovered(orchestrator, snapshot_factory, state="merged")
    agents = FakeAgentExecutor()
    preflight = FakePreflightExecutor(result("failure"))
    orchestrator.executor = agents
    orchestrator.preflight = preflight
    summary = CycleSummary()

    await orchestrator.process_events(summary)

    assert agents.calls == ["reviewer"]
    assert preflight.calls == 0
    assert summary.preflight_runs == 0


async def test_failed_preflight_only_blocks_rules_that_requested_ci(
    tmp_path,
    snapshot_factory,
) -> None:
    """混合规则中直接 Agent 不等待 CI，CI 失败只阻断门禁 Agent。"""

    config = preflight_config(tmp_path)
    config.agents["direct-reviewer"] = config.agents["reviewer"].model_copy()
    config.rules.append(
        config.rules[0].model_copy(
            update={
                "name": "direct-review",
                "agents": ["direct-reviewer"],
                "run_preflight": False,
            }
        )
    )
    orchestrator = Orchestrator(config, recover_interrupted=False)
    enqueue_discovered(orchestrator, snapshot_factory)
    direct_started = asyncio.Event()

    class SignalingAgentExecutor(FakeAgentExecutor):
        async def execute(self, *, agent_name, **kwargs):
            if agent_name == "direct-reviewer":
                direct_started.set()
            return await super().execute(agent_name=agent_name, **kwargs)

    class WaitingPreflightExecutor:
        calls = 0

        async def ensure_passed(self, _event):
            self.calls += 1
            await asyncio.wait_for(direct_started.wait(), timeout=1)
            return result("failure")

    agents = SignalingAgentExecutor()
    preflight = WaitingPreflightExecutor()
    orchestrator.executor = agents
    orchestrator.preflight = preflight
    summary = CycleSummary()

    await orchestrator.process_events(summary)

    assert agents.calls == ["direct-reviewer"]
    assert preflight.calls == 1
    assert summary.preflight_failures == 1
    assert summary.agent_runs == 1


async def test_preflight_infrastructure_error_retries_without_running_agent(
    tmp_path,
    snapshot_factory,
) -> None:
    """Git、进程或状态 API 故障必须按上限重试，且不能放行 Agent。"""

    orchestrator = Orchestrator(preflight_config(tmp_path), recover_interrupted=False)
    event = enqueue_discovered(orchestrator, snapshot_factory)
    agents = FakeAgentExecutor()
    orchestrator.executor = agents
    orchestrator.preflight = FakePreflightExecutor(
        result("error", error="GitHub 状态 API 不可用")
    )
    summary = CycleSummary()

    await orchestrator.process_events(summary)

    assert agents.calls == []
    assert [pending.id for pending in orchestrator.store.pending_events()] == [event.id]
    record = orchestrator.store.list_events()[0]
    max_attempts = orchestrator.config.runtime.event_retry_count + 1
    assert record["status"] == "failed"
    assert record["attempts"] == max_attempts
    assert summary.preflight_errors == max_attempts
    assert any("GitHub 状态 API 不可用" in error for error in summary.errors)


async def test_superseded_preflight_completes_event_without_retrying_agent(
    tmp_path,
    snapshot_factory,
) -> None:
    """被新 Head 取代的事件应终态跳过，并让资源队列继续向后处理。"""

    orchestrator = Orchestrator(preflight_config(tmp_path), recover_interrupted=False)
    event = enqueue_discovered(orchestrator, snapshot_factory)
    agents = FakeAgentExecutor()
    preflight = FakePreflightExecutor(
        result(
            "superseded",
            error=f"事件 Head 已被后续提交取代：期望 {'a' * 40}，当前 {'b' * 40}",
        )
    )
    orchestrator.executor = agents
    orchestrator.preflight = preflight
    summary = CycleSummary()

    await orchestrator.process_events(summary)

    assert agents.calls == []
    assert preflight.calls == 1
    assert orchestrator.store.pending_events() == []
    record = orchestrator.store.list_events()[0]
    assert record["status"] == "completed"
    assert record["attempts"] == 1
    assert record["error"] is None
    assert summary.preflight_failures == 0
    assert summary.preflight_errors == 0
    assert summary.errors == []


async def test_superseded_preflight_continues_with_newer_head_event(
    tmp_path,
    snapshot_factory,
) -> None:
    """旧 Head 跳过后，同一变更请求的新 Head 批次必须在本轮继续执行。"""

    config = preflight_config(tmp_path)
    config.rules[0].events = [
        "change_request.discovered",
        "change_request.commits_changed",
    ]
    orchestrator = Orchestrator(config, recover_interrupted=False)
    old_event = enqueue_discovered(orchestrator, snapshot_factory)
    current = old_event.current_snapshot.model_copy(update={"head_sha": "c" * 40})
    newer_events = detect_events(
        old_event.current_snapshot,
        current,
        batch_id="newer-head-batch",
    )
    orchestrator.store.save_snapshot_and_events(current, newer_events)
    agents = FakeAgentExecutor()

    class SequencedPreflightExecutor:
        def __init__(self) -> None:
            self.heads: list[str] = []

        async def ensure_passed(self, event):
            self.heads.append(event.new.head_sha)
            if len(self.heads) == 1:
                return PreflightResult(
                    run_id="preflight-old",
                    repository_id=event.repository_id,
                    number=event.number,
                    head_sha=event.new.head_sha,
                    status="superseded",
                    error="事件 Head 已被后续提交取代",
                )
            return PreflightResult(
                run_id="preflight-new",
                repository_id=event.repository_id,
                number=event.number,
                head_sha=event.new.head_sha,
                status="success",
            )

    preflight = SequencedPreflightExecutor()
    orchestrator.executor = agents
    orchestrator.preflight = preflight
    summary = CycleSummary()

    await orchestrator.process_events(summary)

    assert preflight.heads == ["b" * 40, "c" * 40]
    assert agents.calls == ["reviewer"]
    assert orchestrator.store.pending_events() == []
    records = {item["event_id"]: item for item in orchestrator.store.list_events()}
    assert records[old_event.id]["status"] == "completed"
    commits_event = next(
        event for event in newer_events if event.type == "change_request.commits_changed"
    )
    assert records[commits_event.id]["status"] == "completed"
    assert summary.preflight_errors == 0


async def test_preflight_repository_emits_initial_event_without_global_opt_in(
    tmp_path,
    monkeypatch,
    snapshot_factory,
) -> None:
    """启用门禁的仓库必须在首次发现 PR 时自动进入 CI，不能静默只建快照。"""

    config = preflight_config(tmp_path)
    assert config.scanner.emit_initial_events is False
    snapshot = snapshot_factory(
        provider="github-main",
        repository_id="demo",
        head_sha="c" * 40,
    )

    class FakeProvider:
        name = "github-main"

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def list_change_requests(self, _repository, *, updated_since=None):
            return [snapshot]

        async def list_change_request_activities(
            self,
            _repository,
            _number,
            *,
            cursor=None,
            since=None,
        ):
            return ChangeRequestActivityBatch(baseline=True)

    monkeypatch.setenv("GITHUB_TOKEN", "provider-token")
    monkeypatch.setattr(
        "teamwork_review_agents.orchestrator.create_provider",
        lambda *_args, **_kwargs: FakeProvider(),
    )
    orchestrator = Orchestrator(config, recover_interrupted=False)
    summary = CycleSummary()

    await orchestrator.scan(summary)

    events = orchestrator.store.pending_events()
    assert [event.type for event in events] == [
        "change_request.discovered"
    ], summary.errors
