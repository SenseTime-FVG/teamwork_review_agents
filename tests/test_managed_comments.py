"""按源版本代次托管顶层评论的行为测试。"""

from __future__ import annotations

from teamwork_review_agents.events import detect_events, detect_target_branch_event
from teamwork_review_agents.managed_comments import ManagedCommentService
from teamwork_review_agents.models import InvocationContext
from teamwork_review_agents.state import StateStore


def test_legacy_preflight_comment_is_migrated_only_once(tmp_path) -> None:
    """旧失败评论迁移后不得因后续重启而复活已删除的映射。"""

    store = StateStore(tmp_path / "state.db")
    store.initialize()
    store.save_preflight_failure_comment(
        repository_id="demo",
        number=7,
        status_context="teamwork/local-ci",
        remote_comment_id="legacy-101",
        head_sha="a" * 40,
        content_hash="legacy-hash",
    )

    store.initialize()
    migrated = store.get_managed_comment(
        repository_id="demo",
        number=7,
        namespace="preflight",
        slot="teamwork/local-ci",
        source_generation=1,
    )
    assert migrated is not None
    assert migrated["remote_comment_id"] == "legacy-101"
    assert store.get_preflight_failure_comment("demo", 7) is None

    store.delete_managed_comment(
        repository_id="demo",
        number=7,
        namespace="preflight",
        slot="teamwork/local-ci",
        source_generation=1,
    )
    store.initialize()
    assert store.get_managed_comment(
        repository_id="demo",
        number=7,
        namespace="preflight",
        slot="teamwork/local-ci",
        source_generation=1,
    ) is None


def test_source_generation_is_monotonic_and_ignores_target_only_changes(
    tmp_path,
    snapshot_factory,
) -> None:
    """目标分支变化应复用代次，源分支变化和回退都必须推进代次。"""

    store = StateStore(tmp_path / "state.db")
    store.initialize()
    first = snapshot_factory(head_sha="a" * 40, target_head_sha="1" * 40)
    initial_event = detect_events(None, first, emit_initial=True)[0]
    store.save_snapshot_and_events(first, [initial_event])

    target_changed = snapshot_factory(
        head_sha="a" * 40,
        target_head_sha="2" * 40,
    )
    target_event = detect_target_branch_event(
        first,
        target_changed,
        batch_id="target-change",
        occurred_at=target_changed.updated_at,
    )[0]
    store.save_snapshot_and_events(target_changed, [target_event])
    assert store.source_generation("demo", 7) == 1

    source_changed = snapshot_factory(
        head_sha="b" * 40,
        target_head_sha="2" * 40,
    )
    source_events = detect_events(target_changed, source_changed)
    store.save_snapshot_and_events(source_changed, source_events)
    assert store.source_generation("demo", 7) == 2

    source_returned = snapshot_factory(
        head_sha="a" * 40,
        target_head_sha="2" * 40,
    )
    returned_events = detect_events(source_changed, source_returned)
    store.save_snapshot_and_events(source_returned, returned_events)
    assert store.source_generation("demo", 7) == 3

    with store.connect() as connection:
        generations = [
            int(row["generation"])
            for row in connection.execute(
                """
                SELECT json_extract(payload, '$.source_generation') AS generation
                FROM event_inbox
                WHERE repository_id = 'demo' AND number = 7
                ORDER BY created_at ASC
                """
            ).fetchall()
        ]
    assert 1 in generations
    assert 2 in generations
    assert 3 in generations


async def test_managed_comment_updates_appends_and_recreates_deleted_comment(
    tmp_path,
    snapshot_factory,
    configured_app_factory,
    monkeypatch,
) -> None:
    """同代次应更新，下一代应创建，远端删除后下次发布应重建。"""

    config = configured_app_factory()
    agent = config.agents["security-reviewer"]
    agent.managed_comment = True
    agent.managed_comment_slot = "stable-review-slot"
    agent.write_scopes = ["change_request"]
    store = StateStore(config.database.path)
    store.initialize()
    comments: dict[str, str] = {}
    created: list[str] = []
    updated: list[str] = []

    class FakeProvider:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def create_change_request_comment(
            self,
            _repository,
            _number,
            body,
        ):
            comment_id = str(len(created) + 1)
            created.append(comment_id)
            comments[comment_id] = body
            return comment_id

        async def update_change_request_comment(
            self,
            _repository,
            comment_id,
            body,
            **_kwargs,
        ):
            if comment_id not in comments:
                return False
            comments[comment_id] = body
            updated.append(comment_id)
            return True

    monkeypatch.setattr(
        "teamwork_review_agents.managed_comments.create_provider",
        lambda *_args, **_kwargs: FakeProvider(),
    )
    monkeypatch.setenv("GITHUB_TOKEN", "provider-token")
    service = ManagedCommentService(config, store)
    snapshot = snapshot_factory(provider="github-main", repository_id="demo")
    event = detect_events(None, snapshot, emit_initial=True)[0].model_copy(
        update={"source_generation": 1}
    )
    context = InvocationContext(
        config_path=str(config.config_path),
        current_agent="security-reviewer",
        run_id="run-one",
        root_run_id="run-one",
        event=event,
    )

    first = await service.publish_agent_comment(context, "第一次审核")
    second = await service.publish_agent_comment(context, "更新后的审核")
    unchanged = await service.publish_agent_comment(context, "更新后的审核")
    assert first["action"] == "created"
    assert second["action"] == "updated"
    assert unchanged["action"] == "unchanged"
    assert created == ["1"]
    assert updated == ["1", "1"]

    next_context = context.model_copy(
        update={
            "event": event.model_copy(update={"source_generation": 2}),
        }
    )
    appended = await service.publish_agent_comment(next_context, "新提交审核")
    assert appended["action"] == "created"
    assert created == ["1", "2"]

    del comments["2"]
    recreated = await service.publish_agent_comment(next_context, "新提交审核")
    assert recreated["action"] == "recreated"
    assert created == ["1", "2", "3"]
