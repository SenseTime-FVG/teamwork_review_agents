"""确定性 CI 前置检查执行测试。"""

from __future__ import annotations

import sys
from contextlib import contextmanager

from teamwork_review_agents.config import PreflightConfig, parse_config_data
from teamwork_review_agents.events import detect_events
from teamwork_review_agents.preflight import (
    PreflightExecutor,
    build_preflight_environment,
    execute_preflight_steps,
)
from teamwork_review_agents.state import StateStore


async def test_preflight_stops_after_first_failed_step_and_preserves_output(tmp_path) -> None:
    """非零退出码必须记录失败步骤，并阻止后续命令执行。"""

    marker = tmp_path / "steps.txt"
    config = PreflightConfig.model_validate(
        {
            "enabled": True,
            "steps": [
                {
                    "name": "first",
                    "command": [
                        sys.executable,
                        "-c",
                        f"from pathlib import Path; Path({str(marker)!r}).write_text('first\\n'); print('first output')",
                    ],
                },
                {
                    "name": "failing",
                    "command": [
                        sys.executable,
                        "-c",
                        f"from pathlib import Path; p=Path({str(marker)!r}); p.write_text(p.read_text()+'failing\\n'); print('failure output'); raise SystemExit(7)",
                    ],
                },
                {
                    "name": "never",
                    "command": [
                        sys.executable,
                        "-c",
                        f"from pathlib import Path; p=Path({str(marker)!r}); p.write_text(p.read_text()+'never\\n')",
                    ],
                },
            ],
        }
    )

    outcome = await execute_preflight_steps(
        config,
        cwd=tmp_path,
        environment=build_preflight_environment(),
    )

    assert outcome.status == "failure"
    assert outcome.failed_step == "failing"
    assert outcome.exit_code == 7
    assert marker.read_text(encoding="utf-8") == "first\nfailing\n"
    assert "first output" in outcome.output
    assert "failure output" in outcome.output


async def test_preflight_times_out_and_truncates_output(tmp_path) -> None:
    """卡住的步骤必须被终止，持久化输出不能超过配置上限。"""

    config = PreflightConfig.model_validate(
        {
            "enabled": True,
            "timeout_seconds": 3,
            "max_output_bytes": 64,
            "steps": [
                {
                    "name": "slow",
                    "timeout_seconds": 1,
                    "command": [
                        sys.executable,
                        "-c",
                        "import time; print('x' * 200, flush=True); time.sleep(30)",
                    ],
                }
            ],
        }
    )

    outcome = await execute_preflight_steps(
        config,
        cwd=tmp_path,
        environment=build_preflight_environment(),
    )

    assert outcome.status == "timed_out"
    assert outcome.failed_step == "slow"
    assert len(outcome.output.encode("utf-8")) <= 64


def test_preflight_environment_excludes_host_credentials(monkeypatch) -> None:
    """被测 PR 代码不得继承 Provider、Codex 或 OpenAI 凭据。"""

    monkeypatch.setenv("GITHUB_TOKEN", "provider-secret")
    monkeypatch.setenv("CODEX_API_KEY", "codex-secret")
    monkeypatch.setenv("OPENAI_API_KEY", "openai-secret")
    monkeypatch.setenv("PATH", "/safe/bin")

    environment = build_preflight_environment()

    assert environment["PATH"] == "/safe/bin"
    assert "GITHUB_TOKEN" not in environment
    assert "CODEX_API_KEY" not in environment
    assert "OPENAI_API_KEY" not in environment


async def test_preflight_executor_reuses_success_for_same_head_and_revision(
    tmp_path,
    monkeypatch,
    snapshot_factory,
) -> None:
    """同一 Head 和配置版本再次触发时不得重复执行命令。"""

    counter = tmp_path / "counter.txt"
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
                    "preflight": {
                        "enabled": True,
                        "steps": [
                            {
                                "name": "count",
                                "command": [
                                    sys.executable,
                                    "-c",
                                    f"from pathlib import Path; p=Path({str(counter)!r}); p.write_text((p.read_text() if p.exists() else '')+'run\\n')",
                                ],
                            }
                        ],
                    },
                }
            ],
        },
        tmp_path / "config.yaml",
    )
    snapshot = snapshot_factory(
        provider="github-main",
        repository_id="demo",
        head_sha="b" * 40,
    )
    event = detect_events(None, snapshot, emit_initial=True)[0]

    @contextmanager
    def fake_worktree(*_args, **_kwargs):
        yield tmp_path

    monkeypatch.setattr(
        "teamwork_review_agents.preflight.temporary_change_request_worktree",
        fake_worktree,
    )
    store = StateStore(config.database.path)
    store.initialize()
    executor = PreflightExecutor(config, store)

    first = await executor.ensure_passed(event)
    second = await executor.ensure_passed(event)

    assert first.status == "success"
    assert second.status == "success"
    assert second.run_id == first.run_id
    assert counter.read_text(encoding="utf-8") == "run\n"
