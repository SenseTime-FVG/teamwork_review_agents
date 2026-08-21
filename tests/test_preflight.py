"""确定性 CI 前置检查执行测试。"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from teamwork_review_agents.config import PreflightConfig, parse_config_data
from teamwork_review_agents.events import (
    create_manual_replay_event,
    detect_activity_events,
    detect_events,
)
from teamwork_review_agents.models import ChangeRequestActivity, PreflightResult
from teamwork_review_agents.preflight import (
    PreflightExecutor,
    build_preflight_environment,
    execute_preflight_steps,
    preflight_idempotency_key,
)
from teamwork_review_agents.preflight_cache import (
    build_repository_cache_environment,
    repository_cache_root,
)
from teamwork_review_agents.preflight_manager import ManualPreflightManager
from teamwork_review_agents.state import StateStore
from teamwork_review_agents.workspace import WorkspaceSnapshotSuperseded


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


async def test_manual_preflight_is_unlimited_but_remains_cancellable(tmp_path) -> None:
    """手动预热忽略配置超时，但收到取消后必须回收命令并保留实时输出。"""

    cancel_event = asyncio.Event()
    chunks: list[str] = []
    updates = []

    async def record_output(chunk: str) -> None:
        chunks.append(chunk)
        if "ready" in chunk:
            cancel_event.set()

    async def record_step(update) -> None:
        updates.append(update)

    config = PreflightConfig.model_validate(
        {
            "enabled": True,
            "timeout_seconds": 1,
            "steps": [
                {
                    "name": "warm-cache",
                    "timeout_seconds": 1,
                    "command": [
                        sys.executable,
                        "-c",
                        "import time; print('ready', flush=True); time.sleep(30)",
                    ],
                }
            ],
        }
    )

    started_at = time.monotonic()
    outcome = await execute_preflight_steps(
        config,
        cwd=tmp_path,
        environment=build_preflight_environment(),
        on_step_update=record_step,
        on_output=record_output,
        cancel_check=cancel_event.is_set,
        unlimited=True,
    )

    assert outcome.status == "cancelled"
    assert "ready" in "".join(chunks)
    assert time.monotonic() - started_at < 3
    assert [(update.step_index, update.status) for update in updates] == [
        (0, "running"),
        (0, "cancelled"),
    ]


def test_repository_cache_is_shared_by_repository_not_branch(tmp_path) -> None:
    """缓存根目录只按仓库稳定身份划分，并覆盖常见依赖管理器。"""

    config = parse_config_data(
        {
            "database": {"path": str(tmp_path / "data" / "state.db")},
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
        },
        tmp_path / "config.yaml",
    )
    repository = config.repository_map()["demo"]
    root = repository_cache_root(config, repository)
    environment = build_repository_cache_environment(root)

    assert root.parent == config.database.path.parent / "preflight-cache"
    assert environment["TEAMWORK_PREFLIGHT_CACHE_DIR"] == str(root.resolve())
    assert environment["TEAMWORK_REPOSITORY_CACHE_DIR"] == str(root.resolve())
    for name in (
        "UV_CACHE_DIR",
        "PIP_CACHE_DIR",
        "NPM_CONFIG_CACHE",
        "npm_config_store_dir",
        "CARGO_HOME",
        "GOMODCACHE",
        "GRADLE_USER_HOME",
        "PLAYWRIGHT_BROWSERS_PATH",
    ):
        assert name in environment
        assert Path(environment[name]).is_relative_to(root.resolve())


async def test_manual_preflight_manager_runs_default_branch_without_event(
    tmp_path,
    monkeypatch,
) -> None:
    """仓库手动 CI 应独立完成默认分支检查，不创建事件或远端状态。"""

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
                                "name": "tests",
                                "command": [
                                    sys.executable,
                                    "-c",
                                    "print('manual ci complete', flush=True)",
                                ],
                            }
                        ],
                    },
                }
            ],
        },
        tmp_path / "config.yaml",
    )
    store = StateStore(config.database.path)
    store.initialize()

    @contextmanager
    def fake_default_worktree(*_args, **_kwargs):
        yield tmp_path, "main", "c" * 40

    monkeypatch.setattr(
        "teamwork_review_agents.preflight_manager.temporary_default_branch_worktree",
        fake_default_worktree,
    )
    manager = ManualPreflightManager(
        SimpleNamespace(config=config, store=store),
    )
    started = await manager.start("demo")
    run_id = str(started["run_id"])
    for _ in range(100):
        detail = store.get_preflight_run(run_id)
        if detail and detail["status"] != "running":
            break
        await asyncio.sleep(0.02)
    await manager.close()

    detail = store.get_preflight_run(run_id)
    assert detail is not None
    assert detail["status"] == "success"
    assert detail["trigger_source"] == "manual"
    assert detail["number"] is None
    assert detail["branch"] == "main"
    assert detail["head_sha"] == "c" * 40
    assert detail["status_published"] == 0
    assert detail["linked_events"] == []
    assert "manual ci complete" in detail["output"]
    assert any(
        log["event_type"] == "output"
        for log in store.list_preflight_logs(run_id)
    )


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


def test_preflight_environment_isolates_windows_user_directories(
    monkeypatch,
    tmp_path,
) -> None:
    """Windows CI 保留系统启动变量，但所有可写用户目录必须逐轮隔离。"""

    monkeypatch.setenv("SystemRoot", "C:/Windows")
    monkeypatch.setenv("ComSpec", "C:/Windows/System32/cmd.exe")
    monkeypatch.setenv("PATHEXT", ".COM;.EXE;.BAT;.CMD")
    home = tmp_path / "preflight-home"

    environment = build_preflight_environment(home=home, windows=True)

    assert environment["SYSTEMROOT"] == "C:/Windows"
    assert environment["COMSPEC"] == "C:/Windows/System32/cmd.exe"
    assert environment["PATHEXT"] == ".COM;.EXE;.BAT;.CMD"
    assert environment["USERPROFILE"] == str(home)
    assert environment["APPDATA"] == str(home / "AppData/Roaming")
    assert environment["LOCALAPPDATA"] == str(home / "AppData/Local")
    assert environment["TEMP"] == str(home / "tmp")
    assert environment["TMP"] == str(home / "tmp")


def test_repository_cache_quotes_maven_path_with_spaces(tmp_path) -> None:
    """Maven 本地仓库参数必须兼容 Windows 用户目录中的空格。"""

    root = tmp_path / "cache root"

    environment = build_repository_cache_environment(root)

    assert environment["MAVEN_OPTS"] == (
        f'-Dmaven.repo.local="{root.resolve() / "maven"}"'
    )


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


async def test_preflight_superseded_head_is_terminal_and_not_published(
    tmp_path,
    monkeypatch,
    snapshot_factory,
) -> None:
    """不可获取的旧 Head 应一次收敛为已跳过，不执行命令或发布平台状态。"""

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
                                "name": "never-run",
                                "command": [sys.executable, "-c", "raise SystemExit(9)"],
                            }
                        ],
                    },
                }
            ],
        },
        tmp_path / "config.yaml",
    )
    expected_head = "a" * 40
    current_head = "b" * 40
    event = detect_events(
        None,
        snapshot_factory(
            provider="github-main",
            repository_id="demo",
            head_sha=expected_head,
        ),
        emit_initial=True,
    )[0]

    @contextmanager
    def superseded_worktree(*_args, **_kwargs):
        raise WorkspaceSnapshotSuperseded(expected_head, current_head)
        yield tmp_path

    published: list[str] = []

    async def record_status(*_args, state, **_kwargs):
        published.append(state)

    monkeypatch.setattr(
        "teamwork_review_agents.preflight.temporary_change_request_worktree",
        superseded_worktree,
    )
    store = StateStore(config.database.path)
    store.initialize()
    executor = PreflightExecutor(config, store)
    monkeypatch.setattr(executor, "_set_remote_status", record_status)

    first = await executor.ensure_passed(event)
    second = await executor.ensure_passed(event)

    assert first.status == "superseded"
    assert first.reused is False
    assert second.status == "superseded"
    assert second.reused is True
    assert second.run_id == first.run_id
    assert published == []
    detail = store.get_preflight_run(first.run_id)
    assert detail is not None
    assert detail["attempts"] == 1
    assert detail["status"] == "superseded"
    assert detail["status_published"] == 0
    assert detail["steps"][0]["status"] == "skipped"
    assert "Head 已更新" in detail["steps"][0]["error"]
    assert any(
        log["event_type"] == "superseded"
        for log in store.list_preflight_logs(first.run_id)
    )


async def test_preflight_infrastructure_error_retries_without_remote_result(
    tmp_path,
    monkeypatch,
    snapshot_factory,
) -> None:
    """基础设施异常应保留重试，但不得发布平台状态或失败评论。"""

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
                        "publish_failure_comment": True,
                        "steps": [
                            {
                                "name": "never-run",
                                "command": [
                                    sys.executable,
                                    "-c",
                                    "raise SystemExit(9)",
                                ],
                            }
                        ],
                    },
                }
            ],
        },
        tmp_path / "config.yaml",
    )
    event = detect_events(
        None,
        snapshot_factory(
            provider="github-main",
            repository_id="demo",
            head_sha="c" * 40,
        ),
        emit_initial=True,
    )[0]
    worktree_attempts = 0

    @contextmanager
    def failing_worktree(*_args, **_kwargs):
        nonlocal worktree_attempts
        worktree_attempts += 1
        raise RuntimeError("Git 操作失败，请检查仓库地址、网络和权限")
        yield tmp_path

    published_statuses: list[str] = []
    synced_comments: list[str] = []

    async def record_status(*_args, state, **_kwargs):
        published_statuses.append(state)

    async def record_comment(_repository, result):
        synced_comments.append(result.status)

    monkeypatch.setattr(
        "teamwork_review_agents.preflight.temporary_change_request_worktree",
        failing_worktree,
    )
    store = StateStore(config.database.path)
    store.initialize()
    executor = PreflightExecutor(config, store)
    monkeypatch.setattr(executor, "_set_remote_status", record_status)
    monkeypatch.setattr(executor, "_sync_failure_comment_safely", record_comment)

    first = await executor.ensure_passed(event)
    second = await executor.ensure_passed(event)
    third = await executor.ensure_passed(event)
    exhausted = await executor.ensure_passed(event)

    assert first.status == "error"
    assert second.status == "error"
    assert third.status == "error"
    assert exhausted.status == "error"
    assert exhausted.reused is True
    assert worktree_attempts == 3
    assert published_statuses == []
    assert synced_comments == []
    detail = store.get_preflight_run(first.run_id)
    assert detail is not None
    assert detail["attempts"] == 3
    assert detail["status"] == "error"
    assert detail["status_published"] == 0


async def test_manual_event_restarts_exhausted_infrastructure_error(
    tmp_path,
    monkeypatch,
    snapshot_factory,
) -> None:
    """手动事件应新建 CI 运行，重新验证已耗尽的基础设施异常。"""

    config = parse_config_data(
        {
            "database": {"path": str(tmp_path / "state.db")},
            "runtime": {"event_retry_count": 0},
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
                                "name": "tests",
                                "command": [sys.executable, "-c", "print('ok')"],
                            }
                        ],
                    },
                }
            ],
        },
        tmp_path / "config.yaml",
    )
    source = detect_events(
        None,
        snapshot_factory(
            provider="github-main",
            repository_id="demo",
            head_sha="d" * 40,
        ),
        emit_initial=True,
    )[0]
    replay = create_manual_replay_event(source)
    worktree_attempts = 0

    @contextmanager
    def recovering_worktree(*_args, **_kwargs):
        nonlocal worktree_attempts
        worktree_attempts += 1
        if worktree_attempts == 1:
            raise RuntimeError("Git 操作失败")
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
        recovering_worktree,
    )
    store = StateStore(config.database.path)
    store.initialize()
    executor = PreflightExecutor(config, store)

    failed = await executor.ensure_passed(source)
    recovered = await executor.ensure_passed(replay)

    assert failed.status == "error"
    assert recovered.status == "success"
    assert recovered.reused is False
    assert recovered.run_id != failed.run_id
    assert worktree_attempts == 2
    assert statuses == ["pending", "success"]
    old_detail = store.get_preflight_run(failed.run_id)
    assert old_detail is not None
    assert old_detail["status"] == "error"
    current = store.load_preflight_result(preflight_idempotency_key(config, replay))
    assert current is not None
    assert current.run_id == recovered.run_id
    assert current.status == "success"


async def test_preflight_uses_final_snapshot_for_deduplicated_commit_batch(
    tmp_path,
    monkeypatch,
    snapshot_factory,
) -> None:
    """同批次多个提交活动只能对最终快照 Head 执行一次 CI。"""

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
                                "name": "success",
                                "command": [sys.executable, "-c", "print('ok')"],
                            }
                        ],
                    },
                }
            ],
        },
        tmp_path / "config.yaml",
    )
    old = snapshot_factory(
        provider="github-main",
        repository_id="demo",
        head_sha="a" * 40,
        updated_at="2026-08-20T05:20:00Z",
    )
    current = snapshot_factory(
        provider="github-main",
        repository_id="demo",
        head_sha="c" * 40,
        updated_at="2026-08-20T05:21:24Z",
    )
    events = detect_activity_events(
        old,
        current,
        (
            ChangeRequestActivity(
                id="commit-b",
                type="committed",
                occurred_at="2026-08-20T05:21:10Z",
                data={"sha": "b" * 40},
            ),
            ChangeRequestActivity(
                id="force-push-c",
                type="head_ref_force_pushed",
                occurred_at="2026-08-20T05:21:24Z",
                data={"sha": "", "commit_id": "c" * 40},
            ),
        ),
        batch_id="force-push-batch",
    )
    commit_events = [
        event for event in events if event.type == "change_request.commits_changed"
    ]
    representative = commit_events[0]
    assert representative.new.head_sha == "b" * 40
    assert representative.current_snapshot.head_sha == "c" * 40
    assert commit_events[-1].new.head_sha == "c" * 40
    assert preflight_idempotency_key(
        config,
        representative,
    ) == preflight_idempotency_key(config, commit_events[-1])

    prepared_snapshots = []

    @contextmanager
    def fake_worktree(_provider, _repository, snapshot, **_kwargs):
        prepared_snapshots.append(snapshot)
        yield tmp_path

    statuses: list[tuple[str, str]] = []

    async def record_status(_repository, head_sha, *, state, **_kwargs):
        statuses.append((head_sha, state))

    monkeypatch.setattr(
        "teamwork_review_agents.preflight.temporary_change_request_worktree",
        fake_worktree,
    )
    store = StateStore(config.database.path)
    store.initialize()
    executor = PreflightExecutor(config, store)
    monkeypatch.setattr(executor, "_set_remote_status", record_status)

    first = await executor.ensure_passed(representative)
    reused = await executor.ensure_passed(commit_events[-1])

    assert first.status == "success"
    assert first.head_sha == "c" * 40
    assert reused.reused is True
    assert reused.run_id == first.run_id
    assert [snapshot.head_sha for snapshot in prepared_snapshots] == ["c" * 40]
    assert statuses == [("c" * 40, "pending"), ("c" * 40, "success")]
    detail = store.get_preflight_run(first.run_id)
    assert detail is not None
    assert detail["head_sha"] == "c" * 40
    assert detail["attempts"] == 1


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


async def test_preflight_failure_comment_is_rotated_preserved_and_deleted_on_success(
    tmp_path,
    monkeypatch,
) -> None:
    """真实失败刷新单条评论，复用不刷新，通过后清理全部历史映射。"""

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
                        "publish_failure_comment": True,
                        "steps": [
                            {"name": "tests", "command": ["python", "-m", "pytest"]}
                        ],
                    },
                }
            ],
        },
        tmp_path / "config.yaml",
    )
    store = StateStore(config.database.path)
    store.initialize()
    store.create_manual_preflight_run(
        run_id="comment-run",
        repository_id="demo",
        config_revision=config.revision,
    )
    repository = config.repositories[0]
    created: list[str] = []
    updated: list[str] = []
    deleted: list[str] = []
    statuses: list[str] = []
    reject_comment = False
    reject_delete = False

    class FakeProvider:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def set_commit_status(self, _repository, _sha, *, state, **_kwargs):
            statuses.append(state)

        async def create_change_request_comment(
            self,
            _repository,
            _number,
            body,
        ):
            if reject_comment:
                raise RuntimeError("评论权限不足 provider-token")
            created.append(body)
            return str(100 + len(created))

        async def update_change_request_comment(
            self,
            _repository,
            _comment_id,
            body,
            **_kwargs,
        ):
            updated.append(body)
            return True

        async def delete_change_request_comment(
            self,
            _repository,
            comment_id,
            **_kwargs,
        ):
            if reject_delete:
                raise RuntimeError("删除评论失败 provider-token")
            deleted.append(comment_id)

    monkeypatch.setenv("GITHUB_TOKEN", "provider-token")
    monkeypatch.setattr(
        "teamwork_review_agents.preflight.create_provider",
        lambda *_args, **_kwargs: FakeProvider(),
    )
    monkeypatch.setattr(
        "teamwork_review_agents.managed_comments.create_provider",
        lambda *_args, **_kwargs: FakeProvider(),
    )
    executor = PreflightExecutor(config, store)
    failure = PreflightResult(
        run_id="comment-run",
        repository_id="demo",
        number=7,
        head_sha="a" * 40,
        status="failure",
        failed_step="tests",
        exit_code=1,
        output="x" * 20_000 + " provider-token",
        error="断言失败 provider-token",
    )

    published = await executor._publish_terminal_result(repository, failure)
    assert published.status == "failure"
    assert published.status_published is True
    assert statuses == ["failure"]
    assert len(created) == 1
    assert "provider-token" not in created[0]
    assert "********" in created[0]
    assert "仅显示末尾 12000 个字符" in created[0]
    assert "在时间线底部发布最新结果" in created[0]
    assert store.get_managed_comment(
        repository_id="demo",
        number=7,
        namespace="preflight",
        slot=repository.preflight.status_context,
        source_generation=1,
    ) is not None

    await executor._sync_failure_comment_safely(
        repository,
        failure,
        replace_existing=False,
    )
    assert len(created) == 1
    assert updated == []
    assert deleted == []

    changed_failure = failure.model_copy(update={"output": "另一条失败信息"})
    await executor._sync_failure_comment_safely(repository, changed_failure)
    assert len(created) == 2
    assert updated == []
    assert deleted == ["101"]
    current = store.get_managed_comment(
        repository_id="demo",
        number=7,
        namespace="preflight",
        slot=repository.preflight.status_context,
        source_generation=1,
    )
    assert current is not None
    assert current["remote_comment_id"] == "102"

    infrastructure_error = failure.model_copy(
        update={"status": "error", "error": "Git 操作失败"}
    )
    await executor._sync_failure_comment_safely(repository, infrastructure_error)
    assert len(created) == 2
    assert updated == []
    assert deleted == ["101"]

    next_generation_failure = failure.model_copy(
        update={
            "head_sha": "b" * 40,
            "source_generation": 2,
            "output": "新源版本仍然失败",
        }
    )
    await executor._sync_failure_comment_safely(
        repository,
        next_generation_failure,
    )
    assert len(created) == 3
    assert deleted == ["101", "102"]
    assert store.get_managed_comment(
        repository_id="demo",
        number=7,
        namespace="preflight",
        slot=repository.preflight.status_context,
        source_generation=1,
    ) is None
    assert store.get_managed_comment(
        repository_id="demo",
        number=7,
        namespace="preflight",
        slot=repository.preflight.status_context,
        source_generation=2,
    ) is not None

    await executor._sync_failure_comment_safely(
        repository,
        next_generation_failure,
        replace_existing=False,
    )
    assert len(created) == 3
    assert deleted == ["101", "102"]

    next_generation_success = next_generation_failure.model_copy(
        update={"status": "success", "output": "全部通过", "error": None}
    )
    monkeypatch.setattr(store, "source_generation", lambda *_args: 3)
    await executor._sync_failure_comment_safely(
        repository,
        next_generation_success,
    )
    assert deleted == ["101", "102"]
    assert store.get_managed_comment(
        repository_id="demo",
        number=7,
        namespace="preflight",
        slot=repository.preflight.status_context,
        source_generation=2,
    ) is not None
    monkeypatch.setattr(store, "source_generation", lambda *_args: 2)

    store.save_managed_comment(
        repository_id="demo",
        number=7,
        namespace="preflight",
        slot=repository.preflight.status_context,
        source_generation=1,
        remote_comment_id="legacy-100",
        source_head_sha="a" * 40,
        content_hash="legacy",
    )

    await executor._sync_failure_comment_safely(
        repository,
        next_generation_success,
    )
    assert deleted == ["101", "102", "legacy-100", "103"]
    assert store.get_managed_comment(
        repository_id="demo",
        number=7,
        namespace="preflight",
        slot=repository.preflight.status_context,
        source_generation=1,
    ) is None
    assert store.get_managed_comment(
        repository_id="demo",
        number=7,
        namespace="preflight",
        slot=repository.preflight.status_context,
        source_generation=2,
    ) is None

    reject_comment = True
    comment_failure = await executor._publish_terminal_result(repository, failure)
    assert comment_failure.status == "failure"
    assert comment_failure.status_published is True
    logs = store.list_preflight_logs("comment-run")
    comment_errors = [log for log in logs if log["event_type"] == "comment_error"]
    assert comment_errors
    assert "provider-token" not in comment_errors[-1]["payload"]
    assert "********" in comment_errors[-1]["payload"]

    reject_comment = False
    await executor._sync_failure_comment_safely(repository, failure)
    assert len(created) == 4
    reject_delete = True
    await executor._sync_failure_comment_safely(repository, changed_failure)
    assert len(created) == 4
    current = store.get_managed_comment(
        repository_id="demo",
        number=7,
        namespace="preflight",
        slot=repository.preflight.status_context,
        source_generation=1,
    )
    assert current is not None
    assert current["remote_comment_id"] == "104"
    comment_errors = [
        log
        for log in store.list_preflight_logs("comment-run")
        if log["event_type"] == "comment_error"
    ]
    assert "provider-token" not in comment_errors[-1]["payload"]
    assert "********" in comment_errors[-1]["payload"]

    manual = failure.model_copy(update={"number": None})
    await executor._sync_failure_comment_safely(repository, manual)
    assert len(created) == 4
