"""Preflight 与 Review Agent 编排门禁测试。"""

from __future__ import annotations

from teamwork_review_agents.config import parse_config_data
from teamwork_review_agents.events import detect_events
from teamwork_review_agents.models import PreflightResult
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
                }
            ],
        },
        tmp_path / "config.yaml",
    )


def enqueue_discovered(orchestrator, snapshot_factory):
    """写入一条真实的首次发现事件并返回它。"""

    snapshot = snapshot_factory(
        provider="github-main",
        repository_id="demo",
        head_sha="b" * 40,
    )
    event = detect_events(None, snapshot, emit_initial=True)[0]
    orchestrator.store.save_snapshot_and_events(snapshot, [event])
    return event


class FakeAgentExecutor:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def execute(self, *, agent_name, **_kwargs):
        self.calls.append(agent_name)
        return object()


class FakePreflightExecutor:
    def __init__(self, result: PreflightResult) -> None:
        self.result = result

    async def ensure_passed(self, _event):
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
