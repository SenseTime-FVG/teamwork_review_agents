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


async def test_preflight_infrastructure_error_requeues_event(
    tmp_path,
    snapshot_factory,
) -> None:
    """Git、进程或状态 API 故障必须进入现有事件重试，而不是放行 Agent。"""

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
    assert summary.preflight_errors == 1
    assert any("GitHub 状态 API 不可用" in error for error in summary.errors)


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
