"""分层环境、配置管理和 Web API 测试。"""

from __future__ import annotations

import os

import pytest
import yaml
from fastapi.testclient import TestClient

from teamwork_review_agents.config import load_config
from teamwork_review_agents.config_manager import ConfigManager, ConfigRevisionConflict
from teamwork_review_agents.codex_settings import read_user_inherited_settings
from teamwork_review_agents.environment import (
    MASK,
    SecretRedactor,
    render_prompt,
    resolve_environment,
    resolve_provider_token,
)
from teamwork_review_agents.events import detect_events
from teamwork_review_agents.models import AgentResult, ChangeRequestActivity
from teamwork_review_agents.webapp import create_app


def write_config(tmp_path):
    """创建包含两级 Secret 和内联 Prompt 的最小配置。"""

    first_workspace = tmp_path / "first"
    second_workspace = tmp_path / "second"
    first_workspace.mkdir()
    second_workspace.mkdir()
    document = {
        "database": {"path": str(tmp_path / "state.db")},
        "scanner": {"interval_seconds": 60},
        "environment": {
            "global": {
                "LEVEL": "global",
                "GITHUB_TEST_TOKEN": {
                    "value": "provider-secret",
                    "secret": False,
                    "expose_to_prompt": True,
                    "expose_to_process": True,
                },
                "GLOBAL_SECRET": {
                    "from_system": "TEST_GLOBAL_SECRET",
                    "secret": True,
                },
            }
        },
        "providers": {
            "provider-main": {
                "kind": "github",
                "base_url": "https://api.github.com",
                "token_env": "GITHUB_TEST_TOKEN",
            }
        },
        "repositories": [
            {
                "id": "first",
                "provider": "provider-main",
                "project": "owner/first",
                "workspace": str(first_workspace),
                "environment": {
                    "LEVEL": "repository",
                    "REPOSITORY_SECRET": {
                        "value": "first-secret",
                        "secret": True,
                    },
                },
            },
            {
                "id": "second",
                "provider": "provider-main",
                "project": "owner/second",
                "workspace": str(second_workspace),
                "environment": {
                    "REPOSITORY_SECRET": {
                        "value": "second-secret",
                        "secret": True,
                    }
                },
            },
        ],
        "agents": {
            "reviewer": {
                "prompt": "级别=${{LEVEL}} Secret=${{GLOBAL_SECRET}} Missing=${{MISSING}}",
                "sandbox": "read-only",
                "environment": {
                    "LEVEL": "agent",
                    "MR_NUMBER": "不能覆盖运行变量",
                },
            }
        },
        "rules": [
            {
                "name": "review",
                "events": ["change_request.discovered"],
                "agents": ["reviewer"],
            }
        ],
    }
    path = tmp_path / "config.yaml"
    path.write_text(
        yaml.safe_dump(document, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    return path


def test_environment_precedence_template_and_redaction(
    tmp_path,
    monkeypatch,
    snapshot_factory,
) -> None:
    config_path = write_config(tmp_path)
    monkeypatch.setenv("TEST_GLOBAL_SECRET", "system-secret")
    config = load_config(config_path)
    repository = config.repository_map()["first"]
    snapshot = snapshot_factory(
        provider=repository.provider,
        repository_id=repository.id,
    )
    event = detect_events(None, snapshot, emit_initial=True)[0]
    resolved = resolve_environment(
        config,
        repository,
        config.agents["reviewer"],
        event,
        "run-test",
    )

    assert resolved.all_values["LEVEL"] == "agent"
    assert resolved.all_values["MR_NUMBER"] == "7"
    assert resolved.prompt_values["GLOBAL_SECRET"] == ""
    assert resolved.process_values["GLOBAL_SECRET"] == "system-secret"
    assert resolved.audit_values["GLOBAL_SECRET"] == MASK
    assert resolved.prompt_values["GITHUB_TEST_TOKEN"] == ""
    assert "GITHUB_TEST_TOKEN" not in resolved.process_values
    assert resolved.audit_values["GITHUB_TEST_TOKEN"] == MASK
    assert render_prompt("${{LEVEL}}/${{MISSING}}", resolved.prompt_values) == "agent/"
    assert SecretRedactor(resolved.secret_values).text(
        "system-secret first-secret"
    ) == f"{MASK} {MASK}"

    delegated = resolve_environment(
        config,
        repository,
        config.agents["reviewer"],
        event,
        "run-child",
        include_change_request=False,
    )
    assert delegated.all_values["REPOSITORY_ID"] == repository.id
    assert delegated.all_values["RUN_ID"] == "run-child"
    assert "MR_TITLE" not in delegated.all_values
    assert "EVENT_TYPE" not in delegated.process_values


def test_config_manager_masks_and_merges_reordered_repositories(tmp_path) -> None:
    config_path = write_config(tmp_path)
    manager = ConfigManager(config_path)
    document = manager.document()
    provider_token = document["environment"]["global"]["GITHUB_TEST_TOKEN"]
    assert provider_token["value"] == MASK
    assert provider_token["secret"] is True
    assert provider_token["expose_to_prompt"] is False
    assert provider_token["expose_to_process"] is False
    assert document["repositories"][0]["environment"]["REPOSITORY_SECRET"]["value"] == MASK
    assert document["repositories"][1]["environment"]["REPOSITORY_SECRET"]["value"] == MASK

    document["repositories"].reverse()
    manager.save(document)
    saved = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    secrets = {
        item["id"]: item["environment"]["REPOSITORY_SECRET"]["value"]
        for item in saved["repositories"]
    }
    assert secrets == {"first": "first-secret", "second": "second-secret"}
    saved_provider_token = saved["environment"]["global"]["GITHUB_TEST_TOKEN"]
    assert saved_provider_token["value"] == "provider-secret"
    assert saved_provider_token["secret"] is True
    assert saved_provider_token["expose_to_prompt"] is False
    assert saved_provider_token["expose_to_process"] is False
    versions = manager.store.list_config_versions()
    assert versions
    assert MASK in manager.store.get_config_version(versions[0]["revision"])["content"]
    assert "first-secret" not in manager.store.get_config_version(versions[0]["revision"])["content"]
    assert "provider-secret" not in manager.store.get_config_version(versions[0]["revision"])["content"]


def test_config_manager_saves_one_agent_and_updates_references(tmp_path) -> None:
    """单 Agent 保存应保护版本，并原子维护全部名称引用。"""

    config_path = write_config(tmp_path)
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    raw["agents"]["helper"] = {
        "prompt": "协助审核。",
        "sandbox": "read-only",
        "allowed_sub_agents": ["reviewer"],
    }
    raw["rules"][0]["agents"].append("helper")
    config_path.write_text(
        yaml.safe_dump(raw, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    manager = ConfigManager(config_path)
    revision = manager.config.revision
    reviewer = manager.document()["agents"]["reviewer"]
    reviewer["prompt"] = "执行独立审核。"

    updated = manager.save_agent(
        expected_revision=revision,
        original_name="reviewer",
        name="primary-reviewer",
        agent=reviewer,
    )
    document = manager.document(mask_secrets=False)
    assert "reviewer" not in document["agents"]
    assert document["agents"]["primary-reviewer"]["prompt"] == "执行独立审核。"
    assert document["agents"]["helper"]["allowed_sub_agents"] == [
        "primary-reviewer"
    ]
    assert document["rules"][0]["agents"] == ["primary-reviewer", "helper"]

    with pytest.raises(ConfigRevisionConflict):
        manager.delete_agent(expected_revision=revision, name="helper")

    deleted = manager.delete_agent(
        expected_revision=updated.revision,
        name="helper",
    )
    document = manager.document(mask_secrets=False)
    assert "helper" not in deleted.agents
    assert document["rules"][0]["agents"] == ["primary-reviewer"]


def test_config_manager_saves_one_rule_without_changing_order(tmp_path) -> None:
    """单规则保存应保护版本，并在重命名时保持原匹配位置。"""

    config_path = write_config(tmp_path)
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    raw["rules"].append(
        {
            "name": "secondary",
            "events": ["change_request.updated"],
            "agents": ["reviewer"],
            "enabled": True,
        }
    )
    config_path.write_text(
        yaml.safe_dump(raw, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    manager = ConfigManager(config_path)
    revision = manager.config.revision
    rule = manager.document()["rules"][0]
    rule["events"] = ["change_request.opened"]
    rule["enabled"] = False

    updated = manager.save_rule(
        expected_revision=revision,
        original_name="review",
        name="primary-review",
        rule=rule,
    )
    document = manager.document(mask_secrets=False)
    assert [item["name"] for item in document["rules"]] == [
        "primary-review",
        "secondary",
    ]
    assert document["rules"][0]["events"] == ["change_request.opened"]
    assert document["rules"][0]["enabled"] is False

    with pytest.raises(ConfigRevisionConflict):
        manager.delete_rule(expected_revision=revision, name="secondary")

    manager.delete_rule(
        expected_revision=updated.revision,
        name="secondary",
    )
    assert [item["name"] for item in manager.document()["rules"]] == [
        "primary-review"
    ]


def test_config_manager_saves_and_safely_deletes_one_repository(tmp_path) -> None:
    """单仓库保存应保持身份和顺序，并阻止删除规则引用。"""

    config_path = write_config(tmp_path)
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    raw["rules"][0]["repositories"] = ["first"]
    config_path.write_text(
        yaml.safe_dump(raw, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    manager = ConfigManager(config_path)
    revision = manager.config.revision
    repository = manager.document()["repositories"][0]
    repository["enabled"] = False

    updated = manager.save_repository(
        expected_revision=revision,
        original_id="first",
        repository_id="first",
        repository=repository,
    )
    document = manager.document(mask_secrets=False)
    assert [item["id"] for item in document["repositories"]] == [
        "first",
        "second",
    ]
    assert document["repositories"][0]["enabled"] is False

    with pytest.raises(ValueError, match="ID 不允许修改"):
        manager.save_repository(
            expected_revision=updated.revision,
            original_id="first",
            repository_id="renamed",
            repository=repository,
        )

    with pytest.raises(ValueError, match="仍被触发规则引用"):
        manager.delete_repository(
            expected_revision=updated.revision,
            repository_id="first",
        )

    with pytest.raises(ConfigRevisionConflict):
        manager.delete_repository(
            expected_revision=revision,
            repository_id="second",
        )

    manager.delete_repository(
        expected_revision=updated.revision,
        repository_id="second",
    )
    assert [item["id"] for item in manager.document()["repositories"]] == [
        "first"
    ]


def test_provider_token_uses_global_config_then_host_fallback(tmp_path, monkeypatch) -> None:
    """Provider Token 应按全局固定值、全局宿主机引用、直接宿主机依次解析。"""

    config_path = write_config(tmp_path)
    monkeypatch.setenv("GITHUB_TEST_TOKEN", "host-fallback-token")
    config = load_config(config_path)
    provider = config.providers["provider-main"]
    assert resolve_provider_token(config, provider) == "provider-secret"

    document = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    document["environment"]["global"]["GITHUB_TEST_TOKEN"] = {
        "from_system": "TEAMWORK_GITHUB_TOKEN",
    }
    monkeypatch.setenv("TEAMWORK_GITHUB_TOKEN", "referenced-host-token")
    config_path.write_text(
        yaml.safe_dump(document, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    config = load_config(config_path)
    assert resolve_provider_token(config, config.providers["provider-main"]) == "referenced-host-token"

    del document["environment"]["global"]["GITHUB_TEST_TOKEN"]
    config_path.write_text(
        yaml.safe_dump(document, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    config = load_config(config_path)
    assert resolve_provider_token(config, config.providers["provider-main"]) == "host-fallback-token"


def test_web_api_saves_agents_independently_with_revision_guard(tmp_path) -> None:
    """Agent 管理 API 应支持创建、重命名、删除和版本冲突。"""

    config_path = write_config(tmp_path)
    app = create_app(config_path, start_scheduler=False)
    with TestClient(app) as client:
        current = client.get("/api/config").json()
        created = client.post(
            "/api/config/agents",
            json={
                "revision": current["revision"],
                "name": "helper",
                "agent": {
                    "prompt": "协助审核。",
                    "sandbox": "read-only",
                    "allowed_sub_agents": ["reviewer"],
                },
            },
        )
        assert created.status_code == 200
        created_body = created.json()
        assert "helper" in created_body["document"]["agents"]

        renamed = client.put(
            "/api/config/agents/helper",
            json={
                "revision": created_body["revision"],
                "name": "assistant",
                "agent": created_body["document"]["agents"]["helper"],
            },
        )
        assert renamed.status_code == 200
        renamed_body = renamed.json()
        assert "helper" not in renamed_body["document"]["agents"]
        assert "assistant" in renamed_body["document"]["agents"]

        conflict = client.request(
            "DELETE",
            "/api/config/agents/assistant",
            json={"revision": current["revision"]},
        )
        assert conflict.status_code == 409

        deleted = client.request(
            "DELETE",
            "/api/config/agents/assistant",
            json={"revision": renamed_body["revision"]},
        )
        assert deleted.status_code == 200
        assert "assistant" not in deleted.json()["document"]["agents"]


def test_web_api_saves_rules_independently_with_revision_guard(tmp_path) -> None:
    """触发规则管理 API 应支持启停、创建、重命名、删除和版本冲突。"""

    config_path = write_config(tmp_path)
    app = create_app(config_path, start_scheduler=False)
    with TestClient(app) as client:
        current = client.get("/api/config").json()
        created = client.post(
            "/api/config/rules",
            json={
                "revision": current["revision"],
                "name": "secondary",
                "rule": {
                    "name": "secondary",
                    "events": ["change_request.updated"],
                    "agents": ["reviewer"],
                    "enabled": True,
                },
            },
        )
        assert created.status_code == 200
        created_body = created.json()
        assert [item["name"] for item in created_body["document"]["rules"]] == [
            "review",
            "secondary",
        ]

        rule = created_body["document"]["rules"][1]
        rule["enabled"] = False
        toggled = client.put(
            "/api/config/rules/secondary",
            json={
                "revision": created_body["revision"],
                "name": "secondary",
                "rule": rule,
            },
        )
        assert toggled.status_code == 200
        toggled_body = toggled.json()
        assert toggled_body["document"]["rules"][0] == created_body["document"][
            "rules"
        ][0]
        assert toggled_body["document"]["rules"][1]["enabled"] is False

        renamed = client.put(
            "/api/config/rules/secondary",
            json={
                "revision": toggled_body["revision"],
                "name": "manual-review",
                "rule": rule,
            },
        )
        assert renamed.status_code == 200
        renamed_body = renamed.json()
        assert [item["name"] for item in renamed_body["document"]["rules"]] == [
            "review",
            "manual-review",
        ]
        assert renamed_body["document"]["rules"][1]["enabled"] is False

        conflict = client.request(
            "DELETE",
            "/api/config/rules/manual-review",
            json={"revision": current["revision"]},
        )
        assert conflict.status_code == 409

        deleted = client.request(
            "DELETE",
            "/api/config/rules/manual-review",
            json={"revision": renamed_body["revision"]},
        )
        assert deleted.status_code == 200
        assert [item["name"] for item in deleted.json()["document"]["rules"]] == [
            "review"
        ]


def test_web_api_saves_repositories_independently_and_blocks_references(
    tmp_path,
) -> None:
    """仓库管理 API 应独立保存，并保护身份、版本和规则引用。"""

    config_path = write_config(tmp_path)
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    raw["rules"][0]["repositories"] = ["first"]
    config_path.write_text(
        yaml.safe_dump(raw, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    app = create_app(config_path, start_scheduler=False)
    with TestClient(app) as client:
        current = client.get("/api/config").json()
        created = client.post(
            "/api/config/repositories",
            json={
                "revision": current["revision"],
                "repository_id": "third",
                "repository": {
                    "id": "third",
                    "provider": "provider-main",
                    "project": "owner/third",
                    "workspace": "./workspaces/third",
                    "enabled": False,
                    "environment": {},
                },
            },
        )
        assert created.status_code == 200
        created_body = created.json()
        assert [item["id"] for item in created_body["document"]["repositories"]] == [
            "first",
            "second",
            "third",
        ]

        repository = created_body["document"]["repositories"][2]
        repository["project"] = "owner/third-updated"
        updated = client.put(
            "/api/config/repositories/third",
            json={
                "revision": created_body["revision"],
                "repository_id": "third",
                "repository": repository,
            },
        )
        assert updated.status_code == 200
        updated_body = updated.json()
        assert updated_body["document"]["repositories"][2]["project"] == (
            "owner/third-updated"
        )

        renamed = client.put(
            "/api/config/repositories/third",
            json={
                "revision": updated_body["revision"],
                "repository_id": "renamed",
                "repository": {**repository, "id": "renamed"},
            },
        )
        assert renamed.status_code == 422

        referenced = client.request(
            "DELETE",
            "/api/config/repositories/first",
            json={"revision": updated_body["revision"]},
        )
        assert referenced.status_code == 422
        assert "review" in referenced.json()["detail"]

        conflict = client.request(
            "DELETE",
            "/api/config/repositories/third",
            json={"revision": current["revision"]},
        )
        assert conflict.status_code == 409

        deleted = client.request(
            "DELETE",
            "/api/config/repositories/third",
            json={"revision": updated_body["revision"]},
        )
        assert deleted.status_code == 200
        assert [item["id"] for item in deleted.json()["document"]["repositories"]] == [
            "first",
            "second",
        ]


def test_web_api_config_preview_logs_and_static_ui(tmp_path, snapshot_factory) -> None:
    config_path = write_config(tmp_path)
    app = create_app(config_path, start_scheduler=False)
    with TestClient(app) as client:
        health = client.get("/api/health").json()
        assert health["status"] == "ok"
        assert health["pid"] == os.getpid()
        response = client.get("/api/config")
        assert response.status_code == 200
        document = response.json()["document"]
        assert document["repositories"][0]["environment"]["REPOSITORY_SECRET"]["value"] == MASK

        preview = client.post(
            "/api/prompts/preview",
            json={"template": "A=${{A}}, B=${{B}}", "variables": {"A": "1"}},
        )
        assert preview.json()["rendered"] == "A=1, B="

        assert client.get("/api/prompt-files").json() == []
        imported = client.post(
            "/api/prompt-files/import",
            files={"file": ("review prompt.md", "请审查当前变更。", "text/markdown")},
        )
        assert imported.status_code == 200
        assert imported.json()["path"] == "./prompts/review-prompt.md"
        assert client.get("/api/prompt-files").json()[0]["name"] == "review-prompt.md"

        duplicate = client.post(
            "/api/prompt-files/import",
            files={"file": ("review prompt.md", "请审查当前变更。", "text/markdown")},
        )
        assert duplicate.json()["path"] == "./prompts/review-prompt.md"

        renamed = client.post(
            "/api/prompt-files/import",
            files={"file": ("review prompt.md", "不同的内容", "text/markdown")},
        )
        assert renamed.json()["path"] == "./prompts/review-prompt-2.md"

        unsupported = client.post(
            "/api/prompt-files/import",
            files={"file": ("prompt.json", "{}", "application/json")},
        )
        assert unsupported.status_code == 422

        invalid = document | {
            "agents": {
                **document["agents"],
                "reviewer": {**document["agents"]["reviewer"], "prompt": ""},
            }
        }
        assert client.put("/api/config", json={"document": invalid}).status_code == 422

        store = app.state.config_manager.store
        snapshot = snapshot_factory(
            provider="provider-main",
            repository_id="first",
            number=12,
            title="等待首次事件的 PR",
        )
        store.save_snapshot_and_events(snapshot, [])
        change_requests = client.get("/api/change-requests").json()
        assert change_requests[0]["number"] == 12
        assert change_requests[0]["discovered_event_emitted"] is False
        assert change_requests[0]["latest_event_supported"] is True
        assert change_requests[0]["latest_event_checked"] is False
        assert client.get("/api/status").json()["stats"]["change_requests"] == {
            "total": 1,
            "opened": 1,
        }

        emitted = client.post(
            "/api/change-requests/first/12/emit-discovered"
        ).json()
        assert emitted["created"] is True
        repeated = client.post(
            "/api/change-requests/first/12/emit-discovered"
        ).json()
        assert repeated == {"created": False, "reason": "首次发现事件已经存在"}
        assert client.get("/api/change-requests").json()[0][
            "discovered_event_emitted"
        ] is True
        assert len(store.pending_events()) == 1
        event_record = client.get("/api/events").json()[0]
        assert event_record["status"] == "pending"
        assert event_record["trigger_count"] == 0
        assert event_record["agent_running_count"] == 0
        assert client.post(
            "/api/change-requests/first/999/emit-discovered"
        ).status_code == 404

        latest_activity = ChangeRequestActivity(
            id="timeline-merged",
            type="merged",
            occurred_at="2026-08-17T08:00:00Z",
        )
        store.save_activity_cursor(
            snapshot.provider,
            snapshot.repository_id,
            snapshot.number,
            {
                "page": 3,
                "item_id": latest_activity.id,
                "latest_activity_checked": True,
                "latest_activity": latest_activity.model_dump(mode="json"),
            },
        )
        latest_event = client.get("/api/change-requests").json()[0]["latest_event"]
        assert latest_event["event_type"] == "change_request.merged"
        assert latest_event["provider_event_id"] == "timeline-merged"
        assert client.get("/api/change-requests").json()[0][
            "latest_event_checked"
        ] is True

        first_manual = client.post(
            "/api/change-requests/first/12/trigger-latest-event"
        )
        second_manual = client.post(
            "/api/change-requests/first/12/trigger-latest-event"
        )
        assert first_manual.status_code == 200
        assert second_manual.status_code == 200
        assert first_manual.json()["event_type"] == "change_request.merged"
        assert first_manual.json()["event_id"] != second_manual.json()["event_id"]
        manual_records = [
            item for item in client.get("/api/events").json()
            if item["origin"] == "manual"
        ]
        assert len(manual_records) == 2
        assert {item["source_activity_id"] for item in manual_records} == {
            "timeline-merged"
        }

        no_latest_snapshot = snapshot_factory(
            provider="provider-main",
            repository_id="first",
            number=13,
        )
        store.save_snapshot_and_events(no_latest_snapshot, [])
        assert client.post(
            "/api/change-requests/first/13/trigger-latest-event"
        ).status_code == 409

        committed_snapshot = snapshot_factory(
            provider="provider-main",
            repository_id="second",
            number=14,
        )
        store.save_snapshot_and_events(committed_snapshot, [])
        committed_activity = ChangeRequestActivity(
            id="timeline-committed",
            type="committed",
            occurred_at="2026-08-18T09:00:00Z",
        )
        store.save_activity_cursor(
            committed_snapshot.provider,
            committed_snapshot.repository_id,
            committed_snapshot.number,
            {
                "item_id": committed_activity.id,
                "latest_activity_checked": True,
                "latest_activity": committed_activity.model_dump(mode="json"),
            },
        )
        dispatch_calls: list[bool] = []
        app.state.runtime.dispatch_events_now = lambda: dispatch_calls.append(True)
        batch_manual = client.post(
            "/api/change-requests/trigger-latest-events",
            json={
                "targets": [
                    {"repository_id": "first", "number": 12},
                    {"repository_id": "second", "number": 14},
                    {"repository_id": "first", "number": 12},
                    {"repository_id": "first", "number": 13},
                ]
            },
        )
        assert batch_manual.status_code == 200
        batch_body = batch_manual.json()
        assert batch_body["requested"] == 4
        assert batch_body["created"] == 2
        assert batch_body["failed"] == 2
        assert [item["event_type"] for item in batch_body["results"] if item["created"]] == [
            "change_request.merged",
            "change_request.commits_changed",
        ]
        assert batch_body["results"][2]["reason"] == "批量请求中存在重复目标"
        assert batch_body["results"][3]["status_code"] == 409
        assert dispatch_calls == [True]
        assert client.post(
            "/api/change-requests/trigger-latest-events",
            json={"targets": []},
        ).status_code == 422

        reservation = store.begin_agent_run(
            proposed_run_id="run-web",
            root_run_id=None,
            parent_run_id=None,
            idempotency_key="web-test",
            event_id=None,
            rule_name="review",
            agent_name="reviewer",
            resource_key="github:first:7",
            prompt="测试 Prompt",
            environment={"SECRET": MASK},
            config_revision="revision-test",
            max_attempts=1,
        )
        assert reservation is not None
        store.append_run_log(
            "run-web",
            stream="stdout",
            event_type="item.completed",
            payload={"message": "完成"},
        )
        store.finish_agent_run(
            AgentResult(
                run_id="run-web",
                root_run_id="run-web",
                agent_name="reviewer",
                status="completed",
                final_message="完成",
            )
        )
        assert client.get("/api/runs/run-web").json()["environment"] == {"SECRET": MASK}
        assert client.get("/api/runs/run-web/logs").json()[0]["event_type"] == "item.completed"
        stream = client.get("/api/runs/run-web/stream")
        assert "event: item.completed" in stream.text
        assert "event: end" in stream.text

        cancellable = store.begin_agent_run(
            proposed_run_id="run-web-cancel",
            root_run_id=None,
            parent_run_id=None,
            idempotency_key="web-cancel-test",
            event_id=None,
            rule_name="review",
            agent_name="reviewer",
            resource_key="github:first:12",
            prompt="等待执行",
            max_attempts=1,
        )
        assert cancellable is not None
        cancelled = client.post("/api/runs/run-web-cancel/cancel")
        assert cancelled.status_code == 200
        assert cancelled.json()["accepted"] is True
        assert client.get("/api/runs/run-web-cancel").json()["status"] == "cancelled"
        cancel_logs = client.get("/api/runs/run-web-cancel/logs").json()
        assert cancel_logs[-1]["event_type"] == "run.cancel_requested"
        assert client.post("/api/runs/unknown/cancel").status_code == 404

        static = client.get("/")
        assert static.status_code == 200
        assert "Teamwork Review Agents" in static.text


def test_overview_api_filters_status_repository_and_limit(
    tmp_path,
    snapshot_factory,
) -> None:
    """概览 API 应组合过滤条件并支持自定义数量与全部模式。"""

    config_path = write_config(tmp_path)
    app = create_app(config_path, start_scheduler=False)
    with TestClient(app) as client:
        store = app.state.config_manager.store
        first = snapshot_factory(
            provider="provider-main",
            repository_id="first",
            number=1,
            state="opened",
            updated_at="2026-08-18T10:00:00Z",
        )
        second = snapshot_factory(
            provider="provider-main",
            repository_id="second",
            number=2,
            state="closed",
            updated_at="2026-08-18T09:00:00Z",
        )
        first_event = detect_events(None, first, emit_initial=True)[0]
        second_event = detect_events(None, second, emit_initial=True)[0]
        store.save_snapshot_and_events(first, [first_event])
        store.save_snapshot_and_events(second, [second_event])
        assert store.claim_event(second_event.id, 2)
        store.record_event_dispatches([second_event.id], [])

        assert len(client.get("/api/change-requests?limit=1").json()) == 1
        all_snapshots = client.get(
            "/api/change-requests?all_records=true"
        ).json()
        assert [item["snapshot_key"] for item in all_snapshots] == [
            first.key,
            second.key,
        ]
        filtered_snapshots = client.get(
            "/api/change-requests?repository_id=second&status=closed&limit=10"
        ).json()
        assert [item["snapshot_key"] for item in filtered_snapshots] == [second.key]

        assert len(client.get("/api/events?limit=1").json()) == 1
        all_events = client.get("/api/events?all_records=true").json()
        assert [item["event_id"] for item in all_events] == [
            first_event.id,
            second_event.id,
        ]
        filtered_events = client.get(
            "/api/events?repository_id=second&status=unmatched&limit=10"
        ).json()
        assert [item["event_id"] for item in filtered_events] == [second_event.id]
        assert client.get("/api/events?status=unknown").status_code == 422
        assert client.get("/api/change-requests?status=unknown").status_code == 422


def test_codex_runtime_options_report_catalog_and_user_model(
    tmp_path,
    monkeypatch,
) -> None:
    """运行时接口应返回本机模型目录和可验证的用户模型来源。"""

    config_path = write_config(tmp_path)
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    (codex_home / "config.toml").write_text(
        'model = "gpt-user"\n'
        'model_reasoning_effort = "high"\n'
        'service_tier = "fast"\n'
        'model_verbosity = "low"\n'
        'personality = "friendly"\n'
        'web_search = "live"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    fake_codex = tmp_path / "fake-codex"
    fake_codex.write_text(
        "#!/bin/sh\n"
        "printf '%s' '{\"models\":[{\"slug\":\"gpt-test\",\"display_name\":\"GPT Test\",\"default_reasoning_level\":\"medium\",\"supported_reasoning_levels\":[{\"effort\":\"low\"},{\"effort\":\"medium\"}],\"additional_speed_tiers\":[\"fast\"]}]}'\n",
        encoding="utf-8",
    )
    fake_codex.chmod(0o755)
    document = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    document["runtime"] = {"codex_binary": str(fake_codex)}
    config_path.write_text(
        yaml.safe_dump(document, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )

    async def effective_config(*_args):
        return {
            "model": "gpt-user",
            "model_reasoning_effort": "high",
            "service_tier": "fast",
            "model_verbosity": "low",
            "personality": "friendly",
            "web_search": "live",
            "credential": "不得返回的配置",
        }

    monkeypatch.setattr(
        "teamwork_review_agents.webapp.read_codex_effective_config",
        effective_config,
    )

    app = create_app(config_path, start_scheduler=False)
    with TestClient(app) as client:
        result = client.get("/api/codex/runtime-options").json()

    assert result["models"][0]["slug"] == "gpt-test"
    assert result["models"][0]["supported_reasoning_levels"] == ["low", "medium"]
    assert result["models"][0]["supports_fast_mode"] is True
    assert result["user_model"] == "gpt-user"
    assert result["inherited_model"] == {
        "value": "gpt-user",
        "source": "codex",
        "label": "继承 Codex 有效配置（gpt-user）",
    }
    assert result["codex_model"] == "gpt-user"
    assert result["codex_model_source"] == "codex"
    assert result["inherited_settings"] == {
        "model_reasoning_effort": {
            "value": "high",
            "source": "codex",
            "known": True,
        },
        "fast_mode": {"value": "fast", "source": "codex", "known": True},
        "model_verbosity": {
            "value": "low",
            "source": "codex",
            "known": True,
        },
        "personality": {
            "value": "friendly",
            "source": "codex",
            "known": True,
        },
        "web_search": {"value": "live", "source": "codex", "known": True},
    }
    assert "credential" not in result
    assert "不得返回的配置" not in str(result)


def test_codex_runtime_options_use_known_defaults_and_unknown_markers(
    tmp_path,
    monkeypatch,
) -> None:
    """未配置的公开默认应解析，未公开值应保持未知。"""

    config_path = write_config(tmp_path)
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    (codex_home / "config.toml").write_text("", encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    fake_codex = tmp_path / "fake-codex"
    fake_codex.write_text(
        "#!/bin/sh\n"
        "printf '%s' '{\"models\":[]}'\n",
        encoding="utf-8",
    )
    fake_codex.chmod(0o755)
    document = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    document["runtime"] = {"codex_binary": str(fake_codex)}
    config_path.write_text(
        yaml.safe_dump(document, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )

    async def effective_config(*_args):
        return {}

    monkeypatch.setattr(
        "teamwork_review_agents.webapp.read_codex_effective_config",
        effective_config,
    )

    app = create_app(config_path, start_scheduler=False)
    with TestClient(app) as client:
        settings = client.get("/api/codex/runtime-options").json()[
            "inherited_settings"
        ]

    assert settings["fast_mode"] == {
        "value": "standard",
        "source": "builtin",
        "known": True,
    }
    assert settings["web_search"] == {
        "value": "cached",
        "source": "builtin",
        "known": True,
    }
    assert settings["model_reasoning_effort"] == {
        "value": None,
        "source": "unknown",
        "known": False,
    }
    assert settings["model_verbosity"]["known"] is False
    assert settings["personality"]["known"] is False


def test_user_config_fallback_does_not_guess_missing_layered_values(tmp_path) -> None:
    """无法读取完整配置分层时，缺失字段必须保持未知。"""

    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    (codex_home / "config.toml").write_text("", encoding="utf-8")

    settings, error = read_user_inherited_settings(codex_home)

    assert error is None
    assert settings["fast_mode"] == {
        "value": None,
        "source": "unknown",
        "known": False,
    }
    assert settings["web_search"] == {
        "value": None,
        "source": "unknown",
        "known": False,
    }
