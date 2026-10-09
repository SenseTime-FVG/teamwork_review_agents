"""版本退避与真实模型循环、看门狗及工具历史的本地集成回归。"""

from __future__ import annotations

import asyncio
import json
from functools import partial
from types import SimpleNamespace

import httpx
import pytest

from teamwork_review_agents import codex_model_client, codex_model_runner, run_control
from teamwork_review_agents.codex_executable import CodexRuntimeError
from teamwork_review_agents.codex_model_client import CodexOAuthCredentials, CodexResponsesClient
from teamwork_review_agents.codex_model_runner import CodexModelRunner
from teamwork_review_agents.config import ModelProviderConfig, ModelSelectionConfig
from teamwork_review_agents.context_compaction import ConversationContext
from teamwork_review_agents.environment import SecretRedactor
from teamwork_review_agents.model_tools import ModelToolExecutor
from teamwork_review_agents.run_control import RunControl


class FakeOAuth:
    """只提供虚构 OAuth，不读取宿主登录或刷新远端凭据。"""

    async def credentials(self):
        return CodexOAuthCredentials("test-access", "test-refresh", 0)


@pytest.fixture
def retry_harness(configured_app_factory, monkeypatch):
    """保留真实协议请求、日志和看门狗，仅替换 CLI、HTTP 与命令工具。"""

    def create(*, recover=True, summary=False, idle=0.15, total=5, parent_run_id=None):
        config = configured_app_factory()
        config.runtime.codex.model = "gpt-test"
        config.runtime.default_model = ModelSelectionConfig(provider="codex-cli", model="gpt-test")
        config.model_providers["backup"] = ModelProviderConfig(
            display_name="备用", driver="openai_responses", base_url="https://backup.example.test", default_model="gpt-backup",
        )
        config.runtime.default_model_fallbacks = [ModelSelectionConfig(provider="backup", model="gpt-backup")]
        agent = config.agents["code-reviewer"].model_copy(update={
            "sandbox": "danger-full-access", "idle_timeout_seconds": idle, "timeout_seconds": total,
        })
        requests, probes, tools, logs = [], [], [], []
        entered = asyncio.Event()

        def probe(command):
            """第一轮有效，第二轮遇到升级窗口，恢复后返回新版。"""

            probes.append(command)
            if len(probes) == 1:
                return "0.153.4"
            if not recover or len(probes) < 4:
                raise CodexRuntimeError("Codex CLI 版本探测失败（退出码 1），未发送模型请求", error_code="codex_version_probe_failed")
            return "0.159.2"

        async def handler(request):
            body = json.loads(request.content)
            requests.append((body, request.headers["version"]))
            assert body["model"] == "gpt-test"
            if len(requests) == 1:
                output = [{"type": "function_call", "call_id": "already-done", "name": "execute_command", "arguments": "{}"}]
            else:
                output = [{"type": "message", "content": [{"type": "output_text", "text": "完成"}]}]
            return httpx.Response(200, json={"output": output})

        async def execute(self, name, arguments, **kwargs):
            tools.append(name)
            return {"already_executed": True}

        async def emit(stream, kind, payload):
            logs.append((kind, payload))
            if kind == "runtime.codex_version_retry":
                entered.set()

        monkeypatch.setattr(codex_model_client, "_codex_client_version", probe)
        monkeypatch.setattr(codex_model_runner, "CodexOAuthStore", lambda path: FakeOAuth())
        monkeypatch.setattr(codex_model_runner, "CodexResponsesClient", partial(CodexResponsesClient, transport=httpx.MockTransport(handler)))
        monkeypatch.setattr(ModelToolExecutor, "execute", execute)
        if summary:
            budget_calls = 0

            async def budget(self, **kwargs):
                """让第二轮真实调用摘要客户端，保持既有工具历史不变。"""

                nonlocal budget_calls
                budget_calls += 1
                if budget_calls == 2:
                    await kwargs["summarize"]({"model": "gpt-test", "input": self.history(), "tools": [], "tool_choice": "none"})
                return None

            monkeypatch.setattr(ConversationContext, "ensure_budget", budget)

        async def run(cancel_check=None):
            return await CodexModelRunner(config).run(
                run_id="retry-run", root_run_id="retry-run", parent_run_id=parent_run_id,
                agent_name="code-reviewer", agent=agent, repository=config.repositories[0],
                context=None, prompt="保留先前已执行工具", redactor=SecretRedactor(()),
                log_callback=emit, cancel_check=cancel_check,
            )

        return SimpleNamespace(run=run, requests=requests, probes=probes, tools=tools, logs=logs, entered=entered)

    return create


@pytest.mark.parametrize("summary", [False, True])
async def test_probe_recovers_in_current_round_without_idle_timeout_or_tool_replay(retry_harness, monkeypatch, summary):
    """普通请求与摘要等待超过 idle 后均可继续，已执行工具只运行一次。"""

    monkeypatch.setattr(codex_model_client, "_VERSION_PROBE_RETRY_DELAYS", (0.3, 0.4, 0.05, 0.05, 0.05))
    h = retry_harness(summary=summary)
    result = await h.run()
    assert result.status == "completed"
    assert len(h.requests) == (3 if summary else 2)
    assert [version for _, version in h.requests] == ["0.153.4"] + ["0.159.2"] * (2 if summary else 1)
    assert h.tools == ["execute_command"]
    outputs = [item for item in h.requests[-1][0]["input"] if item.get("type") == "function_call_output"]
    assert len(outputs) == 1 and json.loads(outputs[0]["output"]) == {"already_executed": True}
    diagnostics = [(kind, payload) for kind, payload in h.logs if kind.startswith("runtime.codex_version_")]
    assert [kind for kind, _ in diagnostics] == ["runtime.codex_version_retry"] * 2 + ["runtime.codex_version_recovered"]
    assert all(payload["request_round"] == 2 for _, payload in diagnostics)
    assert not any(kind == "model.fallback" for kind, _ in h.logs)


async def test_probe_exhaustion_does_not_fallback_or_restart_prior_operations(retry_harness, monkeypatch):
    """六次失败只终止当前运行，不切备用模型或重做第一轮操作。"""

    monkeypatch.setattr(codex_model_client, "_VERSION_PROBE_RETRY_DELAYS", (0.001,) * 5)
    h = retry_harness(recover=False, idle=5)
    result = await h.run()
    assert result.status == "failed" and result.error_code == "codex_version_probe_failed"
    assert result.retryable is False
    assert len(h.probes) == 7
    assert len(h.requests) == 1 and h.tools == ["execute_command"]
    assert sum(kind == "runtime.codex_version_retry" for kind, _ in h.logs) == 5
    assert not any(kind == "model.fallback" for kind, _ in h.logs)
    failure = next(payload for kind, payload in h.logs if kind == "run.runtime_unavailable")
    assert failure["probe_attempts"] == 6 and failure["probe_retries_exhausted"] is True


@pytest.mark.parametrize("stop", ["cancel", "child_total_timeout"])
async def test_watchdog_can_stop_version_backoff(retry_harness, stop):
    """保留真实十秒退避，取消和子任务总时限仍在一秒内中断。"""

    h = retry_harness(recover=False, total=0.35, parent_run_id="parent" if stop == "child_total_timeout" else None)
    result = await asyncio.wait_for(h.run(cancel_check=(lambda: _cancel_after(h.entered)) if stop == "cancel" else None), 2)
    assert result.error_code == ("run_interrupted" if stop == "cancel" else "agent_total_timeout")
    assert len(h.probes) == 2 and len(h.requests) == 1 and h.tools == ["execute_command"]
    assert not any(kind == "runtime.codex_version_recovered" for kind, _ in h.logs)


async def _cancel_after(entered):
    """看门狗仅在确已进入退避后取消。"""

    return entered.is_set()


async def test_runtime_wait_preserves_idle_age_and_is_bounded(monkeypatch):
    """等待不续期父级或自身进展，只扣除期限内耗时，异常退出也清理。"""

    now = 10.0
    monkeypatch.setattr(run_control.time, "monotonic", lambda: now)
    parent = RunControl("parent", last_progress_at=3)
    child = RunControl("child", parent=parent, last_progress_at=8)
    with child.waiting_for_runtime(5):
        assert child.runtime_wait_active(14) is True
        assert child.runtime_wait_active(15) is False
        now = 20
    assert child.runtime_wait_deadline is None
    assert child.last_progress_at == 13 and parent.last_progress_at == 3
    now = 22
    with pytest.raises(ValueError), child.waiting_for_runtime(5):
        now = 23
        raise ValueError("本地模拟异常")
    assert child.runtime_wait_deadline is None
    assert child.last_progress_at == 14
