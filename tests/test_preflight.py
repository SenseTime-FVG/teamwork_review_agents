"""确定性 CI 前置检查执行测试。"""

from __future__ import annotations

import os
import sys
import time
from contextlib import contextmanager

import pytest

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
    updates = []

    async def record_step(update) -> None:
        updates.append(update)

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
        on_step_update=record_step,
    )

    assert outcome.status == "failure"
    assert outcome.failed_step == "failing"
    assert outcome.exit_code == 7
    assert marker.read_text(encoding="utf-8") == "first\nfailing\n"
    assert "first output" in outcome.output
    assert "failure output" in outcome.output
    assert [(update.step_index, update.status) for update in updates] == [
        (0, "running"),
        (0, "success"),
        (1, "running"),
        (1, "failure"),
    ]
    assert updates[-1].exit_code == 7


async def test_preflight_times_out_and_truncates_output(tmp_path) -> None:
    """卡住的步骤必须被终止，持久化输出不能超过配置上限。"""

    updates = []

    async def record_step(update) -> None:
        updates.append(update)

    config = PreflightConfig.model_validate(
        {
            "enabled": True,
            "timeout_seconds": 3,
            "max_output_bytes": 62,
            "steps": [
                {
                    "name": "slow",
                    "timeout_seconds": 1,
                    "command": [
                        sys.executable,
                        "-c",
                        "import time; print('测' * 200, flush=True); time.sleep(30)",
                    ],
                }
            ],
        }
    )

    outcome = await execute_preflight_steps(
        config,
        cwd=tmp_path,
        environment=build_preflight_environment(),
        on_step_update=record_step,
    )

    assert outcome.status == "timed_out"
    assert outcome.failed_step == "slow"
    assert "测" in outcome.output
    assert len(outcome.output.encode("utf-8")) <= 62
    assert [(update.step_index, update.status) for update in updates] == [
        (0, "running"),
        (0, "timed_out"),
    ]
    assert updates[-1].timeout_seconds == 1


async def test_preflight_timeout_kills_background_process_holding_stdout(
    tmp_path,
) -> None:
    """主进程提前退出时，继承 stdout 的后台进程也必须受同一超时约束。"""

    child_code = "import time; time.sleep(5)"
    parent_code = (
        "import subprocess, sys; "
        f"subprocess.Popen([sys.executable, '-c', {child_code!r}]); "
        "print('parent exited', flush=True)"
    )
    config = PreflightConfig.model_validate(
        {
            "enabled": True,
            "timeout_seconds": 3,
            "steps": [
                {
                    "name": "background",
                    "timeout_seconds": 1,
                    "command": [sys.executable, "-c", parent_code],
                }
            ],
        }
    )

    started_at = time.monotonic()
    outcome = await execute_preflight_steps(
        config,
        cwd=tmp_path,
        environment=build_preflight_environment(),
    )

    assert outcome.status == "timed_out"
    assert outcome.failed_step == "background"
    assert "parent exited" in outcome.output
    assert time.monotonic() - started_at < 3


@pytest.mark.skipif(
    os.name == "nt",
    reason="该用例专门验证 POSIX setsid 后代，不适用于 Windows",
)
async def test_preflight_timeout_is_bounded_when_background_process_escapes_group(
    tmp_path,
) -> None:
    """后台进程主动 setsid 后，清理等待也不能突破硬截止时间。"""

    child_code = "import time; time.sleep(5)"
    parent_code = (
        "import subprocess, sys; "
        f"subprocess.Popen([sys.executable, '-c', {child_code!r}], "
        "start_new_session=True); "
        "print('escaped child', flush=True)"
    )
    config = PreflightConfig.model_validate(
        {
            "enabled": True,
            "timeout_seconds": 3,
            "steps": [
                {
                    "name": "escaped-background",
                    "timeout_seconds": 1,
                    "command": [sys.executable, "-c", parent_code],
                }
            ],
        }
    )

    started_at = time.monotonic()
    outcome = await execute_preflight_steps(
        config,
        cwd=tmp_path,
        environment=build_preflight_environment(),
    )

    assert outcome.status == "timed_out"
    assert outcome.failed_step == "escaped-background"
    assert time.monotonic() - started_at < 3


def test_preflight_environment_excludes_host_credentials(monkeypatch, tmp_path) -> None:
    """被测 PR 代码不得继承 Provider、Codex 或 OpenAI 凭据。"""

    monkeypatch.setenv("GITHUB_TOKEN", "provider-secret")
    monkeypatch.setenv("CODEX_API_KEY", "codex-secret")
    monkeypatch.setenv("OPENAI_API_KEY", "openai-secret")
    monkeypatch.setenv("HOME", "/home/service-with-credentials")
    monkeypatch.setenv("PATH", "/safe/bin")

    environment = build_preflight_environment(home=tmp_path)

    assert environment["PATH"] == "/safe/bin"
    assert environment["HOME"] == str(tmp_path)
    assert environment["PYTHONUTF8"] == "1"
    assert environment["PYTHONIOENCODING"] == "utf-8"
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

    statuses: list[str] = []

    class FakeProvider:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def set_commit_status(self, _repository, _sha, *, state, **_kwargs):
            statuses.append(state)

    monkeypatch.setenv("GITHUB_TOKEN", "provider-token")
    monkeypatch.setattr(
        "teamwork_review_agents.preflight.create_provider",
        lambda *_args, **_kwargs: FakeProvider(),
    )

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
    assert first.reused is False
    assert second.status == "success"
    assert second.reused is True
    assert second.run_id == first.run_id
    assert counter.read_text(encoding="utf-8") == "run\n"
    assert statuses == ["pending", "success"]


async def test_preflight_retries_only_final_status_delivery(
    tmp_path,
    monkeypatch,
    snapshot_factory,
) -> None:
    """本地命令已有终态后，状态回写失败只能补发，不能重跑命令。"""

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
        head_sha="c" * 40,
    )
    event = detect_events(None, snapshot, emit_initial=True)[0]

    @contextmanager
    def fake_worktree(*_args, **_kwargs):
        yield tmp_path

    statuses: list[str] = []
    final_attempts = 0

    class FakeProvider:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def set_commit_status(self, _repository, _sha, *, state, **_kwargs):
            nonlocal final_attempts
            statuses.append(state)
            if state == "success":
                final_attempts += 1
                if final_attempts == 1:
                    raise RuntimeError("temporary GitHub outage")

    monkeypatch.setenv("GITHUB_TOKEN", "provider-token")
    monkeypatch.setattr(
        "teamwork_review_agents.preflight.create_provider",
        lambda *_args, **_kwargs: FakeProvider(),
    )
    monkeypatch.setattr(
        "teamwork_review_agents.preflight.temporary_change_request_worktree",
        fake_worktree,
    )
    store = StateStore(config.database.path)
    store.initialize()
    executor = PreflightExecutor(config, store)

    first = await executor.ensure_passed(event)
    second = await executor.ensure_passed(event)

    assert first.status == "error"
    assert second.status == "success"
    assert counter.read_text(encoding="utf-8") == "run\n"
    assert statuses == ["pending", "success", "success"]
