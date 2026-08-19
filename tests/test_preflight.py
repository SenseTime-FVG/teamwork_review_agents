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
from teamwork_review_agents.events import detect_events
from teamwork_review_agents.models import PreflightResult
from teamwork_review_agents.preflight import (
    PreflightExecutor,
    build_preflight_environment,
    execute_preflight_steps,
)
from teamwork_review_agents.preflight_cache import (
    build_repository_cache_environment,
    repository_cache_root,
)
from teamwork_review_agents.preflight_manager import ManualPreflightManager
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


async def test_preflight_failure_comment_is_reused_redacted_and_deleted_on_success(
    tmp_path,
    monkeypatch,
) -> None:
    """自动 CI 只维护一条脱敏失败评论，通过后应删除且写回失败不改 CI 终态。"""

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
            return "101"

        async def update_change_request_comment(
            self,
            _repository,
            _comment_id,
            body,
        ):
            updated.append(body)
            return True

        async def delete_change_request_comment(
            self,
            _repository,
            comment_id,
        ):
            deleted.append(comment_id)

    monkeypatch.setenv("GITHUB_TOKEN", "provider-token")
    monkeypatch.setattr(
        "teamwork_review_agents.preflight.create_provider",
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
    assert store.get_preflight_failure_comment("demo", 7) is not None

    await executor._sync_failure_comment_safely(repository, failure)
    assert len(created) == 1
    assert updated == []

    changed_failure = failure.model_copy(update={"output": "另一条失败信息"})
    await executor._sync_failure_comment_safely(repository, changed_failure)
    assert len(updated) == 1

    success = failure.model_copy(
        update={"status": "success", "output": "全部通过", "error": None}
    )
    await executor._sync_failure_comment_safely(repository, success)
    assert deleted == ["101"]
    assert store.get_preflight_failure_comment("demo", 7) is None

    reject_comment = True
    comment_failure = await executor._publish_terminal_result(repository, failure)
    assert comment_failure.status == "failure"
    assert comment_failure.status_published is True
    logs = store.list_preflight_logs("comment-run")
    comment_errors = [log for log in logs if log["event_type"] == "comment_error"]
    assert comment_errors
    assert "provider-token" not in comment_errors[-1]["payload"]
    assert "********" in comment_errors[-1]["payload"]

    manual = failure.model_copy(update={"number": None})
    await executor._sync_failure_comment_safely(repository, manual)
    assert len(created) == 1
