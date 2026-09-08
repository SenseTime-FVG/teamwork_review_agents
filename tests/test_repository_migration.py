"""仓库展示名称、身份关联和真实 Git 目录迁移的回归测试。"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from teamwork_review_agents import repository_migration as migration
from teamwork_review_agents.config_manager import ConfigManager
from teamwork_review_agents.events import detect_events
from teamwork_review_agents.models import stable_hash
from teamwork_review_agents.preflight_cache import repository_cache_root
from teamwork_review_agents.webapp import create_app
from teamwork_review_agents.workspace_snapshot import (
    ARCHIVE_FILE_NAME,
    SNAPSHOT_DIRECTORY_NAME,
    _read_metadata,
)
from test_environment_and_web import write_config


def git(path: Path, *arguments: str) -> str:
    """测试仅操作临时仓库，不访问网络，也不修改项目自身的 Git 状态。"""

    return subprocess.run(
        ["git", "-C", str(path), *arguments],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout.strip()


def save(manager: ConfigManager, **updates):
    """以界面脱敏后的仓库草稿保存，覆盖真实 Secret 合并路径。"""

    repository = manager.document()["repositories"][0]
    previous = repository["id"]
    repository.update(updates)
    return manager.save_repository(
        expected_revision=manager.config.revision,
        original_id=previous,
        repository_id=repository["id"],
        repository=repository,
    )


def seed_workspace(manager: ConfigManager) -> tuple[Path, Path, Path, str]:
    """创建基础仓库、只读 linked worktree、独立 clone 与保留文件。"""

    config = manager.config
    repo = config.repositories[0]
    base = repo.workspace
    git(base, "init")
    git(base, "config", "user.name", "迁移测试")
    git(base, "config", "user.email", "migration@example.invalid")
    git(base, "config", "commit.gpgsign", "false")
    (base / "tracked.txt").write_text("已提交\n", encoding="utf-8")
    git(base, "add", "tracked.txt")
    git(base, "commit", "-m", "初始化测试仓库")
    head = git(base, "rev-parse", "HEAD")
    root = config.database.path.parent / "worktrees" / stable_hash(repo.id)[:16]
    linked, clone = root / "linked", root / "clone"
    git(base, "worktree", "add", "--detach", str(linked), "HEAD")
    git(base, "clone", "--shared", str(base), str(clone))
    (clone / "tracked.txt").write_text("已暂存\n", encoding="utf-8")
    git(clone, "add", "tracked.txt")
    (clone / "untracked.txt").write_text("不能丢失\n", encoding="utf-8")
    (base / "local.txt").write_text("基础仓库未提交\n", encoding="utf-8")
    (root / ".clone.retained.json").write_text(
        json.dumps({"workspace": str(clone), "reason": "保留未提交修改"}),
        encoding="utf-8",
    )
    cache = repository_cache_root(config, repo)
    cache.mkdir(parents=True)
    (cache / "download.bin").write_bytes(b"cached dependency")
    snapshot = cache / SNAPSHOT_DIRECTORY_NAME / "old-snapshot"
    snapshot.mkdir(parents=True)
    (snapshot / "metadata.json").write_text(
        '{"fingerprint":"old-snapshot"}', encoding="utf-8"
    )
    (snapshot / ARCHIVE_FILE_NAME).write_bytes(b"snapshot archive")
    assert _read_metadata(snapshot) is not None
    return linked, clone, cache, head


def seed_history(
    manager: ConfigManager, snapshot_factory, linked: Path, clone: Path, cache: Path
):
    """保留稳定事件 ID，并构造待处理事件、历史子运行及定时运行。"""

    snapshot = snapshot_factory(provider="provider-main", repository_id="first")
    event = detect_events(None, snapshot, emit_initial=True)[0]
    manager.store.save_snapshot_and_events(
        snapshot, [event], activity_cursor={"cursor": "keep"}
    )
    manager.store.set_service_state(
        "repository_scan:first", {"completed_at": "2026-09-08T10:00:00+00:00"}
    )
    context = {
        "rule_name": "timer",
        "occurrence_id": "occurrence",
        "repository_id": "first",
    }
    with manager.store.connect() as db:
        for run_id, path, parent, source, trigger in (
            ("linked", linked, None, "event", None),
            ("clone", clone, "linked", "event", None),
            ("timer", clone, None, "schedule", json.dumps(context)),
        ):
            db.execute(
                """INSERT INTO agent_runs(run_id,root_run_id,parent_run_id,idempotency_key,
                event_id,agent_name,resource_key,repository_id,trigger_source,trigger_context,
                status,prompt,workspace_path,started_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,0)""",
                (
                    run_id,
                    parent or run_id,
                    parent,
                    f"key-{run_id}",
                    event.id if source == "event" else None,
                    "reviewer",
                    "provider-main:first:7",
                    "first" if parent is None else None,
                    source,
                    trigger,
                    "completed",
                    "原始身份 first",
                    str(path),
                ),
            )
        db.execute(
            """INSERT INTO preflight_runs(run_id,idempotency_key,event_id,repository_id,
            number,head_sha,config_revision,cache_path,status,started_at)
            VALUES('ci','ci-key',?,'first',7,'sha','old',?,'success',0)""",
            (event.id, str(cache)),
        )
        db.execute(
            "INSERT INTO event_preflight_links(event_id,run_id,linked_at) VALUES(?,'ci',0)",
            (event.id,),
        )
        db.execute(
            "INSERT INTO event_agent_dispatches VALUES(?,'key-linked','review','reviewer',0)",
            (event.id,),
        )
        db.execute(
            "INSERT INTO managed_comments VALUES('first',7,'review','main',1,'remote-comment','sha','hash',0)"
        )
        db.execute(
            "INSERT INTO run_logs(run_id,created_at,stream,event_type,payload) VALUES('linked',0,'system','test','first')"
        )
    return event.id


def test_display_name_does_not_migrate_identity_or_directory(tmp_path):
    """展示名称独立保存，清空后仍保留原来的唯一 ID。"""

    manager = ConfigManager(write_config(tmp_path))
    original = manager.config.repositories[0].workspace
    saved = save(manager, display_name="  业务仓库  ")
    assert saved.repositories[0].display_name == "业务仓库"
    assert saved.repositories[0].id == "first"
    assert saved.repositories[0].workspace == original
    assert original.exists()
    assert save(manager, display_name="").repositories[0].display_name is None


@pytest.mark.parametrize(
    "change_id,change_path", [(True, False), (False, True), (True, True)]
)
def test_migrate_real_git_and_history(
    tmp_path, snapshot_factory, change_id, change_path
):
    """三种迁移均保留提交、暂存和未跟踪内容，Git 双向引用仍可使用。"""

    path = write_config(tmp_path)
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    document["rules"][0].update(
        repositories=["first"], conditions={"repository_id": "first"}
    )
    document["scheduled_rules"] = [
        {"name": "timer", "agents": ["reviewer"], "repositories": ["first"]}
    ]
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    manager = ConfigManager(path)
    linked, clone, cache, head = seed_workspace(manager)
    old = manager.config.repositories[0]
    event_id = seed_history(manager, snapshot_factory, linked, clone, cache)
    changes = {"display_name": "新展示名称"}
    if change_id:
        changes["id"] = "renamed"
    if change_path:
        changes["workspace"] = str(tmp_path / "custom" / "destination")
    config = save(manager, **changes)
    new = config.repositories[0]
    root = config.database.path.parent / "worktrees" / stable_hash(new.id)[:16]
    new_cache = repository_cache_root(config, new)
    assert new.workspace == (
        tmp_path / "custom" / "destination" if change_path else tmp_path / "renamed"
    )
    assert not old.workspace.exists()
    if change_id:
        assert not linked.parent.exists()
        assert not cache.exists()
    for workspace in (new.workspace, root / "linked", root / "clone"):
        assert git(workspace, "rev-parse", "HEAD") == head
        git(workspace, "fsck", "--no-dangling")
    assert git(root / "linked", "rev-parse", "--git-common-dir") == str(
        new.workspace / ".git"
    )
    assert str(root / "linked") in git(new.workspace, "worktree", "list", "--porcelain")
    assert "M  tracked.txt" in git(root / "clone", "status", "--porcelain")
    assert (root / "clone" / "untracked.txt").read_text(
        encoding="utf-8"
    ) == "不能丢失\n"
    assert (new.workspace / "local.txt").exists()
    assert (new_cache / "download.bin").read_bytes() == b"cached dependency"
    assert json.loads((root / ".clone.retained.json").read_text())["workspace"] == str(
        root / "clone"
    )
    assert _read_metadata(new_cache / SNAPSHOT_DIRECTORY_NAME / "old-snapshot") is None
    assert config.rules[0].repositories == [new.id]
    assert config.rules[0].conditions["repository_id"] == new.id
    assert config.scheduled_rules[0].repositories == [new.id]
    assert (
        manager.document(mask_secrets=False)["repositories"][0]["environment"][
            "REPOSITORY_SECRET"
        ]["value"]
        == "first-secret"
    )
    with manager.store.connect() as db:
        event = db.execute(
            "SELECT * FROM event_inbox WHERE event_id=?", (event_id,)
        ).fetchone()
        assert event["repository_id"] == new.id
        assert json.loads(event["payload"])["new"]["repository_id"] == new.id
        assert event["status"] == "pending"
        assert (
            db.execute("SELECT snapshot_key FROM snapshots").fetchone()[0]
            == f"{new.id}:7"
        )
        for row in db.execute("SELECT * FROM agent_runs"):
            assert row["repository_id"] == new.id
            assert row["prompt"] == "原始身份 first"
            assert row["workspace_path"] == str(
                root / ("clone" if row["run_id"] == "timer" else row["run_id"])
            )
            assert row["idempotency_key"] == f"key-{row['run_id']}"
            if row["trigger_source"] == "schedule":
                assert row["resource_key"] == f"schedule:timer:{new.id}:occurrence"
                assert json.loads(row["trigger_context"])["repository_id"] == new.id
        assert (
            db.execute("SELECT repository_id FROM managed_comments").fetchone()[0]
            == new.id
        )
        assert (
            db.execute("SELECT remote_comment_id FROM managed_comments").fetchone()[0]
            == "remote-comment"
        )
        assert db.execute("SELECT cache_path FROM preflight_runs").fetchone()[0] == str(
            new_cache
        )
        assert (
            db.execute(
                "SELECT repository_id FROM provider_activity_cursors"
            ).fetchone()[0]
            == new.id
        )
        assert (
            db.execute(
                "SELECT repository_id FROM change_request_source_generations"
            ).fetchone()[0]
            == new.id
        )
        assert db.execute("SELECT payload FROM run_logs").fetchone()[0] == "first"
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
    assert not migration._journal_path(path).exists()
    assert (
        manager.store.get_service_state(f"repository_scan:{new.id}")["completed_at"]
        == "2026-09-08T10:00:00+00:00"
    )


@pytest.mark.parametrize("copy", [False, True])
def test_migration_failure_restores_files_config_and_database(
    tmp_path, monkeypatch, copy
):
    """数据库阶段出错时，恢复已搬移的目录、Git 引用和原始 YAML。"""

    manager = ConfigManager(write_config(tmp_path))
    linked, clone, cache, head = seed_workspace(manager)
    before = manager.path.read_bytes()
    monkeypatch.setattr(migration, "_needs_copy", lambda *args: copy)

    def fail(*args):
        """模拟目录已经搬完后，历史迁移失败。"""
        raise RuntimeError("模拟失败")

    monkeypatch.setattr(migration, "migrate_repository_state", fail)
    with pytest.raises(RuntimeError, match="模拟失败"):
        save(manager, id="renamed")
    assert manager.path.read_bytes() == before
    assert manager.config.repositories[0].id == "first"
    assert git(linked, "rev-parse", "HEAD") == head
    assert git(clone, "rev-parse", "HEAD") == head
    assert (cache / "download.bin").exists()
    assert not (tmp_path / "renamed").exists()
    assert not list(tmp_path.rglob("*.migration-*"))
    assert not migration._journal_path(manager.path).exists()


@pytest.mark.parametrize("commit", [False, True])
def test_restart_recovers_interrupted_migration(tmp_path, monkeypatch, commit):
    """中断前未提交则恢复旧目录，已提交则继续清理旧目录。"""

    manager = ConfigManager(write_config(tmp_path))
    linked, _, _, head = seed_workspace(manager)
    with monkeypatch.context() as patches:
        patches.setattr(migration, "_needs_copy", lambda *args: True)
        patches.setattr(migration, "_finish_journal", lambda *args, **kwargs: None)
        if not commit:

            def fail(*args):
                """模拟服务退出前事务未提交。"""
                raise RuntimeError("模拟中断")

            patches.setattr(migration, "migrate_repository_state", fail)
            with pytest.raises(RuntimeError, match="模拟中断"):
                save(manager, id="renamed")
        else:
            save(manager, id="renamed")
    assert migration._journal_path(manager.path).exists()
    recovered = ConfigManager(manager.path)
    assert recovered.config.repositories[0].id == ("renamed" if commit else "first")
    root = tmp_path / "worktrees" / stable_hash("renamed" if commit else "first")[:16]
    assert git(root / "linked", "rev-parse", "HEAD") == head
    assert (tmp_path / "first").exists() is not commit
    assert (tmp_path / "renamed").exists() is commit
    assert not migration._journal_path(manager.path).exists()


def test_target_conflict_preserves_both_directories(tmp_path):
    """即使目标是空目录也不覆盖，另一仓库的目录同样不能接管。"""

    manager = ConfigManager(write_config(tmp_path))
    target = tmp_path / "occupied"
    target.mkdir()
    (target / "valuable.txt").write_text("保留", encoding="utf-8")
    before = manager.path.read_bytes()
    with pytest.raises(ValueError, match="目标目录已存在"):
        save(manager, workspace=str(target))
    assert manager.path.read_bytes() == before
    assert (target / "valuable.txt").exists()
    assert (tmp_path / "first").exists()


def test_api_migrates_and_refreshes_runtime(tmp_path):
    """单仓库 API 更新成功后，后台与返回文档同时切换到新身份。"""

    app = create_app(write_config(tmp_path), start_scheduler=False)
    with TestClient(app) as client:
        before = client.get("/api/config").json()
        repository = {
            **before["document"]["repositories"][0],
            "id": "new",
            "display_name": "新名称",
        }
        response = client.put(
            "/api/config/repositories/first",
            json={
                "revision": before["revision"],
                "repository_id": "new",
                "repository": repository,
            },
        )
        assert response.status_code == 200, response.text
        assert (
            response.json()["document"]["repositories"][0]["display_name"] == "新名称"
        )
        assert not (tmp_path / "first").exists()
        assert (tmp_path / "new").exists()
        assert app.state.runtime.repository_migrating is False


def test_api_refuses_migration_while_scanning(tmp_path):
    """运行中的扫描阻止移动，但只改展示名称无需维护窗口。"""

    app = create_app(write_config(tmp_path), start_scheduler=False)
    with TestClient(app) as client:
        app.state.runtime.running_cycle = True
        before = client.get("/api/config").json()
        response = client.put(
            "/api/config/repositories/first",
            json={
                "revision": before["revision"],
                "repository_id": "new",
                "repository": {**before["document"]["repositories"][0], "id": "new"},
            },
        )
        assert response.status_code == 422
        assert "后台正在扫描" in response.json()["detail"]
        app.state.runtime.running_cycle = False
        assert (tmp_path / "first").exists()


@pytest.mark.parametrize(
    "target", ["second/nested", "worktrees", "preflight-cache", "."]
)
def test_migration_rejects_overlapping_managed_paths(tmp_path, target):
    """拒绝把基础仓库搬进其他仓库或自身运行/缓存根目录。"""

    manager = ConfigManager(write_config(tmp_path))
    with pytest.raises(ValueError):
        save(manager, workspace=str(tmp_path / target))
    assert (tmp_path / "first").exists()


@pytest.mark.parametrize("case", ["task", "history", "external-worktree"])
def test_migration_refuses_busy_or_ambiguous_ownership(tmp_path, case):
    """活动任务、目标历史或外部 worktree 都必须在搬移前阻止。"""

    manager = ConfigManager(write_config(tmp_path))
    seed_workspace(manager)
    if case == "task":
        with manager.store.connect() as db:
            db.execute(
                "INSERT INTO agent_runs(run_id,root_run_id,idempotency_key,agent_name,resource_key,status,prompt,started_at) VALUES('active','active','active','reviewer','other','running','',0)"
            )
    elif case == "history":
        manager.store.set_service_state(
            "repository_scan:renamed", {"completed_at": "old"}
        )
    else:
        git(
            tmp_path / "first",
            "worktree",
            "add",
            "--detach",
            str(tmp_path / "external"),
            "HEAD",
        )
    with pytest.raises(ValueError):
        save(manager, id="renamed")
    assert (tmp_path / "first").exists()
    assert not (tmp_path / "renamed").exists()


def test_full_config_save_cannot_bypass_directory_migration(tmp_path):
    """全量配置入口没有维护窗口，拒绝绕过单仓库目录迁移。"""

    manager = ConfigManager(write_config(tmp_path))
    document = manager.document()
    document["repositories"][0]["workspace"] = str(tmp_path / "elsewhere")
    with pytest.raises(ValueError, match="仓库详情单独保存"):
        manager.save(document)
    assert (tmp_path / "first").exists()


def test_api_blocks_concurrent_requests_during_migration(tmp_path, monkeypatch):
    """搬迁线程运行期间管理 API 快速拒绝，不阻塞健康检查或启动新任务。"""

    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    entered, release = Event(), Event()
    original = migration.migrate_repository_state

    def slow(*args):
        """用事件模拟跨盘复制期间的维护窗口。"""
        entered.set()
        assert release.wait(timeout=5)
        original(*args)

    monkeypatch.setattr(migration, "migrate_repository_state", slow)
    app = create_app(write_config(tmp_path), start_scheduler=False)
    with TestClient(app) as client, ThreadPoolExecutor(max_workers=1) as pool:
        before = client.get("/api/config").json()
        request = pool.submit(
            client.put,
            "/api/config/repositories/first",
            json={
                "revision": before["revision"],
                "repository_id": "new",
                "repository": {**before["document"]["repositories"][0], "id": "new"},
            },
        )
        try:
            assert entered.wait(timeout=5)
            assert client.get("/api/config").status_code == 409
            assert client.post("/api/control/scan").status_code == 409
            assert client.get("/api/health").status_code == 200
        finally:
            release.set()
        assert request.result(timeout=5).status_code == 200
        assert client.get("/api/config").status_code == 200


def test_committed_migration_cleanup_failure_can_resume(tmp_path, monkeypatch):
    """提交后清理失败不能回滚数据库，重启应继续清理保留下来的来源。"""

    manager = ConfigManager(write_config(tmp_path))
    seed_workspace(manager)
    with monkeypatch.context() as patches:
        patches.setattr(migration, "_needs_copy", lambda *args: True)

        def fail_cleanup(*args):
            """模拟跨盘旧目录清理暂时不可用。"""
            raise OSError("模拟清理失败")

        patches.setattr(migration, "remove_tree", fail_cleanup)
        with pytest.raises(OSError, match="模拟清理失败"):
            save(manager, id="renamed")
    assert migration._journal_path(manager.path).exists()
    assert (tmp_path / "renamed").exists()
    assert (tmp_path / "first").exists()
    with pytest.raises(ValueError, match="尚未恢复"):
        manager.save(manager.document())
    restored = ConfigManager(manager.path)
    assert restored.config.repositories[0].id == "renamed"
    assert not (tmp_path / "first").exists()
    assert not migration._journal_path(manager.path).exists()


def test_migration_does_not_rewrite_repository_pattern_conditions(tmp_path):
    """精确 ID 引用更新，但面向多个仓库的子串条件不是身份引用。"""

    path = write_config(tmp_path)
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    document["rules"][0]["conditions"] = {
        "repository_id__contains": "first",
        "repository_id__in": ["first", "second"],
    }
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    manager = ConfigManager(path)
    config = save(manager, id="renamed")
    assert config.rules[0].conditions["repository_id__in"] == ["renamed", "second"]
    assert config.rules[0].conditions["repository_id__contains"] == "first"
